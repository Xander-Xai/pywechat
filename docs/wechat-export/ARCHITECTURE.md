# WeChat Exporter Architecture

## Navigation and extraction boundary

- The foreground model separates `TARGET_WEIXIN_HWND` from the currently foreground window. A readable WeChat window satisfies `READ_GATE`; clicks and scrolls satisfy `INPUT_GATE` only when WeChat is foreground.
- Contact-list rows are detected and clicked from the current list frame. That frame owns row geometry, row signatures, and viewport analysis.
- After a click, a new stable profile frame owns profile hash verification and OCR. A list frame must never be reused for contact OCR, and a profile frame must not be used to infer old list rows.
- OCR candidate text stays transient in validation and confirmed output. Review rows, checkpoints, telemetry, and logs contain masked candidates or hashes and geometry only.
- Row signatures are ephemeral visual alignment hints between adjacent viewports. They are not persistent contact identity. Profile hashes remain the revisit safety set; a revisit bypasses OCR.

## State and lifecycle

The checkpoint stores confirmed counts and masked IDs, profile hashes, row geometry/signatures, bounded recent viewport hashes, transition accounting, and lifecycle state. Resume validates output/checkpoint consistency and re-anchors against the current stable profile. Older checkpoints may omit `navigation_metrics` and `viewport_cursor`; those fields are initialized with safe defaults.

`HEALTHY_PAUSED` represents a live worker waiting for an input gate. A supervisor may adopt a running or healthy paused worker; transient attempt errors do not imply terminal failure. Supervisor snapshots should retry when worker state changes during capture and use a single lock.

## Output authority

- Automated execution baseline: 150 confirmed records.
- Current authoritative delivery baseline: the Owner-designated 151-row CSV/TXT pair. One record was manually added after automation.
- The checkpoint remains at 150. It must not be rewritten to claim an automated 151st confirmation.
- Frozen hashes and the lineage boundary are documented in `docs/baselines/WECHAT_151_DELIVERY_BASELINE.md` and `docs/baselines/WECHAT_EXPORT_LINEAGE.md`.
