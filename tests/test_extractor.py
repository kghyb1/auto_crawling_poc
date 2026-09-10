from illegal_site_bot.extractor import extract, extract_meta_redirect

from conftest import PROMO_INDEX

SOURCE = "https://promo.test/"


def _extract():
    return extract(PROMO_INDEX, SOURCE, pagination_patterns=("page=", "board"))


def _by_url(result):
    return {candidate.url: candidate for candidate in result.candidates}


def test_reads_page_title():
    assert _extract().title == "먹튀검증 커뮤니티 - 보증업체 모음"


def test_finds_plain_anchor_link():
    assert "http://toto-safe-999.top/join" in _by_url(_extract())


def test_banner_image_link_uses_alt_text():
    candidate = _by_url(_extract())["https://casino-abc777.xyz/"]
    assert candidate.banner is True
    assert "라이브카지노" in candidate.anchor_text


def test_banner_filename_is_included_as_hint():
    candidate = _by_url(_extract())["https://casino-abc777.xyz/"]
    assert "live casino abc777" in candidate.anchor_text.lower()


def test_finds_data_href_attribute():
    candidate = _by_url(_extract())["https://slot-yamato-55.vip/"]
    assert candidate.method == "data_attr"
    assert "야마토" in candidate.anchor_text


def test_finds_onclick_location_href():
    candidate = _by_url(_extract())["https://holdem-king.cc/lobby"]
    assert candidate.method == "js_onclick"


def test_finds_url_inside_script_string():
    assert "https://webtoon24-free.site/" in _by_url(_extract())


def test_finds_obfuscated_domain_written_in_body_text():
    candidate = _by_url(_extract())["https://freeya-dong.com/"]
    assert candidate.method == "text"
    assert "무료야동" in candidate.anchor_text


def test_finds_iframe_source():
    assert "https://plain-ad-frame.example/frame" in _by_url(_extract())


def test_internal_pagination_link_is_not_a_candidate():
    result = _extract()
    urls = _by_url(result)
    assert "https://promo.test/board/list?page=2" not in urls
    assert "https://promo.test/board/list?page=2" in result.internal_links


def test_same_site_asset_is_not_a_candidate():
    assert "https://promo.test/assets/logo.png" not in _by_url(_extract())


class TestContext:
    def test_uses_nearby_description_as_context(self):
        html = """
        <div class="item">
          <h3>에볼루션 라이브카지노 추천</h3>
          <a href="https://target-a.xyz/">바로가기</a>
        </div>
        """
        candidate = _by_url(extract(html, SOURCE))["https://target-a.xyz/"]
        assert "라이브카지노" in candidate.context_text

    def test_ignores_context_of_a_multi_link_banner_list(self):
        """배너를 여럿 늘어놓은 목록에서는 옆 배너 키워드가 섞이지 않아야 합니다."""
        html = """
        <div class="banners">
          <a href="https://target-a.xyz/">A업체</a>
          <a href="https://target-b.xyz/">바카라 B업체</a>
          <a href="https://target-c.xyz/">슬롯 C업체</a>
        </div>
        """
        candidate = _by_url(extract(html, SOURCE))["https://target-a.xyz/"]
        assert candidate.anchor_text == "A업체"
        assert candidate.context_text == ""

    def test_body_text_is_never_used_as_context(self):
        html = "<body><p>카지노 바카라 슬롯 안내</p><a href='https://target-a.xyz/'>링크</a></body>"
        candidate = _by_url(extract(html, SOURCE))["https://target-a.xyz/"]
        assert candidate.context_text == ""


class TestMetaRedirect:
    def test_reads_meta_refresh_target(self):
        html = '<meta http-equiv="refresh" content="0;url=https://final-site.xyz/enter">'
        assert extract_meta_redirect(html, "https://gate.test/go") == "https://final-site.xyz/enter"

    def test_reads_javascript_location_target(self):
        html = "<script>location.replace('https://final-site.xyz/');</script>"
        assert extract_meta_redirect(html, "https://gate.test/go") == "https://final-site.xyz/"

    def test_ignores_same_site_redirect(self):
        html = '<meta http-equiv="refresh" content="0;url=/other">'
        assert extract_meta_redirect(html, "https://gate.test/go") is None
