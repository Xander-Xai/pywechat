# WECHAT_150_SUCCESS_BASELINE

Status: **PASS / TARGET_REACHED**. This records the immutable acceptance baseline for the completed 150-contact export. Navigation and performance experiments must use an isolated output directory such as `output/benchmark_navigation_v2/` and must not write to `output/final/`.

The export passed a stable snapshot check: confirmed checkpoint = 150; CSV/TXT rows = 150 each; CSV/TXT unique counts = 150 each; ordered CSV/TXT contact IDs match; frame mismatches = 0; unaccounted = 0. The 34 duplicate profiles/records were skipped by duplicate protection and did not produce duplicate output rows. Review count = 9.

## Frozen artifacts

Hashes are SHA-256 over the exact bytes present at freeze time. CSV row counts exclude the header. TXT row count is the number of contact lines. JSON line counts are not applicable.

| Artifact | Bytes | Rows | SHA-256 |
| --- | ---: | ---: | --- |
| `output/final/wechat_contacts_150.csv` | 12,053 | 150 | `93F9A470893CD6923ACDA09BD43E8D8BBEFBF201778E702955DD627B0D814E6C` |
| `output/final/wechat_contacts_150.txt` | 1,831 | 150 | `9FFEF6A44136B36F335F8A1A594C61C057C518610AE50CEF0033B68F50DCF2FD` |
| `output/final/wechat_contacts_150_review.csv` | 730 | 9 | `267C84DE34D6AAF79554126AEC19D52A817BDEAB507268D3825405FC11DAB784` |
| `output/final/fixed_geometry_checkpoint.json` | 703,070 | N/A | `5EEDB318AB4309B209B6B98EE06B598016EFE1925EF2A57FD9D11C1D8B1EB06C` |
| `output/final/WECHAT_150_FINAL_EXPORT_REPORT.json` | 1,789 | N/A | `C80BC437AC229D39F07EB20FCAAF12D9C5DBF508E287A4264DB413E84CC8BE37` |

The final report JSON was absent when this freeze began. It was created from the verified checkpoint and output-file hashes without modifying the four pre-existing artifacts. It contains aggregate acceptance data only, not contact PII.

## Navigation V1 baseline

- Confirmed contacts: 150
- Unique profiles visited: 193
- Profile revisits: 594
- Revisit per unique profile: `594 / 193 = 3.0777` (about 3.08)
- Confirmed per unique profile: `150 / 193 = 0.7772`
- Review records: 9
- Duplicate records skipped: 34
- Forward-progress recoveries: 3
- Frame mismatch: 0
- Unaccounted: 0

Review and duplicate records are extraction/accounting outcomes; do not attribute them to navigation without evidence. The verified checkpoint currently exposes aggregate navigation counts, not a complete per-event revisit cause ledger. Any unsupported causal allocation must be recorded as unknown rather than inferred.

Freeze created on 2026-09-27. Source Git HEAD at branch creation: `7b768fd` (`feat: add manual WeChat profile popup OCR smoke`).

## Post-freeze integrity incident — 2026-09-27

A later read-only check found that the current CSV no longer matches the bytes recorded above. At capture it was 12,053 bytes with SHA-256 `93F9A470893CD6923ACDA09BD43E8D8BBEFBF201778E702955DD627B0D814E6C`; the later snapshot is 11,963 bytes with SHA-256 `6211D9FA612E175CEE1BEA5352A842A0EBAE46D244DF20551906950CC2062032`.

The current snapshot still has 150 CSV rows and 150 unique IDs, but `validate_resume` returns `CSV_TXT_SEQUENCE_MISMATCH`. The first differing row is 126. More seriously, the current CSV ID set does not match the checkpoint's 150 confirmed-ID hashes, while the TXT set does. Checkpoint confirmed count remains 150; review, mismatch, and unaccounted counts remain as recorded. No active exporter worker was found in the process snapshot, and the writer/source of the post-capture CSV change is unknown.

This section supersedes any implication that the current `output/final/wechat_contacts_150.csv` still has the frozen hash. The captured hash values above remain historical evidence only. Navigation optimization, replay, smoke, and commits are stopped at the required correctness gate. No attempt was made to rewrite or restore any file under `output/final/`.
