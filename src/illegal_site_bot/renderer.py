"""자바스크립트로 배너를 그리는 홍보사이트용 렌더러 (선택 기능).

Playwright 가 설치되어 있지 않으면 조용히 비활성화되고, 일반 HTTP 수집으로
대체됩니다. 설치 방법::

    pip install playwright
    playwright install chromium

Playwright 의 동기 API 는 스레드 간 공유가 안 되므로, 파이프라인에서는
렌더링이 필요한 사이트만 단일 스레드에서 순차 처리합니다.
"""

from __future__ import annotations

import logging

from .config import RendererConfig

log = logging.getLogger(__name__)


class Renderer:
    """Chromium 으로 페이지를 렌더링해 최종 HTML 을 얻습니다."""

    def __init__(self, config: RendererConfig, user_agent: str) -> None:
        self.config = config
        self.user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._unavailable_reason = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _ensure_browser(self) -> bool:
        if self._browser is not None:
            return True
        if self._unavailable_reason:
            return False
        try:
            from playwright.sync_api import sync_playwright  # 지연 임포트
        except ImportError:
            self._unavailable_reason = (
                "playwright 가 설치되지 않았습니다. "
                "`pip install playwright && playwright install chromium` 후 사용하세요."
            )
            log.warning("렌더링 비활성화: %s", self._unavailable_reason)
            return False

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.config.headless)
        except Exception as exc:  # 브라우저 미설치 등
            self._unavailable_reason = f"브라우저 실행 실패: {exc}"
            log.warning("렌더링 비활성화: %s", self._unavailable_reason)
            self.close()
            return False
        return True

    def render(self, url: str) -> str | None:
        """렌더링된 HTML 을 돌려줍니다. 실패하면 ``None``."""
        if not self.config.enabled or not self._ensure_browser():
            return None
        assert self._browser is not None

        context = None
        try:
            context = self._browser.new_context(user_agent=self.user_agent)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(self.config.wait_ms)
            return page.content()
        except Exception as exc:
            log.warning("렌더링 실패 %s: %s", url, exc)
            return None
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # 이미 닫힌 경우
                    pass

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self) -> "Renderer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
