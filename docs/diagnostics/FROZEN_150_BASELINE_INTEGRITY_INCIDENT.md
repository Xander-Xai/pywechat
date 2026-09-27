# Frozen 150 Export Integrity Incident

Date: 2026-09-27
Status: **RESOLVED / AUTHORITATIVE_BASELINE_REESTABLISHED** (2026-09-27 addendum; the historical 150 files remain absent)

## Scope and stop decision

The navigation V2 optimization was stopped before implementation, replay, or real smoke. The task requires stopping immediately if any frozen 150 artifact changes. The CSV changed after its freeze fingerprint was captured, so the optimization acceptance gate cannot pass.

## Evidence

- Checkpoint confirmed count: 150.
- Current CSV: 150 rows, 150 unique IDs; 11,963 bytes; SHA-256 `6211D9FA612E175CEE1BEA5352A842A0EBAE46D244DF20551906950CC2062032`.
- CSV fingerprint captured earlier in this turn: 12,053 bytes; SHA-256 `93F9A470893CD6923ACDA09BD43E8D8BBEFBF201778E702955DD627B0D814E6C`.
- TXT: 150 rows, 150 unique IDs; SHA-256 `9FFEF6A44136B36F335F8A1A594C61C057C518610AE50CEF0033B68F50DCF2FD`.
- Current stable-snapshot validator result: `PERSISTENT_OUTPUT_INCONSISTENCY:CSV_TXT_SEQUENCE_MISMATCH`.
- First CSV/TXT sequence difference: row 126.
- Current CSV ID set vs checkpoint confirmed-ID hashes: mismatch.
- Current TXT ID set vs checkpoint confirmed-ID hashes: match.
- Frame mismatch = 0; unaccounted = 0.
- No running exporter process was present in the inspected process snapshot; writer/source is **UNKNOWN**.

The CSV's rows remain unique and the checkpoint count remains 150, but those facts do not override the output-to-checkpoint mismatch. This incident report intentionally contains no contact values, names, phone numbers, or profile images.

## Protected response

No file under `output/final/` was rewritten, restored, cleared, or rerun. The earlier captured file hashes are preserved as historical evidence in `docs/baselines/WECHAT_150_SUCCESS_BASELINE.md`. V2 navigation changes, replay benchmark, real smoke, and optimization commits were not performed.

## Navigation analysis state

The checkpoint provides aggregate counts (193 unique profiles visited, 594 revisits, 3 forward-progress recoveries), but no complete per-event causal ledger was analyzed before the correctness stop. Revisit category counts therefore remain **NOT ANALYZED / UNKNOWN**; no causal breakdown is inferred.

## Owner-designated successor baseline — 2026-09-27

Owner confirmed that `output/final/wechat_contacts_151.csv` and `output/final/wechat_contacts_151.txt` contain the original successful export plus one manually added WeChat ID, and designated them as the current authoritative candidates. Read-only validation found 151 rows and 151 unique values in each file, matching ordered content, with no blank, masked, or syntactically invalid values. Their frozen fingerprints and validation record are in [WECHAT_151_DELIVERY_BASELINE.md](../baselines/WECHAT_151_DELIVERY_BASELINE.md); lineage is in [WECHAT_EXPORT_LINEAGE.md](../baselines/WECHAT_EXPORT_LINEAGE.md).

This resolves the missing-150-file gate by establishing a successor authoritative baseline. It does not restore or claim the old 150 files. Navigation V2 must preserve the 151 files' recorded hashes. Historical provenance remains unverified because exact one-record byte removal did not match the old hashes and the checkpoint ID-hash set did not fully match the candidate IDs.
