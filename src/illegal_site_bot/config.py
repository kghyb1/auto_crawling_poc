"""설정 로딩.

코드에 내장된 기본값 위에 ``config/config.yaml`` 값을 덮어씁니다.
따라서 config.yaml 이 없어도 봇은 기본값으로 동작합니다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

DEFAULTS: dict[str, Any] = {
    "crawl": {
        "interval_seconds": 3600,
        "jitter_seconds": 300,
        "concurrency": 4,
        "per_host_delay_seconds": 2.0,
        "request_timeout_seconds": 20,
        "max_retries": 2,
        "retry_backoff_seconds": 3.0,
        "max_pages_per_source": 3,
        "max_response_bytes": 5 * 1024 * 1024,
        "resolve_candidate_redirects": True,
        "max_redirect_hops": 5,
        "max_redirect_resolutions": 150,
        "respect_robots": True,
        "verify_tls": True,
        "user_agent": (
            "IllegalSiteMonitorBot/1.0 "
            "(+compliance-monitoring; contact: admin@example.com)"
        ),
        "extra_headers": {"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"},
    },
    "renderer": {
        "enabled": False,
        "wait_ms": 2500,
        "headless": True,
        "render_all": False,
    },
    "alive_check": {
        "enabled": True,
        "every_cycles": 6,
        "timeout_seconds": 10,
        "batch_size": 200,
    },
    "discovery": {
        "enabled": True,
        "auto_approve": False,
        "auto_approve_score": 75,
        "queue_score": 40,
        "max_depth": 2,
        "max_new_per_cycle": 5,
        "max_candidates_per_cycle": 50,
        "max_total_sources": 500,
        "max_evaluations_per_cycle": 20,
        "max_evaluation_failures": 3,
        "reevaluate_after_days": 30,
        "auto_disable_after_failures": 10,
        "mirror_similarity": 0.8,
    },
    "export": {
        "directory": "data/exports",
        "filename": "불법사이트_URL목록.xlsx",
        "keep_daily_snapshots": True,
        "snapshot_retention_days": 90,
        "min_score": 30,
        "clickable_links": False,
        "always_rewrite": False,
    },
    "storage": {
        "database": "data/collector.sqlite3",
        "observation_retention_days": 180,
    },
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8787,
        "token": "",
    },
    "logging": {
        "level": "INFO",
        "directory": "logs",
        "retention_days": 14,
    },
    "runtime": {
        "state_directory": "run",
        "start_enabled": True,
    },
}


class ConfigError(Exception):
    """설정 파일이 잘못된 경우."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls: type, data: dict[str, Any], section: str):
    """dict 를 dataclass 로 변환. 알 수 없는 키는 오타로 보고 에러를 냅니다."""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"[{section}] 알 수 없는 설정 항목: {', '.join(sorted(unknown))}"
        )
    return cls(**data)


@dataclass(frozen=True)
class CrawlConfig:
    interval_seconds: int
    jitter_seconds: int
    concurrency: int
    per_host_delay_seconds: float
    request_timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    max_pages_per_source: int
    max_response_bytes: int
    resolve_candidate_redirects: bool
    max_redirect_hops: int
    max_redirect_resolutions: int
    respect_robots: bool
    verify_tls: bool
    user_agent: str
    extra_headers: dict[str, str]


@dataclass(frozen=True)
class RendererConfig:
    enabled: bool
    wait_ms: int
    headless: bool
    render_all: bool


@dataclass(frozen=True)
class AliveCheckConfig:
    enabled: bool
    every_cycles: int
    timeout_seconds: int
    batch_size: int


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool
    auto_approve: bool
    auto_approve_score: int
    queue_score: int
    max_depth: int
    max_new_per_cycle: int
    max_candidates_per_cycle: int
    max_total_sources: int
    max_evaluations_per_cycle: int
    max_evaluation_failures: int
    reevaluate_after_days: int
    auto_disable_after_failures: int
    mirror_similarity: float


@dataclass(frozen=True)
class ExportConfig:
    directory: str
    filename: str
    keep_daily_snapshots: bool
    snapshot_retention_days: int
    min_score: int
    clickable_links: bool
    always_rewrite: bool


@dataclass(frozen=True)
class StorageConfig:
    database: str
    observation_retention_days: int


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool
    host: str
    port: int
    token: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    directory: str
    retention_days: int


@dataclass(frozen=True)
class RuntimeConfig:
    state_directory: str
    start_enabled: bool


@dataclass(frozen=True)
class Config:
    root: Path
    crawl: CrawlConfig
    renderer: RendererConfig
    alive_check: AliveCheckConfig
    discovery: DiscoveryConfig
    export: ExportConfig
    storage: StorageConfig
    dashboard: DashboardConfig
    logging: LoggingConfig
    runtime: RuntimeConfig
    source_path: Path | None = None

    def path(self, value: str) -> Path:
        """설정에 적힌 상대 경로를 프로젝트 루트 기준 절대 경로로 바꿉니다."""
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else self.root / candidate

    @property
    def database_path(self) -> Path:
        return self.path(self.storage.database)

    @property
    def export_dir(self) -> Path:
        return self.path(self.export.directory)

    @property
    def export_path(self) -> Path:
        return self.export_dir / self.export.filename

    @property
    def log_dir(self) -> Path:
        return self.path(self.logging.directory)

    @property
    def state_dir(self) -> Path:
        return self.path(self.runtime.state_directory)


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    """설정을 읽어 :class:`Config` 를 만듭니다.

    ``path`` 를 주지 않으면 ``config/config.yaml`` 을 찾고, 없으면 기본값만 씁니다.
    """
    root = Path(root).resolve() if root else PROJECT_ROOT
    config_path = Path(path) if path else root / "config" / "config.yaml"

    raw: dict[str, Any] = {}
    used_path: Path | None = None
    if config_path.is_file():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다 ({config_path}): {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"설정 파일 최상위는 매핑이어야 합니다: {config_path}")
        raw = loaded
        used_path = config_path
    elif path is not None:
        raise ConfigError(f"설정 파일이 없습니다: {config_path}")

    merged = _deep_merge(DEFAULTS, raw)
    unknown_sections = set(merged) - set(DEFAULTS)
    if unknown_sections:
        raise ConfigError(
            f"알 수 없는 설정 섹션: {', '.join(sorted(unknown_sections))}"
        )

    config = Config(
        root=root,
        crawl=_build(CrawlConfig, merged["crawl"], "crawl"),
        renderer=_build(RendererConfig, merged["renderer"], "renderer"),
        alive_check=_build(AliveCheckConfig, merged["alive_check"], "alive_check"),
        discovery=_build(DiscoveryConfig, merged["discovery"], "discovery"),
        export=_build(ExportConfig, merged["export"], "export"),
        storage=_build(StorageConfig, merged["storage"], "storage"),
        dashboard=_build(DashboardConfig, merged["dashboard"], "dashboard"),
        logging=_build(LoggingConfig, merged["logging"], "logging"),
        runtime=_build(RuntimeConfig, merged["runtime"], "runtime"),
        source_path=used_path,
    )
    _validate(config)
    return config


def _validate(config: Config) -> None:
    crawl = config.crawl
    if crawl.interval_seconds < 60:
        raise ConfigError("crawl.interval_seconds 는 60 이상이어야 합니다.")
    if crawl.jitter_seconds < 0:
        raise ConfigError("crawl.jitter_seconds 는 0 이상이어야 합니다.")
    if not 1 <= crawl.concurrency <= 32:
        raise ConfigError("crawl.concurrency 는 1~32 범위여야 합니다.")
    if crawl.per_host_delay_seconds < 0:
        raise ConfigError("crawl.per_host_delay_seconds 는 0 이상이어야 합니다.")
    if crawl.max_pages_per_source < 1:
        raise ConfigError("crawl.max_pages_per_source 는 1 이상이어야 합니다.")
    if not 0 <= config.export.min_score <= 100:
        raise ConfigError("export.min_score 는 0~100 범위여야 합니다.")
    if config.alive_check.every_cycles < 1:
        raise ConfigError("alive_check.every_cycles 는 1 이상이어야 합니다.")

    discovery = config.discovery
    if not 0 <= discovery.max_depth <= 5:
        raise ConfigError(
            "discovery.max_depth 는 0~5 범위여야 합니다. "
            "3 이상은 수집 대상이 급격히 늘어나므로 권장하지 않습니다."
        )
    if not 0 <= discovery.queue_score <= 100:
        raise ConfigError("discovery.queue_score 는 0~100 범위여야 합니다.")
    if not 0 <= discovery.auto_approve_score <= 100:
        raise ConfigError("discovery.auto_approve_score 는 0~100 범위여야 합니다.")
    if discovery.auto_approve_score < discovery.queue_score:
        raise ConfigError(
            "discovery.auto_approve_score 는 queue_score 보다 크거나 같아야 합니다."
        )
    if discovery.max_total_sources < 1:
        raise ConfigError("discovery.max_total_sources 는 1 이상이어야 합니다.")
    if discovery.max_new_per_cycle < 0 or discovery.max_candidates_per_cycle < 0:
        raise ConfigError("discovery 의 사이클당 상한은 0 이상이어야 합니다.")
    if not 0 < discovery.mirror_similarity <= 1:
        raise ConfigError("discovery.mirror_similarity 는 0 초과 1 이하여야 합니다.")
    if not 1 <= config.dashboard.port <= 65535:
        raise ConfigError("dashboard.port 는 1~65535 범위여야 합니다.")
    if config.dashboard.enabled and config.dashboard.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    } and not config.dashboard.token:
        raise ConfigError(
            "dashboard.host 를 외부에 열려면 dashboard.token 을 반드시 설정해야 합니다."
        )
