import hashlib
import json

import pytest
from PIL import Image, ImageDraw

import contact_exporter.fixed_geometry_export as exporter
from contact_exporter.fixed_geometry_export import (
    _adapt_scroll_extra,
    _adjacent_overlap_count,
    _advance_sequence_after_transition,
    _build_viewport_scan_plan,
    _checkpoint_snapshot,
    _confirmed_target_reached,
    _confirmed_value,
    _ContactExtractionState,
    _evaluate_20_preflight,
    _forward_progress_action,
    _list_view_changed,
    _mark_viewport_advance,
    _normalized_point_in_bounds,
    _profile_changed_from_last_terminal,
    _RecognitionDecision,
    _recognize_consensus,
    _recognize_with_retries,
    _record_scroll_progress,
    _refresh_accounting,
    _register_profile_hash,
    _resume_profile_action,
    _same_current_frame,
    _scroll_escalation,
    _terminal_result,
    _write_outputs,
)
from wechat_ocr.engine import OcrToken
from wechat_ocr.masking import contains_mask_placeholder


def _state():
    return {"profile_results": [], "confirmed": [], "review": []}


def test_benchmark_viewport_telemetry_records_geometry_and_safety_state(monkeypatch):
    monkeypatch.setattr(exporter, "BENCHMARK_MODE", True)
    state = {
        "viewport": 2,
        "rows_clicked": 6,
        "profile_revisits": 1,
        "unique_profiles_visited": 5,
        "navigation_telemetry": [],
    }

    exporter._append_benchmark_viewport_event(
        state,
        viewport_id=2,
        previous_viewport_id=1,
        viewport_hash="a" * 32,
        previous_viewport_hash="b" * 32,
        direction="FORWARD",
        alignment_confidence="HIGH",
        recovery_state="RUNNING",
        requested_units=10,
        anchor_displacement=-540,
        visible_rows=list(range(13)),
        row_spacing=54,
        row_signatures=[str(i) * 32 for i in range(13)],
        overlap_indices=[0, 1, 2],
        candidate_indices=list(range(3, 13)),
    )

    event = state["navigation_telemetry"][0]
    assert event["event_id"] == 1
    assert event["viewport_id"] == 2
    assert event["previous_viewport_id"] == 1
    assert event["estimated_rows_advanced"] == 10
    assert event["estimated_overlap_rows"] == 3
    assert event["overlap_rows"] == [0, 1, 2]
    assert event["overlap_ratio"] == pytest.approx(3 / 13, abs=1e-6)
    assert event["skipped_overlap_rows"] == [0, 1, 2]
    assert event["candidate_rows"] == list(range(3, 13))
    assert event["alignment_confidence"] == "HIGH"
    assert event["profile_clicks"] == 6
    assert event["profile_revisits"] == 1
    assert event["new_profiles"] == 5
    assert event["scroll_direction"] == "FORWARD"
    assert event["recovery_state"] == "RUNNING"


def test_terminal_result_is_the_only_way_profiles_processed_increases():
    state = _state()
    assert state.get("profiles_processed", 0) == 0
    _terminal_result(state, row_ordinal=1, status="FINAL_REVIEW", reason="VALUE_NOT_FOUND")
    assert state["profiles_processed"] == 1
    assert state["terminal_processed"] == 1
    assert state["result_accounting"]["unaccounted"] == 0


def test_failure_terminal_is_recorded_instead_of_silently_skipped():
    state = _state()
    _terminal_result(
        state, row_ordinal=2, status="FINAL_REVIEW", reason="LABEL_NOT_FOUND",
        candidate_masked="ab****yz", frame_hash="0123456789abcdef",
    )
    assert len(state["profile_results"]) == 1
    assert state["profile_results"][0]["accounting_reason"] == "label_not_found"
    assert state["profile_results"][0]["frame_hash_prefix"] == "0123456789ab"


def test_legacy_88_profile_counts_can_be_reconciled_when_duplicate_reason_is_known():
    state = _state()
    for index in range(17):
        _terminal_result(state, row_ordinal=index + 1, status="CONFIRMED", reason="CONFIRMED")
    for index in range(71):
        _terminal_result(
            state, row_ordinal=index + 18, status="DUPLICATE", reason="DUPLICATE_WECHAT_ID_ANOMALY"
        )
    accounting = _refresh_accounting(state)
    assert state["terminal_processed"] == 88
    assert accounting["confirmed_unique"] == 17
    assert accounting["confirmed_duplicate"] == 71
    assert accounting["unaccounted"] == 0


def test_current_matching_frame_is_accepted():
    assert _same_current_frame("frame-a", "frame-a", "frame-a") == (True, "")


def test_navigation_extraction_frame_mismatch_is_rejected():
    assert _same_current_frame("frame-a", "frame-b", "frame-a") == (False, "FRAME_MISMATCH")


def test_terminal_hash_match_cannot_be_frame_mismatch():
    state = _state()
    with pytest.raises(RuntimeError, match="STATE_INVARIANT_VIOLATION"):
        _terminal_result(
            state, row_ordinal=1, status="FINAL_REVIEW", reason="FRAME_MISMATCH",
            expected_frame_hash="same", extraction_frame_hash="same",
        )


def test_retry_mismatch_then_matching_low_confidence_uses_final_attempt():
    state = _ContactExtractionState(sequence_index=1)
    state.record_attempt(
        attempt_index=1, expected_hash="old", extraction_hash="other",
        hash_match=False, status="FRAME_MISMATCH",
    )
    state.record_attempt(
        attempt_index=2, expected_hash="current", extraction_hash="current",
        hash_match=True, status="LOW_CONFIDENCE",
    )
    assert state.terminal_status("FRAME_MISMATCH") == "LOW_CONFIDENCE"
    assert state.transient_frame_error is None


def test_retry_mismatch_then_matching_confirmation_uses_final_attempt():
    state = _ContactExtractionState(sequence_index=2)
    state.record_attempt(
        attempt_index=1, expected_hash="old", extraction_hash="other",
        hash_match=False, status="FRAME_MISMATCH",
    )
    state.record_attempt(
        attempt_index=2, expected_hash="current", extraction_hash="current",
        hash_match=True, status="CONFIRMED_BY_CONSENSUS",
    )
    assert state.terminal_status("FRAME_MISMATCH") == "CONFIRMED_BY_CONSENSUS"
    assert state.transient_frame_error is None


def test_attempt_mismatch_can_be_nonterminal_and_contacts_get_fresh_state():
    first = _ContactExtractionState(sequence_index=1)
    first.record_attempt(
        attempt_index=1, expected_hash="old", extraction_hash="new",
        hash_match=False, status="FRAME_MISMATCH",
    )
    first.record_attempt(
        attempt_index=2, expected_hash="new", extraction_hash="new",
        hash_match=True, status="CONFIRMED_BY_CONSENSUS",
    )
    second = _ContactExtractionState(sequence_index=2)
    assert first is not second
    assert second.transient_frame_error is None
    assert second.attempt_results == []


def test_profile_unchanged_is_a_navigation_reason():
    state = _state()
    _terminal_result(state, row_ordinal=1, status="FINAL_REVIEW", reason="PROFILE_NOT_CHANGED")
    assert state["profile_results"][0]["accounting_reason"] == "profile_not_changed"
    assert state["result_accounting"]["frame_mismatch"] == 0


def test_profile_unchanged_does_not_advance_sequence_index():
    state = {"row_ordinal": 7}
    changed = _advance_sequence_after_transition(
        state, {"from_sequence": 7, "to_sequence": 8, "profile_changed": False}
    )
    assert not changed
    assert state["row_ordinal"] == 7
    assert not _profile_changed_from_last_terminal("same", "same")
    assert _profile_changed_from_last_terminal("before", "after")


def test_viewport_scroll_marks_old_profile_to_be_skipped():
    state = {"next_action": "PROCESS_CURRENT"}
    assert _mark_viewport_advance(state)
    assert state["next_action"] == "NAVIGATE_NEXT"


def test_20_person_gate_requires_exactly_19_successful_transitions():
    state = _state()
    state.update({
        "profiles_visited": 20,
        "profiles_changed": 18,
        "frame_matches": 20,
        "frame_mismatches": 0,
        "frame_mismatch_attempts": 2,
        "transition_audit": [
            {"from_sequence": i, "to_sequence": i + 1, "profile_changed": True}
            for i in range(1, 19)
        ],
    })
    for index in range(20):
        _terminal_result(
            state, row_ordinal=index + 1, status="CONFIRMED_BY_CONSENSUS",
            reason="CONFIRMED_BY_CONSENSUS",
        )
    passed, details = _evaluate_20_preflight(state)
    assert not passed
    assert details["transition_count"] == 18
    assert details["successful_transitions"] == 18


def test_stale_frame_does_not_pass_as_current_profile():
    assert _same_current_frame("previous", "previous", "current") == (
        True,
        "PROFILE_NOT_CHANGED",
    )


def test_duplicate_id_anomaly_is_a_terminal_bucket():
    state = _state()
    _terminal_result(
        state, row_ordinal=9, status="DUPLICATE_WECHAT_ID_ANOMALY",
        reason="DUPLICATE_WECHAT_ID_ANOMALY", candidate_masked="ab******yz",
    )
    assert state["result_accounting"]["confirmed_duplicate"] == 1
    assert state["result_accounting"]["unaccounted"] == 0
    assert state["profile_results"][0]["candidate_masked"] == "ab******yz"


def test_all_required_review_reasons_are_explicit_accounting_categories():
    cases = {
        "LABEL_NOT_FOUND": "label_not_found",
        "VALUE_NOT_FOUND": "value_not_found",
        "LOW_CONFIDENCE": "low_confidence",
        "OCR_DISAGREEMENT": "ocr_disagreement",
        "OCR_FAILED": "ocr_failed",
        "FRAME_MISMATCH": "frame_mismatch",
        "PROFILE_NOT_CHANGED": "profile_not_changed",
        "ROW_CLICK_FAILED": "row_click_failed",
        "INVALID_CANDIDATE": "invalid_candidate",
        "PARSER_REJECTED": "parser_rejected",
    }
    state = _state()
    for index, (reason, key) in enumerate(cases.items(), 1):
        _terminal_result(state, row_ordinal=index, status="FINAL_REVIEW", reason=reason)
        assert state["profile_results"][-1]["accounting_reason"] == key
    accounting = _refresh_accounting(state)
    assert accounting["unaccounted"] == 0
    assert (
        accounting["confirmed_records"]
        + accounting["duplicate_records"]
        + accounting["review_records"]
        + accounting["extraction_failures"]
        + accounting["navigation_failures"]
        + accounting["explicit_other_terminal_records"]
    ) == len(cases)


class _SequenceEngine:
    def __init__(self, texts, confidence=0.999):
        self.texts = list(texts)
        self.confidence = confidence
        self.calls = 0

    def recognize(self, _image):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        if not text:
            return []
        return [OcrToken(text=text, confidence=self.confidence, box=((1, 1), (80, 1), (80, 14), (1, 14)))]


def test_two_of_three_recognition_views_can_confirm_a_valid_candidate():
    header = _SequenceEngine(["微信号："])
    value = _SequenceEngine(["wechatid123", "wechatid123", "differentid"])
    decision = _recognize_consensus(
        Image.new("RGB", (600, 200), "white"), header, value
    )
    assert decision.raw_candidate == "wechatid123"
    assert decision.status == "CONFIRMED_BY_CONSENSUS"
    assert float(decision.confidence) >= 0.99


def test_low_confidence_consensus_is_review_not_confirmation():
    header = _SequenceEngine(["微信号："])
    value = _SequenceEngine(["wechatid123"] * 3, confidence=0.80)
    decision = _recognize_consensus(Image.new("RGB", (600, 200), "white"), header, value)
    assert decision.raw_candidate == "wechatid123"
    assert decision.status == "LOW_CONFIDENCE"
    assert float(decision.confidence) == 0.8
    assert decision.candidate_masked == exporter._mask("wechatid123")
    assert _confirmed_value(decision) is None


def test_local_retry_stops_after_first_success(monkeypatch):
    import contact_exporter.fixed_geometry_export as exporter

    calls = []

    def recognize(_header, _engine, recognition_engine=None):
        calls.append(recognition_engine)
        return _RecognitionDecision("wechatid123", "CONFIRMED_BY_CONSENSUS", "0.99900")

    monkeypatch.setattr(exporter, "_recognize_consensus", recognize)
    decision, attempts = _recognize_with_retries(
        Image.new("RGB", (200, 100), "white"), object(), object()
    )
    assert decision.raw_candidate == "wechatid123"
    assert decision.status == "CONFIRMED_BY_CONSENSUS"
    assert attempts == 1
    assert len(calls) == 1


def test_local_retry_is_bounded_to_three_passes(monkeypatch):
    import contact_exporter.fixed_geometry_export as exporter

    calls = []

    def recognize(_header, _engine, recognition_engine=None):
        calls.append(1)
        return _RecognitionDecision(None, "VALUE_NOT_FOUND")

    monkeypatch.setattr(exporter, "_recognize_consensus", recognize)
    decision, attempts = _recognize_with_retries(
        Image.new("RGB", (200, 100), "white"), object(), object()
    )
    assert decision.raw_candidate is None
    assert decision.status == "VALUE_NOT_FOUND"
    assert attempts == 3
    assert len(calls) == 3


def test_validator_and_consensus_receive_raw_candidate_before_display_mask(monkeypatch):
    received = []
    consensus_values = []
    mask_calls = []
    original_validate = exporter.validate_wechat_id
    original_mask = exporter._mask
    original_counter = exporter.Counter

    def validate(value, config=None):
        received.append(value)
        return original_validate(value, config)

    class SpyCounter(original_counter):
        def __init__(self, values=()):
            values = list(values)
            consensus_values.extend(values)
            super().__init__(values)

    def mask(value):
        mask_calls.append(value)
        return original_mask(value)

    monkeypatch.setattr(exporter, "validate_wechat_id", validate)
    monkeypatch.setattr(exporter, "Counter", SpyCounter)
    monkeypatch.setattr(exporter, "_mask", mask)
    header = _SequenceEngine(["微信号："])
    value = _SequenceEngine(["abcdef123"] * 3, confidence=0.80)
    decision = _recognize_consensus(Image.new("RGB", (600, 200), "white"), header, value)
    assert decision.raw_candidate == "abcdef123"
    assert decision.status == "LOW_CONFIDENCE"
    assert decision.confidence == "0.80000"
    assert received and all(value == "abcdef123" for value in received)
    assert consensus_values == ["abcdef123", "abcdef123", "abcdef123"]
    assert mask_calls == ["abcdef123"]
    assert all(not contains_mask_placeholder(value) for value in received)


def test_mask_placeholder_source_is_explicitly_reviewed_never_confirmed(monkeypatch):
    validation_inputs = []
    original_validate = exporter.validate_wechat_id
    def validate(value, config=None):
        validation_inputs.append(value)
        return original_validate(value, config)
    monkeypatch.setattr(exporter, "validate_wechat_id", validate)
    header = _SequenceEngine(["微信号："])
    value = _SequenceEngine(["ab***23"] * 3)
    decision = _recognize_consensus(Image.new("RGB", (600, 200), "white"), header, value)
    assert decision.raw_candidate == "ab***23"
    assert decision.status == "SOURCE_VALUE_MASKED"
    assert decision.candidate_masked == "ab***23"
    assert _confirmed_value(decision) is None
    assert all(not contains_mask_placeholder(value) for value in validation_inputs)

    state = _state()
    _terminal_result(
        state,
        row_ordinal=1,
        status="FINAL_REVIEW",
        reason=decision.status,
        candidate_masked=decision.candidate_masked,
    )
    accounting = state["result_accounting"]
    assert accounting["source_value_masked"] == 1
    assert accounting["review_records"] == 1
    assert accounting["unaccounted"] == 0


def test_mask_detector_does_not_reject_valid_separator_characters():
    for value in ("abc_def", "abc-def", "ab.cdef"):
        assert not contains_mask_placeholder(value)
    for value in ("ab*cd", "ab•cd", "ab·cd", "ab●cd", "ab○cd", "ab…cd", "ab⋯cd", "ab...cd"):
        assert contains_mask_placeholder(value)


def test_confirmed_csv_and_txt_keep_full_valid_id(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "OUT", tmp_path)
    state = _state()
    state["confirmed"] = [{
        "row_ordinal": 1,
        "wechat_id": "abcdef123",
        "status": "CONFIRMED_BY_CONSENSUS",
        "source": "test",
        "confidence": "0.99900",
    }]
    _write_outputs(state)
    csv_text = (tmp_path / "wechat_contacts_150.csv").read_text(encoding="utf-8-sig")
    txt_text = (tmp_path / "wechat_contacts_150.txt").read_text(encoding="utf-8-sig")
    assert "abcdef123" in csv_text
    assert txt_text.strip() == "abcdef123"


def test_review_csv_and_checkpoint_store_only_masked_display_values(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "OUT", tmp_path)
    raw = "abcdef123"
    masked = exporter._mask(raw)
    state = _state()
    state["review"] = [{
        "sequence_index": 1,
        "viewport": 1,
        "row_ordinal": 1,
        "status": "FINAL_REVIEW",
        "reason": "LOW_CONFIDENCE",
        "candidate_masked": masked,
        "confidence": "0.80000",
        "frame_hash_prefix": "0123456789ab",
    }]
    state["profile_results"] = [{
        "sequence_index": 1,
        "row_ordinal": 1,
        "status": "FINAL_REVIEW",
        "reason": "LOW_CONFIDENCE",
        "accounting_reason": "low_confidence",
        "candidate_masked": masked,
        "confidence": "0.80000",
        "frame_hash_prefix": "0123456789ab",
        "attempt_count": 1,
    }]
    _write_outputs(state)
    review = (tmp_path / "wechat_contacts_150_review.csv").read_text(encoding="utf-8-sig")
    snapshot = json.dumps(_checkpoint_snapshot({
        **state,
        "confirmed": [{"wechat_id": raw, "status": "CONFIRMED_BY_CONSENSUS"}],
        "confirmed_frame_hashes": {raw: "frame-hash"},
    }))
    assert masked in review
    assert raw not in review
    assert masked in snapshot
    assert raw not in snapshot


def test_masked_terminal_does_not_satisfy_target_and_next_contact_can_confirm():
    target = 1
    confirmed = []
    decisions = [
        _RecognitionDecision("ab***23", "SOURCE_VALUE_MASKED", "", "ab***23"),
        _RecognitionDecision("abcdef123", "CONFIRMED_BY_CONSENSUS", "0.99900"),
    ]
    processed = 0
    while len(confirmed) < target:
        decision = decisions[processed]
        processed += 1
        value = _confirmed_value(decision)
        if value:
            confirmed.append(value)
    assert processed == 2
    assert confirmed == ["abcdef123"]


def test_20_person_gate_allows_source_masked_review_without_ocr_failure():
    state = _state()
    state.update({
        "profiles_visited": 20,
        "profiles_changed": 19,
        "frame_matches": 20,
        "frame_mismatches": 0,
        "frame_mismatch_attempts": 2,
        "transition_audit": [
            {"from_sequence": i, "to_sequence": i + 1, "profile_changed": True}
            for i in range(1, 20)
        ],
    })
    for index in range(19):
        _terminal_result(
            state,
            row_ordinal=index + 1,
            status="CONFIRMED_BY_CONSENSUS",
            reason="CONFIRMED_BY_CONSENSUS",
        )
    _terminal_result(
        state,
        row_ordinal=20,
        status="FINAL_REVIEW",
        reason="SOURCE_VALUE_MASKED",
        candidate_masked="ab***23",
    )
    state["confirmed"] = [{"wechat_id": "abcdef123"}]
    state["review"] = [{"reason": "source_ui_masked", "candidate_masked": "ab***23"}]
    passed, details = _evaluate_20_preflight(state)
    assert passed
    assert details["source_value_masked"] == 1
    assert details["terminal_frame_mismatch_contacts"] == 0
    assert details["frame_mismatch_attempts"] == 2
    assert details["ocr_failed"] == 0
    assert details["unaccounted"] == 0
    assert details["silent_skips"] == 0


def test_20_person_gate_blocks_frame_mismatch_bucket_even_if_frame_counters_match():
    state = _state()
    state.update({
        "profiles_visited": 20,
        "profiles_changed": 19,
        "frame_matches": 20,
        "frame_mismatches": 0,
        "transition_audit": [
            {"from_sequence": i, "to_sequence": i + 1, "profile_changed": True}
            for i in range(1, 20)
        ],
    })
    for index in range(19):
        _terminal_result(
            state,
            row_ordinal=index + 1,
            status="CONFIRMED_BY_CONSENSUS",
            reason="CONFIRMED_BY_CONSENSUS",
        )
    _terminal_result(
        state,
        row_ordinal=20,
        status="FINAL_REVIEW",
        reason="FRAME_MISMATCH",
    )
    state["confirmed"] = [{"wechat_id": "abcdef123"}]
    passed, details = _evaluate_20_preflight(state)
    assert not passed
    assert details["frame_mismatch_results"] == 1
    assert details["navigation_failures"] == 1


def test_profile_revisit_is_hash_only_and_does_not_change_terminal_or_review():
    state = {"seen_profile_hashes": ["hash-a"], "terminal_processed": 1, "review": []}
    assert _register_profile_hash(state, "hash-a") == "PROFILE_REVISIT"
    assert state["profile_revisits"] == 1
    assert state["terminal_processed"] == 1
    assert state["review"] == []
    assert state["seen_profile_hashes"] == ["hash-a"]


def test_scroll_overlap_sequence_skips_only_old_hashes():
    state = {"seen_profile_hashes": []}
    outcomes = [_register_profile_hash(state, value) for value in "ABCCDE"]
    assert outcomes == ["NEW_PROFILE", "NEW_PROFILE", "NEW_PROFILE", "PROFILE_REVISIT", "NEW_PROFILE", "NEW_PROFILE"]
    assert state["unique_profiles_visited"] == 5
    assert state["profile_revisits"] == 1


def test_four_consecutive_revisits_force_bounded_scroll_and_new_profile_resets():
    state = {"consecutive_profile_revisits": 0, "forward_progress_recoveries": 0}
    for _ in range(4):
        state["consecutive_profile_revisits"] += 1
    assert _forward_progress_action(state) == "CONTINUE"
    assert state.get("force_forward_scroll", False) is False
    assert state.get("forward_progress_recoveries", 0) == 0
    assert _register_profile_hash(state, "new") == "NEW_PROFILE"
    assert state["consecutive_profile_revisits"] == 0


def test_repeated_list_view_is_input_failure():
    state = {"unique_profiles_visited": 5, "last_scroll_unique_profiles": 5}
    assert _record_scroll_progress(state, "A") == "SCROLL_DIRECTION_UNRESOLVED"
    assert _record_scroll_progress(state, "B") == "SCROLL_DIRECTION_UNRESOLVED"
    assert _record_scroll_progress(state, "A", list_changed=False) == "SCROLL_INPUT_FAILED"
    assert state["scroll_input_failure_count"] == 1
    assert state.get("forward_progress_stalls", 0) == 0


def test_wheel_without_list_hash_change_is_scroll_input_failure():
    assert not _list_view_changed("same", "same")
    assert _list_view_changed("before", "after")
    assert _scroll_escalation(3) == (3, 5, 7)
    assert _scroll_escalation(7) == (7, 9, 11)


def test_list_and_row_hash_ignore_green_selected_background():
    plain = Image.new("RGB", (1000, 1000), "white")
    selected = plain.copy()
    draw_plain = ImageDraw.Draw(plain)
    draw_selected = ImageDraw.Draw(selected)
    for index in range(13):
        y = 210 + index * 55
        draw_plain.rectangle((62, y + 18, 120, y + 21), fill="black")
        draw_selected.rectangle((62, y + 18, 120, y + 21), fill="black")
    draw_selected.rectangle((31, 210 + 5 * 55, 158, 210 + 6 * 55), fill=(100, 210, 120))
    draw_selected.rectangle((62, 210 + 5 * 55 + 18, 120, 210 + 5 * 55 + 21), fill="black")
    assert exporter._contact_list_hash(plain) == exporter._contact_list_hash(selected)
    assert exporter._row_signature(plain, 210 + 5 * 55 + 24, 55) == exporter._row_signature(
        selected, 210 + 5 * 55 + 24, 55
    )
    hovered = plain.copy()
    hover_draw = ImageDraw.Draw(hovered)
    hover_draw.rectangle((31, 210 + 2 * 55, 158, 210 + 3 * 55), fill=(225, 225, 225))
    hover_draw.rectangle((62, 210 + 2 * 55 + 18, 120, 210 + 2 * 55 + 21), fill="black")
    assert exporter._row_signature(plain, 210 + 2 * 55 + 24, 55) == exporter._row_signature(
        hovered, 210 + 2 * 55 + 24, 55
    )


def test_list_view_hash_changes_when_visible_rows_move():
    first = Image.new("RGB", (1000, 1000), "white")
    shifted = Image.new("RGB", (1000, 1000), "white")
    draw_first = ImageDraw.Draw(first)
    draw_shifted = ImageDraw.Draw(shifted)
    for index in range(13):
        y = 210 + index * 55
        draw_first.rectangle((50, y + 10, 90 + index, y + 14), fill="black")
        next_y = y - 55
        if next_y >= 200:
            draw_shifted.rectangle((50, next_y + 10, 90 + index, next_y + 14), fill="black")
    assert exporter._list_view_changed(
        exporter._contact_list_hash(first), exporter._contact_list_hash(shifted)
    )


def test_atomic_checkpoint_replace_retries_transient_windows_lock(tmp_path, monkeypatch):
    real_replace = exporter.os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("transient file lock")
        return real_replace(source, target)

    monkeypatch.setattr(exporter.os, "replace", flaky_replace)
    target = tmp_path / "checkpoint.json"
    exporter._atomic_json(target, {"confirmed_count": 11})
    assert calls == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"confirmed_count": 11}


def test_adjacent_viewport_signatures_only_skip_contiguous_overlap():
    previous = list("ABCDEFGHIJKLM")
    current = list("HIJKLMNOPQRST")
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    previous_hashes_for_rows = [digest(value) for value in previous]
    current_hashes_for_rows = [digest(value) for value in current]
    assert _adjacent_overlap_count(previous_hashes_for_rows, current_hashes_for_rows) == 6
    assert current[6:] == list("NOPQRST")
    previous_hashes = [digest(str(index)) for index in range(13)]
    current_hashes = []
    for value in range(7, 20):
        signature = digest(str(value))
        if value < 13:
            signature = f"{int(signature, 16) ^ 0b10101:032x}"
        current_hashes.append(signature)
    # Any uncertain visual difference leaves the candidate clickable.
    assert _adjacent_overlap_count(previous_hashes, current_hashes) == 0
    assert _adjacent_overlap_count(["0" * 32], ["0" * 31 + "1"]) == 0


def test_viewport_plan_skips_six_exact_overlap_rows_and_scans_new_rows():
    rows = [float(i * 54) for i in range(13)]
    previous = list("ABCDEFGHIJKLM")
    current = list("HIJKLMNOPQRST")
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    plan = _build_viewport_scan_plan(
        2, [digest(x) for x in previous], [digest(x) for x in current], rows,
        last_processed_anchor=rows[-1],
    )
    assert plan.overlap_rows == tuple(range(6))
    assert plan.candidate_new_rows == tuple(range(6, 13))
    assert plan.confidence == "HIGH"
    assert plan.last_processed_anchor == rows[-1]


def test_viewport_plan_handles_one_exact_overlap_row():
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    plan = _build_viewport_scan_plan(
        2, [digest(x) for x in "ABCDEFG"], [digest(x) for x in "GHIJKLM"],
        [float(i) for i in range(7)],
    )
    assert plan.overlap_rows == (0,)
    assert plan.candidate_new_rows == tuple(range(1, 7))


def test_similar_new_row_is_not_skipped_and_uncertain_plan_scans_all():
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    old = digest("old-row")
    near = f"{int(old, 16) ^ 0b11:032x}"
    plan = _build_viewport_scan_plan(
        2, [digest("x"), old], [near, digest("new")], [10.0, 20.0],
    )
    assert plan.confidence == "UNCERTAIN"
    assert plan.overlap_rows == ()
    assert plan.candidate_new_rows == (0, 1)


def test_viewport_plan_non_forward_direction_fails_safe_to_scan_all():
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()[:32]
    signatures = [digest(x) for x in "ABCDEFG"]
    plan = _build_viewport_scan_plan(
        2, signatures, signatures, [float(i) for i in range(7)],
        scroll_direction="UPWARD",
    )
    assert plan.confidence == "UNCERTAIN"
    assert plan.candidate_new_rows == tuple(range(7))


@pytest.mark.parametrize(
    ("current", "overlap", "visible", "expected"),
    [(0, 9, 13, 1), (1, 9, 13, 2), (2, 1, 13, 2), (0, 0, 13, -1)],
)
def test_scroll_step_adapts_only_at_overlap_extremes(current, overlap, visible, expected):
    assert _adapt_scroll_extra(current, overlap, visible) == expected


def test_old_checkpoint_gets_metrics_and_viewport_cursor_defaults():
    state = {
        "rows_clicked": 4,
        "profile_revisits": 2,
        "unique_profiles_visited": 3,
        "extractions_attempted": 3,
        "successful_scroll_count": 2,
        "viewport": 3,
        "forward_progress_recoveries": 1,
        "adjacent_overlap_rows_skipped": 5,
        "row_candidates_seen": 4,
    }
    exporter._ensure_navigation_state(state)
    metrics = exporter._update_navigation_metrics(state)
    assert metrics["revisit_per_unique"] == pytest.approx(2 / 3)
    assert metrics["ocr_calls"] == 3
    assert metrics["row_candidates_skipped_as_overlap"] == 5
    assert state["viewport_cursor"] == {
        "last_processed_row": None,
        "overlap_boundary": None,
        "next_candidate_row": None,
    }


def test_profile_revisit_does_not_trigger_forward_failure():
    state = {"seen_profile_hashes": ["old"], "consecutive_profile_revisits": 9,
             "forward_progress_recoveries": 3}
    assert _register_profile_hash(state, "old") == "PROFILE_REVISIT"
    assert _forward_progress_action(state) == "CONTINUE"
    assert state.get("forward_progress_stalls", 0) == 0


def test_distinct_hashes_without_direction_evidence_do_not_count_as_forward():
    state = {"unique_profiles_visited": 11, "last_scroll_unique_profiles": 11}
    assert _record_scroll_progress(state, "viewport-a") == "SCROLL_DIRECTION_UNRESOLVED"
    assert _record_scroll_progress(state, "viewport-b") == "SCROLL_DIRECTION_UNRESOLVED"
    assert _record_scroll_progress(state, "viewport-c") == "SCROLL_DIRECTION_UNRESOLVED"
    assert state.get("forward_viewports_checked", 0) == 0
    assert state.get("forward_progress_stalls", 0) == 0


def test_same_viewport_is_input_failure_not_forward_stall():
    state = {"unique_profiles_visited": 11, "last_scroll_unique_profiles": 11}
    assert _record_scroll_progress(state, "same", direction="FORWARD") == "CONTINUE"
    assert _record_scroll_progress(
        state, "same", direction="NO_MOVEMENT", list_changed=False
    ) == "SCROLL_INPUT_FAILED"
    assert state.get("forward_progress_stalls", 0) == 0


def test_resume_reanchors_at_current_unseen_or_seen_profile_without_reset():
    assert _resume_profile_action(["seen-a", "seen-b"], "new-c") == "PROCESS_CURRENT"
    assert _resume_profile_action(["seen-a", "seen-b"], "seen-b") == "NAVIGATE_NEXT"


def test_navigation_coordinate_bounds_reject_stale_geometry_y():
    assert _normalized_point_in_bounds(100, 600, 880, 640)
    assert not _normalized_point_in_bounds(100, 665, 880, 640)


def test_revisits_before_unseen_profile_do_not_stall():
    state = {"seen_profile_hashes": list("ABCDE"), "unique_profiles_visited": 5}
    for profile in "ABCD":
        assert _register_profile_hash(state, profile) == "PROFILE_REVISIT"
        assert _forward_progress_action(state) == "CONTINUE"
    assert _register_profile_hash(state, "F") == "NEW_PROFILE"
    assert state["unique_profiles_visited"] == 6
    assert state.get("forward_progress_stalls", 0) == 0


def test_confirmed_target_requires_unique_ids_and_reaches_150():
    state = {"confirmed": [{"wechat_id": f"id-{index}"} for index in range(150)]}
    assert _confirmed_target_reached(state, 150)
    state["confirmed"].append({"wechat_id": "id-0"})
    assert _confirmed_target_reached(state, 150)
    assert not _confirmed_target_reached({"confirmed": [{"wechat_id": "same"}] * 150}, 150)
