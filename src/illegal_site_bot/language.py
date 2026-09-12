"""사이트 언어 판별 — 한국어 사이트만 남기기 위한 것.

**핵심 제약**: 저장 여부를 정하는 시점(``pipeline._persist``)에 봇은 그 사이트에
한 번도 접속한 적이 없습니다. 홍보사이트 HTML 에서 링크만 뽑아낸 상태라
가진 것은 URL 과 링크 텍스트뿐입니다. 그런데 링크 텍스트는 *홍보사이트의*
언어이지 대상 사이트의 언어가 아닙니다. 한국 홍보사이트가 외국 서버 도박
사이트를 한글 배너로 링크하는 경우가 흔하므로, 링크 텍스트로 판별하면 안 됩니다.

그래서 3단계로 나눠 요청을 아낍니다.

    1) TLD 로 확정      .kr 계열은 통과, 명백한 외국 ccTLD 는 차단 (요청 0회)
    2) 애매하면 1회 요청  인코딩(cp949/euc-kr) + <html lang> + 한글 비율
    3) 도메인 단위 캐시   같은 도메인을 매 사이클 다시 받지 않도록 DB 에 기록

판별하지 못한 경우(접속 실패, 자바스크립트 전용 페이지, 빈 본문)는 **차단이
아니라 보존**이 기본값입니다. 모르는 것을 버리면 조용히 놓치기 때문입니다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import LanguageFilterConfig
from .fetcher import Fetcher
from .normalizer import registrable_domain, tld_of

log = logging.getLogger(__name__)

KOREAN = "ko"
FOREIGN = "foreign"
UNKNOWN = "unknown"

#: 한글 음절 + 자모. 한자/가나는 포함하지 않습니다(일본어·중국어와 구분해야 함).
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")

#: EUC-KR 계열 인코딩이면 사실상 한국어 사이트입니다.
_KOREAN_ENCODINGS = {"cp949", "euc-kr", "euckr", "ks_c_5601-1987", "ksc5601", "korean"}

_HTML_LANG_RE = re.compile(
    r"<html[^>]*\blang\s*=\s*[\"']?([a-zA-Z\-]{2,10})", re.IGNORECASE
)
_META_LANG_RE = re.compile(
    r"<meta[^>]+http-equiv\s*=\s*[\"']?content-language[\"']?[^>]*content\s*=\s*[\"']?"
    r"([a-zA-Z\-]{2,10})",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")

#: 본문이 이보다 짧으면 비율 계산이 무의미해 '판별 불가'로 둡니다.
MIN_TEXT_LETTERS = 30


@dataclass(frozen=True)
class LanguageVerdict:
    """한 사이트에 대한 언어 판별 결과."""

    language: str = UNKNOWN
    reason: str = ""
    hangul_ratio: float = 0.0
    encoding: str = ""
    fetched: bool = False

    @property
    def is_korean(self) -> bool:
        return self.language == KOREAN

    @property
    def is_foreign(self) -> bool:
        return self.language == FOREIGN


def hangul_ratio(text: str) -> float:
    """글자 중 한글이 차지하는 비율.

    분모를 '글자'로 한정합니다. 숫자·기호·공백까지 넣으면 한국어 페이지도
    비율이 낮게 나와 판별이 흐려집니다.
    """
    if not text:
        return 0.0
    letters = 0
    hangul = 0
    for char in text:
        if not char.isalpha():
            continue
        letters += 1
        if _HANGUL_RE.match(char):
            hangul += 1
    if letters < MIN_TEXT_LETTERS:
        return 0.0
    return hangul / letters


def visible_text(html: str, limit: int = 40_000) -> str:
    """태그를 걷어낸 본문 텍스트 (한글 비율 계산용, 가벼운 정규식 처리)."""
    if not html:
        return ""
    without_scripts = _TAG_RE.sub(" ", html[: limit * 2])
    return _ANY_TAG_RE.sub(" ", without_scripts)[:limit]


def declared_language(html: str) -> str:
    """``<html lang>`` 또는 Content-Language 메타에 적힌 언어 코드."""
    for pattern in (_HTML_LANG_RE, _META_LANG_RE):
        match = pattern.search(html[:8192])
        if match:
            return match.group(1).strip().lower()
    return ""


class LanguageDetector:
    """언어를 판별하고 그 결과를 도메인 단위로 캐시합니다."""

    def __init__(
        self,
        settings: LanguageFilterConfig,
        fetcher: Fetcher | None = None,
        storage=None,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher
        self.storage = storage
        self._checks_used = 0
        self._cycle_cache: dict[str, LanguageVerdict] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def begin_cycle(self) -> None:
        """사이클마다 요청 예산과 임시 캐시를 초기화합니다."""
        self._checks_used = 0
        self._cycle_cache = {}

    @property
    def checks_used(self) -> int:
        return self._checks_used

    # -- 요청 없이 판별 ----------------------------------------------------
    def _verdict_from_tld(self, url: str) -> LanguageVerdict | None:
        tld = tld_of(url)
        if not tld:
            return None
        if tld in self.settings.korean_tlds:
            return LanguageVerdict(KOREAN, f"한국 TLD(.{tld})")
        if tld in self.settings.foreign_tlds:
            return LanguageVerdict(FOREIGN, f"외국 TLD(.{tld})")
        return None

    # -- 본문으로 판별 -----------------------------------------------------
    def verdict_from_page(self, html: str, encoding: str = "") -> LanguageVerdict:
        """이미 받아둔 HTML 로 판별합니다 (추가 요청 없음)."""
        normalized_encoding = (encoding or "").strip().lower()
        if normalized_encoding in _KOREAN_ENCODINGS:
            # EUC-KR 계열로 인코딩된 페이지는 한국어 사이트로 봐도 됩니다.
            return LanguageVerdict(
                KOREAN, f"한국어 인코딩({normalized_encoding})",
                encoding=normalized_encoding, fetched=True,
            )

        text = visible_text(html)
        ratio = hangul_ratio(text)
        declared = declared_language(html)

        if ratio >= self.settings.min_hangul_ratio:
            return LanguageVerdict(
                KOREAN, f"한글 비율 {ratio:.0%}", hangul_ratio=ratio,
                encoding=normalized_encoding, fetched=True,
            )
        if declared.startswith("ko"):
            return LanguageVerdict(
                KOREAN, f"html lang={declared}", hangul_ratio=ratio,
                encoding=normalized_encoding, fetched=True,
            )

        # 글자가 너무 적으면(자바스크립트 전용 페이지 등) 판단을 미룹니다.
        letters = sum(1 for char in text if char.isalpha())
        if letters < MIN_TEXT_LETTERS:
            return LanguageVerdict(
                UNKNOWN, "본문이 너무 짧아 판별 불가", hangul_ratio=ratio,
                encoding=normalized_encoding, fetched=True,
            )

        detail = f"한글 비율 {ratio:.0%}"
        if declared:
            detail += f", html lang={declared}"
        return LanguageVerdict(
            FOREIGN, detail, hangul_ratio=ratio,
            encoding=normalized_encoding, fetched=True,
        )

    # -- 통합 판별 ---------------------------------------------------------
    def detect(self, url: str) -> LanguageVerdict:
        """URL 하나의 언어를 판별합니다. 필요하면 1회 접속합니다."""
        domain = registrable_domain(url)
        if not domain:
            return LanguageVerdict(UNKNOWN, "도메인을 알 수 없음")

        cached = self._cycle_cache.get(domain)
        if cached is not None:
            return cached

        stored = self._stored_verdict(domain)
        if stored is not None:
            self._cycle_cache[domain] = stored
            return stored

        verdict = self._verdict_from_tld(url)
        if verdict is None:
            verdict = self._detect_by_fetching(url)

        self._remember(domain, verdict)
        return verdict

    def _detect_by_fetching(self, url: str) -> LanguageVerdict:
        if not self.settings.fetch_when_unknown or self.fetcher is None:
            return LanguageVerdict(UNKNOWN, "TLD 로 판별 불가 (접속 확인 안 함)")
        if self._checks_used >= self.settings.max_checks_per_cycle:
            # 예산을 다 썼습니다. 버리지 않고 다음 사이클로 미룹니다.
            return LanguageVerdict(UNKNOWN, "이번 사이클 판별 예산 소진")

        self._checks_used += 1
        result = self.fetcher.fetch(url)
        if not result.ok:
            return LanguageVerdict(
                UNKNOWN, f"접속 실패({result.error or result.status})"
            )
        return self.verdict_from_page(result.html, result.encoding)

    def _stored_verdict(self, domain: str) -> LanguageVerdict | None:
        if self.storage is None:
            return None
        row = self.storage.domain_language(domain, self.settings.recheck_after_days)
        if row is None:
            return None
        return LanguageVerdict(
            language=row["language"],
            reason=row["reason"],
            hangul_ratio=float(row["hangul_ratio"] or 0.0),
            encoding=row["encoding"] or "",
        )

    def _remember(self, domain: str, verdict: LanguageVerdict) -> None:
        self._cycle_cache[domain] = verdict
        # 판별하지 못한 것은 캐시하지 않습니다. 다음 사이클에 다시 시도해야
        # 예산 부족이나 일시적 접속 실패로 영영 미판별로 남지 않습니다.
        if self.storage is not None and verdict.language != UNKNOWN:
            self.storage.record_domain_language(
                domain,
                language=verdict.language,
                reason=verdict.reason,
                hangul_ratio=verdict.hangul_ratio,
                encoding=verdict.encoding,
            )

    def remember_page(self, url: str, html: str, encoding: str = "") -> LanguageVerdict:
        """이미 받아둔 페이지로 판별해 캐시에 넣습니다 (추가 요청 없음).

        홍보사이트 후보 평가처럼 어차피 페이지를 받는 곳에서 불러 쓰면
        언어 판별 예산을 쓰지 않고 캐시를 채울 수 있습니다.
        """
        domain = registrable_domain(url)
        verdict = self.verdict_from_page(html, encoding)
        if domain:
            self._remember(domain, verdict)
        return verdict

    # -- 저장 여부 ---------------------------------------------------------
    def should_store(self, verdict: LanguageVerdict) -> bool:
        """이 판별 결과로 DB 에 저장해도 되는지."""
        if not self.enabled:
            return True
        if verdict.language == UNKNOWN:
            return self.settings.keep_when_undetermined
        return verdict.language in self.settings.keep

    def allows(self, url: str) -> tuple[bool, LanguageVerdict]:
        """``(저장해도 되는지, 판별 결과)``. 필터가 꺼져 있으면 판별하지 않습니다."""
        if not self.enabled:
            return True, LanguageVerdict(UNKNOWN, "언어 필터 꺼짐")
        verdict = self.detect(url)
        return self.should_store(verdict), verdict
