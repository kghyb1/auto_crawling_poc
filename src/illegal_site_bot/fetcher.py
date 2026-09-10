"""HTTP 수집 계층.

24시간 돌아가는 봇이라 다음을 신경 씁니다.

  * 호스트별 요청 간격 제한 — 상대 서버에 부담을 주지 않기 위해
  * robots.txt 준수 (설정으로 끌 수 있음)
  * 응답 크기 상한 — 거대한 파일로 메모리가 터지는 것을 막기 위해
  * 한글 사이트 인코딩(EUC-KR/CP949) 자동 판별
  * 실패 시 제한된 횟수만 재시도
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from .config import CrawlConfig
from .normalizer import normalize_url

log = logging.getLogger(__name__)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml", "text/plain", "text/xml")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)


@dataclass
class FetchResult:
    """한 번의 HTTP 요청 결과."""

    url: str
    final_url: str = ""
    status: int | None = None
    html: str = ""
    error: str = ""
    elapsed_ms: int = 0
    content_type: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    skipped_by_robots: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.html) and self.status is not None and 200 <= self.status < 300


class HostThrottle:
    """호스트별로 최소 요청 간격을 지킵니다."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay = max(0.0, delay_seconds)
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_seen: dict[str, float] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def wait(self, host: str) -> None:
        if not self.delay or not host:
            return
        with self._lock_for(host):
            last = self._last_seen.get(host)
            now = time.monotonic()
            if last is not None:
                remaining = self.delay - (now - last)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_seen[host] = time.monotonic()


class RobotsCache:
    """robots.txt 를 호스트별로 한 번만 받아서 캐시합니다."""

    def __init__(self, session: requests.Session, user_agent: str, timeout: int) -> None:
        self._session = session
        self._user_agent = user_agent
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        parser: RobotFileParser | None = None
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            response = self._session.get(
                robots_url, timeout=self._timeout, allow_redirects=True
            )
            if response.status_code == 200 and len(response.content) <= 512_000:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            # 4xx/5xx 는 "제한 없음"으로 봅니다 (표준 관행).
        except requests.RequestException as exc:
            log.debug("robots.txt 확인 실패 %s: %s", robots_url, exc)

        with self._lock:
            self._cache[key] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self._user_agent, url)
        except Exception:  # 파싱이 이상한 robots.txt
            return True


class Fetcher:
    """스레드에서 함께 쓸 수 있는 HTTP 클라이언트."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=max(10, config.concurrency * 2),
            pool_maxsize=max(10, config.concurrency * 4),
            max_retries=0,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        headers = {
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        headers.update(config.extra_headers or {})
        self.session.headers.update(headers)

        self.throttle = HostThrottle(config.per_host_delay_seconds)
        self.robots = (
            RobotsCache(self.session, config.user_agent, config.request_timeout_seconds)
            if config.respect_robots
            else None
        )

    # -- 내부 유틸 ---------------------------------------------------------
    def _decode(self, response: requests.Response, body: bytes) -> str:
        """한글 사이트의 EUC-KR/CP949 를 포함해 본문을 문자열로 만듭니다."""
        candidates: list[str] = []

        match = _META_CHARSET_RE.search(body[:4096])
        if match:
            candidates.append(match.group(1).decode("ascii", "ignore"))

        header_encoding = response.encoding
        if header_encoding and header_encoding.lower() not in {"iso-8859-1", "ascii"}:
            candidates.append(header_encoding)

        candidates.extend(["utf-8", "cp949", "euc-kr"])

        for encoding in candidates:
            normalized = (encoding or "").strip().lower()
            if not normalized:
                continue
            if normalized in {"euc-kr", "ks_c_5601-1987", "ksc5601", "korean"}:
                normalized = "cp949"
            try:
                return body.decode(normalized)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", "replace")

    def _read_capped(self, response: requests.Response) -> bytes:
        limit = self.config.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65_536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                log.debug("응답이 상한(%d bytes)을 넘어 잘랐습니다: %s", limit, response.url)
                break
        return b"".join(chunks)

    # -- 공개 API ----------------------------------------------------------
    def fetch(self, url: str, *, allow_redirects: bool = True) -> FetchResult:
        """HTML 페이지 하나를 받아옵니다. 예외를 던지지 않고 결과에 담아 돌려줍니다."""
        result = FetchResult(url=url)

        if self.robots is not None and not self.robots.allowed(url):
            result.error = "robots.txt 에서 수집을 금지함"
            result.skipped_by_robots = True
            log.info("robots.txt 로 건너뜀: %s", url)
            return result

        host = urlsplit(url).hostname or ""
        attempts = max(1, self.config.max_retries + 1)
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            self.throttle.wait(host)
            try:
                response = self.session.get(
                    url,
                    timeout=self.config.request_timeout_seconds,
                    allow_redirects=allow_redirects,
                    verify=self.config.verify_tls,
                    stream=True,
                )
            except requests.RequestException as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts:
                    time.sleep(self.config.retry_backoff_seconds * attempt)
                    continue
                break

            try:
                result.status = response.status_code
                result.final_url = response.url
                result.redirect_chain = [r.url for r in response.history]
                result.content_type = (response.headers.get("Content-Type") or "").lower()

                if response.status_code >= 400:
                    result.error = f"HTTP {response.status_code}"
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                        time.sleep(self.config.retry_backoff_seconds * attempt)
                        continue
                    break

                if result.content_type and not any(
                    ctype in result.content_type for ctype in HTML_CONTENT_TYPES
                ):
                    result.error = f"HTML 이 아님({result.content_type})"
                    break

                body = self._read_capped(response)
                result.html = self._decode(response, body)
                result.error = ""
                break
            finally:
                response.close()

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def resolve_redirects(self, url: str) -> tuple[str, int | None, list[str]]:
        """경유 링크의 최종 도착지를 찾습니다.

        ``(최종 URL, 상태코드, 경유 URL 목록)`` 을 돌려줍니다. 실패하면 원래
        URL 을 그대로 돌려줍니다.
        """
        host = urlsplit(url).hostname or ""
        self.throttle.wait(host)
        try:
            response = self.session.get(
                url,
                timeout=self.config.request_timeout_seconds,
                allow_redirects=True,
                verify=self.config.verify_tls,
                stream=True,
            )
        except requests.RequestException as exc:
            log.debug("리다이렉트 추적 실패 %s: %s", url, exc)
            return url, None, []

        try:
            chain = [item.url for item in response.history][: self.config.max_redirect_hops]
            final = normalize_url(response.url) or url
            return final, response.status_code, chain
        finally:
            response.close()

    def check_alive(self, url: str, timeout: int) -> tuple[bool, int | None, str]:
        """사이트가 아직 살아있는지 확인합니다.

        ``(생존 여부, 상태코드, 실제 접속된 URL)``. https 로 실패하면 http 로
        한 번 더 시도합니다(정규화 과정에서 https 를 기본으로 붙이기 때문).
        """
        attempts = [url]
        parts = urlsplit(url)
        if parts.scheme == "https":
            attempts.append(urlunsplit(("http", parts.netloc, parts.path, parts.query, "")))

        last_status: int | None = None
        for candidate in attempts:
            host = urlsplit(candidate).hostname or ""
            self.throttle.wait(host)
            for method in ("head", "get"):
                try:
                    response = self.session.request(
                        method,
                        candidate,
                        timeout=timeout,
                        allow_redirects=True,
                        verify=self.config.verify_tls,
                        stream=True,
                    )
                except requests.RequestException:
                    break  # 이 후보 URL 은 접속 불가 - 다음 후보로
                try:
                    last_status = response.status_code
                    if response.status_code < 400:
                        return True, response.status_code, response.url
                    if method == "head" and response.status_code in {403, 405, 501}:
                        continue  # HEAD 를 막는 서버 - GET 으로 재시도
                    break
                finally:
                    response.close()
        return False, last_status, url

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
