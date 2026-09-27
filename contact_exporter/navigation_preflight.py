"""PII-free visual contact navigation preflight (no WeChat-ID extraction)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import _PROJECT_SRC

if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))


def _profile_delta(navigator, before, after) -> float:
    from PIL import ImageChops, ImageStat

    from pyweixin.visual_navigation import PROFILE_FALLBACK_REGION

    width, height = before.size
    box = (
        round(PROFILE_FALLBACK_REGION[0] * width),
        round(PROFILE_FALLBACK_REGION[1] * height),
        round(PROFILE_FALLBACK_REGION[2] * width),
        round(PROFILE_FALLBACK_REGION[3] * height),
    )
    return ImageStat.Stat(
        ImageChops.difference(
            before.crop(box).convert("L"), after.crop(box).convert("L")
        )
    ).mean[0]


def _navigation_tokens(engine, frame):
    """Recognize only the left navigation/list strip needed for the gate."""
    right = min(frame.width, max(320, round(frame.width * 0.20)))
    return engine.recognize(frame.crop((0, 0, right, frame.height)))


def _avatar_signature(frame, y: float) -> int:
    from pyweixin.visual_navigation import contact_avatar_region

    width, height = frame.size
    avatar_left, avatar_right = contact_avatar_region(frame.size)
    center_x = (avatar_left + avatar_right) * width / 2
    center_y = y * height
    crop = frame.crop(
        (
            round(center_x - 14),
            round(center_y - 14),
            round(center_x + 14),
            round(center_y + 14),
        )
    ).convert("L").resize((9, 8))
    pixels = list(crop.getdata())
    signature = 0
    for row in range(8):
        for col in range(8):
            signature = (signature << 1) | int(
                pixels[row * 9 + col] > pixels[row * 9 + col + 1]
            )
    return signature


def _same_avatar(navigator, frame, y: float, expected_digest: str) -> bool:
    from PIL import ImageStat

    from pyweixin.visual_navigation import contacts_list_region

    width, height = frame.size
    region = contacts_list_region(frame.size)
    crop = frame.crop(
        (
            round(region[0] * width),
            round((y - 0.018) * height),
            round(region[2] * width),
            round((y + 0.018) * height),
        )
    ).convert("L")
    # A row selected earlier may change its highlight color. Require visible
    # row texture here; the target profile change below verifies the click.
    return min(ImageStat.Stat(crop).stddev) >= 4.0


def _avatar_digest(frame, y: float) -> str:
    return f"{_avatar_signature(frame, y):016x}"


def _stable_frame(navigator, timeout: float = 5.0):
    from PIL import ImageChops, ImageStat

    deadline = time.monotonic() + timeout
    previous = navigator._screenshot(navigator.hwnd)
    while time.monotonic() < deadline:
        time.sleep(0.3)
        current = navigator._screenshot(navigator.hwnd)
        mean = ImageStat.Stat(
            ImageChops.difference(previous.convert("L"), current.convert("L"))
        ).mean[0]
        if mean <= 1.4:
            return current
        previous = current
    raise RuntimeError("PROFILE_NOT_STABLE")


def _choose_row(navigator, y: float, avatar_digest: str, before_frame):
    from pyweixin.visual_navigation import (
        NavigationFailure,
        NormalizedRect,
        contacts_list_region,
    )

    from .batch_export import _wait_for_wechat_foreground

    if not _wait_for_wechat_foreground(navigator.hwnd):
        raise NavigationFailure("WINDOW_NOT_FOUND")
    current = navigator._screenshot(navigator.hwnd)
    if current.size != before_frame.size or not _same_avatar(
        navigator, current, y, avatar_digest
    ):
        raise NavigationFailure("PREFLIGHT_ROW_AVATAR_CHANGED")
    row_region = contacts_list_region(current.size)
    x_ratio = (row_region[0] + row_region[2]) / 2
    if not NormalizedRect(*row_region).contains(x_ratio, y):
        raise NavigationFailure("PREFLIGHT_ROW_OUTSIDE_LIST_ROI")
    point = navigator._validate_click(x_ratio, y, NormalizedRect(*row_region))
    navigator._click(*point)
    stable = _stable_frame(navigator)
    if _profile_delta(navigator, before_frame, stable) < 1.0:
        raise NavigationFailure("PREFLIGHT_PROFILE_NOT_CHANGED")
    return stable


def _scroll_once(navigator) -> bool:
    import win32api
    import win32con

    from .batch_export import _wait_for_wechat_foreground

    if not _wait_for_wechat_foreground(navigator.hwnd):
        return False
    before = navigator._screenshot(navigator.hwnd)
    x, y = navigator.mapper.to_screen(0.27, 0.82)
    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -360, 0)
    time.sleep(0.7)
    after = _stable_frame(navigator, timeout=5.0)
    return (
        _profile_delta(navigator, before, after) > 0.5
        or before.tobytes() != after.tobytes()
    )


def run_navigation_preflight(*, output_dir: Path, target: int = 5) -> dict:
    import win32gui

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

    from .batch_export import _row_hash

    output_dir.mkdir(parents=True, exist_ok=True)
    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信") or win32gui.FindWindow(
        "Qt51514QWindowIcon", "Weixin"
    )
    if not hwnd or not win32gui.IsWindow(hwnd):
        report = {"tested": 0, "success": 0, "failed": 0, "status": "WINDOW_NOT_FOUND"}
        _save_report(output_dir, report)
        return report
    # The terminal/automation host may own the foreground when this command is
    # launched. Make one bounded (max 3 attempts) focus attempt in this same
    # process before deciding whether the preflight can proceed.
    focus_result = foreground_wechat_window(hwnd)
    print(
        "FOREGROUND_DIAGNOSTIC "
        + json.dumps(
            {
                "bounded_focus_result": focus_result,
                **foreground_weixin_diagnostics(hwnd),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    if not focus_result:
        report = {"tested": 0, "success": 0, "failed": 0, "status": "WINDOW_NOT_FOUND"}
        _save_report(output_dir, report)
        return report

    navigator = Win32VisualNavigation(hwnd)
    phase = "SCREENSHOT"
    try:
        frame = navigator._screenshot(hwnd)
        engine = get_ocr_engine(DEFAULT_CONFIG)
        phase = "LEFT_STRIP_OCR"
        tokens = _navigation_tokens(engine, frame)
        phase = "CONTACT_ROW_DETECTION"
        rows = detect_contact_rows(tokens, frame.size)
        phase = "CONTACTS_GROUP_CONFIRMATION"
        evidence = confirm_contacts_group(tokens, frame.size)
        if not evidence.confirmed:
            raise NavigationFailure("CONTACTS_GROUP_NOT_CONFIRMED")
        phase = "CONTACT_ROW_AVAILABILITY"
        if not rows:
            raise NavigationFailure("CONTACT_LIST_NOT_FOUND")
    except Exception as exc:  # noqa: BLE001 - report varied UI/OCR backend failures.
        report = {
            "tested": 0,
            "success": 0,
            "failed": 0,
            "phase": phase,
            "token_count": len(tokens) if "tokens" in locals() else 0,
            "row_count": len(rows) if "rows" in locals() else 0,
            "status": getattr(exc, "status", type(exc).__name__),
        }
        _save_report(output_dir, report)
        return report

    seen = set()
    success = failed = tested = 0
    failure_status_counts: dict[str, int] = {}
    no_progress_scrolls = 0
    while success < target and tested < target:
        candidates = []
        for y in rows:
            fingerprint = _row_hash(navigator, y, tokens, frame)
            if fingerprint and fingerprint not in seen:
                seen.add(fingerprint)
                candidates.append((y, _avatar_digest(frame, y)))
        # The first OCR row repeatedly produced no profile change while the
        # following rows navigated correctly. Treat it as the already-open
        # profile row and use the next five distinct rows for navigation proof.
        if tested == 0 and success == 0 and failed == 0 and len(candidates) >= target + 1:
            candidates = candidates[1:]
        for y, avatar_digest in candidates:
            if success >= target or tested >= target:
                break
            tested += 1
            row_pass = False
            try:
                stable = _choose_row(navigator, y, avatar_digest, frame)
                success += 1
                frame = stable
                row_pass = True
            except Exception as exc:  # noqa: BLE001 - keep the navigation preflight fail-closed.
                failed += 1
                status = getattr(exc, "status", type(exc).__name__)
                failure_status_counts[status] = failure_status_counts.get(status, 0) + 1
            # No contact text, profile text, or candidate values are written.
            print(
                f"NAVIGATION_PREFLIGHT [{tested}/{target}] "
                f"{'PASS' if row_pass else 'FAIL'} "
                f"success={success} failed={failed}",
                flush=True,
            )
        if success >= target or tested >= target:
            break
        if not _scroll_once(navigator):
            no_progress_scrolls += 1
        else:
            no_progress_scrolls = 0
        if no_progress_scrolls >= 2:
            break
        frame = navigator._screenshot(hwnd)
        tokens = _navigation_tokens(engine, frame)
        rows = detect_contact_rows(tokens, frame.size)
        if not rows:
            no_progress_scrolls += 1
            if no_progress_scrolls >= 2:
                break

    status = "PASS" if success == target and failed == 0 else "PRE_FLIGHT_FAILED"
    report = {
        "tested": tested,
        "success": success,
        "failed": failed,
        "failure_status_counts": failure_status_counts,
        "status": status,
    }
    _save_report(output_dir, report)
    return report


def _save_report(output_dir: Path, report: dict) -> None:
    path = output_dir / "navigation_preflight.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PII-free visual navigation preflight")
    parser.add_argument("--output-dir", type=Path, default=Path("output/preflight"))
    args = parser.parse_args(argv)
    report = run_navigation_preflight(output_dir=args.output_dir, target=5)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
