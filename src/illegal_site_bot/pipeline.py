"""수집 사이클 한 번의 처리 흐름.

    홍보사이트 목록 → 페이지 수집 → 링크 추출 → 경유 링크 최종 목적지 추적
      → 규칙으로 판별/점수 → DB 저장(신규/중복 갱신)

HTTP 요청은 여러 스레드로 병렬 처리하고, DB 쓰기는 결과가 도착하는 대로
메인 스레드에서 처리합니다.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from .classifier import Classifier
from .config import Config
from .discovery import Discovery
from .extractor import Candidate, extract, extract_meta_redirect
from .fetcher import Fetcher
from .language import LanguageDetector
from .normalizer import normalize_url, registrable_domain, same_site
from .renderer import Renderer
from .storage import RunStats, SourceRow, Storage

log = logging.getLogger(__name__)

StopCheck = Callable[[], bool]

#: 경유(리다이렉트) 페이지로 의심되는 URL 형태
_GATEWAY_HINTS = (
    "/go/", "/go?", "/goto", "/link", "/out", "/jump", "/click", "/redirect",
    "/bridge", "/away", "/banner", "/ad/", "/move", "url=", "link=", "target=",
    "goto=", "u=", "redirect=",
)


@dataclass
class SourceOutcome:
    """홍보사이트 한 곳을 수집한 결과 (DB 저장 전 단계)."""

    source_id: int
    source_url: str
    source_name: str
    ok: bool = False
    status: str = ""
    error: str = ""
    pages_fetched: int = 0
    title: str = ""
    # (후보, 후보가 발견된 페이지 URL, 경유 URL)
    findings: list[tuple[Candidate, str, str]] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        classifier: Classifier,
        fetcher: Fetcher,
        renderer: Renderer | None = None,
        pagination_patterns: tuple[str, ...] = (),
        discovery: Discovery | None = None,
        language: LanguageDetector | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.classifier = classifier
        self.fetcher = fetcher
        self.renderer = renderer
        self.pagination_patterns = pagination_patterns
        self.discovery = discovery
        self.language = language
        self._redirect_budget = 0
        self._budget_lock = threading.Lock()

    # -- 경유 링크 처리 ----------------------------------------------------
    @staticmethod
    def _unwrap_query_redirect(url: str) -> str | None:
        """``/go?url=https://target.com`` 처럼 쿼리에 목적지가 담긴 경우를 풀어냅니다."""
        try:
            query = urlsplit(url).query
        except ValueError:
            return None
        if not query:
            return None
        for _key, value in parse_qsl(query, keep_blank_values=False):
            if len(value) < 8 or "." not in value:
                continue
            candidate = normalize_url(value)
            if candidate and not same_site(candidate, url):
                return candidate
        return None

    @staticmethod
    def _looks_like_gateway(url: str) -> bool:
        lowered = url.lower()
        return any(hint in lowered for hint in _GATEWAY_HINTS)

    def _take_redirect_budget(self) -> bool:
        with self._budget_lock:
            if self._redirect_budget <= 0:
                return False
            self._redirect_budget -= 1
            return True

    def _resolve_target(self, url: str) -> tuple[str, str]:
        """후보의 실제 목적지를 찾습니다. ``(최종 URL, 경유 URL)``.

        경유가 없었다면 두 번째 값은 빈 문자열입니다.
        """
        unwrapped = self._unwrap_query_redirect(url)
        if unwrapped:
            return unwrapped, url

        if not self.config.crawl.resolve_candidate_redirects:
            return url, ""
        if not self._looks_like_gateway(url):
            return url, ""
        if not self._take_redirect_budget():
            return url, ""

        final, _status, _chain = self.fetcher.resolve_redirects(url)
        if final and not same_site(final, url):
            return final, url

        # HTTP 리다이렉트가 아니라 meta refresh / JS 로 넘기는 경유 페이지도 있습니다.
        result = self.fetcher.fetch(url)
        if result.ok:
            target = extract_meta_redirect(result.html, result.final_url or url)
            if target and not same_site(target, url):
                return target, url
        return url, ""

    # -- 홍보사이트 한 곳 수집 (워커 스레드) -------------------------------
    def _crawl_source(self, source: SourceRow, stop_check: StopCheck) -> SourceOutcome:
        outcome = SourceOutcome(
            source_id=source.id,
            source_url=source.url,
            source_name=source.name or registrable_domain(source.url),
        )

        max_pages = max(1, source.max_pages or self.config.crawl.max_pages_per_source)
        queue: list[str] = [source.url]
        visited: set[str] = set()
        use_renderer = bool(
            self.renderer
            and self.renderer.enabled
            and (source.render or self.config.renderer.render_all)
        )

        while queue and len(visited) < max_pages:
            if stop_check():
                outcome.error = "중단 요청으로 수집을 멈췄습니다."
                break
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)

            html = ""
            if use_renderer and self.renderer is not None:
                html = self.renderer.render(page_url) or ""

            if not html:
                result = self.fetcher.fetch(page_url)
                if not result.ok:
                    if page_url == source.url:
                        outcome.status = str(result.status or "실패")
                        outcome.error = result.error or "본문을 가져오지 못했습니다."
                        return outcome
                    log.debug("내부 페이지 수집 실패 %s: %s", page_url, result.error)
                    continue
                html = result.html
                if page_url == source.url:
                    outcome.status = str(result.status)

            outcome.pages_fetched += 1
            extracted = extract(html, page_url, self.pagination_patterns)
            if page_url == source.url and extracted.title:
                outcome.title = extracted.title

            for candidate in extracted.candidates:
                target, via = self._resolve_target(candidate.url)
                if target != candidate.url:
                    candidate = Candidate(
                        url=target,
                        anchor_text=candidate.anchor_text,
                        context_text=candidate.context_text,
                        method=candidate.method,
                        banner=candidate.banner,
                    )
                outcome.findings.append((candidate, page_url, via))

            for internal in extracted.internal_links:
                if internal not in visited and len(visited) + len(queue) < max_pages:
                    queue.append(internal)

        outcome.ok = outcome.pages_fetched > 0
        if not outcome.status:
            outcome.status = "OK" if outcome.ok else "실패"
        return outcome

    # -- 저장 (메인 스레드) -------------------------------------------------
    def _persist(
        self, outcome: SourceOutcome, stats: RunStats, source: SourceRow | None = None
    ) -> None:
        rules = self.classifier.rules
        kept = 0
        seen_urls: set[str] = set()

        for candidate, page_url, via in outcome.findings:
            stats.candidates_found += 1
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)

            verdict = self.classifier.classify(
                candidate.url, candidate.anchor_text, candidate.context_text
            )
            if verdict.excluded:
                continue

            # 이 링크가 '또 다른 홍보사이트'인지도 함께 봅니다. 불법사이트 판별과
            # 배타적이지 않습니다 - 홍보사이트는 도박 키워드를 잔뜩 달고 있어서
            # 양쪽에 다 걸리는데, 2단계 평가에서 확정되면 그때 정리됩니다.
            if self.discovery is not None and source is not None:
                if self.discovery.consider(candidate, source):
                    stats.candidates_added += 1

            if verdict.score < rules.candidate_threshold and not verdict.always_keep:
                continue

            # 언어 필터 — 한국어 사이트만 저장합니다.
            # 연락 채널(텔레그램 등)은 언어를 따질 대상이 아니라 건너뜁니다.
            language_code = ""
            if self.language is not None and not verdict.always_keep:
                allowed, language_verdict = self.language.allows(candidate.url)
                language_code = language_verdict.language
                if not allowed:
                    stats.dropped_foreign += 1
                    # 저장을 막는 것은 되돌릴 수 없으므로 무엇을 왜 버렸는지
                    # 반드시 남깁니다.
                    log.info(
                        "외국어로 판단해 저장하지 않음: %s (%s)",
                        candidate.url,
                        language_verdict.reason,
                    )
                    continue

            site_id, is_new = self.storage.record_site(
                candidate.url,
                category=verdict.category,
                category_label=verdict.label,
                base_score=verdict.score,
                matched_keywords=verdict.matched_keywords_text,
                reasons=verdict.reasons_text,
                title="",
                redirect_from=via,
                language=language_code,
            )
            self.storage.record_observation(
                site_id=site_id,
                source_id=outcome.source_id,
                source_url=outcome.source_url,
                page_url=page_url,
                anchor_text=candidate.anchor_text,
                method=candidate.method,
                banner=candidate.banner,
                score=verdict.score,
            )
            self.storage.refresh_site_score(
                site_id, rules.cross_source_bonus_per_source, rules.cross_source_bonus_max
            )
            kept += 1
            if is_new:
                stats.new_sites += 1
                log.info(
                    "신규 발견 [%s %d점] %s  ← %s",
                    verdict.label,
                    verdict.score,
                    candidate.url,
                    outcome.source_name or outcome.source_url,
                )
            else:
                stats.updated_sites += 1

        stats.pages_fetched += outcome.pages_fetched
        if outcome.ok:
            stats.sources_ok += 1
        else:
            stats.sources_failed += 1

        self.storage.record_source_result(
            source_id=outcome.source_id,
            ok=outcome.ok,
            status=outcome.status,
            error=outcome.error,
            found=kept,
        )
        if outcome.ok:
            log.info(
                "수집 완료 %s: 페이지 %d개, 후보 %d건 저장",
                outcome.source_name or outcome.source_url,
                outcome.pages_fetched,
                kept,
            )
        else:
            log.warning(
                "수집 실패 %s: %s",
                outcome.source_name or outcome.source_url,
                outcome.error or outcome.status,
            )

    # -- 사이클 ------------------------------------------------------------
    def run_cycle(self, stop_check: StopCheck = lambda: False) -> RunStats:
        """등록된 모든 홍보사이트를 한 바퀴 수집합니다."""
        sources = self.storage.list_sources(enabled_only=True)
        stats = RunStats(sources_total=len(sources))
        if not sources:
            stats.note = "수집 대상이 없습니다. targets.yaml 또는 add-source 로 등록하세요."
            log.warning(stats.note)
            return stats

        self._redirect_budget = self.config.crawl.max_redirect_resolutions
        if self.discovery is not None:
            self.discovery.begin_cycle()
        if self.language is not None:
            self.language.begin_cycle()

        needs_render = [
            source
            for source in sources
            if self.renderer
            and self.renderer.enabled
            and (source.render or self.config.renderer.render_all)
        ]
        render_ids = {source.id for source in needs_render}
        plain = [source for source in sources if source.id not in render_ids]

        # 1) 일반 사이트: 병렬 수집
        if plain:
            workers = min(self.config.crawl.concurrency, len(plain))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl") as pool:
                futures = {
                    pool.submit(self._crawl_source, source, stop_check): source
                    for source in plain
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # 한 사이트 오류가 전체를 멈추지 않게
                        log.exception("수집 중 예외 %s", source.url)
                        outcome = SourceOutcome(
                            source_id=source.id,
                            source_url=source.url,
                            source_name=source.name,
                            ok=False,
                            status="예외",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    self._persist(outcome, stats, source)

        # 2) 렌더링 필요한 사이트: Playwright 동기 API 제약으로 순차 처리
        for source in needs_render:
            if stop_check():
                stats.note = "중단 요청으로 사이클을 조기 종료했습니다."
                break
            try:
                outcome = self._crawl_source(source, stop_check)
            except Exception as exc:
                log.exception("렌더링 수집 중 예외 %s", source.url)
                outcome = SourceOutcome(
                    source_id=source.id,
                    source_url=source.url,
                    source_name=source.name,
                    ok=False,
                    status="예외",
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._persist(outcome, stats, source)

        return stats

    # -- 생존 확인 ---------------------------------------------------------
    def run_alive_checks(self, stop_check: StopCheck = lambda: False) -> tuple[int, int]:
        """수집된 사이트가 아직 열려 있는지 확인합니다. ``(확인 수, 생존 수)``."""
        settings = self.config.alive_check
        rows = self.storage.sites_to_check(settings.batch_size)
        if not rows:
            return 0, 0

        checked = 0
        alive_count = 0
        workers = min(self.config.crawl.concurrency, max(1, len(rows)))

        def check(row) -> tuple[int, bool, int | None, str]:
            alive, status, working_url = self.fetcher.check_alive(
                row["url"], settings.timeout_seconds
            )
            return int(row["id"]), alive, status, working_url

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="alive") as pool:
            futures = [pool.submit(check, row) for row in rows]
            for future in as_completed(futures):
                if stop_check():
                    break
                try:
                    site_id, alive, status, working_url = future.result()
                except Exception as exc:
                    log.debug("생존 확인 실패: %s", exc)
                    continue
                corrected = working_url if alive and working_url else None
                normalized = normalize_url(corrected) if corrected else None
                self.storage.update_alive(site_id, alive, status, normalized)
                checked += 1
                alive_count += int(alive)

        log.info("생존 확인 %d건 (생존 %d / 접속불가 %d)", checked, alive_count, checked - alive_count)
        return checked, alive_count
