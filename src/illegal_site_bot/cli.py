"""명령줄 인터페이스.

    python bot.py init            처음 한 번: 설정 파일/폴더 준비
    python bot.py start           봇 실행 (24시간 상주)
    python bot.py on / off        수집 ON / OFF  (프로세스는 계속 살아있음)
    python bot.py status          현재 상태 보기
    python bot.py stop            봇 프로세스 정상 종료
    python bot.py run-once        지금 한 바퀴만 수집
    python bot.py export          엑셀 다시 만들기
    python bot.py add-source URL  홍보사이트 추가
    python bot.py preview URL     저장하지 않고 추출 결과만 확인 (규칙 튜닝용)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .classifier import Classifier, load_rules
from .config import Config, ConfigError, load_config
from .control import Control
from .logging_setup import setup_logging
from .normalizer import normalize_url
from .storage import SITE_STATUSES, Storage
from .timeutil import humanize_duration, local_str, now_utc, parse_iso

EXIT_OK = 0
EXIT_ERROR = 1


# --------------------------------------------------------------------------
# 공통 유틸
# --------------------------------------------------------------------------
def _control(config: Config) -> Control:
    return Control(config.state_dir, config.runtime.start_enabled)


def _print_kv(pairs: list[tuple[str, object]]) -> None:
    width = max((len(key) for key, _ in pairs), default=0)
    for key, value in pairs:
        print(f"  {key.ljust(width)} : {value}")


def _require_url(raw: str) -> str:
    normalized = normalize_url(raw)
    if not normalized:
        raise SystemExit(f"주소 형식이 올바르지 않습니다: {raw}")
    return normalized


# --------------------------------------------------------------------------
# 명령 구현
# --------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace, config: Config) -> int:
    """설정 파일과 폴더를 준비합니다."""
    created: list[str] = []
    config_dir = config.root / "config"
    for example, target in (
        ("config.example.yaml", "config.yaml"),
        ("targets.example.yaml", "targets.yaml"),
    ):
        source_path = config_dir / example
        target_path = config_dir / target
        if not source_path.is_file():
            continue
        if target_path.exists():
            print(f"  이미 있음: config/{target}")
            continue
        shutil.copy2(source_path, target_path)
        created.append(f"config/{target}")

    for directory in (config.export_dir, config.log_dir, config.state_dir, config.database_path.parent):
        directory.mkdir(parents=True, exist_ok=True)

    # DB 스키마를 미리 만들어 둡니다.
    Storage(config.database_path).close()

    print("준비 완료.")
    if created:
        print("  생성한 파일: " + ", ".join(created))
    print()
    print("다음 순서로 진행하세요:")
    print("  1) config/targets.yaml 에 수집할 홍보사이트 주소를 넣고 enabled: true 로 바꾸기")
    print("     (또는 python bot.py add-source https://... --name \"이름\")")
    print("  2) python bot.py import-targets   # targets.yaml 을 DB 에 반영")
    print("  3) python bot.py start            # 봇 실행")
    return EXIT_OK


def cmd_start(args: argparse.Namespace, config: Config) -> int:
    """봇을 실행합니다."""
    from .daemon import AlreadyRunningError, BotDaemon

    control = _control(config)
    existing = control.running_process()
    if existing is not None:
        print(f"이미 실행 중입니다. PID {existing.pid} (시작 {local_str(existing.started_at)})")
        if existing.dashboard_url:
            print(f"관리 화면: {existing.dashboard_url}")
        return EXIT_ERROR

    if args.detach:
        return _start_detached(config)

    setup_logging(
        config.log_dir,
        level=config.logging.level,
        retention_days=config.logging.retention_days,
        console=True,
    )
    try:
        daemon = BotDaemon(config, dashboard_enabled=not args.no_dashboard)
    except Exception as exc:
        print(f"봇을 시작할 수 없습니다: {exc}")
        return EXIT_ERROR

    try:
        return daemon.run()
    except AlreadyRunningError as exc:
        print(str(exc))
        return EXIT_ERROR


def _start_detached(config: Config) -> int:
    """백그라운드 프로세스로 띄웁니다."""
    entry = config.root / "bot.py"
    command = [sys.executable, str(entry), "start"]
    log_path = config.log_dir
    log_path.mkdir(parents=True, exist_ok=True)
    stdout_path = log_path / "start-detached.log"

    with stdout_path.open("a", encoding="utf-8") as handle:
        if sys.platform == "win32":  # pragma: no cover - Windows 전용
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
            subprocess.Popen(
                command,
                cwd=str(config.root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                command,
                cwd=str(config.root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )

    control = _control(config)
    for _ in range(40):  # 최대 20초 대기
        time.sleep(0.5)
        process = control.running_process()
        if process is not None:
            print(f"백그라운드로 실행했습니다. PID {process.pid}")
            if process.dashboard_url:
                print(f"관리 화면: {process.dashboard_url}")
            print(f"로그: {config.log_dir / 'bot.log'}")
            return EXIT_OK
    print(f"실행을 확인하지 못했습니다. 로그를 확인하세요: {stdout_path}")
    return EXIT_ERROR


def cmd_stop(args: argparse.Namespace, config: Config) -> int:
    """봇 프로세스를 정상 종료합니다."""
    control = _control(config)
    process = control.running_process()
    if process is None:
        print("실행 중인 봇이 없습니다.")
        control.clear_stop()
        return EXIT_OK

    print(f"종료를 요청했습니다 (PID {process.pid}). 진행 중인 작업을 정리하는 중…")
    control.request_stop("cli stop")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if control.running_process() is None:
            print("정상 종료했습니다.")
            return EXIT_OK
        time.sleep(0.5)

    print(
        f"{args.timeout}초 안에 종료되지 않았습니다. 수집 중인 요청이 끝나기를 기다리는 중일 수 있습니다.\n"
        f"계속 멈춰 있으면 프로세스를 직접 종료하세요 (PID {process.pid})."
    )
    return EXIT_ERROR


def cmd_on(args: argparse.Namespace, config: Config) -> int:
    control = _control(config)
    state = control.set_enabled(True, by="cli")
    print(f"수집을 ON 으로 바꿨습니다. ({local_str(state.updated_at)})")
    if control.running_process() is None:
        print("아직 봇 프로세스가 없습니다. `python bot.py start` 로 실행하세요.")
    else:
        print("실행 중인 봇이 곧 수집을 시작합니다.")
    return EXIT_OK


def cmd_off(args: argparse.Namespace, config: Config) -> int:
    control = _control(config)
    state = control.set_enabled(False, by="cli")
    print(f"수집을 OFF 로 바꿨습니다. ({local_str(state.updated_at)})")
    print("봇 프로세스는 그대로 살아있고, 진행 중인 사이클이 끝나면 멈춥니다.")
    print("프로세스까지 끄려면 `python bot.py stop` 을 쓰세요.")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    control = _control(config)
    storage = Storage(config.database_path)
    try:
        state = control.read_state()
        process = control.running_process()
        heartbeat = control.read_heartbeat()
        summary = storage.summary(config.export.min_score)
        last_run = storage.last_run()

        if args.json:
            print(
                json.dumps(
                    {
                        "enabled": state.enabled,
                        "process": {
                            "running": process is not None,
                            "pid": process.pid if process else None,
                            "started_at": process.started_at if process else None,
                            "dashboard_url": process.dashboard_url if process else None,
                        },
                        "heartbeat": heartbeat,
                        "summary": summary,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_OK

        uptime = ""
        if process is not None:
            started = parse_iso(process.started_at)
            if started:
                uptime = humanize_duration((now_utc() - started).total_seconds())

        print("[ 봇 상태 ]")
        rows: list[tuple[str, object]] = [
            ("프로세스", f"실행 중 (PID {process.pid}, 가동 {uptime})" if process else "정지"),
            ("수집 상태", "ON" if state.enabled else "OFF"),
            (
                "상태 변경",
                f"{local_str(state.updated_at, '-')} ({state.updated_by or '-'})",
            ),
        ]
        if process is not None:
            next_run = heartbeat.get("next_run_at") or ""
            remaining = ""
            parsed = parse_iso(next_run) if next_run else None
            if parsed:
                remaining = f" ({humanize_duration(max(0, (parsed - now_utc()).total_seconds()))} 후)"
            rows.extend(
                [
                    ("현재 작업", heartbeat.get("phase") or "-"),
                    ("다음 수집", f"{local_str(next_run, '-')}{remaining}"),
                    ("완료 사이클", heartbeat.get("cycles_completed", 0)),
                ]
            )
            if process.dashboard_url:
                rows.append(("관리 화면", process.dashboard_url))
        _print_kv(rows)

        print("\n[ 수집 현황 ]")
        _print_kv(
            [
                ("불법사이트 총계", summary.get("total", 0)),
                (f"{config.export.min_score}점 이상", summary.get("scored", 0)),
                ("최근 24시간 신규", summary.get("new_24h", 0)),
                ("최근 7일 신규", summary.get("new_7d", 0)),
                ("생존 / 접속불가 / 미확인",
                 f"{summary.get('alive', 0)} / {summary.get('dead', 0)} / {summary.get('unchecked', 0)}"),
                ("신고완료 처리", summary.get("reported", 0)),
                ("홍보사이트 사용/등록",
                 f"{summary.get('sources_enabled', 0)} / {summary.get('sources_total', 0)}"
                 f" (자동 발견 {summary.get('sources_auto', 0)})"),
                ("검토 대기 후보",
                 f"{summary.get('candidates_pending', 0)}건"
                 f" (평가 대기 {summary.get('candidates_discovered', 0)}건)"),
            ]
        )

        if last_run is not None:
            duration = last_run["duration_ms"]
            print("\n[ 마지막 사이클 ]")
            _print_kv(
                [
                    ("시작", local_str(last_run["started_at"])),
                    ("종료", local_str(last_run["finished_at"], "진행 중")),
                    ("소요", humanize_duration(duration / 1000 if duration else None)),
                    ("대상 성공/실패",
                     f"{last_run['sources_ok']} / {last_run['sources_failed']} (전체 {last_run['sources_total']})"),
                    ("페이지 / 후보",
                     f"{last_run['pages_fetched']} / {last_run['candidates_found']}"),
                    ("신규 / 갱신", f"{last_run['new_sites']} / {last_run['updated_sites']}"),
                    ("비고", last_run["note"] or "-"),
                ]
            )

        print("\n[ 파일 ]")
        _print_kv(
            [
                ("엑셀", config.export_path),
                ("DB", config.database_path),
                ("로그", config.log_dir / "bot.log"),
                ("설정", config.source_path or "(기본값 사용 중 - python bot.py init 권장)"),
            ]
        )
        return EXIT_OK
    finally:
        storage.close()


def cmd_run_once(args: argparse.Namespace, config: Config) -> int:
    """한 바퀴만 수집합니다. 봇이 돌고 있으면 그 봇에게 요청합니다."""
    control = _control(config)
    if control.running_process() is not None:
        control.request_run_once("cli run-once")
        print("실행 중인 봇에게 1회 수집을 요청했습니다. 진행 상황은 로그를 확인하세요.")
        return EXIT_OK

    from .daemon import BotDaemon

    setup_logging(
        config.log_dir,
        level=config.logging.level,
        retention_days=config.logging.retention_days,
        console=True,
    )
    daemon = BotDaemon(config, dashboard_enabled=False)
    summary = daemon.run_single_cycle()
    print(
        f"\n완료: 신규 {summary.new_sites}건, 갱신 {summary.updated_sites}건, "
        f"페이지 {summary.pages_fetched}개, {humanize_duration(summary.duration_seconds)} 소요"
    )
    if summary.export_path:
        print(f"엑셀: {summary.export_path}")
    if summary.error:
        print(f"오류: {summary.error}")
        return EXIT_ERROR
    return EXIT_OK


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """엑셀을 다시 만듭니다."""
    control = _control(config)
    if control.running_process() is not None and not args.local:
        control.request_export("cli export")
        print("실행 중인 봇에게 엑셀 재생성을 요청했습니다.")
        return EXIT_OK

    from .exporter import Exporter

    storage = Storage(config.database_path)
    try:
        classifier = Classifier(load_rules(config.root / "config" / "rules.yaml"))
        result = Exporter(config, storage, classifier).export()
        print(f"엑셀을 만들었습니다: {result.path}")
        print(f"  수록 {result.rows}건 / 최근 24시간 신규 {result.new_rows}건 / 연락채널 {result.contacts}건")
        if result.snapshot:
            print(f"  일별 스냅샷: {result.snapshot}")
        if result.warning:
            print(f"  주의: {result.warning}")
        return EXIT_OK
    finally:
        storage.close()


def cmd_add_source(args: argparse.Namespace, config: Config) -> int:
    storage = Storage(config.database_path)
    try:
        added = 0
        for raw in args.urls:
            url = _require_url(raw)
            storage.upsert_source(
                url=url,
                name=args.name or "",
                enabled=not args.disabled,
                render=args.render,
                max_pages=args.max_pages,
                note=args.note or "",
            )
            print(f"등록: {url}{'  (사용 안 함)' if args.disabled else ''}")
            added += 1
        print(f"총 {added}곳을 등록/갱신했습니다.")
        return EXIT_OK
    finally:
        storage.close()


def cmd_list_sources(args: argparse.Namespace, config: Config) -> int:
    storage = Storage(config.database_path)
    try:
        rows = [row for row in storage.source_rows() if row["state"] == "approved"]
        if not rows:
            print("등록된 홍보사이트가 없습니다. `python bot.py add-source URL` 로 추가하세요.")
            return EXIT_OK
        print(f"{'사용':<5}{'출처':<7}{'깊이':<5}{'연속실패':<9}{'누적':<7}{'마지막 수집':<21}URL")
        for row in rows:
            print(
                f"{'ON' if row['enabled'] else 'OFF':<5}"
                f"{'자동' if row['origin'] == 'auto' else '수동':<7}"
                f"{row['depth']:<5}"
                f"{row['consecutive_failures']:<9}"
                f"{row['found_total']:<7}"
                f"{local_str(row['last_crawled_at'], '-'):<21}"
                f"{row['url']}"
                + (f"  ({row['name']})" if row["name"] else "")
            )

        waiting = storage.count_sources(state="pending") + storage.count_sources(
            state="discovered"
        )
        if waiting:
            print(f"\n검토 대기 중인 후보 {waiting}건이 있습니다: python bot.py candidates")
        return EXIT_OK
    finally:
        storage.close()


def cmd_source_toggle(args: argparse.Namespace, config: Config) -> int:
    storage = Storage(config.database_path)
    try:
        url = _require_url(args.url)
        enabled = args.command == "enable-source"
        if storage.set_source_enabled(url, enabled):
            print(f"{'사용' if enabled else '사용 중지'}: {url}")
            return EXIT_OK
        print(f"등록되지 않은 주소입니다: {url}")
        return EXIT_ERROR
    finally:
        storage.close()


def cmd_remove_source(args: argparse.Namespace, config: Config) -> int:
    storage = Storage(config.database_path)
    try:
        url = _require_url(args.url)
        if storage.remove_source(url):
            print(f"삭제했습니다: {url}")
            return EXIT_OK
        print(f"등록되지 않은 주소입니다: {url}")
        return EXIT_ERROR
    finally:
        storage.close()


def cmd_import_targets(args: argparse.Namespace, config: Config) -> int:
    from .targets import load_targets

    targets_path = config.root / "config" / "targets.yaml"
    targets = load_targets(targets_path)
    if not targets.sources:
        print(f"가져올 대상이 없습니다: {targets_path}")
        print("targets.example.yaml 을 targets.yaml 로 복사한 뒤 주소를 채우세요.")
        return EXIT_ERROR

    storage = Storage(config.database_path)
    try:
        for spec in targets.sources:
            storage.upsert_source(
                url=spec.url,
                name=spec.name,
                enabled=spec.enabled,
                render=spec.render,
                max_pages=spec.max_pages,
                note=spec.note,
            )
        enabled = sum(1 for spec in targets.sources if spec.enabled)
        print(f"{len(targets.sources)}곳을 반영했습니다 (사용 {enabled}곳).")
        return EXIT_OK
    finally:
        storage.close()


def cmd_candidates(args: argparse.Namespace, config: Config) -> int:
    """봇이 찾아낸 홍보사이트 후보 목록."""
    storage = Storage(config.database_path)
    try:
        states = (args.state,) if args.state else ("pending", "discovered")
        rows = storage.sources_by_state(*states)
        if not rows:
            print("대기 중인 후보가 없습니다.")
            print("봇이 수집을 돌면서 다른 홍보사이트를 발견하면 여기에 쌓입니다.")
            return EXIT_OK

        print(f"{'점수':<6}{'상태':<12}{'깊이':<6}URL")
        print("-" * 78)
        for row in rows:
            state_label = {
                "discovered": "평가 대기",
                "pending": "승인 대기",
                "rejected": "기각",
                "auto_disabled": "자동 중지",
                "approved": "승인됨",
            }.get(row["state"], row["state"])
            print(f"{row['promo_score']:<8}{state_label:<12}{row['depth']:<6}{row['url']}")
            if row["name"]:
                print(f"{'':<26}{row['name']}")
            if row["promo_reasons"]:
                print(f"{'':<26}└ {row['promo_reasons']}")
            if row["discovered_from_id"]:
                origin_row = storage.source_by_id(int(row["discovered_from_id"]))
                if origin_row is not None:
                    print(f"{'':<26}← 발견 경로: {origin_row['url']}")
        print("-" * 78)
        print(f"총 {len(rows)}건")
        print("승인: python bot.py approve <URL>   /   기각: python bot.py reject <URL>")
        return EXIT_OK
    finally:
        storage.close()


def cmd_review(args: argparse.Namespace, config: Config) -> int:
    """후보를 승인하거나 기각합니다."""
    from .normalizer import registrable_domain

    storage = Storage(config.database_path)
    try:
        url = _require_url(args.url)
        approving = args.command == "approve"
        state = "approved" if approving else "rejected"
        if not storage.review_source(url, state, by="cli", note=args.note or ""):
            print(f"후보 목록에 없는 주소입니다: {url}")
            print("`python bot.py candidates` 로 목록을 확인하세요.")
            return EXIT_ERROR

        if approving:
            moved = storage.reclassify_site_as_promo(registrable_domain(url))
            print(f"승인했습니다. 다음 사이클부터 수집합니다: {url}")
            if moved:
                print(f"  불법사이트 목록에 있던 {moved}건을 홍보사이트로 재분류했습니다.")
        else:
            print(f"기각했습니다: {url}")
            days = config.discovery.reevaluate_after_days
            if days > 0:
                print(f"  {days}일 뒤 다시 평가 대상이 됩니다.")
        return EXIT_OK
    finally:
        storage.close()


def cmd_evaluate(args: argparse.Namespace, config: Config) -> int:
    """저장하지 않고 홍보사이트 판별 점수만 계산합니다 (2단계 평가 미리보기)."""
    from .discovery import Discovery
    from .fetcher import Fetcher

    url = _require_url(args.url)
    storage = Storage(config.database_path)
    try:
        classifier = Classifier(load_rules(config.root / "config" / "rules.yaml"))
        with Fetcher(config.crawl) as fetcher:
            discovery = Discovery(config, storage, classifier, fetcher)
            print(f"평가 중: {url}")
            evaluation = discovery.evaluate_url(url)

        if not evaluation.ok:
            print(f"가져오지 못했습니다: {evaluation.error}")
            return EXIT_ERROR

        settings = config.discovery
        if evaluation.score >= settings.auto_approve_score:
            verdict = f"자동 승인 기준({settings.auto_approve_score}점) 이상"
        elif evaluation.score >= settings.queue_score:
            verdict = f"승인 대기 기준({settings.queue_score}점) 이상"
        else:
            verdict = f"기준 미달 ({settings.queue_score}점 미만) - 홍보사이트가 아닌 것으로 판단"

        print()
        _print_kv(
            [
                ("제목", evaluation.title or "-"),
                ("홍보사이트 점수", f"{evaluation.score} / 100  → {verdict}"),
                ("이미 아는 불법 도메인", f"{evaluation.known_illegal}곳"),
                ("외부 도메인", f"{evaluation.outbound_domains}개"),
                ("배너 링크 비율", f"{evaluation.banner_ratio:.0%}"),
                ("로그인 랜딩 페이지", "예 (감점)" if evaluation.landing_page else "아니오"),
            ]
        )
        print("\n[ 판별 근거 ]")
        for reason in evaluation.reasons or ["(없음)"]:
            print(f"  - {reason}")

        if args.add and evaluation.score >= settings.queue_score:
            storage.add_candidate(url, discovered_from_id=None, depth=0, note="수동 평가로 추가")
            storage.review_source(url, "pending", by="cli")
            print(f"\n후보 목록에 추가했습니다. 승인하려면: python bot.py approve {url}")
        return EXIT_OK
    finally:
        storage.close()


def cmd_mark(args: argparse.Namespace, config: Config) -> int:
    """사이트의 처리 상태/메모를 기록합니다 (엑셀에도 그대로 표시됩니다)."""
    storage = Storage(config.database_path)
    try:
        url = _require_url(args.url)
        if storage.set_site_status(url, args.status, args.memo):
            print(f"기록했습니다: {url} → {args.status}")
            return EXIT_OK
        print(f"수집 목록에 없는 주소입니다: {url}")
        return EXIT_ERROR
    finally:
        storage.close()


def cmd_preview(args: argparse.Namespace, config: Config) -> int:
    """저장하지 않고 추출/판별 결과만 보여줍니다. 규칙을 다듬을 때 씁니다."""
    from .extractor import extract
    from .fetcher import Fetcher
    from .targets import load_targets

    url = _require_url(args.url)
    classifier = Classifier(load_rules(config.root / "config" / "rules.yaml"))
    targets = load_targets(config.root / "config" / "targets.yaml")

    print(f"수집 중: {url}")
    with Fetcher(config.crawl) as fetcher:
        result = fetcher.fetch(url)
        if not result.ok:
            print(f"가져오지 못했습니다: {result.error or result.status}")
            return EXIT_ERROR
        print(f"  HTTP {result.status}, {len(result.html):,}자, {result.elapsed_ms}ms")

        extracted = extract(result.html, result.final_url or url, targets.pagination_patterns)
        print(f"  제목: {extracted.title or '-'}")
        print(f"  외부 링크 후보 {len(extracted.candidates)}건, 내부 페이지 {len(extracted.internal_links)}건\n")

        kept: list[tuple[int, str, str, str, str]] = []
        excluded = 0
        below = 0
        for candidate in extracted.candidates:
            verdict = classifier.classify(
                candidate.url, candidate.anchor_text, candidate.context_text
            )
            if verdict.excluded:
                excluded += 1
                continue
            if verdict.score < classifier.rules.candidate_threshold and not verdict.always_keep:
                below += 1
                if not args.all:
                    continue
            kept.append(
                (
                    verdict.score,
                    verdict.label,
                    candidate.url,
                    candidate.method,
                    verdict.matched_keywords_text or verdict.reasons_text,
                )
            )

        kept.sort(reverse=True)
        print(f"{'점수':<6}{'카테고리':<12}{'방법':<12}URL / 근거")
        for score, label, candidate_url, method, why in kept:
            print(f"{score:<6}{label:<12}{method:<12}{candidate_url}")
            if why:
                print(f"{'':<30}└ {why}")
        print(
            f"\n제외 {excluded}건, 기준({classifier.rules.candidate_threshold}점) 미달 {below}건"
            + ("" if args.all else " (--all 로 함께 볼 수 있습니다)")
        )
    return EXIT_OK


# --------------------------------------------------------------------------
# 파서
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="불법홍보사이트에서 홍보되는 불법사이트 URL을 수집해 엑셀로 정리하는 봇",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config", metavar="PATH", help="설정 파일 경로 (기본: config/config.yaml)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="명령")

    subparsers.add_parser("init", help="설정 파일과 폴더를 준비합니다").set_defaults(func=cmd_init)

    start = subparsers.add_parser("start", help="봇을 실행합니다 (24시간 상주)")
    start.add_argument("--detach", action="store_true", help="백그라운드로 실행")
    start.add_argument("--no-dashboard", action="store_true", help="관리 화면 없이 실행")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", help="봇 프로세스를 정상 종료합니다")
    stop.add_argument("--timeout", type=int, default=60, help="종료 대기 시간(초, 기본 60)")
    stop.set_defaults(func=cmd_stop)

    for name, help_text, func in (
        ("on", "수집을 ON 으로 (프로세스는 그대로)", cmd_on),
        ("resume", "on 과 동일", cmd_on),
        ("off", "수집을 OFF 로 (프로세스는 그대로)", cmd_off),
        ("pause", "off 와 동일", cmd_off),
    ):
        subparsers.add_parser(name, help=help_text).set_defaults(func=func)

    status = subparsers.add_parser("status", help="현재 상태를 봅니다")
    status.add_argument("--json", action="store_true", help="JSON 으로 출력")
    status.set_defaults(func=cmd_status)

    subparsers.add_parser("run-once", help="지금 한 바퀴만 수집합니다").set_defaults(
        func=cmd_run_once
    )

    export = subparsers.add_parser("export", help="엑셀을 다시 만듭니다")
    export.add_argument(
        "--local",
        action="store_true",
        help="봇이 돌고 있어도 이 명령이 직접 엑셀을 만듭니다",
    )
    export.set_defaults(func=cmd_export)

    add_source = subparsers.add_parser("add-source", help="홍보사이트를 추가합니다")
    add_source.add_argument("urls", nargs="+", metavar="URL")
    add_source.add_argument("--name", help="표시 이름")
    add_source.add_argument("--note", help="비고")
    add_source.add_argument("--render", action="store_true", help="자바스크립트 렌더링 사용")
    add_source.add_argument("--max-pages", type=int, help="이 사이트에서 따라갈 페이지 수")
    add_source.add_argument("--disabled", action="store_true", help="등록만 하고 수집은 하지 않음")
    add_source.set_defaults(func=cmd_add_source)

    subparsers.add_parser("list-sources", help="홍보사이트 목록을 봅니다").set_defaults(
        func=cmd_list_sources
    )

    for name, help_text in (
        ("enable-source", "홍보사이트를 수집 대상으로 켭니다"),
        ("disable-source", "홍보사이트를 수집 대상에서 끕니다"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("url", metavar="URL")
        sub.set_defaults(func=cmd_source_toggle)

    remove = subparsers.add_parser("remove-source", help="홍보사이트를 목록에서 지웁니다")
    remove.add_argument("url", metavar="URL")
    remove.set_defaults(func=cmd_remove_source)

    subparsers.add_parser(
        "import-targets", help="config/targets.yaml 을 DB 에 반영합니다"
    ).set_defaults(func=cmd_import_targets)

    candidates = subparsers.add_parser(
        "candidates", help="봇이 찾아낸 홍보사이트 후보를 봅니다"
    )
    candidates.add_argument(
        "--state",
        choices=("discovered", "pending", "rejected", "approved", "auto_disabled"),
        help="특정 상태만 보기 (기본: 평가 대기 + 승인 대기)",
    )
    candidates.set_defaults(func=cmd_candidates)

    for name, help_text in (
        ("approve", "홍보사이트 후보를 승인합니다 (다음 사이클부터 수집)"),
        ("reject", "홍보사이트 후보를 기각합니다"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("url", metavar="URL")
        sub.add_argument("--note", help="비고")
        sub.set_defaults(func=cmd_review)

    evaluate = subparsers.add_parser(
        "evaluate", help="어떤 주소가 홍보사이트인지 점수만 계산합니다 (저장 안 함)"
    )
    evaluate.add_argument("url", metavar="URL")
    evaluate.add_argument(
        "--add", action="store_true", help="기준을 넘으면 후보 목록에도 추가"
    )
    evaluate.set_defaults(func=cmd_evaluate)

    mark = subparsers.add_parser("mark", help="사이트의 처리 상태/메모를 기록합니다")
    mark.add_argument("url", metavar="URL")
    mark.add_argument(
        "--status", choices=SITE_STATUSES, default="confirmed", help="처리 상태"
    )
    mark.add_argument("--memo", help="메모 (엑셀에 함께 표시됩니다)")
    mark.set_defaults(func=cmd_mark)

    preview = subparsers.add_parser(
        "preview", help="저장하지 않고 추출/판별 결과만 확인합니다 (규칙 튜닝용)"
    )
    preview.add_argument("url", metavar="URL")
    preview.add_argument("--all", action="store_true", help="기준 미달 후보도 함께 표시")
    preview.set_defaults(func=cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return int(args.func(args, config) or EXIT_OK)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return EXIT_ERROR
    except BrokenPipeError:
        # `python bot.py status | head` 처럼 출력이 중간에 끊긴 경우 (오류가 아님)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except SystemExit:
        raise
    except Exception as exc:  # 사용자에게는 읽을 수 있는 메시지로
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.command in {"start", "run-once"}:
            print(f"자세한 내용은 로그를 확인하세요: {Path(config.log_dir) / 'bot.log'}", file=sys.stderr)
        return EXIT_ERROR
