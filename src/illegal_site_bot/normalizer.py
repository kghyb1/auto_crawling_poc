"""URL 정규화 / 도메인 추출 / 난독화 해제.

홍보사이트는 도메인을 그대로 쓰지 않고 ``abc[.]com``, ``abc(닷)com``,
``hxxps://abc.com`` 처럼 흘려 쓰는 경우가 많습니다. 여기서 그런 표기를 정상
URL 로 되돌리고, 중복 판정을 위해 형태를 통일합니다.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_SCHEME = "https"

#: 광고/유입 추적용 쿼리 파라미터. 같은 페이지가 다른 URL 로 중복 저장되는 것을 막습니다.
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "spm",
    "_ga",
    "_gl",
}

#: 여러 단계로 된 공개 접미사(등록가능도메인 계산용). 자주 쓰이는 것만 담았습니다.
MULTI_LABEL_SUFFIXES = {
    # 한국
    "co.kr", "ne.kr", "or.kr", "re.kr", "pe.kr", "go.kr", "mil.kr", "ac.kr",
    "hs.kr", "ms.kr", "es.kr", "sc.kr", "kg.kr", "seoul.kr", "busan.kr",
    # 일본 / 중국 / 대만 / 홍콩
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ed.jp", "gr.jp", "lg.jp",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.tw", "net.tw", "org.tw", "com.hk", "net.hk", "org.hk", "idv.hk",
    # 영미권 / 유럽
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "net.nz",
    "org.nz", "co.za", "com.br", "net.br", "org.br", "com.mx", "com.ar",
    "com.co", "com.pe", "com.tr", "com.ua", "com.ru", "com.pl", "com.es",
    "com.pt", "com.gr", "com.cy",
    # 동남아 / 인도
    "com.sg", "com.my", "com.ph", "com.vn", "co.th", "in.th", "co.id",
    "co.in", "net.in", "org.in", "com.bd", "com.pk", "com.kh", "com.la",
    # 서비스형 도메인
    "github.io", "blogspot.com", "herokuapp.com", "netlify.app",
    "vercel.app", "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
    "cloudfront.net", "amazonaws.com", "r2.dev", "onrender.com",
    "weebly.com", "wixsite.com", "tistory.com", "cafe24.com",
}

#: 본문 텍스트에서 "맨 도메인"을 찾을 때 인정할 최상위 도메인.
#: index.php 같은 문자열을 도메인으로 오인하지 않기 위해 화이트리스트로 둡니다.
TEXT_SCAN_TLDS = {
    "com", "net", "org", "kr", "co", "io", "me", "tv", "cc", "biz", "info",
    "xyz", "top", "vip", "icu", "club", "site", "online", "shop", "store",
    "live", "life", "fun", "space", "world", "win", "men", "bet", "casino",
    "game", "games", "link", "click", "pro", "asia", "us", "uk", "jp", "cn",
    "tw", "hk", "sg", "my", "ph", "vn", "th", "id", "in", "ru", "su", "ua",
    "de", "fr", "it", "es", "nl", "pl", "tr", "br", "mx", "ar", "ca", "au",
    "nz", "za", "app", "dev", "art", "red", "blue", "one", "sbs", "cfd",
    "lol", "bond", "makeup", "quest", "monster", "beauty", "hair", "skin",
    "mom", "cyou", "rest", "autos", "boats", "homes", "motorcycles",
}

_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9\-._~%]+$", re.IGNORECASE)
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9¡-￿-]{1,63}(?<!-)$", re.IGNORECASE)

# 텍스트에서 URL / 맨 도메인을 찾는 정규식 (난독화 해제 후 적용)
_TLD_ALT = "|".join(sorted(TEXT_SCAN_TLDS, key=len, reverse=True))
TEXT_URL_RE = re.compile(
    r"(?<![\w@.\-])"                                  # 이메일/파일명 중간은 제외
    r"(?:(?P<scheme>https?)://)?"
    r"(?P<host>(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+"
    rf"(?:{_TLD_ALT}))"
    r"(?![a-z0-9\-])"                                 # index.php 를 index.ph 로 오인하지 않도록
    r"(?P<port>:\d{2,5})?"
    r"(?P<path>/[^\s\"'<>()\[\]{}]*)?",
    re.IGNORECASE,
)

# 난독화 치환 규칙: (정규식, 치환문자)
_DEOBFUSCATE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"h[x*]{2}ps?", re.IGNORECASE), "https"),
    (re.compile(r"\s*[\[\(\{<]\s*(?:\.|dot|점|닷|디오티)\s*[\]\)\}>]\s*", re.IGNORECASE), "."),
    (re.compile(r"\s+(?:dot|점|닷)\s+", re.IGNORECASE), "."),
    (re.compile(r"(?<=[a-z0-9])\s*[·・∙•]\s*(?=[a-z0-9])", re.IGNORECASE), "."),
    (re.compile(r"[\[\(\{<]\s*(?:@|at)\s*[\]\)\}>]", re.IGNORECASE), "@"),
    # "abc . com" / "abc .com" 처럼 점 주변 공백만 있는 경우
    (re.compile(r"(?<=[a-z0-9])\s+\.\s*(?=[a-z0-9])", re.IGNORECASE), "."),
    (re.compile(r"(?<=[a-z0-9])\.\s+(?=[a-z0-9])", re.IGNORECASE), "."),
)


def deobfuscate_text(text: str) -> str:
    """``abc[.]com`` → ``abc.com`` 처럼 흘려 쓴 도메인 표기를 되돌립니다."""
    if not text:
        return ""
    # 전각 문자(ａｂｃ．ｃｏｍ)를 반각으로
    cleaned = unicodedata.normalize("NFKC", text)
    for pattern, replacement in _DEOBFUSCATE_RULES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _encode_host(host: str) -> str | None:
    """호스트를 소문자 + punycode 형태로 통일합니다."""
    host = host.strip().strip(".").lower()
    if not host or " " in host:
        return None
    if host.startswith("["):  # IPv6 리터럴은 다루지 않습니다.
        return None

    labels = host.split(".")
    if len(labels) < 2:
        return None

    encoded: list[str] = []
    for label in labels:
        if not label or not _LABEL_RE.match(label):
            return None
        if label.isascii():
            encoded.append(label)
            continue
        try:
            encoded.append(label.encode("idna").decode("ascii"))
        except (UnicodeError, ValueError):
            return None

    result = ".".join(encoded)
    if not _HOST_RE.match(result):
        return None

    tld = encoded[-1]
    if not (tld.isalpha() or tld.startswith("xn--")) or len(tld) < 2:
        return None
    return result


def is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalize_url(raw: str, base: str | None = None) -> str | None:
    """URL 을 비교 가능한 표준 형태로 바꿉니다. 실패하면 ``None``.

    - 상대 경로는 ``base`` 기준으로 절대 경로로 만듭니다.
    - http/https 만 허용하고, 스킴이 없으면 https 로 봅니다.
    - 호스트는 소문자 punycode, 기본 포트와 fragment 는 제거합니다.
    - 추적용 쿼리 파라미터는 제거합니다.
    """
    if not raw:
        return None

    candidate = raw.strip().strip("\"'<>")
    if not candidate:
        return None

    lowered = candidate.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:", "about:", "#")):
        return None

    if base and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", candidate):
        if candidate.startswith("//"):
            candidate = urlsplit(base).scheme + ":" + candidate
        else:
            candidate = urljoin(base, candidate)
    elif candidate.startswith("//"):
        candidate = f"{DEFAULT_SCHEME}:{candidate}"
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", candidate):
        candidate = f"{DEFAULT_SCHEME}://{candidate}"

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None

    host = parts.hostname or ""
    if is_ip_host(host):
        encoded_host = host
    else:
        maybe_host = _encode_host(host)
        if maybe_host is None:
            return None
        encoded_host = maybe_host

    netloc = encoded_host
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{encoded_host}:{port}"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(query_pairs, doseq=True)

    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    """URL 에서 호스트만 뽑습니다."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def registrable_domain(host_or_url: str) -> str:
    """등록가능도메인(예: ``a.b.example.co.kr`` → ``example.co.kr``)을 돌려줍니다.

    번들된 공개 접미사 목록을 쓰므로 완벽하지는 않지만, 같은 사이트의
    서브도메인을 하나로 묶는 데는 충분합니다.
    """
    host = host_or_url
    if "://" in host_or_url or host_or_url.startswith("//"):
        host = host_of(host_or_url)
    host = host.strip().strip(".").lower()
    if not host:
        return ""
    if is_ip_host(host):
        return host

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    for depth in (3, 2):
        if len(labels) > depth:
            suffix = ".".join(labels[-depth:])
            if suffix in MULTI_LABEL_SUFFIXES:
                return ".".join(labels[-(depth + 1):])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    """두 URL 이 같은 등록가능도메인인지."""
    left = registrable_domain(a)
    return bool(left) and left == registrable_domain(b)


def tld_of(host_or_url: str) -> str:
    domain = registrable_domain(host_or_url)
    return domain.rsplit(".", 1)[-1] if "." in domain else ""


def iter_urls_in_text(text: str) -> list[str]:
    """본문 텍스트에서 URL / 맨 도메인을 찾아 정규화된 URL 목록으로 돌려줍니다."""
    found: list[str] = []
    seen: set[str] = set()
    for match in TEXT_URL_RE.finditer(deobfuscate_text(text)):
        scheme = match.group("scheme") or DEFAULT_SCHEME
        host = match.group("host")
        port = match.group("port") or ""
        path = match.group("path") or "/"
        # 문장 끝 마침표/괄호가 경로에 붙어 들어오는 경우 제거
        path = path.rstrip(".,;:!?)]}”’'\"")
        normalized = normalize_url(f"{scheme}://{host}{port}{path}")
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found
