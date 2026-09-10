"""SQLite 저장소.

엑셀은 매번 다시 만드는 '보고서'이고, 실제 원본 데이터는 이 DB 입니다.
언제 어느 홍보사이트에서 발견했는지(관측 이력)까지 남기므로 신고 자료로
쓸 수 있습니다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .normalizer import host_of, registrable_domain
from .timeutil import days_ago, iso_now

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SITE_STATUSES = ("new", "confirmed", "reported", "ignored")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id                   INTEGER PRIMARY KEY,
    url                  TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    render               INTEGER NOT NULL DEFAULT 0,
    max_pages            INTEGER,
    note                 TEXT NOT NULL DEFAULT '',
    added_at             TEXT NOT NULL,
    last_crawled_at      TEXT,
    last_status          TEXT NOT NULL DEFAULT '',
    last_error           TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    found_total          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sites (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    domain           TEXT NOT NULL,
    host             TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT 'unknown',
    category_label   TEXT NOT NULL DEFAULT '미분류',
    base_score       INTEGER NOT NULL DEFAULT 0,
    score            INTEGER NOT NULL DEFAULT 0,
    matched_keywords TEXT NOT NULL DEFAULT '',
    reasons          TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    seen_count       INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    redirect_from    TEXT NOT NULL DEFAULT '',
    alive            INTEGER,
    http_status      INTEGER,
    last_checked_at  TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    memo             TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sites_domain      ON sites(domain);
CREATE INDEX IF NOT EXISTS idx_sites_score       ON sites(score DESC);
CREATE INDEX IF NOT EXISTS idx_sites_first_seen  ON sites(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_sites_checked     ON sites(last_checked_at);
CREATE INDEX IF NOT EXISTS idx_sites_category    ON sites(category);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    page_url    TEXT NOT NULL DEFAULT '',
    anchor_text TEXT NOT NULL DEFAULT '',
    method      TEXT NOT NULL DEFAULT '',
    banner      INTEGER NOT NULL DEFAULT 0,
    score       INTEGER NOT NULL DEFAULT 0,
    found_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_site  ON observations(site_id);
CREATE INDEX IF NOT EXISTS idx_obs_found ON observations(found_at);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    sources_total    INTEGER NOT NULL DEFAULT 0,
    sources_ok       INTEGER NOT NULL DEFAULT 0,
    sources_failed   INTEGER NOT NULL DEFAULT 0,
    pages_fetched    INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    new_sites        INTEGER NOT NULL DEFAULT 0,
    updated_sites    INTEGER NOT NULL DEFAULT 0,
    duration_ms      INTEGER,
    note             TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class SourceRow:
    """수집 대상 홍보사이트."""

    id: int
    url: str
    name: str
    enabled: bool
    render: bool
    max_pages: int | None
    note: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SourceRow":
        return cls(
            id=row["id"],
            url=row["url"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            render=bool(row["render"]),
            max_pages=row["max_pages"],
            note=row["note"],
        )


@dataclass
class RunStats:
    """한 사이클 결과."""

    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    pages_fetched: int = 0
    candidates_found: int = 0
    new_sites: int = 0
    updated_sites: int = 0
    note: str = ""


class Storage:
    """SQLite 접근 래퍼. 스레드 간 공유해도 안전합니다(락으로 직렬화)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- 기본 --------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] > SCHEMA_VERSION:
                raise RuntimeError(
                    f"DB 스키마 버전({row['version']})이 이 프로그램({SCHEMA_VERSION})보다 "
                    "높습니다. 봇을 최신 버전으로 업데이트하세요."
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    # -- 홍보사이트(수집원) -------------------------------------------------
    def upsert_source(
        self,
        url: str,
        name: str = "",
        enabled: bool = True,
        render: bool = False,
        max_pages: int | None = None,
        note: str = "",
        update_existing: bool = True,
    ) -> int:
        """홍보사이트를 등록하거나 갱신하고 id 를 돌려줍니다."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sources WHERE url = ?", (url,)
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO sources (url, name, enabled, render, max_pages, note, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (url, name, int(enabled), int(render), max_pages, note, iso_now()),
                )
                self._conn.commit()
                return int(cursor.lastrowid)

            if update_existing:
                self._conn.execute(
                    """
                    UPDATE sources
                       SET name = ?, enabled = ?, render = ?, max_pages = ?, note = ?
                     WHERE id = ?
                    """,
                    (name, int(enabled), int(render), max_pages, note, row["id"]),
                )
                self._conn.commit()
            return int(row["id"])

    def list_sources(self, enabled_only: bool = False) -> list[SourceRow]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [SourceRow.from_row(row) for row in self._query(sql)]

    def source_rows(self) -> list[sqlite3.Row]:
        """엑셀 출력용 전체 컬럼."""
        return self._query("SELECT * FROM sources ORDER BY id")

    def count_sources(self, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return int(self._query(sql)[0]["n"])

    def set_source_enabled(self, url: str, enabled: bool) -> bool:
        cursor = self._execute(
            "UPDATE sources SET enabled = ? WHERE url = ?", (int(enabled), url)
        )
        return cursor.rowcount > 0

    def remove_source(self, url: str) -> bool:
        cursor = self._execute("DELETE FROM sources WHERE url = ?", (url,))
        return cursor.rowcount > 0

    def record_source_result(
        self,
        source_id: int,
        ok: bool,
        status: str,
        error: str = "",
        found: int = 0,
    ) -> None:
        with self._lock:
            if ok:
                self._conn.execute(
                    """
                    UPDATE sources
                       SET last_crawled_at = ?, last_status = ?, last_error = '',
                           consecutive_failures = 0, found_total = found_total + ?
                     WHERE id = ?
                    """,
                    (iso_now(), status, found, source_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE sources
                       SET last_crawled_at = ?, last_status = ?, last_error = ?,
                           consecutive_failures = consecutive_failures + 1
                     WHERE id = ?
                    """,
                    (iso_now(), status, error[:500], source_id),
                )
            self._conn.commit()

    # -- 불법사이트 --------------------------------------------------------
    def record_site(
        self,
        url: str,
        *,
        category: str,
        category_label: str,
        base_score: int,
        matched_keywords: str = "",
        reasons: str = "",
        title: str = "",
        redirect_from: str = "",
    ) -> tuple[int, bool]:
        """사이트를 등록하거나 갱신합니다. ``(site_id, 신규여부)``."""
        now = iso_now()
        domain = registrable_domain(url)
        host = host_of(url)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, base_score, category, category_label, title, redirect_from
                  FROM sites WHERE url = ?
                """,
                (url,),
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO sites (
                        url, domain, host, category, category_label, base_score, score,
                        matched_keywords, reasons, title, first_seen_at, last_seen_at,
                        seen_count, distinct_sources, redirect_from, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 'new')
                    """,
                    (
                        url,
                        domain,
                        host,
                        category,
                        category_label,
                        base_score,
                        base_score,
                        matched_keywords,
                        reasons,
                        title,
                        now,
                        now,
                        redirect_from,
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid), True

            site_id = int(row["id"])
            # 점수는 가장 높았던 값을 유지합니다(한 번이라도 강한 근거가 있었다면 남김).
            # 카테고리도 점수가 더 높은 판별 쪽을 따릅니다.
            old_base = int(row["base_score"] or 0)
            if base_score >= old_base:
                new_base, keep_category, keep_label = base_score, category, category_label
            else:
                new_base = old_base
                keep_category, keep_label = row["category"], row["category_label"]
            self._conn.execute(
                """
                UPDATE sites
                   SET last_seen_at = ?, seen_count = seen_count + 1,
                       base_score = ?, category = ?, category_label = ?,
                       matched_keywords = CASE WHEN ? <> '' THEN ? ELSE matched_keywords END,
                       reasons = CASE WHEN ? <> '' THEN ? ELSE reasons END,
                       title = CASE WHEN title = '' AND ? <> '' THEN ? ELSE title END,
                       redirect_from = CASE
                            WHEN redirect_from = '' AND ? <> '' THEN ? ELSE redirect_from END
                 WHERE id = ?
                """,
                (
                    now,
                    new_base,
                    keep_category,
                    keep_label,
                    matched_keywords,
                    matched_keywords,
                    reasons,
                    reasons,
                    title,
                    title,
                    redirect_from,
                    redirect_from,
                    site_id,
                ),
            )
            self._conn.commit()
            return site_id, False

    def record_observation(
        self,
        site_id: int,
        source_id: int | None,
        source_url: str,
        page_url: str,
        anchor_text: str,
        method: str,
        banner: bool,
        score: int,
    ) -> None:
        self._execute(
            """
            INSERT INTO observations (
                site_id, source_id, source_url, page_url, anchor_text,
                method, banner, score, found_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_id,
                source_id,
                source_url,
                page_url,
                anchor_text[:300],
                method,
                int(banner),
                score,
                iso_now(),
            ),
        )

    def refresh_site_score(self, site_id: int, bonus_per_source: int, bonus_max: int) -> int:
        """서로 다른 홍보사이트에서 몇 곳에 걸렸는지 세고 최종 점수를 갱신합니다."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT source_url) AS n FROM observations WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            distinct = int(row["n"] if row else 0)
            base_row = self._conn.execute(
                "SELECT base_score FROM sites WHERE id = ?", (site_id,)
            ).fetchone()
            base = int(base_row["base_score"] if base_row else 0)
            bonus = min(max(0, distinct - 1) * bonus_per_source, bonus_max)
            score = max(0, min(100, base + bonus))
            self._conn.execute(
                "UPDATE sites SET distinct_sources = ?, score = ? WHERE id = ?",
                (distinct, score, site_id),
            )
            self._conn.commit()
            return score

    def update_alive(
        self, site_id: int, alive: bool, http_status: int | None, working_url: str | None = None
    ) -> None:
        with self._lock:
            if working_url:
                # https 로 저장되어 있었지만 http 로만 열리는 경우 URL 을 교정합니다.
                exists = self._conn.execute(
                    "SELECT id FROM sites WHERE url = ? AND id <> ?", (working_url, site_id)
                ).fetchone()
                if exists is None:
                    self._conn.execute(
                        "UPDATE sites SET url = ? WHERE id = ?", (working_url, site_id)
                    )
            self._conn.execute(
                """
                UPDATE sites
                   SET alive = ?, http_status = ?, last_checked_at = ?
                 WHERE id = ?
                """,
                (int(alive), http_status, iso_now(), site_id),
            )
            self._conn.commit()

    def sites_to_check(self, limit: int) -> list[sqlite3.Row]:
        """생존 확인이 가장 오래된 사이트부터 돌려줍니다."""
        return self._query(
            """
            SELECT id, url FROM sites
             WHERE status <> 'ignored'
             ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC
             LIMIT ?
            """,
            (limit,),
        )

    def set_site_status(self, url: str, status: str, memo: str | None = None) -> bool:
        if status not in SITE_STATUSES:
            raise ValueError(f"status 는 {SITE_STATUSES} 중 하나여야 합니다.")
        if memo is None:
            cursor = self._execute(
                "UPDATE sites SET status = ? WHERE url = ?", (status, url)
            )
        else:
            cursor = self._execute(
                "UPDATE sites SET status = ?, memo = ? WHERE url = ?", (status, memo, url)
            )
        return cursor.rowcount > 0

    # -- 조회 (엑셀/대시보드용) --------------------------------------------
    def sites_for_export(
        self, min_score: int, categories_excluded: Iterable[str] = ()
    ) -> list[sqlite3.Row]:
        excluded = tuple(categories_excluded)
        sql = "SELECT * FROM sites WHERE score >= ?"
        params: list[Any] = [min_score]
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            sql += f" AND category NOT IN ({placeholders})"
            params.extend(excluded)
        sql += " ORDER BY score DESC, last_seen_at DESC"
        return self._query(sql, params)

    def sites_by_category(self, category: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM sites WHERE category = ? ORDER BY last_seen_at DESC",
            (category,),
        )

    def sites_first_seen_since(self, iso_timestamp: str, min_score: int = 0) -> list[sqlite3.Row]:
        return self._query(
            """
            SELECT * FROM sites
             WHERE first_seen_at >= ? AND score >= ?
             ORDER BY first_seen_at DESC
            """,
            (iso_timestamp, min_score),
        )

    def observations_for_site(self, site_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self._query(
            """
            SELECT * FROM observations
             WHERE site_id = ?
             ORDER BY found_at DESC
             LIMIT ?
            """,
            (site_id, limit),
        )

    def sources_for_site(self, site_id: int) -> list[str]:
        rows = self._query(
            """
            SELECT DISTINCT source_url FROM observations
             WHERE site_id = ? AND source_url <> ''
             ORDER BY source_url
            """,
            (site_id,),
        )
        return [row["source_url"] for row in rows]

    def source_urls_grouped(self) -> dict[int, list[str]]:
        """사이트 id → 그 사이트를 홍보한 홍보사이트 URL 목록 (엑셀 출력용 일괄 조회)."""
        rows = self._query(
            """
            SELECT site_id, source_url FROM observations
             WHERE source_url <> ''
             GROUP BY site_id, source_url
             ORDER BY site_id, source_url
            """
        )
        grouped: dict[int, list[str]] = {}
        for row in rows:
            grouped.setdefault(int(row["site_id"]), []).append(row["source_url"])
        return grouped

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def summary(self, min_score: int = 0) -> dict[str, Any]:
        rows = self._query(
            """
            SELECT
                COUNT(*)                                          AS total,
                COALESCE(SUM(score >= ?), 0)                       AS scored,
                COALESCE(SUM(alive = 1), 0)                        AS alive,
                COALESCE(SUM(alive = 0), 0)                        AS dead,
                COALESCE(SUM(alive IS NULL), 0)                    AS unchecked,
                COALESCE(SUM(status = 'reported'), 0)              AS reported
              FROM sites
            """,
            (min_score,),
        )
        base = dict(rows[0]) if rows else {}
        day = self._query(
            "SELECT COUNT(*) AS n FROM sites WHERE first_seen_at >= ?", (days_ago(1),)
        )
        week = self._query(
            "SELECT COUNT(*) AS n FROM sites WHERE first_seen_at >= ?", (days_ago(7),)
        )
        by_category = self._query(
            """
            SELECT category, category_label, COUNT(*) AS n
              FROM sites WHERE score >= ?
             GROUP BY category, category_label
             ORDER BY n DESC
            """,
            (min_score,),
        )
        base.update(
            {
                "new_24h": int(day[0]["n"]) if day else 0,
                "new_7d": int(week[0]["n"]) if week else 0,
                "sources_total": self.count_sources(),
                "sources_enabled": self.count_sources(enabled_only=True),
                "by_category": [dict(row) for row in by_category],
            }
        )
        return base

    # -- 실행 이력 ---------------------------------------------------------
    def start_run(self) -> int:
        cursor = self._execute("INSERT INTO runs (started_at) VALUES (?)", (iso_now(),))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, stats: RunStats, duration_ms: int) -> None:
        self._execute(
            """
            UPDATE runs
               SET finished_at = ?, sources_total = ?, sources_ok = ?, sources_failed = ?,
                   pages_fetched = ?, candidates_found = ?, new_sites = ?, updated_sites = ?,
                   duration_ms = ?, note = ?
             WHERE id = ?
            """,
            (
                iso_now(),
                stats.sources_total,
                stats.sources_ok,
                stats.sources_failed,
                stats.pages_fetched,
                stats.candidates_found,
                stats.new_sites,
                stats.updated_sites,
                duration_ms,
                stats.note[:500],
                run_id,
            ),
        )

    def last_run(self) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # -- 정리 --------------------------------------------------------------
    def prune_observations(self, retention_days: int) -> int:
        """오래된 관측 이력을 지웁니다. 사이트 목록 자체는 지우지 않습니다."""
        if retention_days <= 0:
            return 0
        cursor = self._execute(
            "DELETE FROM observations WHERE found_at < ?", (days_ago(retention_days),)
        )
        deleted = cursor.rowcount
        if deleted > 0:
            log.info("오래된 관측 이력 %d건 정리", deleted)
        return max(0, deleted)
