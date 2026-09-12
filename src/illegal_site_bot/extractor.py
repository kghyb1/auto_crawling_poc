"""홍보사이트 HTML 에서 외부 링크 후보를 추출합니다.

홍보사이트는 링크를 여러 방식으로 심어둡니다. 그래서 한 가지 방법만으로는
빠지는 것이 많습니다.

  * ``<a href>`` — 가장 흔한 경우
  * 이미지 배너 (``<a><img></a>``) — 링크 텍스트가 없으므로 alt/title 을 봅니다
  * ``onclick="location.href='...'"`` / ``window.open('...')``
  * ``data-href`` / ``data-url`` 같은 커스텀 속성
  * ``<iframe src>``, ``<meta http-equiv=refresh>``
  * 스크립트 안에 문자열로 박아둔 주소
  * 본문에 텍스트로만 적어둔 주소 (``abc[.]com``)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from .normalizer import (
    iter_urls_in_text,
    normalize_url,
    registrable_domain,
    same_site,
)

MAX_TEXT_LEN = 200

#: 링크가 들어있을 수 있는 커스텀 속성
LINK_ATTRIBUTES = (
    "href",
    "data-href",
    "data-url",
    "data-link",
    "data-src",
    "data-target",
    "data-go",
)

#: 자바스크립트 안에서 이동 주소를 찾는 패턴
_JS_URL_RE = re.compile(
    r"""(?:location\s*(?:\.\s*(?:href|replace|assign)\s*)?(?:=|\()\s*|window\.open\s*\(\s*)"""
    r"""["']([^"']{4,400})["']""",
    re.IGNORECASE,
)
_JS_QUOTED_URL_RE = re.compile(r"""["'](https?://[^"'\s]{4,400})["']""", re.IGNORECASE)
_META_REFRESH_RE = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    """홍보 페이지에서 발견한 외부 링크 하나."""

    url: str
    anchor_text: str = ""
    context_text: str = ""
    method: str = "a_href"
    banner: bool = False

    @property
    def domain(self) -> str:
        return registrable_domain(self.url)


@dataclass
class ExtractResult:
    title: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)


# 후보 신뢰도 순서: 앵커 텍스트가 있는 링크 > 배너 > 스크립트/텍스트
_METHOD_RANK = {
    "a_href": 5,
    "data_attr": 4,
    "js_onclick": 4,
    "iframe": 3,
    "meta_refresh": 3,
    "js_script": 2,
    "text": 1,
}


#: 엑셀(openpyxl)이 거부하는 제어문자. 크롤링한 제목/텍스트에 섞여 들어오면
#: 엑셀 저장이 통째로 실패하므로 들어오는 길목에서 제거합니다.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: object) -> str:
    if not isinstance(text, str) or not text:
        return ""
    stripped = _CONTROL_CHARS_RE.sub("", text)
    return re.sub(r"\s+", " ", stripped).strip()[:MAX_TEXT_LEN]


def _anchor_text(tag: Tag) -> tuple[str, bool]:
    """링크의 표시 텍스트와, 이미지 배너인지 여부를 돌려줍니다."""
    parts: list[str] = [tag.get_text(" ", strip=True)]
    banner = False
    for img in tag.find_all("img"):
        banner = True
        for attr in ("alt", "title"):
            value = img.get(attr)
            if isinstance(value, str):
                parts.append(value)
        src = img.get("src")
        if isinstance(src, str):
            # 배너 파일명에 업체명이 들어있는 경우가 잦습니다: /img/casino-abc777.png
            name = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            parts.append(re.sub(r"[_\-]+", " ", name))
    for attr in ("title", "aria-label"):
        value = tag.get(attr)
        if isinstance(value, str):
            parts.append(value)
    return _clean(" ".join(p for p in parts if p)), banner


#: 문맥을 찾아 올라갈 때 여기까지 오면 멈춥니다. 페이지 전체 텍스트를 문맥으로
#: 쓰면 다른 배너의 키워드가 섞여 카테고리 판정이 엉망이 됩니다.
_CONTEXT_STOP_TAGS = {"body", "html", "[document]", "main"}


#: 이 개수를 넘는 링크가 들어있는 컨테이너는 문맥으로 쓰지 않습니다.
#: 배너를 여러 개 늘어놓은 목록에서는 옆 배너의 키워드가 섞여 들어옵니다.
_MAX_LINKS_IN_CONTEXT = 2


def _context_text(tag: Tag) -> str:
    """링크 주변 문맥을 가져옵니다.

    "이 링크에 대한 설명"으로 볼 수 있는 가장 가까운 조상 요소의 텍스트만
    씁니다. 링크가 여럿 들어있는 배너 목록은 다른 업체 설명이 섞이므로
    문맥이 없는 것으로 처리합니다.
    """
    parent = tag.parent
    for _ in range(3):
        if parent is None or parent.name in _CONTEXT_STOP_TAGS:
            break
        if len(parent.find_all(["a", "area"])) > _MAX_LINKS_IN_CONTEXT:
            return ""
        text = _clean(parent.get_text(" ", strip=True))
        if len(text) > 10:
            return text
        parent = parent.parent
    return ""


def _looks_like_pagination(url: str, patterns: tuple[str, ...]) -> bool:
    lowered = url.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def extract(
    html: str,
    source_url: str,
    pagination_patterns: tuple[str, ...] = (),
) -> ExtractResult:
    """HTML 에서 외부 링크 후보와 내부 페이지 링크를 뽑아냅니다."""
    result = ExtractResult()
    if not html:
        return result

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # lxml 미설치 등 - 내장 파서로 대체
        soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.string:
        result.title = _clean(str(soup.title.string))

    # 같은 URL 이 여러 방법으로 발견되면 신뢰도 높은 쪽을 남깁니다.
    best: dict[str, Candidate] = {}
    internal: list[str] = []
    seen_internal: set[str] = set()

    def add(candidate: Candidate) -> None:
        previous = best.get(candidate.url)
        if previous is None:
            best[candidate.url] = candidate
            return
        new_rank = (
            _METHOD_RANK.get(candidate.method, 0),
            len(candidate.anchor_text),
            candidate.banner,
        )
        old_rank = (
            _METHOD_RANK.get(previous.method, 0),
            len(previous.anchor_text),
            previous.banner,
        )
        if new_rank > old_rank:
            # 텍스트 정보는 합쳐서 보존합니다.
            merged_anchor = candidate.anchor_text or previous.anchor_text
            merged_context = candidate.context_text or previous.context_text
            best[candidate.url] = Candidate(
                url=candidate.url,
                anchor_text=merged_anchor,
                context_text=merged_context,
                method=candidate.method,
                banner=candidate.banner or previous.banner,
            )

    def consider(raw_url: str, anchor: str, context: str, method: str, banner: bool) -> None:
        normalized = normalize_url(raw_url, base=source_url)
        if not normalized:
            return
        if same_site(normalized, source_url):
            if _looks_like_pagination(normalized, pagination_patterns):
                if normalized not in seen_internal:
                    seen_internal.add(normalized)
                    internal.append(normalized)
            return
        add(
            Candidate(
                url=normalized,
                anchor_text=_clean(anchor),
                context_text=_clean(context),
                method=method,
                banner=banner,
            )
        )

    # 1) <a> 와 링크성 커스텀 속성
    for tag in soup.find_all(["a", "area", "div", "li", "span", "button", "img"]):
        is_anchor = tag.name in {"a", "area"}
        # 앵커 텍스트와 문맥은 태그당 한 번만 계산합니다. 속성마다 다시 구하면
        # 배너가 수백 개인 페이지에서 조상 트리를 수천 번 훑게 됩니다.
        # 문맥은 ''(문맥 없음)도 정상 결과라 계산 여부를 따로 들고 있어야 합니다.
        text_cache: tuple[str, bool] | None = _anchor_text(tag) if is_anchor else None
        context_cache: str | None = None

        def anchor_info() -> tuple[str, bool]:
            nonlocal text_cache
            if text_cache is None:
                text_cache = _anchor_text(tag)
            return text_cache

        def context_of() -> str:
            nonlocal context_cache
            if context_cache is None:
                context_cache = _context_text(tag)
            return context_cache

        for attr in LINK_ATTRIBUTES:
            value = tag.get(attr)
            if not isinstance(value, str) or not value.strip():
                continue
            if attr == "href" and not is_anchor:
                continue
            if attr == "data-src" and tag.name == "img":
                continue  # 이미지 지연 로딩 경로일 뿐
            anchor, banner = anchor_info()
            method = "a_href" if attr == "href" else "data_attr"
            consider(value, anchor, context_of(), method, banner)

        # onclick 등 인라인 스크립트
        for attr in ("onclick", "onmousedown", "ontouchstart"):
            script = tag.get(attr)
            if not isinstance(script, str):
                continue
            anchor, banner = anchor_info()
            for match in _JS_URL_RE.finditer(script):
                consider(match.group(1), anchor, context_of(), "js_onclick", banner)

    # 2) iframe / frame
    for tag in soup.find_all(["iframe", "frame"]):
        src = tag.get("src")
        if isinstance(src, str):
            consider(src, _clean(tag.get("title")), "", "iframe", False)

    # 3) meta refresh
    for tag in soup.find_all("meta"):
        equiv = tag.get("http-equiv")
        if isinstance(equiv, str) and equiv.lower() == "refresh":
            content = tag.get("content")
            if isinstance(content, str):
                match = _META_REFRESH_RE.search(content)
                if match:
                    consider(match.group(1), "", "", "meta_refresh", False)

    # 4) <script> 본문에 문자열로 박힌 주소
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if not body or len(body) > 200_000:
            continue
        for match in _JS_URL_RE.finditer(body):
            consider(match.group(1), "", "", "js_script", False)
        for match in _JS_QUOTED_URL_RE.finditer(body):
            consider(match.group(1), "", "", "js_script", False)

    # 5) 본문 텍스트에 적어둔 주소 (스크립트/스타일/주석 제외)
    for element in soup.find_all(string=True):
        if isinstance(element, Comment):
            continue
        parent_name = element.parent.name if element.parent else ""
        if parent_name in {"script", "style", "noscript", "title"}:
            continue
        text = str(element)
        if len(text.strip()) < 4 or "." not in text:
            continue
        urls = iter_urls_in_text(text)
        if not urls:
            continue
        # 텍스트로 적힌 주소는 그 문장 자체가 이미 문맥이므로 따로 더 넓히지 않습니다.
        for url in urls:
            if same_site(url, source_url):
                continue
            consider(url, _clean(text), "", "text", False)

    result.candidates = sorted(best.values(), key=lambda c: c.url)
    result.internal_links = internal
    return result


@dataclass
class PageSignals:
    """페이지의 성격을 가늠하는 신호들 (홍보사이트 판별 2단계에서 사용)."""

    title: str = ""
    headings: str = ""
    meta_description: str = ""
    text: str = ""
    has_password_input: bool = False


def extract_page_signals(html: str, max_text: int = 20_000) -> PageSignals:
    """제목·헤딩·본문·로그인 폼 여부를 뽑습니다."""
    signals = PageSignals()
    if not html:
        return signals
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.string:
        signals.title = _clean(str(soup.title.string))

    headings = [
        tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2"], limit=10)
    ]
    signals.headings = _clean(" ".join(headings))

    for tag in soup.find_all("meta"):
        name = tag.get("name")
        if isinstance(name, str) and name.lower() == "description":
            signals.meta_description = _clean(tag.get("content"))
            break

    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    signals.text = soup.get_text(" ", strip=True)[:max_text]

    signals.has_password_input = any(
        isinstance(tag.get("type"), str) and tag.get("type").lower() == "password"
        for tag in soup.find_all("input")
    )
    return signals


def extract_meta_redirect(html: str, source_url: str) -> str | None:
    """리다이렉트 경유 페이지에서 다음 목적지를 찾습니다 (meta refresh / JS 이동)."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all("meta"):
        equiv = tag.get("http-equiv")
        if isinstance(equiv, str) and equiv.lower() == "refresh":
            content = tag.get("content")
            if isinstance(content, str):
                match = _META_REFRESH_RE.search(content)
                if match:
                    target = normalize_url(match.group(1), base=source_url)
                    if target and not same_site(target, source_url):
                        return target

    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        for match in _JS_URL_RE.finditer(body or ""):
            target = normalize_url(match.group(1), base=source_url)
            if target and not same_site(target, source_url):
                return target
    return None
