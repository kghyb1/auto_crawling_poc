import pytest

from illegal_site_bot.storage import SCHEMA_VERSION, RunStats, Storage
from illegal_site_bot.timeutil import days_ago


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db" / "test.sqlite3")
    yield store
    store.close()


def _record(storage, url="https://casino-a.xyz/", score=60, category="gambling"):
    return storage.record_site(
        url,
        category=category,
        category_label="도박/베팅",
        base_score=score,
        matched_keywords="카지노",
        reasons="키워드 일치(1건)",
    )


class TestSources:
    def test_insert_then_update_keeps_one_row(self, storage):
        first = storage.upsert_source("https://promo.test/", name="첫 이름")
        second = storage.upsert_source("https://promo.test/", name="바뀐 이름")
        assert first == second
        assert storage.count_sources() == 1
        assert storage.list_sources()[0].name == "바뀐 이름"

    def test_update_existing_false_keeps_original(self, storage):
        storage.upsert_source("https://promo.test/", name="원래")
        storage.upsert_source("https://promo.test/", name="새것", update_existing=False)
        assert storage.list_sources()[0].name == "원래"

    def test_enabled_only_filter(self, storage):
        storage.upsert_source("https://a.test/", enabled=True)
        storage.upsert_source("https://b.test/", enabled=False)
        assert storage.count_sources() == 2
        assert storage.count_sources(enabled_only=True) == 1
        assert [source.url for source in storage.list_sources(enabled_only=True)] == [
            "https://a.test/"
        ]

    def test_toggle_and_remove(self, storage):
        storage.upsert_source("https://a.test/")
        assert storage.set_source_enabled("https://a.test/", False) is True
        assert storage.list_sources()[0].enabled is False
        assert storage.remove_source("https://a.test/") is True
        assert storage.count_sources() == 0

    def test_toggle_unknown_url_returns_false(self, storage):
        assert storage.set_source_enabled("https://nope.test/", True) is False

    def test_failure_counter_accumulates_then_resets(self, storage):
        source_id = storage.upsert_source("https://a.test/")
        storage.record_source_result(source_id, ok=False, status="500", error="서버 오류")
        storage.record_source_result(source_id, ok=False, status="500", error="서버 오류")
        row = storage.source_rows()[0]
        assert row["consecutive_failures"] == 2
        assert row["last_error"] == "서버 오류"

        storage.record_source_result(source_id, ok=True, status="200", found=5)
        row = storage.source_rows()[0]
        assert row["consecutive_failures"] == 0
        assert row["last_error"] == ""
        assert row["found_total"] == 5


class TestSites:
    def test_first_record_is_new(self, storage):
        _site_id, is_new = _record(storage)
        assert is_new is True

    def test_second_record_is_not_new_and_counts_up(self, storage):
        site_id, _ = _record(storage)
        again_id, is_new = _record(storage)
        assert again_id == site_id
        assert is_new is False
        row = storage.sites_for_export(0)[0]
        assert row["seen_count"] == 2

    def test_derives_domain_and_host(self, storage):
        _record(storage, url="https://www.casino-a.xyz/enter")
        row = storage.sites_for_export(0)[0]
        assert row["domain"] == "casino-a.xyz"
        assert row["host"] == "www.casino-a.xyz"

    def test_keeps_highest_score_and_its_category(self, storage):
        _record(storage, score=70, category="gambling")
        _record(storage, score=30, category="adult")
        row = storage.sites_for_export(0)[0]
        assert row["base_score"] == 70
        assert row["category"] == "gambling"

    def test_upgrades_category_when_new_score_is_higher(self, storage):
        _record(storage, score=30, category="gambling")
        storage.record_site(
            "https://casino-a.xyz/",
            category="adult",
            category_label="성인/음란",
            base_score=80,
            matched_keywords="야동",
            reasons="키워드 일치(1건)",
        )
        row = storage.sites_for_export(0)[0]
        assert row["category"] == "adult"
        assert row["category_label"] == "성인/음란"

    def test_min_score_filter(self, storage):
        _record(storage, url="https://high.xyz/", score=80)
        _record(storage, url="https://low.xyz/", score=20)
        urls = [row["url"] for row in storage.sites_for_export(50)]
        assert urls == ["https://high.xyz/"]

    def test_category_can_be_excluded_from_export(self, storage):
        _record(storage, url="https://a.xyz/", score=60, category="gambling")
        _record(storage, url="https://t.me/x", score=60, category="contact")
        urls = [row["url"] for row in storage.sites_for_export(0, categories_excluded=("contact",))]
        assert urls == ["https://a.xyz/"]

    def test_status_and_memo_are_recorded(self, storage):
        _record(storage)
        assert storage.set_site_status("https://casino-a.xyz/", "reported", "신고 접수") is True
        row = storage.sites_for_export(0)[0]
        assert row["status"] == "reported"
        assert row["memo"] == "신고 접수"

    def test_status_rejects_unknown_value(self, storage):
        _record(storage)
        with pytest.raises(ValueError):
            storage.set_site_status("https://casino-a.xyz/", "존재하지않음")


class TestObservationsAndScore:
    def test_cross_source_bonus_applied(self, storage):
        site_id, _ = _record(storage, score=50)
        for source_url in ("https://promo1.test/", "https://promo2.test/"):
            storage.record_observation(
                site_id, None, source_url, source_url, "카지노", "a_href", True, 50
            )
        score = storage.refresh_site_score(site_id, bonus_per_source=6, bonus_max=24)
        assert score == 56  # 서로 다른 홍보사이트 2곳 → +6

        row = storage.sites_for_export(0)[0]
        assert row["distinct_sources"] == 2
        assert row["score"] == 56

    def test_bonus_is_capped(self, storage):
        site_id, _ = _record(storage, score=50)
        for index in range(10):
            url = f"https://promo{index}.test/"
            storage.record_observation(site_id, None, url, url, "", "a_href", False, 50)
        assert storage.refresh_site_score(site_id, 6, 24) == 74

    def test_same_source_counted_once(self, storage):
        site_id, _ = _record(storage, score=50)
        for _ in range(3):
            storage.record_observation(
                site_id, None, "https://promo1.test/", "https://promo1.test/", "", "a_href", False, 50
            )
        assert storage.refresh_site_score(site_id, 6, 24) == 50

    def test_source_urls_grouped(self, storage):
        site_id, _ = _record(storage)
        storage.record_observation(site_id, None, "https://p1.test/", "", "", "a_href", False, 50)
        storage.record_observation(site_id, None, "https://p2.test/", "", "", "a_href", False, 50)
        assert storage.source_urls_grouped()[site_id] == ["https://p1.test/", "https://p2.test/"]

    def test_prune_removes_only_old_observations(self, storage):
        site_id, _ = _record(storage)
        storage.record_observation(site_id, None, "https://p1.test/", "", "", "a_href", False, 50)
        with storage._lock:  # 오래된 이력을 직접 하나 만들어 둡니다
            storage._conn.execute(
                """
                INSERT INTO observations (site_id, source_url, found_at)
                VALUES (?, 'https://old.test/', ?)
                """,
                (site_id, days_ago(400)),
            )
            storage._conn.commit()

        assert storage.prune_observations(retention_days=180) == 1
        assert len(storage.observations_for_site(site_id)) == 1

    def test_prune_disabled_when_retention_zero(self, storage):
        site_id, _ = _record(storage)
        storage.record_observation(site_id, None, "https://p1.test/", "", "", "a_href", False, 50)
        assert storage.prune_observations(retention_days=0) == 0


class TestAliveCheck:
    def test_marks_alive_and_status(self, storage):
        site_id, _ = _record(storage)
        storage.update_alive(site_id, True, 200)
        row = storage.sites_for_export(0)[0]
        assert row["alive"] == 1
        assert row["http_status"] == 200
        assert row["last_checked_at"]

    def test_corrects_url_when_only_http_works(self, storage):
        site_id, _ = _record(storage, url="https://casino-a.xyz/")
        storage.update_alive(site_id, True, 200, working_url="http://casino-a.xyz/")
        assert storage.sites_for_export(0)[0]["url"] == "http://casino-a.xyz/"

    def test_does_not_create_duplicate_url(self, storage):
        first_id, _ = _record(storage, url="https://casino-a.xyz/")
        _record(storage, url="http://casino-a.xyz/")
        storage.update_alive(first_id, True, 200, working_url="http://casino-a.xyz/")
        # 이미 같은 URL 이 다른 행에 있으므로 교체하지 않습니다.
        assert {row["url"] for row in storage.sites_for_export(0)} == {
            "https://casino-a.xyz/",
            "http://casino-a.xyz/",
        }

    def test_unchecked_sites_come_first(self, storage):
        checked_id, _ = _record(storage, url="https://checked.xyz/")
        storage.update_alive(checked_id, True, 200)
        _record(storage, url="https://never-checked.xyz/")
        rows = storage.sites_to_check(limit=10)
        assert rows[0]["url"] == "https://never-checked.xyz/"


class TestSummaryAndRuns:
    def test_summary_counts(self, storage):
        site_id, _ = _record(storage, url="https://a.xyz/", score=80)
        storage.refresh_site_score(site_id, 0, 0)
        _record(storage, url="https://b.xyz/", score=10)
        storage.upsert_source("https://promo.test/", enabled=True)

        summary = storage.summary(min_score=50)
        assert summary["total"] == 2
        assert summary["scored"] == 1
        assert summary["new_24h"] == 2
        assert summary["sources_enabled"] == 1
        assert summary["unchecked"] == 2
        labels = {entry["category"] for entry in summary["by_category"]}
        assert labels == {"gambling"}

    def test_run_lifecycle(self, storage):
        run_id = storage.start_run()
        stats = RunStats(sources_total=3, sources_ok=2, sources_failed=1, new_sites=5)
        storage.finish_run(run_id, stats, duration_ms=1500)

        row = storage.last_run()
        assert row is not None
        assert row["sources_total"] == 3
        assert row["new_sites"] == 5
        assert row["duration_ms"] == 1500
        assert row["finished_at"]
        assert len(storage.recent_runs()) == 1


def test_reopening_database_keeps_data(tmp_path):
    path = tmp_path / "db" / "test.sqlite3"
    first = Storage(path)
    _record(first)
    first.close()

    second = Storage(path)
    try:
        assert len(second.sites_for_export(0)) == 1
    finally:
        second.close()


# 스키마 v1 (자동 발견 기능이 없던 버전) 로 만들어진 DB 를 재현합니다.
_V1_SCHEMA = """
CREATE TABLE schema_info (version INTEGER NOT NULL);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    render INTEGER NOT NULL DEFAULT 0,
    max_pages INTEGER,
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    last_crawled_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    found_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sites (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    host TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'unknown',
    category_label TEXT NOT NULL DEFAULT '미분류',
    base_score INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    matched_keywords TEXT NOT NULL DEFAULT '',
    reasons TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    redirect_from TEXT NOT NULL DEFAULT '',
    alive INTEGER,
    http_status INTEGER,
    last_checked_at TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    memo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    source_url TEXT NOT NULL DEFAULT '',
    page_url TEXT NOT NULL DEFAULT '',
    anchor_text TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    banner INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    found_at TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_total INTEGER NOT NULL DEFAULT 0,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    new_sites INTEGER NOT NULL DEFAULT 0,
    updated_sites INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
"""


class TestSchemaMigration:
    """기존 사용자의 DB 를 지우지 않고 v1 → v2 로 올릴 수 있어야 합니다."""

    @pytest.fixture
    def v1_database(self, tmp_path):
        import sqlite3 as sqlite

        path = tmp_path / "db" / "v1.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite.connect(str(path))
        conn.executescript(_V1_SCHEMA)
        conn.execute("INSERT INTO schema_info (version) VALUES (1)")
        conn.execute(
            """
            INSERT INTO sources (url, name, enabled, added_at, found_total)
            VALUES ('https://old-promo.test/', '기존 수집원', 1, '2026-01-01T00:00:00Z', 7)
            """
        )
        conn.execute(
            """
            INSERT INTO sites (url, domain, host, first_seen_at, last_seen_at, score)
            VALUES ('https://old-site.xyz/', 'old-site.xyz', 'old-site.xyz',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 70)
            """
        )
        conn.commit()
        conn.close()
        return path

    def test_upgrade_keeps_existing_data(self, v1_database):
        storage = Storage(v1_database)
        try:
            sources = storage.list_sources()
            assert [source.url for source in sources] == ["https://old-promo.test/"]
            assert sources[0].name == "기존 수집원"
            assert [row["url"] for row in storage.sites_for_export(0)] == [
                "https://old-site.xyz/"
            ]
        finally:
            storage.close()

    def test_existing_sources_become_approved_manual_seeds(self, v1_database):
        storage = Storage(v1_database)
        try:
            source = storage.list_sources()[0]
            assert source.state == "approved"
            assert source.origin == "manual"
            assert source.depth == 0
            # 그대로 수집 대상이어야 합니다.
            assert [s.url for s in storage.list_sources(enabled_only=True)] == [
                "https://old-promo.test/"
            ]
        finally:
            storage.close()

    def test_new_columns_are_usable_after_upgrade(self, v1_database):
        storage = Storage(v1_database)
        try:
            new_id = storage.add_candidate(
                "https://found.test/", discovered_from_id=None, depth=1
            )
            assert new_id is not None
            storage.record_evaluation(
                new_id, state="pending", promo_score=55, promo_reasons="테스트"
            )
            row = storage.source_by_url("https://found.test/")
            assert row["state"] == "pending" and row["promo_score"] == 55
        finally:
            storage.close()

    def test_version_is_bumped_and_upgrade_is_idempotent(self, v1_database):
        Storage(v1_database).close()
        storage = Storage(v1_database)  # 두 번 열어도 문제없어야 합니다
        try:
            version = storage._query("SELECT version FROM schema_info")[0]["version"]
            assert version == SCHEMA_VERSION
            assert len(storage.list_sources()) == 1
        finally:
            storage.close()

    def test_runs_table_gains_discovery_counters(self, v1_database):
        storage = Storage(v1_database)
        try:
            run_id = storage.start_run()
            storage.finish_run(
                run_id, RunStats(candidates_added=3, sources_approved=1), duration_ms=10
            )
            row = storage.last_run()
            assert row["candidates_added"] == 3
            assert row["sources_approved"] == 1
        finally:
            storage.close()


class TestAliveUrlCorrection:
    """리다이렉트로 URL 이 바뀌면 domain/host 도 같이 따라가야 합니다."""

    def test_domain_and_host_follow_the_new_url(self, storage):
        site_id, _ = _record(storage, url="https://old-domain.com/")
        storage.update_alive(site_id, True, 200, working_url="http://totally-different.net/")

        row = storage.sites_for_export(0)[0]
        assert row["url"] == "http://totally-different.net/"
        assert row["host"] == "totally-different.net"
        assert row["domain"] == "totally-different.net"

    def test_mark_finds_the_site_by_its_new_host(self, storage):
        site_id, _ = _record(storage, url="https://old-domain.com/")
        storage.update_alive(site_id, True, 200, working_url="http://new-home.net/")
        assert storage.mark_sites("new-home.net", "reported") == ["http://new-home.net/"]
