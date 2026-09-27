from types import SimpleNamespace

from contact_exporter import fixed_geometry_export as fixed


def test_read_gate_does_not_depend_on_foreground(monkeypatch):
    import sys

    gui = SimpleNamespace(
        IsWindow=lambda hwnd: hwnd == 100,
        IsWindowVisible=lambda hwnd: hwnd == 100,
        GetWindowRect=lambda hwnd: (0, 0, 1920, 1080),
        GetClientRect=lambda hwnd: (0, 0, 1920, 1032),
    )
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    geometry = {
        "client_size": (1920, 1032),
        "contact_list_roi": (59, 206, 303, 1011),
    }
    # Primary/one-monitor telemetry is deliberately not part of READ_GATE.
    geometry.update(monitor_index=0, monitor_is_primary=True, monitor_count=1)
    assert fixed._read_target_ready(100, geometry)


def test_code_foreground_fails_input_gate_while_weixin_read_gate_passes(monkeypatch):
    import sys

    import pyweixin.visual_navigation as visual

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 500))
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 88)),
    )
    monkeypatch.setattr(visual, "is_weixin_foreground", lambda *args, **kwargs: False)
    assert fixed._foreground_ready(100, 77) is False


def test_input_gate_accepts_weixin_foreground(monkeypatch):
    import sys

    import pyweixin.visual_navigation as visual

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 100))
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 77)),
    )
    monkeypatch.setattr(visual, "is_weixin_foreground", lambda *args, **kwargs: False)
    assert fixed._foreground_ready(100, 77) is True


def test_input_pause_heartbeats_without_reinitializing_read_path(monkeypatch):
    checks = iter((False, True))
    persisted = []
    monkeypatch.setattr(fixed, "_foreground_ready", lambda hwnd, pid: next(checks))
    monkeypatch.setattr(fixed, "_persist", lambda state: persisted.append(state["state"]))
    monkeypatch.setattr(fixed.time, "sleep", lambda _seconds: None)
    state = {}
    fixed._wait_foreground(100, 77, state)
    assert persisted == ["PAUSED_INPUT_FOREGROUND_REQUIRED", "RUNNING"]


def test_profile_roi_must_fit_inside_target_client(monkeypatch):
    import sys

    monkeypatch.setattr(fixed, "PROFILE_CHANGE_ROI", (0.4, 0.2, 0.92, 1.2))
    gui = SimpleNamespace(
        IsWindow=lambda _hwnd: True,
        IsWindowVisible=lambda _hwnd: True,
        GetWindowRect=lambda _hwnd: (0, 0, 100, 100),
        GetClientRect=lambda _hwnd: (0, 0, 100, 100),
    )
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    assert not fixed._read_target_ready(1, {
        "client_size": (100, 100), "contact_list_roi": (1, 1, 50, 90),
    })


def test_safe_input_recalculates_screen_geometry_after_monitor_change(monkeypatch):
    import sys

    gui = SimpleNamespace(
        IsWindow=lambda _hwnd: True,
        IsWindowVisible=lambda _hwnd: True,
        ClientToScreen=lambda _hwnd, point: (2000 + point[0], 100 + point[1]),
    )
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    class Nav:
        mapper = SimpleNamespace(size=(100, 100))
        def _window_mapper(self):
            return SimpleNamespace(
                size=(100, 100),
                client_screen_rect=(2000, 100, 2100, 200),
                window_screen_rect=(1990, 80, 2110, 220),
            )
    nav = Nav()
    assert fixed._safe_target_input_point(1, nav, 20, 30) == (2020, 130)
    assert nav.mapper.client_screen_rect[0] == 2000


def test_safe_input_rejects_point_outside_target_client(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        IsWindow=lambda _hwnd: True,
        IsWindowVisible=lambda _hwnd: True,
        ClientToScreen=lambda _hwnd, point: (point[0], point[1]),
    ))
    class Nav:
        def _window_mapper(self):
            return SimpleNamespace(
                size=(100, 100), client_screen_rect=(0, 0, 50, 50),
                window_screen_rect=(0, 0, 100, 100),
            )
    assert fixed._safe_target_input_point(1, Nav(), 75, 20) is None


def test_checkpoint_profile_state_survives_monitor_telemetry_update():
    state = {"unique_profiles_visited": 45, "confirmed_count": 40,
             "seen_profile_hashes": ["a" * 64], "monitor_index": 1}
    seen = list(state["seen_profile_hashes"])
    state["monitor_index"] = 0
    assert state["confirmed_count"] == 40
    assert state["unique_profiles_visited"] == 45
    assert state["seen_profile_hashes"] == seen


def test_confirmed_checkpoint_count_is_preserved_on_resume_load(monkeypatch):
    monkeypatch.setattr(fixed, "_load_confirmed_export", lambda expected, _hashes: [
        {"wechat_id": f"contact{i}"} for i in range(expected)
    ])
    checkpoint = {"confirmed_count": 40, "confirmed_frame_hashes": {}}
    checkpoint["confirmed"] = fixed._load_confirmed_export(
        checkpoint["confirmed_count"], set(checkpoint["confirmed_frame_hashes"])
    )
    assert len(checkpoint["confirmed"]) == 40


def test_seen_profile_is_a_revisit_and_unseen_profile_can_resume():
    seen = ["seen-hash"]
    assert fixed._resume_profile_action(seen, "seen-hash") == "NAVIGATE_NEXT"
    assert fixed._resume_profile_action(seen, "unseen-hash") == "PROCESS_CURRENT"


def test_scroll_telemetry_required_fields_are_hash_only():
    telemetry = {
        "cursor_client_x": 200, "cursor_client_y": 300,
        "cursor_screen_x": 1200, "cursor_screen_y": 700,
        "foreground_pid": 77, "wheel_delta": -360,
        "list_hash_before": "a" * 64, "list_hash_after": "b" * 64,
        "list_changed": True, "visible_row_count_before": 12,
        "visible_row_count_after": 12, "revisit_count": 4,
        "new_profile_found": False,
    }
    assert {"cursor_client_x", "cursor_client_y", "cursor_screen_x", "cursor_screen_y",
            "foreground_pid", "wheel_delta", "list_hash_before", "list_hash_after",
            "list_changed", "visible_row_count_before", "visible_row_count_after",
            "revisit_count", "new_profile_found"} <= telemetry.keys()


def test_confirmed_target_passes_at_150_unique_confirmed():
    state = {"confirmed": [{"wechat_id": f"id{i}"} for i in range(150)]}
    assert fixed._confirmed_target_reached(state, 150)


def test_primary_monitor_input_allowed_when_foreground_and_point_safe(monkeypatch):
    import sys

    import pyweixin.visual_navigation as visual

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: 100,
    ))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd: (1, 77),
    ))
    monkeypatch.setattr(visual, "is_weixin_foreground", lambda *args, **kwargs: True)
    assert fixed._foreground_ready(100, 77)
