"""pywechat-specific UI adapter for the shared WeChat ID OCR core.

Imports of the Python 3.10+ shared package are lazy so legacy pywechat
installations fail closed without breaking package import.
"""


def inspect_profile(profile_window, main_window):
    """Capture and run OCR first, then use UIA only as a secondary check."""
    try:
        from PIL import ImageGrab
        from wechat_ocr import DEFAULT_CONFIG, WeChatIdExtractor
        from wechat_ocr.extractor import (
            extract_wechat_id_from_uia,
            reconcile_ocr_result,
        )
        from wechat_ocr.models import WeChatIdResult
        from wechat_ocr.roi import fallback_profile_roi

        try:
            rect = profile_window.rectangle()
            box = (rect.left, rect.top, rect.right, rect.bottom)
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("empty profile rectangle")
        except Exception:
            rect = main_window.rectangle()
            box = fallback_profile_roi(rect)
            if box is None:
                raise ValueError("profile ROI unavailable")
        image = ImageGrab.grab(bbox=box, all_screens=True)
        result = WeChatIdExtractor(config=DEFAULT_CONFIG).extract(image)
        controls = profile_window.descendants(control_type="Text")
        texts = [element.window_text() for element in controls]
        ui_candidate = extract_wechat_id_from_uia(texts)
        result = reconcile_ocr_result(result, ui_candidate)
        return result, controls
    except Exception as exc:
        try:
            from wechat_ocr.models import WeChatIdResult

            return WeChatIdResult(status="OCR_FAILED", reason=type(exc).__name__), []
        except Exception:
            return _UnavailableResult(type(exc).__name__), []


class _UnavailableResult:
    status = "OCR_FAILED"
    value = None
    is_confirmed = False

    def __init__(self, reason):
        self.reason = reason
