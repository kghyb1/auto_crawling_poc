"""24시간 동작하는 봇 본체.

루프 구조

    [ON?] → 사이클 실행 → (주기적으로) 생존 확인 → 엑셀 출력
          → 다음 실행까지 1초 단위로 대기하며 요청 감시

ON/OFF, 즉시 실행, 엑셀 재생성 요청은 모두 ``run/`` 폴더의 플래그 파일로
전달되므로, 봇이 도는 중에 다른 터미널이나 대시보드에서 조작할 수 있습니다.
한 사이클에서 예외가 나도 로그만 남기고 다음 사이클로 넘어갑니다.
"""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from .classifier import Classifier, load_rules
from .config import Config
from .control import Control
from .discovery import Discovery
from .exporter import Exporter
from .fetcher import Fetcher
from .language import LanguageDetector
from .pipeline import Pipeline
from .renderer import Renderer
from .storage import RunStats, Storage
from .targets import load_targets
from .timeutil import iso_now, local_str, now_utc, to_iso

log = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """이미 다른 봇 프로세스가 돌고 있는 경우."""


@dataclass
class LastCycle:
    """마지막 사이클 요약 (하트비트/대시보드 표시용)."""

    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    pages_fetched: int = 0
    candidates_found: int = 0
    new_sites: int = 0
    updated_sites: int = 0
    candidates_added: int = 0
    sources_approved: int = 0
    dropped_foreign: int = 0
    export_path: str = ""
    error: str = ""
    note: str = ""


@dataclass
class DaemonStatus:
    """대시보드가 읽어가는 현재 상태."""

    phase: str = "대기"
    enabled: bool = True
    started_at: str = ""
    next_run_at: str = ""
    cycles_completed: int = 0
    last_cycle: LastCycle = field(default_factory=LastCycle)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_cycle"] = asdict(self.last_cycle)
        return data


class BotDaemon:
    """봇 프로세스."""

    def __init__(self, config: Config, dashboard_enabled: bool | None = None) -> None:
        self.config = config
        self.control = Control(config.state_dir, config.runtime.start_enabled)
        self.storage = Storage(config.database_path)
        self.classifier = Classifier(load_rules(config.root / "config" / "rules.yaml"))
        self.fetcher = Fetcher(config.crawl)
        self.renderer = Renderer(config.renderer, config.crawl.user_agent)

        targets = load_targets(config.root / "config" / "targets.yaml")
        self.discovery = Discovery(
            config=config,
            storage=self.storage,
            classifier=self.classifier,
            fetcher=self.fetcher,
        )
        self.language = LanguageDetector(
            config.language_filter, fetcher=self.fetcher, storage=self.storage
        )
        self.pipeline = Pipeline(
            config=config,
            storage=self.storage,
            classifier=self.classifier,
            fetcher=self.fetcher,
            renderer=self.renderer,
            pagination_patterns=targets.pagination_patterns,
            discovery=self.discovery,
            language=self.language,
        )
        self.exporter = Exporter(config, self.storage, self.classifier)

        self._stop_event = threading.Event()
        self._status = DaemonStatus(started_at=iso_now())
        self._status_lock = threading.Lock()
        self._dashboard_server = None
        self._dashboard_enabled = (
            config.dashboard.enabled if dashboard_enabled is None else dashboard_enabled
        )
        self._cycle_count = 0
        self._last_prune_day = ""

        # DB 에 수집 대상이 하나도 없으면 targets.yaml 을 자동으로 한 번 가져옵니다.
        if self.storage.count_sources() == 0 and targets.sources:
            imported = self.import_targets()
            log.info("targets.yaml 에서 홍보사이트 %d곳을 가져왔습니다.", imported)

    # -- 상태 --------------------------------------------------------------
    def status(self) -> DaemonStatus:
        with self._status_lock:
            return DaemonStatus(
                phase=self._status.phase,
                enabled=self._status.enabled,
                started_at=self._status.started_at,
                next_run_at=self._status.next_run_at,
                cycles_completed=self._status.cycles_completed,
                last_cycle=LastCycle(**asdict(self._status.last_cycle)),
            )

    def _update_status(self, **changes: Any) -> None:
        with self._status_lock:
            for key, value in changes.items():
                setattr(self._status, key, value)

    def _write_heartbeat(self) -> None:
        status = self.status()
        self.control.write_heartbeat(
            {
                "phase": status.phase,
                "enabled": status.enabled,
                "started_at": status.started_at,
                "next_run_at": status.next_run_at,
                "cycles_completed": status.cycles_completed,
                "last_cycle": asdict(status.last_cycle),
            }
        )

    # -- 준비 --------------------------------------------------------------
    def import_targets(self) -> int:
        """``config/targets.yaml`` 의 홍보사이트를 DB 에 반영합니다."""
        targets = load_targets(self.config.root / "config" / "targets.yaml")
        for spec in targets.sources:
            self.storage.upsert_source(
                url=spec.url,
                name=spec.name,
                enabled=spec.enabled,
                render=spec.render,
                max_pages=spec.max_pages,
                note=spec.note,
            )
        self.pipeline.pagination_patterns = targets.pagination_patterns
        return len(targets.sources)

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            log.info("종료 신호(%s)를 받아 정리 후 종료합니다.", signum)
            self._stop_event.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # 메인 스레드가 아닌 경우 등
                pass

    def _start_dashboard(self) -> str:
        if not self._dashboard_enabled:
            return ""
        from .dashboard import start_dashboard  # 지연 임포트 (순환 참조 방지)

        try:
            self._dashboard_server, url = start_dashboard(
                self.config, self.control, self.storage, self.classifier
            )
            log.info("관리 화면: %s", url)
            return url
        except OSError as exc:
            log.warning(
                "대시보드를 열지 못했습니다(포트 %d): %s. CLI 로만 조작할 수 있습니다.",
                self.config.dashboard.port,
                exc,
            )
            return ""

    # -- 사이클 ------------------------------------------------------------
    def _should_stop(self) -> bool:
        if self._stop_event.is_set():
            return True
        if self.control.stop_requested():
            log.info("중단 요청 파일을 확인했습니다. 종료합니다.")
            self.control.clear_stop()
            self._stop_event.set()
            return True
        return False

    def run_cycle(self, reason: str = "정기") -> LastCycle:
        """사이클 한 번 (수집 → 생존 확인 → 엑셀)."""
        summary = LastCycle(started_at=iso_now())
        started = time.monotonic()
        run_id = self.storage.start_run()
        log.info("=== 수집 사이클 시작 (%s) ===", reason)

        stats = RunStats()
        try:
            self._update_status(phase="수집 중")
            stats = self.pipeline.run_cycle(self._should_stop)
        except Exception as exc:
            log.exception("사이클 처리 중 예외")
            summary.error = f"{type(exc).__name__}: {exc}"
            stats.note = summary.error

        # 홍보사이트 자동 발견 - 이번 사이클에 모인 후보를 예산 안에서 평가합니다.
        # 수집 도중이 아니라 여기서 일괄 처리해야 사이클 소요 시간이 예측 가능합니다.
        if self.config.discovery.enabled and not self._should_stop():
            try:
                self._update_status(phase="홍보사이트 후보 평가 중")
                found = self.discovery.evaluate_pending(self._should_stop)
                stats.sources_approved = found.approved
                if found.evaluated or found.approved or found.queued:
                    log.info(
                        "후보 평가: %d건 확인 → 자동승인 %d / 대기 %d / 기각 %d / 미러 %d",
                        found.evaluated,
                        found.approved,
                        found.queued,
                        found.rejected,
                        found.mirrors,
                    )
                self.discovery.cleanup_dead_sources()
            except Exception:
                log.exception("홍보사이트 후보 평가 중 예외")

        duration_ms = int((time.monotonic() - started) * 1000)
        self.storage.finish_run(run_id, stats, duration_ms)

        self._cycle_count += 1
        summary.finished_at = iso_now()
        summary.duration_seconds = duration_ms / 1000
        summary.sources_total = stats.sources_total
        summary.sources_ok = stats.sources_ok
        summary.sources_failed = stats.sources_failed
        summary.pages_fetched = stats.pages_fetched
        summary.candidates_found = stats.candidates_found
        summary.new_sites = stats.new_sites
        summary.updated_sites = stats.updated_sites
        summary.candidates_added = stats.candidates_added
        summary.sources_approved = stats.sources_approved
        summary.dropped_foreign = stats.dropped_foreign
        summary.note = stats.note

        # 생존 확인 (설정한 주기마다)
        alive_settings = self.config.alive_check
        if (
            alive_settings.enabled
            and not self._should_stop()
            and self._cycle_count % max(1, alive_settings.every_cycles) == 0
        ):
            try:
                self._update_status(phase="생존 확인 중")
                self.pipeline.run_alive_checks(self._should_stop)
            except Exception:
                log.exception("생존 확인 중 예외")

        # 엑셀 출력
        if stats.new_sites or stats.updated_sites or self.config.export.always_rewrite:
            try:
                self._update_status(phase="엑셀 저장 중")
                result = self.exporter.export()
                summary.export_path = str(result.path)
                if result.warning:
                    summary.note = (summary.note + " " + result.warning).strip()
            except Exception as exc:
                log.exception("엑셀 저장 중 예외")
                summary.error = (summary.error + f" 엑셀 저장 실패: {exc}").strip()
        else:
            log.info("변경 사항이 없어 엑셀은 다시 만들지 않았습니다.")

        # 하루 한 번 오래된 관측 이력 정리
        today = now_utc().strftime("%Y-%m-%d")
        if self._last_prune_day != today:
            self._last_prune_day = today
            try:
                self.storage.prune_observations(self.config.storage.observation_retention_days)
            except Exception:
                log.exception("이력 정리 중 예외")

        log.info(
            "=== 사이클 종료: 대상 %d곳(성공 %d/실패 %d), 페이지 %d, 후보 %d, "
            "신규 %d, 갱신 %d, 외국어 제외 %d, %.1f초 ===",
            stats.sources_total,
            stats.sources_ok,
            stats.sources_failed,
            stats.pages_fetched,
            stats.candidates_found,
            stats.new_sites,
            stats.updated_sites,
            stats.dropped_foreign,
            summary.duration_seconds,
        )
        with self._status_lock:
            self._status.cycles_completed = self._cycle_count
            self._status.last_cycle = summary
        return summary

    def export_now(self) -> None:
        try:
            self._update_status(phase="엑셀 저장 중")
            result = self.exporter.export()
            with self._status_lock:
                self._status.last_cycle.export_path = str(result.path)
        except Exception:
            log.exception("엑셀 저장 실패")

    # -- 대기 --------------------------------------------------------------
    def _wait(self, seconds: float) -> str:
        """지정 시간만큼 대기하면서 요청을 감시합니다. 대기를 끝낸 이유를 돌려줍니다."""
        deadline = time.monotonic() + max(0.0, seconds)
        was_enabled = self.control.is_enabled()
        last_heartbeat = 0.0

        while True:
            if self._should_stop():
                return "stop"

            now = time.monotonic()
            if now - last_heartbeat >= 5:
                self._write_heartbeat()
                last_heartbeat = now

            if self.control.take_run_once():
                return "run_once"
            if self.control.take_export():
                return "export"

            enabled = self.control.is_enabled()
            if enabled != was_enabled:
                self._update_status(enabled=enabled)
                log.info("수집 상태가 %s 로 바뀌었습니다.", "ON" if enabled else "OFF")
                if enabled:
                    return "enabled"
                was_enabled = enabled
                # OFF 로 바뀌면 예정되어 있던 다음 수집 시각도 지웁니다.
                self._update_status(phase="정지(OFF)", next_run_at="")

            if now >= deadline:
                return "timeout"
            time.sleep(min(1.0, max(0.05, deadline - now)))

    # -- 메인 루프 ---------------------------------------------------------
    def run(self) -> int:
        """봇을 시작합니다. 정상 종료 시 0."""
        existing = self.control.running_process()
        if existing is not None:
            raise AlreadyRunningError(
                f"이미 봇이 실행 중입니다 (PID {existing.pid}, 시작 {local_str(existing.started_at)}). "
                "먼저 `python bot.py stop` 으로 종료하세요."
            )

        self.control.clear_stop()
        self._install_signal_handlers()
        dashboard_url = self._start_dashboard()
        self.control.write_pid(dashboard_url=dashboard_url)

        enabled = self.control.is_enabled()
        # 상태 파일이 없으면 설정의 기본값으로 한 번 만들어 둡니다.
        if not self.control.control_path.exists():
            self.control.set_enabled(enabled, by="시작 기본값")
        self._update_status(enabled=enabled, phase="시작")

        log.info(
            "봇 시작 (수집 %s, 주기 %d초, 대상 %d곳, 동시 %d)",
            "ON" if enabled else "OFF",
            self.config.crawl.interval_seconds,
            self.storage.count_sources(enabled_only=True),
            self.config.crawl.concurrency,
        )
        if not enabled:
            log.info("현재 OFF 상태입니다. `python bot.py on` 으로 수집을 시작하세요.")

        exit_code = 0
        # 다음 수집 예정 시각(monotonic). 엑셀 재생성 같은 중간 요청이
        # 이 시각을 밀어내지 않도록 따로 들고 있습니다.
        next_run_monotonic: float | None = None
        try:
            # 켜져 있으면 시작 직후 한 번 수집합니다.
            reason = "enabled" if enabled else "off"
            while not self._should_stop():
                if reason in {"run_once", "enabled", "timeout"} and (
                    self.control.is_enabled() or reason == "run_once"
                ):
                    self.run_cycle("수동 요청" if reason == "run_once" else "정기")
                elif reason == "export":
                    self.export_now()

                if self._should_stop():
                    break

                if self.control.is_enabled():
                    if reason == "export" and next_run_monotonic is not None:
                        # 엑셀만 다시 만든 경우입니다. 원래 예정 시각을 유지합니다.
                        wait_seconds = max(0.0, next_run_monotonic - time.monotonic())
                    else:
                        interval = self.config.crawl.interval_seconds
                        jitter = random.uniform(0, self.config.crawl.jitter_seconds)
                        wait_seconds = interval + jitter
                        next_run_monotonic = time.monotonic() + wait_seconds
                    next_run = now_utc() + timedelta(seconds=wait_seconds)
                    self._update_status(
                        phase="다음 수집 대기", next_run_at=to_iso(next_run), enabled=True
                    )
                    log.info(
                        "다음 수집 예정: %s (%.0f초 후)", local_str(next_run), wait_seconds
                    )
                else:
                    wait_seconds = float(self.config.crawl.interval_seconds)
                    next_run_monotonic = None
                    self._update_status(phase="정지(OFF)", next_run_at="", enabled=False)

                self._write_heartbeat()
                reason = self._wait(wait_seconds)
        except KeyboardInterrupt:
            log.info("키보드 중단으로 종료합니다.")
        except Exception:
            log.exception("봇이 예기치 않게 중단되었습니다.")
            exit_code = 1
        finally:
            self._update_status(phase="종료 중")
            self._write_heartbeat()
            self.shutdown()
        log.info("봇을 종료했습니다.")
        return exit_code

    def run_single_cycle(self) -> LastCycle:
        """데몬 없이 1회만 수집합니다 (CLI ``run-once`` 에서 사용)."""
        try:
            summary = self.run_cycle("1회 실행")
            if not summary.export_path:
                self.export_now()
            return summary
        finally:
            self.shutdown(clear_pid=False)

    def shutdown(self, clear_pid: bool = True) -> None:
        if self._dashboard_server is not None:
            try:
                self._dashboard_server.shutdown()
                self._dashboard_server.server_close()
            except Exception:
                pass
            self._dashboard_server = None
        for closer in (self.renderer.close, self.fetcher.close, self.storage.close):
            try:
                closer()
            except Exception:
                pass
        if clear_pid:
            self.control.clear_pid()
