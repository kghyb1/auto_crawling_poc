"""테스트 공용 픽스처.

외부 인터넷에 나가지 않도록, 가짜 홍보사이트를 로컬 HTTP 서버로 띄워서
테스트합니다.
"""

from __future__ import annotations

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from illegal_site_bot.classifier import Classifier, load_rules
from illegal_site_bot.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_RULES = PROJECT_ROOT / "config" / "rules.yaml"

# 가짜 홍보사이트 메인 페이지. 링크를 심는 여러 방식을 한꺼번에 담았습니다.
PROMO_INDEX = """<!doctype html>
<html><head><title>먹튀검증 커뮤니티 - 보증업체 모음</title></head>
<body>
  <div class="banner-list">
    <a href="https://casino-abc777.xyz/">
      <img src="/img/live-casino-abc777.png" alt="에볼루션 라이브카지노 ABC777">
    </a>
    <a href="http://toto-safe-999.top/join" title="안전놀이터 추천">토토 안전놀이터 가입</a>
    <a href="https://www.google.com/search?q=%EA%B2%80%EC%83%89">구글 검색</a>
    <a href="https://t.me/promo_admin_contact">텔레그램 문의</a>
    <div class="item" data-href="https://slot-yamato-55.vip/">야마토 슬롯 사이트</div>
    <span onclick="location.href='https://holdem-king.cc/lobby'">홀덤 바로가기</span>
    <a href="/assets/logo.png"><img src="/assets/logo.png" alt="로고"></a>
  </div>
  <p>먹튀 없는 무료야동 새 주소 안내: freeya-dong[.]com 으로 접속하세요.</p>
  <div class="partners">
    <a href="{PARTNER_URL}">제휴 먹튀검증 커뮤니티</a>
  </div>
  <iframe src="https://plain-ad-frame.example/frame"></iframe>
  <a href="/board/list?page=2">다음 페이지</a>
  <script>var target = "https://webtoon24-free.site/";</script>
</body></html>
"""

PROMO_PAGE2 = """<!doctype html>
<html><head><title>보증업체 모음 2페이지</title></head>
<body>
  <a href="https://baccarat-vip-365.club/"><img alt="바카라 VIP 365 첫충 이벤트"></a>
</body></html>
"""


# 발견 대상이 되는 "두 번째 홍보사이트". 이미 수집된 불법 도메인들을 링크하므로
# 2단계 평가의 A 신호(아는 불법 도메인과의 겹침)가 강하게 나옵니다.
PARTNER_INDEX = """<!doctype html>
<html><head><title>먹튀검증 커뮤니티 - 보증업체 순위</title></head>
<body>
  <h1>먹튀검증 보증업체 순위</h1>
  <div class="banners">
    <a href="https://casino-abc777.xyz/"><img alt="ABC777 카지노"></a>
    <a href="http://toto-safe-999.top/"><img alt="토토 안전놀이터"></a>
    <a href="https://slot-yamato-55.vip/"><img alt="야마토 슬롯"></a>
    <a href="https://holdem-king.cc/"><img alt="홀덤"></a>
    <a href="https://baccarat-vip-365.club/"><img alt="바카라"></a>
    <a href="https://newly-found-casino-42.top/"><img alt="신규 카지노 42"></a>
  </div>
</body></html>
"""

# 홍보사이트가 아니라 "불법사이트 본체"로 보이는 페이지 (역신호 확인용)
LANDING_PAGE = """<!doctype html>
<html><head><title>ABC777 카지노</title></head>
<body>
  <h1>로그인</h1>
  <form>
    <input type="text" name="id" placeholder="아이디">
    <input type="password" name="pw" placeholder="비밀번호">
    <button>로그인</button>
  </form>
  <a href="https://t.me/abc777_help">고객센터</a>
</body></html>
"""


class _PartnerHandler(BaseHTTPRequestHandler):
    """두 번째 홍보사이트 + 불법사이트 본체 흉내."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        return

    def _respond(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/landing"):
            self._respond(LANDING_PAGE.encode("utf-8"))
        elif self.path in {"/", "/index.html"}:
            self._respond(PARTNER_INDEX.encode("utf-8"))
        else:
            self._respond(b"not found", status=404)


class _PromoHandler(BaseHTTPRequestHandler):
    """가짜 홍보사이트 응답."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # 테스트 출력 조용히
        return

    def _respond(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/board/list?page=2"):
            self._respond(PROMO_PAGE2.encode("utf-8"))
        elif self.path in {"/", "/index.html"}:
            page = PROMO_INDEX.replace(
                "{PARTNER_URL}", getattr(self, "partner_url", "https://partner-promo.test/")
            )
            self._respond(page.encode("utf-8"))
        elif self.path == "/robots.txt":
            self._respond(b"not found", status=404, content_type="text/plain")
        else:
            self._respond(b"not found", status=404, content_type="text/plain")


@pytest.fixture(scope="session")
def partner_server():
    """발견 대상이 되는 두 번째 홍보사이트 (다른 호스트로 띄웁니다)."""
    server = ThreadingHTTPServer(("127.0.0.2", 0), _PartnerHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def mirror_server():
    """두 번째 홍보사이트와 내용이 같은 복제(미러) 사이트."""
    server = ThreadingHTTPServer(("127.0.0.3", 0), _PartnerHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def promo_server(partner_server):
    """가짜 홍보사이트 주소(예: ``http://127.0.0.1:PORT/``)를 돌려줍니다."""
    _PromoHandler.partner_url = partner_server
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PromoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def rules():
    return load_rules(REAL_RULES)


@pytest.fixture
def classifier(rules):
    return Classifier(rules)


@pytest.fixture
def config(tmp_path: Path):
    """임시 폴더를 루트로 하는 설정. 실제 rules.yaml 을 그대로 씁니다."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_RULES, config_dir / "rules.yaml")

    (config_dir / "config.yaml").write_text(
        """
crawl:
  interval_seconds: 60
  jitter_seconds: 0
  concurrency: 2
  per_host_delay_seconds: 0
  request_timeout_seconds: 5
  max_retries: 0
  max_pages_per_source: 2
  resolve_candidate_redirects: false
  respect_robots: true
alive_check:
  enabled: false
export:
  directory: "exports"
  filename: "결과.xlsx"
  keep_daily_snapshots: false
  min_score: 25
  always_rewrite: true
storage:
  database: "data/test.sqlite3"
dashboard:
  enabled: false
logging:
  directory: "logs"
runtime:
  state_directory: "run"
""".lstrip(),
        encoding="utf-8",
    )
    return load_config(config_dir / "config.yaml", root=tmp_path)
