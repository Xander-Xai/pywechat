"""Bounded, PII-free OCR preflight on three Contacts-group profiles."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import _PROJECT_SRC

if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))


def run_ocr_preflight(output_dir: Path) -> dict:
    from pyweixin.visual_navigation import (
        NavigationFailure,
        Win32VisualNavigation,
        confirm_contacts_group,
        detect_contact_rows,
        foreground_wechat_window,
        foreground_weixin_diagnostics,
    )
    from wechat_ocr.config import DEFAULT_CONFIG
    from wechat_ocr.engine import get_ocr_engine

    from .batch_export import _avatar_digest, _row_hash
    from .navigation_preflight import _choose_row, _navigation_tokens

    output_dir.mkdir(parents=True, exist_ok=True)
    nav_path = output_dir / "navigation_preflight.json"
    try:
        navigation = json.loads(nav_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report = {"tested": 0, "confirmed": 0, "status": "NAVIGATION_GATE_MISSING"}
        _save(output_dir, report)
        return report
    if not (
        navigation.get("status") == "PASS"
        and navigation.get("tested") == 5
        and navigation.get("success") == 5
    ):
        report = {"tested": 0, "confirmed": 0, "status": "NAVIGATION_GATE_FAILED"}
        _save(output_dir, report)
        return report

    import win32gui

    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信") or win32gui.FindWindow(
        "Qt51514QWindowIcon", "Weixin"
    )
    if not hwnd or not foreground_wechat_window(hwnd):
        report = {"tested": 0, "confirmed": 0, "status": "WEIXIN_FOREGROUND_FAILED"}
        _save(output_dir, report)
        return report
    print(
        "OCR_PREFLIGHT_FOREGROUND "
        + json.dumps(foreground_weixin_diagnostics(hwnd), separators=(",", ":")),
        flush=True,
    )

    navigator = Win32VisualNavigation(hwnd)
    tested = visited = confirmed = 0
    outcomes: Counter[str] = Counter()
    try:
        frame = navigator._screenshot(hwnd)
        engine = get_ocr_engine(DEFAULT_CONFIG)
        tokens = _navigation_tokens(engine, frame)
        group = confirm_contacts_group(tokens, frame.size)
        if not group.confirmed:
            raise NavigationFailure("CONTACTS_GROUP_NOT_CONFIRMED")
        rows = detect_contact_rows(tokens, frame.size)
        fingerprints = set()
        candidates = []
        for y in rows:
            fingerprint = _row_hash(navigator, y, tokens, frame)
            if fingerprint and fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                candidates.append((y, _avatar_digest(frame, y)))
        # The first visible row is the currently open detail profile on this
        # desktop layout; it is excluded from the three new navigation samples.
        candidates = candidates[1:4]
        if len(candidates) < 3:
            raise NavigationFailure("CONTACTS_GROUP_HAS_TOO_FEW_ROWS")

        for y, avatar_digest in candidates:
            tested += 1
            try:
                stable = _choose_row(navigator, y, avatar_digest, frame)
                visited += 1
                frame = stable
                navigator.last_stable_frame = stable
                navigator.current_profile_frame = stable
                from pyweixin.visual_navigation import profile_hash

                navigator.current_profile_frame_hash = profile_hash(stable)
                result, _, _ = navigator.extract_selected_profile(
                    page_verified=True,
                    uia_candidate_provider=None,
                    open_profile_popup=False,
                    image=stable,
                )
                if result.is_confirmed and result.value:
                    confirmed += 1
                    outcomes["CONFIRMED"] += 1
                else:
                    outcomes[str(result.status)] += 1
            except Exception as exc:  # noqa: BLE001 - isolate a single preflight sample.
                outcomes[getattr(exc, "status", type(exc).__name__)] += 1

        status = (
            "PASS"
            if tested == 3 and visited == 3 and confirmed >= 1
            else "PRE_FLIGHT_FAILED"
        )
        report = {
            "tested": tested,
            "visited": visited,
            "confirmed": confirmed,
            "outcome_counts": dict(outcomes),
            "contacts_group_verified": True,
            "model": DEFAULT_CONFIG.model_label(),
            "status": status,
        }
    except Exception as exc:  # noqa: BLE001 - emit a sanitized preflight failure report.
        report = {
            "tested": tested,
            "visited": visited,
            "confirmed": confirmed,
            "outcome_counts": dict(outcomes),
            "contacts_group_verified": True,
            "status": getattr(exc, "status", type(exc).__name__),
        }
    _save(output_dir, report)
    return report


def _save(output_dir: Path, report: dict) -> None:
    (output_dir / "ocr_preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PII-free WeChat OCR preflight")
    parser.add_argument("--output-dir", type=Path, default=Path("output/preflight"))
    args = parser.parse_args(argv)
    report = run_ocr_preflight(args.output_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
