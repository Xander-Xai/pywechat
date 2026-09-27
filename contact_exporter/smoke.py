"""Read-only diagnostic entry points for manually opened WeChat surfaces."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageChops, ImageStat

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
MANUAL_PROFILE_POPUP_NOT_DETECTED = "MANUAL_PROFILE_POPUP_NOT_DETECTED"
POPUP_CAPTURE_UNAVAILABLE = "POPUP_CAPTURE_UNAVAILABLE"
PROFILE_POPUP_VERIFIED = "PROFILE_POPUP_VERIFIED"
PROFILE_POPUP_UNVERIFIED = "PROFILE_POPUP_UNVERIFIED"


@dataclass(frozen=True)
class PopupWindow:
    hwnd: int
    owner: int
    pid: int
    process_name: str
    class_name: str
    title: str
    bounds: tuple[int, int, int, int]
    source: str = "owned_profile_popup"


@dataclass(frozen=True)
class PopupOcr:
    profile_verified: bool
    box_count: int
    wechat_label_found: bool
    candidate_found: bool
    candidate_masked: str
    confidence: float | None
    status: str
    reason: str = ""


def enumerate_weixin_profile_popups() -> list[PopupWindow]:
    """Enumerate profile popup HWNDs owned by a Weixin process, read-only."""
    import os

    import psutil
    import win32gui
    import win32process

    windows: list[PopupWindow] = []
    main_windows: list[PopupWindow] = []

    def visit(hwnd: int, _context) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name()
            if os.path.basename(process_name).casefold() not in {
                "weixin.exe",
                "wechat.exe",
            }:
                return True
            rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
            owner = int(win32gui.GetWindow(hwnd, 4) or 0)
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            if width < 240 or height < 180 or width > 2400 or height > 1800:
                return True
            record = PopupWindow(
                hwnd, owner, pid, process_name, class_name, title, rect
            )
            if class_name == "mmui::ProfileUniquePop" and title == "Weixin":
                windows.append(record)
            elif class_name in {
                "Qt51514QWindowIcon",
                "mmui::MainWindow",
            } and title.casefold() in {"weixin", "微信"}:
                main_windows.append(
                    PopupWindow(
                        hwnd,
                        owner,
                        pid,
                        process_name,
                        class_name,
                        title,
                        rect,
                        "main_window_visual_surface",
                    )
                )
        except Exception:
            # Window destruction during enumeration is a normal race.
            return True
        return True

    win32gui.EnumWindows(visit, None)
    # Some Weixin builds render Friend Profile as a visual surface inside the
    # main HWND and expose no owned profile HWND. OCR field verification below
    # decides whether this main-window candidate is actually a profile view.
    return windows or main_windows


def capture_popup(window: PopupWindow) -> Image.Image:
    """Capture a popup rectangle in memory without changing window focus."""
    from PIL import ImageGrab

    image = ImageGrab.grab(bbox=window.bounds, all_screens=True).convert("RGB")
    expected = (
        window.bounds[2] - window.bounds[0],
        window.bounds[3] - window.bounds[1],
    )
    if image.size != expected:
        raise RuntimeError("popup capture dimensions do not match window bounds")
    if max(ImageStat.Stat(image.convert("L")).stddev) < 1.5:
        raise RuntimeError("popup capture is blank or unavailable")
    return image


def _capture_is_stable(first: Image.Image, second: Image.Image) -> bool:
    if first.size != second.size:
        return False
    difference = ImageChops.difference(first.convert("L"), second.convert("L"))
    return ImageStat.Stat(difference).mean[0] <= 1.4


def _mean_frame_delta(first: Image.Image, second: Image.Image) -> float:
    if first.size != second.size:
        return float("inf")
    difference = ImageChops.difference(first.convert("L"), second.convert("L"))
    return ImageStat.Stat(difference).mean[0]


def _manual_surface_crop(window: PopupWindow, image: Image.Image) -> Image.Image:
    """Crop the centered in-window profile surface when no popup HWND exists."""
    if window.source != "main_window_visual_surface":
        return image
    width, height = image.size
    surface_height = min(height, round(height * 0.75))
    surface_width = min(width, round(surface_height * 0.72))
    if surface_width < 160 or surface_height < 180:
        raise RuntimeError("main window is too small for a profile surface")
    left = (width - surface_width) // 2
    top = (height - surface_height) // 2
    return image.crop((left, top, left + surface_width, top + surface_height))


def recognize_profile_fields(image: Image.Image) -> tuple[bool, int]:
    """Use the configured shared detector to verify profile-like popup content."""
    from pyweixin.visual_navigation import has_profile_content
    from wechat_ocr.config import DEFAULT_CONFIG
    from wechat_ocr.engine import get_ocr_engine

    tokens = get_ocr_engine(DEFAULT_CONFIG).recognize(image)
    return has_profile_content(tokens), len(tokens)


def extract_popup_ocr(image: Image.Image, profile_verified: bool) -> PopupOcr:
    """Run the production dual-view extractor and return only masked evidence."""
    from wechat_ocr import DEFAULT_CONFIG, WeChatIdExtractor
    from wechat_ocr.masking import mask_wechat_id

    result = WeChatIdExtractor(config=DEFAULT_CONFIG).extract(image)
    label_found = any(
        bool(item.get("has_wechat_label")) for item in result.view_diagnostics
    )
    candidates = list(dict.fromkeys(value for value in result.ocr_candidates if value))
    candidate_found = bool(candidates)
    masked_candidates = [mask_wechat_id(value) for value in candidates]
    return PopupOcr(
        profile_verified=profile_verified,
        box_count=sum(
            int(item.get("detected_box_count", 0)) for item in result.view_diagnostics
        ),
        wechat_label_found=label_found,
        candidate_found=candidate_found,
        candidate_masked=(
            mask_wechat_id(result.value)
            if result.value
            else " | ".join(masked_candidates)
        ),
        confidence=result.confidence,
        status=result.status,
        reason=getattr(result, "reason", "") or "",
    )


class ManualPopupSmoke:
    """Poll for up to three operator-opened popups with no input/focus APIs."""

    def __init__(
        self,
        *,
        enumerate_popups: Callable[
            [], Sequence[PopupWindow]
        ] = enumerate_weixin_profile_popups,
        capture: Callable[[PopupWindow], Image.Image] = capture_popup,
        verify_profile: Callable[
            [Image.Image], tuple[bool, int]
        ] = recognize_profile_fields,
        extract: Callable[[Image.Image, bool], PopupOcr] = extract_popup_ocr,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.enumerate_popups = enumerate_popups
        self.capture = capture
        self.verify_profile = verify_profile
        self.extract = extract
        self.monotonic = monotonic
        self.sleep = sleep

    def _wait_for_popup(
        self, timeout_seconds: float
    ) -> tuple[PopupWindow | None, str | None]:
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            candidates = self.enumerate_popups()
            if candidates:
                return candidates[0], None
            self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))
        return None, MANUAL_PROFILE_POPUP_NOT_DETECTED

    def _wait_until_closed(
        self,
        popup: PopupWindow,
        timeout_seconds: float,
        last_surface: Image.Image,
    ) -> None:
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            if popup.source == "main_window_visual_surface":
                try:
                    current = _manual_surface_crop(popup, self.capture(popup))
                except Exception:
                    self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))
                    continue
                # The embedded popup has no separate HWND. Wait for a material
                # visual transition (operator closes or changes the profile).
                if _mean_frame_delta(last_surface, current) >= 3.0:
                    return
            elif all(window.hwnd != popup.hwnd for window in self.enumerate_popups()):
                return
            self.sleep(min(0.5, max(0.0, deadline - self.monotonic())))

    def run(
        self, *, max_contacts: int = 3, wait_seconds: float = 60.0
    ) -> list[dict[str, object]]:
        if not 1 <= max_contacts <= 3:
            raise ValueError("manual popup smoke is limited to 1..3 contacts")
        reports: list[dict[str, object]] = []
        print(
            MANUAL_ACTION_REQUIRED
            + "\nMANUAL ACTION: Bring Weixin to the foreground, open Contacts, "
            + "select a contact, open its profile popup, and leave it open."
            + "\n请手动将微信切到前台，打开通讯录并选择联系人，"
            + "点击头像/资料入口，保持个人资料 popup 打开。",
            flush=True,
        )

        for index in range(1, max_contacts + 1):
            started = self.monotonic()
            popup, wait_error = self._wait_for_popup(wait_seconds)
            if popup is None:
                reports.append(
                    self._empty_report(
                        index,
                        wait_error or MANUAL_PROFILE_POPUP_NOT_DETECTED,
                        started,
                        profile_status=MANUAL_PROFILE_POPUP_NOT_DETECTED,
                    )
                )
                break
            try:
                first = _manual_surface_crop(popup, self.capture(popup))
                self.sleep(0.25)
                second = _manual_surface_crop(popup, self.capture(popup))
            except Exception:
                reports.append(
                    self._empty_report(
                        index,
                        POPUP_CAPTURE_UNAVAILABLE,
                        started,
                        popup_detected=True,
                        profile_status=POPUP_CAPTURE_UNAVAILABLE,
                    )
                )
                break
            if not _capture_is_stable(first, second):
                reports.append(
                    self._empty_report(
                        index,
                        POPUP_CAPTURE_UNAVAILABLE,
                        started,
                        popup_detected=True,
                        profile_status=POPUP_CAPTURE_UNAVAILABLE,
                    )
                )
                break

            try:
                profile_verified, box_count = self.verify_profile(second)
            except Exception:
                reports.append(
                    self._empty_report(
                        index,
                        "OCR_FAILED",
                        started,
                        popup_detected=True,
                        profile_status="OCR_FAILED",
                    )
                )
                break
            if not profile_verified:
                reports.append(
                    self._empty_report(
                        index,
                        PROFILE_POPUP_UNVERIFIED,
                        started,
                        popup_detected=True,
                        profile_status=PROFILE_POPUP_UNVERIFIED,
                    )
                )
                break

            try:
                ocr = self.extract(second, True)
            except Exception:
                reports.append(
                    self._empty_report(
                        index,
                        "OCR_FAILED",
                        started,
                        popup_detected=True,
                        profile_verified=True,
                        box_count=box_count,
                        profile_status=PROFILE_POPUP_VERIFIED,
                    )
                )
                break
            report = {
                "index": index,
                "popup_detected": True,
                "profile_verified": True,
                "profile_status": PROFILE_POPUP_VERIFIED,
                "box_count": max(box_count, ocr.box_count),
                "wechat_label_found": ocr.wechat_label_found,
                "candidate_found": ocr.candidate_found,
                "candidate_masked": ocr.candidate_masked,
                "confidence": ocr.confidence,
                "status": ocr.status,
                "reason": ocr.reason,
                "latency": round(self.monotonic() - started, 3),
            }
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
            if index < max_contacts:
                print(
                    f"MANUAL_ACTION_REQUIRED: 请关闭第 {index} 个 popup，"
                    f"再手动打开下一个联系人资料 popup；"
                    f"等待 {wait_seconds:g} 秒。",
                    flush=True,
                )
                self._wait_until_closed(popup, wait_seconds, second)
        return reports

    def _empty_report(
        self,
        index: int,
        status: str,
        started: float,
        *,
        popup_detected: bool = False,
        profile_verified: bool = False,
        box_count: int = 0,
        profile_status: str = PROFILE_POPUP_UNVERIFIED,
    ) -> dict[str, object]:
        report = {
            "index": index,
            "popup_detected": popup_detected,
            "profile_verified": profile_verified,
            "profile_status": profile_status,
            "box_count": box_count,
            "wechat_label_found": False,
            "candidate_found": False,
            "candidate_masked": "",
            "confidence": None,
            "status": status,
            "latency": round(self.monotonic() - started, 3),
        }
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only WeChat popup OCR smoke")
    parser.add_argument("--manual-profile-popup", action="store_true")
    parser.add_argument("--max-contacts", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not args.manual_profile_popup:
        parser.error("use --manual-profile-popup to start operator-assisted capture")
    reports = ManualPopupSmoke().run(
        max_contacts=args.max_contacts, wait_seconds=args.wait_seconds
    )
    return 0 if reports and any(item["profile_verified"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
