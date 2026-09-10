"""수집 결과를 엑셀(.xlsx) 로 정리합니다.

엑셀은 매번 새로 만드는 '보고서'입니다. 사람이 엑셀에 직접 적은 메모는
다음 출력 때 사라지므로, 처리 상태나 메모는 ``python bot.py mark`` 로
DB 에 남겨야 합니다(그러면 엑셀에도 계속 따라옵니다).

시트 구성
    요약              한눈에 보는 수치
    불법사이트목록     수집된 URL 전체 (핵심 시트)
    신규_최근24시간    지난 24시간에 처음 발견된 것
    연락채널          텔레그램/카톡 등 접촉 채널
    홍보사이트_수집원  어디를 돌고 있는지 + 마지막 결과
    실행이력          사이클별 통계
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .classifier import CONTACT_CATEGORY, Classifier
from .config import Config
from .storage import Storage
from .timeutil import days_ago, humanize_duration, local_str, now_utc

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14)
LABEL_FONT = Font(bold=True)
THIN_BORDER = Border(*(Side(style="thin", color="D9D9D9"),) * 4)

RISK_FILLS = {
    "높음": PatternFill("solid", fgColor="FFC7CE"),
    "보통": PatternFill("solid", fgColor="FFEB9C"),
    "낮음": PatternFill("solid", fgColor="E2EFDA"),
}

ALIVE_TEXT = {1: "생존", 0: "접속불가", None: "미확인"}
STATUS_TEXT = {
    "new": "신규",
    "confirmed": "확인",
    "reported": "신고완료",
    "ignored": "제외",
}

# (헤더, 너비)
SITE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("번호", 6),
    ("URL", 52),
    ("도메인", 26),
    ("카테고리", 12),
    ("위험도", 8),
    ("점수", 7),
    ("최초 발견", 18),
    ("최근 발견", 18),
    ("발견 횟수", 9),
    ("홍보사이트 수", 12),
    ("발견된 홍보사이트", 40),
    ("일치 키워드", 28),
    ("판별 근거", 26),
    ("경유 URL", 30),
    ("생존", 9),
    ("HTTP", 7),
    ("확인 시각", 18),
    ("처리 상태", 10),
    ("메모", 24),
)

SOURCE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("번호", 6),
    ("홍보사이트 URL", 50),
    ("이름", 22),
    ("사용", 7),
    ("렌더링", 8),
    ("최대 페이지", 11),
    ("마지막 수집", 18),
    ("마지막 상태", 12),
    ("연속 실패", 10),
    ("누적 수집", 10),
    ("마지막 오류", 40),
    ("비고", 24),
)

RUN_COLUMNS: tuple[tuple[str, int], ...] = (
    ("시작", 18),
    ("종료", 18),
    ("소요", 12),
    ("대상", 7),
    ("성공", 7),
    ("실패", 7),
    ("페이지", 8),
    ("후보", 8),
    ("신규", 8),
    ("갱신", 8),
    ("비고", 40),
)


@dataclass
class ExportResult:
    path: Path
    snapshot: Path | None
    rows: int
    new_rows: int
    contacts: int
    warning: str = ""


def _style_header(sheet: Worksheet, columns: Sequence[tuple[str, int]], row: int = 1) -> None:
    for index, (title, width) in enumerate(columns, start=1):
        cell = sheet.cell(row=row, column=index, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[row].height = 24
    sheet.freeze_panes = sheet.cell(row=row + 1, column=1)
    last_column = get_column_letter(len(columns))
    sheet.auto_filter.ref = f"A{row}:{last_column}{row}"


def _write_url(sheet: Worksheet, row: int, column: int, url: str, clickable: bool) -> None:
    cell = sheet.cell(row=row, column=column, value=url)
    cell.alignment = Alignment(vertical="center")
    if clickable and url:
        cell.hyperlink = url
        cell.font = Font(color="0563C1", underline="single")
    else:
        # 기본은 일반 텍스트입니다. 실수로 불법사이트에 접속하는 것을 막기 위함입니다.
        cell.font = Font(name="Consolas", size=10)


class Exporter:
    def __init__(self, config: Config, storage: Storage, classifier: Classifier) -> None:
        self.config = config
        self.storage = storage
        self.classifier = classifier

    # -- 시트별 작성 -------------------------------------------------------
    def _write_sites_sheet(
        self,
        sheet: Worksheet,
        rows: Sequence[sqlite3.Row],
        source_map: dict[int, list[str]],
    ) -> None:
        _style_header(sheet, SITE_COLUMNS)
        clickable = self.config.export.clickable_links

        for index, row in enumerate(rows, start=1):
            excel_row = index + 1
            risk = self.classifier.risk_label(int(row["score"]))
            sources = source_map.get(int(row["id"]), [])

            sheet.cell(row=excel_row, column=1, value=index).alignment = Alignment(
                horizontal="center"
            )
            _write_url(sheet, excel_row, 2, row["url"], clickable)
            sheet.cell(row=excel_row, column=3, value=row["domain"])
            sheet.cell(row=excel_row, column=4, value=row["category_label"])

            risk_cell = sheet.cell(row=excel_row, column=5, value=risk)
            risk_cell.alignment = Alignment(horizontal="center")
            if risk in RISK_FILLS:
                risk_cell.fill = RISK_FILLS[risk]

            sheet.cell(row=excel_row, column=6, value=int(row["score"])).alignment = Alignment(
                horizontal="center"
            )
            sheet.cell(row=excel_row, column=7, value=local_str(row["first_seen_at"]))
            sheet.cell(row=excel_row, column=8, value=local_str(row["last_seen_at"]))
            sheet.cell(row=excel_row, column=9, value=int(row["seen_count"]))
            sheet.cell(row=excel_row, column=10, value=int(row["distinct_sources"]))
            sheet.cell(row=excel_row, column=11, value="\n".join(sources)).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            sheet.cell(row=excel_row, column=12, value=row["matched_keywords"]).alignment = (
                Alignment(wrap_text=True, vertical="top")
            )
            sheet.cell(row=excel_row, column=13, value=row["reasons"]).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            sheet.cell(row=excel_row, column=14, value=row["redirect_from"])
            alive_cell = sheet.cell(
                row=excel_row, column=15, value=ALIVE_TEXT.get(row["alive"], "미확인")
            )
            alive_cell.alignment = Alignment(horizontal="center")
            sheet.cell(row=excel_row, column=16, value=row["http_status"]).alignment = Alignment(
                horizontal="center"
            )
            sheet.cell(row=excel_row, column=17, value=local_str(row["last_checked_at"]))
            sheet.cell(
                row=excel_row, column=18, value=STATUS_TEXT.get(row["status"], row["status"])
            ).alignment = Alignment(horizontal="center")
            sheet.cell(row=excel_row, column=19, value=row["memo"]).alignment = Alignment(
                wrap_text=True, vertical="top"
            )

        if rows:
            last = len(rows) + 1
            sheet.conditional_formatting.add(
                f"F2:F{last}",
                DataBarRule(
                    start_type="num",
                    start_value=0,
                    end_type="num",
                    end_value=100,
                    color="638EC6",
                    showValue=True,
                ),
            )

    def _write_sources_sheet(self, sheet: Worksheet, rows: Sequence[sqlite3.Row]) -> None:
        _style_header(sheet, SOURCE_COLUMNS)
        for index, row in enumerate(rows, start=1):
            excel_row = index + 1
            sheet.cell(row=excel_row, column=1, value=index).alignment = Alignment(
                horizontal="center"
            )
            _write_url(sheet, excel_row, 2, row["url"], False)
            sheet.cell(row=excel_row, column=3, value=row["name"])
            sheet.cell(
                row=excel_row, column=4, value="ON" if row["enabled"] else "OFF"
            ).alignment = Alignment(horizontal="center")
            sheet.cell(
                row=excel_row, column=5, value="예" if row["render"] else "-"
            ).alignment = Alignment(horizontal="center")
            sheet.cell(row=excel_row, column=6, value=row["max_pages"]).alignment = Alignment(
                horizontal="center"
            )
            sheet.cell(row=excel_row, column=7, value=local_str(row["last_crawled_at"]))
            sheet.cell(row=excel_row, column=8, value=row["last_status"])
            failures = int(row["consecutive_failures"])
            failure_cell = sheet.cell(row=excel_row, column=9, value=failures)
            failure_cell.alignment = Alignment(horizontal="center")
            if failures >= 3:
                failure_cell.fill = RISK_FILLS["높음"]
            sheet.cell(row=excel_row, column=10, value=int(row["found_total"]))
            sheet.cell(row=excel_row, column=11, value=row["last_error"]).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            sheet.cell(row=excel_row, column=12, value=row["note"])

    def _write_runs_sheet(self, sheet: Worksheet, rows: Sequence[sqlite3.Row]) -> None:
        _style_header(sheet, RUN_COLUMNS)
        for index, row in enumerate(rows, start=1):
            excel_row = index + 1
            duration = row["duration_ms"]
            sheet.cell(row=excel_row, column=1, value=local_str(row["started_at"]))
            sheet.cell(row=excel_row, column=2, value=local_str(row["finished_at"], "진행 중"))
            sheet.cell(
                row=excel_row,
                column=3,
                value=humanize_duration(duration / 1000 if duration else None),
            )
            for offset, key in enumerate(
                (
                    "sources_total",
                    "sources_ok",
                    "sources_failed",
                    "pages_fetched",
                    "candidates_found",
                    "new_sites",
                    "updated_sites",
                ),
                start=4,
            ):
                cell = sheet.cell(row=excel_row, column=offset, value=int(row[key] or 0))
                cell.alignment = Alignment(horizontal="center")
            sheet.cell(row=excel_row, column=11, value=row["note"])

    def _write_summary_sheet(
        self,
        sheet: Worksheet,
        summary: dict[str, Any],
        total_rows: int,
        new_rows: int,
        contacts: int,
    ) -> None:
        sheet.column_dimensions["A"].width = 26
        sheet.column_dimensions["B"].width = 30
        sheet.column_dimensions["C"].width = 14
        sheet.column_dimensions["D"].width = 12

        sheet["A1"] = "불법사이트 URL 수집 현황"
        sheet["A1"].font = TITLE_FONT
        sheet["A2"] = f"생성 시각: {local_str(now_utc())}"
        sheet["A2"].font = Font(color="808080", size=9)

        last_run = self.storage.last_run()
        rows: list[tuple[str, Any]] = [
            ("수집된 불법사이트(전체)", int(summary.get("total") or 0)),
            (f"엑셀 수록 기준({self.config.export.min_score}점 이상)", total_rows),
            ("최근 24시간 신규", int(summary.get("new_24h") or 0)),
            ("최근 7일 신규", int(summary.get("new_7d") or 0)),
            ("생존 확인", int(summary.get("alive") or 0)),
            ("접속 불가", int(summary.get("dead") or 0)),
            ("생존 미확인", int(summary.get("unchecked") or 0)),
            ("신고 완료 처리", int(summary.get("reported") or 0)),
            ("연락 채널(텔레그램 등)", contacts),
            ("홍보사이트 등록/사용", f"{summary.get('sources_total', 0)} / {summary.get('sources_enabled', 0)}"),
        ]
        if last_run is not None:
            duration = last_run["duration_ms"]
            rows.extend(
                [
                    ("마지막 사이클 시작", local_str(last_run["started_at"])),
                    ("마지막 사이클 종료", local_str(last_run["finished_at"], "진행 중")),
                    ("마지막 사이클 소요", humanize_duration(duration / 1000 if duration else None)),
                ]
            )

        row_index = 4
        for label, value in rows:
            label_cell = sheet.cell(row=row_index, column=1, value=label)
            label_cell.font = LABEL_FONT
            label_cell.border = THIN_BORDER
            value_cell = sheet.cell(row=row_index, column=2, value=value)
            value_cell.border = THIN_BORDER
            row_index += 1

        row_index += 1
        sheet.cell(row=row_index, column=1, value="카테고리별 집계").font = TITLE_FONT
        row_index += 1
        for header_index, header in enumerate(("카테고리", "건수"), start=1):
            cell = sheet.cell(row=row_index, column=header_index, value=header)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center")
        row_index += 1
        for entry in summary.get("by_category") or []:
            sheet.cell(row=row_index, column=1, value=entry.get("category_label") or entry.get("category"))
            sheet.cell(row=row_index, column=2, value=int(entry.get("n") or 0)).alignment = (
                Alignment(horizontal="center")
            )
            row_index += 1

        row_index += 1
        note = sheet.cell(
            row=row_index,
            column=1,
            value=(
                "※ 이 파일은 매 사이클마다 다시 생성됩니다. 엑셀에 직접 적은 메모는 사라지므로 "
                "처리 상태/메모는 `python bot.py mark <URL> --status reported --memo \"...\"` 로 "
                "남기세요(그러면 엑셀에도 계속 표시됩니다)."
            ),
        )
        note.font = Font(color="808080", size=9)
        note.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.merge_cells(start_row=row_index, start_column=1, end_row=row_index + 2, end_column=4)

    # -- 저장 --------------------------------------------------------------
    def _save_workbook(self, workbook: Workbook, target: Path) -> tuple[Path, str]:
        """임시 파일에 쓴 뒤 교체합니다. 엑셀이 파일을 잡고 있으면 다른 이름으로 저장."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".export-", suffix=".xlsx")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            workbook.save(temp_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        try:
            os.replace(temp_path, target)
            return target, ""
        except (PermissionError, OSError) as exc:
            # Windows 에서 엑셀로 파일을 열어둔 경우 교체가 막힙니다.
            fallback = target.with_name(
                f"{target.stem}_열려있어_임시저장_{datetime.now():%H%M%S}{target.suffix}"
            )
            try:
                os.replace(temp_path, fallback)
            except OSError:
                temp_path.unlink(missing_ok=True)
                raise
            warning = (
                f"{target.name} 이 열려 있어 덮어쓰지 못했습니다({exc.__class__.__name__}). "
                f"{fallback.name} 으로 저장했습니다. 엑셀을 닫고 다시 내보내세요."
            )
            log.warning(warning)
            return fallback, warning

    def _write_snapshot(self, source: Path) -> Path | None:
        if not self.config.export.keep_daily_snapshots:
            return None
        snapshot = source.with_name(f"{source.stem}_{datetime.now():%Y%m%d}{source.suffix}")
        try:
            shutil.copy2(source, snapshot)
        except OSError as exc:
            log.warning("일별 스냅샷 저장 실패: %s", exc)
            return None
        self._prune_snapshots(source)
        return snapshot

    def _prune_snapshots(self, source: Path) -> None:
        retention = self.config.export.snapshot_retention_days
        if retention <= 0:
            return
        cutoff = now_utc().timestamp() - retention * 86_400
        for path in source.parent.glob(f"{source.stem}_*{source.suffix}"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.debug("오래된 스냅샷 삭제: %s", path.name)
            except OSError:
                continue

    # -- 공개 API ----------------------------------------------------------
    def export(self) -> ExportResult:
        """현재 DB 내용을 엑셀 파일로 씁니다."""
        min_score = self.config.export.min_score
        sites = self.storage.sites_for_export(min_score, categories_excluded=(CONTACT_CATEGORY,))
        new_sites = [
            row
            for row in self.storage.sites_first_seen_since(days_ago(1), min_score)
            if row["category"] != CONTACT_CATEGORY
        ]
        contacts = self.storage.sites_by_category(CONTACT_CATEGORY)
        source_map = self.storage.source_urls_grouped()
        summary = self.storage.summary(min_score)

        workbook = Workbook()
        summary_sheet = workbook.active
        summary_sheet.title = "요약"
        self._write_summary_sheet(
            summary_sheet, summary, len(sites), len(new_sites), len(contacts)
        )

        self._write_sites_sheet(workbook.create_sheet("불법사이트목록"), sites, source_map)
        self._write_sites_sheet(workbook.create_sheet("신규_최근24시간"), new_sites, source_map)
        self._write_sites_sheet(workbook.create_sheet("연락채널"), contacts, source_map)
        self._write_sources_sheet(workbook.create_sheet("홍보사이트_수집원"), self.storage.source_rows())
        self._write_runs_sheet(workbook.create_sheet("실행이력"), self.storage.recent_runs(100))

        target = self.config.export_path
        saved_path, warning = self._save_workbook(workbook, target)
        snapshot = self._write_snapshot(saved_path) if not warning else None

        log.info(
            "엑셀 저장 완료: %s (총 %d건 / 신규 %d건 / 연락채널 %d건)",
            saved_path,
            len(sites),
            len(new_sites),
            len(contacts),
        )
        return ExportResult(
            path=saved_path,
            snapshot=snapshot,
            rows=len(sites),
            new_rows=len(new_sites),
            contacts=len(contacts),
            warning=warning,
        )
