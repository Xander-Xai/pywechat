# Navigation V2

## Contract

- `ViewportScanPlan` aligns the current row signatures against only the immediately previous viewport. It contains viewport ID, visible row geometry, overlap indices, new-row candidate indices, last processed anchor, direction, and confidence.
- Only exact, unique, contiguous visual signatures can establish overlap. Selected green backgrounds are normalized before hashing. Ambiguous, fuzzy, repeated, or non-forward matches produce no overlap and keep all visible rows clickable.
- The cursor resumes after the confirmed overlap boundary. If alignment is uncertain, it begins at the first visible row. Extra safe clicks are preferred to silently skipping a contact.
- Profile hashes remain the post-click identity/revisit guard. A seen profile hash increments revisit metrics and bypasses OCR.
- `AdaptiveScrollController` targets a geometry-derived displacement that leaves 2.5 visible rows, bounded by observed pixels per scroll unit and the current viewport. It reduces movement when direction/alignment is uncertain or overlap is absent. Direction remains verified by `ROW_ANCHOR`; scroll tuning never relaxes direction or frame checks.
- The plan stores geometry and anonymous hashes, not contact values or screenshots. Legacy checkpoints initialize plan/metrics fields without changing the automated confirmed count.

## Metrics

Checkpoints and live status expose `row_candidates_seen`, `row_candidates_skipped_as_overlap`, `profile_clicks`, `profile_revisits`, `new_profiles`, `ocr_calls`, `scroll_events`, `viewport_count`, and `recovery_count`, plus per-unique derived rates. These metrics contain no PII.

## Acceptance status

V1 baseline: 193 unique profiles, 594 revisits, 3.0777 revisits per unique. Historical replay remains `REPLAY_EVIDENCE_INCOMPLETE`; do not infer a V1-to-V2 reduction from it. The retained geometry diagnosis measured 13 median visible rows, 54 px median row spacing, and 50 px median forward displacement (about 0.93 rows advanced and 12.07 rows overlapping). This is a strong performance hypothesis, not attribution of all 594 revisits.

The controlled live benchmark is separate from the authoritative 151-contact deliverables. Calibration and validation outputs belong under `output/benchmark_navigation_v2/`, never `output/final/`.

`calibration/run_6` completed its 10-new-profile smoke and replayed as `REPLAY_EVIDENCE_COMPLETE`: 31 profile clicks, 22 revisits, 10 new profiles, 13 OCR calls, 4 viewports, 3 scrolls, and zero overlap rows skipped. Revisit per unique was 2.2. All 3 scrolls retained more than 6 estimated overlap rows (median about 8.1), so this calibration was underpowered and does not pass the tuning criterion. `run_7` tested bounded discrete wheel pulses, but its first viewport could not establish a safe final adjacent alignment; it recorded zero profiles and remains incomplete. The measured pulses advanced the list, while final anchor evidence was insufficient and scrollbar evidence conflicted.

Independent `validation/run_2`, seeded from the completed `run_6` checkpoint, stopped at 6 new profiles when the next scroll could not establish safe forward direction/alignment. At stop it had 5 clicks, 0 revisits, 6 OCR calls, 1 viewport, 0 overlap rows skipped, 0 frame mismatches, and 0 unaccounted outcomes. Its telemetry replay is `REPLAY_EVIDENCE_INCOMPLETE`; the sample is below the required 30 new profiles and is not a performance result. The automatic retry was stopped after it returned to the same unresolved-direction state. The separate `validation/run_1` startup attempt failed before checkpoint creation because no contact row was selected; it is not sample evidence.

The 151-contact delivery hashes were rechecked before the benchmark attempts and remained unchanged. The 10-profile calibration met replay completeness but missed its overlap target; the independent validation did not reach its minimum sample. Do not claim Navigation V2 performance PASS or a reduction against V1. Historical attribution remains `REPLAY_EVIDENCE_INCOMPLETE`, with all 594 revisits still `OTHER / UNKNOWN`.
