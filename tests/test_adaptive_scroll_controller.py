from contact_exporter.fixed_geometry_export import (
    AdaptiveScrollController,
    _run_target_reached,
    _scroll_alignment_confidence,
    _scroll_pulse_budget_remaining,
)


def test_controller_targets_geometry_and_increases_underpowered_scroll():
    controller = AdaptiveScrollController(current_scroll_units=3)
    controller.update_geometry(visible_rows=13, row_spacing=54)
    assert controller.target_displacement == 567.0
    assert controller.max_scroll_units == 12  # conservative before measured units

    controller.observe(
        actual_units=3, displacement=-50, direction="FORWARD",
        alignment_confidence="HIGH",
    )
    assert controller.observed_displacement == 50
    assert controller.overlap_ratio > 0.9
    assert controller.current_scroll_units == 5
    assert controller.current_scroll_units <= controller.max_scroll_units


def test_controller_increases_from_measured_anchor_overlap_when_visual_row_hashes_are_uncertain():
    controller = AdaptiveScrollController(current_scroll_units=2)
    controller.update_geometry(visible_rows=8, row_spacing=54)
    # Exact row hashes may fail to align, but two unique anchors verified forward motion.
    controller.observe(
        actual_units=2, displacement=-50, direction="FORWARD",
        alignment_confidence="HIGH",
    )
    assert controller.current_scroll_units == 4
    assert controller.target_displacement == 297


def test_anchor_confidence_guides_tuning_without_changing_viewport_scan_confidence():
    assert _scroll_alignment_confidence("FORWARD", 2, "UNCERTAIN") == "HIGH"
    assert _scroll_alignment_confidence("FORWARD", 1, "UNCERTAIN") == "UNCERTAIN"
    assert _scroll_alignment_confidence("UPWARD", 3, "UNCERTAIN") == "UNCERTAIN"


def test_discrete_wheel_pulses_stop_at_target_and_viewport_safety_cap():
    assert _scroll_pulse_budget_remaining(250, 297, 378)
    assert not _scroll_pulse_budget_remaining(297, 297, 378)
    assert not _scroll_pulse_budget_remaining(378, 297, 378)


def test_controller_reduces_scroll_for_zero_or_unverified_overlap():
    controller = AdaptiveScrollController(current_scroll_units=7)
    controller.update_geometry(visible_rows=12, row_spacing=48)
    controller.observe(
        actual_units=7, displacement=0, direction="FORWARD",
        alignment_confidence="HIGH",
    )
    assert controller.current_scroll_units == 6
    controller.observe(
        actual_units=6, displacement=-500, direction="FORWARD",
        alignment_confidence="UNCERTAIN",
    )
    assert controller.current_scroll_units == 5


def test_profile_limit_is_unique_profiles_not_clicks_or_confirmed_target():
    state = {"unique_profiles_visited": 9, "confirmed": [], "rows_clicked": 90}
    assert not _run_target_reached(state, target=150, profile_limit=10)
    state["unique_profiles_visited"] = 10
    assert _run_target_reached(state, target=150, profile_limit=10)
