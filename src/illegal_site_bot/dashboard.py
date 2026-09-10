"""브라우저에서 봇을 켜고 끄는 로컬 관리 화면.

의존성을 늘리지 않기 위해 표준 라이브러리 ``http.server`` 만 씁니다.
기본값은 127.0.0.1 바인딩이라 이 PC 에서만 열립니다. 외부에 노출하려면
``dashboard.token`` 을 반드시 설정해야 합니다(설정 검증에서 강제).

봇에게 보내는 지시는 CLI 와 똑같이 ``run/`` 폴더의 플래그 파일로 전달되므로,
데몬과 대시보드 사이에 별도의 통신 채널이 필요하지 않습니다.
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .classifier import CONTACT_CATEGORY, Classifier
from .config import Config
from .control import Control
from .storage import Storage
from .timeutil import days_ago, humanize_duration, local_str, now_utc, parse_iso

log = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>불법사이트 수집 봇</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f5f7; --card:#fff; --line:#e3e5e8;
          --text:#1b1d20; --muted:#6b7178; --on:#0f9d58; --off:#c0392b; --accent:#1f3864; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#15171a; --card:#1e2125; --line:#2e3236; --text:#e8eaed; --muted:#9aa0a6; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--text);
         font:14px/1.55 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif; }
  .wrap { max-width: 1000px; margin: 0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:18px; margin-bottom:16px; }
  .row { display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  .badge { display:inline-flex; align-items:center; gap:8px; font-weight:700; font-size:18px; }
  .dot { width:12px; height:12px; border-radius:50%; }
  .dot.on { background:var(--on); box-shadow:0 0 0 4px rgba(15,157,88,.18); }
  .dot.off { background:var(--off); box-shadow:0 0 0 4px rgba(192,57,43,.18); }
  .dot.dead { background:var(--muted); }
  button { font:inherit; font-weight:600; padding:9px 18px; border-radius:8px;
           border:1px solid var(--line); background:var(--card); color:var(--text); cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--on); border-color:var(--on); color:#fff; }
  button.danger  { background:var(--off); border-color:var(--off); color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }
  .stat b { display:block; font-size:22px; }
  .stat span { color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; font-size:12px; }
  td.url { font-family:ui-monospace,Consolas,monospace; word-break:break-all; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:13px; }
  .kv div:nth-child(odd) { color:var(--muted); }
  .msg { margin-top:12px; font-size:13px; color:var(--muted); min-height:18px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:99px; border:1px solid var(--line); }
  .high { background:#fdecea; color:#b71c1c; border-color:#f5c6c1; }
  .mid  { background:#fff8e1; color:#8d6e00; border-color:#ffe9a8; }
  .low  { background:#eef7ee; color:#2e6b31; border-color:#cfe6cf; }
  footer { color:var(--muted); font-size:11px; text-align:center; margin-top:8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>불법사이트 URL 수집 봇</h1>
  <div class="sub" id="host"></div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div>
        <div class="badge"><span class="dot" id="dot"></span><span id="stateText">확인 중…</span></div>
        <div class="sub" style="margin:6px 0 0" id="phase"></div>
      </div>
      <div class="row">
        <button class="primary" id="btnOn">수집 ON</button>
        <button class="danger"  id="btnOff">수집 OFF</button>
        <button id="btnRun">지금 1회 수집</button>
        <button id="btnExport">엑셀 다시 만들기</button>
      </div>
    </div>
    <div class="msg" id="msg"></div>
  </div>

  <div class="grid" id="stats"></div>

  <div class="card">
    <h1 style="font-size:15px">실행 상태</h1>
    <div class="kv" id="runtime"></div>
  </div>

  <div class="card">
    <h1 style="font-size:15px">최근 발견 (최신 20건)</h1>
    <table>
      <thead><tr><th>URL</th><th>카테고리</th><th>위험도</th><th>점수</th><th>최초 발견</th></tr></thead>
      <tbody id="recent"><tr><td colspan="5">불러오는 중…</td></tr></tbody>
    </table>
  </div>

  <footer>이 화면은 이 PC 에서만 열립니다. 새로고침 없이 5초마다 갱신됩니다.</footer>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function riskClass(r) { return r === "높음" ? "high" : r === "보통" ? "mid" : "low"; }

async function api(path, method) {
  const url = path + (TOKEN ? (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : "");
  const res = await fetch(url, { method: method || "GET", headers: TOKEN ? { "X-Bot-Token": TOKEN } : {} });
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

async function act(path, label) {
  $("msg").textContent = label + " 요청 중…";
  try {
    const data = await api(path, "POST");
    $("msg").textContent = data.message || (label + " 완료");
    await refresh();
  } catch (err) {
    $("msg").textContent = label + " 실패: " + err.message;
  }
}

function render(d) {
  $("host").textContent = `${d.host} · DB ${d.database} · 엑셀 ${d.export_path}`;
  const running = d.process_alive;
  const on = d.enabled;
  $("dot").className = "dot " + (!running ? "dead" : on ? "on" : "off");
  $("stateText").textContent = !running
    ? "봇 프로세스 정지"
    : on ? "수집 ON" : "수집 OFF (프로세스는 실행 중)";
  $("phase").textContent = running
    ? `${d.phase || "-"}${d.next_run_text ? " · 다음 수집 " + d.next_run_text : ""}`
    : "`python bot.py start` 로 봇을 실행하세요.";
  $("btnOn").disabled = on && running;
  $("btnOff").disabled = !on && running;
  $("btnRun").disabled = !running;

  $("stats").innerHTML = d.stats.map(s =>
    `<div class="stat"><b>${esc(s.value)}</b><span>${esc(s.label)}</span></div>`).join("");
  $("runtime").innerHTML = d.runtime.map(([k, v]) =>
    `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("");
  $("recent").innerHTML = d.recent.length
    ? d.recent.map(r => `<tr>
        <td class="url">${esc(r.url)}</td>
        <td>${esc(r.category)}</td>
        <td><span class="pill ${riskClass(r.risk)}">${esc(r.risk)}</span></td>
        <td>${esc(r.score)}</td>
        <td>${esc(r.first_seen)}</td></tr>`).join("")
    : `<tr><td colspan="5">아직 수집된 항목이 없습니다.</td></tr>`;
}

async function refresh() {
  try { render(await api("/api/status")); }
  catch (err) { $("msg").textContent = "상태를 읽지 못했습니다: " + err.message; }
}

$("btnOn").onclick     = () => act("/api/on", "수집 ON");
$("btnOff").onclick    = () => act("/api/off", "수집 OFF");
$("btnRun").onclick    = () => act("/api/run-once", "1회 수집");
$("btnExport").onclick = () => act("/api/export", "엑셀 재생성");
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class DashboardState:
    """핸들러가 참조하는 공유 객체."""

    def __init__(
        self, config: Config, control: Control, storage: Storage, classifier: Classifier
    ) -> None:
        self.config = config
        self.control = control
        self.storage = storage
        self.classifier = classifier


def _status_payload(state: DashboardState) -> dict[str, Any]:
    config = state.config
    control = state.control
    storage = state.storage

    bot_state = control.read_state()
    process = control.running_process()
    heartbeat = control.read_heartbeat()
    summary = storage.summary(config.export.min_score)
    last_cycle = heartbeat.get("last_cycle") or {}

    next_run_at = heartbeat.get("next_run_at") or ""
    next_run_text = ""
    if next_run_at:
        parsed = parse_iso(next_run_at)
        if parsed:
            remaining = (parsed - now_utc()).total_seconds()
            next_run_text = f"{local_str(next_run_at)} ({humanize_duration(max(0, remaining))} 후)"

    recent_rows = storage.sites_first_seen_since(days_ago(30), config.export.min_score)[:20]
    recent = [
        {
            "url": row["url"],
            "category": row["category_label"],
            "risk": state.classifier.risk_label(int(row["score"])),
            "score": int(row["score"]),
            "first_seen": local_str(row["first_seen_at"]),
        }
        for row in recent_rows
        if row["category"] != CONTACT_CATEGORY
    ]

    uptime = ""
    if process is not None:
        started = parse_iso(process.started_at)
        if started:
            uptime = humanize_duration((now_utc() - started).total_seconds())

    return {
        "enabled": bot_state.enabled,
        "process_alive": process is not None,
        "phase": heartbeat.get("phase") if process is not None else "정지",
        "next_run_text": next_run_text,
        "host": process.host if process else "-",
        "database": str(config.database_path),
        "export_path": str(config.export_path),
        "stats": [
            {"label": "수집된 불법사이트", "value": int(summary.get("total") or 0)},
            {"label": "최근 24시간 신규", "value": int(summary.get("new_24h") or 0)},
            {"label": "최근 7일 신규", "value": int(summary.get("new_7d") or 0)},
            {"label": "생존 확인", "value": int(summary.get("alive") or 0)},
            {"label": "접속 불가", "value": int(summary.get("dead") or 0)},
            {
                "label": "홍보사이트(사용/등록)",
                "value": f"{summary.get('sources_enabled', 0)}/{summary.get('sources_total', 0)}",
            },
        ],
        "runtime": [
            ["프로세스", f"PID {process.pid} (가동 {uptime})" if process else "실행 중이 아님"],
            ["수집 상태", "ON" if bot_state.enabled else "OFF"],
            ["상태 변경", f"{local_str(bot_state.updated_at, '-')} ({bot_state.updated_by or '-'})"],
            ["수집 주기", f"{config.crawl.interval_seconds}초 (+ 지연 최대 {config.crawl.jitter_seconds}초)"],
            ["완료한 사이클", heartbeat.get("cycles_completed", 0)],
            [
                "마지막 사이클",
                f"신규 {last_cycle.get('new_sites', 0)}건 / 갱신 {last_cycle.get('updated_sites', 0)}건 "
                f"/ 대상 {last_cycle.get('sources_ok', 0)}곳 성공, {last_cycle.get('sources_failed', 0)}곳 실패 "
                f"({humanize_duration(last_cycle.get('duration_seconds'))})",
            ],
            ["마지막 엑셀", last_cycle.get("export_path") or "-"],
            ["마지막 오류", last_cycle.get("error") or "없음"],
        ],
        "recent": recent,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "IllegalSiteBotDashboard/1.0"
    state: DashboardState  # 서버 생성 시 주입

    # -- 유틸 --------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # 접근 로그는 DEBUG 로만
        log.debug("dashboard %s - %s", self.address_string(), fmt % args)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.state.config.dashboard.token
        if not token:
            return True
        supplied = self.headers.get("X-Bot-Token") or (query.get("token") or [""])[0]
        return supplied == token

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_text(self, text: str, status: HTTPStatus) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    # -- 라우팅 ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (표준 라이브러리 규약)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)

        if parts.path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parts.path == "/api/status":
            if not self._authorized(query):
                self._send_text("토큰이 필요합니다.", HTTPStatus.UNAUTHORIZED)
                return
            try:
                self._send_json(_status_payload(self.state))
            except Exception as exc:
                log.exception("대시보드 상태 조회 실패")
                self._send_text(f"상태 조회 실패: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_text("찾을 수 없습니다.", HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._authorized(query):
            self._send_text("토큰이 필요합니다.", HTTPStatus.UNAUTHORIZED)
            return

        control = self.state.control
        actions = {
            "/api/on": lambda: (
                control.set_enabled(True, by="대시보드"),
                "수집을 ON 으로 바꿨습니다. 곧 수집을 시작합니다.",
            )[1],
            "/api/off": lambda: (
                control.set_enabled(False, by="대시보드"),
                "수집을 OFF 로 바꿨습니다. 진행 중인 사이클이 끝나면 멈춥니다.",
            )[1],
            "/api/run-once": lambda: (
                control.request_run_once("대시보드"),
                "1회 수집을 요청했습니다.",
            )[1],
            "/api/export": lambda: (
                control.request_export("대시보드"),
                "엑셀 재생성을 요청했습니다.",
            )[1],
        }

        action = actions.get(parts.path)
        if action is None:
            self._send_text("찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
            return

        try:
            message = action()
        except Exception as exc:
            log.exception("대시보드 조작 실패 %s", parts.path)
            self._send_text(f"처리 실패: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parts.path in {"/api/run-once", "/api/export"} and control.running_process() is None:
            message += " (봇 프로세스가 실행 중이 아니라 다음 시작 때 처리됩니다.)"
        self._send_json({"ok": True, "message": message})


def start_dashboard(
    config: Config, control: Control, storage: Storage, classifier: Classifier
) -> tuple[ThreadingHTTPServer, str]:
    """대시보드를 백그라운드 스레드로 띄우고 ``(서버, 주소)`` 를 돌려줍니다."""
    state = DashboardState(config, control, storage, classifier)
    handler = type("BoundHandler", (_Handler,), {"state": state})

    server = ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()

    host = config.dashboard.host
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{config.dashboard.port}/"
    if config.dashboard.token:
        url += f"?token={config.dashboard.token}"
    return server, url
