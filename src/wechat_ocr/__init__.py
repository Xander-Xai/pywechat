"""Shared WeChat ID OCR core; independent of WeChat automation packages."""

from .config import DEFAULT_CONFIG, WeChatIdOcrConfig
from .extractor import (
    WeChatIdExtractor,
    extract_wechat_id_from_uia,
    reconcile_ocr_result,
)
from .models import WeChatIdResult

__all__ = [
    "DEFAULT_CONFIG",
    "WeChatIdOcrConfig",
    "WeChatIdExtractor",
    "WeChatIdResult",
    "extract_wechat_id_from_uia",
    "reconcile_ocr_result",
]
