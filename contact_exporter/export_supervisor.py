"""Lifecycle supervisor for the one-off WeChat contact export worker."""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wechat_ocr.config import DEFAULT_CONFIG
from wechat_ocr.validator import validate_wechat_id

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "final"
CHECKPOINT = OUT / "fixed_geometry_checkpoint.json"
PROGRESS = OUT / "wechat_contacts_150.progress.json"
LIVE = OUT / "fixed_geometry_live_status.json"
STATUS = OUT / "supervisor_status.json"
CSV_PATH = OUT / "wechat_contacts_150.csv"
TXT_PATH = OUT / "wechat_contacts_150.txt"
REVIEW_PATH = OUT / "wechat_contacts_150_review.csv"
MAX_AUTOMATIC_RESTARTS = 5
MAX_SAME_COUNT_FAILURES = 3
HEARTBEAT_TIMEOUT_SECONDS = 12
POLL_INTERVAL_SECONDS = 1.5
SNAPSHOT_ATTEMPTS = 7
SNAPSHOT_BACKOFF_SECONDS = (0.1, 0.25, 0.5, 1.0, 1.0, 1.0)
LOCK_PATH = OUT / "supervisor.lock"
HEALTHY_ACTIVE_STATES = {"RUNNING"}
HEALTHY_PAUSED_STATES = {
    "PAUSED_INPUT_FOREGROUND_REQUIRED", "PAUSED_FOREGROUND_LOST",
    "WAITING_INPUT_FOREGROUND", "PAUSED_RECOVERY_REQUIRED",
}
TERMINAL_SUCCESS_STATES = {"PASS", "COMPLETED"}
TERMINAL_BLOCKED_STATES = {
    "END_OF_CONTACT_LIST", "FATAL_CORRECTNESS_ERROR",
    "REPEATED_PROCESS_FAILURE", "CORRECTNESS_BLOCK", "PROCESS_ERROR",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", delete=False,
        dir=path.parent, prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp",
    ) as fp:
        temp = Path(fp.name)
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class ResumeCheck:
    safe: bool
    reason: str
    confirmed: int = 0
    unique_profiles: int = 0
    review: int = 0
    csv_rows: int = 0
    txt_rows: int = 0
    csv_unique: int = 0
    txt_unique: int = 0
    sequence_match: bool = False
    frame_mismatches: int = 0
    unaccounted: int = 0


class TransientSnapshotError(Exception):
    """An export snapshot could not be read from one stable file generation."""


def _snapshot_paths(out: Path) -> tuple[Path, ...]:
    return tuple(out / name for name in (
        CHECKPOINT.name, PROGRESS.name, CSV_PATH.name, TXT_PATH.name, REVIEW_PATH.name,
    ))


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, getattr(stat, "st_ino", 0)


def _read_snapshot_once(out: Path) -> dict:
    paths = _snapshot_paths(out)
    try:
        before = tuple(_file_signature(path) for path in paths)
        checkpoint = json.loads(paths[0].read_text(encoding="utf-8"))
        progress = json.loads(paths[1].read_text(encoding="utf-8"))
        with paths[2].open(encoding="utf-8-sig", newline="") as fp:
            csv_rows = list(csv.DictReader(fp))
        txt_ids = [line.strip() for line in paths[3].read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        with paths[4].open(encoding="utf-8-sig", newline="") as fp:
            review_rows = list(csv.DictReader(fp))
        after = tuple(_file_signature(path) for path in paths)
    except (OSError, ValueError, csv.Error) as exc:
        raise TransientSnapshotError(type(exc).__name__) from exc
    if before != after or any(signature is None for signature in after):
        raise TransientSnapshotError("GENERATION_CHANGED_DURING_READ")
    return {
        "checkpoint": checkpoint, "progress": progress, "csv_rows": csv_rows,
        "txt_ids": txt_ids, "review_rows": review_rows, "signature": after,
    }


def read_consistent_export_snapshot(out: Path = OUT, *, attempts: int = SNAPSHOT_ATTEMPTS) -> dict:
    """Retry transient reads and return only a stable multi-file snapshot."""
    last_error = "UNKNOWN"
    for attempt in range(attempts):
        try:
            return _read_snapshot_once(out)
        except TransientSnapshotError as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(SNAPSHOT_BACKOFF_SECONDS[min(attempt, len(SNAPSHOT_BACKOFF_SECONDS) - 1)])
    raise TransientSnapshotError(last_error)


def _validate_snapshot(snapshot: dict) -> ResumeCheck:
    checkpoint = snapshot["checkpoint"]
    progress = snapshot["progress"]
    csv_rows = snapshot["csv_rows"]
    txt_ids = snapshot["txt_ids"]
    review_rows = snapshot["review_rows"]

    ids = [row.get("wechat_id", "") for row in csv_rows]
    ids_valid = all(validate_wechat_id(value, DEFAULT_CONFIG).valid for value in ids)
    confirmed = int(checkpoint.get("confirmed_count", -1))
    progress_confirmed = int(progress.get("confirmed_count", -2))
    unique = int(checkpoint.get("unique_profiles_visited", 0))
    reviews = len(checkpoint.get("review", []))
    frame_mismatches = int(checkpoint.get("frame_mismatches", 0))
    unaccounted = int(checkpoint.get("result_accounting", {}).get("unaccounted", 0))
    sequence_match = ids == txt_ids
    csv_unique, txt_unique = len(set(ids)), len(set(txt_ids))
    hashes = set(checkpoint.get("confirmed_frame_hashes", {}))
    expected_hashes = {hashlib.sha256(value.encode("utf-8")).hexdigest() for value in ids}
    if confirmed < 1 or confirmed != progress_confirmed:
        reason = "CHECKPOINT_COUNT_MISMATCH"
    elif len(checkpoint.get("confirmed", [])) != confirmed:
        reason = "CHECKPOINT_CONFIRMED_ROWS_MISMATCH"
    elif len(ids) != confirmed or len(txt_ids) != confirmed:
        reason = "OUTPUT_ROW_COUNT_MISMATCH"
    elif csv_unique != confirmed or txt_unique != confirmed:
        reason = "DUPLICATE_OUTPUT_ID"
    elif not ids_valid:
        reason = "INVALID_OUTPUT_ID"
    elif not sequence_match:
        reason = "CSV_TXT_SEQUENCE_MISMATCH"
    elif hashes != expected_hashes:
        reason = "CONFIRMED_ID_HASH_MISMATCH"
    elif frame_mismatches or unaccounted:
        reason = "FRAME_OR_ACCOUNTING_INVARIANT_FAILED"
    elif reviews != len(review_rows):
        reason = "REVIEW_ROW_COUNT_MISMATCH"
    else:
        reason = "PASS"
    return ResumeCheck(
        safe=reason == "PASS", reason=reason, confirmed=confirmed,
        unique_profiles=unique, review=reviews,
        csv_rows=len(ids), txt_rows=len(txt_ids), csv_unique=csv_unique,
        txt_unique=txt_unique, sequence_match=sequence_match,
        frame_mismatches=frame_mismatches, unaccounted=unaccounted,
    )


def validate_resume(out: Path = OUT, *, persistent_attempts: int = 5) -> ResumeCheck:
    last: ResumeCheck | None = None
    for attempt in range(persistent_attempts):
        try:
            snapshot = read_consistent_export_snapshot(out)
        except TransientSnapshotError as exc:
            return ResumeCheck(False, f"TRANSIENT_OUTPUT_READ:{exc}")
        last = _validate_snapshot(snapshot)
        if last.safe:
            return last
        if attempt + 1 < persistent_attempts:
            time.sleep(SNAPSHOT_BACKOFF_SECONDS[min(attempt, len(SNAPSHOT_BACKOFF_SECONDS) - 1)])
    assert last is not None
    return ResumeCheck(**{**last.__dict__, "reason": f"PERSISTENT_OUTPUT_INCONSISTENCY:{last.reason}"})


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import psutil
            return psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _acquire_supervisor_lock(out: Path) -> Path:
    lock = out / LOCK_PATH.name
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                data = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
                malformed = False
            except (OSError, ValueError, TypeError):
                pid = 0
                malformed = True
            if _pid_alive(pid):
                raise RuntimeError("LIVE_SUPERVISOR_LOCK")
            if malformed:
                try:
                    if (time.time() - lock.stat().st_mtime) < 2.0:
                        raise RuntimeError("LIVE_SUPERVISOR_LOCK")
                except FileNotFoundError:
                    continue
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump({"pid": os.getpid(), "created_at": _utc_now()}, fp)
            fp.flush()
            os.fsync(fp.fileno())
        return lock
    raise RuntimeError("SUPERVISOR_LOCK_ACQUISITION_FAILED")


def _release_supervisor_lock(lock: Path) -> None:
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        if int(data.get("pid", -1)) == os.getpid():
            lock.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def _adopt_worker(pid: int, out: Path, heartbeat_timeout: float):
    age, live = _heartbeat_age(out / LIVE.name)
    state = str(live.get("state", ""))
    if not _pid_alive(pid) or live.get("pid") != pid or classify_worker_state(state) not in {"HEALTHY_ACTIVE", "HEALTHY_PAUSED"}:
        raise RuntimeError("ATTACH_WORKER_IDENTITY_MISMATCH")
    if age is None or age > heartbeat_timeout:
        raise RuntimeError("ATTACH_WORKER_HEARTBEAT_STALE")
    try:
        import psutil
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()).casefold()
        process_cwd = str(Path(process.cwd()).resolve()).casefold()
    except Exception as exc:
        raise RuntimeError("ATTACH_WORKER_PROCESS_INSPECTION_FAILED") from exc
    if "contact_exporter.fixed_geometry_export" not in command or "--resume" not in command:
        raise RuntimeError("ATTACH_WORKER_COMMAND_MISMATCH")
    if str(ROOT.resolve()).casefold() not in " ".join(process.cmdline()).casefold():
        raise RuntimeError("ATTACH_WORKER_ROOT_MISMATCH")
    if out.resolve() != (ROOT / "output" / "final").resolve() or process_cwd != str(ROOT.resolve()).casefold():
        raise RuntimeError("ATTACH_WORKER_OUTPUT_DIRECTORY_MISMATCH")
    return _AttachedWorker(pid, state)


class _AttachedWorker:
    def __init__(self, pid: int, state: str = "RUNNING"):
        self.pid = pid
        self.returncode = None
        self.state = state

    def poll(self):
        if _pid_alive(self.pid):
            return None
        self.returncode = 1
        return self.returncode


@dataclass
class RestartBudget:
    max_restarts: int = MAX_AUTOMATIC_RESTARTS
    max_same_count_failures: int = MAX_SAME_COUNT_FAILURES
    restart_count: int = 0
    restarts_since_progress: int = 0
    same_count_failures: int = 0
    last_progress: tuple[int, int] | None = None

    def record_progress(self, confirmed: int, unique_profiles: int) -> None:
        current = (confirmed, unique_profiles)
        if self.last_progress is None or current[0] > self.last_progress[0] or current[1] > self.last_progress[1]:
            self.restarts_since_progress = 0
            self.same_count_failures = 0
        self.last_progress = current

    def allow_restart(self, confirmed: int, unique_profiles: int) -> tuple[bool, str]:
        self.record_progress(confirmed, unique_profiles)
        self.same_count_failures += 1
        if self.same_count_failures >= self.max_same_count_failures:
            return False, "BLOCKED / REPEATED_PROCESS_FAILURE"
        if self.restarts_since_progress >= self.max_restarts:
            return False, "BLOCKED / MAX_AUTOMATIC_RESTARTS"
        self.restart_count += 1
        self.restarts_since_progress += 1
        return True, "RESTART_ALLOWED"


def _worker_command(target: int) -> list[str]:
    return ["-u", "-m", "contact_exporter.fixed_geometry_export", "--target", str(target), "--resume"]


def _venv_python(root: Path = ROOT) -> Path:
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.is_file() else Path(sys.executable)


def spawn_background_python(
    arguments: list[str], *, stdout_path: Path, stderr_path: Path,
    cwd: Path = ROOT, executable: Path | None = None,
) -> subprocess.Popen:
    if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
        raise TypeError("background Python arguments must be a list of strings")
    python = (executable or _venv_python(cwd)).resolve()
    command = [str(python), *arguments]
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("ab", buffering=0) as stdout_fp, stderr_path.open("ab", buffering=0) as stderr_fp:
        return subprocess.Popen(
            command, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
            stdout=stdout_fp, stderr=stderr_fp, creationflags=creationflags,
            startupinfo=startupinfo, close_fds=True,
        )


def _spawn_worker(target: int, *, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    return spawn_background_python(
        _worker_command(target),
        stdout_path=stdout_path, stderr_path=stderr_path, cwd=ROOT,
    )


def _heartbeat_age(live_path: Path = LIVE) -> tuple[float | None, dict]:
    try:
        live = json.loads(live_path.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(live["heartbeat_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
        return max(0.0, age), live
    except (OSError, ValueError, KeyError, TypeError):
        return None, {}


def _user32():
    if os.name != "nt":
        return None
    api = ctypes.windll.user32
    api.GetForegroundWindow.restype = ctypes.c_void_p
    api.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    api.GetWindowThreadProcessId.restype = ctypes.c_ulong
    api.IsWindow.argtypes = [ctypes.c_void_p]
    api.IsWindow.restype = ctypes.c_bool
    api.IsWindowVisible.argtypes = [ctypes.c_void_p]
    api.IsWindowVisible.restype = ctypes.c_bool
    api.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    api.ShowWindow.restype = ctypes.c_bool
    api.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    api.SetForegroundWindow.restype = ctypes.c_bool
    return api


def _window_owner_pid(hwnd: int) -> int:
    api = _user32()
    if api is None or not hwnd:
        return 0
    pid = ctypes.c_ulong()
    api.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _process_tree_contains(pid: int, roots: set[int]) -> bool:
    try:
        import psutil
        process = psutil.Process(pid)
        for node in [process, *process.parents()]:
            if node.pid in roots:
                return True
    except psutil.Error:
        return False
    return False


def _process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name().casefold()
    except psutil.Error:
        return ""


def is_exporter_owned_foreground(
    hwnd: int, *, worker_pids: set[int] | None = None, supervisor_pid: int | None = None,
) -> bool:
    """Return true only when the foreground window owner is in our process tree."""
    owner_pid = _window_owner_pid(hwnd)
    if not owner_pid:
        return False
    if _process_name(owner_pid) not in {
        "cmd.exe", "conhost.exe", "openconsole.exe", "python.exe", "pythonw.exe",
    }:
        return False
    roots = set(worker_pids or ())
    roots.add(os.getpid() if supervisor_pid is None else supervisor_pid)
    return _process_tree_contains(owner_pid, roots)


def _logged_weixin_window(out: Path) -> tuple[int, int] | None:
    log_path = out / "worker.stdout.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.startswith("TARGET_WINDOW "):
            continue
        try:
            payload = json.loads(line[len("TARGET_WINDOW "):])
            hwnd, pid = int(payload["hwnd"]), int(payload["pid"])
            import psutil
            api = _user32()
            if (
                api is not None and api.IsWindow(ctypes.c_void_p(hwnd))
                and api.IsWindowVisible(ctypes.c_void_p(hwnd))
                and _window_owner_pid(hwnd) == pid
                and any(token in psutil.Process(pid).name().casefold() for token in ("weixin", "wechat"))
            ):
                return hwnd, pid
        except (KeyError, TypeError, ValueError, OSError, ImportError):
            continue
        except Exception as exc:  # noqa: BLE001 - stale window handles fail closed
            try:
                import psutil
                if isinstance(exc, psutil.Error):
                    continue
            except ImportError:
                pass
            continue
    return None


def recover_self_console_foreground(
    *, worker_pids: set[int], supervisor_pid: int | None = None, out: Path = OUT,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Hide only our foreground process window, then restore the verified Weixin window."""
    api = _user32()
    if api is None:
        return "WINDOW_API_UNAVAILABLE"
    foreground = int(api.GetForegroundWindow() or 0)
    if not foreground:
        return "NO_FOREGROUND_WINDOW"
    target = _logged_weixin_window(out)
    if target is None:
        return "WEIXIN_WINDOW_UNVERIFIED"
    target_hwnd, _target_pid = target
    if foreground == target_hwnd:
        return "ALREADY_WEIXIN_FOREGROUND"
    if not is_exporter_owned_foreground(
        foreground, worker_pids=worker_pids, supervisor_pid=supervisor_pid,
    ):
        return "FOREGROUND_NOT_EXPORTER_OWNED"
    api.ShowWindow(ctypes.c_void_p(foreground), 0)
    api.ShowWindow(ctypes.c_void_p(target_hwnd), 9)
    api.SetForegroundWindow(ctypes.c_void_p(target_hwnd))
    sleep(0.2)
    return "WEIXIN_FOREGROUND_RESTORED" if int(api.GetForegroundWindow() or 0) == target_hwnd else "WEIXIN_FOREGROUND_DENIED"


def classify_worker_state(state: str) -> str:
    if state in HEALTHY_ACTIVE_STATES:
        return "HEALTHY_ACTIVE"
    if state in HEALTHY_PAUSED_STATES:
        return "HEALTHY_PAUSED"
    if state in TERMINAL_SUCCESS_STATES:
        return "TERMINAL_SUCCESS"
    if state in TERMINAL_BLOCKED_STATES:
        return "TERMINAL_BLOCKED"
    if state in {"UNHANDLED_EXCEPTION", "WORKER_CRASH", "WORKER_PROCESS_ERROR"}:
        return "PROCESS_FAILURE"
    return "UNKNOWN"


def _run_supervisor_locked(
    *, target: int = 150, root: Path = ROOT, out: Path = OUT,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
    max_restarts: int = MAX_AUTOMATIC_RESTARTS,
    max_same_count_failures: int = MAX_SAME_COUNT_FAILURES,
    max_cycles: int | None = None,
    spawn: Callable[..., subprocess.Popen] = _spawn_worker,
    attach_worker_pid: int | None = None,
) -> int:
    status_path = out / STATUS.name
    live_path = out / LIVE.name
    budget = RestartBudget(max_restarts, max_same_count_failures)
    worker = None
    worker_pid = None
    adopted_worker_pid = None
    last_exit_code = None
    last_exit_reason = None
    cycles = 0
    state = "STARTING"
    check = validate_resume(out)
    if not check.safe and not check.reason.startswith("TRANSIENT_OUTPUT_READ"):
        state = "CORRECTNESS_BLOCK"
        _atomic_json(status_path, {
            "supervisor_pid": os.getpid(), "worker_pid": None,
            "restart_count": 0, "state": state, "reason": check.reason,
            "heartbeat_at": _utc_now(), "confirmed_unique": check.confirmed,
        })
        return 2

    if attach_worker_pid is not None:
        worker = _adopt_worker(attach_worker_pid, out, heartbeat_timeout)
        worker_pid = attach_worker_pid
        adopted_worker_pid = attach_worker_pid

    def write_status(current_state: str, *, reason: str | None = None) -> None:
        age, live = _heartbeat_age(live_path)
        latest = validate_resume(out)
        _atomic_json(status_path, {
            "supervisor_pid": os.getpid(), "worker_pid": worker_pid,
            "worker_heartbeat": live.get("heartbeat_at"),
            "worker_heartbeat_age_seconds": round(age, 2) if age is not None else None,
            "restart_count": budget.restart_count,
            "restarts_since_progress": budget.restarts_since_progress,
            "last_exit_code": last_exit_code, "last_exit_reason": last_exit_reason,
            "confirmed_unique": latest.confirmed,
            "unique_profiles_visited": latest.unique_profiles,
            "review": latest.review, "state": current_state,
            "reason": reason, "heartbeat_at": _utc_now(),
            "adopted_worker_pid": adopted_worker_pid,
            "worker_state": live.get("state"),
            "worker_classification": classify_worker_state(str(live.get("state", ""))),
            "lock_acquired": True,
        })

    while True:
        cycles += 1
        check = validate_resume(out)
        if not check.safe:
            if check.reason.startswith("TRANSIENT_OUTPUT_READ"):
                state = "TRANSIENT_OUTPUT_READ"
                write_status(state, reason=check.reason)
                time.sleep(poll_interval)
                if max_cycles is not None and cycles >= max_cycles:
                    return 0
                continue
            state = "CORRECTNESS_BLOCK"
            write_status(state, reason=check.reason)
            return 2
        if check.confirmed >= target:
            if worker is not None and worker.poll() is None:
                state = "TARGET_REACHED_WAITING_FOR_WORKER_EXIT"
                write_status(state)
                while worker.poll() is None:
                    time.sleep(poll_interval)
                last_exit_code = worker.returncode
            final_check = validate_resume(out)
            if final_check.safe and final_check.confirmed == target:
                state = "PASS"
                write_status(state)
                return 0
            state = "CORRECTNESS_BLOCK"
            write_status(state, reason="FINAL_EXPORT_GATE_FAILED")
            return 2

        if worker is None:
            worker = spawn(
                target, stdout_path=out / "worker.stdout.log",
                stderr_path=out / "worker.stderr.log",
            )
            worker_pid = worker.pid
            _atomic_json(live_path, {
                "pid": worker_pid, "state": "STARTING", "heartbeat_at": _utc_now(),
                "confirmed_unique": check.confirmed,
                "unique_profiles_visited": check.unique_profiles,
                "review": check.review, "unaccounted": check.unaccounted,
            })
            state = "RUNNING"
            write_status(state)

        age, live = _heartbeat_age(live_path)
        if worker.poll() is None:
            if age is not None and age > heartbeat_timeout:
                if adopted_worker_pid is not None:
                    state = "TRANSIENT_WORKER_HEARTBEAT"
                    write_status(state, reason="ADOPTED_WORKER_HEARTBEAT_STALE")
                    time.sleep(poll_interval)
                    continue
                last_exit_reason = "WORKER_HEARTBEAT_STALE"
                try:
                    worker.terminate()
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
                last_exit_code = worker.returncode
            else:
                worker_class = classify_worker_state(str(live.get("state", "")))
                if worker_class == "HEALTHY_PAUSED":
                    state = "MONITORING_PAUSED_WORKER"
                    reason = None
                    if live.get("state") == "PAUSED_INPUT_FOREGROUND_REQUIRED":
                        owned_pids = {int(worker.pid)}
                        try:
                            owned_pids.add(int(live.get("pid", 0)))
                        except (TypeError, ValueError):
                            pass
                        recovery = recover_self_console_foreground(
                            worker_pids=owned_pids, supervisor_pid=os.getpid(), out=out,
                        )
                        if recovery == "WEIXIN_FOREGROUND_RESTORED":
                            reason = "SELF_INFLICTED_FOREGROUND_LOSS_RECOVERED"
                        elif recovery not in {"FOREGROUND_NOT_EXPORTER_OWNED", "ALREADY_WEIXIN_FOREGROUND"}:
                            reason = recovery
                elif worker_class in {"TERMINAL_BLOCKED", "PROCESS_FAILURE"}:
                    state = "BLOCKED / WORKER_TERMINAL_STATE"
                    write_status(state, reason=str(live.get("state")))
                    return 3
                else:
                    state = "RUNNING"
                write_status(state, reason=reason if worker_class == "HEALTHY_PAUSED" else None)
                time.sleep(poll_interval)
                if max_cycles is not None and cycles >= max_cycles:
                    return 0
                continue

        if worker.poll() is not None:
            last_exit_code = worker.returncode
            checkpoint = json.loads((out / CHECKPOINT.name).read_text(encoding="utf-8"))
            last_exit_reason = str(checkpoint.get("state", "UNKNOWN"))
            if last_exit_reason == "END_OF_CONTACT_LIST":
                state = "BLOCKED / END_OF_CONTACT_LIST"
                write_status(state)
                return 3
            if last_exit_reason in {"FATAL_CORRECTNESS_ERROR", "CORRECTNESS_BLOCK"}:
                state = "CORRECTNESS_BLOCK"
                write_status(state)
                return 2
            check = validate_resume(out)
            if not check.safe:
                state = "CORRECTNESS_BLOCK"
                write_status(state, reason=check.reason)
                return 2
            allowed, budget_state = budget.allow_restart(check.confirmed, check.unique_profiles)
            if not allowed:
                state = budget_state
                write_status(state, reason=last_exit_reason)
                return 4
            state = "RESTARTING"
            write_status(state, reason=last_exit_reason)
            worker = None
        if max_cycles is not None and cycles >= max_cycles:
            return 0


def run_supervisor(
    *, target: int = 150, root: Path = ROOT, out: Path = OUT,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
    max_restarts: int = MAX_AUTOMATIC_RESTARTS,
    max_same_count_failures: int = MAX_SAME_COUNT_FAILURES,
    max_cycles: int | None = None,
    spawn: Callable[..., subprocess.Popen] = _spawn_worker,
    attach_worker_pid: int | None = None,
) -> int:
    lock = _acquire_supervisor_lock(out)
    try:
        return _run_supervisor_locked(
            target=target, root=root, out=out, poll_interval=poll_interval,
            heartbeat_timeout=heartbeat_timeout, max_restarts=max_restarts,
            max_same_count_failures=max_same_count_failures,
            max_cycles=max_cycles, spawn=spawn,
            attach_worker_pid=attach_worker_pid,
        )
    finally:
        _release_supervisor_lock(lock)


def _detach(*, target: int = 150, attach_worker_pid: int | None = None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stdout_path = OUT / "supervisor.stdout.log"
    stderr_path = OUT / "supervisor.stderr.log"
    arguments = ["-u", "-m", "contact_exporter.export_supervisor", "--run", "--target", str(target)]
    if attach_worker_pid is not None:
        arguments.extend(["--attach-worker", str(attach_worker_pid)])
    process = spawn_background_python(
        arguments, stdout_path=stdout_path, stderr_path=stderr_path, cwd=ROOT,
    )
    print(json.dumps({"supervisor_pid": process.pid, "detached": True, "attach_worker_pid": attach_worker_pid}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise the resumable WeChat contact export worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--detach", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--target", type=int, default=150)
    parser.add_argument("--attach-worker", type=int, default=None)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--max-cycles", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.target < 1:
        parser.error("--target must be positive")
    if args.detach:
        return _detach(target=args.target, attach_worker_pid=args.attach_worker)
    try:
        return run_supervisor(
            target=args.target, poll_interval=args.poll_interval,
            max_cycles=args.max_cycles, attach_worker_pid=args.attach_worker,
        )
    except KeyboardInterrupt:
        _atomic_json(STATUS, {
            "supervisor_pid": os.getpid(), "state": "USER_STOPPED",
            "heartbeat_at": _utc_now(),
        })
        return 130
    except Exception as exc:  # noqa: BLE001 - supervisor failure is persisted without exception text
        OUT.mkdir(parents=True, exist_ok=True)
        if str(exc) != "LIVE_SUPERVISOR_LOCK":
            _atomic_json(STATUS, {
                "supervisor_pid": os.getpid(), "state": "PROCESS_ERROR",
                "exception_type": type(exc).__name__, "heartbeat_at": _utc_now(),
            })
        print(f"SUPERVISOR_PROCESS_ERROR exception_type={type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
