"""One-off fixed-geometry WeChat contact exporter.

This task-specific runner uses the Weixin client rectangle, OCR-derived row
centers, a single row spacing calibration, and a stable profile-header hash.
Screenshots stay in memory; logs and diagnostics never contain OCR text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

from . import _PROJECT_SRC

if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from PIL import ImageEnhance, ImageOps

from pyweixin.visual_navigation import PROFILE_CHANGE_ROI, Win32VisualNavigation
from wechat_ocr.config import DEFAULT_CONFIG
from wechat_ocr.engine import get_ocr_engine
from wechat_ocr.masking import contains_mask_placeholder
from wechat_ocr.validator import normalize_candidate, validate_wechat_id

# Weixin 4.x client: narrow list starts just after the icon rail and ends
# before the large profile/content area. Values are client-relative fractions.
CONTACT_LIST_ROI = (0.031, 0.20, 0.158, 0.98)
# WeChat's contact pane boundary is distinct from the narrow OCR/row crop above.
# Detect the scrollbar near the pane's measured right edge, with a small range
# for DPI rounding; looking beside the text crop mistakes row content for a thumb.
CONTACT_SCROLLBAR_ROI = (0.30, 0.12, 0.335, 0.98)
FORMAL_FIELDS = ["row_ordinal", "wechat_id", "status", "source", "confidence"]
REVIEW_FIELDS = [
    "sequence_index", "viewport", "row_ordinal", "status", "reason",
    "candidate_masked", "confidence", "frame_hash_prefix",
]
ACCOUNTING_KEYS = (
    "confirmed_unique", "confirmed_duplicate", "label_not_found",
    "value_not_found", "low_confidence", "source_value_masked",
    "ocr_disagreement", "frame_mismatch",
    "ocr_failed", "profile_not_changed", "row_click_failed", "invalid_candidate",
    "parser_rejected", "other", "unaccounted",
)
OUT = Path("output/final")
CHECKPOINT = OUT / "fixed_geometry_checkpoint.json"
LIVE = OUT / "fixed_geometry_live_status.json"
OUTPUT_STEM = "wechat_contacts_150"
PROGRESS = OUT / f"{OUTPUT_STEM}.progress.json"
BENCHMARK_ROOT = Path("output/benchmark_navigation_v2").resolve()
BENCHMARK_MODE = False
BENCHMARK_SEED_PROFILE_HASHES: list[str] = []
BENCHMARK_SEED_CONFIRMED_ID_HASHES: list[str] = []
BENCHMARK_SEED_SCROLL_CONTROLLER: dict = {}
_LIVE_LOCK = threading.Lock()


def _atomic_replace(path: Path, write_content, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="", delete=False,
            dir=path.parent, prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp",
        ) as handle:
            temp = Path(handle.name)
            write_content(handle)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_text(path: Path, content: str, *, encoding: str = "utf-8-sig") -> None:
    _atomic_replace(path, lambda fp: fp.write(content), encoding=encoding)


def _register_profile_hash(state: dict, profile_hash: str) -> str:
    """Return NEW_PROFILE or PROFILE_REVISIT while keeping hashes only."""
    if state.get("pending_forward_viewport"):
        state["pending_forward_viewport_scanned"] = True
        state["row_profile_checks"] = state.get("row_profile_checks", 0) + 1
        attempts = state.get("scroll_attempts", [])
        if attempts:
            attempts[-1]["rows_scanned"] = True
            attempts[-1]["row_profile_checks"] = attempts[-1].get("row_profile_checks", 0) + 1
    seen = state.setdefault("seen_profile_hashes", [])
    if profile_hash in seen:
        state["profile_revisits"] = state.get("profile_revisits", 0) + 1
        state["consecutive_profile_revisits"] = state.get("consecutive_profile_revisits", 0) + 1
        return "PROFILE_REVISIT"
    seen.append(profile_hash)
    state["unique_profiles_visited"] = state.get("unique_profiles_visited", 0) + 1
    state["profiles_visited"] = state["unique_profiles_visited"]
    state["consecutive_profile_revisits"] = 0
    state["scrolls_without_new_profile"] = 0
    if state.get("pending_forward_viewport"):
        state["pending_forward_new_profiles"] = state.get("pending_forward_new_profiles", 0) + 1
        state["forward_progress_probe_new_profiles"] = state.get(
            "forward_progress_probe_new_profiles", 0
        ) + 1
        attempts = state.get("scroll_attempts", [])
        if attempts:
            attempts[-1]["new_profile_found"] = True
            attempts[-1]["new_profile_count"] = attempts[-1].get("new_profile_count", 0) + 1
        if state["unique_profiles_visited"] >= 55 and state.get("forward_progress_probe_started"):
            state["forward_direction_fix"] = "PASS"
            state["forward_progress_probe_recovered"] = True
            state["forward_progress_probe_started"] = False
    return "NEW_PROFILE"


def _mark_forward_progress_recovered(state: dict) -> None:
    state["forward_progress_probe_recovered"] = True
    if state.get("unique_profiles_visited", 0) >= 55:
        state["forward_direction_fix"] = "PASS"
        state["forward_progress_probe_started"] = False
    else:
        state["forward_progress_probe_started"] = True


def _forward_progress_action(state: dict) -> str:
    # A profile revisit can be an ordinary viewport overlap. Only distinct,
    # successfully scrolled list views can contribute to the stall decision.
    return "CONTINUE"


def _next_visible_row(rows: list[float], current_y: float, spacing: float, bottom: float):
    return next((
        row_y for row_y in rows
        if row_y > current_y + spacing * 0.35 and row_y <= bottom - 1
    ), None)


def _viewport_signature(frame, rows: list[float]) -> str:
    list_hash = _contact_list_hash(frame)
    geometry = ",".join(str(round(y)) for y in rows)
    return hashlib.sha256(f"{list_hash}:{geometry}".encode("ascii")).hexdigest()


def _neutralize_green_selection(crop):
    """Remove the green selected-row band from anonymous visual hashes."""
    import numpy as np

    rgb = np.asarray(crop.convert("RGB"), dtype=np.int16)
    green = (
        (rgb[:, :, 1] > 90)
        & (rgb[:, :, 1] > rgb[:, :, 0] * 1.10)
        & (rgb[:, :, 1] > rgb[:, :, 2] * 1.05)
    )
    gray = np.asarray(ImageOps.grayscale(crop), dtype=np.uint8).copy()
    source_gray = gray.copy()
    selected_lines = green.mean(axis=1) > 0.18
    neutral_lines = np.flatnonzero(~selected_lines)
    for y in np.flatnonzero(selected_lines):
        above = neutral_lines[neutral_lines < y]
        below = neutral_lines[neutral_lines > y]
        if above.size or below.size:
            candidates = []
            if above.size:
                candidates.append(int(above[-1]))
            if below.size:
                candidates.append(int(below[0]))
            background = np.median(source_gray[candidates], axis=0).astype(np.uint8)
            gray[y, green[y, :]] = background[green[y, :]]
    from PIL import Image

    return Image.fromarray(gray)


def _record_viewport_signature(state: dict, signature: str) -> bool:
    recent = state.setdefault("recent_viewport_signatures", [])
    cycle = len(recent) >= 2 and signature == recent[-2] and signature != recent[-1]
    repeated = signature in recent
    recent.append(signature)
    del recent[:-3]
    return cycle or repeated


def _record_scroll_progress(
    state: dict,
    signature: str,
    *,
    direction: str = "DIRECTION_UNVERIFIED_VIEWPORT",
    list_changed: bool = True,
) -> str:
    """Count only verified downward movement; list hashes alone are insufficient."""
    if not list_changed and direction != "FORWARD":
        state["scroll_input_failure_count"] = state.get("scroll_input_failure_count", 0) + 1
        return "SCROLL_INPUT_FAILED"
    if direction != "FORWARD":
        state["direction_unverified_viewports"] = state.get("direction_unverified_viewports", 0) + 1
        return "SCROLL_DIRECTION_UNRESOLVED"
    if state.get("pending_forward_viewport"):
        completion = _complete_forward_viewport(state)
        if completion != "CONTINUE":
            return completion
    state["pending_forward_new_profiles"] = 0
    state["pending_forward_viewport"] = True
    state["pending_forward_viewport_scanned"] = False
    state["successful_scroll_count"] = state.get("successful_scroll_count", 0) + 1
    state["verified_downward_scrolls"] = state.get("verified_downward_scrolls", 0) + 1
    state["last_scroll_unique_profiles"] = int(state.get("unique_profiles_visited", 0))
    recent = state.setdefault("recent_viewport_signatures", [])
    recent.append(signature)
    del recent[:-8]
    state["distinct_viewport_count"] = len(recent)
    state.setdefault("recent_scroll_positions", []).append({
        "viewport_hash": signature,
        "progress": state.get("current_scroll_progress"),
    })
    del state["recent_scroll_positions"][:-8]
    return "CONTINUE"


def _complete_forward_viewport(state: dict) -> str:
    if not state.get("pending_forward_viewport"):
        return "CONTINUE"
    if not state.get("pending_forward_viewport_scanned"):
        state["pending_forward_viewport"] = False
        state["pending_forward_new_profiles"] = 0
        state["forward_viewports_unscanned"] = state.get("forward_viewports_unscanned", 0) + 1
        return "FORWARD_VIEWPORT_UNSCANNED"
    state["forward_viewports_checked"] = state.get("forward_viewports_checked", 0) + 1
    if state.get("pending_forward_new_profiles", 0) == 0:
        state["forward_viewports_without_new_profile"] = state.get(
            "forward_viewports_without_new_profile", 0
        ) + 1
    else:
        state["forward_viewports_without_new_profile"] = 0
    state["pending_forward_viewport"] = False
    state["pending_forward_viewport_scanned"] = False
    state["pending_forward_new_profiles"] = 0
    if state["forward_viewports_without_new_profile"] >= 5:
        state["forward_progress_stalls"] = state.get("forward_progress_stalls", 0) + 1
        return "FORWARD_PROGRESS_STALLED"
    return "CONTINUE"


def _row_signature(frame, center: float, spacing: float) -> str:
    """Anonymous, local-use visual signature for one visible list row."""
    width, height = frame.size
    left, _top, right, _bottom = CONTACT_LIST_ROI
    half = max(8, round(spacing * 0.46))
    crop = frame.crop((round(left * width), max(0, round(center - half)),
                       round(right * width), min(height, round(center + half))))
    gray = ImageOps.autocontrast(_neutralize_green_selection(crop)).resize((16, 8))
    pixels = list(gray.tobytes())
    mean = sum(pixels) / max(1, len(pixels))
    bits = 0
    for index, value in enumerate(pixels):
        bits = (bits << 1) | (value >= mean)
    return f"{bits:032x}"


def _scroll_escalation(base_notches: int, max_units: int | None = None) -> tuple[int, ...]:
    base = max(1, int(base_notches))
    candidates = (base, base + 2, base + 4)
    if max_units is not None:
        candidates = tuple(min(max(1, int(max_units)), value) for value in candidates)
    return tuple(dict.fromkeys(candidates))


def _list_view_changed(before_hash: str, after_hash: str) -> bool:
    return bool(before_hash and after_hash and before_hash != after_hash)


def _scrollbar_progress(
    thumb_top: float, thumb_bottom: float, track_top: float, track_bottom: float
) -> float | None:
    track_height = track_bottom - track_top
    thumb_height = thumb_bottom - thumb_top
    travel = track_height - thumb_height
    if track_height <= 0 or thumb_height <= 0 or travel <= 0:
        return None
    return float(min(1.0, max(0.0, (thumb_top - track_top) / travel)))


def _scrollbar_direction(before: float | None, after: float | None, epsilon: float = 0.002) -> str:
    if before is None or after is None:
        return "DIRECTION_UNVERIFIED_VIEWPORT"
    delta = after - before
    if delta > epsilon:
        return "FORWARD"
    if delta < -epsilon:
        return "UPWARD"
    return "NO_MOVEMENT"


def _combine_scroll_direction_evidence(
    anchor_direction: str,
    anchor_count: int,
    scrollbar_direction: str | None,
) -> tuple[str, bool]:
    """Use row anchors as movement authority; flag contradictory scrollbar evidence."""
    if anchor_count < 2 or anchor_direction not in {"FORWARD", "UPWARD", "NO_MOVEMENT"}:
        return "DIRECTION_UNVERIFIED_VIEWPORT", scrollbar_direction is not None
    conflict = scrollbar_direction is not None and scrollbar_direction != anchor_direction
    return anchor_direction, conflict


def _scroll_alignment_confidence(direction: str, anchor_count: int, viewport_confidence: str) -> str:
    """Trust repeated measured anchors for scroll tuning without relaxing row-skip gates."""
    if direction == "FORWARD" and anchor_count >= 2:
        return "HIGH"
    return viewport_confidence


def _scroll_pulse_budget_remaining(
    cumulative_displacement: float,
    target_displacement: float,
    safe_displacement: float,
) -> bool:
    """Keep discrete wheel pulses within both the geometry target and viewport cap."""
    return (
        cumulative_displacement < max(0.0, target_displacement)
        and cumulative_displacement < max(0.0, safe_displacement)
    )


def _scroll_strategy_for_direction(direction: str, thumb_detected: bool) -> str:
    if direction in {"UPWARD", "NO_MOVEMENT", "SCROLL_OSCILLATION"}:
        return "SCROLLBAR_DRAG_PRIMARY" if thumb_detected else "SCROLL_DIRECTION_UNRESOLVED"
    return "WHEEL"


def _mark_legacy_scroll_attempts(attempts: list[dict]) -> int:
    count = 0
    for attempt in attempts:
        if not attempt.get("direction_status"):
            attempt["direction_status"] = "DIRECTION_UNVERIFIED_VIEWPORT"
            attempt["direction_method"] = "LEGACY_HASH_ONLY"
            attempt["counted_as_forward_viewport"] = False
            attempt["diagnosis"] = "DIRECTION_UNVERIFIED_VIEWPORT"
            attempt["rows_scanned"] = False
            count += 1
        elif attempt.get("direction_status") == "FORWARD" and not attempt.get("rows_scanned"):
            attempt["counted_as_forward_viewport"] = False
            attempt["direction_reassessment"] = "FORWARD_DIRECTION_VERIFIED_ROWS_NOT_SCANNED"
    return count


def _detect_scrollbar_thumb(frame) -> dict | None:
    """Find a high-contrast thumb on the narrow right edge of the contact ROI."""
    import numpy as np

    gray = np.asarray(ImageOps.grayscale(frame), dtype=np.uint8)
    width, height = frame.size
    left, top, right, bottom = CONTACT_SCROLLBAR_ROI
    x0, x1 = round(left * width), round(right * width)
    y0, y1 = round(top * height), round(bottom * height)
    strip = gray[y0:y1, max(x0, x1 - 12):x1]
    if strip.shape[0] < 80 or strip.shape[1] < 3:
        return None
    kernel = np.ones(5, dtype=np.float32) / 5
    candidates = []
    for column in range(strip.shape[1]):
        profile = np.convolve(strip[:, column].astype(np.float32), kernel, mode="same")
        edge = max(12, len(profile) // 12)
        baseline = float(np.median(profile))
        deviation = np.abs(profile - baseline)
        mask = deviation >= 14
        starts = np.flatnonzero(mask & ~np.r_[False, mask[:-1]])
        ends = np.flatnonzero(mask & ~np.r_[mask[1:], False]) + 1
        for start, end in zip(starts, ends, strict=True):
            length = int(end - start)
            if length < max(12, round(len(profile) * 0.015)) or length > len(profile) * 0.65:
                continue
            before_band = profile[max(0, start - edge):start]
            after_band = profile[end:min(len(profile), end + edge)]
            side_levels = [
                float(np.median(band)) for band in (before_band, after_band) if band.size
            ]
            thumb_level = float(np.median(profile[start:end]))
            if not side_levels or max(abs(level - baseline) for level in side_levels) > 10:
                continue
            if abs(thumb_level - baseline) < 18:
                continue
            score = abs(thumb_level - baseline) * min(1.0, length / 40)
            candidates.append((score, start, end, column))
    if not candidates:
        return None
    score, start, end, column = max(candidates)
    if score < 18:
        return None
    return {
        "thumb_top": int(y0 + start),
        "thumb_bottom": int(y0 + end),
        "track_top": int(y0),
        "track_bottom": int(y1),
        "x": int(max(x0, x1 - 12) + column),
        "progress": _scrollbar_progress(y0 + start, y0 + end, y0, y1),
    }


def _row_anchor_direction(
    before_signatures: list[str], before_centers: list[float],
    after_signatures: list[str], after_centers: list[float],
    *, min_shift: float = 4.0,
) -> tuple[str, float | None, int]:
    """Use shared anonymous row crops to verify that content moved upward."""
    before = [(sig, y) for sig, y in zip(before_signatures, before_centers, strict=False)]
    after = [(sig, y) for sig, y in zip(after_signatures, after_centers, strict=False)]
    shifts = []
    for signature, before_y in before:
        if before_signatures.count(signature) != 1:
            continue
        matches = [y for candidate, y in after if candidate == signature]
        if len(matches) == 1:
            shifts.append(matches[0] - before_y)
    if len(shifts) < 2:
        return "DIRECTION_UNVERIFIED_VIEWPORT", None, len(shifts)
    median_shift = float(statistics.median(shifts))
    if median_shift < -min_shift:
        return "FORWARD", median_shift, len(shifts)
    if median_shift > min_shift:
        return "UPWARD", median_shift, len(shifts)
    return "NO_MOVEMENT", median_shift, len(shifts)


def _scroll_oscillation(history: list[dict], progress: float, signature: str, epsilon: float = 0.002) -> bool:
    points = [item.get("progress") for item in history if item.get("progress") is not None]
    points.append(progress)
    if len(points) < 4 or max(points) - min(points) > 0.03:
        return False
    deltas = [b - a for a, b in pairwise(points)]
    signs = [1 if delta > epsilon else -1 if delta < -epsilon else 0 for delta in deltas]
    nonzero = [sign for sign in signs if sign]
    hashes = [item.get("viewport_hash") for item in history[-3:]] + [signature]
    return len(nonzero) >= 3 and any(a != b for a, b in pairwise(nonzero[-3:])) and len(set(hashes)) > 1


def _resume_profile_action(seen_profile_hashes: list[str], profile_hash: str) -> str:
    return "NAVIGATE_NEXT" if profile_hash in seen_profile_hashes else "PROCESS_CURRENT"


def _normalized_point_in_bounds(x: float, y: float, width: int, height: int) -> bool:
    return width > 0 and height > 0 and 0.0 <= x / width <= 1.0 and 0.0 <= y / height <= 1.0


def _adjacent_overlap_count(previous: list[str], current: list[str]) -> int:
    """Longest suffix of prior visible rows matching current viewport prefix."""
    limit = min(len(previous), len(current))
    for count in range(limit, 0, -1):
        previous_slice = previous[-count:]
        current_slice = current[:count]
        if (
            len(set(previous_slice)) != count
            or len(set(current_slice)) != count
            or any(previous.count(signature) != 1 for signature in previous_slice)
            or any(current.count(signature) != 1 for signature in current_slice)
        ):
            continue
        distances = [
            (int(a, 16) ^ int(b, 16)).bit_count()
            for a, b in zip(previous_slice, current_slice, strict=True)
        ]
        if all(distance == 0 for distance in distances) and (
            count >= 2 or distances == [0]
        ):
            return count
    return 0


def _adapt_scroll_extra(current_extra: int, overlap_count: int, visible_count: int) -> int:
    """Apply a bounded one-notch adjustment from measured adjacent overlap."""
    if visible_count <= 0:
        return max(-2, min(4, int(current_extra)))
    ratio = overlap_count / visible_count
    if ratio > 0.60:
        return min(4, int(current_extra) + 1)
    if ratio < 0.05:
        return max(-2, int(current_extra) - 1)
    return max(-2, min(4, int(current_extra)))


def _confirmed_target_reached(state: dict, target: int) -> bool:
    identifiers = [row.get("wechat_id", "") for row in state.get("confirmed", [])]
    return len(identifiers) >= target and len(set(identifiers)) >= target


def _run_target_reached(state: dict, target: int, profile_limit: int | None) -> bool:
    return _confirmed_target_reached(state, target) or (
        profile_limit is not None
        and int(state.get("unique_profiles_visited", 0)) >= profile_limit
    )


def _record_health_check(state: dict) -> None:
    unique = int(state.get("unique_profiles_visited", 0))
    if unique <= 0 or unique % 25:
        return
    checks = state.setdefault("health_checks", [])
    if checks and checks[-1].get("unique_profiles_visited") == unique:
        return
    terminal = int(state.get("terminal_processed", 0))
    confirmed = len(state.get("confirmed", []))
    checks.append({
        "unique_profiles_visited": unique,
        "terminal_processed": terminal,
        "confirmed_unique": confirmed,
        "review": len(state.get("review", [])),
        "profile_revisits": state.get("profile_revisits", 0),
        "duplicate_id_anomaly": state.get("duplicate_ids_skipped", 0),
        "confirmation_rate": round(confirmed / unique, 6),
        "frame_mismatch": state.get("frame_mismatches", 0),
        "unaccounted": state.get("result_accounting", {}).get("unaccounted", 0),
        "forward_progress_recoveries": state.get("forward_progress_recoveries", 0),
    })


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_replace(
        path,
        lambda fp: json.dump(value, fp, ensure_ascii=False, indent=2),
    )


def _atomic_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    def write(fp) -> None:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    _atomic_replace(path, write, encoding="utf-8-sig")


def _identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint_snapshot(state: dict) -> dict:
    """Persist counts and masked IDs; raw confirmed IDs live in export files."""
    snapshot = dict(state)
    confirmed = state.get("confirmed", [])
    snapshot["confirmed_count"] = len(confirmed)
    snapshot["confirmed"] = [
        {
            **{key: value for key, value in row.items() if key != "wechat_id"},
            "wechat_id_masked": _mask(str(row.get("wechat_id", ""))),
        }
        for row in confirmed
    ]
    frame_hashes = state.get("confirmed_frame_hashes", {})
    snapshot["confirmed_frame_hashes"] = {
        key if len(key) == 64 and all(ch in "0123456789abcdef" for ch in key.casefold())
        else _identifier_hash(key): value
        for key, value in frame_hashes.items()
    }
    return snapshot


def _load_confirmed_export(
    expected_count: int, expected_hashes: set[str] | None = None
) -> list[dict]:
    path = OUT / f"{OUTPUT_STEM}.csv"
    if not path.exists():
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_NOT_FOUND")
    confirmed = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            value = row.get("wechat_id", "")
            if contains_mask_placeholder(value) or not validate_wechat_id(value, DEFAULT_CONFIG).valid:
                raise RuntimeError("RESUME_CONFIRMED_EXPORT_INVALID_OR_MASKED")
            confirmed.append({
                **row,
                "row_ordinal": int(row["row_ordinal"]),
            })
    if len(confirmed) != expected_count:
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_COUNT_MISMATCH")
    if expected_hashes is not None and {
        _identifier_hash(row["wechat_id"]) for row in confirmed
    } != expected_hashes:
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_HASH_MISMATCH")
    return confirmed


def _mask(value: str) -> str:
    if len(value) <= 5:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


@dataclass(frozen=True)
class _RecognitionDecision:
    """Transient raw OCR evidence plus separate safe display data."""

    raw_candidate: str | None
    status: str
    confidence: str = ""
    candidate_masked: str = ""
    attempt_statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViewportScanPlan:
    """Ephemeral alignment plan; row signatures never become contact IDs."""

    viewport_id: int
    visible_rows: tuple[float, ...]
    overlap_rows: tuple[int, ...]
    candidate_new_rows: tuple[int, ...]
    last_processed_anchor: float | None
    scroll_direction: str
    confidence: str


@dataclass
class AdaptiveScrollController:
    """Geometry-driven, bounded controller for measured vertical list movement."""

    current_scroll_units: int
    target_displacement: float = 0.0
    observed_displacement: float | None = None
    observed_scroll_units: int | None = None
    overlap_ratio: float | None = None
    direction: str = "UNVERIFIED"
    confidence: str = "LOW"
    visible_rows: int = 0
    row_spacing: float = 0.0
    max_scroll_units: int = 1
    desired_overlap_rows: float = 2.5

    def update_geometry(self, visible_rows: int, row_spacing: float) -> None:
        self.visible_rows = max(0, int(visible_rows))
        self.row_spacing = max(0.0, float(row_spacing))
        if self.visible_rows and self.row_spacing:
            overlap_goal = min(self.desired_overlap_rows, max(1.0, self.visible_rows - 1.0))
            self.target_displacement = max(
                self.row_spacing,
                (self.visible_rows - overlap_goal) * self.row_spacing,
            )
            if self.observed_displacement and self.observed_scroll_units:
                px_per_unit = self.observed_displacement / self.observed_scroll_units
                safe_units = int(((self.visible_rows - 1) * self.row_spacing) / px_per_unit)
                self.max_scroll_units = max(1, min(64, safe_units))
            else:
                self.max_scroll_units = max(1, self.visible_rows - 1)

    def observe(
        self,
        *,
        actual_units: int,
        displacement: float | None,
        direction: str,
        alignment_confidence: str,
    ) -> None:
        units = max(1, int(actual_units))
        self.observed_scroll_units = units
        self.direction = direction
        self.confidence = alignment_confidence
        if displacement is None or not self.row_spacing or not self.visible_rows:
            self.observed_displacement = None
            self.overlap_ratio = None
            self.current_scroll_units = min(self.max_scroll_units, units)
            self.confidence = "LOW"
            return

        self.observed_displacement = abs(float(displacement))
        rows_advanced = self.observed_displacement / self.row_spacing
        overlap_rows = max(0.0, self.visible_rows - rows_advanced)
        self.overlap_ratio = min(1.0, overlap_rows / self.visible_rows)
        px_per_unit = self.observed_displacement / units
        if px_per_unit > 0:
            geometry_limit = int(
                ((self.visible_rows - 1) * self.row_spacing) / px_per_unit
            )
            self.max_scroll_units = max(1, min(64, geometry_limit))
            target_units = max(1, round(self.target_displacement / px_per_unit))
        else:
            target_units = units

        if (
            direction != "FORWARD" or alignment_confidence != "HIGH"
            or self.observed_displacement == 0 or overlap_rows < 1.0
        ):
            self.current_scroll_units = max(1, min(self.max_scroll_units, units - 1))
            self.confidence = "LOW"
        elif overlap_rows > self.desired_overlap_rows + 1.0:
            increase = 2 if rows_advanced < (self.visible_rows - self.desired_overlap_rows) * 0.5 else 1
            self.current_scroll_units = min(
                self.max_scroll_units, units + increase, target_units
            )
        elif overlap_rows < self.desired_overlap_rows - 1.0:
            self.current_scroll_units = max(1, units - 1)
        else:
            self.current_scroll_units = min(self.max_scroll_units, units)

    def to_dict(self) -> dict:
        return {
            "current_scroll_units": self.current_scroll_units,
            "target_displacement": round(self.target_displacement, 3),
            "observed_displacement": (
                round(self.observed_displacement, 3)
                if self.observed_displacement is not None else None
            ),
            "observed_scroll_units": self.observed_scroll_units,
            "overlap_ratio": round(self.overlap_ratio, 6) if self.overlap_ratio is not None else None,
            "direction": self.direction,
            "confidence": self.confidence,
            "visible_rows": self.visible_rows,
            "row_spacing": round(self.row_spacing, 3),
            "max_scroll_units": self.max_scroll_units,
            "desired_overlap_rows": self.desired_overlap_rows,
        }


def _ensure_navigation_state(state: dict) -> dict:
    """Add V2 defaults to a legacy checkpoint without changing its evidence."""
    state.setdefault("navigation_metrics", {})
    state.setdefault("viewport_cursor", {
        "last_processed_row": state.get("current_y"),
        "overlap_boundary": None,
        "next_candidate_row": state.get("current_y"),
    })
    state.setdefault("scroll_extra_notches", 0)
    return state


def _build_viewport_scan_plan(
    viewport_id: int,
    previous_signatures: list[str],
    current_signatures: list[str],
    current_rows: list[float],
    *,
    last_processed_anchor: float | None = None,
    scroll_direction: str = "FORWARD",
) -> ViewportScanPlan:
    """Skip only a confident contiguous adjacent overlap; uncertain means scan all."""
    overlap = 0
    confidence = "UNCERTAIN"
    if scroll_direction == "FORWARD" and previous_signatures and current_signatures:
        overlap = _adjacent_overlap_count(previous_signatures, current_signatures)
        if overlap:
            confidence = "HIGH"
    boundary = min(overlap, len(current_rows))
    return ViewportScanPlan(
        viewport_id=int(viewport_id),
        visible_rows=tuple(float(y) for y in current_rows),
        overlap_rows=tuple(range(boundary)),
        candidate_new_rows=tuple(range(boundary, len(current_rows))),
        last_processed_anchor=last_processed_anchor,
        scroll_direction=scroll_direction,
        confidence=confidence,
    )


def _load_scroll_controller(state: dict, visible_rows: int, row_spacing: float, initial_units: int) -> AdaptiveScrollController:
    saved = state.get("adaptive_scroll_controller")
    try:
        controller = AdaptiveScrollController(**saved) if isinstance(saved, dict) else None
    except (TypeError, ValueError):
        controller = None
    if controller is None:
        controller = AdaptiveScrollController(current_scroll_units=max(1, int(initial_units)))
    controller.update_geometry(visible_rows, row_spacing)
    return controller


def _update_navigation_metrics(state: dict) -> dict:
    """Refresh aggregate, PII-free metrics for checkpoint and live status."""
    metrics = state.setdefault("navigation_metrics", {})
    metrics.update({
        "row_candidates_seen": int(state.get("row_candidates_seen", state.get("rows_clicked", 0))),
        "row_candidates_skipped_as_overlap": int(state.get("adjacent_overlap_rows_skipped", 0)),
        "profile_clicks": int(state.get("rows_clicked", 0)),
        "profile_revisits": int(state.get("profile_revisits", 0)),
        "new_profiles": int(state.get("unique_profiles_visited", 0)),
        "ocr_calls": int(state.get("extractions_attempted", 0)),
        "scroll_events": int(state.get("successful_scroll_count", 0)),
        "viewport_count": int(state.get("viewport", state.get("viewports", 0))),
        "recovery_count": int(state.get("forward_progress_recoveries", 0)),
    })
    unique = metrics["new_profiles"]
    metrics["revisit_per_unique"] = round(metrics["profile_revisits"] / unique, 6) if unique else 0.0
    metrics["clicks_per_unique"] = round(metrics["profile_clicks"] / unique, 6) if unique else 0.0
    metrics["ocr_calls_per_unique"] = round(metrics["ocr_calls"] / unique, 6) if unique else 0.0
    metrics["new_profiles_per_viewport"] = round(
        unique / metrics["viewport_count"], 6
    ) if metrics["viewport_count"] else 0.0
    return metrics


def _append_benchmark_viewport_event(
    state: dict,
    *,
    viewport_hash: str,
    previous_viewport_hash: str,
    direction: str,
    requested_units: int | None,
    anchor_displacement: float | None,
    visible_rows: list[float],
    row_spacing: float,
    row_signatures: list[str],
    overlap_indices: list[int],
    candidate_indices: list[int],
    recovery_type: str | None = None,
) -> None:
    if not BENCHMARK_MODE:
        return
    state["telemetry_event_id"] = int(state.get("telemetry_event_id", 0)) + 1
    event = {
        "event_id": state["telemetry_event_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "viewport_hash": viewport_hash,
        "previous_viewport_hash": previous_viewport_hash,
        "scroll_direction": direction,
        "requested_scroll_units": requested_units,
        "anchor_displacement": anchor_displacement,
        "visible_row_count": len(visible_rows),
        "median_row_spacing": round(float(row_spacing), 3),
        "row_signature_hashes": list(row_signatures),
        "overlap_indices": list(overlap_indices),
        "candidate_indices": list(candidate_indices),
        "clicked_indices": [],
        "profiles": [],
        "recovery_type": recovery_type,
    }
    state.setdefault("navigation_telemetry", []).append(event)
    state["last_viewport_event_index"] = len(state["navigation_telemetry"]) - 1


def _record_benchmark_profile_event(
    state: dict,
    profile_hash: str,
    *,
    row_index: int | None,
    outcome: str,
    revisit: bool,
    new_profile: bool,
    ocr_called: bool,
    ocr_call_count: int = 0,
) -> None:
    if not BENCHMARK_MODE:
        return
    events = state.get("navigation_telemetry", [])
    index = state.get("last_viewport_event_index")
    if not isinstance(index, int) or not 0 <= index < len(events):
        return
    item = {
        "row_index": row_index,
        "profile_hash_prefix": profile_hash[:12],
        "outcome": outcome,
        "revisit": bool(revisit),
        "new_profile": bool(new_profile),
        "ocr_called": bool(ocr_called),
        "ocr_call_count": max(0, int(ocr_call_count)),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    events[index].setdefault("profiles", []).append(item)
    state["last_benchmark_profile_index"] = len(events[index]["profiles"]) - 1


def _record_benchmark_candidate_click(state: dict, row_index: int) -> None:
    if not BENCHMARK_MODE:
        return
    events = state.get("navigation_telemetry", [])
    event_index = state.get("last_viewport_event_index")
    if not isinstance(event_index, int) or not 0 <= event_index < len(events):
        return
    event = events[event_index]
    event.setdefault("clicked_indices", []).append(int(row_index))
    event.setdefault("clicks", []).append({
        "row_index": int(row_index),
        "click_attempts": 0,
    })
    state["last_benchmark_click_index"] = len(event["clicks"]) - 1


def _update_benchmark_candidate_clicks(state: dict, click_attempts: int) -> None:
    if not BENCHMARK_MODE:
        return
    events = state.get("navigation_telemetry", [])
    event_index = state.get("last_viewport_event_index")
    click_index = state.get("last_benchmark_click_index")
    if (
        isinstance(event_index, int) and 0 <= event_index < len(events)
        and isinstance(click_index, int)
        and 0 <= click_index < len(events[event_index].get("clicks", []))
    ):
        events[event_index]["clicks"][click_index]["click_attempts"] = max(
            0, int(click_attempts)
        )


def _update_benchmark_ocr_event(
    state: dict, *, ocr_called: bool, status: str, confidence: str,
    ocr_call_count: int = 0,
) -> None:
    if not BENCHMARK_MODE:
        return
    events = state.get("navigation_telemetry", [])
    event_index = state.get("last_viewport_event_index")
    profile_index = state.get("last_benchmark_profile_index")
    if (
        isinstance(event_index, int) and 0 <= event_index < len(events)
        and isinstance(profile_index, int)
        and 0 <= profile_index < len(events[event_index].get("profiles", []))
    ):
        profile = events[event_index]["profiles"][profile_index]
        profile["ocr_called"] = bool(ocr_called)
        profile["ocr_call_count"] = max(0, int(ocr_call_count))
        profile["outcome"] = "CONFIRMED" if status == "CONFIRMED_BY_CONSENSUS" else "REVIEW"
        profile["confidence"] = confidence


@dataclass
class _ContactExtractionState:
    """Fresh per-contact attempt state; transient failures never cross contacts."""

    sequence_index: int
    attempt_results: list[dict] = field(default_factory=list)
    transient_frame_error: str | None = None

    def record_attempt(
        self, *, attempt_index: int, expected_hash: str,
        extraction_hash: str, hash_match: bool, status: str,
    ) -> None:
        self.transient_frame_error = None if hash_match else "FRAME_MISMATCH"
        self.attempt_results.append({
            "sequence_index": self.sequence_index,
            "attempt_index": attempt_index,
            "expected_hash_prefix": expected_hash[:12],
            "extraction_hash_prefix": extraction_hash[:12],
            "hash_match": hash_match,
            "attempt_status": status,
        })

    def terminal_status(self, fallback: str) -> str:
        for attempt in reversed(self.attempt_results):
            if attempt["hash_match"]:
                attempt["terminal"] = True
                return attempt["attempt_status"]
        if self.attempt_results:
            self.attempt_results[-1]["terminal"] = True
        return fallback


def _confirmed_value(decision: _RecognitionDecision) -> str | None:
    """Return raw ID only for a complete, valid, unmasked confirmation."""
    raw = decision.raw_candidate
    if decision.status != "CONFIRMED_BY_CONSENSUS" or not raw:
        return None
    if contains_mask_placeholder(raw) or not validate_wechat_id(raw, DEFAULT_CONFIG).valid:
        return None
    return raw


def _profile_hash(frame) -> str:
    width, height = frame.size
    l, t, r, b = PROFILE_CHANGE_ROI
    crop = frame.crop((round(l * width), round(t * height), round(r * width), round(b * height)))
    return hashlib.sha256(crop.convert("RGB").tobytes()).hexdigest()


def _contact_list_hash(frame) -> str:
    width, height = frame.size
    l, t, r, b = CONTACT_LIST_ROI
    crop = frame.crop((round(l * width), round(t * height), round(r * width), round(b * height)))
    normalized = _neutralize_green_selection(crop).resize((24, 32))
    pixels = list(normalized.tobytes())
    mean = sum(pixels) / max(1, len(pixels))
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | (pixel >= mean)
    return f"{bits:0192x}"


def _locate_weixin_window() -> tuple[int, int]:
    import psutil
    import win32gui
    import win32process

    matches: list[tuple[int, int, int]] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
            return True
        try:
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            name = psutil.Process(pid).name().casefold()
            if name in {"weixin.exe", "wechat.exe"}:
                rect = win32gui.GetWindowRect(hwnd)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if area:
                    matches.append((area, int(hwnd), pid))
        except (psutil.Error, OSError):
            pass
        return True

    win32gui.EnumWindows(visit, None)
    if not matches:
        raise RuntimeError("WEIXIN_WINDOW_NOT_FOUND")
    matches.sort(reverse=True)
    _, hwnd, pid = matches[0]
    return hwnd, pid


def _client_geometry(hwnd: int) -> dict:
    import win32api
    import win32con
    import win32gui

    client = win32gui.GetClientRect(hwnd)
    left_top = win32gui.ClientToScreen(hwnd, (client[0], client[1]))
    right_bottom = win32gui.ClientToScreen(hwnd, (client[2], client[3]))
    width, height = client[2] - client[0], client[3] - client[1]
    roi = (
        round(CONTACT_LIST_ROI[0] * width), round(CONTACT_LIST_ROI[1] * height),
        round(CONTACT_LIST_ROI[2] * width), round(CONTACT_LIST_ROI[3] * height),
    )
    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    monitor_info = win32api.GetMonitorInfo(monitor)
    monitors = win32api.EnumDisplayMonitors()
    monitor_index = next(
        (index for index, item in enumerate(monitors) if item[0] == monitor), -1
    )
    virtual = (
        win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
        win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN),
        win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN),
        win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN),
    )
    return {
        "window_screen_rect": tuple(win32gui.GetWindowRect(hwnd)),
        "client_screen_rect": (left_top[0], left_top[1], right_bottom[0], right_bottom[1]),
        "client_size": (width, height),
        "contact_list_roi": roi,
        "monitor_handle": int(monitor),
        "monitor_index": monitor_index,
        "monitor_rect": tuple(monitor_info["Monitor"]),
        "monitor_is_primary": bool(monitor_info["Flags"] & 1),
        "virtual_screen_rect": virtual,
    }


def _read_target_ready(hwnd: int, geometry: dict) -> bool:
    """Read readiness is bound to the target window, independent of focus."""
    import win32gui

    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return False
    rect = win32gui.GetWindowRect(hwnd)
    client = win32gui.GetClientRect(hwnd)
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return False
    width, height = client[2] - client[0], client[3] - client[1]
    left, top, right, bottom = geometry["contact_list_roi"]
    profile_left, profile_top, profile_right, profile_bottom = PROFILE_CHANGE_ROI
    profile_roi = (
        round(profile_left * width), round(profile_top * height),
        round(profile_right * width), round(profile_bottom * height),
    )
    return (
        width > 0 and height > 0 and right > left and bottom > top
        and left >= 0 and top >= 0 and right <= width and bottom <= height
        and profile_roi[0] >= 0 and profile_roi[1] >= 0
        and profile_roi[2] <= width and profile_roi[3] <= height
        and geometry["client_size"] == (width, height)
    )


def _safe_target_input_point(hwnd: int, nav, x: int, y: int) -> tuple[int, int] | None:
    """Refresh and validate a client point against the target HWND geometry."""
    import win32gui

    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return None
    mapper = nav._window_mapper()
    nav.mapper = mapper
    width, height = mapper.size
    if not (0 <= int(x) < width and 0 <= int(y) < height):
        return None
    screen_x, screen_y = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
    client_left, client_top, client_right, client_bottom = mapper.client_screen_rect
    window_left, window_top, window_right, window_bottom = mapper.window_screen_rect
    if not (
        client_left <= screen_x < client_right
        and client_top <= screen_y < client_bottom
        and window_left <= screen_x < window_right
        and window_top <= screen_y < window_bottom
    ):
        return None
    return int(screen_x), int(screen_y)


def _scrollbar_drag_delta(track_top: int, track_bottom: int) -> int:
    track_height = max(0, int(track_bottom) - int(track_top))
    if not track_height:
        return 0
    return max(2, min(round(track_height * 0.06), round(track_height * 0.04)))


def _drag_scrollbar_thumb(hwnd: int, pid: int, nav, thumb: dict, state: dict) -> bool:
    import win32api
    import win32con

    track_top, track_bottom = int(thumb["track_top"]), int(thumb["track_bottom"])
    thumb_top, thumb_bottom = int(thumb["thumb_top"]), int(thumb["thumb_bottom"])
    x = int(thumb["x"])
    start_y = (thumb_top + thumb_bottom) // 2
    delta = _scrollbar_drag_delta(track_top, track_bottom)
    end_y = min(track_bottom - (thumb_bottom - thumb_top) // 2, start_y + delta)
    if delta <= 0 or end_y <= start_y:
        return False
    _wait_foreground(hwnd, pid, state)
    start = _safe_target_input_point(hwnd, nav, x, start_y)
    end = _safe_target_input_point(hwnd, nav, x, end_y)
    if start is None or end is None or not _foreground_ready(hwnd, pid):
        return False
    win32api.SetCursorPos(start)
    if not _foreground_ready(hwnd, pid):
        return False
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    try:
        time.sleep(0.12)
        last_y = start_y
        for step in range(1, 5):
            next_y = start_y + round((end_y - start_y) * step / 4)
            point = _safe_target_input_point(hwnd, nav, x, next_y)
            if point is None or not _foreground_ready(hwnd, pid):
                return False
            win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, 0, next_y - last_y, 0, 0)
            last_y = next_y
            time.sleep(0.06)
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return True


def _ready_dialog() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("微信联系人采集准备")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.geometry("+100+100")
    tk.Label(
        root,
        text="请在微信“联系人”页展开联系人分组并滚动到列表最顶部。\n然后点击第一个普通联系人，保持微信在前台且不要改窗口尺寸。\n确认完成后点击下方按钮；程序将从 sequence_index=1 开始。",
        padx=24, pady=18, justify="left",
    ).pack()
    tk.Button(root, text="已滚到顶部并选中第一个普通联系人", width=34, command=root.destroy).pack(pady=(0, 18))
    root.mainloop()


def _foreground_ready(hwnd: int, pid: int) -> bool:
    import win32gui
    import win32process

    from pyweixin.visual_navigation import is_weixin_foreground

    foreground = int(win32gui.GetForegroundWindow() or 0)
    if not foreground:
        return False
    fg_pid = int(win32process.GetWindowThreadProcessId(foreground)[1])
    return fg_pid == pid or is_weixin_foreground(hwnd, target_pid=pid, foreground_hwnd=foreground)


def _wait_foreground(hwnd: int, pid: int, state: dict) -> None:
    while not _foreground_ready(hwnd, pid):
        state["state"] = "PAUSED_INPUT_FOREGROUND_REQUIRED"
        _persist(state)
        time.sleep(1.0)
    state["state"] = "RUNNING"
    _persist(state)


def _persist(state: dict) -> None:
    _refresh_accounting(state)
    _update_navigation_metrics(state)
    snapshot = _checkpoint_snapshot(state)
    with _LIVE_LOCK:
        _atomic_json(CHECKPOINT, snapshot)
        _atomic_json(OUT / f"{OUTPUT_STEM}.progress.json", snapshot)
        _atomic_json(LIVE, _live_status(snapshot, pid=os.getpid()))
        if BENCHMARK_MODE:
            finished = state.get("state") in {"BENCHMARK_PROFILE_LIMIT_REACHED", "TARGET_REACHED"}
            _atomic_json(OUT / "navigation_telemetry.json", {
                "schema_version": 1,
                "status": "REPLAY_EVIDENCE_COMPLETE" if finished else "IN_PROGRESS",
                "run_id": state.get("run_id"),
                "unique_profiles": int(state.get("unique_profiles_visited", 0)),
                "profile_revisits": int(state.get("profile_revisits", 0)),
                "profile_clicks": int(state.get("rows_clicked", 0)),
                "ocr_calls": int(state.get("extractions_attempted", 0)),
                "events": state.get("navigation_telemetry", []),
            })


def _live_status(snapshot: dict, *, pid: int) -> dict:
    return {
        "pid": pid, "state": snapshot.get("state", "RUNNING"),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "confirmed_unique": int(snapshot.get("confirmed_count", len(snapshot.get("confirmed", [])))),
        "review": len(snapshot.get("review", [])), "rows_clicked": snapshot.get("rows_clicked", 0),
        "profiles_changed": snapshot.get("profiles_changed", 0),
        "profile_revisits": snapshot.get("profile_revisits", 0),
        "unique_profiles_visited": snapshot.get("unique_profiles_visited", 0),
        "forward_progress_recoveries": snapshot.get("forward_progress_recoveries", 0),
        "forward_progress_stalls": snapshot.get("forward_progress_stalls", 0),
        "frame_matches": snapshot.get("frame_matches", 0),
        "frame_mismatches": snapshot.get("frame_mismatches", 0),
        "viewport": snapshot.get("viewport", 0), "row_ordinal": snapshot.get("row_ordinal", 0),
        "terminal_processed": snapshot.get("terminal_processed", 0),
        "profiles_visited": snapshot.get("profiles_visited", 0),
        "extractions_attempted": snapshot.get("extractions_attempted", 0),
        "unaccounted": snapshot.get("result_accounting", {}).get("unaccounted", 0),
        "navigation_metrics": snapshot.get("navigation_metrics", {}),
    }


def _heartbeat_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(1.5):
        try:
            with _LIVE_LOCK:
                if CHECKPOINT.exists():
                    snapshot = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
                    _atomic_json(LIVE, _live_status(snapshot, pid=os.getpid()))
                else:
                    _atomic_json(LIVE, {
                        "pid": os.getpid(), "state": "STARTING",
                        "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "confirmed_unique": 0, "unique_profiles_visited": 0,
                        "review": 0, "unaccounted": 0,
                    })
        except (OSError, ValueError, TypeError):
            continue


def _refresh_accounting(state: dict) -> dict:
    """Reconcile every terminal profile result into one explicit bucket."""
    counts = Counter(row.get("accounting_reason", "other") for row in state.get("profile_results", []))
    accounting = {key: int(counts.get(key, 0)) for key in ACCOUNTING_KEYS if key != "unaccounted"}
    terminal = len(state.get("profile_results", []))
    extraction_reasons = {
        "label_not_found", "value_not_found", "low_confidence",
        "ocr_disagreement", "ocr_failed", "invalid_candidate", "parser_rejected",
    }
    navigation_reasons = {"frame_mismatch", "profile_not_changed", "row_click_failed"}
    extraction_failures = sum(counts.get(key, 0) for key in extraction_reasons)
    navigation_failures = sum(counts.get(key, 0) for key in navigation_reasons)
    confirmed = int(counts.get("confirmed_unique", 0))
    duplicates = int(counts.get("confirmed_duplicate", 0))
    explicit_other = sum(
        1 for row in state.get("profile_results", [])
        if row.get("accounting_reason") == "other" and row.get("status") == "FAILED_FINAL"
    )
    review_records = terminal - confirmed - duplicates - extraction_failures - navigation_failures - explicit_other
    accounting.update({
        "confirmed_records": confirmed,
        "duplicate_records": duplicates,
        "review_records": max(0, review_records),
        "extraction_failures": extraction_failures,
        "navigation_failures": navigation_failures,
        "explicit_other_terminal_records": explicit_other,
        "processed_profiles": terminal,
    })
    state["terminal_processed"] = terminal
    state["profiles_processed"] = terminal  # compatibility alias; terminal only
    accounting["unaccounted"] = terminal - (
        confirmed + duplicates + review_records + extraction_failures
        + navigation_failures + explicit_other
    )
    state["result_accounting"] = accounting
    state["terminal_frame_mismatch_contacts"] = int(counts.get("frame_mismatch", 0))
    return accounting


def _evaluate_20_preflight(state: dict) -> tuple[bool, dict]:
    accounting = _refresh_accounting(state)
    details = {
        "profiles_visited": state.get("profiles_visited", 0),
        "terminal_processed": state.get("terminal_processed", 0),
        "profiles_changed": state.get("profiles_changed", 0),
        "transition_count": len(state.get("transition_audit", [])),
        "successful_transitions": sum(
            bool(item.get("profile_changed")) for item in state.get("transition_audit", [])
        ),
        "frame_matches": state.get("frame_matches", 0),
        "frame_mismatches": state.get("frame_mismatches", 0),
        "confirmed": len(state.get("confirmed", [])),
        "review": len(state.get("review", [])),
        "low_confidence": accounting["low_confidence"],
        "source_value_masked": accounting["source_value_masked"],
        "frame_mismatch_results": accounting["frame_mismatch"],
        "terminal_frame_mismatch_contacts": accounting["frame_mismatch"],
        "frame_mismatch_attempts": state.get("frame_mismatch_attempts", 0),
        "ocr_failed": accounting["ocr_failed"],
        "navigation_failures": accounting["navigation_failures"],
        "unaccounted": accounting["unaccounted"],
        "silent_skips": max(
            0, state.get("profiles_visited", 0) - state.get("terminal_processed", 0)
        ),
    }
    passed = (
        details["profiles_visited"] == 20
        and details["terminal_processed"] == 20
        and details["profiles_changed"] >= 19
        and details["transition_count"] == 19
        and details["successful_transitions"] == 19
        and details["frame_matches"] == 20
        and details["frame_mismatches"] == 0
        and details["ocr_failed"] == 0
        and details["navigation_failures"] == 0
        and details["silent_skips"] == 0
        and details["unaccounted"] == 0
        and details["confirmed"] >= 1
    )
    return passed, details


def _terminal_result(
    state: dict, *, row_ordinal: int, status: str, reason: str,
    candidate_masked: str = "", confidence: str = "", frame_hash: str = "",
    attempt_count: int = 1, expected_frame_hash: str = "",
    extraction_frame_hash: str = "",
) -> None:
    """Append one sanitized terminal result; callers persist it before advancing."""
    final_hash_match = bool(
        expected_frame_hash and extraction_frame_hash
        and expected_frame_hash == extraction_frame_hash
    )
    if reason == "FRAME_MISMATCH" and final_hash_match:
        raise RuntimeError("STATE_INVARIANT_VIOLATION: final frame match cannot terminate as FRAME_MISMATCH")
    reason_key = {
        "CONFIRMED": "confirmed_unique",
        "CONFIRMED_BY_CONSENSUS": "confirmed_unique",
        "DUPLICATE_WECHAT_ID_ANOMALY": "confirmed_duplicate",
        "LABEL_NOT_FOUND": "label_not_found",
        "VALUE_NOT_FOUND": "value_not_found",
        "LOW_CONFIDENCE": "low_confidence",
        "SOURCE_VALUE_MASKED": "source_value_masked",
        "OCR_DISAGREEMENT": "ocr_disagreement",
        "FRAME_MISMATCH": "frame_mismatch",
        "OCR_FAILED": "ocr_failed",
        "PROFILE_NOT_CHANGED": "profile_not_changed",
        "ROW_CLICK_FAILED": "row_click_failed",
        "INVALID_CANDIDATE": "invalid_candidate",
        "PARSER_REJECTED": "parser_rejected",
    }.get(reason, "other")
    result = {
        "sequence_index": len(state.get("profile_results", [])) + 1,
        "row_ordinal": row_ordinal,
        "status": status,
        "reason": reason,
        "accounting_reason": reason_key,
        "candidate_masked": candidate_masked,
        "confidence": confidence,
        "frame_hash_prefix": frame_hash[:12],
        "expected_frame_hash_prefix": expected_frame_hash[:12],
        "extraction_frame_hash_prefix": extraction_frame_hash[:12],
        "final_hash_match": final_hash_match if expected_frame_hash and extraction_frame_hash else None,
        "attempt_count": attempt_count,
    }
    state.setdefault("profile_results", []).append(result)
    _refresh_accounting(state)
    _record_health_check(state)


def _same_current_frame(navigation_hash: str, extraction_hash: str, expected_hash: str) -> tuple[bool, str]:
    """Separate a stale extraction frame from a profile that never transitioned."""
    if not navigation_hash or navigation_hash != extraction_hash:
        return False, "FRAME_MISMATCH"
    if expected_hash and navigation_hash != expected_hash:
        return True, "PROFILE_NOT_CHANGED"
    return True, ""


def _advance_sequence_after_transition(state: dict, transition: dict) -> bool:
    """Advance only after a verified new profile is available."""
    if not transition.get("profile_changed") and not transition.get("row_selection_changed"):
        return False
    state["row_ordinal"] += 1
    return True


def _profile_changed_from_last_terminal(previous_hash: str, current_hash: str) -> bool:
    """A repeated stable profile image cannot count as a newly reached contact."""
    return bool(current_hash and (not previous_hash or current_hash != previous_hash))


def _mark_viewport_advance(state: dict) -> bool:
    """A viewport scroll advances navigation; do not reprocess the old profile."""
    state["next_action"] = "NAVIGATE_NEXT"
    return True


def _review_result(
    state: dict, *, row_ordinal: int, reason: str, candidate_masked: str = "",
    confidence: str = "", frame_hash: str = "", status: str = "FINAL_REVIEW",
) -> dict:
    return {
        "sequence_index": len(state.get("profile_results", [])) + 1,
        "viewport": state.get("viewport", 0),
        "row_ordinal": row_ordinal,
        "status": status,
        "reason": reason,
        "candidate_masked": candidate_masked,
        "confidence": confidence,
        "frame_hash_prefix": frame_hash[:12],
    }


def _write_outputs(state: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    identifiers = [row.get("wechat_id", "") for row in state["confirmed"]]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("CONFIRMED_OUTPUT_DUPLICATE_IDS")
    for row in state["confirmed"]:
        value = row.get("wechat_id", "")
        if (
            contains_mask_placeholder(value)
            or not validate_wechat_id(value, DEFAULT_CONFIG).valid
        ):
            raise RuntimeError("CONFIRMED_OUTPUT_CONTAINS_INVALID_OR_MASKED_ID")
    if not _formal_outputs_match(state):
        _atomic_csv(OUT / f"{OUTPUT_STEM}.csv", FORMAL_FIELDS, state["confirmed"])
        _atomic_text(
            OUT / f"{OUTPUT_STEM}.txt",
            "".join(row["wechat_id"] + "\n" for row in state["confirmed"]),
        )
    safe_reviews = []
    for row in state["review"]:
        safe = dict(row)
        candidate = str(safe.get("candidate_masked", "") or "")
        if candidate and not contains_mask_placeholder(candidate):
            safe["candidate_masked"] = _mask(candidate)
        safe_reviews.append({key: safe.get(key, "") for key in REVIEW_FIELDS})
    _atomic_csv(OUT / f"{OUTPUT_STEM}_review.csv", REVIEW_FIELDS, safe_reviews)


def _formal_outputs_match(state: dict) -> bool:
    csv_path = OUT / f"{OUTPUT_STEM}.csv"
    txt_path = OUT / f"{OUTPUT_STEM}.txt"
    if not csv_path.exists() or not txt_path.exists():
        return False
    desired = [
        {key: str(row.get(key, "")) for key in FORMAL_FIELDS}
        for row in state.get("confirmed", [])
    ]
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            actual = list(reader)
            if reader.fieldnames != FORMAL_FIELDS or actual != desired:
                return False
        expected_text = "".join(str(row.get("wechat_id", "")) + "\n" for row in state.get("confirmed", []))
        return txt_path.read_text(encoding="utf-8-sig") == expected_text
    except (OSError, UnicodeError, csv.Error):
        return False


def _resume_output_consistency(
    csv_rows: list[dict],
    txt_ids: list[str],
    checkpoint_count: int,
    *,
    minimum_confirmed_count: int = 40,
) -> dict:
    csv_ids = [row.get("wechat_id", "") for row in csv_rows]
    result = {
        "csv_rows": len(csv_rows),
        "csv_unique": len(set(csv_ids)),
        "txt_rows": len(txt_ids),
        "txt_unique": len(set(txt_ids)),
        "sequence_match": csv_ids == txt_ids,
        "checkpoint_confirmed": int(checkpoint_count),
    }
    result["consistent"] = (
        result["checkpoint_confirmed"] >= minimum_confirmed_count
        and result["csv_rows"] == result["checkpoint_confirmed"]
        and result["csv_unique"] == result["checkpoint_confirmed"]
        and result["txt_rows"] == result["checkpoint_confirmed"]
        and result["txt_unique"] == result["checkpoint_confirmed"]
        and result["sequence_match"]
    )
    return result


def _verify_resume_outputs() -> dict:
    checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    csv_path = OUT / f"{OUTPUT_STEM}.csv"
    txt_path = OUT / f"{OUTPUT_STEM}.txt"
    with csv_path.open(encoding="utf-8-sig", newline="") as fp:
        csv_rows = list(csv.DictReader(fp))
    txt_ids = [line.strip() for line in txt_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    return _resume_output_consistency(
        csv_rows,
        txt_ids,
        checkpoint.get("confirmed_count", -1),
        # A fresh isolated benchmark begins with a valid, exactly empty output
        # set. Production resume retains its historical 40-row safety floor.
        minimum_confirmed_count=0 if checkpoint.get("benchmark_mode") is True else 40,
    )


def _cluster_rows(tokens, roi_top: int, roi_bottom: int) -> list[float]:
    ys = sorted(
        (token.center_y + roi_top)
        for token in tokens
        if getattr(token, "text", "").strip()
        and token.center_y + roi_top >= roi_top + 0.12 * (roi_bottom - roi_top)
    )
    if not ys:
        return []
    clusters: list[list[float]] = [[ys[0]]]
    tolerance = max(7.0, (roi_bottom - roi_top) * 0.009)
    for y in ys[1:]:
        if y - clusters[-1][-1] <= tolerance:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    centers = [statistics.median(group) for group in clusters]
    if len(centers) < 3:
        return []
    gaps = [b - a for a, b in pairwise(centers) if b - a >= 18]
    if not gaps:
        return []
    # Row text produces a dense, repeated spacing mode; group labels are removed
    # as outliers rather than being treated as people.
    spacing = statistics.median(sorted(gaps)[: max(3, (len(gaps) * 2) // 3)])
    rows = [centers[0]]
    for y in centers[1:]:
        if y - rows[-1] >= spacing * 0.65:
            rows.append(y)
    return rows


def _detect_rows(frame, engine) -> tuple[list[float], float, dict]:
    width, height = frame.size
    l, t, r, b = CONTACT_LIST_ROI
    roi = (round(l * width), round(t * height), round(r * width), round(b * height))
    crop = frame.crop(roi)
    tokens = engine.recognize(crop)
    rows = _cluster_rows(tokens, roi[1], roi[3])
    if len(rows) < 3:
        raise RuntimeError("ROW_CALIBRATION_FAILED")
    spacing = statistics.median([y2 - y1 for y1, y2 in pairwise(rows)])
    diagnostics = {"visible_rows": len(rows), "median_row_spacing": round(spacing, 2)}
    return rows, spacing, diagnostics


def _selected_row(rows: list[float], frame) -> int:
    import numpy as np

    width, height = frame.size
    l, _t, r, _b = CONTACT_LIST_ROI
    x0, x1 = round(l * width), round(r * width)
    pixels = np.asarray(frame.convert("RGB"), dtype=np.int16)
    scores = []
    half = max(5, round(height * 0.012))
    for center in rows:
        top, bottom = max(0, round(center) - half), min(height, round(center) + half)
        band = pixels[top:bottom, x0:x1]
        green = (band[:, :, 1] > 90) & (band[:, :, 1] > band[:, :, 0] * 1.10) & (band[:, :, 1] > band[:, :, 2] * 1.05)
        scores.append(float(green.mean()) if green.size else 0.0)
    index = max(range(len(scores)), key=scores.__getitem__)
    if scores[index] < 0.18:
        raise RuntimeError("SELECTED_ROW_NOT_VISIBLE")
    return index


def _row_selection_advanced(before_frame, after_frame, rows: list[float], target_y: int):
    """Require the existing green row selection to move to the clicked row."""
    if len(rows) < 2:
        return None
    try:
        before_index = _selected_row(rows, before_frame)
        after_index = _selected_row(rows, after_frame)
    except RuntimeError:
        return None
    target_index = min(range(len(rows)), key=lambda index: abs(rows[index] - target_y))
    if after_index == target_index and after_index != before_index:
        return before_index, after_index
    return None


def _nonselectable_row_proven(before_frame, after_frame, rows: list[float], target_y: int):
    """Return the unchanged selected index only when the requested row stayed unselected."""
    if len(rows) < 2 or _profile_hash(before_frame) != _profile_hash(after_frame):
        return None
    try:
        before_index = _selected_row(rows, before_frame)
        after_index = _selected_row(rows, after_frame)
    except RuntimeError:
        return None
    target_index = min(range(len(rows)), key=lambda index: abs(rows[index] - target_y))
    if before_index == after_index and target_index > before_index:
        return before_index
    return None


def _stable_profile(nav, old_hash: str | None = None, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    last_hash = ""
    stable = 0
    frame = None
    while time.monotonic() < deadline:
        frame = nav._screenshot(nav.hwnd)
        digest = _profile_hash(frame)
        if digest and (old_hash is None or digest != old_hash):
            if digest == last_hash:
                stable += 1
            else:
                last_hash, stable = digest, 1
            if stable >= 2:
                return frame, digest
        else:
            last_hash, stable = "", 0
        time.sleep(0.12)
    return None, ""


def _recognize_consensus(
    profile_crop, engine, recognition_engine=None
) -> _RecognitionDecision:
    from wechat_ocr.extractor import _label_token, candidate_near_label

    tokens = engine.recognize(profile_crop)
    label = next((t for t in tokens if _label_token(t.text)[0]), None)
    if label is None:
        return _RecognitionDecision(None, "LABEL_NOT_FOUND")
    _, inline_value = _label_token(label.text)
    pad_x = max(4, round(label.height * 0.25))
    pad_y = max(4, round(label.height * 0.35))
    value_top = max(0, round(label.top - pad_y))
    value_bottom = min(profile_crop.height, round(label.bottom + pad_y))
    if inline_value:
        # RapidOCR can merge the fixed label and value into one detection box.
        # Keep that label-anchored box for all three views; the earlier version
        # cropped only to its right and silently discarded the inline value.
        value_left = max(0, round(label.left - pad_x))
        value_right = min(profile_crop.width, round(label.right + pad_x))
    else:
        value_left = max(0, round(label.right - pad_x * 0.25))
        value_right = profile_crop.width
    if value_right <= value_left + 8 or value_bottom <= value_top:
        return _RecognitionDecision(None, "VALUE_NOT_FOUND")
    value_crop = profile_crop.crop((value_left, value_top, value_right, value_bottom))
    raw = value_crop.convert("RGB")
    upscaled = raw.resize((raw.width * 2, raw.height * 2))
    contrast = ImageEnhance.Contrast(ImageOps.grayscale(raw)).enhance(1.5)
    views = (raw, upscaled, contrast)
    value_engine = recognition_engine or engine
    readings: list[tuple[str, float]] = []
    invalid_seen = False
    source_masked_candidate = ""
    for view in views:
        view_tokens = value_engine.recognize(view)
        anchored_value, anchored_score = candidate_near_label(view_tokens, DEFAULT_CONFIG)
        if anchored_value and contains_mask_placeholder(anchored_value):
            source_masked_candidate = anchored_value
            break
        if anchored_value and validate_wechat_id(anchored_value, DEFAULT_CONFIG).valid:
            readings.append((anchored_value, float(anchored_score or 0.0)))
            continue
        found = None
        for token in view_tokens:
            _, inline = _label_token(token.text)
            candidate = normalize_candidate(inline if inline else token.text, DEFAULT_CONFIG.labels)
            if contains_mask_placeholder(candidate):
                source_masked_candidate = candidate
                break
            if validate_wechat_id(candidate, DEFAULT_CONFIG).valid:
                found = (candidate, float(token.confidence))
                break
            invalid_seen = invalid_seen or bool(candidate)
        if source_masked_candidate:
            break
        readings.append(found or ("", 0.0))
    if source_masked_candidate:
        return _RecognitionDecision(
            source_masked_candidate,
            "SOURCE_VALUE_MASKED",
            "",
            _mask(source_masked_candidate),
        )
    counts = Counter(value for value, _ in readings if value)
    if not counts:
        status = "INVALID_CANDIDATE" if invalid_seen else "VALUE_NOT_FOUND"
        return _RecognitionDecision(None, status)
    value, count = counts.most_common(1)[0]
    if count < 2:
        best_value, best_score = max(readings, key=lambda item: item[1])
        return _RecognitionDecision(
            best_value,
            "OCR_DISAGREEMENT",
            f"{best_score:.5f}",
            _mask(best_value),
        )
    confidence = statistics.median(score for candidate, score in readings if candidate == value)
    if confidence < DEFAULT_CONFIG.min_confidence_confirm:
        return _RecognitionDecision(
            value, "LOW_CONFIDENCE", f"{confidence:.5f}", _mask(value)
        )
    return _RecognitionDecision(
        value, "CONFIRMED_BY_CONSENSUS", f"{confidence:.5f}"
    )


def _recognize_with_retries(profile_crop, engine, recognition_engine) -> tuple[_RecognitionDecision, int]:
    """Run bounded header passes on the same frame, then finalize one reason."""
    attempts = [profile_crop]
    attempts.append(profile_crop.resize((profile_crop.width * 2, profile_crop.height * 2)))
    enhanced = ImageEnhance.Contrast(ImageOps.grayscale(profile_crop)).enhance(1.5)
    attempts.append(enhanced.resize((enhanced.width * 2, enhanced.height * 2)))
    decision = _RecognitionDecision(None, "VALUE_NOT_FOUND")
    attempt_statuses = []
    for attempt, header in enumerate(attempts, start=1):
        decision = _recognize_consensus(
            header, engine, recognition_engine=recognition_engine
        )
        attempt_statuses.append(decision.status)
        if _confirmed_value(decision):
            return replace(decision, attempt_statuses=tuple(attempt_statuses)), attempt
        if decision.status in {"OCR_FAILED", "SOURCE_VALUE_MASKED"}:
            return replace(decision, attempt_statuses=tuple(attempt_statuses)), attempt
    return replace(decision, attempt_statuses=tuple(attempt_statuses)), len(attempts)


def _profile_crop(frame):
    width, height = frame.size
    l, t, r, b = PROFILE_CHANGE_ROI
    return frame.crop((round(l * width), round(t * height), round(r * width), round(b * height)))


def _click(hwnd: int, pid: int, nav, x: int, y: int, state: dict) -> tuple[bool, object, str]:
    import win32api
    import win32con

    _wait_foreground(hwnd, pid, state)
    old_frame = nav._screenshot(hwnd)
    old_hash = _profile_hash(old_frame)
    row_centers = state.get("previous_viewport_row_centers", [])
    transition = {
        "from_sequence": int(state.get("row_ordinal", 1)),
        "to_sequence": int(state.get("row_ordinal", 1)) + 1,
        "click_attempted": False,
        "target_row_found": bool(0 <= y < nav.mapper.size[1]),
        "profile_hash_before_prefix": old_hash[:12],
        "profile_hash_after_prefix": "",
        "profile_changed": False,
        "stable_frame_hash_prefix": "",
        "extraction_frame_hash_prefix": "",
        "status": "PENDING",
    }
    state.setdefault("transition_audit", []).append(transition)
    width, height = nav.mapper.size
    left, _, right, _ = CONTACT_LIST_ROI
    alternate_x = round(width * (left + (right - left) * 0.48))
    click_x_candidates = list(dict.fromkeys((x, alternate_x)))
    for offset in (0, 5, -5):
        target_y = y + offset
        for target_x in click_x_candidates:
            _wait_foreground(hwnd, pid, state)
            if not _foreground_ready(hwnd, pid):
                continue
            if not _normalized_point_in_bounds(target_x, target_y, width, height):
                transition.update({"status": "NAVIGATION_COORDINATE_OUT_OF_BOUNDS"})
                state["state"] = "NAVIGATION_COORDINATE_OUT_OF_BOUNDS"
                state["next_action"] = "REANCHOR_CURRENT_VIEWPORT"
                _persist(state)
                return False, old_frame, old_hash
            point = _safe_target_input_point(hwnd, nav, target_x, target_y)
            if point is None:
                transition.update({"status": "UNSAFE_INPUT_COORDINATE"})
                state["state"] = "UNSAFE_INPUT_COORDINATE"
                _persist(state)
                return False, old_frame, old_hash
            if not _foreground_ready(hwnd, pid):
                continue
            sx, sy = point
            win32api.SetCursorPos((sx, sy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            transition["click_attempted"] = True
            transition["click_x_client"] = target_x
            state["rows_clicked"] += 1
            _persist(state)
            frame, digest = _stable_profile(nav, old_hash, 2.0)
            if frame is not None and digest != old_hash:
                state["profiles_changed"] += 1
                transition.update({
                    "profile_hash_after_prefix": digest[:12],
                    "profile_changed": True,
                    "stable_frame_hash_prefix": digest[:12],
                    "status": "PROFILE_CHANGED",
                })
                _persist(state)
                return True, frame, digest
            if row_centers:
                current_frame = nav._screenshot(hwnd)
                selection = _row_selection_advanced(old_frame, current_frame, row_centers, target_y)
                if selection is not None:
                    before_index, after_index = selection
                    transition.update({
                        "row_selection_changed": True,
                        "selected_row_index_before": before_index,
                        "selected_row_index_after": after_index,
                        "profile_changed": False,
                        "status": "ROW_SELECTION_CHANGED_PROFILE_HASH_UNCHANGED",
                    })
                    _persist(state)
                    return True, current_frame, old_hash
    transition.update({
        "profile_hash_after_prefix": _profile_hash(frame)[:12] if frame is not None else old_hash[:12],
        "stable_frame_hash_prefix": _profile_hash(frame)[:12] if frame is not None else "",
        "status": "PROFILE_NOT_CHANGED",
    })
    _persist(state)
    return False, old_frame, old_hash


def run(
    target: int,
    *,
    resume: bool = False,
    ready_confirmed: bool = False,
    profile_limit: int | None = None,
) -> int:

    hwnd, pid = _locate_weixin_window()
    geometry = _client_geometry(hwnd)
    print(
        "TARGET_WINDOW " + json.dumps({
            "hwnd": hwnd, "pid": pid,
            "monitor_index": geometry["monitor_index"],
            "window_rect": geometry["window_screen_rect"],
            "client_size": geometry["client_size"],
            "monitor_rect": geometry["monitor_rect"],
            "monitor_is_primary": geometry["monitor_is_primary"],
            "virtual_screen_rect": geometry["virtual_screen_rect"],
        }, separators=(",", ":")), flush=True,
    )
    if not _read_target_ready(hwnd, geometry):
        raise RuntimeError("READ_TARGET_NOT_READY")
    print("READ_GATE=PASS", flush=True)
    if resume:
        if not CHECKPOINT.exists():
            raise RuntimeError("RESUME_CHECKPOINT_NOT_FOUND")
        baseline = _verify_resume_outputs()
        print("RESUME_OUTPUT_CONSISTENCY " + json.dumps(baseline, separators=(",", ":")), flush=True)
        if not baseline["consistent"]:
            raise RuntimeError("RESUME_OUTPUT_CONSISTENCY_FAILED")
    import win32gui
    import win32process

    foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
    foreground_pid = (
        int(win32process.GetWindowThreadProcessId(foreground_hwnd)[1])
        if foreground_hwnd else 0
    )
    print(
        "FOREGROUND " + json.dumps({
            "hwnd": foreground_hwnd, "pid": foreground_pid,
        }, separators=(",", ":")), flush=True,
    )
    print(
        "INPUT_GATE=" + ("PASS" if _foreground_ready(hwnd, pid) else "WAITING"),
        flush=True,
    )
    if not resume and not ready_confirmed:
        _ready_dialog()
    nav = Win32VisualNavigation(hwnd)
    engine = get_ocr_engine(DEFAULT_CONFIG)
    # Keep detector + recognizer enabled on the value crop. RapidOCR's
    # use_det=False result omits boxes, which OcrEngine._to_tokens requires.
    recognition_engine = engine
    first_frame, first_hash = _stable_profile(nav, timeout=2.0)
    if first_frame is None:
        raise RuntimeError("PROFILE_ROI_NOT_STABLE")
    first_thumb = _detect_scrollbar_thumb(first_frame)
    second_thumb = _detect_scrollbar_thumb(nav._screenshot(hwnd))
    scrollbar_sample_stable = bool(
        first_thumb and second_thumb
        and first_thumb["x"] == second_thumb["x"]
        and abs(first_thumb["thumb_top"] - second_thumb["thumb_top"]) <= 3
        and abs(first_thumb["thumb_bottom"] - second_thumb["thumb_bottom"]) <= 3
    )
    resume_scroll_progress = second_thumb["progress"] if scrollbar_sample_stable else None
    rows, spacing, calibration = _detect_rows(first_frame, engine)
    if resume:
        if not CHECKPOINT.exists():
            raise RuntimeError("RESUME_CHECKPOINT_NOT_FOUND")
        state = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        _ensure_navigation_state(state)
        if "profile_results" not in state or "confirmed_frame_hashes" not in state:
            raise RuntimeError("LEGACY_CHECKPOINT_HAS_NO_TERMINAL_ACCOUNTING; do not resume")
        if "seen_profile_hashes" not in state:
            raise RuntimeError("LEGACY_CHECKPOINT_HAS_NO_PROFILE_REVISIT_STATE; do not resume")
        recover_after_stall = (
            state.get("state") == "FORWARD_PROGRESS_STALLED"
            or state.get("pause_reason") == "FORWARD_PROGRESS_STALLED"
        )
        if (
            tuple(state.get("client_size", ())) != tuple(geometry["client_size"])
            or state.get("profile_hash") != first_hash
        ):
            selected_index = _selected_row(rows, first_frame)
            state["current_y"] = rows[selected_index]
            state["resume_reanchored"] = True
            state["resume_profile_action"] = _resume_profile_action(
                state.get("seen_profile_hashes", []), first_hash
            )
            state["profile_hash"] = first_hash
            state["next_action"] = state["resume_profile_action"]
        state["client_size"] = tuple(geometry["client_size"])
        expected_confirmed = int(state.get("confirmed_count", len(state.get("confirmed", []))))
        previous_hashes = state.get("confirmed_frame_hashes", {})
        state["confirmed_frame_hashes"] = {
            key if len(key) == 64 and all(ch in "0123456789abcdef" for ch in key.casefold())
            else _identifier_hash(key): value
            for key, value in previous_hashes.items()
        }
        state["confirmed"] = _load_confirmed_export(
            expected_confirmed, set(state["confirmed_frame_hashes"])
        )
        state["pid"] = os.getpid()
        state["target"] = target
        state["state"] = "RUNNING"
        state["calibration"] = calibration
        state["row_spacing"] = spacing
        state["profile_roi_valid"] = True
        state.setdefault(
            "next_action",
            "NAVIGATE_NEXT" if state.get("profiles_processed", 0) else "PROCESS_CURRENT",
        )
        state.setdefault("frame_mismatch_attempts", 0)
        state.setdefault("terminal_frame_mismatch_contacts", 0)
        state.setdefault("attempt_results", [])
        state.setdefault("transition_audit", [])
        state.setdefault("seen_profile_hashes", [])
        state.setdefault("seeded_confirmed_id_hashes", [])
        state.setdefault("benchmark_mode", False)
        state.setdefault("adaptive_scroll_controller", dict(BENCHMARK_SEED_SCROLL_CONTROLLER))
        state.setdefault("row_candidates_seen", 0)
        state.setdefault("navigation_telemetry", [])
        state.setdefault("telemetry_event_id", 0)
        state.setdefault("profile_revisits", 0)
        state.setdefault("unique_profiles_visited", state.get("profiles_visited", 0))
        state["profiles_visited"] = state["unique_profiles_visited"]
        state.setdefault("consecutive_profile_revisits", 0)
        state.setdefault("forward_progress_recoveries", 0)
        state.setdefault("forward_progress_stalls", 0)
        state.setdefault("scroll_extra_notches", 0)
        state.setdefault("scrolls_without_new_profile", 0)
        state.setdefault("successful_scroll_count", 0)
        state.setdefault("scroll_input_failure_count", 0)
        state.setdefault("scroll_recovery_attempts", 0)
        state.setdefault("recent_viewport_hashes", [])
        state.setdefault("previous_viewport_row_signatures", [])
        current_list_hash = _contact_list_hash(first_frame)
        if state.get("row_signature_version") != 3:
            state["previous_viewport_row_signatures"] = [
                _row_signature(first_frame, row_y, spacing) for row_y in rows
            ]
            state["recent_viewport_signatures"] = [current_list_hash]
            state["scrolls_without_new_profile"] = 0
            state["last_scroll_unique_profiles"] = state.get("unique_profiles_visited", 0)
            state["row_signature_version"] = 3
            state["previous_list_view_hash"] = current_list_hash
            state["current_list_view_hash"] = current_list_hash
        state.setdefault("recent_viewport_signatures", [])
        state.setdefault("profile_revisit_records", [])
        state.setdefault("health_checks", [])
        state.setdefault("navigation_metrics", {})
        state.setdefault("viewport_cursor", {
            "last_processed_row": state.get("current_y"),
            "overlap_boundary": None,
            "next_candidate_row": state.get("current_y"),
        })
        state.setdefault("forward_viewports_checked", 0)
        state.setdefault("forward_viewports_without_new_profile", 0)
        state.setdefault("direction_unverified_viewports", 0)
        state.setdefault("scroll_strategy", "WHEEL")
        state.setdefault("scrollbar_position_history", [])
        state["pending_forward_viewport"] = False
        state["pending_forward_new_profiles"] = 0
        state["forward_progress_probe_new_profiles"] = 0
        state["legacy_direction_unverified_attempts"] = _mark_legacy_scroll_attempts(
            state.get("scroll_attempts", [])
        )
        reassessed_unscanned = 0
        for attempt in state.get("scroll_attempts", []):
            if attempt.get("direction_status") == "FORWARD" and not attempt.get("rows_scanned"):
                attempt["counted_as_forward_viewport"] = False
                attempt["direction_reassessment"] = "FORWARD_DIRECTION_VERIFIED_ROWS_NOT_SCANNED"
                reassessed_unscanned += 1
        state["forward_viewports_reassessed_unscanned"] = reassessed_unscanned
        state["forward_viewports_checked"] = 0
        state["forward_viewports_without_new_profile"] = 0
        state["forward_progress_probe_started"] = True
        state["forward_direction_fix"] = "PENDING"
        state["current_scroll_progress"] = resume_scroll_progress
        state["scrollbar_thumb_detected"] = scrollbar_sample_stable
        last_scroll_attempt = state.get("scroll_attempts", [])[-1:]
        last_scroll_attempt = last_scroll_attempt[0] if last_scroll_attempt else {}
        if last_scroll_attempt.get("drag_attempted") and last_scroll_attempt.get(
            "direction_status"
        ) != "FORWARD":
            state["scrollbar_detection_reliable"] = False
            state["scrollbar_detector_failure_reason"] = "DETECTED_EDGE_FEATURE_DID_NOT_DRAG"
            state["scroll_strategy"] = "WHEEL"
            state["scroll_direction_method"] = "ROW_ANCHOR"
            state["scrollbar_thumb_detected"] = False
            state["current_scroll_progress"] = None
            last_scroll_attempt["direction_reassessment"] = "DIRECTION_UNVERIFIED_VIEWPORT"
            last_scroll_attempt["direction_reassessment_reason"] = (
                "thumb candidate moved with content and did not respond to drag"
            )
        if scrollbar_sample_stable and state.get("scroll_attempts"):
            last_direction_attempt = state["scroll_attempts"][-1]
            if last_direction_attempt.get("direction_status") in {
                "UPWARD", "NO_MOVEMENT", "SCROLL_OSCILLATION"
            } and state.get("scrollbar_detection_reliable") is not False:
                state["scroll_strategy"] = "SCROLLBAR_DRAG_PRIMARY"
        if recover_after_stall:
            state["scroll_recovery_attempts"] += 1
            state["force_forward_scroll"] = True
            state["scroll_extra_notches"] = 0
            state["scrolls_without_new_profile"] = 0
            state["last_scroll_unique_profiles"] = state.get("unique_profiles_visited", 0)
            state["state"] = "RUNNING"
            state["recovery_reason"] = "FORWARD_DIRECTION_PROBE"
        skip_profile_once = state.get("next_action") == "NAVIGATE_NEXT"
        if state.get("scroll_strategy") == "SCROLLBAR_DRAG_PRIMARY":
            state["force_forward_scroll"] = True
            state["next_action"] = "NAVIGATE_NEXT"
            skip_profile_once = True
        if state.get("scrollbar_detector_failure_reason"):
            state["scrollbar_detection_reliable"] = False
            state["scroll_direction_method"] = "ROW_ANCHOR"
            next_visible_row = _next_visible_row(
                rows, state.get("current_y", rows[0]), spacing,
                geometry["client_size"][1] * CONTACT_LIST_ROI[3],
            )
            state["force_forward_scroll"] = next_visible_row is None
            state["next_action"] = "NAVIGATE_NEXT"
            skip_profile_once = True
    else:
        current_index = _selected_row(rows, first_frame)
        state = {
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), "pid": os.getpid(),
            "target": target, "state": "RUNNING", "confirmed": [], "review": [],
            "benchmark_mode": bool(BENCHMARK_MODE),
            "rows_clicked": 0, "profiles_changed": 0, "frame_matches": 0,
            "frame_mismatches": 0, "duplicate_ids_skipped": 0, "row_click_failed": 0,
            "frame_mismatch_attempts": 0, "terminal_frame_mismatch_contacts": 0,
            "attempt_results": [], "transition_audit": [],
            "seen_profile_hashes": list(BENCHMARK_SEED_PROFILE_HASHES),
            "seeded_confirmed_id_hashes": list(BENCHMARK_SEED_CONFIRMED_ID_HASHES),
            "adaptive_scroll_controller": dict(BENCHMARK_SEED_SCROLL_CONTROLLER),
            "profile_revisits": 0, "row_candidates_seen": 0,
            "navigation_telemetry": [], "telemetry_event_id": 0,
            "unique_profiles_visited": 0, "consecutive_profile_revisits": 0,
            "forward_progress_recoveries": 0, "forward_progress_stalls": 0,
            "force_forward_scroll": False, "scroll_extra_notches": 0,
            "scrolls_without_new_profile": 0, "recent_viewport_signatures": [],
            "successful_scroll_count": 0, "scroll_input_failure_count": 0,
            "scroll_recovery_attempts": 0,
            "recent_viewport_hashes": [], "previous_viewport_row_signatures": [],
            "row_signature_version": 3,
            "last_scroll_unique_profiles": None, "profile_revisit_records": [],
            "health_checks": [],
            "viewports": 1, "viewport": 1, "row_ordinal": 1,
            "bootstrap_top_operator_attested": True,
            "profiles_processed": 0, "terminal_processed": 0, "profiles_visited": 0,
            "extractions_attempted": 0, "failed": 0, "profile_results": [],
            "confirmed_frame_hashes": {}, "duplicate_records": [],
            "first_10": {}, "first_10_gate": "PENDING",
            "first_20": {}, "first_20_gate": "PENDING",
            "row_spacing": spacing, "current_y": rows[current_index],
            "navigation_metrics": {},
            "viewport_cursor": {
                "last_processed_row": rows[current_index],
                "overlap_boundary": None,
                "next_candidate_row": rows[current_index],
            },
            "profile_hash": first_hash, "calibration": calibration,
            "client_size": tuple(geometry["client_size"]),
            "last_terminal_profile_hash": "",
            "profile_roi_valid": True, "last_hash_match": True,
            "next_action": "PROCESS_CURRENT",
        }
        skip_profile_once = bool(
            BENCHMARK_MODE and not resume and first_hash in BENCHMARK_SEED_PROFILE_HASHES
        )
    state["previous_viewport_row_signatures"] = [
        _row_signature(first_frame, row_y, spacing) for row_y in rows
    ]
    state["previous_viewport_row_centers"] = list(rows)
    state["current_list_view_hash"] = _contact_list_hash(first_frame)
    state.setdefault("last_candidate_index", _selected_row(rows, first_frame))
    if BENCHMARK_MODE and not state.get("navigation_telemetry"):
        _append_benchmark_viewport_event(
            state,
            viewport_hash=state["current_list_view_hash"],
            previous_viewport_hash="",
            direction="INITIAL",
            requested_units=None,
            anchor_displacement=None,
            visible_rows=rows,
            row_spacing=spacing,
            row_signatures=state["previous_viewport_row_signatures"],
            overlap_indices=[],
            candidate_indices=list(range(len(rows))),
        )
    _persist(state)
    _write_outputs(state)
    state.setdefault("first_20", {})
    state.setdefault("first_20_gate", "PENDING")
    print(f"CALIBRATION visible_rows={len(rows)} median_row_spacing={spacing:.2f} profile_roi_valid=true", flush=True)
    while not _run_target_reached(state, target, profile_limit):
        if not skip_profile_once:
            current_frame, current_hash = _stable_profile(nav, timeout=2.0)
            if current_frame is None:
                state["state"] = "NAVIGATION_UNRESOLVED"
                state["next_action"] = "RETRY_NAVIGATION_AFTER_OPERATOR_CHECK"
                _persist(state)
                _write_outputs(state)
                return 3
            else:
                if current_hash in state.get("seen_profile_hashes", []):
                    _register_profile_hash(state, current_hash)
                    _record_benchmark_profile_event(
                        state, current_hash, row_index=state.get("last_candidate_index"),
                        outcome="PROFILE_REVISIT", revisit=True, new_profile=False,
                        ocr_called=False,
                    )
                    state.setdefault("profile_revisit_records", []).append({
                        "row_ordinal": state["row_ordinal"],
                        "profile_hash_prefix": current_hash[:12],
                        "viewport": state.get("viewport", 0),
                    })
                    progress_action = _forward_progress_action(state)
                    state["next_action"] = "NAVIGATE_NEXT"
                    if progress_action == "FORWARD_PROGRESS_STALLED":
                        state["state"] = "FORWARD_PROGRESS_STALLED"
                        _persist(state)
                        _write_outputs(state)
                        return 3
                    state["state"] = progress_action if progress_action != "CONTINUE" else "RUNNING"
                    skip_profile_once = True
                    _persist(state)
                    continue
                navigation_hash = current_hash
                extraction_frame = current_frame
                extraction_hash = _profile_hash(extraction_frame)
                expected_hash = state.get("profile_hash", "")
                frame_valid, frame_error = _same_current_frame(
                    navigation_hash, extraction_hash, expected_hash
                )
                if frame_error == "PROFILE_NOT_CHANGED":
                    state["state"] = "NAVIGATION_UNRESOLVED"
                    state["next_action"] = "RETRY_NAVIGATION_AFTER_OPERATOR_CHECK"
                    _persist(state)
                    _write_outputs(state)
                    return 3
                _register_profile_hash(state, current_hash)
                _record_benchmark_profile_event(
                    state, current_hash, row_index=state.get("last_candidate_index"),
                    outcome="NEW_PROFILE", revisit=False, new_profile=True,
                    ocr_called=False,
                )
                if state.get("forward_progress_probe_started") and state.get("scroll_attempts"):
                    state["scroll_attempts"][-1]["new_profile_found"] = True
                    state["scroll_attempts"][-1]["diagnosis"] = "FORWARD_PROGRESS_RECOVERED"
                    _mark_forward_progress_recovered(state)
                    _atomic_json(OUT / "scroll_diagnostics.json", {
                        "attempts": [state["scroll_attempts"][-1]],
                        "diagnosis": "FORWARD_PROGRESS_RECOVERED",
                    })
                contact_state = _ContactExtractionState(sequence_index=state["row_ordinal"])
                attempt_count = 0
                attempt_statuses = []
                state["frame_matches"] += int(frame_valid)
                state["frame_mismatches"] += int(not frame_valid)
                state["frame_mismatch_attempts"] += int(not frame_valid)
                state["last_hash_match"] = frame_valid
                if not frame_valid or frame_error:
                    outcome, candidate_masked, confidence, status = None, "", "", frame_error
                    attempt_statuses = [frame_error]
                else:
                    crop = _profile_crop(extraction_frame)
                    try:
                        state["extractions_attempted"] = state.get("extractions_attempted", 0) + 1
                        decision, attempt_count = _recognize_with_retries(
                            crop, engine, recognition_engine
                        )
                        state["extractions_attempted"] += max(0, attempt_count - 1)
                        outcome = _confirmed_value(decision)
                        status = decision.status
                        attempt_statuses = list(decision.attempt_statuses)
                        confidence = decision.confidence
                        candidate_masked = decision.candidate_masked
                    except Exception as exc:  # noqa: BLE001 - OCR backends raise varied runtime errors
                        outcome, status, confidence = None, "OCR_FAILED", ""
                        candidate_masked = ""
                        attempt_count = 1
                        attempt_statuses = ["OCR_FAILED"]
                        state["last_ocr_error_type"] = type(exc).__name__
                for attempt_index, attempt_status in enumerate(attempt_statuses or [status], 1):
                    contact_state.record_attempt(
                        attempt_index=attempt_index,
                        expected_hash=expected_hash,
                        extraction_hash=extraction_hash,
                        hash_match=frame_valid,
                        status=attempt_status,
                    )
                status = contact_state.terminal_status(status)
                state.setdefault("attempt_results", []).extend(contact_state.attempt_results)
                if outcome:
                    outcome_id_hash = _identifier_hash(outcome)
                    seen_hashes = set(state.get("confirmed_frame_hashes", {}))
                    seen_hashes.update(state.get("seeded_confirmed_id_hashes", []))
                    if outcome_id_hash in seen_hashes:
                        prior_hash = state["confirmed_frame_hashes"].get(
                            outcome_id_hash, ""
                        )
                        status = "DUPLICATE_WECHAT_ID_ANOMALY"
                        state["duplicate_ids_skipped"] += 1
                        state["duplicate_records"].append({
                            "row_ordinal": state["row_ordinal"],
                            "status": status,
                            "reason": status,
                            "candidate_masked": _mask(outcome),
                            "frame_hash_prefix": navigation_hash[:12],
                            "prior_frame_hash_prefix": prior_hash[:12],
                        })
                    else:
                        state["confirmed"].append({
                            "row_ordinal": state["row_ordinal"], "wechat_id": outcome,
                            "status": "CONFIRMED_BY_CONSENSUS", "source": "label_anchored_3_view_consensus",
                            "confidence": confidence,
                        })
                        state["confirmed_frame_hashes"][outcome_id_hash] = navigation_hash
                        status = "CONFIRMED_BY_CONSENSUS"
                _update_benchmark_ocr_event(
                    state, ocr_called=attempt_count > 0,
                    ocr_call_count=attempt_count,
                    status=status, confidence=confidence,
                )
                if not outcome:
                    state["review"].append(_review_result(
                        state, row_ordinal=state["row_ordinal"], reason=status,
                        candidate_masked=candidate_masked, confidence=confidence,
                        frame_hash=navigation_hash,
                    ))
                result_reason = status
                state["last_terminal_profile_hash"] = navigation_hash
                if state.get("transition_audit"):
                    transition = state["transition_audit"][-1]
                    if transition.get("to_sequence") == state["row_ordinal"]:
                        transition["extraction_frame_hash_prefix"] = extraction_hash[:12]
                state["failed"] = state.get("failed", 0) + int(
                    status not in {"CONFIRMED_BY_CONSENSUS", "DUPLICATE_WECHAT_ID_ANOMALY"}
                )
                _terminal_result(
                    state, row_ordinal=state["row_ordinal"], status=status,
                    reason=result_reason, candidate_masked=candidate_masked,
                    confidence=confidence, frame_hash=navigation_hash,
                    attempt_count=attempt_count,
                    expected_frame_hash=expected_hash,
                    extraction_frame_hash=extraction_hash,
                )
                _persist(state)
                _write_outputs(state)
                if status == "PROFILE_NOT_CHANGED":
                    state["state"] = "NAVIGATION_BLOCKED_PROFILE_NOT_CHANGED"
                    state["next_action"] = "RETRY_NAVIGATION_AFTER_OPERATOR_CHECK"
                    _persist(state)
                    return 3
            state["next_action"] = "NAVIGATE_NEXT"
            _persist(state)
            skip_profile_once = False
            if target == 10 and state["profiles_processed"] >= 10 and state["first_10_gate"] == "PENDING":
                state["first_10"] = {
                    "rows_processed": 10,
                    "profiles_changed": state["profiles_changed"],
                    "frame_matches": state["frame_matches"],
                    "frame_mismatches": state["frame_mismatches"],
                    "confirmed": len(state["confirmed"]), "review": len(state["review"]),
                }
                passed = (
                    state["profiles_changed"] >= 8
                    and state["frame_matches"] == state["profiles_processed"]
                    and state["frame_mismatches"] == 0
                    and len(state["confirmed"]) >= 1
                )
                state["first_10_gate"] = "PASS" if passed else "FAIL"
                state["first_10"]["status"] = state["first_10_gate"]
                _persist(state)
                _write_outputs(state)
                if not passed:
                    state["state"] = "FIRST_10_VALIDATION_BLOCKED"
                    _persist(state)
                    return 3
                if target == 10:
                    state["state"] = "FIRST_10_VALIDATION_PASSED"
                    _persist(state)
                    return 0
            if (
                target == 20
                and state["terminal_processed"] >= 20
                and state["first_20_gate"] == "PENDING"
            ):
                passed, state["first_20"] = _evaluate_20_preflight(state)
                state["first_20_gate"] = "PASS" if passed else "FAIL"
                state["first_20"]["status"] = state["first_20_gate"]
                _persist(state)
                _write_outputs(state)
                if not passed:
                    state["state"] = "TWENTY_CONTACT_PREFLIGHT_BLOCKED"
                    _persist(state)
                    return 3
                if target == 20:
                    state["state"] = "TWENTY_CONTACT_PREFLIGHT_PASSED"
                    _persist(state)
                    return 0
        else:
            skip_profile_once = False
        if _run_target_reached(state, target, profile_limit):
            break

        next_y = _next_visible_row(
            rows, state["current_y"], spacing,
            geometry["client_size"][1] * CONTACT_LIST_ROI[3],
        )
        if next_y is not None and not state.get("force_forward_scroll"):
            candidate_index = min(range(len(rows)), key=lambda index: abs(rows[index] - next_y))
            state["last_candidate_index"] = candidate_index
            _record_benchmark_candidate_click(state, candidate_index)
            click_x = round(geometry["client_size"][0] * (CONTACT_LIST_ROI[0] + (CONTACT_LIST_ROI[2] - CONTACT_LIST_ROI[0]) * 0.70))
            state["row_candidates_seen"] = state.get("row_candidates_seen", 0) + 1
            clicks_before = int(state.get("rows_clicked", 0))
            moved, _new_frame, new_hash = _click(hwnd, pid, nav, click_x, round(next_y), state)
            _update_benchmark_candidate_clicks(
                state, int(state.get("rows_clicked", 0)) - clicks_before
            )
            if moved:
                state["profile_hash"] = new_hash
                state["current_y"] = next_y
                state.setdefault("viewport_cursor", {})["last_processed_row"] = next_y
                state["viewport_cursor"]["next_candidate_row"] = _next_visible_row(
                    rows, next_y, spacing,
                    geometry["client_size"][1] * CONTACT_LIST_ROI[3],
                )
                state["nonselectable_list_row_streak"] = 0
                if not _advance_sequence_after_transition(state, state["transition_audit"][-1]):
                    raise RuntimeError("STATE_INVARIANT_VIOLATION: sequence advanced without profile change")
                state["next_action"] = "PROCESS_CURRENT"
            else:
                before_frame = state.get("last_failed_click_frame", _new_frame)
                after_frame = nav._screenshot(hwnd)
                unchanged_selected_index = _nonselectable_row_proven(
                    before_frame, after_frame,
                    state.get("previous_viewport_row_centers", rows), round(next_y),
                )
                if (
                    unchanged_selected_index is not None
                    and state.get("nonselectable_list_row_streak", 0) < 2
                ):
                    skip_record = {
                        "row_center_client_y": round(next_y),
                        "selected_row_index": unchanged_selected_index,
                        "profile_hash_prefix": _profile_hash(after_frame)[:12],
                        "reason": "TARGET_ROW_REMAINED_UNSELECTED_AFTER_GATED_CLICKS",
                    }
                    state.setdefault("nonselectable_list_rows_skipped", []).append(skip_record)
                    state["nonselectable_list_row_count"] = state.get(
                        "nonselectable_list_row_count", 0
                    ) + 1
                    state["nonselectable_list_row_streak"] = state.get(
                        "nonselectable_list_row_streak", 0
                    ) + 1
                    state["current_y"] = next_y
                    state["state"] = "RUNNING"
                    state["next_action"] = "NAVIGATE_NEXT"
                    _persist(state)
                    continue
                state["state"] = "NAVIGATION_UNRESOLVED"
                state["next_action"] = "RETRY_NAVIGATION_AFTER_OPERATOR_CHECK"
                _persist(state)
                _write_outputs(state)
                return 3
            _persist(state)
            _write_outputs(state)
        else:
            if state.get("pending_forward_viewport"):
                completion = _complete_forward_viewport(state)
                if completion == "FORWARD_VIEWPORT_UNSCANNED":
                    state["state"] = completion
                    state["next_action"] = "RETRY_AFTER_VIEWPORT_SCAN"
                    _persist(state)
                    _write_outputs(state)
                    return 3
                at_bottom = state.get("current_scroll_progress") is not None and state[
                    "current_scroll_progress"
                ] >= 0.98
                if at_bottom and state.get("forward_viewports_without_new_profile", 0) >= 1:
                    state["state"] = "END_OF_CONTACT_LIST"
                    state["next_action"] = "CONTACT_LIST_END_VERIFIED"
                    if state.get("scroll_attempts"):
                        state["scroll_attempts"][-1]["diagnosis"] = "END_OF_CONTACT_LIST"
                    _persist(state)
                    _write_outputs(state)
                    return 3
                if completion == "FORWARD_PROGRESS_STALLED":
                    state["state"] = "FORWARD_PROGRESS_STALLED"
                    state["next_action"] = "RETRY_AFTER_NEW_DIRECTION_EVIDENCE"
                    if state.get("scroll_attempts"):
                        state["scroll_attempts"][-1]["diagnosis"] = "FORWARD_PROGRESS_STALLED"
                        state["scroll_attempts"][-1]["forward_viewports_without_new_profile"] = 5
                    _persist(state)
                    _write_outputs(state)
                    return 3
            _wait_foreground(hwnd, pid, state)
            import win32api
            import win32con
            l, t, r, b = CONTACT_LIST_ROI
            cursor_client_x = round((l + r) / 2 * geometry["client_size"][0])
            cursor_client_y = round((t + b) / 2 * geometry["client_size"][1])
            point = _safe_target_input_point(hwnd, nav, cursor_client_x, cursor_client_y)
            if point is None:
                state["state"] = "UNSAFE_INPUT_COORDINATE"
                state["next_action"] = "RETRY_AFTER_TARGET_GEOMETRY_CHECK"
                _persist(state)
                _write_outputs(state)
                return 3
            sx, sy = point
            base_notches = min(
                3,
                max(1, round(((b - t) * geometry["client_size"][1] / spacing) * 0.70 / 3)),
            )
            saved_controller = state.get("adaptive_scroll_controller", {})
            notches = int(saved_controller.get("current_scroll_units", base_notches))
            notches = max(1, notches + int(state.get("scroll_extra_notches", 0)))
            state["force_forward_scroll"] = False
            state["scroll_extra_notches"] = 0
            before_frame = nav._screenshot(hwnd)
            before_digest = _contact_list_hash(before_frame)
            before_rows, spacing, _ = _detect_rows(before_frame, engine)
            controller = _load_scroll_controller(
                state, len(before_rows), spacing, notches
            )
            notches = min(controller.max_scroll_units, controller.current_scroll_units)
            state["adaptive_scroll_controller"] = controller.to_dict()
            before_signatures = [
                _row_signature(before_frame, row_y, spacing) for row_y in before_rows
            ]
            before_thumb = (
                _detect_scrollbar_thumb(before_frame)
                if state.get("scrollbar_detection_reliable") is not False else None
            )
            state["previous_list_view_hash"] = before_digest
            fresh = before_frame
            after_thumb = before_thumb
            changed = False
            attempts = []
            direction_status = "DIRECTION_UNVERIFIED_VIEWPORT"
            if state.get("scroll_strategy") == "SCROLLBAR_DRAG_PRIMARY":
                if before_thumb is None:
                    direction_status = "DIRECTION_UNVERIFIED_VIEWPORT"
                else:
                    thumb_center_y = (before_thumb["thumb_top"] + before_thumb["thumb_bottom"]) // 2
                    before_point = _safe_target_input_point(
                        hwnd, nav, before_thumb["x"], thumb_center_y
                    )
                    foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
                    drag_foreground_pid = int(
                        win32process.GetWindowThreadProcessId(foreground_hwnd)[1]
                    ) if foreground_hwnd else 0
                    drag_succeeded = _drag_scrollbar_thumb(hwnd, pid, nav, before_thumb, state)
                    fresh = nav._screenshot(hwnd)
                    after_thumb = _detect_scrollbar_thumb(fresh)
                    after_digest = _contact_list_hash(fresh)
                    drag_rows, drag_spacing, _ = _detect_rows(fresh, engine)
                    drag_signatures = [
                        _row_signature(fresh, row_y, drag_spacing) for row_y in drag_rows
                    ]
                    _anchor_direction, anchor_shift, anchor_count = _row_anchor_direction(
                        before_signatures, before_rows, drag_signatures, drag_rows,
                    )
                    scrollbar_direction = _scrollbar_direction(
                        before_thumb.get("progress"),
                        after_thumb.get("progress") if after_thumb else None,
                    )
                    direction_status, scrollbar_conflict = _combine_scroll_direction_evidence(
                        _anchor_direction, anchor_count, scrollbar_direction
                    )
                    if scrollbar_conflict:
                        state["scrollbar_detection_reliable"] = False
                        state["scrollbar_detector_failure_reason"] = "SCROLLBAR_ROW_ANCHOR_CONFLICT"
                        state["scroll_strategy"] = "WHEEL"
                    attempt = {
                        "scroll_attempt_index": 1,
                        "wheel_requested_direction": "NOT_USED",
                        "wheel_delta": 0,
                        "strategy": "SCROLLBAR_DRAG_PRIMARY",
                        "drag_attempted": drag_succeeded,
                        "drag_distance_px": _scrollbar_drag_delta(
                            before_thumb["track_top"], before_thumb["track_bottom"]
                        ),
                        "cursor_client_x": before_thumb["x"],
                        "cursor_client_y": thumb_center_y,
                        "cursor_screen_x": before_point[0] if before_point else None,
                        "cursor_screen_y": before_point[1] if before_point else None,
                        "foreground_pid": drag_foreground_pid,
                        "weixin_foreground": drag_foreground_pid == pid,
                        "direction_method": "ROW_ANCHOR_SCROLLBAR_DRAG",
                        "direction_status": direction_status,
                        "scrollbar_direction_status": scrollbar_direction,
                        "scrollbar_thumb_top_before": before_thumb["thumb_top"],
                        "scrollbar_thumb_bottom_before": before_thumb["thumb_bottom"],
                        "scrollbar_thumb_top_after": after_thumb.get("thumb_top") if after_thumb else None,
                        "scrollbar_thumb_bottom_after": after_thumb.get("thumb_bottom") if after_thumb else None,
                        "scrollbar_track_top": before_thumb["track_top"],
                        "scrollbar_track_bottom": before_thumb["track_bottom"],
                        "scroll_progress_before": before_thumb["progress"],
                        "scroll_progress_after": after_thumb.get("progress") if after_thumb else None,
                        "list_hash_before": before_digest,
                        "list_hash_after": after_digest,
                        "list_changed": _list_view_changed(before_digest, after_digest),
                        "visible_row_count_before": len(before_rows),
                        "visible_row_count_after": len(_detect_rows(fresh, engine)[0]),
                        "row_anchor_shared_count": anchor_count,
                        "row_anchor_median_shift": anchor_shift,
                        "requested_scroll_units": None,
                        "revisit_count": int(state.get("profile_revisits", 0)),
                        "new_profile_found": False,
                        "counted_as_forward_viewport": direction_status == "FORWARD",
                    }
                    attempts.append(attempt)
                    state["scroll_attempts"] = (state.get("scroll_attempts", []) + [attempt])[-50:]
                    if direction_status == "FORWARD":
                        changed = True
                        state["current_scroll_progress"] = after_thumb["progress"]
                        state["scroll_direction_method"] = "SCROLLBAR_DRAG"
            for scroll_attempt, wheel_notches in enumerate(
                _scroll_escalation(notches, controller.max_scroll_units)
                if state.get("scroll_strategy") != "SCROLLBAR_DRAG_PRIMARY" else (),
                1,
            ):
                _wait_foreground(hwnd, pid, state)
                point = _safe_target_input_point(hwnd, nav, cursor_client_x, cursor_client_y)
                foreground_ok = _foreground_ready(hwnd, pid)
                if point is None:
                    state["state"] = "UNSAFE_INPUT_COORDINATE"
                    state["next_action"] = "RETRY_AFTER_TARGET_GEOMETRY_CHECK"
                    _persist(state)
                    _write_outputs(state)
                    return 3
                if not foreground_ok:
                    continue
                sx, sy = point
                win32api.SetCursorPos((sx, sy))
                cursor_inside = l <= (l + r) / 2 <= r and t <= (t + b) / 2 <= b
                if not cursor_inside or not _foreground_ready(hwnd, pid):
                    continue
                wheel_foreground = int(win32gui.GetForegroundWindow() or 0)
                wheel_foreground_pid = int(
                    win32process.GetWindowThreadProcessId(wheel_foreground)[1]
                ) if wheel_foreground else 0
                if not _foreground_ready(hwnd, pid):
                    continue
                attempt_before_frame = nav._screenshot(hwnd)
                attempt_before = _contact_list_hash(attempt_before_frame)
                fresh = attempt_before_frame
                after_rows = list(before_rows)
                after_spacing = spacing
                pulse_signatures = list(before_signatures)
                wheel_pulses_sent = 0
                cumulative_displacement = 0.0
                safe_displacement = max(0.0, (len(before_rows) - 1) * spacing)
                previous = attempt_before
                for _pulse in range(max(1, int(wheel_notches))):
                    if not _scroll_pulse_budget_remaining(
                        cumulative_displacement,
                        controller.target_displacement,
                        safe_displacement,
                    ):
                        break
                    if not _foreground_ready(hwnd, pid):
                        break
                    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120, 0)
                    wheel_pulses_sent += 1
                    time.sleep(0.30)
                    pulse_frame = nav._screenshot(hwnd)
                    digest = _contact_list_hash(pulse_frame)
                    stable = 0
                    deadline = time.monotonic() + 0.75
                    while time.monotonic() < deadline:
                        time.sleep(0.15)
                        pulse_frame = nav._screenshot(hwnd)
                        next_digest = _contact_list_hash(pulse_frame)
                        stable = stable + 1 if next_digest == digest else 0
                        digest = next_digest
                        if stable >= 2:
                            break
                    try:
                        pulse_rows, pulse_spacing, _ = _detect_rows(pulse_frame, engine)
                    except RuntimeError:
                        break
                    current_pulse_signatures = [
                        _row_signature(pulse_frame, row_y, pulse_spacing)
                        for row_y in pulse_rows
                    ]
                    pulse_direction, pulse_shift, pulse_anchor_count = _row_anchor_direction(
                        pulse_signatures, after_rows, current_pulse_signatures, pulse_rows
                    )
                    if pulse_direction != "FORWARD" or pulse_anchor_count < 2:
                        break
                    cumulative_displacement += abs(float(pulse_shift or 0.0))
                    fresh = pulse_frame
                    previous = digest
                    after_rows = pulse_rows
                    after_spacing = pulse_spacing
                    pulse_signatures = current_pulse_signatures
                    changed = True
                after_signatures = [
                    _row_signature(fresh, row_y, after_spacing) for row_y in after_rows
                ]
                after_thumb = (
                    _detect_scrollbar_thumb(fresh)
                    if state.get("scrollbar_detection_reliable") is not False else None
                )
                scrollbar_direction = None
                if before_thumb is not None and after_thumb is not None:
                    scrollbar_direction = _scrollbar_direction(
                        before_thumb["progress"], after_thumb["progress"]
                    )
                _anchor_direction, anchor_shift, anchor_count = _row_anchor_direction(
                    before_signatures, before_rows,
                    after_signatures, after_rows,
                )
                direction_status, scrollbar_conflict = _combine_scroll_direction_evidence(
                    _anchor_direction, anchor_count, scrollbar_direction
                )
                direction_method = "ROW_ANCHOR"
                if scrollbar_direction is not None:
                    direction_method = (
                        "ROW_ANCHOR_SCROLLBAR_CONFLICT" if scrollbar_conflict
                        else "ROW_ANCHOR+SCROLLBAR"
                    )
                if scrollbar_conflict:
                    state["scrollbar_detection_reliable"] = False
                    state["scrollbar_detector_failure_reason"] = "SCROLLBAR_ROW_ANCHOR_CONFLICT"
                    state["scroll_strategy"] = "WHEEL"
                before_progress = before_thumb.get("progress") if before_thumb else None
                after_progress = after_thumb.get("progress") if after_thumb else None
                attempt = {
                    "scroll_attempt_index": scroll_attempt,
                    "wheel_requested_direction": "DOWN",
                    "cursor_client_x": cursor_client_x,
                    "cursor_client_y": cursor_client_y,
                    "cursor_screen_x": sx, "cursor_screen_y": sy,
                    "foreground_pid": wheel_foreground_pid,
                    "cursor_inside_contact_list_roi": cursor_inside,
                    "weixin_foreground": wheel_foreground_pid == pid,
                    "wheel_delta": -wheel_pulses_sent * 120,
                    "requested_scroll_units": wheel_notches,
                    "wheel_pulses_sent": wheel_pulses_sent,
                    "cumulative_anchor_displacement": round(cumulative_displacement, 3),
                    "list_hash_before": attempt_before,
                    "list_hash_after": previous,
                    "list_changed": changed,
                    "visible_row_count_before": len(before_rows),
                    "visible_row_count_after": len(after_rows),
                    "direction_method": direction_method,
                    "direction_status": direction_status,
                    "scrollbar_direction_status": scrollbar_direction,
                    "counted_as_forward_viewport": False,
                    "scrollbar_thumb_top_before": before_thumb.get("thumb_top") if before_thumb else None,
                    "scrollbar_thumb_bottom_before": before_thumb.get("thumb_bottom") if before_thumb else None,
                    "scrollbar_thumb_top_after": after_thumb.get("thumb_top") if after_thumb else None,
                    "scrollbar_thumb_bottom_after": after_thumb.get("thumb_bottom") if after_thumb else None,
                    "scrollbar_track_top": before_thumb.get("track_top") if before_thumb else None,
                    "scrollbar_track_bottom": before_thumb.get("track_bottom") if before_thumb else None,
                    "scroll_progress_before": before_progress,
                    "scroll_progress_after": after_progress,
                    "row_anchor_shared_count": anchor_count,
                    "row_anchor_median_shift": anchor_shift,
                    "revisit_count": int(state.get("profile_revisits", 0)),
                    "new_profile_found": False,
                    "new_profile_count": 0,
                }

                oscillation_detected = False
                if before_progress is not None and after_progress is not None:
                    position_history = state.setdefault("scrollbar_position_history", [])
                    if not position_history or position_history[-1].get("progress") != before_progress:
                        position_history.append({
                            "progress": before_progress,
                            "viewport_hash": attempt_before,
                        })
                    oscillation_detected = _scroll_oscillation(
                        position_history, after_progress, previous
                    )
                    position_history.append({
                        "progress": after_progress,
                        "viewport_hash": previous,
                    })
                    del position_history[:-8]
                    if oscillation_detected:
                        direction_status = "SCROLL_OSCILLATION"
                        state["scroll_oscillation_count"] = state.get("scroll_oscillation_count", 0) + 1
                        attempt["oscillation_detected"] = True

                if direction_status in {"UPWARD", "NO_MOVEMENT", "SCROLL_OSCILLATION"} and before_thumb and after_thumb:
                    state["scroll_strategy"] = _scroll_strategy_for_direction(
                        direction_status, thumb_detected=True
                    )
                    attempt["strategy_switch"] = state["scroll_strategy"]
                    attempt["drag_distance_px"] = _scrollbar_drag_delta(
                        before_thumb["track_top"], before_thumb["track_bottom"]
                    )
                if direction_status == "FORWARD":
                    changed = True
                    state["current_scroll_progress"] = after_progress
                    state["scroll_direction_method"] = direction_method
                    attempt["counted_as_forward_viewport"] = direction_status == "FORWARD"
                elif direction_status == "UPWARD":
                    state["verified_upward_scrolls"] = state.get("verified_upward_scrolls", 0) + 1
                    state["scroll_strategy"] = _scroll_strategy_for_direction(
                        direction_status, thumb_detected=bool(before_thumb and after_thumb)
                    )
                elif direction_status == "NO_MOVEMENT":
                    state["scroll_no_movement_count"] = state.get("scroll_no_movement_count", 0) + 1
                    state["scroll_strategy"] = _scroll_strategy_for_direction(
                        direction_status, thumb_detected=bool(before_thumb and after_thumb)
                    )
                attempts.append(attempt)
                state["scroll_attempts"] = (state.get("scroll_attempts", []) + [attempt])[-50:]
                _atomic_json(OUT / "scroll_diagnostics.json", {
                    "attempts": attempts,
                    "diagnosis": direction_status,
                })
                if changed:
                    break
                if direction_status in {"UPWARD", "NO_MOVEMENT", "SCROLL_OSCILLATION"}:
                    break
                if _list_view_changed(attempt_before, previous):
                    break
            if not changed:
                if direction_status == "NO_ANCHOR_OVERLAP":
                    controller.current_scroll_units = max(1, notches - 1)
                    controller.direction = direction_status
                    controller.confidence = "LOW"
                    state["adaptive_scroll_controller"] = controller.to_dict()
                    state["scroll_strategy"] = "WHEEL"
                if (
                    state.get("scroll_strategy") == "SCROLLBAR_DRAG_PRIMARY"
                    and before_thumb is not None and after_thumb is not None
                    and any(
                        item.get("strategy_switch") == "SCROLLBAR_DRAG_PRIMARY"
                        and item.get("wheel_delta") not in (None, 0)
                        for item in attempts
                    )
                ):
                    state["state"] = "RUNNING"
                    state["next_action"] = "NAVIGATE_NEXT"
                    state["force_forward_scroll"] = True
                    if attempts:
                        attempts[-1]["diagnosis"] = "WHEEL_DIRECTION_INVERTED"
                        attempts[-1]["strategy_switch"] = "SCROLLBAR_DRAG_PRIMARY"
                        attempts[-1]["counted_as_forward_viewport"] = False
                        state["scroll_attempts"][-1].update(attempts[-1])
                    _persist(state)
                    _write_outputs(state)
                    skip_profile_once = True
                    continue
                wheel_attempts = [item for item in attempts if item.get("wheel_delta") not in (None, 0)]
                no_hash_change = bool(wheel_attempts) and all(
                    not item["list_changed"] for item in wheel_attempts
                )
                if (
                    after_thumb is not None
                    and after_thumb.get("progress", 0) >= 0.98
                    and state.get("forward_viewports_without_new_profile", 0) >= 1
                ):
                    state["state"] = "END_OF_CONTACT_LIST"
                    state["next_action"] = "CONTACT_LIST_END_VERIFIED"
                    if attempts:
                        attempts[-1]["diagnosis"] = "END_OF_CONTACT_LIST"
                    _persist(state)
                    _write_outputs(state)
                    return 3
                state["scroll_input_failure_count"] = state.get("scroll_input_failure_count", 0) + int(no_hash_change)
                state["last_scroll_diagnostic"] = {
                    "attempts": attempts,
                    "diagnosis": "SCROLL_INPUT_FAILED" if no_hash_change else "SCROLL_DIRECTION_UNRESOLVED",
                }
                _atomic_json(OUT / "scroll_diagnostics.json", state["last_scroll_diagnostic"])
                state["state"] = state["last_scroll_diagnostic"]["diagnosis"]
                state["next_action"] = "RETRY_SCROLL_INPUT_AFTER_OPERATOR_CHECK" if no_hash_change else "RETRY_AFTER_DIRECTION_DIAGNOSTIC"
                _persist(state)
                _write_outputs(state)
                return 3
            if direction_status != "FORWARD":
                changed = False
            if not changed:
                state["state"] = "SCROLL_OSCILLATION" if direction_status == "SCROLL_OSCILLATION" else "SCROLL_DIRECTION_UNRESOLVED"
                state["next_action"] = "RETRY_AFTER_DIRECTION_DIAGNOSTIC"
                _persist(state)
                _write_outputs(state)
                return 3
            after_digest = _contact_list_hash(fresh)
            previous_viewport_hash = state.get("current_list_view_hash", before_digest)
            state["previous_list_view_hash"] = before_digest
            state["current_list_view_hash"] = after_digest
            recent_hashes = state.setdefault("recent_viewport_hashes", [])
            recent_hashes.append(after_digest)
            del recent_hashes[:-8]
            new_rows, spacing, calibration = _detect_rows(fresh, engine)
            rows = new_rows
            current_row_signatures = [
                _row_signature(fresh, row_y, spacing) for row_y in new_rows
            ]
            scan_plan = _build_viewport_scan_plan(
                int(state.get("viewport", 0)) + 1,
                state.get("previous_viewport_row_signatures", []),
                current_row_signatures,
                new_rows,
                last_processed_anchor=state.get("viewport_cursor", {}).get("last_processed_row"),
                scroll_direction=direction_status,
            )
            overlap_count = len(scan_plan.overlap_rows)
            state["adjacent_overlap_rows_skipped"] = (
                state.get("adjacent_overlap_rows_skipped", 0) + overlap_count
            )
            state["last_viewport_scan_plan"] = {
                "viewport_id": scan_plan.viewport_id,
                "visible_rows": list(scan_plan.visible_rows),
                "overlap_rows": list(scan_plan.overlap_rows),
                "candidate_new_rows": list(scan_plan.candidate_new_rows),
                "last_processed_anchor": scan_plan.last_processed_anchor,
                "scroll_direction": scan_plan.scroll_direction,
                "confidence": scan_plan.confidence,
            }
            overlap_boundary = (
                scan_plan.overlap_rows[-1] if scan_plan.overlap_rows else None
            )
            next_candidate = (
                scan_plan.candidate_new_rows[0]
                if scan_plan.candidate_new_rows else None
            )
            state["viewport_cursor"] = {
                "last_processed_row": state.get("current_y"),
                "overlap_boundary": overlap_boundary,
                "next_candidate_row": (
                    new_rows[next_candidate] if next_candidate is not None else None
                ),
            }
            state["previous_viewport_row_signatures"] = current_row_signatures
            state["previous_viewport_row_centers"] = list(new_rows)
            actual_units = int(
                (attempts[-1].get("wheel_pulses_sent") or attempts[-1].get("requested_scroll_units"))
                or notches
            ) if attempts else notches
            anchor_displacement = (
                attempts[-1].get("row_anchor_median_shift") if attempts else None
            )
            scroll_confidence = _scroll_alignment_confidence(
                direction_status, anchor_count, scan_plan.confidence
            )
            controller.observe(
                actual_units=actual_units,
                displacement=anchor_displacement,
                direction=direction_status,
                alignment_confidence=scroll_confidence,
            )
            state["adaptive_scroll_controller"] = controller.to_dict()
            state["scroll_extra_notches"] = 0
            _append_benchmark_viewport_event(
                state,
                viewport_hash=after_digest,
                previous_viewport_hash=previous_viewport_hash,
                direction=direction_status,
                requested_units=actual_units,
                anchor_displacement=anchor_displacement,
                visible_rows=new_rows,
                row_spacing=spacing,
                row_signatures=current_row_signatures,
                overlap_indices=list(scan_plan.overlap_rows),
                candidate_indices=list(scan_plan.candidate_new_rows),
                recovery_type=state.get("recovery_reason"),
            )
            scroll_action = _record_scroll_progress(
                state,
                after_digest,
                direction=direction_status,
                list_changed=_list_view_changed(before_digest, after_digest),
            )
            if attempts:
                attempts[-1]["revisit_count"] = int(state.get("profile_revisits", 0))
                attempts[-1]["diagnosis"] = "FORWARD_VIEWPORT_VERIFIED"
                attempts[-1]["counted_as_forward_viewport"] = True
                attempts[-1]["forward_viewport_index"] = state.get("verified_downward_scrolls", 0)
                state["scroll_attempts"][-1].update(attempts[-1])
                _atomic_json(OUT / "scroll_diagnostics.json", {
                    "attempts": attempts,
                    "diagnosis": "FORWARD_VIEWPORT_VERIFIED",
                })
            if scroll_action == "FORWARD_PROGRESS_STALLED":
                state["state"] = scroll_action
                state["next_action"] = "RETRY_NAVIGATION_AFTER_OPERATOR_CHECK"
                _persist(state)
                _write_outputs(state)
                return 3
            if scroll_action == "FORWARD_VIEWPORT_UNSCANNED":
                state["state"] = scroll_action
                state["next_action"] = "RETRY_AFTER_VIEWPORT_SCAN"
                _persist(state)
                _write_outputs(state)
                return 3
            if overlap_count:
                state["current_y"] = new_rows[min(overlap_count - 1, len(new_rows) - 1)]
            else:
                # Without confident overlap, restart at the first visible row;
                # duplicate clicks are safer than silently skipping unseen rows.
                state["current_y"] = new_rows[0] - spacing
                state["viewport_cursor"]["next_candidate_row"] = new_rows[0]
            state["row_spacing"] = spacing
            state["viewports"] += 1
            state["viewport"] = state["viewports"]
            skip_profile_once = _mark_viewport_advance(state)
        _persist(state)
    state["state"] = (
        "TARGET_REACHED"
        if _confirmed_target_reached(state, target)
        else "BENCHMARK_PROFILE_LIMIT_REACHED"
    )
    state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _persist(state)
    _write_outputs(state)
    return 0


_RECOVERABLE_EXIT_STATES = {
    "FORWARD_VIEWPORT_UNSCANNED",
    "FORWARD_PROGRESS_STALLED",
    "NAVIGATION_UNRESOLVED",
    "NAVIGATION_BLOCKED_PROFILE_NOT_CHANGED",
    "SCROLL_DIRECTION_UNRESOLVED",
    "SCROLL_INPUT_FAILED",
    "SCROLL_OSCILLATION",
    "UNSAFE_INPUT_COORDINATE",
    "NAVIGATION_COORDINATE_OUT_OF_BOUNDS",
}


def _pause_for_recovery(reason: str, retry_count: int) -> None:
    checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    checkpoint["pause_reason"] = reason
    checkpoint["pause_retry_count"] = retry_count
    checkpoint["state"] = "PAUSED_RECOVERY_REQUIRED"
    checkpoint["pause_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LIVE_LOCK:
        _atomic_json(CHECKPOINT, checkpoint)
        _atomic_json(PROGRESS, checkpoint)
        _atomic_json(LIVE, _live_status(checkpoint, pid=os.getpid()))


def _record_process_error(exc: BaseException) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    category = "UNHANDLED_EXCEPTION"
    output_consistent = None
    if CHECKPOINT.exists():
        try:
            checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
            try:
                output_consistent = bool(_verify_resume_outputs().get("consistent"))
            except (OSError, ValueError, TypeError, csv.Error):
                output_consistent = False
            if output_consistent is False:
                category = "FATAL_CORRECTNESS_ERROR"
            checkpoint.update({
                "state": "PROCESS_ERROR" if category == "UNHANDLED_EXCEPTION" else category,
                "last_error_category": category,
                "last_error_type": type(exc).__name__,
                "last_error_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "output_consistency_pass": output_consistent,
            })
            with _LIVE_LOCK:
                _atomic_json(CHECKPOINT, checkpoint)
                _atomic_json(PROGRESS, checkpoint)
        except (OSError, ValueError, TypeError):
            category = "CHECKPOINT_IO_ERROR"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    crash = {
        "pid": os.getpid(), "state": "PROCESS_ERROR", "category": category,
        "exception_type": type(exc).__name__, "occurred_at": timestamp,
        "output_consistency_pass": output_consistent,
    }
    with _LIVE_LOCK:
        _atomic_json(OUT / "worker_crash.json", crash)
        prior = {}
        try:
            if LIVE.exists():
                prior = json.loads(LIVE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        prior.update({
            "pid": os.getpid(), "state": "PROCESS_ERROR", "heartbeat_at": timestamp,
            "last_error_category": category, "last_error_type": type(exc).__name__,
        })
        _atomic_json(LIVE, prior)
    print(
        f"WORKER_PROCESS_ERROR category={category} exception_type={type(exc).__name__}",
        file=sys.stderr, flush=True,
    )


def _record_user_stop() -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"pid": os.getpid(), "state": "USER_STOPPED", "heartbeat_at": timestamp}
    try:
        if CHECKPOINT.exists():
            checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
            checkpoint["state"] = "USER_STOPPED"
            checkpoint["stopped_at"] = timestamp
            with _LIVE_LOCK:
                _atomic_json(CHECKPOINT, checkpoint)
                _atomic_json(PROGRESS, checkpoint)
        with _LIVE_LOCK:
            _atomic_json(LIVE, payload)
    except (OSError, ValueError, TypeError):
        pass


def _run_with_recovery(args) -> int:
    resume = args.resume
    profile_limit = getattr(args, "profile_limit", None)
    attempts_for_checkpoint: dict[tuple[str, int, int], int] = {}
    while True:
        if profile_limit is None:
            code = run(args.target, resume=resume, ready_confirmed=args.ready_confirmed)
        else:
            code = run(
                args.target,
                resume=resume,
                ready_confirmed=args.ready_confirmed,
                profile_limit=profile_limit,
            )
        if code == 0:
            return 0
        if code != 3 or not CHECKPOINT.exists():
            return code
        checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        reason = str(checkpoint.get("state", "UNKNOWN"))
        if args.target < 150 or reason not in _RECOVERABLE_EXIT_STATES:
            return code
        confirmed = int(checkpoint.get("confirmed_count", len(checkpoint.get("confirmed", []))))
        unique = int(checkpoint.get("unique_profiles_visited", 0))
        key = (reason, confirmed, unique)
        attempts = attempts_for_checkpoint.get(key, 0) + 1
        attempts_for_checkpoint[key] = attempts
        if attempts > 3:
            return code
        _pause_for_recovery(reason, attempts)
        time.sleep(min(2 ** attempts, 8))
        resume = True


def main(argv=None) -> int:
    global OUT, CHECKPOINT, LIVE, PROGRESS
    global BENCHMARK_MODE, BENCHMARK_SEED_PROFILE_HASHES
    global BENCHMARK_SEED_CONFIRMED_ID_HASHES
    global BENCHMARK_SEED_SCROLL_CONTROLLER
    parser = argparse.ArgumentParser(description="Fixed-geometry one-off WeChat ID export")
    parser.add_argument("--target", type=int, default=150)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ready-confirmed", action="store_true")
    parser.add_argument("--benchmark-mode", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--profile-limit", type=int)
    parser.add_argument("--seed-checkpoint", type=Path)
    args = parser.parse_args(argv)
    if args.target < 1:
        parser.error("--target must be positive")
    if args.profile_limit is not None and args.profile_limit < 1:
        parser.error("--profile-limit must be positive")
    if args.benchmark_mode:
        project_root = _PROJECT_SRC.parent
        benchmark_root = (project_root / "output" / "benchmark_navigation_v2").resolve()
        if args.output_dir is None or args.profile_limit is None:
            parser.error("--benchmark-mode requires --output-dir and --profile-limit")
        output_dir = args.output_dir.resolve()
        if not output_dir.is_relative_to(benchmark_root) or output_dir == benchmark_root:
            parser.error("benchmark output must be a child of output/benchmark_navigation_v2")
        if output_dir.exists() and not args.resume:
            parser.error("fresh benchmark output directory already exists")
        if args.resume and not (output_dir / "fixed_geometry_checkpoint.json").is_file():
            parser.error("benchmark resume needs its own output directory checkpoint")
        if args.seed_checkpoint is not None and not args.seed_checkpoint.is_file():
            parser.error("--seed-checkpoint must refer to an existing checkpoint")
        BENCHMARK_MODE = True
        OUT = output_dir
        CHECKPOINT = OUT / "fixed_geometry_checkpoint.json"
        LIVE = OUT / "fixed_geometry_live_status.json"
        PROGRESS = OUT / f"{OUTPUT_STEM}.progress.json"
        BENCHMARK_SEED_PROFILE_HASHES = []
        BENCHMARK_SEED_CONFIRMED_ID_HASHES = []
        BENCHMARK_SEED_SCROLL_CONTROLLER = {}
        if args.seed_checkpoint is not None:
            seed = json.loads(args.seed_checkpoint.read_text(encoding="utf-8"))
            BENCHMARK_SEED_PROFILE_HASHES = [
                value for value in seed.get("seen_profile_hashes", [])
                if isinstance(value, str) and len(value) == 64
                and all(char in "0123456789abcdef" for char in value.casefold())
            ]
            id_hashes = seed.get("confirmed_frame_hashes", {})
            if isinstance(id_hashes, dict):
                BENCHMARK_SEED_CONFIRMED_ID_HASHES = [
                    key for key in id_hashes
                    if isinstance(key, str) and len(key) == 64
                    and all(char in "0123456789abcdef" for char in key.casefold())
                ]
            controller = seed.get("adaptive_scroll_controller", {})
            if isinstance(controller, dict):
                allowed = {
                    "current_scroll_units", "target_displacement", "observed_displacement",
                    "observed_scroll_units", "overlap_ratio", "direction", "confidence",
                    "visible_rows", "row_spacing", "max_scroll_units", "desired_overlap_rows",
                }
                BENCHMARK_SEED_SCROLL_CONTROLLER = {
                    key: value for key, value in controller.items() if key in allowed
                }
    elif args.output_dir is not None or args.profile_limit is not None or args.seed_checkpoint is not None:
        parser.error("--output-dir, --profile-limit, and --seed-checkpoint require --benchmark-mode")
    else:
        BENCHMARK_MODE = False
        BENCHMARK_SEED_PROFILE_HASHES = []
        BENCHMARK_SEED_CONFIRMED_ID_HASHES = []
        BENCHMARK_SEED_SCROLL_CONTROLLER = {}
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(stop_event,), name="wechat-worker-heartbeat", daemon=True,
    )
    heartbeat.start()
    try:
        return _run_with_recovery(args)
    except KeyboardInterrupt:
        stop_event.set()
        _record_user_stop()
        return 130
    except Exception as exc:  # noqa: BLE001 - persist fail-safe state for every runtime exception
        stop_event.set()
        _record_process_error(exc)
        raise RuntimeError(
            f"WORKER_PROCESS_ERROR:{type(exc).__name__}"
        ) from None
    finally:
        stop_event.set()
        heartbeat.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
