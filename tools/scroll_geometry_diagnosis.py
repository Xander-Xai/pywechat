"""Derive an anonymized scroll geometry hypothesis from checkpoint evidence."""

from __future__ import annotations

import argparse
import json
import statistics
from itertools import pairwise
from pathlib import Path


def diagnose(checkpoint: dict) -> dict:
    centers = checkpoint.get("previous_viewport_row_centers", [])
    spacings = [float(b) - float(a) for a, b in pairwise(centers) if float(b) > float(a)]
    row_spacing = statistics.median(spacings) if spacings else None
    attempts = [
        item for item in checkpoint.get("scroll_attempts", [])
        if item.get("direction_status") == "FORWARD"
        and isinstance(item.get("row_anchor_median_shift"), (int, float))
        and isinstance(item.get("visible_row_count_before"), (int, float))
    ]
    if not attempts or not row_spacing:
        return {
            "status": "INSUFFICIENT_GEOMETRY_EVIDENCE",
            "evidence_class": "STRONG_PERFORMANCE_HYPOTHESIS",
            "retained_forward_scrolls": len(attempts),
            "median_visible_rows": None,
            "median_row_spacing_px": row_spacing,
            "median_scroll_displacement_px": None,
            "estimated_rows_advanced": None,
            "estimated_overlap_rows": None,
            "estimated_overlap_ratio": None,
        }
    visible = statistics.median(float(a["visible_row_count_before"]) for a in attempts)
    displacement = statistics.median(abs(float(a["row_anchor_median_shift"])) for a in attempts)
    advanced = displacement / row_spacing
    overlap = max(0.0, visible - advanced)
    return {
        "status": "SCROLL_GEOMETRY_DIAGNOSIS",
        "evidence_class": "STRONG_PERFORMANCE_HYPOTHESIS",
        "retained_forward_scrolls": len(attempts),
        "row_spacing_source": "median adjacent row-center deltas in checkpoint geometry",
        "median_visible_rows": round(visible, 4),
        "median_row_spacing_px": round(row_spacing, 4),
        "median_scroll_displacement_px": round(displacement, 4),
        "estimated_rows_advanced": round(advanced, 6),
        "estimated_overlap_rows": round(overlap, 6),
        "estimated_overlap_ratio": round(overlap / visible, 6) if visible else None,
        "interpretation": "Scrolls advance about one row while most visible rows overlap; this does not attribute all historical revisits.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = diagnose(json.loads(args.checkpoint.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "SCROLL_GEOMETRY_DIAGNOSIS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
