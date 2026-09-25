"""Spatially anchored, OCR-primary WeChat ID extraction."""

from __future__ import annotations

import os
import re
import time
from typing import Iterable, Sequence

from .config import DEFAULT_CONFIG, WeChatIdOcrConfig
from .engine import OcrToken, get_ocr_engine
from .errors import OcrError
from .preprocess import recognition_views
from .models import WeChatIdResult
from .validator import normalize_candidate, risky_characters, validate_wechat_id
from .masking import mask_wechat_id

_LABEL = re.compile(
    r"^(?:微\s*信\s*号|微信\s*ID|WeChat\s*ID|Weixin\s*ID)\s*([:：])?\s*(.*)$", re.I
)


def _label_token(text: str) -> tuple[bool, str]:
    # Whitespace normalization applies only to the fixed UI label. Candidate
    # characters in the original token are preserved for strict validation.
    match = _LABEL.match(text.strip())
    if not match:
        return False, ""
    separator, suffix = match.groups()
    # A merged label/value token is accepted only with an explicit colon.
    return True, suffix if separator else ""


def label_diagnostics(tokens: Sequence[OcrToken]) -> dict[str, object]:
    """Return PII-safe extraction evidence; never includes token text."""
    label_tokens = []
    approximate = False
    for token in tokens:
        found, _ = _label_token(token.text)
        if found:
            label_tokens.append(token)
        elif "微信" in token.text and any(ch in token.text for ch in "号昵地备"):
            approximate = True
    candidate, score = candidate_near_label(tokens)
    return {
        "detected_box_count": len(tokens),
        "recognized_token_count": sum(bool(token.text.strip()) for token in tokens),
        "has_wechat_label": bool(label_tokens),
        "has_approximate_label": approximate,
        "candidate_found": candidate is not None,
        "candidate_valid": bool(candidate and validate_wechat_id(candidate).valid),
        "candidate_confidence": score,
        "failure_stage": (
            "OCR_LABEL_NOT_FOUND"
            if not label_tokens
            else "OCR_LABEL_FOUND_VALUE_NOT_FOUND"
            if not candidate
            else "OCR_VALUE_INVALID"
            if not validate_wechat_id(candidate).valid
            else "OCR_CANDIDATE_FOUND"
        ),
    }


def diagnose_profile_rois(rois: dict[str, object]) -> dict[str, object]:
    """Run the finite PII-safe 1x/2x/3x two-view profile benchmark."""
    engine = get_ocr_engine(DEFAULT_CONFIG)
    report: dict[str, object] = {}
    started = time.perf_counter()
    for roi_name, image in rois.items():
        scale_report = {}
        for scale in (1, 2, 3):
            pair = []
            token_log = []
            scale_started = time.perf_counter()
            for view_index, view in enumerate(recognition_views(image, scale)):
                tokens = engine.recognize(view)
                evidence = label_diagnostics(tokens)
                pair.append(evidence)
                if os.getenv("WECHAT_OCR_DIAGNOSTIC", "false").casefold() == "true":
                    for token in tokens:
                        normalized = token.text.strip()
                        label, _ = _label_token(normalized)
                        valid = validate_wechat_id(normalized).valid
                        if label:
                            token_type, safe_text = "SYSTEM_LABEL", "微信号"
                        elif normalized in {"昵称", "地区", "备注", "电话"}:
                            token_type, safe_text = "SYSTEM_LABEL", normalized
                        elif valid:
                            token_type, safe_text = (
                                "POTENTIAL_ID",
                                mask_wechat_id(normalized),
                            )
                        else:
                            token_type, safe_text = "OTHER_REDACTED", "<REDACTED>"
                        token_log.append(
                            {
                                "view": view_index,
                                "token_type": token_type,
                                "safe_text": safe_text,
                                "bbox": [
                                    round(token.left, 1),
                                    round(token.top, 1),
                                    round(token.right, 1),
                                    round(token.bottom, 1),
                                ],
                                "confidence": round(token.confidence, 4),
                            }
                        )
            scale_report[str(scale)] = {
                "detected_box_count": [item["detected_box_count"] for item in pair],
                "recognized_token_count": [
                    item["recognized_token_count"] for item in pair
                ],
                "has_wechat_label": [item["has_wechat_label"] for item in pair],
                "has_approximate_label": [
                    item["has_approximate_label"] for item in pair
                ],
                "candidate_found": [item["candidate_found"] for item in pair],
                "candidate_valid": [item["candidate_valid"] for item in pair],
                "confidence": [item["candidate_confidence"] for item in pair],
                "latency_seconds": round(time.perf_counter() - scale_started, 3),
                "tokens": token_log,
            }
        report[roi_name] = {
            "width": image.width,
            "height": image.height,
            "scales": scale_report,
        }
    report["total_latency_seconds"] = round(time.perf_counter() - started, 3)
    return report


def candidate_near_label(
    tokens: Sequence[OcrToken], config: WeChatIdOcrConfig | None = None
) -> tuple[str | None, float | None]:
    """Choose only the nearest same-line right token, or a nearby next-line token."""
    cfg = config or DEFAULT_CONFIG
    choices: list[tuple[int, float, str, float]] = []
    for label in tokens:
        is_label, inline = _label_token(label.text)
        if not is_label:
            continue
        if inline:
            value = normalize_candidate(inline, cfg.labels)
            if value:
                choices.append((0, 0.0, value, label.confidence))
        for value_token in tokens:
            if value_token is label:
                continue
            value = normalize_candidate(value_token.text, cfg.labels)
            if not value:
                continue
            vertical = abs(value_token.center_y - label.center_y)
            if (
                value_token.left >= label.right
                and vertical
                <= max(label.height, value_token.height) * cfg.line_tolerance_ratio
            ):
                gap = value_token.left - label.right
                if gap <= label.height * cfg.max_horizontal_gap_ratio:
                    choices.append(
                        (0, gap, value, min(label.confidence, value_token.confidence))
                    )
            elif value_token.top >= label.bottom:
                gap = value_token.top - label.bottom
                if (
                    gap <= label.height * cfg.max_line_gap_ratio
                    and abs(value_token.left - label.left) <= label.width * 1.5
                ):
                    choices.append(
                        (1, gap, value, min(label.confidence, value_token.confidence))
                    )
    if not choices:
        return None, None
    choices.sort(key=lambda item: (item[0], item[1]))
    # Multiple equally placed values are ambiguous; never concatenate them.
    best = choices[0]
    tied = [item for item in choices if item[:2] == best[:2]]
    if len({item[2] for item in tied}) > 1:
        return None, None
    return best[2], best[3]


def extract_wechat_id_from_uia(
    texts: Iterable[str], config: WeChatIdOcrConfig | None = None
) -> str | None:
    """Read a UIA label/value pair for secondary verification only."""
    values = list(texts)
    cfg = config or DEFAULT_CONFIG
    for index, text in enumerate(values):
        is_label, inline = _label_token(str(text))
        if is_label:
            candidate = inline or (
                str(values[index + 1]).strip() if index + 1 < len(values) else ""
            )
            candidate = normalize_candidate(candidate, cfg.labels)
            if validate_wechat_id(candidate, cfg).valid:
                return candidate
    return None


class WeChatIdExtractor:
    def __init__(self, engine=None, config: WeChatIdOcrConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self._engine = engine

    def extract(self, image, *, ui_candidate: str | None = None) -> WeChatIdResult:
        if not self.config.ocr_enabled or not self.config.ocr_primary:
            return WeChatIdResult(status="OCR_FAILED", reason="ocr_primary_disabled")
        candidates: list[str | None] = []
        scores: list[float | None] = []
        view_diagnostics: list[dict[str, object]] = []
        recognized_views: list[list[OcrToken]] = []
        try:
            engine = self._engine or get_ocr_engine(self.config)
            for view in recognition_views(image):
                tokens = engine.recognize(view)
                recognized_views.append(tokens)
                evidence = label_diagnostics(tokens)
                value, score = candidate_near_label(tokens, self.config)
                if value and not validate_wechat_id(value, self.config).valid:
                    value = None
                candidates.append(value)
                scores.append(score)
                view_diagnostics.append(evidence)
        except OcrError as exc:
            return WeChatIdResult(
                status="OCR_FAILED",
                source="none",
                ui_candidate=ui_candidate,
                reason=exc.code,
                diagnostics=["OCR_FAILED"],
            )
        except Exception:
            return WeChatIdResult(
                status="OCR_FAILED",
                source="none",
                ui_candidate=ui_candidate,
                reason="OCR_INFERENCE_FAILED",
                diagnostics=["OCR_FAILED"],
            )
        result = reconcile_ocr_result(
            WeChatIdResult(
                ocr_candidates=candidates,
                confidence=min(
                    (score for score in scores if score is not None), default=None
                ),
                view_diagnostics=view_diagnostics,
                diagnostics=[str(item["failure_stage"]) for item in view_diagnostics],
            ),
            ui_candidate,
            self.config,
        )
        if result.status == "NOT_FOUND":
            labels = [bool(item["has_wechat_label"]) for item in view_diagnostics]
            any_text = any(
                int(item["recognized_token_count"]) for item in view_diagnostics
            )
            other_profile_labels = any(
                any(label in token.text for label in ("昵称", "地区", "备注", "电话"))
                for tokens in recognized_views
                for token in tokens
            )
            raw_candidates = [
                candidate_near_label(tokens, self.config)[0]
                for tokens in recognized_views
            ]
            if all(labels) and any(
                value and not validate_wechat_id(value, self.config).valid
                for value in raw_candidates
            ):
                result.reason = "OCR_VALUE_INVALID"
                result.status = "OCR_VALUE_INVALID"
            elif all(labels):
                result.reason = "OCR_LABEL_FOUND_VALUE_NOT_FOUND"
                result.status = "OCR_LABEL_FOUND_VALUE_NOT_FOUND"
            elif not any_text:
                result.reason = "OCR_NO_TEXT"
                result.status = "OCR_NO_TEXT"
            elif other_profile_labels:
                result.reason = "WECHAT_ID_NOT_VISIBLE"
                result.status = "WECHAT_ID_NOT_VISIBLE"
            else:
                result.reason = "OCR_LABEL_NOT_FOUND"
                result.status = "OCR_LABEL_NOT_FOUND"
        elif result.reason == "view_disagreement":
            result.diagnostics.append("OCR_VIEW_DISAGREEMENT")
        elif result.reason == "uia_mismatch":
            result.diagnostics.append("OCR_UIA_DISAGREEMENT")
        return result


def reconcile_ocr_result(
    ocr_result: WeChatIdResult,
    ui_candidate: str | None,
    config: WeChatIdOcrConfig | None = None,
) -> WeChatIdResult:
    """Apply UIA only after the OCR-first result has been computed."""
    cfg = config or DEFAULT_CONFIG
    if ocr_result.status == "OCR_FAILED":
        return WeChatIdResult(
            status="OCR_FAILED",
            source="uia" if ui_candidate else "none",
            ui_candidate=ui_candidate,
            reason=ocr_result.reason,
            diagnostics=["OCR_FAILED"],
        )
    candidates = ocr_result.ocr_candidates
    if len(candidates) != 2:
        return WeChatIdResult(
            status="OCR_FAILED",
            source="none",
            reason="OCR_INFERENCE_FAILED",
            diagnostics=["OCR_FAILED"],
        )
    scores = [ocr_result.confidence]
    if not any(candidates):
        if ui_candidate:
            return WeChatIdResult(
                value=ui_candidate,
                status="NEED_REVIEW",
                source="uia",
                ui_candidate=ui_candidate,
                reason="uia_only",
                diagnostics=["UIA_CANDIDATE", "NEED_REVIEW"],
            )
        return WeChatIdResult(
            status=ocr_result.status,
            reason=ocr_result.reason or "label_or_value_not_found",
            diagnostics=list(ocr_result.diagnostics),
            view_diagnostics=list(ocr_result.view_diagnostics),
        )
    if candidates[0] != candidates[1]:
        return WeChatIdResult(
            value=None,
            status="NEED_REVIEW",
            source="ocr+uia" if ui_candidate else "ocr",
            confidence=None,
            ocr_candidates=candidates,
            ui_candidate=ui_candidate,
            reason="view_disagreement",
            diagnostics=["OCR_DISAGREEMENT", "NEED_REVIEW"],
            view_diagnostics=list(ocr_result.view_diagnostics),
        )
    value = candidates[0]
    assert value is not None
    confidence = min((score for score in scores if score is not None), default=None)
    if ui_candidate and ui_candidate != value:
        return WeChatIdResult(
            value=None,
            status="NEED_REVIEW",
            source="ocr+uia",
            confidence=confidence,
            ocr_candidates=candidates,
            ui_candidate=ui_candidate,
            reason="uia_mismatch",
            diagnostics=[
                "CROSS_CHECK_MISMATCH",
                "OCR_UIA_DISAGREEMENT",
                "NEED_REVIEW",
            ],
            view_diagnostics=list(ocr_result.view_diagnostics),
        )
    if confidence is not None and confidence >= cfg.min_confidence_confirm:
        return WeChatIdResult(
            value=value,
            status="CONFIRMED",
            source="ocr+uia" if ui_candidate else "ocr",
            confidence=confidence,
            ocr_candidates=candidates,
            ui_candidate=ui_candidate,
            risky_chars=risky_characters(value),
            reason="multi_view_consensus",
            diagnostics=["CROSS_CHECK_MATCH"] if ui_candidate else ["OCR_CANDIDATE"],
        )
    if confidence is not None and ui_candidate == value:
        return WeChatIdResult(
            value=value,
            status="CONFIRMED",
            source="ocr+uia",
            confidence=confidence,
            ocr_candidates=candidates,
            ui_candidate=ui_candidate,
            risky_chars=risky_characters(value),
            reason="low_confidence_uia_match",
            diagnostics=["CROSS_CHECK_MATCH"],
        )
    return WeChatIdResult(
        value=None,
        status="NEED_REVIEW",
        source="ocr",
        confidence=confidence,
        ocr_candidates=candidates,
        ui_candidate=ui_candidate,
        reason="low_confidence",
        diagnostics=["NEED_REVIEW"],
    )
