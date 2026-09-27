import csv
import json

from PIL import Image, ImageDraw

from contact_exporter import fixed_geometry_export as fixed


def test_hash_change_and_upward_thumb_does_not_count_as_forward():
    before = fixed._scrollbar_progress(500, 560, 200, 980)
    after = fixed._scrollbar_progress(460, 520, 200, 980)
    assert fixed._list_view_changed("hash-a", "hash-b")
    assert fixed._scrollbar_direction(before, after) == "UPWARD"


def test_thumb_downward_and_changed_hash_is_verified_forward():
    before = fixed._scrollbar_progress(500, 560, 200, 980)
    after = fixed._scrollbar_progress(520, 580, 200, 980)
    assert fixed._list_view_changed("hash-a", "hash-b")
    assert fixed._scrollbar_direction(before, after) == "FORWARD"


def test_thumb_same_position_is_not_forward_progress():
    progress = fixed._scrollbar_progress(500, 560, 200, 980)
    assert fixed._scrollbar_direction(progress, progress) == "NO_MOVEMENT"


def test_row_anchors_override_conflicting_scrollbar_direction():
    direction, conflict = fixed._combine_scroll_direction_evidence("FORWARD", 2, "UPWARD")
    assert direction == "FORWARD"
    assert conflict


def test_scrollbar_motion_without_row_anchor_motion_is_unverified():
    direction, conflict = fixed._combine_scroll_direction_evidence("NO_MOVEMENT", 7, "FORWARD")
    assert direction == "NO_MOVEMENT"
    assert conflict


def test_scroll_direction_fails_closed_when_anchors_are_insufficient():
    direction, conflict = fixed._combine_scroll_direction_evidence("FORWARD", 1, "FORWARD")
    assert direction == "DIRECTION_UNVERIFIED_VIEWPORT"
    assert conflict


def test_inverted_wheel_selects_scrollbar_drag_or_fails_closed():
    assert fixed._scroll_strategy_for_direction("UPWARD", True) == "SCROLLBAR_DRAG_PRIMARY"
    assert fixed._scroll_strategy_for_direction("UPWARD", False) == "SCROLL_DIRECTION_UNRESOLVED"


def test_small_scrollbar_drag_moves_thumb_down():
    track_top, track_bottom = 200, 980
    delta = fixed._scrollbar_drag_delta(track_top, track_bottom)
    before = fixed._scrollbar_progress(500, 560, track_top, track_bottom)
    after = fixed._scrollbar_progress(500 + delta, 560 + delta, track_top, track_bottom)
    assert 0 < delta <= round((track_bottom - track_top) * 0.06)
    assert fixed._scrollbar_direction(before, after) == "FORWARD"


def test_scrollbar_thumb_detector_returns_client_relative_geometry():
    image = Image.new("RGB", (1000, 1000), "#303030")
    draw = ImageDraw.Draw(image)
    draw.rectangle((331, 450, 333, 520), fill="#eeeeee")
    thumb = fixed._detect_scrollbar_thumb(image)
    assert thumb is not None
    assert abs(thumb["thumb_top"] - 450) <= 2
    assert abs(thumb["thumb_bottom"] - 521) <= 2
    assert thumb["track_top"] == 120
    assert thumb["track_bottom"] == 980
    assert 331 <= thumb["x"] <= 333


def test_scrollbar_thumb_detector_keeps_detection_at_track_bottom():
    image = Image.new("RGB", (1000, 1000), "#303030")
    ImageDraw.Draw(image).rectangle((331, 930, 333, 979), fill="#eeeeee")
    thumb = fixed._detect_scrollbar_thumb(image)
    assert thumb is not None
    assert thumb["progress"] >= 0.98


def test_resume_output_gate_requires_40_unique_rows_and_matching_order():
    rows = [{"wechat_id": f"contact-{index}"} for index in range(40)]
    ids = [row["wechat_id"] for row in rows]
    assert fixed._resume_output_consistency(rows, ids, 40)["consistent"]
    assert not fixed._resume_output_consistency(rows, list(reversed(ids)), 40)["consistent"]


def test_resume_output_gate_accepts_consistent_incremental_confirmed_rows():
    rows = [{"wechat_id": f"contact-{index}"} for index in range(41)]
    ids = [row["wechat_id"] for row in rows]
    assert fixed._resume_output_consistency(rows, ids, 41)["consistent"]
    assert not fixed._resume_output_consistency(rows, ids[:-1], 41)["consistent"]


def test_benchmark_resume_output_gate_accepts_small_exact_sample():
    rows = [{"wechat_id": f"profile-{index}"} for index in range(5)]
    ids = [row["wechat_id"] for row in rows]
    assert not fixed._resume_output_consistency(rows, ids, 5)["consistent"]
    assert fixed._resume_output_consistency(
        rows, ids, 5, minimum_confirmed_count=1
    )["consistent"]


def test_benchmark_resume_verifier_uses_isolated_sample_threshold(tmp_path, monkeypatch):
    rows = [{"wechat_id": f"profile-{index}"} for index in range(5)]
    monkeypatch.setattr(fixed, "OUT", tmp_path)
    monkeypatch.setattr(fixed, "CHECKPOINT", tmp_path / "fixed_geometry_checkpoint.json")
    fixed.CHECKPOINT.write_text(json.dumps({"benchmark_mode": True, "confirmed_count": 5}))
    with (tmp_path / f"{fixed.OUTPUT_STEM}.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fixed.FORMAL_FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows):
            writer.writerow({**row, "row_ordinal": index + 1, "status": "CONFIRMED", "source": "OCR", "confidence": "HIGH"})
    (tmp_path / f"{fixed.OUTPUT_STEM}.txt").write_text(
        "".join(row["wechat_id"] + "\n" for row in rows), encoding="utf-8"
    )
    assert fixed._verify_resume_outputs()["consistent"]


def test_benchmark_resume_verifier_accepts_exactly_empty_fresh_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(fixed, "OUT", tmp_path)
    monkeypatch.setattr(fixed, "CHECKPOINT", tmp_path / "fixed_geometry_checkpoint.json")
    fixed.CHECKPOINT.write_text(json.dumps({"benchmark_mode": True, "confirmed_count": 0}))
    with (tmp_path / f"{fixed.OUTPUT_STEM}.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        csv.writer(fp).writerow(fixed.FORMAL_FIELDS)
    (tmp_path / f"{fixed.OUTPUT_STEM}.txt").write_text("", encoding="utf-8")

    assert fixed._verify_resume_outputs()["consistent"]


def test_row_anchor_proves_content_moved_upward():
    direction, shift, matched = fixed._row_anchor_direction(
        ["a", "b", "c"], [400, 500, 600],
        ["b", "c", "d"], [300, 400, 500],
    )
    assert direction == "FORWARD"
    assert shift == -200
    assert matched == 2


def test_profile_revisit_during_verified_progress_does_not_stall():
    state = {
        "seen_profile_hashes": ["known"],
        "unique_profiles_visited": 45,
        "pending_forward_viewport": True,
        "pending_forward_new_profiles": 0,
    }
    assert fixed._register_profile_hash(state, "known") == "PROFILE_REVISIT"
    assert fixed._complete_forward_viewport(state) == "CONTINUE"


def test_five_completed_monotonic_viewports_without_new_profiles_stall():
    state = {"unique_profiles_visited": 45, "seen_profile_hashes": ["known"]}
    for index in range(5):
        assert fixed._record_scroll_progress(
            state, f"view-{index}", direction="FORWARD", list_changed=True
        ) == "CONTINUE"
        fixed._register_profile_hash(state, "known")
        result = fixed._complete_forward_viewport(state)
    assert result == "FORWARD_PROGRESS_STALLED"
    assert state["forward_viewports_without_new_profile"] == 5


def test_verified_but_unscanned_viewport_does_not_count_as_checked_or_stalled():
    state = {"unique_profiles_visited": 45}
    assert fixed._record_scroll_progress(state, "v", direction="FORWARD") == "CONTINUE"
    assert fixed._complete_forward_viewport(state) == "FORWARD_VIEWPORT_UNSCANNED"
    assert state.get("forward_viewports_checked", 0) == 0
    assert state.get("forward_viewports_without_new_profile", 0) == 0


def test_navigation_uses_detected_visible_row_centers_after_scroll():
    rows = [500, 550, 600, 650]
    assert fixed._next_visible_row(rows, 599, 50, 700) == 650
    assert fixed._next_visible_row(rows, 650, 50, 700) is None


def test_identical_profile_hash_can_advance_only_when_selected_row_moves():
    before = Image.new("RGB", (400, 500), "#303030")
    after = Image.new("RGB", (400, 500), "#303030")
    roi_left, roi_right = round(0.04 * 400), round(0.17 * 400)
    ImageDraw.Draw(before).rectangle((roi_left, 188, roi_right, 212), fill="#35d65a")
    ImageDraw.Draw(after).rectangle((roi_left, 238, roi_right, 262), fill="#35d65a")
    assert fixed._row_selection_advanced(before, after, [200, 250, 300], 250) == (0, 1)
    transition = {"profile_changed": False, "row_selection_changed": True}
    state = {"row_ordinal": 10}
    assert fixed._advance_sequence_after_transition(state, transition)
    assert state["row_ordinal"] == 11


def test_nonselectable_row_requires_unchanged_selection_and_different_target():
    frame = Image.new("RGB", (400, 500), "#303030")
    roi_left, roi_right = round(0.04 * 400), round(0.17 * 400)
    ImageDraw.Draw(frame).rectangle((roi_left, 238, roi_right, 262), fill="#35d65a")
    rows = [200, 250, 300]
    assert fixed._nonselectable_row_proven(frame, frame.copy(), rows, 300) == 1
    assert fixed._nonselectable_row_proven(frame, frame.copy(), rows, 250) is None


def test_hashes_changing_while_progress_oscillates_is_detected():
    history = [
        {"progress": 0.50, "viewport_hash": "a"},
        {"progress": 0.51, "viewport_hash": "b"},
        {"progress": 0.50, "viewport_hash": "c"},
    ]
    assert fixed._scroll_oscillation(history, 0.51, "d")


def test_legacy_hash_only_scrolls_are_marked_direction_unverified():
    attempts = [{"list_changed": True}, {"list_changed": True}]
    assert fixed._mark_legacy_scroll_attempts(attempts) == 2
    assert all(item["direction_status"] == "DIRECTION_UNVERIFIED_VIEWPORT" for item in attempts)
    assert all(not item["counted_as_forward_viewport"] for item in attempts)


def test_ten_new_profiles_after_verified_progress_mark_fix_pass():
    state = {
        "seen_profile_hashes": [],
        "unique_profiles_visited": 45,
        "pending_forward_viewport": True,
        "pending_forward_new_profiles": 0,
        "forward_progress_probe_started": True,
    }
    for index in range(10):
        fixed._register_profile_hash(state, f"new-profile-{index}")
    assert state["unique_profiles_visited"] == 55
    assert state["forward_direction_fix"] == "PASS"
    assert state["forward_progress_probe_recovered"] is True
    assert state["forward_progress_probe_started"] is False


def test_probe_keeps_running_after_first_new_profile_until_55_unique():
    state = {"unique_profiles_visited": 46, "forward_progress_probe_started": True}
    fixed._mark_forward_progress_recovered(state)
    assert state["forward_progress_probe_recovered"] is True
    assert state["forward_progress_probe_started"] is True
    assert state.get("forward_direction_fix") != "PASS"
