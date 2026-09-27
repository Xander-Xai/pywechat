# WeChat Export Lineage

## Automated execution baseline

- Automated export: 150 unique profiles confirmed; worker state `TARGET_REACHED`.
- `frame_mismatch = 0`; `unaccounted = 0`; `duplicate_write = 0`.
- Navigation telemetry: 193 unique profiles visited, 594 profile revisits, 3 forward-progress recoveries.
- The checkpoint and final report record the automated 150 run. They do not include the later manual record.

## Post-export owner edit

Owner confirmed one WeChat ID was manually added to the successful 150-contact delivery. The exact added value is intentionally not recorded here.

## Current authoritative delivery

- `output/final/wechat_contacts_151.csv`
- `output/final/wechat_contacts_151.txt`
- 151 unique IDs; CSV/TXT ordered contents match.
- Frozen byte hashes and validation details: [WECHAT_151_DELIVERY_BASELINE.md](WECHAT_151_DELIVERY_BASELINE.md).

## Historical artifact status

The original 150 CSV and TXT files are currently absent. Their historical hashes are preserved in [WECHAT_150_SUCCESS_BASELINE.md](WECHAT_150_SUCCESS_BASELINE.md). The 151 files are the new `AUTHORITATIVE_DELIVERY_BASELINE`; the 150 run remains the `HISTORICAL_EXECUTION_BASELINE`. The automated checkpoint must remain at 150.

Historical content-level provenance is unverified: a one-record byte-removal replay did not match the old file hashes, and candidate/checkpoint ID-hash sets did not fully coincide. Owner designation and the independent 151-file integrity checks establish the current authority without claiming the absent 150 files were restored.
