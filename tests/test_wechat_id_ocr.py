from __future__ import annotations

import pytest
from PIL import Image

from pyweixin.ocr import capture
from pyweixin.ocr.config import WeChatIdOcrConfig
from pyweixin.ocr.engine import (
    OcrEngine,
    OcrToken,
    _build_params,
    get_ocr_engine,
    reset_ocr_engine_cache,
)
from pyweixin.ocr.errors import OcrModelLoadFailed
from pyweixin.ocr.masking import mask_wechat_id
from pyweixin.ocr.result import WeChatIdResult
from pyweixin.ocr.roi import fallback_profile_roi
from pyweixin.ocr.validator import normalize_candidate, validate_wechat_id
from pyweixin.ocr.wechat_id import (
    WeChatIdExtractor,
    candidate_near_label,
    extract_wechat_id_from_uia,
    reconcile_ocr_result,
)
from wechat_ocr.extractor import WeChatIdExtractor as SharedWeChatIdExtractor
from wechat_ocr.masking import contains_mask_placeholder


SYNTHETIC_WECHAT_ID = "wx" + "id_" + "fixture" + "123456"


def token(text, x, y=10, confidence=0.99, width=60, height=16):
    return OcrToken(
        text,
        confidence,
        ((x, y), (x + width, y), (x + width, y + height), (x, y + height)),
    )


def good_view(value="wechat123", score=0.99):
    return [
        token("微信号：", 0, width=58),
        token(value, 65, confidence=score, width=85),
    ]


class FakeEngine:
    def __init__(self, views):
        self.views, self.calls = list(views), 0

    def recognize(self, _image):
        value = self.views[self.calls]
        self.calls += 1
        return value


def extractor(views, confidence=0.95):
    return WeChatIdExtractor(
        FakeEngine(views), WeChatIdOcrConfig(min_confidence_confirm=confidence)
    )


def test_spatial_anchor_uses_nearest_right_value_not_other_id():
    tokens = [
        token("昵称", 0),
        token("random123", 100, 60),
        token("微信号：", 0, 100, width=58),
        token(SYNTHETIC_WECHAT_ID, 65, 100, width=100),
        token("phone123", 300, 100),
    ]
    assert candidate_near_label(tokens)[0] == SYNTHETIC_WECHAT_ID


def test_supported_ids_and_ambiguous_glyphs_are_never_rewritten():
    for value in (
        "wechat123",
        SYNTHETIC_WECHAT_ID,
        "abc-def",
        "abc_def",
        "a0O1Il",
        "test-S5",
    ):
        assert validate_wechat_id(value).valid
        assert normalize_candidate(value) == value


def test_high_confidence_two_view_consensus_confirms():
    result = extractor([good_view(), good_view()]).extract(Image.new("RGB", (200, 60)))
    assert (result.value, result.status, result.source) == (
        "wechat123",
        "CONFIRMED",
        "ocr",
    )


def test_ocr_disagreement_is_review_even_if_uia_agrees_with_one():
    result = extractor([good_view("abcO123"), good_view("abc0123")]).extract(
        Image.new("RGB", (200, 60))
    )
    result = reconcile_ocr_result(result, "abc0123")
    assert result.status == "NEED_REVIEW" and result.reason == "view_disagreement"


def test_low_confidence_matching_uia_confirms():
    result = extractor([good_view(score=0.83), good_view(score=0.86)]).extract(
        Image.new("RGB", (200, 60))
    )
    result = reconcile_ocr_result(result, "wechat123")
    assert result.status == "CONFIRMED" and result.source == "ocr+uia"


def test_low_confidence_mismatching_uia_needs_review():
    result = extractor([good_view(score=0.83), good_view(score=0.86)]).extract(
        Image.new("RGB", (200, 60))
    )
    result = reconcile_ocr_result(result, "wechat124")
    assert result.status == "NEED_REVIEW" and result.reason == "uia_mismatch"


def test_high_confidence_mismatching_uia_still_needs_review():
    result = extractor([good_view(), good_view()]).extract(Image.new("RGB", (200, 60)))
    result = reconcile_ocr_result(result, "wechat124")
    assert result.status == "NEED_REVIEW" and result.reason == "uia_mismatch"


def test_ocr_absent_uia_only_needs_review_and_empty_is_not_found():
    absent = WeChatIdResult(ocr_candidates=[None, None])
    assert reconcile_ocr_result(absent, "wechat123").status == "NEED_REVIEW"
    assert reconcile_ocr_result(absent, None).status == "NOT_FOUND"


def test_label_without_value_and_multiple_ids_never_guess():
    assert candidate_near_label([token("微信号：", 0, width=58)]) == (None, None)
    tokens = [
        token("微信号：", 0, width=58),
        token("first123", 65),
        token("other123", 65),
    ]
    assert candidate_near_label(tokens) == (None, None)


def test_label_normalization_preserves_merged_id_suffix():
    assert (
        candidate_near_label([token("微信号：abc_123", 0, width=120)])[0] == "abc_123"
    )


def test_shared_extractor_returns_explicit_masked_source_review():
    masked = "ab***23"
    result = SharedWeChatIdExtractor(
        FakeEngine([good_view(masked), good_view(masked)]),
        WeChatIdOcrConfig(min_confidence_confirm=0.99),
    ).extract(Image.new("RGB", (200, 60)))
    assert result.status == "SOURCE_VALUE_MASKED"
    assert result.reason == "source_ui_masked"
    assert result.value is None
    assert result.candidate_masked == mask_wechat_id(masked)
    assert not result.is_confirmed
    assert contains_mask_placeholder(masked)
    assert (
        candidate_near_label([token("微 信 号 : abc_123", 0, width=145)])[0]
        == "abc_123"
    )


def test_label_value_on_next_visual_line_requires_nearby_alignment():
    close = [
        token("微信号：", 20, y=10, width=58),
        token("abc_123", 28, y=32, width=70),
    ]
    far = [
        token("微信号：", 20, y=10, width=58),
        token("abc_123", 220, y=32, width=70),
    ]
    assert candidate_near_label(close)[0] == "abc_123"
    assert candidate_near_label(far)[0] is None


def test_label_without_candidate_and_invalid_candidate_are_distinguished():
    missing = extractor([[token("微信号：", 0, width=58)]] * 2).extract(
        Image.new("RGB", (200, 60))
    )
    assert missing.reason == "OCR_LABEL_FOUND_VALUE_NOT_FOUND"
    invalid = extractor(
        [[token("微信号：", 0, width=58), token("bad@id", 65)]] * 2
    ).extract(Image.new("RGB", (200, 60)))
    assert invalid.reason == "OCR_VALUE_INVALID"


def test_other_profile_fields_without_wechat_label_are_not_ocr_engine_failures():
    result = extractor([[token("昵称", 0), token("地区", 80)]] * 2).extract(
        Image.new("RGB", (200, 60))
    )
    assert result.status == "WECHAT_ID_NOT_VISIBLE"


def test_profile_roi_stability_timeout_is_specific():
    from pyweixin.visual_navigation import PROFILE_NOT_STABLE, Win32VisualNavigation

    nav = object.__new__(Win32VisualNavigation)
    nav.hwnd = 1
    nav._screenshot = lambda _hwnd: Image.new("RGB", (100, 100), (0, 0, 0))
    nav._wait = lambda _seconds: None
    # Deterministic timeout without wall-clock waiting.
    import pyweixin.visual_navigation as module

    ticks = iter([0.0, 0.0, 6.0])
    monkeypatch_time = module.time.perf_counter
    module.time.perf_counter = lambda: next(ticks)
    try:
        with pytest.raises(Exception) as exc:
            nav.wait_profile_stable()
        assert getattr(exc.value, "status", None) == PROFILE_NOT_STABLE
    finally:
        module.time.perf_counter = monkeypatch_time


def test_profile_roi_stability_returns_after_two_stable_deltas():
    from pyweixin.visual_navigation import Win32VisualNavigation

    nav = object.__new__(Win32VisualNavigation)
    nav.hwnd = 1
    nav._screenshot = lambda _hwnd: Image.new("RGB", (120, 100), (50, 50, 50))
    nav._wait = lambda _seconds: None
    stable, latency = nav.wait_profile_stable()
    assert stable.size == (120, 100)
    assert latency >= 0


def test_diagnostic_tokens_never_log_merged_wechat_id(monkeypatch):
    from wechat_ocr import extractor as extractor_module

    class Stub:
        def recognize(self, _image):
            return [token("微信号：sensitive_id123", 0, width=190)]

    monkeypatch.setattr(extractor_module, "get_ocr_engine", lambda _config: Stub())
    monkeypatch.setenv("WECHAT_OCR_DIAGNOSTIC", "true")
    report = extractor_module.diagnose_profile_rois(
        {"original": Image.new("RGB", (120, 60))}
    )
    tokens = report["original"]["scales"]["2"]["tokens"]
    assert tokens[0]["token_type"] == "SYSTEM_LABEL"
    assert tokens[0]["safe_text"] == "微信号"
    assert all("sensitive_id123" not in item["safe_text"] for item in tokens)


def test_uia_helper_is_label_anchored():
    assert (
        extract_wechat_id_from_uia(["other123", "微信号：", SYNTHETIC_WECHAT_ID])
        == SYNTHETIC_WECHAT_ID
    )
    assert extract_wechat_id_from_uia(["other123"]) is None


def test_engine_cache_builds_once_per_config(monkeypatch):
    from pyweixin.ocr import engine

    reset_ocr_engine_cache()
    made = []

    class Stub:
        def __init__(self, config):
            made.append(config)

    monkeypatch.setattr(engine, "OcrEngine", Stub)
    cfg = WeChatIdOcrConfig()
    assert get_ocr_engine(cfg) is get_ocr_engine(cfg)
    assert len(made) == 1
    reset_ocr_engine_cache()


def test_rapidocr_output_malformed_is_explicit_failure():
    class Broken:
        txts = ["id"]
        scores = []
        boxes = []

    try:
        OcrEngine._to_tokens(Broken())
    except Exception as exc:
        assert getattr(exc, "code", None) == "OCR_INFERENCE_FAILED"
    else:
        raise AssertionError("malformed result must fail closed")


def test_rapidocr_partial_empty_output_is_not_silently_accepted():
    class Broken:
        txts = None
        scores = [0.9]
        boxes = []

    try:
        OcrEngine._to_tokens(Broken())
    except Exception as exc:
        assert getattr(exc, "code", None) == "OCR_INFERENCE_FAILED"
    else:
        raise AssertionError("partial result must fail closed")


def test_model_load_error_has_explicit_ocr_failed_status(monkeypatch):
    from pyweixin.ocr import wechat_id

    def fail(_config):
        raise OcrModelLoadFailed("load failed")

    monkeypatch.setattr(wechat_id, "get_ocr_engine", fail)
    result = WeChatIdExtractor().extract(Image.new("RGB", (120, 40)))
    assert result.status == "OCR_FAILED"
    assert result.reason == "OCR_MODEL_LOAD_FAILED"


def test_preprocessing_produces_two_non_destructive_views():
    from pyweixin.ocr.preprocess import recognition_views

    source = Image.new("RGB", (40, 20), (120, 120, 120))
    first, second = recognition_views(source)
    assert first.size == second.size == (80, 40)
    assert first.mode == "RGB" and second.mode == "L"


def test_roi_geometry_and_model_load_errors_are_fail_closed():
    failed = WeChatIdResult(status="OCR_FAILED", reason="OCR_MODEL_LOAD_FAILED")
    assert reconcile_ocr_result(failed, "wechat123").status == "OCR_FAILED"
    # Typical 100%, 125%, 150%, windowed and maximised rectangles all keep
    # normalized relative coordinates identical by construction.
    for left, top, w, h in (
        (0, 0, 1200, 800),
        (120, 80, 1500, 1000),
        (0, 0, 2880, 1800),
    ):

        class Rect:
            pass

        rect = Rect()
        rect.left = left
        rect.top = top
        rect.right = left + w
        rect.bottom = top + h
        roi = fallback_profile_roi(rect)
        assert roi is not None
        assert roi[0] >= left and roi[2] <= left + w and roi[3] <= top + h

    class Empty:
        left = 0
        top = 0
        right = 0
        bottom = 0

    assert fallback_profile_roi(Empty()) is None


def test_profile_capture_uses_accessible_rectangle(monkeypatch):
    class Rect:
        left, top, right, bottom = 10, 20, 310, 220

    class Window:
        def rectangle(self):
            return Rect()

    seen = {}

    def fake_grab(**kwargs):
        seen.update(kwargs)
        return Image.new("RGB", (1, 1))

    monkeypatch.setattr(capture.ImageGrab, "grab", fake_grab)
    capture.capture_profile_image(Window(), Window())
    assert seen["bbox"] == (10, 20, 310, 220)
    assert seen["all_screens"] is True


def test_default_model_is_small_detector_and_medium_recognizer():
    from pyweixin.ocr.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.det_model_type == "small"
    assert DEFAULT_CONFIG.rec_model_type == "medium"
    params = _build_params(DEFAULT_CONFIG)
    assert params["Det.model_type"].value == "small"
    assert params["Rec.model_type"].value == "medium"


def test_medium_detector_requires_explicit_configuration():
    config = WeChatIdOcrConfig(det_model_type="medium")
    params = _build_params(config)
    assert params["Det.model_type"].value == "medium"
    assert params["Rec.model_type"].value == "medium"


def test_wechat_id_masking_never_emits_the_full_identifier():
    assert mask_wechat_id("wechat123") == "we***23"
    assert mask_wechat_id("abc") == "***"
    assert mask_wechat_id(None) == ""
