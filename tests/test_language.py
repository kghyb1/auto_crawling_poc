"""언어 필터 테스트 — 한국어 사이트만 DB 에 남는지."""

from __future__ import annotations

import dataclasses

import pytest

from illegal_site_bot.fetcher import Fetcher
from illegal_site_bot.language import (
    FOREIGN,
    KOREAN,
    UNKNOWN,
    LanguageDetector,
    declared_language,
    hangul_ratio,
    visible_text,
)
from illegal_site_bot.storage import Storage

KOREAN_TEXT = "실시간 라이브 카지노 지금 가입하시면 첫충 이벤트를 드립니다 고객센터 문의"
ENGLISH_TEXT = (
    "Welcome to the best online casino. Join now and claim your welcome bonus "
    "from our support team anytime you like."
)


def _with_language(config, **changes):
    return dataclasses.replace(
        config, language_filter=dataclasses.replace(config.language_filter, **changes)
    )


@pytest.fixture
def detector(config):
    """실제 fetcher + storage 를 물린 판별기.

    여기서만 판별용 요청을 켭니다. 이 파일의 테스트는 모두 로컬 픽스처
    주소(127.0.0.x)만 쓰므로 외부 인터넷에 나가지 않습니다.
    """
    storage = Storage(config.database_path)
    fetcher = Fetcher(config.crawl)
    settings = _with_language(config, fetch_when_unknown=True).language_filter
    instance = LanguageDetector(settings, fetcher=fetcher, storage=storage)
    instance.begin_cycle()
    try:
        yield instance
    finally:
        fetcher.close()
        storage.close()


def _make(config, storage, fetcher, **changes) -> LanguageDetector:
    changes.setdefault("fetch_when_unknown", True)
    instance = LanguageDetector(
        _with_language(config, **changes).language_filter, fetcher=fetcher, storage=storage
    )
    instance.begin_cycle()
    return instance


class TestHangulRatio:
    def test_korean_text_scores_high(self):
        assert hangul_ratio(KOREAN_TEXT) > 0.9

    def test_english_text_scores_zero(self):
        assert hangul_ratio(ENGLISH_TEXT) == 0.0

    def test_mixed_text_counts_only_letters(self):
        # 숫자와 기호는 분모에서 빠지므로 한글 비율이 희석되지 않습니다.
        assert hangul_ratio(KOREAN_TEXT + " 010-1234-5678 !!! ###") > 0.9

    def test_short_text_is_not_judged(self):
        # 글자가 너무 적으면 비율이 무의미해 0 으로 둡니다.
        assert hangul_ratio("한글") == 0.0

    def test_japanese_is_not_counted_as_korean(self):
        assert hangul_ratio("オンラインカジノへようこそ今すぐ登録してください本日限定") == 0.0

    def test_chinese_is_not_counted_as_korean(self):
        assert hangul_ratio("欢迎来到在线赌场立即注册领取奖金我们提供真人游戏") == 0.0


class TestPageHelpers:
    def test_visible_text_drops_scripts_and_tags(self):
        html = "<html><script>var x='카지노카지노';</script><p>안녕하세요</p></html>"
        text = visible_text(html)
        assert "안녕하세요" in text
        assert "var x" not in text

    def test_declared_language_reads_html_lang(self):
        assert declared_language('<html lang="ko-KR"><body></body></html>') == "ko-kr"

    def test_declared_language_missing_returns_empty(self):
        assert declared_language("<html><body></body></html>") == ""


class TestVerdictFromPage:
    """이미 받아둔 HTML 로 판별 (추가 요청 없음)."""

    def test_korean_body_is_korean(self, detector):
        verdict = detector.verdict_from_page(f"<html><body>{KOREAN_TEXT}</body></html>")
        assert verdict.language == KOREAN
        assert verdict.hangul_ratio > 0.9

    def test_english_body_is_foreign(self, detector):
        verdict = detector.verdict_from_page(f"<html><body>{ENGLISH_TEXT}</body></html>")
        assert verdict.language == FOREIGN

    def test_euckr_encoding_alone_means_korean(self, detector):
        # 본문이 영어여도 EUC-KR 인코딩이면 한국어 사이트로 봅니다.
        verdict = detector.verdict_from_page(
            f"<html><body>{ENGLISH_TEXT}</body></html>", encoding="cp949"
        )
        assert verdict.language == KOREAN
        assert "인코딩" in verdict.reason

    def test_html_lang_ko_is_enough_without_much_text(self, detector):
        html = f'<html lang="ko"><body>{ENGLISH_TEXT}</body></html>'
        assert detector.verdict_from_page(html).language == KOREAN

    def test_empty_page_is_undetermined_not_foreign(self, detector):
        """자바스크립트 전용 페이지를 외국어로 단정하면 조용히 놓칩니다."""
        verdict = detector.verdict_from_page("<html><body><div id=app></div></body></html>")
        assert verdict.language == UNKNOWN


class TestTldShortcut:
    def test_kr_domain_passes_without_a_request(self, detector):
        verdict = detector.detect("https://some-toto.co.kr/")
        assert verdict.language == KOREAN
        assert verdict.fetched is False
        assert detector.checks_used == 0

    def test_foreign_cctld_is_blocked_without_a_request(self, detector):
        verdict = detector.detect("https://something.jp/")
        assert verdict.language == FOREIGN
        assert detector.checks_used == 0

    def test_generic_tld_needs_a_request(self, detector, partner_server):
        detector.detect(partner_server + "korean")
        assert detector.checks_used == 1


class TestDetectByFetching:
    def test_korean_page_is_kept(self, detector, partner_server):
        assert detector.detect(partner_server + "korean").language == KOREAN

    def test_euckr_page_is_kept(self, detector, partner_server):
        verdict = detector.detect(partner_server + "euckr")
        assert verdict.language == KOREAN
        assert verdict.encoding == "cp949"

    def test_english_page_is_rejected(self, detector, partner_server):
        assert detector.detect(partner_server + "english").language == FOREIGN

    def test_unreachable_page_is_undetermined(self, detector):
        assert detector.detect("http://127.0.0.1:9/").language == UNKNOWN

    def test_javascript_only_page_is_undetermined(self, detector, partner_server):
        assert detector.detect(partner_server + "empty").language == UNKNOWN


class TestCaching:
    def test_same_domain_is_fetched_only_once(self, detector, partner_server):
        detector.detect(partner_server + "korean")
        detector.detect(partner_server + "korean")
        detector.detect(partner_server + "english")   # 같은 도메인
        assert detector.checks_used == 1

    def test_verdict_survives_into_the_next_cycle(self, config, detector, partner_server):
        detector.detect(partner_server + "english")
        assert detector.checks_used == 1

        detector.begin_cycle()   # 사이클이 바뀌어도 DB 캐시가 남아야 합니다
        verdict = detector.detect(partner_server + "english")
        assert verdict.language == FOREIGN
        assert detector.checks_used == 0

    def test_undetermined_is_not_cached(self, detector):
        """일시적 실패를 캐시하면 영영 미판별로 남습니다."""
        detector.detect("http://127.0.0.1:9/")
        assert detector.storage.domain_language("127.0.0.1") is None

    def test_remember_page_fills_the_cache_without_a_request(self, detector):
        verdict = detector.remember_page(
            "https://free-check.xyz/", f"<html><body>{KOREAN_TEXT}</body></html>"
        )
        assert verdict.language == KOREAN
        assert detector.checks_used == 0
        assert detector.storage.domain_language("free-check.xyz")["language"] == KOREAN


class TestBudget:
    def test_budget_limits_requests_per_cycle(self, config, detector, partner_server):
        limited = _make(
            config, detector.storage, detector.fetcher, max_checks_per_cycle=1
        )
        first = limited.detect(partner_server + "english")
        # 다른 도메인이라 캐시가 없지만 예산이 없어 판별하지 않습니다.
        second = limited.detect("https://never-checked.xyz/")
        assert first.language == FOREIGN
        assert second.language == UNKNOWN
        assert "예산" in second.reason

    def test_budget_resets_each_cycle(self, config, detector, partner_server):
        limited = _make(
            config, detector.storage, detector.fetcher, max_checks_per_cycle=1
        )
        limited.detect(partner_server + "english")
        assert limited.checks_used == 1
        limited.begin_cycle()
        assert limited.checks_used == 0

    def test_fetching_can_be_turned_off_entirely(self, config, detector, partner_server):
        offline = _make(
            config, detector.storage, detector.fetcher, fetch_when_unknown=False
        )
        verdict = offline.detect(partner_server + "english")
        assert verdict.language == UNKNOWN
        assert offline.checks_used == 0


class TestShouldStore:
    def test_korean_is_stored(self, detector, partner_server):
        allowed, _ = detector.allows(partner_server + "korean")
        assert allowed is True

    def test_foreign_is_not_stored(self, detector, partner_server):
        allowed, verdict = detector.allows(partner_server + "english")
        assert allowed is False
        assert verdict.language == FOREIGN

    def test_undetermined_is_kept_by_default(self, detector):
        allowed, verdict = detector.allows("http://127.0.0.1:9/")
        assert verdict.language == UNKNOWN
        assert allowed is True, "판별 못한 것을 버리면 조용히 놓칩니다"

    def test_undetermined_can_be_dropped_by_config(self, config, detector):
        strict = _make(
            config, detector.storage, detector.fetcher, keep_when_undetermined=False
        )
        allowed, _ = strict.allows("http://127.0.0.1:9/")
        assert allowed is False

    def test_disabled_filter_allows_everything_without_checking(
        self, config, detector, partner_server
    ):
        off = _make(config, detector.storage, detector.fetcher, enabled=False)
        allowed, _ = off.allows(partner_server + "english")
        assert allowed is True
        assert off.checks_used == 0
