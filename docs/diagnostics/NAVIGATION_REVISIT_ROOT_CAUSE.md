# Navigation V1 Revisit Root Cause

Date: 2026-09-27
Input: anonymized aggregate checkpoint, progress snapshot, final report, and retained scroll telemetry. No contact names, IDs, phone numbers, or images are reproduced.

## Baseline

- Automated run: 193 unique profiles visited, 594 profile revisits, 3 forward-progress recoveries.
- Revisit per unique: `594 / 193 = 3.0777`.
- 594 revisit records are present, each with a row ordinal, viewport number, and profile-hash prefix. They do not contain a complete event timestamp or an exact match to the previous viewport's row signature.
- Only a bounded set of 50 scroll-attempt records is retained. It is not a complete chronological join for all 594 revisit records.

## Evidence by proposed cause

| Cause | Revisit count | Share | Finding |
| --- | ---: | ---: | --- |
| ADJACENT_VIEWPORT_OVERLAP | 0 individually attributable | 0% | Plausible, but per-revisit row-signature alignment was not retained. |
| ROW_CURSOR_RESET | 0 individually attributable | 0% | Cursor history is insufficient to distinguish reset from ordinary overlap. |
| VIEWPORT_RESCAN | 0 individually attributable | 0% | No full viewport-by-viewport rescan event ledger. |
| SCROLL_TOO_SMALL | 0 individually attributable | 0% | Strong aggregate signal below, but no exact revisit-to-scroll mapping. |
| SCROLL_OSCILLATION | 0 individually attributable | 0% | Retained attempts all verify downward movement; this does not cover the whole run. |
| RECOVERY_RESCAN | 0 individually attributable | 0% | Three recoveries are counted, but no per-revisit attribution exists. |
| SELECTED_ROW_REPROCESS | 0 individually attributable | 0% | Selected-row state is not recorded per revisit. |
| OTHER / UNKNOWN | **594** | **100%** | Evidence does not support a defensible per-event allocation. |

“0 individually attributable” means the count cannot be assigned from the available evidence; it does not claim the mechanism never occurred.

## Aggregate navigation signal

The retained 50 scroll attempts all requested `DOWN` with wheel delta `-360`, verified `ROW_ANCHOR` forward movement, and showed 13 or 14 visible rows. The reproducible geometry diagnosis derives a 54 px median row spacing from the checkpoint's adjacent row centers and measures a 50 px median anchor displacement. That estimates 0.925926 rows advanced, 12.074074 overlapping rows, and a 0.928775 overlap ratio. This is strong evidence that the retained scroll steps advanced by about one row while most of the viewport remained visible. It cannot prove which of the 594 individual revisits came from that cause.

The checkpoint also reports 1,149 adjacent-overlap row skips, 112 verified downward scrolls, 157 viewport count, and 594 profile revisits. These aggregate counters have different scopes and cannot be subtracted or joined to manufacture a revisit breakdown.

## Conclusion

`ROOT_CAUSE_BREAKDOWN = REPLAY_EVIDENCE_INCOMPLETE`. Preserve the V1 KPI at 3.08 revisits per unique profile. Do not label the 594 as caused by any one mechanism. The live benchmark emits privacy-safe viewport events with candidate/click indices, overlap, displacement, and profile-hash outcomes; `tools/navigation_replay.py --telemetry` replayed completed calibration `run_6` as `REPLAY_EVIDENCE_COMPLETE`.

### Live evidence (2026-09-27)

Calibration `run_6` completed 10 new profiles with 31 clicks and 22 revisits (2.2 revisits per unique). Across 3 scrolls, median estimated overlap was about 8.1 rows; all were above 6, so scrolling remained underpowered. A second calibration attempt (`run_7`) tested discrete wheel pulses but stopped before visiting a profile because final row-anchor evidence was insufficient and scrollbar evidence conflicted. Independent validation `run_2` stopped at 6 new profiles after the same fail-safe direction/alignment condition; telemetry replay correctly reports `REPLAY_EVIDENCE_INCOMPLETE`. It does not meet the 30-profile minimum and cannot support a performance claim. `validation/run_1` failed before checkpoint creation because the UI had no selected contact row.

The live observations show that the current viewport row signatures can lose enough stable anchors during scroll that the controller cannot verify a safe adjacent viewport. The fail-safe retained all candidate rows and skipped zero rows; it did not silently advance. This is a current-navigation finding only and does not change the historical classification: all 594 V1 revisits remain `OTHER / UNKNOWN`. Row signatures remain short-lived visual alignment hints, not contact identities.
