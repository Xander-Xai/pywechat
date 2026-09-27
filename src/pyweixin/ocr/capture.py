"""In-memory screenshots of a contact profile with a window-relative fallback."""

from __future__ import annotations

from PIL import ImageGrab

from .config import DEFAULT_CONFIG, WeChatIdOcrConfig
from .errors import OcrInferenceFailed
from .roi import fallback_profile_roi
from .wechat_id import WeChatIdExtractor
from .result import WeChatIdResult


def capture_profile_image(
    profile_window, main_window, config: WeChatIdOcrConfig | None = None
):
    """Capture the accessible profile rectangle or a relative main-window ROI."""
    cfg = config or DEFAULT_CONFIG
    try:
        rect = profile_window.rectangle()
        box = (rect.left, rect.top, rect.right, rect.bottom)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("empty profile rectangle")
    except Exception:
        try:
            box = fallback_profile_roi(main_window.rectangle(), cfg)
        except Exception as exc:
            raise OcrInferenceFailed("联系人资料 ROI 不可用", detail=str(exc)) from exc
        if box is None:
            raise OcrInferenceFailed("联系人资料 ROI 不可用")
    try:
        return ImageGrab.grab(bbox=box, all_screens=True)
    except Exception as exc:
        raise OcrInferenceFailed("联系人资料截图失败", detail=str(exc)) from exc


def extract_profile_wechat_id(
    profile_window, main_window, config: WeChatIdOcrConfig | None = None
) -> WeChatIdResult:
    """Run both OCR views against a profile screenshot; no UIA text is read here."""
    cfg = config or DEFAULT_CONFIG
    try:
        image = capture_profile_image(profile_window, main_window, cfg)
    except OcrInferenceFailed as exc:
        return WeChatIdResult(
            status="OCR_FAILED", reason=exc.code, diagnostics=["OCR_FAILED"]
        )
    return WeChatIdExtractor(config=cfg).extract(image)
