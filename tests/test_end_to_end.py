"""통합 테스트: 로컬 가짜 홍보사이트 → 수집 → 판별 → DB → 엑셀.

외부 인터넷에는 나가지 않습니다 (conftest 의 로컬 HTTP 서버만 사용).
"""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from illegal_site_bot.classifier import Classifier, load_rules
from illegal_site_bot.exporter import Exporter
from illegal_site_bot.fetcher import Fetcher
from illegal_site_bot.pipeline import Pipeline
from illegal_site_bot.storage import Storage


@pytest.fixture
def bot(config, promo_server):
    """가짜 홍보사이트 한 곳이 등록된 상태의 파이프라인 묶음."""
    storage = Storage(config.database_path)
    classifier = Classifier(load_rules(config.root / "config" / "rules.yaml"))
    fetcher = Fetcher(config.crawl)
    pipeline = Pipeline(
        config=config,
        storage=storage,
        classifier=classifier,
        fetcher=fetcher,
        renderer=None,
        pagination_patterns=("page=", "board"),
    )
    storage.upsert_source(promo_server, name="테스트 홍보사이트")
    try:
        yield {
            "config": config,
            "storage": storage,
            "classifier": classifier,
            "pipeline": pipeline,
            "source": promo_server,
        }
    finally:
        fetcher.close()
        storage.close()


def _collected_urls(storage: Storage) -> set[str]:
    return {row["url"] for row in storage.sites_for_export(0)}


class TestCycle:
    def test_cycle_reports_success(self, bot):
        stats = bot["pipeline"].run_cycle()
        assert stats.sources_total == 1
        assert stats.sources_ok == 1
        assert stats.sources_failed == 0
        assert stats.new_sites > 0

    def test_follows_internal_pagination(self, bot):
        stats = bot["pipeline"].run_cycle()
        # 메인 페이지 + 2페이지 (max_pages_per_source = 2)
        assert stats.pages_fetched == 2
        assert "https://baccarat-vip-365.club/" in _collected_urls(bot["storage"])

    def test_collects_links_from_every_extraction_path(self, bot):
        bot["pipeline"].run_cycle()
        urls = _collected_urls(bot["storage"])
        assert "https://casino-abc777.xyz/" in urls          # 이미지 배너 <a>
        assert "http://toto-safe-999.top/join" in urls       # 일반 <a>
        assert "https://slot-yamato-55.vip/" in urls         # data-href
        assert "https://holdem-king.cc/lobby" in urls        # onclick
        assert "https://freeya-dong.com/" in urls            # 본문 텍스트 abc[.]com
        assert "https://webtoon24-free.site/" in urls        # <script> 문자열

    def test_excludes_search_engines_and_low_score_noise(self, bot):
        bot["pipeline"].run_cycle()
        urls = _collected_urls(bot["storage"])
        assert not any("google.com" in url for url in urls)
        assert "https://plain-ad-frame.example/frame" not in urls

    def test_categorises_by_content(self, bot):
        bot["pipeline"].run_cycle()
        rows = {row["url"]: row for row in bot["storage"].sites_for_export(0)}
        assert rows["https://casino-abc777.xyz/"]["category"] == "gambling"
        assert rows["https://freeya-dong.com/"]["category"] == "adult"
        assert rows["https://t.me/promo_admin_contact"]["category"] == "contact"

    def test_records_which_promo_site_it_came_from(self, bot):
        bot["pipeline"].run_cycle()
        rows = {row["url"]: row for row in bot["storage"].sites_for_export(0)}
        site_id = rows["https://casino-abc777.xyz/"]["id"]
        assert bot["storage"].sources_for_site(site_id) == [bot["source"]]

    def test_source_result_is_recorded(self, bot):
        bot["pipeline"].run_cycle()
        row = bot["storage"].source_rows()[0]
        assert row["last_status"] == "200"
        assert row["consecutive_failures"] == 0
        assert row["found_total"] > 0
        assert row["last_crawled_at"]

    def test_second_cycle_finds_nothing_new(self, bot):
        first = bot["pipeline"].run_cycle()
        second = bot["pipeline"].run_cycle()
        assert second.new_sites == 0
        assert second.updated_sites == first.new_sites

        row = next(
            r for r in bot["storage"].sites_for_export(0)
            if r["url"] == "https://casino-abc777.xyz/"
        )
        assert row["seen_count"] == 2

    def test_stop_request_ends_cycle_early(self, bot):
        stats = bot["pipeline"].run_cycle(stop_check=lambda: True)
        assert stats.pages_fetched == 0
        assert stats.new_sites == 0

    def test_unreachable_source_is_marked_failed(self, bot, config):
        bot["storage"].upsert_source("http://127.0.0.1:9/", name="닫힌 포트")
        stats = bot["pipeline"].run_cycle()
        assert stats.sources_failed == 1
        failed = next(row for row in bot["storage"].source_rows() if ":9/" in row["url"])
        assert failed["consecutive_failures"] == 1
        assert failed["last_error"]

    def test_same_site_seen_from_two_promo_sources_scores_higher(self, bot, promo_server):
        # 점수가 100 으로 포화되지 않은 항목으로 확인합니다.
        target = "https://holdem-king.cc/lobby"
        bot["pipeline"].run_cycle()
        single = next(
            row for row in bot["storage"].sites_for_export(0) if row["url"] == target
        )
        assert single["score"] < 100

        # 같은 내용을 다른 주소로 한 번 더 등록하면 '여러 곳에서 홍보됨'이 됩니다.
        bot["storage"].upsert_source(promo_server + "index.html", name="두 번째 경로")
        bot["pipeline"].run_cycle()
        doubled = next(
            row for row in bot["storage"].sites_for_export(0) if row["url"] == target
        )
        assert doubled["distinct_sources"] == 2
        assert doubled["score"] > single["score"]


class TestExcelExport:
    @pytest.fixture
    def exported(self, bot):
        bot["pipeline"].run_cycle()
        exporter = Exporter(bot["config"], bot["storage"], bot["classifier"])
        return exporter.export()

    def test_creates_configured_file(self, exported, config):
        assert exported.path == config.export_path
        assert exported.path.is_file()
        assert exported.rows > 0

    def test_has_expected_sheets(self, exported):
        workbook = load_workbook(exported.path)
        assert workbook.sheetnames == [
            "요약",
            "불법사이트목록",
            "신규_최근24시간",
            "연락채널",
            "홍보사이트_수집원",
            "실행이력",
        ]

    def test_main_sheet_lists_urls_with_scores(self, exported):
        sheet = load_workbook(exported.path)["불법사이트목록"]
        headers = [cell.value for cell in sheet[1]]
        assert headers[:6] == ["번호", "URL", "도메인", "카테고리", "위험도", "점수"]

        rows = {row[1]: row for row in sheet.iter_rows(min_row=2, values_only=True)}
        assert "https://casino-abc777.xyz/" in rows
        casino = rows["https://casino-abc777.xyz/"]
        assert casino[2] == "casino-abc777.xyz"   # 도메인
        assert casino[3] == "도박/베팅"            # 카테고리
        assert isinstance(casino[5], int) and casino[5] > 0  # 점수

    def test_urls_are_plain_text_by_default(self, exported):
        sheet = load_workbook(exported.path)["불법사이트목록"]
        # 실수로 접속하지 않도록 기본값은 하이퍼링크가 아닙니다.
        assert sheet.cell(row=2, column=2).hyperlink is None

    def test_contacts_go_to_their_own_sheet(self, exported):
        workbook = load_workbook(exported.path)
        contacts = [
            row[1] for row in workbook["연락채널"].iter_rows(min_row=2, values_only=True)
        ]
        main = [
            row[1] for row in workbook["불법사이트목록"].iter_rows(min_row=2, values_only=True)
        ]
        assert "https://t.me/promo_admin_contact" in contacts
        assert "https://t.me/promo_admin_contact" not in main

    def test_promo_source_sheet_lists_the_source(self, exported, promo_server):
        sheet = load_workbook(exported.path)["홍보사이트_수집원"]
        rows = [row for row in sheet.iter_rows(min_row=2, values_only=True)]
        assert any(row[1] == promo_server for row in rows)
        assert any(row[3] == "ON" for row in rows)

    def test_new_sheet_contains_todays_finds(self, exported):
        sheet = load_workbook(exported.path)["신규_최근24시간"]
        urls = [row[1] for row in sheet.iter_rows(min_row=2, values_only=True)]
        assert "https://casino-abc777.xyz/" in urls

    def test_export_is_rewritable(self, bot, exported):
        exporter = Exporter(bot["config"], bot["storage"], bot["classifier"])
        again = exporter.export()
        assert again.path == exported.path
        assert again.rows == exported.rows

    def test_marked_status_shows_up_in_excel(self, bot):
        bot["pipeline"].run_cycle()
        bot["storage"].set_site_status("https://casino-abc777.xyz/", "reported", "신고 완료")
        result = Exporter(bot["config"], bot["storage"], bot["classifier"]).export()

        sheet = load_workbook(result.path)["불법사이트목록"]
        row = next(
            row for row in sheet.iter_rows(min_row=2, values_only=True)
            if row[1] == "https://casino-abc777.xyz/"
        )
        assert row[17] == "신고완료"
        assert row[18] == "신고 완료"


class TestRobots:
    def test_disallowed_source_is_skipped(self, bot, monkeypatch):
        """robots.txt 가 막으면 수집하지 않습니다."""
        fetcher = bot["pipeline"].fetcher
        assert fetcher.robots is not None
        monkeypatch.setattr(fetcher.robots, "allowed", lambda url: False)

        stats = bot["pipeline"].run_cycle()
        assert stats.sources_failed == 1
        assert stats.new_sites == 0
