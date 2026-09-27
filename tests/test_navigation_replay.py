from tools.navigation_replay import build_report, build_telemetry_report
from tools.scroll_geometry_diagnosis import diagnose


def test_replay_fails_closed_when_historical_event_streams_are_missing():
    report = build_report({
        "rows_clicked": 820,
        "profile_revisits": 594,
        "unique_profiles_visited": 193,
        "scroll_attempts": [{"direction_status": "FORWARD"}],
    })
    assert report["status"] == "REPLAY_EVIDENCE_INCOMPLETE"
    assert report["v1"]["revisit_per_unique"] == 3.07772
    assert report["v2"] is None
    assert report["reduction_percent"] is None


def test_replay_requires_every_ordered_stream_before_reporting_v2():
    report = build_report({
        "rows_clicked": 10,
        "profile_revisits": 2,
        "unique_profiles_visited": 5,
        "viewport_events": [],
        "candidate_decisions": [],
        "row_signature_events": [],
    })
    assert report["status"] == "REPLAY_EVIDENCE_INCOMPLETE"
    assert report["v2"] is None
    assert report["missing_event_streams"] == ["scroll_event_timeline"]


def test_live_replay_reconstructs_metrics_from_anonymous_events():
    report = build_telemetry_report({
        "status": "REPLAY_EVIDENCE_COMPLETE",
        "run_id": "anonymous-run",
        "events": [
            {"scroll_direction": "INITIAL", "candidate_indices": [0, 1], "clicks": [
                {"click_attempts": 1}], "profiles": [
                    {"profile_hash_prefix": "a" * 12, "new_profile": True,
                     "revisit": False, "ocr_called": True}]},
            {"scroll_direction": "FORWARD", "visible_row_count": 10,
             "median_row_spacing": 50, "anchor_displacement": -350,
             "overlap_indices": [0, 1, 2], "candidate_indices": [3, 4, 5],
             "clicks": [{"click_attempts": 2}], "profiles": [
                 {"profile_hash_prefix": "b" * 12, "new_profile": True,
                  "revisit": False, "ocr_called": True},
                 {"profile_hash_prefix": "a" * 12, "new_profile": False,
                  "revisit": True, "ocr_called": False}]},
        ],
    })
    assert report["status"] == "REPLAY_EVIDENCE_COMPLETE"
    assert report["v2"]["new_profiles"] == 2
    assert report["v2"]["profile_revisits"] == 1
    assert report["v2"]["profile_clicks"] == 3
    assert report["v2"]["median_overlap_rows"] == 3.0
    assert report["v2"]["overlap_rows_skipped"] == 3


def test_live_replay_refuses_incomplete_telemetry():
    report = build_telemetry_report({"status": "IN_PROGRESS", "events": []})
    assert report["status"] == "REPLAY_EVIDENCE_INCOMPLETE"
    assert report["v2"] is None


def test_live_replay_sums_ocr_attempts_and_supports_legacy_boolean():
    report = build_telemetry_report({
        "status": "REPLAY_EVIDENCE_COMPLETE",
        "events": [{"scroll_direction": "INITIAL", "profiles": [
            {"profile_hash_prefix": "a", "new_profile": True,
             "ocr_called": True, "ocr_call_count": 3},
            {"profile_hash_prefix": "b", "new_profile": True,
             "ocr_called": True},
        ]}],
    })
    assert report["v2"]["ocr_calls"] == 4

    telemetry = {
        "status": "REPLAY_EVIDENCE_COMPLETE", "ocr_calls": 13,
        "events": [{"scroll_direction": "INITIAL", "profiles": [
            {"profile_hash_prefix": "a", "new_profile": True, "ocr_called": True},
        ]}],
    }
    assert build_telemetry_report(telemetry)["v2"]["ocr_calls"] == 13


def test_scroll_diagnosis_derives_spacing_from_observed_row_centers():
    report = diagnose({
        "previous_viewport_row_centers": [10, 30, 50],
        "scroll_attempts": [{
            "direction_status": "FORWARD",
            "row_anchor_median_shift": -40,
            "visible_row_count_before": 8,
        }],
    })
    assert report["median_row_spacing_px"] == 20
    assert report["estimated_rows_advanced"] == 2
    assert report["estimated_overlap_rows"] == 6
