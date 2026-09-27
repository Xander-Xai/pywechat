"""Keyboard-sequential, bounded-retry WeChat-ID export.

Process the currently selected profile, finish its OCR/review state, save,
then send one released DOWN key and require a changed, stable header before
reading the next contact. A single visual click is reserved for recovery.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import _PROJECT_SRC  # makes the source checkout importable

if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

FORMAL_FIELDS = ["contact_index", "wechat_id", "status", "source", "confidence"]
REVIEW_FIELDS = [
    "row_ordinal",
    "status",
    "reason",
    "candidate_masked",
    "confidence",
    "source",
]
HARD_STOPS = {"WINDOW_NOT_FOUND", "WINDOW_NOT_READY", "WINDOW_CLOSED", "NOT_LOGIN"}
OPERATOR_STOPS = {"WINDOW_FOREGROUND_LOST", "OPERATOR_FOREGROUND_REQUIRED"} | HARD_STOPS

# Bounded startup, contact retry, and unique confirmed target.
MAX_NAV_RETRIES = 3
MAX_ROW_ATTEMPTS = 3

TARGET = 150


@dataclass(frozen=True)
class NavigationMove:
    moved: bool
    status: str
    profile_hash: str = ""
    attempts: int = 0


class KeyboardSequentialNavigator:
    """Move exactly one contact at a time, proving movement by profile change.

    The highlight detector is intentionally absent from the success contract.
    The final fallback can click the next row once, after two discrete DOWN
    attempts have failed.
    """

    def __init__(
        self,
        *,
        press_down: Callable[[], None],
        wait_profile_change: Callable[[str, float], str | None],
        operator_recovery: Callable[[], bool],
        first_probe: bool = False,
    ) -> None:
        self.press_down = press_down
        self.wait_profile_change = wait_profile_change
        self.operator_recovery = operator_recovery
        self.first_probe = first_probe

    def move_next(self, old_hash: str) -> NavigationMove:
        try:
            self.press_down()
            new_hash = self.wait_profile_change(old_hash, 2.0)
        except Exception as exc:
            if getattr(exc, "status", "") in OPERATOR_STOPS | {"OPERATOR_ACTION_REQUIRED"}:
                raise
            new_hash = None
        if new_hash and new_hash != old_hash:
            self.first_probe = False
            return NavigationMove(True, "MOVE_SUCCESS", new_hash, 1)
        return NavigationMove(False, "NAVIGATION_STALLED", old_hash, 1)


def process_current_contact(
    extract: Callable[..., object], *, frame=None,
    navigation_profile_hash: str = "", extraction_frame_hash: str = "",
    max_attempts: int = 3, threshold: float = 0.99
) -> tuple[str, object]:
    """Process the selected contact using its explicitly owned frame, if supplied."""
    if frame is not None and not frame_hashes_match(
        navigation_profile_hash, extraction_frame_hash
    ):
        return "FRAME_MISMATCH", None
    last_result = None
    for _ in range(max_attempts):
        last_result = extract(frame) if frame is not None else extract()
        # Review and explicit OCR outcomes are terminal for this contact. A
        # low-confidence candidate is a data-quality decision, not a hang.
        if getattr(last_result, "status", "") in {
            "NEED_REVIEW", "OCR_LABEL_NOT_FOUND", "OCR_LOW_CONFIDENCE",
            "OCR_FAILED", "SOURCE_VALUE_MASKED", "CONFIRMED",
        }:
            if getattr(last_result, "status", "") == "SOURCE_VALUE_MASKED":
                return "SOURCE_VALUE_MASKED", last_result
            if (
                getattr(last_result, "status", "") == "CONFIRMED"
                and getattr(last_result, "value", None)
                and (getattr(last_result, "confidence", 0.0) or 0.0) >= threshold
            ):
                return "CONFIRMED", last_result
            return "FINAL_REVIEW", last_result
        if (
            getattr(last_result, "status", "") == "CONFIRMED"
            and getattr(last_result, "value", None)
            and (getattr(last_result, "confidence", 0.0) or 0.0) >= threshold
        ):
            return "CONFIRMED", last_result
    return "FINAL_REVIEW", last_result


def frame_hashes_match(navigation_profile_hash: str, extraction_frame_hash: str) -> bool:
    """Fail closed unless navigation and OCR refer to the same profile frame."""
    return bool(navigation_profile_hash and extraction_frame_hash
                and navigation_profile_hash == extraction_frame_hash)


def stage_stalled(*, last_heartbeat: float, now: float, timeout: float = 30.0) -> bool:
    """A stage is stalled only when its own progress heartbeat goes stale."""
    return now - last_heartbeat > timeout


def classify_duplicate_id(
    wechat_id: str, confirmed_ids: set[str], old_profile_hash: str, new_profile_hash: str
) -> str:
    if wechat_id not in confirmed_ids:
        return "NEW_ID"
    if not new_profile_hash or new_profile_hash == old_profile_hash:
        return "SUSPECT_NAVIGATION_DUPLICATE"
    return "DUPLICATE_WECHAT_ID_ANOMALY"


def goal_reached(state: dict, target: int = 150) -> bool:
    return int(state.get("confirmed_unique", 0)) >= target


def profile_read_ready(*, hwnd_exists: bool, roi_valid: bool, visual_valid: bool, stable: bool, ocr_boxes: int) -> bool:
    """Read readiness is independent of whichever application owns foreground."""
    return bool(hwnd_exists and roi_valid and visual_valid and stable and ocr_boxes > 0)


def send_down_if_foreground(*, is_ready: Callable[[], bool], wait_ready: Callable[[], bool], send_down: Callable[[], None]) -> bool:
    """Fail closed: never emit Down until foreground readiness is true."""
    if not is_ready() and not wait_ready():
        return False
    # wait_ready is the foreground check; avoid a second check that can undo
    # the successful wake-up and prevent the one permitted key press.
    send_down()
    return True


def wait_for_foreground(
    *, is_ready: Callable[[], bool], heartbeat: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep, interval: float = 1.0,
) -> bool:
    """Pause indefinitely for the operator while refreshing live status."""
    while not is_ready():
        heartbeat()
        sleep(interval)
    return True


def _atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _write_outputs(out: Path, confirmed: list[dict], review: list[dict]) -> int:
    """Write unique-ordered CSV, TXT, and per-row review rows.

    Unique confirmed WeChat IDs are guaranteed upstream; the writer still
    enforces it with a set and reports how many duplicates were suppressed
    (a hard audit signal that should stay zero).
    """
    from wechat_ocr.masking import contains_mask_placeholder
    from wechat_ocr.validator import validate_wechat_id

    out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    unique_rows: list[dict] = []
    dropped = 0
    for row in confirmed:
        value = str(row.get("wechat_id", ""))
        if contains_mask_placeholder(value) or not validate_wechat_id(value).valid:
            raise RuntimeError("CONFIRMED_OUTPUT_CONTAINS_INVALID_OR_MASKED_ID")
        if row["wechat_id"] in seen:
            dropped += 1
            continue
        seen.add(row["wechat_id"])
        unique_rows.append(dict(row))
    with (out / "wechat_contacts_150.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as fp:
        writer = csv.DictWriter(fp, fieldnames=FORMAL_FIELDS)
        writer.writeheader()
        writer.writerows(unique_rows)
    with (out / "wechat_contacts_150.txt").open(
        "w", encoding="utf-8-sig", newline=""
    ) as fp:
        fp.write("\n".join(row["wechat_id"] for row in unique_rows))
        if unique_rows:
            fp.write("\n")
    with (out / "wechat_contacts_150_review.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as fp:
        writer = csv.DictWriter(fp, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        safe_review = []
        for row in review:
            safe = dict(row)
            candidate = str(safe.get("candidate_masked", "") or "")
            if candidate and not contains_mask_placeholder(candidate):
                safe["candidate_masked"] = _mask(candidate)
            safe_review.append({key: safe.get(key, "") for key in REVIEW_FIELDS})
        writer.writerows(safe_review)
    return dropped


def _mask(value: str) -> str:
    from wechat_ocr.masking import mask_wechat_id

    return mask_wechat_id(value)


def _id_hash(wechat_id: str) -> str:
    return hashlib.sha256(wechat_id.encode("utf-8")).hexdigest()


def _checkpoint_snapshot(state: dict) -> dict:
    """Keep raw confirmed IDs in CSV/TXT outputs, not progress diagnostics."""
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
    return snapshot


def _load_confirmed_export(output_dir: Path, expected_count: int, expected_hashes: list[str]) -> list[dict]:
    from wechat_ocr.masking import contains_mask_placeholder
    from wechat_ocr.validator import validate_wechat_id

    if expected_count == 0:
        return []
    path = output_dir / "wechat_contacts_150.csv"
    if not path.exists():
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_NOT_FOUND")
    confirmed = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            value = row.get("wechat_id", "")
            if contains_mask_placeholder(value) or not validate_wechat_id(value).valid:
                raise RuntimeError("RESUME_CONFIRMED_EXPORT_INVALID_OR_MASKED")
            confirmed.append({**row, "contact_index": int(row["contact_index"])})
    if len(confirmed) != expected_count:
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_COUNT_MISMATCH")
    hashes = [_id_hash(row["wechat_id"]) for row in confirmed]
    if expected_hashes and hashes != expected_hashes:
        raise RuntimeError("RESUME_CONFIRMED_EXPORT_HASH_MISMATCH")
    return confirmed


def _fresh_state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "navigation_mode": "keyboard_sequential",
        "sequence_index": 0,
        "contacts_visited": 0,
        "confirmed": [],
        "confirmed_id_hashes": [],
        "confirmed_unique": 0,
        "review": [],
        "review_count": 0,
        "duplicate_results": 0,
        "duplicate_writes": 0,
        "same_profile_repeats": 0,
        "navigation_stalls": 0,
        "navigation_stalls_total": 0,
        "consecutive_unrecovered_navigation_failures": 0,
        "navigation_recoveries": 0,
        "navigation_clicks": 0,
        "last_navigation_diagnostics": {},
        "frame_diagnostics": [],
        "last_navigation_profile_hash_prefix": "",
        "last_extraction_frame_hash_prefix": "",
        "last_frame_hash_match": False,
        "ocr_failures": 0,
        "failed_rows": 0,
        "last_confirmed_id_hash": "",
        "last_confirmed_profile_hash": "",
        "last_profile_hash": "",
        "last_processed_profile_hash": "",
        "navigation_state": "INITIAL_SELECTED_CONTACT",
        "pending_advance": False,
        "preflight_10": {},
        "preflight_20": {},
        "batch_tests": {"collected": 0, "passed": 0, "failed": 0},
        "bootstrap": {"profile_ready": False, "keyboard_focus_probe": "PENDING", "operator_interventions": 0},
        "profile_read_ready": False,
        "input_foreground_ready": False,
        "foreground_pauses": 0,
        "first_contact_ocr_status": "PENDING",
        "first_down": {"foreground_ready": False, "profile_changed": False},
        "profile_changes": 0,
        "navigation_failures": 0,
        "first_5": {},
        "stop_reason": "",
        "finished": False,
    }


def _restore_checkpoint_state(fresh: dict, checkpoint: dict) -> dict:
    """Restore the saved cursor position and pending transition verbatim."""
    fresh.update(checkpoint)
    return fresh


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _wait_for_wechat_foreground(hwnd: int) -> bool:
    from pyweixin.visual_navigation import is_weixin_foreground

    return is_weixin_foreground(hwnd)


def _profile_header_hash(frame) -> str:
    from pyweixin.visual_navigation import profile_hash

    return profile_hash(frame)


def _header_hash(nav) -> str:
    return _profile_header_hash(nav._screenshot(nav.hwnd))


def _stable_initial_header(nav, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    previous = ""
    while time.monotonic() < deadline:
        current = _header_hash(nav)
        if current and current == previous:
            return current
        previous = current
        time.sleep(0.2)
    return ""


def _press_down_once(list_control=None) -> None:
    """Send one discrete Down press and release; never hold the key."""
    if list_control is not None and hasattr(list_control, "type_keys"):
        list_control.type_keys("{DOWN}")
        return
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_DOWN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_DOWN, 0, win32con.KEYEVENTF_KEYUP, 0)


def _wait_header_change(nav, old_hash: str, timeout: float | None = 2.0) -> str | None:
    deadline = None if timeout is None else time.monotonic() + min(timeout, 2.0)
    candidate = ""
    stable_frames = 0
    while deadline is None or time.monotonic() < deadline:
        current = _header_hash(nav)
        if current and current != old_hash:
            if current == candidate:
                stable_frames += 1
            else:
                candidate = current
                stable_frames = 1
            if stable_frames >= 2:
                return current
        else:
            candidate = ""
            stable_frames = 0
        time.sleep(0.15)
    return None


def _make_keyboard_navigator(session, operator_recovery, *, first_probe=False) -> KeyboardSequentialNavigator:
    from pyweixin.visual_navigation import NavigationFailure

    nav = session.navigator

    def press_down() -> None:
        if not send_down_if_foreground(
            is_ready=lambda: _wait_for_wechat_foreground(nav.hwnd),
            wait_ready=operator_recovery,
            send_down=_press_down_once,
        ):
            raise NavigationFailure("WINDOW_FOREGROUND_LOST")

    return KeyboardSequentialNavigator(
        press_down=press_down,
        # After a released Down is sent, keep the worker alive until the
        # selected profile visibly changes. The batch heartbeat runs in its
        # own thread while this wait is open.
        wait_profile_change=lambda old, timeout: _wait_header_change(nav, old, timeout=None),
        operator_recovery=operator_recovery,
        first_probe=first_probe,
    )


def _contact_list_geometry(nav):
    """Find the selected contact row and its next visible row from pixels only."""
    import numpy as np
    import win32api
    import win32gui
    from PIL import ImageGrab

    left, top, right, bottom = win32gui.GetWindowRect(nav.hwnd)
    origin_x = win32api.GetSystemMetrics(76)
    origin_y = win32api.GetSystemMetrics(77)
    frame = np.asarray(ImageGrab.grab(all_screens=True).convert("RGB"))
    wx0, wx1 = left - origin_x, right - origin_x
    wy0, wy1 = top - origin_y, bottom - origin_y
    crop = frame[max(0, wy0 + 180):min(frame.shape[0], wy1), max(0, wx0):min(frame.shape[1], wx1)]
    if crop.size == 0:
        return None

    pixels = crop.astype(np.int16)
    green = (
        (pixels[:, :, 1] > 95)
        & (pixels[:, :, 1] > pixels[:, :, 0] * 1.30)
        & (pixels[:, :, 1] > pixels[:, :, 2] * 1.12)
    )
    row_counts = green.sum(axis=1)
    active = row_counts >= max(80, int(crop.shape[1] * 0.045))
    bands = []
    start = None
    for y, is_active in enumerate(active):
        if is_active and start is None:
            start = y
        elif not is_active and start is not None:
            if y - start >= 12:
                bands.append((start, y - 1))
            start = None
    if start is not None and len(active) - start >= 12:
        bands.append((start, len(active) - 1))
    if not bands:
        return None

    selected_top, selected_bottom = max(
        bands, key=lambda band: int(row_counts[band[0]:band[1] + 1].sum())
    )
    band_mask = green[selected_top:selected_bottom + 1]
    column_counts = band_mask.sum(axis=0)
    columns = np.flatnonzero(column_counts >= max(6, int(band_mask.shape[0] * 0.40)))
    if columns.size < 100:
        return None
    list_left = left - origin_x + int(columns[0])
    list_right = left - origin_x + int(columns[-1])
    list_width = list_right - list_left + 1
    selected_center = top - origin_y + 180 + (selected_top + selected_bottom) / 2
    row_pitch = selected_bottom - selected_top + 1
    next_center = selected_center + row_pitch
    viewport_bottom = bottom - origin_y - 8
    viewport_top = top - origin_y + 180
    if next_center + row_pitch / 2 > viewport_bottom:
        return {
            "list_left": list_left, "list_right": list_right, "list_width": list_width,
            "selected_center_y": selected_center, "row_pitch": row_pitch,
            "next_center_y": None, "viewport_bottom": viewport_bottom,
            "viewport_top": viewport_top, "current_row_near_viewport_bottom": True,
            "virtual_origin_x": origin_x, "virtual_origin_y": origin_y,
        }

    avatar_left = list_left + int(list_width * 0.14)
    avatar_right = min(list_right, avatar_left + 35)
    half = max(12, int(row_pitch * 0.31))
    py0 = max(0, int(next_center - half - origin_y))
    py1 = min(frame.shape[0], int(next_center + half - origin_y))
    patch = frame[py0:py1, avatar_left - origin_x:avatar_right - origin_x + 1]
    if patch.size == 0 or float(patch.std(axis=(0, 1)).mean()) < 12:
        return None
    return {
        "list_left": list_left, "list_right": list_right, "list_width": list_width,
        "selected_center_y": selected_center, "row_pitch": row_pitch,
        "next_center_y": next_center, "viewport_bottom": viewport_bottom,
        "viewport_top": viewport_top, "current_row_near_viewport_bottom": False,
        "virtual_origin_x": origin_x, "virtual_origin_y": origin_y,
    }


def _click_screen_once(x: int, y: int) -> None:
    import win32api
    import win32con

    win32api.SetCursorPos((int(x), int(y)))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


class ContactRowClickNavigator:
    """Advance by visible row geometry and verify only a stable profile change."""

    def __init__(
        self,
        nav,
        *,
        on_status: Callable[..., None] | None = None,
        is_foreground: Callable[[], bool] | None = None,
        pause_foreground: Callable[[], bool] | None = None,
        geometry_provider: Callable | None = None,
        click: Callable[[int, int], None] | None = None,
        wait_profile_change: Callable[[str, float], str | None] | None = None,
        scroll: Callable[[dict], bool] | None = None,
        on_click: Callable[[], None] | None = None,
        on_cycle_failure: Callable[[], None] | None = None,
        consecutive_failures: int = 0,
        max_recovery_cycles: int = 3,
    ):
        self.nav = nav
        self.on_status = on_status or (lambda status: None)
        self.is_foreground = is_foreground or (lambda: _wait_for_wechat_foreground(nav.hwnd))
        self.pause_foreground = pause_foreground or (lambda: False)
        self.geometry_provider = geometry_provider or _contact_list_geometry
        self.click = click or _click_screen_once
        self.wait_profile_change = wait_profile_change or self._wait_for_profile
        self.scroll = scroll or self._scroll_and_stabilize
        self.on_click = on_click or (lambda: None)
        self.on_cycle_failure = on_cycle_failure or (lambda: None)
        self.consecutive_failures = consecutive_failures
        self.max_recovery_cycles = max_recovery_cycles
        self.diagnostics = {
            "current_row_found": False,
            "next_row_found": False,
            "current_row_near_viewport_bottom": False,
            "scroll_attempted": False,
            "profile_changed_after_click": False,
            "foreground_ready": False,
            "stall_stage": "",
        }

    def _status(self, name: str, stage: str | None = None, **details) -> None:
        if stage is not None:
            self.diagnostics["stall_stage"] = stage
        self.diagnostics.update(details)
        try:
            self.on_status(name, dict(self.diagnostics))
        except TypeError:
            self.on_status(name)

    def _ensure_foreground(self) -> bool:
        if self.is_foreground():
            self.diagnostics["foreground_ready"] = True
            return True
        self.diagnostics["foreground_ready"] = False
        self._status("PAUSED_FOREGROUND_LOST", "FOREGROUND_LOST")
        if not self.pause_foreground():
            return False
        self.diagnostics["foreground_ready"] = self.is_foreground()
        return self.diagnostics["foreground_ready"]

    def _list_digest(self, geometry):
        import numpy as np
        import win32api
        import win32gui
        from PIL import ImageGrab

        _, top, _, bottom = win32gui.GetWindowRect(self.nav.hwnd)
        ox, oy = geometry["virtual_origin_x"], geometry["virtual_origin_y"]
        x0 = max(0, geometry["list_left"] - ox)
        x1 = min(win32api.GetSystemMetrics(78), geometry["list_right"] - ox + 1)
        y0 = max(0, top - oy + 180)
        y1 = min(win32api.GetSystemMetrics(79), bottom - oy - 8)
        frame = np.asarray(ImageGrab.grab(all_screens=True).convert("RGB"))
        return hashlib.sha256(frame[y0:y1, x0:x1:3].tobytes()).digest()

    def _scroll_and_stabilize(self, geometry, timeout: float = 3.0) -> bool:
        import win32api
        import win32con

        if not self._ensure_foreground():
            return False
        before = self._list_digest(geometry)
        x = round(geometry["list_left"] + geometry["list_width"] * 0.50)
        y = round((geometry["selected_center_y"] + geometry["viewport_bottom"]) / 2)
        win32api.SetCursorPos((x + geometry["virtual_origin_x"], y + geometry["virtual_origin_y"]))
        # Three wheel notches move about 60-70% of the visible contact rows,
        # leaving overlap so the selected row remains a usable anchor.
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -3 * 120, 0)
        deadline = time.monotonic() + timeout
        changed = False
        previous = None
        stable_frames = 0
        while time.monotonic() < deadline:
            if not self._ensure_foreground():
                continue
            digest = self._list_digest(geometry)
            if digest != before:
                changed = True
                if digest == previous:
                    stable_frames += 1
                    if stable_frames >= 2:
                        return True
                else:
                    previous, stable_frames = digest, 0
            time.sleep(0.15)
        return changed and stable_frames >= 2

    def _wait_for_profile(self, old_hash: str, timeout: float = 3.0) -> str | None:
        active_elapsed = 0.0
        candidate = ""
        stable_frames = 0
        while active_elapsed < timeout:
            if not self._ensure_foreground():
                continue
            started = time.monotonic()
            # Navigation verifies a fresh frame. Keep that exact stable frame
            # for extraction too: extract_selected_profile prefers
            # nav.last_stable_frame over taking a new screenshot.
            frame = self.nav._screenshot(self.nav.hwnd)
            current = _profile_header_hash(frame)
            if current and current != old_hash:
                if current == candidate:
                    stable_frames += 1
                else:
                    candidate, stable_frames = current, 1
                if stable_frames >= 2:
                    self.nav.last_stable_frame = frame
                    self.nav.current_profile_frame = frame
                    self.nav.current_profile_frame_hash = current
                    return current
            else:
                candidate, stable_frames = "", 0
            time.sleep(0.15)
            active_elapsed += max(0.15, time.monotonic() - started)
        return None

    def _geometry(self, stage: str):
        geometry = self.geometry_provider(self.nav)
        self.diagnostics["current_row_found"] = bool(geometry)
        self.diagnostics["next_row_found"] = bool(geometry and geometry.get("next_center_y") is not None)
        self.diagnostics["current_row_near_viewport_bottom"] = bool(
            geometry and geometry.get("current_row_near_viewport_bottom", geometry.get("next_center_y") is None)
        )
        if not geometry:
            self._status("ROW_REANCHOR", stage, current_row_found=False, next_row_found=False)
        else:
            self._status(
                "ROW_GEOMETRY_FOUND", stage,
                current_row_found=True,
                next_row_found=self.diagnostics["next_row_found"],
                current_row_near_viewport_bottom=self.diagnostics["current_row_near_viewport_bottom"],
            )
        return geometry

    def _click_and_verify(self, geometry, old_hash: str, stage: str, click_count: list[int]):
        if not geometry or geometry.get("next_center_y") is None:
            self._status("NEXT_ROW_NOT_FOUND", stage, next_row_found=False)
            return None
        if not self._ensure_foreground():
            return None
        x = geometry["list_left"] + round(0.65 * geometry["list_width"])
        y = round(geometry["next_center_y"])
        self.click(x + geometry["virtual_origin_x"], y + geometry["virtual_origin_y"])
        click_count[0] += 1
        self.on_click()
        self.diagnostics["profile_changed_after_click"] = False
        self._status("CONTACT_ROW_CLICK_SENT", stage, click_sent=True, click_x=x, click_y=y)
        new_hash = self.wait_profile_change(old_hash, 3.0)
        if new_hash and new_hash != old_hash:
            self.diagnostics["profile_changed_after_click"] = True
            self._status("PROFILE_CHANGED_STABLE", stage, profile_changed_after_click=True)
            return new_hash
        self._status("CLICK_SENT_PROFILE_UNCHANGED", stage, profile_changed_after_click=False)
        return None

    def _advance_viewport(self, geometry, stage: str):
        self.diagnostics["scroll_attempted"] = True
        self._status("VIEWPORT_ADVANCE", stage, scroll_attempted=True)
        if not self.scroll(geometry):
            self._status("SCROLL_FAILED", "SCROLL_FAILED", scroll_attempted=True)
            return None
        geometry = self._geometry("VIEWPORT_REANCHOR")
        if geometry and geometry.get("next_center_y") is not None:
            return geometry
        self._status("NEXT_ROW_NOT_FOUND", "NEXT_ROW_NOT_FOUND", scroll_attempted=True)
        return None

    def move_next(self, old_hash: str) -> NavigationMove:
        click_count = [0]
        recovered = False
        for cycle in range(1, self.max_recovery_cycles + 1):
            self._status("MOUSE_NAVIGATION", f"CYCLE_{cycle}_NORMAL")
            geometry = self._geometry("CURRENT_ROW_SCAN")
            if geometry and geometry.get("next_center_y") is None:
                geometry = self._advance_viewport(geometry, "NORMAL_VIEWPORT_ADVANCE")
                recovered = True
            new_hash = self._click_and_verify(geometry, old_hash, "NORMAL_CLICK", click_count)
            if new_hash:
                self.consecutive_failures = 0
                return NavigationMove(True, "NAVIGATION_RECOVERED" if recovered else "PROFILE_CHANGED_STABLE", new_hash, click_count[0])

            # Recovery 1: re-scan and click the newly located next row once.
            self._status("NAVIGATION_RECOVERY_1", f"CYCLE_{cycle}_RECOVERY_1")
            geometry = self._geometry("RECOVERY_1_RESCAN")
            if geometry and geometry.get("next_center_y") is None:
                geometry = self._advance_viewport(geometry, "RECOVERY_1_VIEWPORT_ADVANCE")
            new_hash = self._click_and_verify(geometry, old_hash, "RECOVERY_1_CLICK", click_count)
            if new_hash:
                self.consecutive_failures = 0
                return NavigationMove(True, "NAVIGATION_RECOVERED", new_hash, click_count[0])

            # Recovery 2: small list-only scroll, keep the selected row anchored, click again.
            self._status("NAVIGATION_RECOVERY_2", f"CYCLE_{cycle}_RECOVERY_2")
            geometry = self._geometry("RECOVERY_2_RESCAN")
            if geometry:
                self.diagnostics["scroll_attempted"] = True
                if self.scroll(geometry):
                    geometry = self._geometry("RECOVERY_2_REANCHOR")
                else:
                    geometry = None
            new_hash = self._click_and_verify(geometry, old_hash, "RECOVERY_2_CLICK", click_count)
            if new_hash:
                self.consecutive_failures = 0
                return NavigationMove(True, "NAVIGATION_RECOVERED", new_hash, click_count[0])

            # Recovery 3: full fresh ROI/row re-anchor, with one viewport advance if needed.
            self._status("NAVIGATION_RECOVERY_3", f"CYCLE_{cycle}_RECOVERY_3")
            geometry = self._geometry("RECOVERY_3_FULL_REANCHOR")
            if geometry and geometry.get("next_center_y") is None:
                geometry = self._advance_viewport(geometry, "RECOVERY_3_VIEWPORT_ADVANCE")
            new_hash = self._click_and_verify(geometry, old_hash, "RECOVERY_3_CLICK", click_count)
            if new_hash:
                self.consecutive_failures = 0
                return NavigationMove(True, "NAVIGATION_RECOVERED", new_hash, click_count[0])

            self._status(
                "NAVIGATION_RECOVERY_CYCLE_FAILED", f"CYCLE_{cycle}_FAILED",
                recovery_cycle=cycle,
            )

        # All recovery cycles belong to one next-contact transition. Count that
        # transition once; cumulative historical stalls never affect this value.
        self.consecutive_failures += 1
        self.on_cycle_failure()
        stalled = self.consecutive_failures >= 3
        status = "NAVIGATION_STALLED" if stalled else "NAVIGATION_RECOVERY_PENDING"
        self._status(
            status,
            "THREE_RECOVERY_CYCLES_FAILED",
            consecutive_unrecovered_navigation_failures=self.consecutive_failures,
        )
        return NavigationMove(False, status, old_hash, click_count[0])


def _manual_bootstrap_session(*, prompt=True):
    """Establish read readiness without requiring or changing foreground."""
    import win32gui

    from pyweixin.visual_navigation import (
        PROFILE_HEADER_REGION,
        PageEvidence,
        VisualContactSession,
        Win32VisualNavigation,
        _gray_region_score,
        profile_hash,
    )

    if prompt:
        print("OPERATOR_ACTION_REQUIRED", flush=True)
        print("请手动完成以下操作：", flush=True)
        print("1. 打开微信主窗口；", flush=True)
        print("2. 点击左侧“通讯录/联系人”；", flush=True)
        print("3. 展开“联系人”分组；", flush=True)
        print("4. 点击任意一个普通联系人，使该联系人呈绿色选中状态；", flush=True)
        print("5. 已选联系人资料就绪后程序会先读取；发送 Down 前会单独等待微信前台。", flush=True)
    hwnd = 0
    while not hwnd:
        hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
        if not hwnd:
            hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "Weixin")
        if not hwnd:
            time.sleep(0.5)
    nav = Win32VisualNavigation(hwnd)
    previous_hash = ""
    while True:
        frame = nav._screenshot(hwnd)
        width, height = frame.size
        x1, y1, x2, y2 = PROFILE_HEADER_REGION
        box = (round(x1 * width), round(y1 * height), round(x2 * width), round(y2 * height))
        roi_valid = 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
        if not roi_valid:
            time.sleep(0.35)
            continue
        header = frame.crop(box)
        tokens = nav._tokens(header)
        box_count = sum(bool(str(getattr(token, "text", "")).strip()) for token in tokens)
        visual_valid = _gray_region_score(header, (0, 0, *header.size)) >= 0.16
        current_hash = profile_hash(frame)
        profile_stable = bool(current_hash and current_hash == previous_hash)
        previous_hash = current_hash
        if profile_read_ready(
            hwnd_exists=True,
            roi_valid=roi_valid,
            visual_valid=visual_valid,
            stable=profile_stable,
            ocr_boxes=box_count,
        ):
            nav.last_stable_frame = frame
            nav.current_profile_frame = frame
            nav.current_profile_frame_hash = current_hash
            session = VisualContactSession(
                nav, PageEvidence(True, "profile_read_ready", box_count)
            )
            session.profile_read_ready = True
            session.profile_header_roi_valid = roi_valid
            session.profile_header_visual_valid = visual_valid
            session.profile_header_box_count = box_count
            session.profile_header_hash = current_hash
            print("PROFILE_READ_READY", flush=True)
            return session
        time.sleep(0.35)


def _keyboard_report(
    state: dict,
    csv_ids: list[str],
    txt_ids: list[str],
    dropped: int,
) -> dict:
    csv_rows = len(csv_ids)
    txt_rows = len(txt_ids)
    csv_unique = len(set(csv_ids))
    txt_unique = len(set(txt_ids))
    unique_ids = csv_unique
    txt_matches = (
        csv_ids == txt_ids
        and csv_rows == txt_rows == csv_unique == txt_unique
        and dropped == 0
    )
    final_status = "PASS" if unique_ids == TARGET and csv_rows == TARGET and txt_rows == TARGET and txt_matches else state.get("stop_reason") or "RUNNING"
    return {
        "report": "WECHAT_FOREGROUND_DECOUPLED_EXPORT_REPORT",
        "PROFILE_READ_READY": state.get("profile_read_ready", False),
        "INPUT_FOREGROUND_READY": state.get("input_foreground_ready", False),
        "FIRST_CONTACT": {"visited": 1 if state.get("contacts_visited", 0) else 0, "ocr_status": state.get("first_contact_ocr_status", "PENDING")},
        "FIRST_DOWN": state.get("first_down", {"foreground_ready": False, "profile_changed": False}),
        "BATCH": {
            "visited": state.get("contacts_visited", 0),
            "confirmed_unique": unique_ids,
            "review": state.get("review_count", 0),
            "foreground_pauses": state.get("foreground_pauses", 0),
            "navigation_stalls": state.get("navigation_stalls", 0),
        },
        "CSV_ROWS": csv_rows,
        "TXT_ROWS": txt_rows,
        "CSV_UNIQUE": csv_unique,
        "TXT_UNIQUE": txt_unique,
        "CSV_TXT_MATCH": "PASS" if txt_matches else "FAIL",
        "FINAL": final_status,
        "OUTPUT": {"CSV": "output/final/wechat_contacts_150.csv", "TXT": "output/final/wechat_contacts_150.txt", "REVIEW": "output/final/wechat_contacts_150_review.csv", "CHECKPOINT": "output/final/wechat_contacts_150.progress.json"},
    }


def probe_current_contact() -> dict:
    """Read only the currently visible profile and return PII-masked evidence."""
    session = _manual_bootstrap_session(prompt=False)
    nav = session.navigator
    started = time.monotonic()
    result, _, _ = nav.extract_selected_profile(
        page_verified=session.page_evidence.confirmed,
        uia_candidate_provider=None,
        open_profile_popup=False,
        image=getattr(nav, "current_profile_frame", None),
    )
    candidates = [value for value in (getattr(result, "ocr_candidates", None) or []) if value]
    value = getattr(result, "value", None) or (candidates[0] if candidates else "")
    from wechat_ocr.extractor import label_diagnostics

    diagnostics = label_diagnostics(getattr(nav, "last_profile_tokens", ()))
    report = {
        "profile_read_ready": bool(session.profile_read_ready),
        "ocr_box_count": int(getattr(session, "profile_header_box_count", 0)),
        "wechat_label_found": bool(diagnostics.get("has_wechat_label")),
        "candidate_found": bool(value),
        "candidate_masked": _mask(str(value)) if value else "",
        "confidence": getattr(result, "confidence", None),
        "status": getattr(result, "status", "OCR_FAILED"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    for key, label in (
        ("profile_read_ready", "PROFILE_READ_READY"),
        ("ocr_box_count", "OCR_BOX_COUNT"),
        ("wechat_label_found", "WECHAT_LABEL_FOUND"),
        ("candidate_found", "CANDIDATE_FOUND"),
        ("candidate_masked", "CANDIDATE_MASKED"),
        ("confidence", "CONFIDENCE"),
        ("status", "STATUS"),
    ):
        print(f"{label}: {report[key]}", flush=True)
    return report


def run_keyboard_batch(
    *,
    output_dir: Path,
    resume: bool = False,
    limit: int = TARGET,
    preflight_contacts: int = 0,
    manual_bootstrap: bool = False,
    arm_delay: int = 0,
) -> int:
    """Export from the currently selected contact using one DOWN per move."""
    from pyweixin.visual_navigation import NavigationFailure, is_weixin_foreground

    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "wechat_contacts_150.progress.json"
    state = _fresh_state(time.strftime("%Y%m%d-%H%M%S"))
    state["navigation_mode"] = "manual_bootstrap_mouse_sequential"
    if resume and checkpoint_path.exists():
        loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if loaded.get("navigation_mode") not in {
            "keyboard_sequential",
            "manual_bootstrap_keyboard_sequential",
            "manual_bootstrap_mouse_sequential",
        }:
            print("CHECKPOINT_SCHEMA_MISMATCH", flush=True)
            return 2
        state.update(loaded)
        expected_confirmed = int(
            loaded.get("confirmed_count", loaded.get("confirmed_unique", len(loaded.get("confirmed", []))))
        )
        state["confirmed"] = _load_confirmed_export(
            output_dir,
            expected_confirmed,
            list(loaded.get("confirmed_id_hashes", [])),
        )
    state.setdefault("bootstrap", {"profile_ready": False, "keyboard_focus_probe": "PENDING", "operator_interventions": 0})
    state.setdefault("profile_changes", 0)
    state.setdefault("navigation_failures", 0)
    state["navigation_stalls_total"] = max(
        state.get("navigation_stalls_total", 0), state.get("navigation_stalls", 0)
    )
    state.setdefault("consecutive_unrecovered_navigation_failures", 0)
    state.setdefault("navigation_recoveries", 0)
    state.setdefault("navigation_clicks", 0)
    state.setdefault("last_navigation_diagnostics", {})
    state.setdefault("first_5", {})
    state.setdefault("profile_read_ready", False)
    state.setdefault("input_foreground_ready", False)
    state.setdefault("foreground_pauses", 0)
    state.setdefault("first_contact_ocr_status", "PENDING")
    state.setdefault("first_down", {"foreground_ready": False, "profile_changed": False})
    state.setdefault(
        "mouse_navigation_baseline",
        {"navigation_failures": state.get("navigation_failures", 0)},
    )
    if resume and state.get("contacts_visited", 0) >= 5:
        gate = state.get("first_5", {})
        mouse_failures = state.get("navigation_failures", 0) - state["mouse_navigation_baseline"].get("navigation_failures", 0)
        if (
            gate.get("status") == "BLOCKED"
            and state.get("profile_changes", 0) >= 4
            and mouse_failures == 0
            and len(state.get("confirmed", [])) + len(state.get("review", [])) >= 5
        ):
            state["first_5"] = {
                **gate,
                "profile_changes": state.get("profile_changes", 0),
                "navigation_failures": 0,
                "status": "PASS",
                "legacy_keyboard_navigation_failures": state.get("navigation_failures", 0),
            }

    def write_live_status() -> None:
        complete = state.get("finished", False)
        _atomic_json(output_dir / "live_status.json", {
            "state": "COMPLETE" if complete else state.get("current_status", "RUNNING"),
            "pid": os.getpid(),
            "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "visited": state.get("contacts_visited", 0),
            "sequence_index": state.get("sequence_index", 0),
            "confirmed": state.get("confirmed_unique", 0),
            "confirmed_unique": state.get("confirmed_unique", 0),
            "review": state.get("review_count", 0),
            "pending_advance": state.get("pending_advance", False),
            "navigation_recoveries": state.get("navigation_recoveries", 0),
            "consecutive_unrecovered_navigation_failures": state.get(
                "consecutive_unrecovered_navigation_failures", 0
            ),
            "frame_matches": sum(
                bool(item.get("hash_match")) for item in state.get("frame_diagnostics", [])
            ),
            "frame_mismatches": sum(
                not bool(item.get("hash_match")) for item in state.get("frame_diagnostics", [])
            ),
            "target": limit,
            "last_status": state.get("current_status", "RUNNING"),
            "profile_read_ready": state.get("profile_read_ready", False),
        })

    def save() -> None:
        dropped = _write_outputs(output_dir, state["confirmed"], state["review"])
        _atomic_json(checkpoint_path, _checkpoint_snapshot(state))
        with (output_dir / "wechat_contacts_150.csv").open(
            encoding="utf-8-sig", newline=""
        ) as fp:
            csv_ids = [row["wechat_id"] for row in csv.DictReader(fp)]
        txt_ids = [
            line.strip()
            for line in (output_dir / "wechat_contacts_150.txt")
            .read_text(encoding="utf-8-sig")
            .splitlines()
            if line.strip()
        ]
        report = _keyboard_report(state, csv_ids, txt_ids, dropped)
        _atomic_json(output_dir / "stats.json", report)
        _atomic_json(output_dir / "WECHAT_FOREGROUND_DECOUPLED_EXPORT_REPORT.json", report)
        frame_diagnostics = state.get("frame_diagnostics", [])
        clean_first_ten = frame_diagnostics[:10]
        _atomic_json(output_dir / "frame_validation.json", {
            "visited": len(frame_diagnostics),
            "frame_matches": sum(bool(item.get("hash_match")) for item in frame_diagnostics),
            "frame_mismatches": sum(not bool(item.get("hash_match")) for item in frame_diagnostics),
            "clean_first_10": {
                "visited": len(clean_first_ten),
                "frame_matches": sum(bool(item.get("hash_match")) for item in clean_first_ten),
                "frame_mismatches": sum(not bool(item.get("hash_match")) for item in clean_first_ten),
                "ocr_statuses": [item.get("ocr_status", "") for item in clean_first_ten],
                "diagnostics": clean_first_ten,
                "status": (
                    "PASS" if len(clean_first_ten) == 10
                    and all(item.get("hash_match") for item in clean_first_ten)
                    else "BLOCKED" if any(not item.get("hash_match") for item in clean_first_ten)
                    else "IN_PROGRESS"
                ),
            },
        })
        write_live_status()

    def heartbeat_loop() -> None:
        while True:
            time.sleep(1.0)
            write_live_status()

    threading.Thread(target=heartbeat_loop, name="batch-heartbeat", daemon=True).start()

    try:
        if not manual_bootstrap:
            raise NavigationFailure("MANUAL_BOOTSTRAP_REQUIRED")
        state["current_status"] = "OPERATOR_ACTION_REQUIRED"
        save()
        session = _manual_bootstrap_session()
        nav = session.navigator
        nav.current_profile_frame = nav.last_stable_frame
        nav.current_profile_frame_hash = session.profile_header_hash
        state["profile_read_ready"] = True
        state["bootstrap"]["profile_ready"] = True
        state["current_status"] = "PROFILE_READ_READY"
        state["bootstrap"]["operator_interventions"] = state["bootstrap"].get("operator_interventions", 0)
        state["last_profile_hash"] = session.profile_header_hash
        if not state["sequence_index"]:
            state["sequence_index"] = 1
        if not resume:
            state["pending_advance"] = False
        save()

        if arm_delay > 0:
            from wechat_ocr import DEFAULT_CONFIG
            from wechat_ocr.engine import get_ocr_engine

            get_ocr_engine(DEFAULT_CONFIG)
            print("ARMED", flush=True)
            print(f"请在 {arm_delay} 秒内切换到微信窗口；之后不要返回终端。", flush=True)
            for _ in range(arm_delay):
                time.sleep(1)

        def operator_focus_recovery() -> bool:
            nonlocal last_progress, no_progress_started
            state["bootstrap"]["operator_interventions"] = state["bootstrap"].get("operator_interventions", 0) + 1
            print("OPERATOR_ACTION_REQUIRED", flush=True)
            print("PAUSED_FOREGROUND_LOST", flush=True)
            print("请点击微信窗口，然后不要再操作。", flush=True)
            state["foreground_pauses"] = state.get("foreground_pauses", 0) + 1
            state["input_foreground_ready"] = False
            state["current_status"] = "PAUSED_FOREGROUND_LOST"
            save()
            wait_for_foreground(
                is_ready=lambda: is_weixin_foreground(nav.hwnd),
                heartbeat=write_live_status,
                interval=1.0,
            )
            state["input_foreground_ready"] = True
            state["current_status"] = "RUNNING"
            last_progress = time.monotonic()
            no_progress_started = 0.0
            save()
            return True

        def set_navigation_status(status: str, diagnostics: dict | None = None) -> None:
            state["current_status"] = status
            if diagnostics is not None:
                state["last_navigation_diagnostics"] = diagnostics
            _atomic_json(checkpoint_path, state)
            write_live_status()
            print(status, flush=True)

        def record_navigation_click() -> None:
            state["navigation_clicks"] = state.get("navigation_clicks", 0) + 1

        def record_unrecovered_cycle() -> None:
            state["navigation_stalls"] = state.get("navigation_stalls", 0) + 1
            state["navigation_stalls_total"] = state.get("navigation_stalls", 0)
            state["consecutive_unrecovered_navigation_failures"] = state.get(
                "consecutive_unrecovered_navigation_failures", 0
            ) + 1

        navigator = ContactRowClickNavigator(
            nav,
            on_status=set_navigation_status,
            is_foreground=lambda: is_weixin_foreground(nav.hwnd),
            pause_foreground=operator_focus_recovery,
            on_click=record_navigation_click,
            on_cycle_failure=record_unrecovered_cycle,
            consecutive_failures=state.get("consecutive_unrecovered_navigation_failures", 0),
        )
        last_progress = time.monotonic()
        no_progress_started = 0.0

        while state["confirmed_unique"] < limit:
            if time.monotonic() - last_progress >= 15.0:
                if not no_progress_started:
                    no_progress_started = time.monotonic()
                    print("NO_PROGRESS", flush=True)
                    state["current_status"] = "OPERATOR_ACTION_REQUIRED"
                    save()
                if time.monotonic() - no_progress_started >= 30.0:
                    state["stop_reason"] = "OPERATOR_ACTION_REQUIRED"
                    save()
                    return 2

            if state.get("pending_advance"):
                move = navigator.move_next(state["last_profile_hash"])
                if not move.moved:
                    state["current_status"] = move.status
                    state["stop_reason"] = move.status
                    save()
                    if move.status == "NAVIGATION_STALLED":
                        print("NAVIGATION_STALLED", flush=True)
                        while True:
                            write_live_status()
                            time.sleep(1.0)
                    # A failed bounded recovery is one failed transition. Keep
                    # pending_advance intact and retry from the same checkpoint.
                    last_progress = time.monotonic()
                    no_progress_started = 0.0
                    continue
                if move.status == "NAVIGATION_RECOVERED":
                    state["navigation_recoveries"] = state.get("navigation_recoveries", 0) + 1
                state["consecutive_unrecovered_navigation_failures"] = 0
                state["last_profile_hash"] = move.profile_hash
                state["sequence_index"] += 1
                input_ready = is_weixin_foreground(nav.hwnd)
                state["input_foreground_ready"] = input_ready
                if state["sequence_index"] == 2:
                    state["first_down"] = {"foreground_ready": input_ready, "profile_changed": True}
                state["profile_changes"] = state.get("profile_changes", 0) + 1
                if state["sequence_index"] == 2:
                    state["bootstrap"]["keyboard_focus_probe"] = "YES"
                state["pending_advance"] = False
                state["navigation_state"] = "PROFILE_CHANGED_STABLE"
                save()

            current_profile_hash = session.profile_header_hash if state["contacts_visited"] == 0 else _stable_initial_header(nav, timeout=2.0)
            if not current_profile_hash:
                state["stop_reason"] = "NAVIGATION_STALLED"
                state["navigation_stalls"] += 1
                save()
                print("NAVIGATION_STALLED", flush=True)
                return 2
            previous_profile_hash = state.get("last_processed_profile_hash", "")
            extraction_attempt = 0

            extraction_frame = getattr(nav, "current_profile_frame", None)
            extraction_frame_hash = (
                _profile_header_hash(extraction_frame) if extraction_frame is not None else ""
            )
            frame_match = frame_hashes_match(current_profile_hash, extraction_frame_hash)
            nav_hash_prefix = current_profile_hash[:12]
            extraction_hash_prefix = extraction_frame_hash[:12]
            state["last_navigation_profile_hash_prefix"] = nav_hash_prefix
            state["last_extraction_frame_hash_prefix"] = extraction_hash_prefix
            state["last_frame_hash_match"] = frame_match
            frame_diag = {
                "sequence_index": state["sequence_index"],
                "navigation_profile_hash_prefix": nav_hash_prefix,
                "extraction_frame_hash_prefix": extraction_hash_prefix,
                "hash_match": frame_match,
                "ocr_status": "NOT_STARTED",
            }
            state.setdefault("frame_diagnostics", []).append(frame_diag)
            if not frame_match:
                frame_diag["ocr_status"] = "FRAME_MISMATCH"
                state["current_status"] = "FRAME_MISMATCH"
                state["stop_reason"] = "BLOCKED_FRAME_MISMATCH"
                save()
                print("FRAME_MISMATCH", flush=True)
                return 2

            def extract_with_escalation(frame):
                nonlocal extraction_attempt
                extraction_attempt += 1
                return nav.extract_selected_profile(
                    page_verified=session.page_evidence.confirmed,
                    uia_candidate_provider=session._uia_candidate,
                    open_profile_popup=False,
                    ocr_scale=1 if extraction_attempt == 1 else 2,
                    allow_recrop=extraction_attempt >= 3,
                    image=frame,
                )[0]

            result_status, result = process_current_contact(
                extract_with_escalation,
                frame=extraction_frame,
                navigation_profile_hash=current_profile_hash,
                extraction_frame_hash=extraction_frame_hash,
                max_attempts=MAX_ROW_ATTEMPTS,
            )
            if result_status == "FRAME_MISMATCH":
                frame_diag["ocr_status"] = "FRAME_MISMATCH"
                state["current_status"] = "FRAME_MISMATCH"
                state["stop_reason"] = "BLOCKED_FRAME_MISMATCH"
                save()
                print("FRAME_MISMATCH", flush=True)
                return 2
            frame_diag["ocr_status"] = result_status
            value = str(getattr(result, "value", "") or "")
            confidence = getattr(result, "confidence", None)
            confidence_text = "" if confidence is None else f"{confidence:.5f}"
            source = str(getattr(result, "source", "none"))
            terminal = result_status

            from wechat_ocr.masking import contains_mask_placeholder
            from wechat_ocr.validator import validate_wechat_id
            if result_status == "CONFIRMED" and (
                contains_mask_placeholder(value) or not validate_wechat_id(value).valid
            ):
                result_status = terminal = "SOURCE_VALUE_MASKED" if contains_mask_placeholder(value) else "FINAL_REVIEW"

            if result_status == "CONFIRMED":
                id_hash = _id_hash(value)
                existing_ids = {row["wechat_id"] for row in state["confirmed"]}
                duplicate = classify_duplicate_id(
                    value,
                    existing_ids,
                    previous_profile_hash or current_profile_hash,
                    current_profile_hash,
                )
                if duplicate in {"SUSPECT_NAVIGATION_DUPLICATE", "DUPLICATE_WECHAT_ID_ANOMALY"}:
                    state["duplicate_results"] += 1
                    terminal = "REVIEW duplicate_existing_id"
                    state["review"].append(
                        {"row_ordinal": state["sequence_index"], "status": duplicate,
                         "reason": "already_confirmed_id", "candidate_masked": _mask(value),
                         "confidence": confidence_text, "source": source}
                    )
                else:
                    state["confirmed"].append(
                        {"contact_index": len(state["confirmed"]) + 1, "wechat_id": value,
                         "status": "CONFIRMED", "source": source, "confidence": confidence_text}
                    )
                    state["confirmed_id_hashes"].append(id_hash)
                    state["confirmed_unique"] = len(existing_ids) + 1
                    state["last_confirmed_id_hash"] = id_hash
                    state["last_confirmed_profile_hash"] = current_profile_hash
            elif result_status == "SOURCE_VALUE_MASKED":
                state["review"].append(
                    {"row_ordinal": state["sequence_index"], "status": "SOURCE_VALUE_MASKED",
                     "reason": "source_ui_masked",
                     "candidate_masked": str(getattr(result, "candidate_masked", "") or _mask(value)),
                     "confidence": confidence_text, "source": source}
                )
            else:
                state["ocr_failures"] += 1
                state["failed_rows"] += 1
                state["review"].append(
                    {"row_ordinal": state["sequence_index"], "status": "FINAL_REVIEW",
                     "reason": str(getattr(result, "reason", "OCR_FAILED") or "OCR_FAILED"),
                     "candidate_masked": _mask(value), "confidence": confidence_text,
                     "source": source}
                )

            state["contacts_visited"] += 1
            if state["contacts_visited"] == 1:
                state["first_contact_ocr_status"] = getattr(result, "status", terminal)
            state["review_count"] = len(state["review"])
            state["last_processed_profile_hash"] = current_profile_hash
            state["last_profile_hash"] = current_profile_hash
            state["navigation_state"] = terminal
            state["pending_advance"] = state["confirmed_unique"] < min(limit, TARGET)
            state["current_status"] = terminal
            state["input_foreground_ready"] = is_weixin_foreground(nav.hwnd)
            save()
            last_progress = time.monotonic()
            no_progress_started = 0.0
            print(f"[visited={state['contacts_visited']} | confirmed={state['confirmed_unique']}/{limit} | review={state['review_count']}]", flush=True)
            print("CONFIRMED" if terminal == "CONFIRMED" else f"REVIEW {terminal}", flush=True)

            if state["contacts_visited"] == 5:
                mouse_navigation_failures = state.get("navigation_failures", 0) - state[
                    "mouse_navigation_baseline"
                ].get("navigation_failures", 0)
                gate = {
                    "profiles_visited": 5,
                    "profile_changes": state.get("profile_changes", 0),
                    "confirmed": state["confirmed_unique"],
                    "review": state["review_count"],
                    "navigation_failures": mouse_navigation_failures,
                    "legacy_keyboard_navigation_failures": state[
                        "mouse_navigation_baseline"
                    ].get("navigation_failures", 0),
                    "duplicate_writes": state["duplicate_writes"],
                }
                gate["status"] = "PASS" if gate["profile_changes"] >= 4 and gate["navigation_failures"] == 0 and gate["duplicate_writes"] == 0 and (gate["confirmed"] + gate["review"] >= 5) else "BLOCKED"
                state["first_5"] = gate
                save()
                print("FIRST_5 PASS; CONTINUING" if gate["status"] == "PASS" else "FIRST_5 BLOCKED", flush=True)
                if gate["status"] != "PASS":
                    state["stop_reason"] = "NAVIGATION_STALLED"
                    return 2

        state["stop_reason"] = "UNIQUE_TARGET_REACHED"
        state["pending_advance"] = False
        state["finished"] = True
        save()
        print("BATCH_GOAL_REACHED", flush=True)
        return 0
    except KeyboardInterrupt:
        state["stop_reason"] = "OPERATOR_INTERRUPT"
        save()
        print(f"{state['stop_reason']} checkpoint=SAVED", flush=True)
        return 3
    except NavigationFailure as exc:
        status = str(getattr(exc, "status", type(exc).__name__))
        state["stop_reason"] = (
            "OPERATOR_ACTION_REQUIRED"
            if status in {"OPERATOR_FOREGROUND_REQUIRED", "WINDOW_FOREGROUND_LOST"}
            else status
        )
        state["navigation_state"] = status
        save()
        print(state["stop_reason"], flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001 - GUI/OCR backends raise varied errors; checkpoint before pausing.
        state["stop_reason"] = f"BLOCKED_{type(exc).__name__}"
        save()
        print(f"BATCH_RECOVERY_REQUIRED checkpoint=SAVED error={type(exc).__name__}", flush=True)
        return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only WeChat ID export")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--manual-bootstrap", action="store_true")
    parser.add_argument("--probe-current-contact", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("output/final"))
    parser.add_argument("--target", "--limit", dest="limit", type=int, default=TARGET)
    parser.add_argument("--arm-delay", type=int, default=0)
    parser.add_argument(
        "--preflight-contacts",
        type=int,
        default=0,
        help="If >0: run a production preflight of that many unique confirmed "
        "contacts and stop.",
    )
    args = parser.parse_args(argv)
    if args.probe_current_contact:
        try:
            report = probe_current_contact()
        except Exception as exc:  # noqa: BLE001 - probe reports backend failures without exposing details.
            print("PROFILE_READ_READY: false", flush=True)
            print("OCR_BOX_COUNT: 0", flush=True)
            print("WECHAT_LABEL_FOUND: false", flush=True)
            print("CANDIDATE_FOUND: false", flush=True)
            print("CANDIDATE_MASKED: ", flush=True)
            print("CONFIDENCE: ", flush=True)
            print(f"STATUS: OCR_FAILED ({type(exc).__name__})", flush=True)
            return 2
        return 0 if report["profile_read_ready"] else 2
    return run_keyboard_batch(
        output_dir=args.output_dir,
        resume=args.resume,
        limit=args.limit,
        preflight_contacts=args.preflight_contacts,
        manual_bootstrap=args.manual_bootstrap,
        arm_delay=args.arm_delay,
    )


if __name__ == "__main__":
    raise SystemExit(main())
