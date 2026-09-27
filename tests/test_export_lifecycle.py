import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from contact_exporter import export_supervisor as supervisor
from contact_exporter import fixed_geometry_export as worker


def _write_resume_fixture(out: Path, count: int = 71, unique: int = 90) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ids = [f"contactid{index:04d}" for index in range(count)]
    frame_hashes = {hashlib.sha256(value.encode()).hexdigest(): f"frame-{index}" for index, value in enumerate(ids)}
    checkpoint = {
        "state": "PAUSED_RECOVERY_REQUIRED", "confirmed_count": count,
        "confirmed": [{"row_ordinal": index + 1, "wechat_id_masked": "co*****id"} for index in range(count)],
        "confirmed_frame_hashes": frame_hashes, "unique_profiles_visited": unique,
        "review": [{"reason": "LOW_CONFIDENCE"}, {"reason": "OCR_FAILED"}],
        "frame_mismatches": 0, "result_accounting": {"unaccounted": 0},
    }
    (out / "fixed_geometry_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    (out / "wechat_contacts_150.progress.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    with (out / "wechat_contacts_150.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=worker.FORMAL_FIELDS)
        writer.writeheader()
        for index, value in enumerate(ids):
            writer.writerow({"row_ordinal": index + 1, "wechat_id": value,
                             "status": "CONFIRMED_BY_CONSENSUS", "source": "test", "confidence": "1.0"})
    (out / "wechat_contacts_150.txt").write_text("".join(value + "\n" for value in ids), encoding="utf-8-sig")
    with (out / "wechat_contacts_150_review.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=worker.REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows([
            {"sequence_index": 1, "reason": "LOW_CONFIDENCE"},
            {"sequence_index": 2, "reason": "OCR_FAILED"},
        ])


def test_resume_validator_keeps_71_and_requires_exact_order(tmp_path):
    out = tmp_path / "final"
    _write_resume_fixture(out)
    check = supervisor.validate_resume(out)
    assert check.safe
    assert (check.confirmed, check.unique_profiles, check.review) == (71, 90, 2)
    assert (check.csv_rows, check.txt_rows, check.csv_unique, check.txt_unique) == (71, 71, 71, 71)
    assert check.sequence_match


def test_resume_validator_blocks_output_mismatch(tmp_path):
    out = tmp_path / "final"
    _write_resume_fixture(out)
    (out / "wechat_contacts_150.txt").write_text("wrong\n", encoding="utf-8-sig")
    check = supervisor.validate_resume(out)
    assert not check.safe
    assert check.reason == "PERSISTENT_OUTPUT_INCONSISTENCY:OUTPUT_ROW_COUNT_MISMATCH"


def test_restart_budget_stops_three_failures_without_progress():
    budget = supervisor.RestartBudget(max_restarts=5, max_same_count_failures=3)
    assert budget.allow_restart(71, 90) == (True, "RESTART_ALLOWED")
    assert budget.allow_restart(71, 90) == (True, "RESTART_ALLOWED")
    assert budget.allow_restart(71, 90) == (False, "BLOCKED / REPEATED_PROCESS_FAILURE")


def test_restart_budget_resets_after_progress():
    budget = supervisor.RestartBudget(max_restarts=5, max_same_count_failures=3)
    budget.allow_restart(71, 90)
    assert budget.allow_restart(72, 91) == (True, "RESTART_ALLOWED")
    assert budget.same_count_failures == 1


def test_worker_transient_navigation_return_retries_from_checkpoint(tmp_path, monkeypatch):
    checkpoint = tmp_path / "fixed_geometry_checkpoint.json"
    checkpoint.write_text(json.dumps({
        "state": "FORWARD_VIEWPORT_UNSCANNED", "confirmed_count": 71,
        "unique_profiles_visited": 90,
    }), encoding="utf-8")
    monkeypatch.setattr(worker, "CHECKPOINT", checkpoint)
    reasons = []
    monkeypatch.setattr(worker, "_pause_for_recovery", lambda reason, count: reasons.append((reason, count)))
    calls = []

    def fake_run(target, *, resume, ready_confirmed):
        calls.append((target, resume, ready_confirmed))
        return 3 if len(calls) == 1 else 0

    monkeypatch.setattr(worker, "run", fake_run)
    assert worker._run_with_recovery(SimpleNamespace(target=150, resume=True, ready_confirmed=False)) == 0
    assert calls == [(150, True, False), (150, True, False)]
    assert reasons == [("FORWARD_VIEWPORT_UNSCANNED", 1)]


def test_worker_recovery_marks_pause_without_changing_counts(tmp_path, monkeypatch):
    checkpoint = tmp_path / "fixed_geometry_checkpoint.json"
    progress = tmp_path / "progress.json"
    live = tmp_path / "live.json"
    base = {"state": "FORWARD_VIEWPORT_UNSCANNED", "confirmed_count": 71,
            "unique_profiles_visited": 90, "review": [{}, {}]}
    checkpoint.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(worker, "CHECKPOINT", checkpoint)
    monkeypatch.setattr(worker, "PROGRESS", progress)
    monkeypatch.setattr(worker, "LIVE", live)
    worker._pause_for_recovery("FORWARD_VIEWPORT_UNSCANNED", 1)
    result = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result["state"] == "PAUSED_RECOVERY_REQUIRED"
    assert (result["confirmed_count"], result["unique_profiles_visited"], len(result["review"])) == (71, 90, 2)


def test_worker_exception_record_is_sanitized_and_checkpointed(tmp_path, monkeypatch):
    out = tmp_path / "final"
    out.mkdir(parents=True)
    checkpoint = out / "fixed_geometry_checkpoint.json"
    progress = out / "wechat_contacts_150.progress.json"
    live = out / "fixed_geometry_live_status.json"
    payload = {"state": "RUNNING", "confirmed_count": 71, "confirmed": [{"wechat_id_masked": "co*****id"}]}
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(worker, "OUT", out)
    monkeypatch.setattr(worker, "CHECKPOINT", checkpoint)
    monkeypatch.setattr(worker, "PROGRESS", progress)
    monkeypatch.setattr(worker, "LIVE", live)
    monkeypatch.setattr(worker, "_verify_resume_outputs", lambda: {"consistent": True})
    worker._record_process_error(ValueError("PRIVATE_CONTACT_VALUE"))
    for path in (checkpoint, progress, out / "worker_crash.json", live):
        content = path.read_text(encoding="utf-8")
        assert "PRIVATE_CONTACT_VALUE" not in content
    crash = json.loads((out / "worker_crash.json").read_text(encoding="utf-8"))
    assert crash["category"] == "UNHANDLED_EXCEPTION"
    assert crash["exception_type"] == "ValueError"


def test_worker_heartbeat_updates_live_status_every_two_seconds(tmp_path, monkeypatch):
    out = tmp_path / "final"
    out.mkdir()
    checkpoint = out / "fixed_geometry_checkpoint.json"
    live = out / "fixed_geometry_live_status.json"
    checkpoint.write_text(json.dumps({
        "state": "RUNNING", "confirmed_count": 71, "confirmed": [{}] * 71,
        "unique_profiles_visited": 90, "review": [{}, {}], "result_accounting": {"unaccounted": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(worker, "CHECKPOINT", checkpoint)
    monkeypatch.setattr(worker, "LIVE", live)

    class StopAfterOneTick:
        calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    worker._heartbeat_loop(StopAfterOneTick())
    status = json.loads(live.read_text(encoding="utf-8"))
    assert status["state"] == "RUNNING"
    assert status["confirmed_unique"] == 71
    assert status["unique_profiles_visited"] == 90
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["heartbeat_at"])).total_seconds()
    assert age < 2


def test_atomic_json_has_no_partial_temp_file(tmp_path):
    path = tmp_path / "checkpoint.json"
    worker._atomic_json(path, {"confirmed_count": 71})
    assert json.loads(path.read_text(encoding="utf-8")) == {"confirmed_count": 71}
    assert list(tmp_path.glob("*.tmp")) == []


def test_worker_command_always_resumes_existing_export():
    command = supervisor._worker_command(150)
    assert supervisor._venv_python().name.casefold() == "python.exe"
    assert command[-1] == "--resume"
    assert "--target" in command
    assert "activate.bat" not in " ".join(command).casefold()


def test_background_python_uses_no_window_direct_venv_and_file_logs(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=8111)

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    stdout = tmp_path / "worker_stdout.log"
    stderr = tmp_path / "worker_stderr.log"
    process = supervisor.spawn_background_python(
        ["-u", "-m", "contact_exporter.fixed_geometry_export", "--target", "150", "--resume"],
        stdout_path=stdout, stderr_path=stderr, cwd=supervisor.ROOT,
    )
    kwargs = captured["kwargs"]
    flags = kwargs["creationflags"]
    assert process.pid == 8111
    assert captured["command"][0] == str(supervisor._venv_python().resolve())
    assert "activate.bat" not in " ".join(captured["command"]).casefold()
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == supervisor.subprocess.DEVNULL
    assert Path(kwargs["stdout"].name) == stdout
    assert Path(kwargs["stderr"].name) == stderr
    assert flags & getattr(supervisor.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert not flags & getattr(supervisor.subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    assert kwargs["startupinfo"].dwFlags & supervisor.subprocess.STARTF_USESHOWWINDOW
    assert kwargs["startupinfo"].wShowWindow == supervisor.subprocess.SW_HIDE


def test_detached_supervisor_uses_shared_no_window_launcher(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(supervisor, "ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "OUT", tmp_path / "output" / "final")

    def fake_spawn(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=1888)

    monkeypatch.setattr(supervisor, "spawn_background_python", fake_spawn)
    assert supervisor._detach(target=150, attach_worker_pid=45740) == 0
    assert "contact_exporter.export_supervisor" in captured["arguments"]
    assert "--run" in captured["arguments"]
    assert captured["arguments"][-2:] == ["--attach-worker", "45740"]
    assert captured["kwargs"]["stdout_path"].name == "supervisor.stdout.log"
    assert captured["kwargs"]["stderr_path"].name == "supervisor.stderr.log"


def test_background_dummy_process_survives_without_visible_console(tmp_path):
    import pytest
    if os.name != "nt":
        pytest.skip("Windows no-window lifecycle verification")
    stdout = tmp_path / "dummy.stdout.log"
    stderr = tmp_path / "dummy.stderr.log"
    process = supervisor.spawn_background_python(
        ["-c", "import time; time.sleep(5)"], stdout_path=stdout,
        stderr_path=stderr, cwd=supervisor.ROOT,
    )
    try:
        import psutil
        import win32gui
        import win32process
        assert process.poll() is None
        owned = {process.pid}
        try:
            owned.update(child.pid for child in psutil.Process(process.pid).children(recursive=True))
        except psutil.Error:
            pass
        visible_owned_windows = []

        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in owned:
                    visible_owned_windows.append(hwnd)
            return True

        win32gui.EnumWindows(collect, None)
        assert visible_owned_windows == []
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_exporter_owned_foreground_requires_process_tree_ownership(monkeypatch):
    observed = {}
    monkeypatch.setattr(supervisor, "_window_owner_pid", lambda hwnd: 6001 if hwnd == 99 else 7001)
    monkeypatch.setattr(supervisor, "_process_name", lambda pid: "conhost.exe" if pid == 6001 else "code.exe")

    def tree_contains(pid, roots):
        observed["pid"] = pid
        observed["roots"] = roots
        return pid == 6001 and 5001 in roots and pid == 6001

    monkeypatch.setattr(supervisor, "_process_tree_contains", tree_contains)
    assert supervisor.is_exporter_owned_foreground(99, worker_pids={5001}, supervisor_pid=5002)
    assert not supervisor.is_exporter_owned_foreground(100, worker_pids={5001}, supervisor_pid=5002)
    assert observed["roots"] == {5001, 5002}


def test_user_app_is_not_exporter_owned_even_if_window_is_foreground(monkeypatch):
    monkeypatch.setattr(supervisor, "_window_owner_pid", lambda _hwnd: 6001)
    monkeypatch.setattr(supervisor, "_process_name", lambda _pid: "code.exe")
    monkeypatch.setattr(supervisor, "_process_tree_contains", lambda *_args: True)
    assert not supervisor.is_exporter_owned_foreground(99, worker_pids={5001}, supervisor_pid=5002)


def test_self_console_is_hidden_and_verified_weixin_restored(tmp_path, monkeypatch):
    actions = []
    current = {"hwnd": 101}
    api = SimpleNamespace(
        GetForegroundWindow=lambda: current["hwnd"],
        ShowWindow=lambda hwnd, cmd: actions.append((int(hwnd.value), cmd)) or True,
        SetForegroundWindow=lambda hwnd: current.update(hwnd=int(hwnd.value)) or True,
    )
    monkeypatch.setattr(supervisor, "_user32", lambda: api)
    monkeypatch.setattr(supervisor, "_logged_weixin_window", lambda _out: (202, 9001))
    monkeypatch.setattr(supervisor, "is_exporter_owned_foreground", lambda *_args, **_kwargs: True)
    result = supervisor.recover_self_console_foreground(
        worker_pids={8001}, supervisor_pid=8002, out=tmp_path,
        sleep=lambda _seconds: None,
    )
    assert result == "WEIXIN_FOREGROUND_RESTORED"
    assert actions == [(101, 0), (202, 9)]
    assert current["hwnd"] == 202


def test_user_application_foreground_is_never_hidden_or_stolen(tmp_path, monkeypatch):
    for foreground_owner in ("Code.exe", "chrome.exe", "explorer.exe", "powershell.exe"):
        actions = []
        api = SimpleNamespace(
            GetForegroundWindow=lambda: 101,
            ShowWindow=lambda hwnd, cmd, actions=actions: actions.append(("hide", int(hwnd.value), cmd)) or True,
            SetForegroundWindow=lambda hwnd, actions=actions: actions.append(("focus", int(hwnd.value))) or True,
        )
        monkeypatch.setattr(supervisor, "_user32", lambda api=api: api)
        monkeypatch.setattr(supervisor, "_logged_weixin_window", lambda _out: (202, 9001))
        monkeypatch.setattr(supervisor, "is_exporter_owned_foreground", lambda *_a, **_k: False)
        result = supervisor.recover_self_console_foreground(
            worker_pids={8001}, supervisor_pid=8002, out=tmp_path,
        )
        assert result == "FOREGROUND_NOT_EXPORTER_OWNED", foreground_owner
        assert actions == [], foreground_owner


def test_supervisor_restarts_safe_nonzero_worker_then_bounds_repeat_failures(tmp_path):
    out = tmp_path / "final"
    _write_resume_fixture(out)
    launches = []

    class ExitedWorker:
        pid = 4321
        returncode = 3

        @staticmethod
        def poll():
            return 3

    def spawn(target, *, stdout_path, stderr_path):
        launches.append((target, stdout_path.name, stderr_path.name))
        return ExitedWorker()

    result = supervisor.run_supervisor(
        target=150, out=out, poll_interval=0, spawn=spawn,
    )
    assert result == 4
    assert len(launches) == 3
    status = json.loads((out / "supervisor_status.json").read_text(encoding="utf-8"))
    assert status["state"] == "BLOCKED / REPEATED_PROCESS_FAILURE"
    assert status["confirmed_unique"] == 71


def test_supervisor_stops_at_150_only_after_output_gate(tmp_path):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=150, unique=180)

    def no_spawn(*_args, **_kwargs):
        raise AssertionError("completed target must not start another worker")

    assert supervisor.run_supervisor(target=150, out=out, spawn=no_spawn) == 0
    status = json.loads((out / "supervisor_status.json").read_text(encoding="utf-8"))
    assert status["state"] == "PASS"
    assert status["confirmed_unique"] == 150


def test_foreground_wait_keeps_worker_alive(monkeypatch):
    states = []
    checks = iter([False, True])
    monkeypatch.setattr(worker, "_foreground_ready", lambda _hwnd, _pid: next(checks))
    monkeypatch.setattr(worker, "_persist", lambda state: states.append(state["state"]))
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)
    state = {"state": "RUNNING"}
    worker._wait_foreground(1, 1, state)
    assert states == ["PAUSED_INPUT_FOREGROUND_REQUIRED", "RUNNING"]


def test_supervisor_heartbeat_reader_reports_fresh_timestamp(tmp_path):
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"heartbeat_at": datetime.now(timezone.utc).isoformat(), "state": "RUNNING"}), encoding="utf-8")
    age, status = supervisor._heartbeat_age(live)
    assert age is not None and age < 2
    assert status["state"] == "RUNNING"


def test_snapshot_retries_temporary_file_not_found(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    original = supervisor._read_snapshot_once
    calls = 0

    def once_then_success(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise supervisor.TransientSnapshotError("FileNotFoundError")
        return original(path)

    monkeypatch.setattr(supervisor, "_read_snapshot_once", once_then_success)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _delay: None)
    check = supervisor.validate_resume(out)
    assert check.safe and check.confirmed == 91
    assert calls == 2


def test_snapshot_retries_permission_error(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    original = supervisor._read_snapshot_once
    calls = 0

    def once_then_success(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise supervisor.TransientSnapshotError("PermissionError")
        return original(path)

    monkeypatch.setattr(supervisor, "_read_snapshot_once", once_then_success)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _delay: None)
    snapshot = supervisor.read_consistent_export_snapshot(out)
    assert len(snapshot["csv_rows"]) == 91
    assert calls == 2


def test_snapshot_discards_file_generation_change_then_accepts_stable_91(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    original = supervisor._file_signature
    calls = 0

    def change_first_generation(path):
        nonlocal calls
        value = original(path)
        if path.name == "fixed_geometry_checkpoint.json":
            calls += 1
            if calls == 2:
                return (*value[:3], value[3] + 1)
        return value

    monkeypatch.setattr(supervisor, "_file_signature", change_first_generation)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _delay: None)
    snapshot = supervisor.read_consistent_export_snapshot(out)
    assert len(snapshot["csv_rows"]) == 91
    assert calls >= 4


def test_repeated_stable_mismatch_becomes_persistent_block(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    txt = out / "wechat_contacts_150.txt"
    txt.write_text("".join(f"contactid{index:04d}\n" for index in range(90)), encoding="utf-8-sig")
    monkeypatch.setattr(supervisor, "SNAPSHOT_BACKOFF_SECONDS", (0,))
    check = supervisor.validate_resume(out, persistent_attempts=3)
    assert not check.safe
    assert check.reason == "PERSISTENT_OUTPUT_INCONSISTENCY:OUTPUT_ROW_COUNT_MISMATCH"


def test_adopt_worker_requires_live_pid_and_fresh_matching_heartbeat(tmp_path, monkeypatch):
    pid = 45904
    root = tmp_path
    out = root / "output" / "final"
    out.mkdir(parents=True)
    monkeypatch.setattr(supervisor, "ROOT", root)
    (out / "fixed_geometry_live_status.json").write_text(json.dumps({
        "pid": pid, "state": "RUNNING", "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    fake_proc = SimpleNamespace(
        cmdline=lambda: ["python", str(supervisor.ROOT / ".venv" / "Scripts" / "python.exe"),
                         "-m", "contact_exporter.fixed_geometry_export", "--target", "150", "--resume"],
        exe=lambda: str(supervisor.ROOT / ".venv" / "Scripts" / "python.exe"),
        cwd=lambda: str(supervisor.ROOT),
    )
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda _pid: fake_proc))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)
    adopted = supervisor._adopt_worker(pid, out, 12)
    assert adopted.pid == pid


def test_adopt_worker_allows_active_and_recoverable_paused_states(tmp_path, monkeypatch):
    pid = 45904
    root = tmp_path
    out = root / "output" / "final"
    out.mkdir(parents=True)
    monkeypatch.setattr(supervisor, "ROOT", root)
    fake_proc = SimpleNamespace(
        cmdline=lambda: [str(root / ".venv" / "Scripts" / "python.exe"), "-m",
                         "contact_exporter.fixed_geometry_export", "--target", "150", "--resume"],
        cwd=lambda: str(root),
    )
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda _pid: fake_proc))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)
    for state in ("RUNNING", "PAUSED_INPUT_FOREGROUND_REQUIRED", "PAUSED_FOREGROUND_LOST"):
        (out / "fixed_geometry_live_status.json").write_text(json.dumps({
            "pid": pid, "state": state, "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
        adopted = supervisor._adopt_worker(pid, out, 12)
        assert adopted.pid == pid
        assert adopted.state == state
    assert supervisor.classify_worker_state("WAITING_INPUT_FOREGROUND") == "HEALTHY_PAUSED"
    assert supervisor.classify_worker_state("PAUSED_RECOVERY_REQUIRED") == "HEALTHY_PAUSED"


def test_adopt_worker_rejects_stale_dead_and_terminal_workers(tmp_path, monkeypatch):
    import pytest

    pid = 45904
    root = tmp_path
    out = root / "output" / "final"
    out.mkdir(parents=True)
    monkeypatch.setattr(supervisor, "ROOT", root)
    fake_proc = SimpleNamespace(
        cmdline=lambda: [str(root / ".venv" / "Scripts" / "python.exe"), "-m",
                         "contact_exporter.fixed_geometry_export", "--target", "150", "--resume"],
        cwd=lambda: str(root),
    )
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda _pid: fake_proc))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)
    live = out / "fixed_geometry_live_status.json"
    live.write_text(json.dumps({"pid": pid, "state": "RUNNING", "heartbeat_at": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ATTACH_WORKER_HEARTBEAT_STALE"):
        supervisor._adopt_worker(pid, out, 12)
    live.write_text(json.dumps({"pid": pid, "state": "FATAL_CORRECTNESS_ERROR",
                                "heartbeat_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ATTACH_WORKER_IDENTITY_MISMATCH"):
        supervisor._adopt_worker(pid, out, 12)
    monkeypatch.setattr(supervisor, "_pid_alive", lambda _value: False)
    live.write_text(json.dumps({"pid": pid, "state": "RUNNING",
                                "heartbeat_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ATTACH_WORKER_IDENTITY_MISMATCH"):
        supervisor._adopt_worker(pid, out, 12)


def test_attach_worker_does_not_spawn_or_overwrite_live_status(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    pid = 45904
    live_path = out / "fixed_geometry_live_status.json"
    live = {"pid": pid, "state": "PAUSED_INPUT_FOREGROUND_REQUIRED",
            "heartbeat_at": datetime.now(timezone.utc).isoformat()}
    live_path.write_text(json.dumps(live), encoding="utf-8")
    before = live_path.read_bytes()
    monkeypatch.setattr(supervisor, "_adopt_worker", lambda *_args: supervisor._AttachedWorker(pid))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("adopting worker must not start a second worker")

    assert supervisor.run_supervisor(out=out, attach_worker_pid=pid, max_cycles=1,
                                     poll_interval=0, spawn=forbidden_spawn) == 0
    assert live_path.read_bytes() == before
    status = json.loads((out / "supervisor_status.json").read_text(encoding="utf-8"))
    assert status["worker_pid"] == pid
    assert status["adopted_worker_pid"] == pid
    assert status["state"] == "MONITORING_PAUSED_WORKER"


def test_paused_worker_is_monitored_for_thirty_seconds_without_restart(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    pid = 45904
    live_path = out / "fixed_geometry_live_status.json"
    live_path.write_text(json.dumps({"pid": pid, "state": "PAUSED_INPUT_FOREGROUND_REQUIRED",
                                    "heartbeat_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    monkeypatch.setattr(supervisor, "_adopt_worker", lambda *_args: supervisor._AttachedWorker(pid, "PAUSED_INPUT_FOREGROUND_REQUIRED"))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)
    elapsed = []
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: elapsed.append(seconds))
    launches = []

    def forbidden_spawn(*_args, **_kwargs):
        launches.append(True)
        raise AssertionError("paused adopted worker must not trigger a new worker")

    assert supervisor.run_supervisor(out=out, attach_worker_pid=pid, max_cycles=20,
                                     poll_interval=1.5, spawn=forbidden_spawn) == 0
    assert sum(elapsed) == 30
    assert launches == []
    status = json.loads((out / "supervisor_status.json").read_text(encoding="utf-8"))
    assert status["state"] == "MONITORING_PAUSED_WORKER"


def test_paused_worker_can_transition_to_running_without_reattach(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    pid = 45904
    monkeypatch.setattr(supervisor, "_adopt_worker", lambda *_args: supervisor._AttachedWorker(pid))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda value: value == pid)
    states = iter(["PAUSED_INPUT_FOREGROUND_REQUIRED"] * 2 + ["RUNNING"] * 12)
    monkeypatch.setattr(supervisor, "_heartbeat_age", lambda _path: (0.1, {
        "pid": pid, "state": next(states, "RUNNING"),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    status_states = []
    original_atomic_json = supervisor._atomic_json

    def capture_status(path, payload):
        if path.name == "supervisor_status.json":
            status_states.append(payload["state"])
        return original_atomic_json(path, payload)

    monkeypatch.setattr(supervisor, "_atomic_json", capture_status)
    assert supervisor.run_supervisor(out=out, attach_worker_pid=pid, max_cycles=2,
                                     poll_interval=0) == 0
    assert "MONITORING_PAUSED_WORKER" in status_states
    assert "RUNNING" in status_states


def test_supervisor_lock_recovers_dead_pid_and_refuses_live_pid(tmp_path, monkeypatch):
    out = tmp_path / "final"
    out.mkdir()
    lock = out / "supervisor.lock"
    lock.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: pid == 99999)
    acquired = supervisor._acquire_supervisor_lock(out)
    assert json.loads(acquired.read_text(encoding="utf-8"))["pid"] == supervisor.os.getpid()
    supervisor._release_supervisor_lock(acquired)
    lock.write_text(json.dumps({"pid": 99999}), encoding="utf-8")
    try:
        supervisor._acquire_supervisor_lock(out)
    except RuntimeError as exc:
        assert str(exc) == "LIVE_SUPERVISOR_LOCK"
    else:
        raise AssertionError("live supervisor lock must refuse a duplicate")


def test_pid_alive_uses_windows_process_inspection():
    assert supervisor._pid_alive(os.getpid())
    assert not supervisor._pid_alive(2_147_483_647)


def test_transient_monitor_read_does_not_stop_supervisor(tmp_path, monkeypatch):
    out = tmp_path / "final"
    _write_resume_fixture(out, count=91, unique=110)
    original = supervisor.validate_resume
    calls = 0

    def transient_then_stable(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return supervisor.ResumeCheck(False, "TRANSIENT_OUTPUT_READ:PermissionError")
        return original(path)

    monkeypatch.setattr(supervisor, "validate_resume", transient_then_stable)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _delay: None)
    launches = []

    class ActiveWorker:
        pid = 50001
        returncode = None

        @staticmethod
        def poll():
            return None

    def spawn(*_args, **_kwargs):
        launches.append(True)
        return ActiveWorker()

    assert supervisor.run_supervisor(out=out, max_cycles=2, poll_interval=0, spawn=spawn) == 0
    assert calls >= 3
    assert launches == [True]
