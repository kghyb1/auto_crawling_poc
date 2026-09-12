from illegal_site_bot import normalizer as n


class TestNormalizeUrl:
    def test_lowercases_host_and_drops_default_port_and_fragment(self):
        assert (
            n.normalize_url("HTTP://Example.COM:80/Path?a=1#frag")
            == "http://example.com/Path?a=1"
        )

    def test_drops_tracking_params_but_keeps_others(self):
        assert (
            n.normalize_url("https://a.com/x?utm_source=promo&id=7&fbclid=zz")
            == "https://a.com/x?id=7"
        )

    def test_resolves_relative_against_base(self):
        assert (
            n.normalize_url("/list?page=2", base="https://promo.test/board/")
            == "https://promo.test/list?page=2"
        )

    def test_bare_domain_gets_default_scheme_and_root_path(self):
        assert n.normalize_url("abc-777.xyz") == "https://abc-777.xyz/"

    def test_protocol_relative_uses_base_scheme(self):
        assert n.normalize_url("//cdn.a.com/x.js", base="http://b.com/") == "http://cdn.a.com/x.js"

    def test_rejects_non_http_schemes(self):
        for raw in ("javascript:void(0)", "mailto:a@b.com", "tel:010", "data:text/html,x"):
            assert n.normalize_url(raw) is None

    def test_rejects_hostless_and_malformed(self):
        assert n.normalize_url("") is None
        assert n.normalize_url("localhost") is None  # 점이 없으면 도메인으로 보지 않습니다
        assert n.normalize_url("https://a b.com/") is None

    def test_keeps_non_default_port(self):
        assert n.normalize_url("http://a.com:8080/x") == "http://a.com:8080/x"

    def test_idn_host_becomes_punycode(self):
        normalized = n.normalize_url("https://한글도메인.kr/")
        assert normalized is not None
        host = n.host_of(normalized)
        assert host.startswith("xn--") and host.endswith(".kr")
        # punycode 를 되돌리면 원래 한글 도메인이어야 합니다.
        assert host.encode("ascii").decode("idna") == "한글도메인.kr"


class TestRegistrableDomain:
    def test_two_label_domain(self):
        assert n.registrable_domain("example.com") == "example.com"

    def test_strips_subdomains(self):
        assert n.registrable_domain("www.a.b.example.com") == "example.com"

    def test_handles_multi_label_suffix(self):
        assert n.registrable_domain("shop.example.co.kr") == "example.co.kr"
        assert n.registrable_domain("a.b.example.co.uk") == "example.co.uk"

    def test_accepts_full_url(self):
        assert n.registrable_domain("https://m.example.or.kr/path") == "example.or.kr"

    def test_ip_host_returned_as_is(self):
        assert n.registrable_domain("http://127.0.0.1:8000/x") == "127.0.0.1"

    def test_same_site_compares_registrable_domain(self):
        assert n.same_site("https://a.promo.com/x", "https://b.promo.com/y")
        assert not n.same_site("https://promo.com/x", "https://other.com/y")


class TestDeobfuscate:
    def test_bracketed_dot(self):
        assert "abc.com" in n.deobfuscate_text("접속 abc[.]com 하세요")

    def test_korean_dot_word(self):
        assert "xyz.net" in n.deobfuscate_text("xyz (닷) net")

    def test_hxxp_scheme(self):
        assert "https://q.com" in n.deobfuscate_text("hxxps://q.com")

    def test_fullwidth_characters(self):
        assert "abc.com" in n.deobfuscate_text("ａｂｃ．ｃｏｍ")


class TestIterUrlsInText:
    def test_finds_obfuscated_and_plain_urls(self):
        found = n.iter_urls_in_text("먹튀없는 abc[.]com 과 https://ok-777.vip/join 확인")
        assert found == ["https://abc.com/", "https://ok-777.vip/join"]

    def test_ignores_filenames_and_emails(self):
        found = n.iter_urls_in_text("index.php style.css sample.jpg admin@naver.com")
        assert found == []

    def test_strips_trailing_punctuation_from_path(self):
        assert n.iter_urls_in_text("주소는 a-1.com/enter 입니다.") == ["https://a-1.com/enter"]

    def test_keeps_port(self):
        assert n.iter_urls_in_text("win-88.top:8080/enter") == ["https://win-88.top:8080/enter"]


def test_tld_of():
    assert n.tld_of("https://a.b.example.xyz/p") == "xyz"
    assert n.tld_of("https://example.co.kr/") == "kr"


class TestDeobfuscationDoesNotInventDomains:
    """평범한 문장이 없는 도메인으로 둔갑하면 수집 결과가 오염됩니다."""

    def test_english_sentence_boundaries_are_not_domains(self):
        for sentence in (
            "Sale ends soon. Shop now for details",
            "We tested it. Info here",
            "Hello world. Online casino site",
            "Check this. Top rated",
        ):
            assert n.iter_urls_in_text(sentence) == [], sentence

    def test_real_obfuscation_still_works(self):
        assert n.iter_urls_in_text("접속 abc[.]com") == ["https://abc.com/"]
        assert n.iter_urls_in_text("xyz (닷) net") == ["https://xyz.net/"]
        assert n.iter_urls_in_text("qq · com") == ["https://qq.com/"]
        # 마침표 *앞에* 공백이 있는 형태는 문장에서 나오지 않으므로 계속 처리합니다.
        assert n.iter_urls_in_text("abc . com 으로") == ["https://abc.com/"]
