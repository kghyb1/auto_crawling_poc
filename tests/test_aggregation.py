"""엑셀 출력용 사이트 묶기 테스트."""

from __future__ import annotations

import pytest

from illegal_site_bot.aggregation import GROUP_MODES, group_key_of, group_sites
from illegal_site_bot.storage import Storage


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db" / "test.sqlite3")
    yield store
    store.close()


def _site(storage, url, *, score=60, category="gambling", keywords="카지노"):
    site_id, _ = storage.record_site(
        url,
        category=category,
        category_label="도박/베팅",
        base_score=score,
        matched_keywords=keywords,
        reasons=f"키워드 일치({keywords})",
    )
    return site_id


def _observe(storage, site_id, *source_urls):
    for source_url in source_urls:
        storage.record_observation(
            site_id, None, source_url, source_url, "", "a_href", False, 50
        )


def _groups(storage, mode="host", **kwargs):
    return group_sites(
        storage.sites_for_export(0), storage.source_urls_grouped(), mode=mode, **kwargs
    )


class TestGroupKey:
    def test_url_mode_keeps_the_whole_address(self):
        assert group_key_of("https://a.abc.com/join", "url") == "https://a.abc.com/join"

    def test_host_mode_keeps_subdomains_apart(self):
        assert group_key_of("https://a.abc.com/join", "host") == "a.abc.com"
        assert group_key_of("https://b.abc.com/", "host") == "b.abc.com"

    def test_domain_mode_merges_subdomains(self):
        assert group_key_of("https://a.abc.com/join", "domain") == "abc.com"
        assert group_key_of("https://b.abc.com/", "domain") == "abc.com"

    def test_all_modes_are_supported(self):
        for mode in GROUP_MODES:
            assert group_key_of("https://abc.com/x", mode)


class TestHostGrouping:
    def test_paths_of_the_same_host_collapse_into_one_row(self, storage):
        _site(storage, "http://toto-999.top/")
        _site(storage, "http://toto-999.top/join")
        groups = _groups(storage)
        assert len(groups) == 1
        assert groups[0].key == "toto-999.top"
        assert groups[0].url_count == 2

    def test_different_subdomains_stay_separate(self, storage):
        _site(storage, "https://a.abc.com/")
        _site(storage, "https://b.abc.com/")
        assert {group.key for group in _groups(storage)} == {"a.abc.com", "b.abc.com"}

    def test_domain_mode_merges_those_subdomains(self, storage):
        _site(storage, "https://a.abc.com/")
        _site(storage, "https://b.abc.com/")
        groups = _groups(storage, mode="domain")
        assert len(groups) == 1
        assert groups[0].key == "abc.com"
        assert groups[0].hosts == ["a.abc.com", "b.abc.com"]

    def test_url_mode_does_not_group_at_all(self, storage):
        _site(storage, "http://toto-999.top/")
        _site(storage, "http://toto-999.top/join")
        assert len(_groups(storage, mode="url")) == 2

    def test_representative_url_is_the_shortest_path(self, storage):
        _site(storage, "http://toto-999.top/board/read?id=9")
        _site(storage, "http://toto-999.top/")
        assert _groups(storage)[0].representative_url == "http://toto-999.top/"

    def test_all_urls_are_listed(self, storage):
        _site(storage, "http://toto-999.top/join")
        _site(storage, "http://toto-999.top/")
        assert _groups(storage)[0].urls == [
            "http://toto-999.top/",
            "http://toto-999.top/join",
        ]


class TestGroupScoring:
    """묶어서 세야 '여러 홍보사이트에서 발견' 가산이 제대로 붙습니다."""

    def test_promo_sites_are_counted_across_the_whole_group(self, storage):
        first = _site(storage, "http://toto-999.top/join", score=50)
        second = _site(storage, "http://toto-999.top/", score=50)
        _observe(storage, first, "https://promo1.test/")
        _observe(storage, second, "https://promo2.test/")

        group = _groups(storage)[0]
        assert group.distinct_sources == 2
        assert group.source_urls == ["https://promo1.test/", "https://promo2.test/"]

    def test_grouping_recovers_the_lost_bonus(self, storage):
        """URL 단위로는 각각 1곳이라 가산이 없지만, 묶으면 2곳이라 붙습니다."""
        first = _site(storage, "http://toto-999.top/join", score=50)
        second = _site(storage, "http://toto-999.top/", score=50)
        _observe(storage, first, "https://promo1.test/")
        _observe(storage, second, "https://promo2.test/")

        per_url = _groups(storage, mode="url", bonus_per_source=6, bonus_max=24)
        assert [group.score for group in per_url] == [50, 50]

        grouped = _groups(storage, mode="host", bonus_per_source=6, bonus_max=24)
        assert grouped[0].score == 56

    def test_bonus_is_capped(self, storage):
        site_id = _site(storage, "http://toto-999.top/", score=50)
        _observe(storage, site_id, *[f"https://promo{i}.test/" for i in range(10)])
        assert _groups(storage, bonus_per_source=6, bonus_max=24)[0].score == 74

    def test_same_promo_site_counted_once(self, storage):
        first = _site(storage, "http://toto-999.top/a", score=50)
        second = _site(storage, "http://toto-999.top/b", score=50)
        _observe(storage, first, "https://promo1.test/")
        _observe(storage, second, "https://promo1.test/")
        assert _groups(storage, bonus_per_source=6, bonus_max=24)[0].distinct_sources == 1

    def test_group_takes_the_highest_scoring_members_category(self, storage):
        _site(storage, "https://x.com/a", score=30, category="gambling")
        storage.record_site(
            "https://x.com/b",
            category="adult",
            category_label="성인/음란",
            base_score=80,
            matched_keywords="야동",
            reasons="키워드 일치",
        )
        group = _groups(storage)[0]
        assert group.category == "adult"
        assert group.base_score == 80

    def test_keywords_from_all_members_are_merged(self, storage):
        _site(storage, "https://x.com/a", keywords="카지노, 바카라")
        _site(storage, "https://x.com/b", keywords="바카라, 슬롯")
        keywords = _groups(storage)[0].matched_keywords
        assert keywords == "카지노, 바카라, 슬롯"

    def test_seen_counts_add_up(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/a")   # 같은 URL 두 번 → seen_count 2
        _site(storage, "https://x.com/b")
        assert _groups(storage)[0].seen_count == 3

    def test_groups_are_sorted_by_score(self, storage):
        _site(storage, "https://low.com/", score=30)
        _site(storage, "https://high.com/", score=90)
        assert [group.key for group in _groups(storage)] == ["high.com", "low.com"]


class TestGroupTimestampsAndStatus:
    def test_first_and_last_seen_span_the_group(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/b")
        group = _groups(storage)[0]
        assert group.first_seen_at <= group.last_seen_at

    def test_group_is_alive_if_any_url_is_alive(self, storage):
        dead = _site(storage, "https://x.com/a")
        alive = _site(storage, "https://x.com/b")
        storage.update_alive(dead, False, 404)
        storage.update_alive(alive, True, 200)

        group = _groups(storage)[0]
        assert group.alive == 1
        assert group.http_status == 200

    def test_group_is_dead_only_when_every_url_is_dead(self, storage):
        first = _site(storage, "https://x.com/a")
        second = _site(storage, "https://x.com/b")
        storage.update_alive(first, False, 404)
        storage.update_alive(second, False, 500)
        assert _groups(storage)[0].alive == 0

    def test_unchecked_group_reports_unknown(self, storage):
        _site(storage, "https://x.com/a")
        assert _groups(storage)[0].alive is None

    def test_most_advanced_status_wins(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/b")
        storage.set_site_status("https://x.com/b", "reported", "신고함")
        group = _groups(storage)[0]
        assert group.status == "reported"
        assert group.memo == "신고함"

    def test_memos_from_several_urls_are_joined(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/b")
        storage.set_site_status("https://x.com/a", "confirmed", "메모1")
        storage.set_site_status("https://x.com/b", "confirmed", "메모2")
        assert _groups(storage)[0].memo == "메모1 / 메모2"


def test_invalid_mode_is_rejected(storage):
    with pytest.raises(ValueError):
        group_sites([], {}, mode="없는모드")


class TestStatusPrecedence:
    """ignored 는 '진행'이 아니라 '제외' 결정이라 신고 상태를 덮으면 안 됩니다."""

    def test_ignored_does_not_override_reported(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/b")
        storage.set_site_status("https://x.com/a", "reported", "신고함")
        storage.set_site_status("https://x.com/b", "ignored", "오탐")
        assert _groups(storage)[0].status == "reported"

    def test_new_wins_over_ignored(self, storage):
        _site(storage, "https://x.com/a")
        _site(storage, "https://x.com/b")
        storage.set_site_status("https://x.com/b", "ignored")
        assert _groups(storage)[0].status == "new"
