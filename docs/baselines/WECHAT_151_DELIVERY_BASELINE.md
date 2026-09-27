# WECHAT_151_DELIVERY_BASELINE

Status: **PASS — CURRENT AUTHORITATIVE DELIVERY BASELINE**

Owner confirmed that these candidate files represent the original successful export plus one manually added WeChat ID. The two files below were read only and are frozen by their byte-level SHA-256 fingerprints. No complete WeChat IDs are recorded in this document.

## Artifacts

| Artifact | Path | Bytes | Encoding | Newlines | Data rows | Unique | SHA-256 |
| --- | --- | ---: | --- | --- | ---: | ---: | --- |
| CSV | `output/final/wechat_contacts_151.csv` | 1,848 | UTF-8 (no BOM) | LF | 151 | 151 | `ef63f3a3a24abcbb17be76eedd4e6cfb02822daf19512266da3425392695a06b` |
| TXT | `output/final/wechat_contacts_151.txt` | 1,838 | UTF-8 (no BOM) | LF | 151 | 151 | `aa48d0c82a9f03ee758f791110430ef82476874802f8818086bc6d8b68caf72f` |

The CSV has one `wechat_id` column and a header. The TXT has one identifier per line. Both files end with LF. The CSV has 152 physical lines including its header; the TXT has 151 physical lines.

## Validation

- `CSV_ROWS = 151`, `TXT_ROWS = 151`
- `CSV_UNIQUE = 151`, `TXT_UNIQUE = 151`
- Normalized CSV and TXT sequences match item by item and in order: **PASS**
- Blank values: 0
- Mask placeholders (`*`, bullets, ellipsis variants): 0
- Invalid values under the project's conservative WeChat-ID validator: 0
- Hyphen and underscore remain allowed characters.
- Automated checkpoint remains at 150 confirmed records; the manual addition is not represented as an automated confirmation.

## Historical provenance check

Removing each individual CSV or TXT record while preserving the candidate's physical line bytes did not reproduce the historical 150-file SHA-256. Candidate IDs matched 149 of the 150 historical confirmed-ID hashes in the checkpoint; the set comparison is not sufficient to establish a one-record-only lineage. Therefore historical provenance is **HISTORICAL_PROVENANCE_UNVERIFIED**. This does not invalidate the Owner-designated current 151-record authority.

Historical 150 hashes remain in [WECHAT_150_SUCCESS_BASELINE.md](WECHAT_150_SUCCESS_BASELINE.md). The original 150 CSV and TXT files remain absent and were not recreated.

Frozen: 2026-09-27 12:28:49 +08:00
