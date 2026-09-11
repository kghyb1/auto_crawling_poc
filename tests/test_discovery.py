"""홍보사이트 자동 발견 테스트 (1단계 후보 등록 / 2단계 평가)."""

from __future__ import annotations

import dataclasses

import pytest

from illegal_site_bot.discovery import Discovery, root_url
from illegal_site_bot.extractor import Candidate
from illegal_site_bot.fetcher import Fetcher
from illegal_site_bot.storage import SourceRow, Storage


def _source(source_id: int = 1, url: str = "https://seed.test/", depth: int = 0) -> SourceRow:
    return SourceRow(
        id=source_id,
        url=url,
        name="시드 홍보사이트",
        enabled=True,
        render=False,
        max_pages=None,
        note="",
        depth=depth,
    )


def _candidate(url: str, anchor: str = "", context: str = "") -> Candidate:
    return Candidate(url=url, anchor_text=anchor, context_text=context)


def _with_discovery(config, **changes):
    """frozen 설정에서 discovery 값만 바꾼 사본을 만듭니다."""
    return dataclasses.replace(
        config, discovery=dataclasses.replace(config.discovery, **changes)
    )


@pytest.fixture
def discovery(config, classifier):
    storage = Storage(config.database_path)
    fetcher = Fetcher(config.crawl)
    instance = Discovery(config, storage, classifier, fetcher)
    instance.begin_cycle()
    try:
        yield instance
    finally:
        fetcher.close()
        storage.close()


def _make(config, classifier, storage, fetcher, **changes) -> Discovery:
    instance = Discovery(_with_discovery(config, **changes), storage, classifier, fetcher)
    instance.begin_cycle()
    return instance


def test_root_url_keeps_only_the_home_page():
    assert root_url("https://promo.com/board/read?id=3") == "https://promo.com/"
    assert root_url("http://promo.com:8080/x") == "http://promo.com:8080/"


class TestConsider:
    """1단계 - 요청 없이 링크 텍스트만 보고 후보로 등록할지 결정."""

    def test_registers_when_link_text_has_promo_vocabulary(self, discovery):
        added = discovery.consider(
            _candidate("https://other-promo.com/", anchor="제휴 먹튀검증 커뮤니티"),
            _source(),
        )
        assert added is True
        rows = discovery.storage.sources_by_state("discovered")
        assert [row["url"] for row in rows] == ["https://other-promo.com/"]
        assert rows[0]["origin"] == "auto"
        assert rows[0]["depth"] == 1

    def test_ignores_links_without_promo_vocabulary(self, discovery):
        assert discovery.consider(
            _candidate("https://casino-abc.xyz/", anchor="에볼루션 라이브카지노"), _source()
        ) is False
        assert discovery.storage.sources_by_state("discovered") == []

    def test_matches_vocabulary_in_surrounding_context(self, discovery):
        assert discovery.consider(
            _candidate("https://p.com/", anchor="바로가기", context="보증업체 순위 안내"),
            _source(),
        ) is True

    def test_registers_home_page_not_the_deep_link(self, discovery):
        discovery.consider(
            _candidate("https://p.com/board/read?id=9", anchor="먹튀검증"), _source()
        )
        rows = discovery.storage.sources_by_state("discovered")
        assert [row["url"] for row in rows] == ["https://p.com/"]

    def test_records_where_it_was_found(self, discovery):
        seed_id = discovery.storage.upsert_source("https://seed.test/", name="시드")
        discovery.begin_cycle()
        discovery.consider(
            _candidate("https://p.com/", anchor="먹튀검증"), _source(source_id=seed_id)
        )
        row = discovery.storage.sources_by_state("discovered")[0]
        assert row["discovered_from_id"] == seed_id
        assert "먹튀검증" in row["note"]

    def test_excluded_domains_are_not_candidates(self, discovery):
        assert discovery.consider(
            _candidate("https://www.google.com/search", anchor="먹튀검증 커뮤니티"), _source()
        ) is False

    def test_contact_channels_are_not_candidates(self, discovery):
        assert discovery.consider(
            _candidate("https://t.me/somebody", anchor="먹튀검증 문의"), _source()
        ) is False

    def test_already_registered_domain_is_skipped(self, discovery):
        discovery.storage.upsert_source("https://p.com/")
        discovery.begin_cycle()
        assert discovery.consider(
            _candidate("https://p.com/other", anchor="먹튀검증"), _source()
        ) is False

    def test_same_candidate_twice_registers_once(self, discovery):
        first = discovery.consider(_candidate("https://p.com/", anchor="먹튀검증"), _source())
        second = discovery.consider(_candidate("https://p.com/", anchor="먹튀검증"), _source())
        assert (first, second) == (True, False)
        assert len(discovery.storage.sources_by_state("discovered")) == 1

    def test_disabled_discovery_registers_nothing(self, config, classifier, discovery):
        off = _make(
            config, classifier, discovery.storage, discovery.fetcher, enabled=False
        )
        assert off.consider(_candidate("https://p.com/", anchor="먹튀검증"), _source()) is False


class TestDepthLimit:
    def test_depth_one_is_allowed_from_a_seed(self, discovery):
        assert discovery.consider(
            _candidate("https://d1.com/", anchor="먹튀검증"), _source(depth=0)
        ) is True
        assert discovery.storage.sources_by_state("discovered")[0]["depth"] == 1

    def test_depth_two_is_allowed(self, discovery):
        assert discovery.consider(
            _candidate("https://d2.com/", anchor="먹튀검증"), _source(depth=1)
        ) is True
        assert discovery.storage.sources_by_state("discovered")[0]["depth"] == 2

    def test_depth_three_is_refused_with_default_max_depth(self, discovery):
        assert discovery.consider(
            _candidate("https://d3.com/", anchor="먹튀검증"), _source(depth=2)
        ) is False
        assert discovery.storage.sources_by_state("discovered") == []

    def test_max_depth_is_configurable(self, config, classifier, discovery):
        shallow = _make(
            config, classifier, discovery.storage, discovery.fetcher, max_depth=1
        )
        assert shallow.consider(
            _candidate("https://d2.com/", anchor="먹튀검증"), _source(depth=1)
        ) is False

    def test_shorter_path_lowers_recorded_depth(self, discovery):
        discovery.consider(_candidate("https://p.com/", anchor="먹튀검증"), _source(depth=1))
        assert discovery.storage.sources_by_state("discovered")[0]["depth"] == 2
        # 같은 곳을 더 짧은 경로로 다시 발견하면 깊이를 낮춥니다.
        discovery.storage.add_candidate(
            "https://p.com/", discovered_from_id=None, depth=1
        )
        assert discovery.storage.sources_by_state("discovered")[0]["depth"] == 1


class TestCaps:
    def test_per_cycle_candidate_cap(self, config, classifier, discovery):
        capped = _make(
            config, classifier, discovery.storage, discovery.fetcher,
            max_candidates_per_cycle=2,
        )
        results = [
            capped.consider(_candidate(f"https://p{i}.com/", anchor="먹튀검증"), _source())
            for i in range(5)
        ]
        assert results == [True, True, False, False, False]

    def test_cap_resets_next_cycle(self, config, classifier, discovery):
        capped = _make(
            config, classifier, discovery.storage, discovery.fetcher,
            max_candidates_per_cycle=1,
        )
        assert capped.consider(_candidate("https://a.com/", anchor="먹튀검증"), _source())
        assert not capped.consider(_candidate("https://b.com/", anchor="먹튀검증"), _source())
        capped.begin_cycle()
        assert capped.consider(_candidate("https://b.com/", anchor="먹튀검증"), _source())

    def test_total_source_cap_stops_discovery(self, config, classifier, discovery):
        discovery.storage.upsert_source("https://seed1.test/")
        discovery.storage.upsert_source("https://seed2.test/")
        limited = _make(
            config, classifier, discovery.storage, discovery.fetcher, max_total_sources=2
        )
        assert limited.consider(
            _candidate("https://new.com/", anchor="먹튀검증"), _source()
        ) is False


class TestEvaluateUrl:
    """2단계 - 페이지를 실제로 받아 점수를 냄."""

    @pytest.fixture
    def seeded(self, discovery):
        """이미 알고 있는 불법 도메인 5개를 DB 에 심어 둡니다 (A 신호)."""
        for url in (
            "https://casino-abc777.xyz/",
            "http://toto-safe-999.top/",
            "https://slot-yamato-55.vip/",
            "https://holdem-king.cc/",
            "https://baccarat-vip-365.club/",
        ):
            discovery.storage.record_site(
                url, category="gambling", category_label="도박/베팅", base_score=80
            )
        return discovery

    def test_promo_page_scores_high(self, seeded, partner_server):
        evaluation = seeded.evaluate_url(partner_server)
        assert evaluation.ok
        assert evaluation.known_illegal == 5
        assert evaluation.score >= seeded.settings.auto_approve_score
        assert "먹튀검증" in evaluation.title

    def test_known_illegal_overlap_is_the_dominant_signal(self, discovery, partner_server):
        """아는 불법 도메인이 없으면(DB 가 비어 있으면) 점수가 확 낮아집니다."""
        cold = discovery.evaluate_url(partner_server)
        assert cold.known_illegal == 0

        for url in ("https://casino-abc777.xyz/", "http://toto-safe-999.top/",
                    "https://slot-yamato-55.vip/"):
            discovery.storage.record_site(
                url, category="gambling", category_label="도박/베팅", base_score=80
            )
        warm = discovery.evaluate_url(partner_server)
        assert warm.known_illegal == 3
        assert warm.score > cold.score

    def test_counts_outbound_domains(self, seeded, partner_server):
        evaluation = seeded.evaluate_url(partner_server)
        assert evaluation.outbound_domains == 6

    def test_title_vocabulary_is_reported(self, seeded, partner_server):
        evaluation = seeded.evaluate_url(partner_server)
        assert any("제목/헤딩 어휘" in reason for reason in evaluation.reasons)

    def test_login_landing_page_is_penalised(self, seeded, partner_server):
        evaluation = seeded.evaluate_url(partner_server + "landing")
        assert evaluation.landing_page is True
        assert evaluation.score < seeded.settings.queue_score

    def test_unreachable_page_reports_error(self, discovery):
        evaluation = discovery.evaluate_url("http://127.0.0.1:9/")
        assert not evaluation.ok
        assert evaluation.error


class TestEvaluatePending:
    @pytest.fixture
    def queued(self, discovery, partner_server):
        for url in ("https://casino-abc777.xyz/", "http://toto-safe-999.top/",
                    "https://slot-yamato-55.vip/", "https://holdem-king.cc/",
                    "https://baccarat-vip-365.club/"):
            discovery.storage.record_site(
                url, category="gambling", category_label="도박/베팅", base_score=80
            )
        discovery.storage.add_candidate(partner_server, discovered_from_id=None, depth=1)
        return discovery

    def test_queues_for_human_approval_by_default(self, queued, partner_server):
        stats = queued.evaluate_pending()
        assert stats.evaluated == 1
        assert stats.queued == 1
        assert stats.approved == 0

        row = queued.storage.source_by_url(partner_server)
        assert row["state"] == "pending"
        assert row["promo_score"] >= queued.settings.queue_score
        assert row["promo_reasons"]
        assert row["evaluated_at"]

    def test_pending_source_is_not_crawled_yet(self, queued, partner_server):
        queued.evaluate_pending()
        crawled = [source.url for source in queued.storage.list_sources(enabled_only=True)]
        assert partner_server not in crawled

    def test_auto_approve_makes_it_crawlable(self, config, classifier, queued, partner_server):
        auto = _make(
            config, classifier, queued.storage, queued.fetcher, auto_approve=True
        )
        stats = auto.evaluate_pending()
        assert stats.approved == 1

        row = auto.storage.source_by_url(partner_server)
        assert row["state"] == "approved"
        assert row["enabled"] == 1
        assert partner_server in [s.url for s in auto.storage.list_sources(enabled_only=True)]

    def test_auto_approve_respects_per_cycle_cap(self, config, classifier, queued):
        queued.storage.add_candidate(
            "https://another.test/", discovered_from_id=None, depth=1
        )
        auto = _make(
            config, classifier, queued.storage, queued.fetcher,
            auto_approve=True, max_new_per_cycle=0,
        )
        stats = auto.evaluate_pending()
        assert stats.approved == 0

    def test_low_score_page_is_rejected(self, queued, partner_server):
        queued.storage.add_candidate(
            partner_server + "landing", discovered_from_id=None, depth=1
        )
        queued.evaluate_pending()
        row = queued.storage.source_by_url(partner_server + "landing")
        assert row["state"] == "rejected"

    def test_evaluation_budget_is_respected(self, config, classifier, queued):
        for index in range(5):
            queued.storage.add_candidate(
                f"https://extra{index}.test/", discovered_from_id=None, depth=1
            )
        budgeted = _make(
            config, classifier, queued.storage, queued.fetcher,
            max_evaluations_per_cycle=2,
        )
        stats = budgeted.evaluate_pending()
        assert stats.evaluated + stats.failed == 2

    def test_unreachable_candidate_is_rejected_after_repeated_failures(
        self, config, classifier, discovery
    ):
        discovery.storage.add_candidate(
            "http://127.0.0.1:9/", discovered_from_id=None, depth=1
        )
        instance = _make(
            config, classifier, discovery.storage, discovery.fetcher,
            max_evaluation_failures=2,
        )
        instance.evaluate_pending()
        assert instance.storage.source_by_url("http://127.0.0.1:9/")["state"] == "discovered"
        instance.evaluate_pending()
        assert instance.storage.source_by_url("http://127.0.0.1:9/")["state"] == "rejected"

    def test_approved_promo_site_is_removed_from_illegal_list(
        self, config, classifier, queued, partner_server
    ):
        """홍보사이트는 도박 키워드 때문에 불법사이트로도 잡히는데, 확정되면 정리합니다."""
        queued.storage.record_site(
            partner_server, category="gambling", category_label="도박/베팅", base_score=60
        )
        auto = _make(config, classifier, queued.storage, queued.fetcher, auto_approve=True)
        auto.evaluate_pending()

        site = next(
            row for row in auto.storage.sites_for_export(0) if row["url"] == partner_server
        )
        assert site["status"] == "ignored"
        assert "홍보사이트로 재분류" in site["memo"]

    def test_human_approval_also_reclassifies(self, queued, partner_server):
        queued.storage.record_site(
            partner_server, category="gambling", category_label="도박/베팅", base_score=60
        )
        queued.evaluate_pending()
        assert queued.approve(partner_server, by="테스트") is True

        row = queued.storage.source_by_url(partner_server)
        assert row["state"] == "approved" and row["enabled"] == 1
        site = next(
            r for r in queued.storage.sites_for_export(0) if r["url"] == partner_server
        )
        assert site["status"] == "ignored"

    def test_reject_keeps_it_out_of_the_crawl(self, queued, partner_server):
        queued.evaluate_pending()
        assert queued.reject(partner_server, by="테스트") is True
        row = queued.storage.source_by_url(partner_server)
        assert row["state"] == "rejected" and row["enabled"] == 0

    def test_review_unknown_url_returns_false(self, discovery):
        assert discovery.approve("https://nope.test/") is False


class TestMirrorDetection:
    def test_site_with_the_same_link_set_is_flagged_as_mirror(
        self, config, classifier, discovery, partner_server, mirror_server
    ):
        for url in ("https://casino-abc777.xyz/", "http://toto-safe-999.top/",
                    "https://slot-yamato-55.vip/"):
            discovery.storage.record_site(
                url, category="gambling", category_label="도박/베팅", base_score=80
            )
        # 첫 번째 후보를 평가해 링크 도메인 집합을 남깁니다.
        discovery.storage.add_candidate(partner_server, discovered_from_id=None, depth=1)
        discovery.evaluate_pending()
        assert discovery.storage.source_by_url(partner_server)["link_domains"]

        # 같은 내용을 다른 주소로 운영하는 복제 사이트는 미러로 걸러져야 합니다.
        mirror_url = mirror_server
        discovery.storage.add_candidate(mirror_url, discovered_from_id=None, depth=1)
        stats = discovery.evaluate_pending()
        assert stats.mirrors == 1

        row = discovery.storage.source_by_url(mirror_url)
        assert row["state"] == "rejected"
        assert "미러" in row["promo_reasons"]
        assert row["mirror_of_id"]


class TestDeadSourceCleanup:
    def test_repeated_failures_disable_the_source(self, config, classifier, discovery):
        source_id = discovery.storage.upsert_source("https://dead.test/")
        for _ in range(3):
            discovery.storage.record_source_result(
                source_id, ok=False, status="실패", error="접속 불가"
            )
        instance = _make(
            config, classifier, discovery.storage, discovery.fetcher,
            auto_disable_after_failures=3,
        )
        assert instance.cleanup_dead_sources() == ["https://dead.test/"]

        row = instance.storage.source_by_url("https://dead.test/")
        assert row["state"] == "auto_disabled" and row["enabled"] == 0
        assert "https://dead.test/" not in [
            s.url for s in instance.storage.list_sources(enabled_only=True)
        ]

    def test_healthy_source_is_left_alone(self, discovery):
        discovery.storage.upsert_source("https://alive.test/")
        assert discovery.cleanup_dead_sources() == []
