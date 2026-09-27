# WeChat Export Incident Retrospective

## Confirmed lessons

1. Target HWND is not the same as the foreground HWND.
2. Monitor position does not establish Windows global foreground ownership.
3. Separate `READ_GATE` from `INPUT_GATE`.
4. Keep raw OCR evidence separate from masked review and log display.
5. An attempt error is not automatically a terminal result.
6. A profile revisit is not a duplicate WeChat ID.
7. `LIST_FRAME` owns row geometry and viewport analysis; `PROFILE_FRAME` owns profile hashing and OCR.
8. Checkpoint and resume must preserve accounting and re-anchor against current UI state.
9. `HEALTHY_PAUSED` is not process failure.
10. Supervisor snapshot collection must retry across unstable worker state.
11. A paused worker can be adopted without spawning another worker.
12. Background CMD windows must not take foreground from WeChat.
13. A row signature is only an adjacent viewport hint, never a permanent contact identity.

## Delivery integrity incident

The frozen 150 CSV/TXT artifacts became unavailable and the original 150 CSV had previously failed its recorded hash/sequence gate. No file was reconstructed from the checkpoint. The Owner later designated a 151-row CSV/TXT successor pair, confirmed it represented the successful delivery plus one manual addition, and the pair passed independent read-only row, uniqueness, syntax, and order checks. The current 151 pair is frozen as delivery authority; the automated checkpoint remains 150. The old 150 files remain absent, and byte-level historical provenance is unverified.

## Navigation evidence limit

The automated run recorded 594 revisits. Retained scroll telemetry strongly suggests the effective scroll step was about one row, but it lacks a complete event join linking each revisit to the preceding viewport signature and scroll. All 594 revisits therefore remain in `OTHER / UNKNOWN`; no precise causal split is claimed.
