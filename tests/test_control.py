import os

from illegal_site_bot.control import Control, process_alive


def test_process_alive_for_current_process():
    assert process_alive(os.getpid()) is True


def test_process_alive_false_for_unused_pid():
    # 존재할 가능성이 거의 없는 PID
    assert process_alive(4_000_000) is False


def test_process_alive_false_for_invalid_pid():
    assert process_alive(0) is False
    assert process_alive(-1) is False


class TestEnabledState:
    def test_defaults_to_constructor_value_when_no_file(self, tmp_path):
        assert Control(tmp_path / "run", default_enabled=True).is_enabled() is True
        assert Control(tmp_path / "run2", default_enabled=False).is_enabled() is False

    def test_set_enabled_persists_across_instances(self, tmp_path):
        first = Control(tmp_path / "run", default_enabled=True)
        first.set_enabled(False, by="테스트", note="점검")

        second = Control(tmp_path / "run", default_enabled=True)
        state = second.read_state()
        assert state.enabled is False
        assert state.updated_by == "테스트"
        assert state.note == "점검"
        assert state.updated_at  # 기록 시각이 남아야 합니다

    def test_toggling_back_on(self, tmp_path):
        control = Control(tmp_path / "run")
        control.set_enabled(False)
        control.set_enabled(True)
        assert control.is_enabled() is True


class TestProcessInfo:
    def test_write_and_read_pid(self, tmp_path):
        control = Control(tmp_path / "run")
        info = control.write_pid(dashboard_url="http://127.0.0.1:8787/")
        assert info.pid == os.getpid()

        read = control.read_pid()
        assert read is not None
        assert read.pid == os.getpid()
        assert read.dashboard_url == "http://127.0.0.1:8787/"

    def test_running_process_detects_live_process(self, tmp_path):
        control = Control(tmp_path / "run")
        control.write_pid()
        assert control.running_process() is not None

    def test_running_process_cleans_up_dead_pid_file(self, tmp_path):
        control = Control(tmp_path / "run")
        control.pid_path.write_text('{"pid": 4000000}', encoding="utf-8")
        assert control.running_process() is None
        assert not control.pid_path.exists()

    def test_read_pid_tolerates_broken_file(self, tmp_path):
        control = Control(tmp_path / "run")
        control.pid_path.write_text("{not json", encoding="utf-8")
        assert control.read_pid() is None


class TestRequestFlags:
    def test_stop_request_lifecycle(self, tmp_path):
        control = Control(tmp_path / "run")
        assert control.stop_requested() is False
        control.request_stop("테스트")
        assert control.stop_requested() is True
        control.clear_stop()
        assert control.stop_requested() is False

    def test_run_once_is_consumed_only_once(self, tmp_path):
        control = Control(tmp_path / "run")
        control.request_run_once()
        assert control.take_run_once() is True
        assert control.take_run_once() is False

    def test_export_is_consumed_only_once(self, tmp_path):
        control = Control(tmp_path / "run")
        control.request_export()
        assert control.take_export() is True
        assert control.take_export() is False


def test_heartbeat_round_trip(tmp_path):
    control = Control(tmp_path / "run")
    control.write_heartbeat({"phase": "수집 중", "cycles_completed": 3})
    data = control.read_heartbeat()
    assert data["phase"] == "수집 중"
    assert data["cycles_completed"] == 3
    assert data["pid"] == os.getpid()
    assert data["at"]


def test_missing_heartbeat_returns_empty(tmp_path):
    assert Control(tmp_path / "run").read_heartbeat() == {}
