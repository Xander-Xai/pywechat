from __future__ import annotations

import contextlib
import io
import sys
import types

from PIL import Image, ImageDraw

from contact_exporter.smoke import (
    MANUAL_PROFILE_POPUP_NOT_DETECTED,
    POPUP_CAPTURE_UNAVAILABLE,
    PROFILE_POPUP_VERIFIED,
    ManualPopupSmoke,
    PopupOcr,
    PopupWindow,
    _manual_surface_crop,
    extract_popup_ocr,
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def popup(hwnd=80):
    return PopupWindow(
        hwnd=hwnd,
        owner=70,
        pid=700,
        process_name="Weixin.exe",
        class_name="mmui::ProfileUniquePop",
        title="Weixin",
        bounds=(10, 10, 410, 510),
    )


def patterned_image():
    image = Image.new("RGB", (400, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 150, 130), fill="#888888")
    draw.text((190, 60), "Profile fields", fill="black")
    return image


def test_main_window_visual_surface_uses_one_centered_popup_roi():
    main_window = PopupWindow(
        **{
            **popup().__dict__,
            "bounds": (0, 0, 1936, 1048),
            "source": "main_window_visual_surface",
        }
    )
    crop = _manual_surface_crop(main_window, Image.new("RGB", (1936, 1048), "white"))
    assert crop.size == (566, 786)
    assert _manual_surface_crop(popup(), patterned_image()).size == (400, 500)


def test_embedded_popup_waits_for_visual_transition_before_next_contact():
    clock = Clock()
    current = patterned_image()
    changed = current.copy()
    ImageDraw.Draw(changed).rectangle((15, 15, 370, 470), fill="#222222")
    frames = iter((current, changed))
    manual_window = PopupWindow(
        **{
            **popup().__dict__,
            "bounds": (0, 0, 400, 500),
            "source": "main_window_visual_surface",
        }
    )
    smoke = ManualPopupSmoke(
        capture=lambda _window: next(frames, changed),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    smoke._wait_until_closed(
        manual_window, 1, _manual_surface_crop(manual_window, current)
    )
    assert clock.value == 0.5


def test_manual_mode_never_calls_foreground_api(monkeypatch):
    import pyweixin.visual_navigation as navigation

    monkeypatch.setattr(
        navigation,
        "foreground_wechat_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreground API called")
        ),
    )
    clock = Clock()
    windows = iter(([popup()], []))
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, []),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 4),
        extract=lambda _image, verified: PopupOcr(
            verified, 8, True, True, "wx***23", 0.99, "CONFIRMED"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        report = smoke.run(max_contacts=1, wait_seconds=1)[0]
    assert report["status"] == "CONFIRMED"


def test_manual_mode_never_clicks_or_sends_keyboard_input(monkeypatch):
    calls = []
    for module_name, names in {
        "pyautogui": ("click", "press", "write", "hotkey"),
        "win32api": ("SetCursorPos", "mouse_event", "keybd_event"),
        "win32gui": ("SetForegroundWindow", "BringWindowToTop"),
    }.items():
        stub = types.ModuleType(module_name)
        for name in names:
            setattr(stub, name, lambda *args, _name=name, **kwargs: calls.append(_name))
        monkeypatch.setitem(sys.modules, module_name, stub)

    clock = Clock()
    windows = iter(([popup()], []))
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, []),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 3),
        extract=lambda _image, verified: PopupOcr(
            verified, 4, False, False, "", None, "WECHAT_ID_NOT_VISIBLE"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        smoke.run(max_contacts=1, wait_seconds=1)
    assert calls == []


def test_detected_verified_popup_runs_shared_ocr_callback_once():
    clock = Clock()
    windows = iter(([popup()], []))
    calls = []
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, []),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 5),
        extract=lambda image, verified: (
            calls.append((image, verified))
            or PopupOcr(True, 6, True, True, "wx***23", 0.99, "CONFIRMED")
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        report = smoke.run(max_contacts=1, wait_seconds=1)
    assert len(calls) == 1 and calls[0][1] is True
    assert report[0]["profile_status"] == PROFILE_POPUP_VERIFIED


def test_timeout_returns_manual_popup_not_detected():
    clock = Clock()
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: [],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        report = smoke.run(max_contacts=1, wait_seconds=1)
    assert report[0]["status"] == MANUAL_PROFILE_POPUP_NOT_DETECTED
    assert report[0]["popup_detected"] is False


def test_capture_failure_is_distinct_and_does_not_run_ocr():
    clock = Clock()
    windows = iter(([popup()],))
    ocr_calls = []
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, [popup()]),
        capture=lambda _window: (_ for _ in ()).throw(OSError("private detail")),
        extract=lambda *_args: ocr_calls.append(True),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        report = smoke.run(max_contacts=1, wait_seconds=1)
    assert report[0]["status"] == POPUP_CAPTURE_UNAVAILABLE
    assert ocr_calls == []


def test_confirmed_id_is_masked_in_output_only():
    clock = Clock()
    windows = iter(([popup()], []))
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, []),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 5),
        extract=lambda *_args: PopupOcr(
            True, 5, True, True, "abc***89", 0.99, "CONFIRMED"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        smoke.run(max_contacts=1, wait_seconds=1)
    assert "abc***89" in output.getvalue()
    assert "abc123xyz89" not in output.getvalue()
    assert '"candidate_found":true' in output.getvalue()


def test_label_missing_is_reported_without_relabeling_as_ocr_failed(monkeypatch):
    fake_result = types.SimpleNamespace(
        view_diagnostics=[{"has_wechat_label": False, "detected_box_count": 2}] * 2,
        ocr_candidates=[None, None],
        value=None,
        confidence=None,
        status="OCR_LABEL_NOT_FOUND",
    )
    fake_package = types.ModuleType("wechat_ocr")
    fake_package.DEFAULT_CONFIG = object()
    fake_package.WeChatIdExtractor = lambda **_kwargs: types.SimpleNamespace(
        extract=lambda _image: fake_result
    )
    fake_masking = types.ModuleType("wechat_ocr.masking")
    fake_masking.mask_wechat_id = lambda _value: ""
    monkeypatch.setitem(sys.modules, "wechat_ocr", fake_package)
    monkeypatch.setitem(sys.modules, "wechat_ocr.masking", fake_masking)
    result = extract_popup_ocr(patterned_image(), profile_verified=True)
    assert result.status == "OCR_LABEL_NOT_FOUND"
    assert not result.wechat_label_found and not result.candidate_found


def test_review_candidates_are_masked_before_report(monkeypatch):
    fake_result = types.SimpleNamespace(
        view_diagnostics=[{"has_wechat_label": True, "detected_box_count": 3}] * 2,
        ocr_candidates=["abC123", "wxid90123"],
        value=None,
        confidence=0.97,
        status="NEED_REVIEW",
        reason="view_disagreement",
    )
    fake_package = types.ModuleType("wechat_ocr")
    fake_package.DEFAULT_CONFIG = object()
    fake_package.WeChatIdExtractor = lambda **_kwargs: types.SimpleNamespace(
        extract=lambda _image: fake_result
    )
    fake_masking = types.ModuleType("wechat_ocr.masking")
    fake_masking.mask_wechat_id = lambda value: f"{value[:2]}***{value[-2:]}"
    monkeypatch.setitem(sys.modules, "wechat_ocr", fake_package)
    monkeypatch.setitem(sys.modules, "wechat_ocr.masking", fake_masking)
    result = extract_popup_ocr(patterned_image(), profile_verified=True)
    assert result.candidate_masked == "ab***23 | wx***23"
    assert result.reason == "view_disagreement"


def test_model_runtime_exception_is_classified_as_ocr_failed(monkeypatch):
    fake_package = types.ModuleType("wechat_ocr")
    fake_package.DEFAULT_CONFIG = object()
    fake_package.WeChatIdExtractor = lambda **_kwargs: types.SimpleNamespace(
        extract=lambda _image: (_ for _ in ()).throw(
            RuntimeError("private inference detail")
        )
    )
    fake_masking = types.ModuleType("wechat_ocr.masking")
    fake_masking.mask_wechat_id = lambda _value: ""
    monkeypatch.setitem(sys.modules, "wechat_ocr", fake_package)
    monkeypatch.setitem(sys.modules, "wechat_ocr.masking", fake_masking)
    clock = Clock()
    windows = iter(([popup()],))
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, [popup()]),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 2),
        extract=lambda _image, verified: extract_popup_ocr(_image, verified),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        report = smoke.run(max_contacts=1, wait_seconds=1)
    assert report[0]["status"] == "OCR_FAILED"
    assert "private inference detail" not in output.getvalue()


def test_manual_mode_does_not_save_screenshots(monkeypatch):
    def fail_save(*_args, **_kwargs):
        raise AssertionError("screenshot save attempted")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    clock = Clock()
    windows = iter(([popup()], []))
    smoke = ManualPopupSmoke(
        enumerate_popups=lambda: next(windows, []),
        capture=lambda _window: patterned_image(),
        verify_profile=lambda _image: (True, 2),
        extract=lambda _image, verified: PopupOcr(
            verified, 3, False, False, "", None, "WECHAT_ID_NOT_VISIBLE"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        smoke.run(max_contacts=1, wait_seconds=1)
