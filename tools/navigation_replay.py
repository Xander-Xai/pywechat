"""Replay historical checkpoints cautiously and complete live benchmark telemetry."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

REQUIRED_REPLAY_FIELDS = {
    "viewport_events", "candidate_decisions", "row_signature_events", "scroll_event_timeline",
}


def build_report(checkpoint: dict) -> dict:
    """Historical aggregates cannot substitute for the missing ordered event stream."""
    missing = sorted(REQUIRED_REPLAY_FIELDS - set(checkpoint))
    unique = int(checkpoint.get("unique_profiles_visited", 0))
    revisits = int(checkpoint.get("profile_revisits", 0))
    complete = not missing
    return {
        "benchmark": "WECHAT_NAVIGATION_V2_REPLAY",
        "status": "READY" if complete else "REPLAY_EVIDENCE_INCOMPLETE",
        "evidence_kind": "HISTORICAL_CHECKPOINT",
        "v1": {
            "profile_clicks": int(checkpoint.get("rows_clicked", 0)),
            "profile_revisits": revisits,
            "unique_profiles": unique,
            "revisit_per_unique": round(revisits / unique, 6) if unique else None,
        },
        "v2": None,
        "reduction_percent": None,
        "missing_event_streams": missing,
        "note": (
            "Historical checkpoint aggregates and a bounded scroll tail do not "
            "provide the ordered viewport/row decisions needed for a faithful replay."
            if not complete else ""
        ),
    }


def build_telemetry_report(telemetry: dict) -> dict:
    """Reconstruct metrics from ordered, anonymous live benchmark events."""
    events = telemetry.get("events")
    if telemetry.get("status") != "REPLAY_EVIDENCE_COMPLETE" or not isinstance(events, list):
        return {
            "benchmark": "WECHAT_NAVIGATION_V2_REPLAY",
            "status": "REPLAY_EVIDENCE_INCOMPLETE",
            "evidence_kind": "LIVE_BENCHMARK_TELEMETRY",
            "v2": None,
            "missing_event_streams": ["complete_ordered_telemetry"],
        }

    profiles = [p for e in events for p in e.get("profiles", []) if isinstance(p, dict)]
    ids = [p.get("profile_hash_prefix") for p in profiles if p.get("profile_hash_prefix")]
    unique_profiles = sum(bool(p.get("new_profile")) for p in profiles)
    revisits = sum(bool(p.get("revisit")) for p in profiles)
    if any("ocr_call_count" in p for p in profiles):
        ocr_calls = sum(
            max(0, int(p.get("ocr_call_count", bool(p.get("ocr_called")))))
            for p in profiles
        )
    else:
        # Older live streams kept exact OCR attempts in their aggregate header,
        # while profile events only recorded whether OCR ran at least once.
        legacy_profile_count = sum(bool(p.get("ocr_called")) for p in profiles)
        ocr_calls = max(0, int(telemetry.get("ocr_calls", legacy_profile_count)))
    click_attempts = sum(
        max(0, int(c.get("click_attempts", 0)))
        for e in events for c in e.get("clicks", []) if isinstance(c, dict)
    )
    viewport_events = [e for e in events if e.get("scroll_direction") != "INITIAL"]
    scroll_events = [e for e in viewport_events if e.get("scroll_direction") == "FORWARD"]
    overlaps = [
        max(0.0, float(e["visible_row_count"]) - abs(float(e["anchor_displacement"])) / float(e["median_row_spacing"]))
        for e in scroll_events
        if e.get("anchor_displacement") is not None and float(e.get("median_row_spacing") or 0) > 0
    ]
    skipped = sum(len(e.get("overlap_indices", [])) for e in scroll_events)
    value = {
        "profile_clicks": click_attempts,
        "profile_revisits": revisits,
        "new_profiles": unique_profiles,
        "unique_profile_hash_count": len(set(ids)),
        "revisit_per_unique": round(revisits / unique_profiles, 6) if unique_profiles else None,
        "clicks_per_unique": round(click_attempts / unique_profiles, 6) if unique_profiles else None,
        "ocr_calls": ocr_calls,
        "ocr_calls_per_unique": round(ocr_calls / unique_profiles, 6) if unique_profiles else None,
        "viewport_count": len(events),
        "scroll_count": len(scroll_events),
        "new_profiles_per_viewport": round(unique_profiles / len(events), 6) if events else None,
        "overlap_rows_skipped": skipped,
        "median_overlap_rows": round(statistics.median(overlaps), 4) if overlaps else None,
        "mean_overlap_rows": round(statistics.fmean(overlaps), 4) if overlaps else None,
        "candidate_rows": sum(len(e.get("candidate_indices", [])) for e in events),
        "direction_unverified_events": sum(e.get("scroll_direction") not in {"INITIAL", "FORWARD"} for e in events),
        "telemetry_event_count": len(events),
    }
    online_metrics = {
        "profile_clicks": telemetry.get("profile_clicks"),
        "profile_revisits": telemetry.get("profile_revisits"),
        "new_profiles": telemetry.get("unique_profiles"),
        "ocr_calls": telemetry.get("ocr_calls"),
    }
    compared_metrics = {
        name: int(expected)
        for name, expected in online_metrics.items()
        if expected is not None
    }
    mismatches = {
        name: {"online": expected, "replayed": value[name]}
        for name, expected in compared_metrics.items()
        if expected != value[name]
    }
    return {
        "benchmark": "WECHAT_NAVIGATION_V2_REPLAY",
        "status": "REPLAY_EVIDENCE_COMPLETE",
        "evidence_kind": "LIVE_BENCHMARK_TELEMETRY",
        "run_id": telemetry.get("run_id"),
        "v2": value,
        "metrics_match": not mismatches if compared_metrics else None,
        "metric_mismatches": mismatches,
        "missing_event_streams": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--checkpoint", type=Path)
    source.add_argument("--telemetry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads((args.checkpoint or args.telemetry).read_text(encoding="utf-8"))
    report = build_report(data) if args.checkpoint else build_telemetry_report(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    status = report["status"]
    v2 = report.get("v2")
    print(json.dumps({"status": status, "v2": v2}, ensure_ascii=False))
    return 0 if status in {"READY", "REPLAY_EVIDENCE_COMPLETE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
