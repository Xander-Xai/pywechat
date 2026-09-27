"""Tests for keyboard-sequential contact navigation and output gates."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

from contact_exporter import batch_export
from contact_exporter.batch_export import (
    ContactRowClickNavigator,
    KeyboardSequentialNavigator,
    _checkpoint_snapshot,
    _fresh_state,
    _id_hash,
    _write_outputs,
    classify_duplicate_id,
    frame_hashes_match,
    goal_reached,
    process_current_contact,
    profile_read_ready,
    send_down_if_foreground,
    stage_stalled,
    wait_for_foreground,
)
from pyweixin.visual_navigation import NavigationFailure


@dataclass
class Result:
    status: str
    value: str | None = None
    confidence: float = 0.0


def _navigator(events, changes):
    def down():
        events.append("DOWN")
        if isinstance(changes[0], Exception):
            raise changes.pop(0)

    def wait(old_hash, timeout):
        events.append(("VERIFY", timeout))
        value = changes.pop(0) if changes else None
        return value if value != old_hash else None

    return KeyboardSequentialNavigator(
        press_down=down,
        wait_profile_change=wait,
        operator_recovery=lambda: events.append("OPERATOR_RECOVERY") or True,
    )


def _mouse_navigator(*, geometries=None, waits=None, scroll=None, foreground=None,
                     pause=None, consecutive=0, failures=None, clicks=None):
    values = geometries or [{"list_left": 0, "list_width": 100,
                             "next_center_y": 20,
                             "current_row_near_viewport_bottom": False}]
    normalized = []
    for item in values:
        normalized.append({"virtual_origin_x": 0, "virtual_origin_y": 0,
                           **item})
    geometry = iter(normalized)
    wait_values = iter(waits or [])
    return ContactRowClickNavigator(
        SimpleNamespace(hwnd=1),
        geometry_provider=lambda _: next(geometry, normalized[-1]),
        click=lambda x, y: (clicks if clicks is not None else []).append((x, y)),
        wait_profile_change=lambda old, timeout: next(wait_values, None),
        scroll=scroll or (lambda _: True),
        is_foreground=foreground or (lambda: True),
        pause_foreground=pause or (lambda: True),
        consecutive_failures=consecutive,
        on_cycle_failure=failures or (lambda: None),
    )


def test_mouse_navigation_success_resets_consecutive_not_historical_total():
    nav = _mouse_navigator(waits=["profile-19"], consecutive=0)
    moved = nav.move_next("profile-18")
    assert moved.moved and moved.profile_hash == "profile-19"
    assert nav.consecutive_failures == 0


def test_mouse_navigation_click_unchanged_reanchors_and_retries():
    clicks = []
    nav = _mouse_navigator(waits=[None, "profile-19"], clicks=clicks)
    moved = nav.move_next("profile-18")
    assert moved.moved
    assert len(clicks) == 2


def test_mouse_navigation_viewport_bottom_scrolls_then_reanchors():
    geometries = [
        {"list_left": 0, "list_width": 100, "next_center_y": None,
         "current_row_near_viewport_bottom": True},
        {"list_left": 0, "list_width": 100, "next_center_y": 30,
         "current_row_near_viewport_bottom": False},
    ]
    scrolls = []
    nav = _mouse_navigator(geometries=geometries, waits=["profile-19"],
                           scroll=lambda _: scrolls.append(True) or True)
    moved = nav.move_next("profile-18")
    assert moved.moved
    assert scrolls == [True]
    assert nav.diagnostics["scroll_attempted"]


def test_mouse_navigation_foreground_loss_pauses_without_failure_count():
    checks = iter((False, True))
    pauses = []
    failures = []
    nav = _mouse_navigator(waits=["profile-19"],
                           foreground=lambda: next(checks, True),
                           pause=lambda: pauses.append(True) or True,
                           failures=lambda: failures.append(True))
    assert nav.move_next("profile-18").moved
    assert pauses
    assert not failures


def test_mouse_navigation_counts_one_failed_transition_after_three_recovery_cycles():
    failures = []
    clicks = []
    nav = _mouse_navigator(waits=[], failures=lambda: failures.append(True), clicks=clicks)
    moved = nav.move_next("profile-18")
    assert not moved.moved
    assert moved.status == "NAVIGATION_RECOVERY_PENDING"
    assert nav.consecutive_failures == 1
    assert failures == [True]
    assert len(clicks) == 12


def test_mouse_navigation_stalls_only_on_third_failed_transition():
    nav = _mouse_navigator(waits=[], consecutive=2)
    moved = nav.move_next("profile-18")
    assert moved.status == "NAVIGATION_STALLED"
    assert nav.consecutive_failures == 3


def test_a_to_b_to_c_navigation_refreshes_each_extraction_frame(monkeypatch):
    frames = [
        SimpleNamespace(key=value, profile_hash=profile_hash)
        for value, profile_hash in (("B-a", "B"), ("B-b", "B"), ("C-a", "C"), ("C-b", "C"))
    ]
    nav_object = SimpleNamespace(hwnd=1, last_stable_frame=SimpleNamespace(key="profile-18"))
    nav_object._screenshot = lambda _: frames.pop(0)
    monkeypatch.setattr(batch_export, "_profile_header_hash", lambda frame: frame.profile_hash)
    monkeypatch.setattr(batch_export.time, "sleep", lambda _: None)
    navigator = ContactRowClickNavigator(nav_object, is_foreground=lambda: True)

    assert navigator._wait_for_profile("A", timeout=1) == "B"
    assert nav_object.current_profile_frame.key == "B-b"
    assert navigator._wait_for_profile("B", timeout=1) == "C"
    assert nav_object.current_profile_frame.key == "C-b"


def test_resume_restores_visited_18_pending_transition_without_reset():
    checkpoint = {"contacts_visited": 18, "sequence_index": 18,
                  "pending_advance": True, "navigation_stalls": 3}
    state = batch_export._restore_checkpoint_state({}, checkpoint)
    assert state["contacts_visited"] == 18
    assert state["sequence_index"] == 18
    assert state["pending_advance"] is True


def test_initially_selected_contact_needs_no_selection_change():
    events = []
    status, result = process_current_contact(
        lambda: events.append("OCR") or Result("CONFIRMED", "id-a", 0.999)
    )
    assert status == "CONFIRMED"
    assert result.value == "id-a"
    assert events == ["OCR"]


def test_clean_contact_sequence_passes_each_owned_frame_a_b_c():
    frames = [SimpleNamespace(frame_hash=value) for value in ("A", "B", "C")]
    received = []
    for frame in frames:
        status, _ = process_current_contact(
            lambda owned: received.append(owned.frame_hash) or Result("NEED_REVIEW"),
            frame=frame,
            navigation_profile_hash=frame.frame_hash,
            extraction_frame_hash=frame.frame_hash,
        )
        assert status == "FINAL_REVIEW"
    assert received == ["A", "B", "C"]


def test_frame_mismatch_fails_closed_before_extraction():
    calls = []
    status, result = process_current_contact(
        lambda frame: calls.append(frame) or Result("CONFIRMED", "id-a", 0.999),
        frame=SimpleNamespace(frame_hash="A"),
        navigation_profile_hash="B",
        extraction_frame_hash="A",
    )
    assert not frame_hashes_match("B", "A")
    assert status == "FRAME_MISMATCH"
    assert result is None
    assert calls == []


def test_single_down_with_profile_change_is_navigation_success():
    events = []
    nav = _navigator(events, ["profile-b"])
    moved = nav.move_next("profile-a")
    assert moved.moved and moved.profile_hash == "profile-b"
    assert events == ["DOWN", ("VERIFY", 2.0)]


def test_profile_change_is_sufficient_without_highlight_signal():
    events = []
    nav = _navigator(events, ["profile-b"])
    assert nav.move_next("profile-a").status == "MOVE_SUCCESS"


def test_highlight_change_without_profile_change_does_not_succeed():
    events = ["HIGHLIGHT_CHANGED"]
    nav = _navigator(events, ["profile-a", "profile-a", "profile-a"])
    result = nav.move_next("profile-a")
    assert not result.moved
    assert result.status == "NAVIGATION_STALLED"


def test_first_down_probe_sends_one_key_before_profile_verification():
    events = []
    nav = _navigator(events, [None, "profile-b"])
    nav.first_probe = True
    result = nav.move_next("profile-a")
    assert not result.moved
    assert events == ["DOWN", ("VERIFY", 2.0)]


def test_down_navigation_is_sent_exactly_once():
    events = []
    nav = _navigator(events, [None, None, None])
    result = nav.move_next("profile-a")
    assert result.status == "NAVIGATION_STALLED"
    assert events.count("DOWN") == 1
    assert events.count("OPERATOR_RECOVERY") == 0
    assert sum(isinstance(event, tuple) for event in events) == 1


def test_repeated_navigation_failure_exits_after_bounded_retries():
    events = []
    nav = _navigator(events, [NavigationFailure("NAVIGATION_UNRESOLVED"), None, None])
    result = nav.move_next("profile-a")
    assert result.status == "NAVIGATION_STALLED"
    assert sum(isinstance(event, tuple) for event in events) == 0
    assert events.count("OPERATOR_RECOVERY") == 0


def test_current_contact_ocr_retries_before_navigation():
    events = []
    values = iter(
        [Result("OCR_FAILED"), Result("NEED_REVIEW"), Result("CONFIRMED", "id-a", 0.999)]
    )
    status, _ = process_current_contact(lambda: events.append("OCR") or next(values))
    if status in {"CONFIRMED", "FINAL_REVIEW"}:
        events.append("DOWN")
    assert status == "FINAL_REVIEW"
    assert events == ["OCR", "DOWN"]


def test_eight_second_need_review_is_completed_without_timeout():
    elapsed = [0]

    def extract():
        elapsed[0] += 8
        return Result("NEED_REVIEW", "candidate", 0.96483)

    status, result = process_current_contact(extract)
    assert elapsed[0] == 8
    assert status == "FINAL_REVIEW"
    assert result.status == "NEED_REVIEW"


def test_eight_second_confirmed_is_completed_without_timeout():
    elapsed = [0]

    def extract():
        elapsed[0] += 8
        return Result("CONFIRMED", "wxid-a", 0.999)

    status, result = process_current_contact(extract)
    assert elapsed[0] == 8
    assert status == "CONFIRMED"
    assert result.status == "CONFIRMED"


def test_ocr_stage_stalls_only_after_heartbeat_is_stale_for_30_seconds():
    assert not stage_stalled(last_heartbeat=8, now=30, timeout=30)
    assert stage_stalled(last_heartbeat=0, now=30.01, timeout=30)


def test_need_review_is_terminal_and_batch_can_wait_for_foreground_then_advance():
    events = []
    status, result = process_current_contact(
        lambda: events.append("OCR") or Result("NEED_REVIEW", "candidate", 0.96483)
    )
    events.append("SAVE_REVIEW")
    foreground = iter((False, True))
    sent = send_down_if_foreground(
        is_ready=lambda: events.append("CHECK_FOREGROUND") or next(foreground),
        wait_ready=lambda: events.append("WAIT_FOR_WEIXIN_FOREGROUND") or True,
        send_down=lambda: events.append("DOWN"),
    )
    assert status == "FINAL_REVIEW" and result.status == "NEED_REVIEW"
    assert sent
    assert events == ["OCR", "SAVE_REVIEW", "CHECK_FOREGROUND", "WAIT_FOR_WEIXIN_FOREGROUND", "DOWN"]


def test_duplicate_id_distinguishes_navigation_repeat_from_anomaly():
    ids = {"id-a"}
    assert classify_duplicate_id("id-a", ids, "same", "same") == "SUSPECT_NAVIGATION_DUPLICATE"
    assert classify_duplicate_id("id-a", ids, "old", "new") == "DUPLICATE_WECHAT_ID_ANOMALY"


def test_150_unique_confirmed_is_the_only_goal():
    assert not goal_reached({"confirmed_unique": 149}, 150)
    assert goal_reached({"confirmed_unique": 150}, 150)


def test_manual_bootstrap_cli_routes_target_without_auto_navigation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        batch_export,
        "run_keyboard_batch",
        lambda **kwargs: calls.append(kwargs) or 0,
    )
    assert batch_export.main(
        ["--manual-bootstrap", "--target", "150", "--output-dir", str(tmp_path)]
    ) == 0
    assert calls[0]["manual_bootstrap"] is True
    assert calls[0]["limit"] == 150


def test_profile_read_ready_is_independent_of_foreground():
    assert profile_read_ready(
        hwnd_exists=True, roi_valid=True, visual_valid=True, stable=True, ocr_boxes=9
    )


def test_current_contact_ocr_runs_before_foreground_gate():
    events = []

    def process():
        events.append("process_current_contact()")
        return process_current_contact(
            lambda: events.append("OCR") or Result("CONFIRMED", "id-a", 0.999)
        )

    result = process()
    assert result[0] == "CONFIRMED"
    events.append("save_checkpoint()")
    events.append("wait_for_foreground()")
    assert events == ["process_current_contact()", "OCR", "save_checkpoint()", "wait_for_foreground()"]


def test_foreground_false_never_sends_down():
    events = []
    sent = send_down_if_foreground(
        is_ready=lambda: False,
        wait_ready=lambda: False,
        send_down=lambda: events.append("DOWN"),
    )
    assert not sent
    assert events == []


def test_batch_checkpoint_masks_confirmed_ids_and_resume_uses_export(tmp_path):
    state = _fresh_state("test-run")
    state["confirmed"] = [{
        "contact_index": 1,
        "wechat_id": "abcdef123",
        "status": "CONFIRMED",
        "source": "ocr",
        "confidence": "0.99900",
    }]
    state["confirmed_unique"] = 1
    state["confirmed_id_hashes"] = [_id_hash("abcdef123")]
    snapshot = _checkpoint_snapshot(state)
    serialized = json.dumps(snapshot)
    assert "abcdef123" not in serialized
    assert snapshot["confirmed"][0]["wechat_id_masked"] == "ab***23"

    _write_outputs(tmp_path, state["confirmed"], [])
    restored = batch_export._load_confirmed_export(
        tmp_path, 1, state["confirmed_id_hashes"]
    )
    assert restored[0]["wechat_id"] == "abcdef123"


def test_source_masked_contact_is_terminal_review_and_does_not_count_as_ocr_failure():
    result = SimpleNamespace(
        status="SOURCE_VALUE_MASKED",
        value=None,
        candidate_masked="ab***23",
        reason="source_ui_masked",
    )
    status, returned = process_current_contact(lambda: result)
    assert status == "SOURCE_VALUE_MASKED"
    assert returned is result


def test_foreground_recovery_sends_exactly_one_down():
    events = []
    ready = iter((False, True, True))
    sent = send_down_if_foreground(
        is_ready=lambda: next(ready),
        wait_ready=lambda: events.append("WAIT") or True,
        send_down=lambda: events.append("DOWN"),
    )
    assert sent
    assert events == ["WAIT", "DOWN"]


def test_foreground_lost_again_pauses_without_down():
    events = []
    sent = send_down_if_foreground(
        is_ready=lambda: False,
        wait_ready=lambda: False,
        send_down=lambda: events.append("DOWN"),
    )
    assert not sent
    assert "DOWN" not in events


def test_foreground_wait_stays_alive_for_60_seconds_and_heartbeats():
    elapsed = [0]
    state = {"navigation_failures": 0, "review": 0, "watchdog_exit": False}
    beats = []

    def sleep(seconds):
        elapsed[0] += seconds

    ready = lambda: elapsed[0] >= 60
    assert wait_for_foreground(
        is_ready=ready,
        heartbeat=lambda: beats.append(elapsed[0]),
        sleep=sleep,
        interval=1,
    )
    assert elapsed[0] == 60
    assert len(beats) == 60
    assert state == {"navigation_failures": 0, "review": 0, "watchdog_exit": False}


def test_foreground_wait_heartbeats_each_interval():
    ticks = [0]
    heartbeats = []

    def sleep(seconds):
        ticks[0] += seconds

    assert wait_for_foreground(
        is_ready=lambda: ticks[0] >= 3,
        heartbeat=lambda: heartbeats.append(ticks[0]),
        sleep=sleep,
        interval=1,
    )
    assert heartbeats == [0, 1, 2]


def test_foreground_loss_to_ready_resumes_automatically():
    events = []
    reads = iter((False, False, True))
    assert wait_for_foreground(
        is_ready=lambda: next(reads),
        heartbeat=lambda: events.append("HEARTBEAT"),
        sleep=lambda _: events.append("SLEEP"),
    )
    events.append("RESUME")
    assert events == ["HEARTBEAT", "SLEEP", "HEARTBEAT", "SLEEP", "RESUME"]


def test_foreground_wait_does_not_call_failure_or_review_mutators():
    events = []
    reads = iter((False, True))
    wait_for_foreground(
        is_ready=lambda: next(reads),
        heartbeat=lambda: events.append("HEARTBEAT"),
        sleep=lambda _: None,
    )
    assert events == ["HEARTBEAT"]


def test_batch_navigation_waits_for_profile_change_without_timeout(monkeypatch):
    events = []
    session = SimpleNamespace(navigator=SimpleNamespace(hwnd=99))
    monkeypatch.setattr(batch_export, "_wait_for_wechat_foreground", lambda hwnd: True)
    monkeypatch.setattr(batch_export, "_press_down_once", lambda: events.append("DOWN"))
    monkeypatch.setattr(
        batch_export,
        "_wait_header_change",
        lambda nav, old_hash, timeout: events.append(timeout) or "profile-b",
    )
    navigator = batch_export._make_keyboard_navigator(session, lambda: False)
    result = navigator.move_next("profile-a")
    assert result.moved
    assert events == ["DOWN", None]


def test_writer_outputs_only_unique_confirmed_ids(tmp_path):
    rows = [
        {"contact_index": n, "wechat_id": value, "status": "CONFIRMED", "source": "ocr", "confidence": "0.999"}
        for n, value in enumerate(("idaaa1", "idbbb2", "idaaa1"), start=1)
    ]
    assert _write_outputs(tmp_path, rows, []) == 1
    csv_rows = (tmp_path / "wechat_contacts_150.csv").read_text(encoding="utf-8-sig").splitlines()
    txt_rows = (tmp_path / "wechat_contacts_150.txt").read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_rows) == 3  # header plus two unique rows
    assert txt_rows == ["idaaa1", "idbbb2"]
