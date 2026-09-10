"""봇 ON/OFF 및 프로세스 상태 관리.

상태는 모두 ``run/`` 폴더의 작은 파일로 남기므로, 봇이 돌아가는 중에 다른
터미널이나 대시보드에서 켜고 끌 수 있습니다.

  * ``control.json``   수집 ON/OFF (봇 프로세스는 살아있고 수집만 멈춤)
  * ``bot.pid``        실행 중인 프로세스 정보
  * ``stop.request``   프로세스 자체를 정상 종료시키는 요청
  * ``run_once.request`` / ``export.request``  즉시 1회 실행 / 엑셀 재생성 요청
  * ``heartbeat.json`` 살아있음 표시 + 최근 상태 (대시보드/모니터링용)
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .timeutil import iso_now


def process_alive(pid: int) -> bool:
    """해당 PID 의 프로세스가 살아있는지 확인합니다.

    Windows 에서 ``os.kill(pid, 0)`` 은 프로세스를 종료시켜 버리므로
    반드시 플랫폼별로 다르게 처리해야 합니다.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":  # pragma: no cover - Windows 전용 경로
        import ctypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            # 아직 종료되지 않았다면 WAIT_TIMEOUT 이 돌아옵니다.
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 다른 사용자 소유지만 살아있음
    return True


def _write_atomic(path: Path, text: str) -> None:
    """같은 폴더에 임시 파일로 쓴 뒤 교체합니다(중간에 깨진 파일이 남지 않도록)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class BotState:
    """수집 ON/OFF 상태."""

    enabled: bool = True
    updated_at: str = ""
    updated_by: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "note": self.note,
        }


@dataclass
class ProcessInfo:
    """실행 중인 봇 프로세스 정보."""

    pid: int = 0
    started_at: str = ""
    host: str = ""
    dashboard_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def alive(self) -> bool:
        return self.pid > 0 and process_alive(self.pid)


class Control:
    """``run/`` 폴더의 상태 파일들을 다룹니다."""

    def __init__(self, state_dir: Path, default_enabled: bool = True) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.default_enabled = default_enabled

        self.control_path = self.dir / "control.json"
        self.pid_path = self.dir / "bot.pid"
        self.stop_path = self.dir / "stop.request"
        self.run_once_path = self.dir / "run_once.request"
        self.export_path = self.dir / "export.request"
        self.heartbeat_path = self.dir / "heartbeat.json"

    # -- 수집 ON/OFF -------------------------------------------------------
    def read_state(self) -> BotState:
        data = _read_json(self.control_path)
        if not data:
            return BotState(enabled=self.default_enabled, updated_at="", updated_by="기본값")
        return BotState(
            enabled=bool(data.get("enabled", self.default_enabled)),
            updated_at=str(data.get("updated_at") or ""),
            updated_by=str(data.get("updated_by") or ""),
            note=str(data.get("note") or ""),
        )

    def set_enabled(self, enabled: bool, by: str = "cli", note: str = "") -> BotState:
        state = BotState(enabled=enabled, updated_at=iso_now(), updated_by=by, note=note)
        _write_atomic(
            self.control_path, json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        )
        return state

    def is_enabled(self) -> bool:
        return self.read_state().enabled

    # -- 프로세스 ----------------------------------------------------------
    def write_pid(self, dashboard_url: str = "", **extra: Any) -> ProcessInfo:
        info = ProcessInfo(
            pid=os.getpid(),
            started_at=iso_now(),
            host=socket.gethostname(),
            dashboard_url=dashboard_url,
            extra=extra,
        )
        payload = {
            "pid": info.pid,
            "started_at": info.started_at,
            "host": info.host,
            "dashboard_url": info.dashboard_url,
            **extra,
        }
        _write_atomic(self.pid_path, json.dumps(payload, ensure_ascii=False, indent=2))
        return info

    def read_pid(self) -> ProcessInfo | None:
        data = _read_json(self.pid_path)
        if not data or not data.get("pid"):
            return None
        try:
            pid = int(data["pid"])
        except (TypeError, ValueError):
            return None
        known = {"pid", "started_at", "host", "dashboard_url"}
        return ProcessInfo(
            pid=pid,
            started_at=str(data.get("started_at") or ""),
            host=str(data.get("host") or ""),
            dashboard_url=str(data.get("dashboard_url") or ""),
            extra={key: value for key, value in data.items() if key not in known},
        )

    def running_process(self) -> ProcessInfo | None:
        """살아있는 봇 프로세스 정보. 죽은 PID 파일은 정리합니다."""
        info = self.read_pid()
        if info is None:
            return None
        if info.alive:
            return info
        self.clear_pid()
        return None

    def clear_pid(self) -> None:
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # -- 요청 플래그 -------------------------------------------------------
    def _touch(self, path: Path, reason: str) -> None:
        _write_atomic(
            path,
            json.dumps({"requested_at": iso_now(), "reason": reason}, ensure_ascii=False),
        )

    def _take(self, path: Path) -> bool:
        """플래그가 있으면 지우고 True. (한 번만 소비됩니다)"""
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def request_stop(self, reason: str = "cli") -> None:
        self._touch(self.stop_path, reason)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def clear_stop(self) -> None:
        self._take(self.stop_path)

    def request_run_once(self, reason: str = "cli") -> None:
        self._touch(self.run_once_path, reason)

    def take_run_once(self) -> bool:
        return self._take(self.run_once_path)

    def request_export(self, reason: str = "cli") -> None:
        self._touch(self.export_path, reason)

    def take_export(self) -> bool:
        return self._take(self.export_path)

    # -- 하트비트 ----------------------------------------------------------
    def write_heartbeat(self, payload: dict[str, Any]) -> None:
        data = {"at": iso_now(), "pid": os.getpid(), **payload}
        try:
            _write_atomic(self.heartbeat_path, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError:
            pass  # 하트비트 실패로 봇이 멈추면 안 됩니다.

    def read_heartbeat(self) -> dict[str, Any]:
        return _read_json(self.heartbeat_path)
