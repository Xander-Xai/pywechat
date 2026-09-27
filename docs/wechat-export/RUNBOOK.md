# WeChat Exporter Runbook

## Before running

1. Confirm the target WeChat window can be read. Do not require it to be foreground until input is needed.
2. For clicks and scrolling, wait on `INPUT_GATE`; do not bypass it based on monitor position or a remembered HWND.
3. Inspect checkpoint, CSV/TXT consistency, worker heartbeat, and supervisor lock before resuming or adopting a worker.
4. Keep the automated export output scope separate from `output/final/wechat_contacts_151.*`, which is the frozen authoritative delivery pair.

## Resume and supervision

- Resume only when confirmed export rows match checkpoint hashes and stable snapshot validation passes.
- `HEALTHY_PAUSED` is an adoptable waiting state. Do not spawn a second worker when one is live and adopted.
- Retry supervisor snapshots when heartbeat and worker state appear inconsistent; do not treat one transient observation as terminal.
- Keep checkpoint, heartbeat, single-supervisor lock, and detached worker behavior intact during navigation changes.

## Navigation diagnostics

Inspect aggregate `navigation_metrics` for revisits, clicks, OCR calls, scroll events, viewport count, and recoveries. Correlate them with bounded viewport/row-anchor telemetry. Unknown causes remain unknown. Run `tools/navigation_replay.py` against anonymized checkpoint telemetry before any real navigation smoke.

## Windows background launch

Use `.venv\\Scripts\\python.exe` directly with `shell=False` and `CREATE_NO_WINDOW` for formal background Python workers. Do not route formal workers through `activate.bat`, `cmd /c`, or `CREATE_NEW_CONSOLE`. A dummy integration check may validate launcher behavior without touching WeChat or formal outputs.

## Privacy and delivery

Do not print contact values or save screenshots. Raw OCR candidates may only flow to validation and confirmed output. Mask review/log display values. After any optimization, compare the 151 CSV/TXT SHA-256 values, row and unique counts, and ordered sequence with the frozen baseline. Never run the automated 150 target into the delivery directory.
