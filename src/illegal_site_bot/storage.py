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

SCHEMA_VERSION = 2

SITE_STATUSES = ("new", "confirmed", "reported", "ignored")

#: 홍보사이트(수집원)의 생애 주기
#:   discovered  봇이 찾아냈지만 아직 평가하지 않음
#:   pending     평가까지 끝나고 사람 승인을 기다리는 중
#:   approved    수집 대상 (enabled 가 1 이면 실제로 돌기 시작)
#:   rejected    홍보사이트가 아니라고 판단 (일정 기간 뒤 재평가)
#:   auto_disabled  연속 실패가 많아 자동으로 내려둠
SOURCE_STATES = ("discovered", "pending", "approved", "rejected", "auto_disabled")

#: 수집 사이클이 실제로 도는 상태
CRAWLABLE_STATE = "approved"

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
    found_total          INTEGER NOT NULL DEFAULT 0,
    -- 아래는 스키마 v2(홍보사이트 자동 발견)에서 추가된 컬럼입니다.
    state                TEXT NOT NULL DEFAULT 'approved',
    origin               TEXT NOT NULL DEFAULT 'manual',
    discovered_from_id   INTEGER,
    depth                INTEGER NOT NULL DEFAULT 0,
    promo_score          INTEGER NOT NULL DEFAULT 0,
    promo_reasons        TEXT NOT NULL DEFAULT '',
    discovered_at        TEXT,
    evaluated_at         TEXT,
    evaluation_failures  INTEGER NOT NULL DEFAULT 0,
    reviewed_at          TEXT,
    reviewed_by          TEXT NOT NULL DEFAULT '',
    link_domains         TEXT NOT NULL DEFAULT '',
    mirror_of_id         INTEGER
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
    note             TEXT NOT NULL DEFAULT '',
    candidates_added INTEGER NOT NULL DEFAULT 0,
    sources_approved INTEGER NOT NULL DEFAULT 0
);
"""

#: v2 컬럼을 만든 *뒤에* 걸어야 하는 인덱스.
#: v1 DB 에는 아직 state 컬럼이 없으므로 _SCHEMA 에 넣으면 마이그레이션이 깨집니다.
_V2_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sources_state ON sources(state);
"""

#: 스키마 v1 로 만들어진 DB 를 v2 로 올릴 때 추가해야 하는 컬럼.
#: 기존 행은 DEFAULT 값으로 채워지므로(수동 등록 = approved/manual/depth 0)
#: 사용자가 쓰던 DB 를 지우지 않고 그대로 이어서 쓸 수 있습니다.
_V2_COLUMNS: dict[str, dict[str, str]] = {
    "sources": {
        "state": "TEXT NOT NULL DEFAULT 'approved'",
        "origin": "TEXT NOT NULL DEFAULT 'manual'",
        "discovered_from_id": "INTEGER",
        "depth": "INTEGER NOT NULL DEFAULT 0",
        "promo_score": "INTEGER NOT NULL DEFAULT 0",
        "promo_reasons": "TEXT NOT NULL DEFAULT ''",
        "discovered_at": "TEXT",
        "evaluated_at": "TEXT",
        "evaluation_failures": "INTEGER NOT NULL DEFAULT 0",
        "reviewed_at": "TEXT",
        "reviewed_by": "TEXT NOT NULL DEFAULT ''",
        "link_domains": "TEXT NOT NULL DEFAULT ''",
        "mirror_of_id": "INTEGER",
    },
    "runs": {
        "candidates_added": "INTEGER NOT NULL DEFAULT 0",
        "sources_approved": "INTEGER NOT NULL DEFAULT 0",
    },
}


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
    state: str = CRAWLABLE_STATE
    origin: str = "manual"
    depth: int = 0
    promo_score: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SourceRow":
        keys = row.keys()
        return cls(
            id=row["id"],
            url=row["url"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            render=bool(row["render"]),
            max_pages=row["max_pages"],
            note=row["note"],
            state=row["state"] if "state" in keys else CRAWLABLE_STATE,
            origin=row["origin"] if "origin" in keys else "manual",
            depth=int(row["depth"] or 0) if "depth" in keys else 0,
            promo_score=int(row["promo_score"] or 0) if "promo_score" in keys else 0,
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
    candidates_added: int = 0
    sources_approved: int = 0
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
    def _existing_columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> list[str]:
        """없는 컬럼만 ALTER TABLE 로 추가하고, 추가한 컬럼 이름을 돌려줍니다."""
        present = self._existing_columns(table)
        added: list[str] = []
        for name, definition in columns.items():
            if name in present:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.append(f"{table}.{name}")
        return added

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)

            row = self._conn.execute("SELECT version FROM schema_info").fetchone()
            current = int(row["version"]) if row is not None else None

            if current is not None and current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"DB 스키마 버전({current})이 이 프로그램({SCHEMA_VERSION})보다 "
                    "높습니다. 봇을 최신 버전으로 업데이트하세요."
                )

            # v1 로 만들어진 기존 DB 에 v2 컬럼을 채워 넣습니다.
            # CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블을 바꾸지 않기 때문에
            # 여기서 따로 처리해야 합니다.
            added: list[str] = []
            for table, columns in _V2_COLUMNS.items():
                added.extend(self._ensure_columns(table, columns))
            if added:
                log.info("DB 스키마를 v%d 로 올렸습니다: %s", SCHEMA_VERSION, ", ".join(added))

            # 컬럼이 모두 갖춰진 뒤에 인덱스를 겁니다.
            self._conn.executescript(_V2_INDEXES)

            if current is None:
                self._conn.execute(
                    "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif current < SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION,))

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
        """홍보사이트를 사람이 등록하거나 갱신하고 id 를 돌려줍니다.

        봇이 찾아둔 후보를 이 방법으로 등록하면 **depth 0 시드로 승격**됩니다.
        거기서부터 다시 max_depth 만큼 탐색이 뻗어나갑니다.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sources WHERE url = ?", (url,)
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO sources (
                        url, name, enabled, render, max_pages, note, added_at,
                        state, origin, depth
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', 0)
                    """,
                    (
                        url,
                        name,
                        int(enabled),
                        int(render),
                        max_pages,
                        note,
                        iso_now(),
                        CRAWLABLE_STATE,
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid)

            if update_existing:
                self._conn.execute(
                    """
                    UPDATE sources
                       SET name = ?, enabled = ?, render = ?, max_pages = ?, note = ?,
                           state = ?, origin = 'manual', depth = 0,
                           reviewed_at = ?, reviewed_by = 'manual'
                     WHERE id = ?
                    """,
                    (
                        name,
                        int(enabled),
                        int(render),
                        max_pages,
                        note,
                        CRAWLABLE_STATE,
                        iso_now(),
                        row["id"],
                    ),
                )
                self._conn.commit()
            return int(row["id"])

    def list_sources(self, enabled_only: bool = False) -> list[SourceRow]:
        """홍보사이트 목록. ``enabled_only`` 면 실제로 수집을 도는 것만."""
        sql = "SELECT * FROM sources"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE enabled = 1 AND state = ?"
            params.append(CRAWLABLE_STATE)
        sql += " ORDER BY id"
        return [SourceRow.from_row(row) for row in self._query(sql, params)]

    def source_rows(self) -> list[sqlite3.Row]:
        """엑셀 출력용 전체 컬럼."""
        return self._query("SELECT * FROM sources ORDER BY id")

    def count_sources(self, enabled_only: bool = False, state: str | None = None) -> int:
        """홍보사이트 개수.

        기본값은 '승인된 것'만 셉니다. 발견 단계의 후보까지 섞어 세면
        엑셀/대시보드의 수집원 숫자가 부풀어 보이기 때문입니다.
        """
        sql = "SELECT COUNT(*) AS n FROM sources"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE enabled = 1 AND state = ?"
            params.append(CRAWLABLE_STATE)
        elif state is not None:
            sql += " WHERE state = ?"
            params.append(state)
        else:
            sql += " WHERE state = ?"
            params.append(CRAWLABLE_STATE)
        return int(self._query(sql, params)[0]["n"])

    def count_sources_all_states(self) -> int:
        """후보까지 포함한 전체 행 수 (총량 상한 검사용)."""
        return int(self._query("SELECT COUNT(*) AS n FROM sources")[0]["n"])

    # -- 홍보사이트 자동 발견 ----------------------------------------------
    def add_candidate(
        self,
        url: str,
        *,
        name: str = "",
        discovered_from_id: int | None,
        depth: int,
        note: str = "",
    ) -> int | None:
        """새로 발견한 홍보사이트 후보를 등록합니다.

        이미 있는 주소면 ``None`` 을 돌려주되, 더 짧은 경로로 발견된 경우
        depth 만 낮춰 갱신합니다.
        """
        now = iso_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, depth FROM sources WHERE url = ?", (url,)
            ).fetchone()
            if row is not None:
                if depth < int(row["depth"] or 0):
                    self._conn.execute(
                        "UPDATE sources SET depth = ?, discovered_from_id = ? WHERE id = ?",
                        (depth, discovered_from_id, row["id"]),
                    )
                    self._conn.commit()
                return None

            cursor = self._conn.execute(
                """
                INSERT INTO sources (
                    url, name, enabled, render, note, added_at,
                    state, origin, discovered_from_id, depth, discovered_at
                ) VALUES (?, ?, 0, 0, ?, ?, 'discovered', 'auto', ?, ?, ?)
                """,
                (url, name, note, now, discovered_from_id, depth, now),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def source_by_id(self, source_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM sources WHERE id = ?", (source_id,))
        return rows[0] if rows else None

    def source_by_url(self, url: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM sources WHERE url = ?", (url,))
        return rows[0] if rows else None

    def sources_by_state(self, *states: str) -> list[sqlite3.Row]:
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        return self._query(
            f"""
            SELECT * FROM sources
             WHERE state IN ({placeholders})
             ORDER BY promo_score DESC, discovered_at ASC, id ASC
            """,
            states,
        )

    def sources_to_evaluate(self, limit: int, reevaluate_before: str | None) -> list[sqlite3.Row]:
        """평가 대기 중인 후보. 아직 안 본 것 먼저, 그다음 재평가 대상."""
        rows = self._query(
            """
            SELECT * FROM sources
             WHERE state = 'discovered'
             ORDER BY depth ASC, discovered_at ASC
             LIMIT ?
            """,
            (limit,),
        )
        if len(rows) >= limit or not reevaluate_before:
            return rows[:limit]

        rows.extend(
            self._query(
                """
                SELECT * FROM sources
                 WHERE state = 'rejected'
                   AND (evaluated_at IS NULL OR evaluated_at < ?)
                 ORDER BY evaluated_at ASC
                 LIMIT ?
                """,
                (reevaluate_before, limit - len(rows)),
            )
        )
        return rows

    def record_evaluation(
        self,
        source_id: int,
        *,
        state: str,
        promo_score: int,
        promo_reasons: str,
        link_domains: str = "",
        mirror_of_id: int | None = None,
        name: str = "",
    ) -> None:
        """2단계 평가 결과를 기록합니다. 승인이면 바로 수집 대상이 됩니다."""
        if state not in SOURCE_STATES:
            raise ValueError(f"state 는 {SOURCE_STATES} 중 하나여야 합니다.")
        with self._lock:
            self._conn.execute(
                """
                UPDATE sources
                   SET state = ?, promo_score = ?, promo_reasons = ?,
                       link_domains = ?, mirror_of_id = ?, evaluated_at = ?,
                       enabled = CASE WHEN ? = 'approved' THEN 1 ELSE enabled END,
                       name = CASE WHEN name = '' AND ? <> '' THEN ? ELSE name END
                 WHERE id = ?
                """,
                (
                    state,
                    promo_score,
                    promo_reasons,
                    link_domains,
                    mirror_of_id,
                    iso_now(),
                    state,
                    name,
                    name,
                    source_id,
                ),
            )
            self._conn.commit()

    def record_evaluation_failure(self, source_id: int, max_failures: int) -> int:
        """평가용 접속이 실패했을 때. 누적 실패가 한도를 넘으면 기각합니다."""
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET evaluation_failures = evaluation_failures + 1 WHERE id = ?",
                (source_id,),
            )
            row = self._conn.execute(
                "SELECT evaluation_failures FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            failures = int(row["evaluation_failures"] if row else 0)
            if failures >= max_failures:
                self._conn.execute(
                    """
                    UPDATE sources
                       SET state = 'rejected', evaluated_at = ?,
                           promo_reasons = '평가용 접속 실패가 반복됨'
                     WHERE id = ?
                    """,
                    (iso_now(), source_id),
                )
            self._conn.commit()
            return failures

    def review_source(self, url: str, state: str, by: str = "cli", note: str = "") -> bool:
        """사람이 후보를 승인/거부합니다."""
        if state not in SOURCE_STATES:
            raise ValueError(f"state 는 {SOURCE_STATES} 중 하나여야 합니다.")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE sources
                   SET state = ?, reviewed_at = ?, reviewed_by = ?,
                       enabled = CASE WHEN ? = 'approved' THEN 1 ELSE 0 END,
                       note = CASE WHEN ? <> '' THEN ? ELSE note END
                 WHERE url = ?
                """,
                (state, iso_now(), by, state, note, note, url),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def known_illegal_domains(self, domains: Sequence[str]) -> set[str]:
        """주어진 도메인 중 이미 불법사이트로 기록된 것 (2단계 평가의 A 신호)."""
        if not domains:
            return set()
        unique = list({domain for domain in domains if domain})
        found: set[str] = set()
        # SQLite 의 변수 개수 제한(기본 999)을 넘지 않도록 나눠서 조회합니다.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._query(
                f"SELECT DISTINCT domain FROM sites WHERE domain IN ({placeholders})", chunk
            )
            found.update(row["domain"] for row in rows)
        return found

    def source_domains(self) -> set[str]:
        """등록된(후보 포함) 홍보사이트의 등록가능도메인 집합."""
        rows = self._query("SELECT url FROM sources")
        return {registrable_domain(row["url"]) for row in rows}

    def link_domain_sets(self, exclude_id: int) -> list[tuple[int, str, set[str]]]:
        """미러 판정을 위해 다른 홍보사이트의 외부 링크 도메인 집합을 가져옵니다."""
        rows = self._query(
            """
            SELECT id, url, link_domains FROM sources
             WHERE link_domains <> '' AND id <> ?
            """,
            (exclude_id,),
        )
        result: list[tuple[int, str, set[str]]] = []
        for row in rows:
            domains = {item for item in row["link_domains"].split(",") if item}
            if domains:
                result.append((int(row["id"]), row["url"], domains))
        return result

    def auto_disable_failing_sources(self, max_failures: int) -> list[str]:
        """연속 실패가 한도를 넘은 홍보사이트를 자동으로 내립니다."""
        if max_failures <= 0:
            return []
        rows = self._query(
            """
            SELECT url FROM sources
             WHERE state = ? AND consecutive_failures >= ?
            """,
            (CRAWLABLE_STATE, max_failures),
        )
        if not rows:
            return []
        self._execute(
            """
            UPDATE sources
               SET state = 'auto_disabled', enabled = 0
             WHERE state = ? AND consecutive_failures >= ?
            """,
            (CRAWLABLE_STATE, max_failures),
        )
        return [row["url"] for row in rows]

    def discovery_stats_by_depth(self) -> list[sqlite3.Row]:
        """깊이별 성과 — max_depth 를 조정할 때 근거가 됩니다."""
        return self._query(
            """
            SELECT depth,
                   COUNT(*)                     AS sources,
                   COALESCE(SUM(found_total), 0) AS found_total,
                   COALESCE(AVG(found_total), 0) AS found_avg
              FROM sources
             WHERE state = ?
             GROUP BY depth
             ORDER BY depth
            """,
            (CRAWLABLE_STATE,),
        )

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

    def reclassify_site_as_promo(self, domain: str) -> int:
        """홍보사이트로 확정된 도메인이 불법사이트 목록에도 들어가 있으면 제외 처리.

        홍보사이트는 링크 텍스트에 도박 키워드가 잔뜩 있어서 불법사이트로도
        잡히기 쉽습니다. 2단계 평가에서 홍보사이트로 확정되면 여기서 정리합니다.
        """
        if not domain:
            return 0
        cursor = self._execute(
            """
            UPDATE sites
               SET status = 'ignored',
                   memo = CASE WHEN memo = '' THEN ? ELSE memo END
             WHERE domain = ? AND status <> 'ignored'
            """,
            ("홍보사이트로 재분류됨 (수집원 목록으로 이동)", domain),
        )
        return max(0, cursor.rowcount)

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
        self,
        min_score: int,
        categories_excluded: Iterable[str] = (),
        include_ignored: bool = True,
    ) -> list[sqlite3.Row]:
        """엑셀에 실을 사이트 목록.

        ``include_ignored=False`` 면 제외 처리된 것(오탐으로 판정했거나
        홍보사이트로 재분류된 것)은 빼고 돌려줍니다.
        """
        excluded = tuple(categories_excluded)
        sql = "SELECT * FROM sites WHERE score >= ?"
        params: list[Any] = [min_score]
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            sql += f" AND category NOT IN ({placeholders})"
            params.extend(excluded)
        if not include_ignored:
            sql += " AND status <> 'ignored'"
        sql += " ORDER BY score DESC, last_seen_at DESC"
        return self._query(sql, params)

    def sites_by_category(self, category: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM sites WHERE category = ? ORDER BY last_seen_at DESC",
            (category,),
        )

    def sites_first_seen_since(
        self, iso_timestamp: str, min_score: int = 0, include_ignored: bool = True
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT * FROM sites
             WHERE first_seen_at >= ? AND score >= ?
        """
        if not include_ignored:
            sql += " AND status <> 'ignored'"
        sql += " ORDER BY first_seen_at DESC"
        return self._query(sql, (iso_timestamp, min_score))

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
                COALESCE(SUM(status = 'reported'), 0)              AS reported,
                COALESCE(SUM(status = 'ignored'), 0)               AS ignored
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
                "candidates_pending": self.count_sources(state="pending"),
                "candidates_discovered": self.count_sources(state="discovered"),
                "sources_auto": int(
                    self._query(
                        "SELECT COUNT(*) AS n FROM sources WHERE origin = 'auto' AND state = ?",
                        (CRAWLABLE_STATE,),
                    )[0]["n"]
                ),
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
                   duration_ms = ?, note = ?, candidates_added = ?, sources_approved = ?
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
                stats.candidates_added,
                stats.sources_approved,
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
