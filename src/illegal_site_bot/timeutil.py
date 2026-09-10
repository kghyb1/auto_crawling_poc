"""시간 처리. 저장은 UTC, 표시는 로컬 시간으로 통일합니다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().strftime(ISO_FORMAT)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(value: datetime | str | None) -> datetime | None:
    """UTC 값(또는 ISO 문자열)을 이 PC의 로컬 시간으로 바꿉니다."""
    parsed = parse_iso(value) if isinstance(value, str) else value
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def local_str(value: datetime | str | None, default: str = "") -> str:
    local = to_local(value)
    return local.strftime(DISPLAY_FORMAT) if local else default


def hours_ago(hours: float) -> str:
    return to_iso(now_utc() - timedelta(hours=hours))


def days_ago(days: float) -> str:
    return to_iso(now_utc() - timedelta(days=days))


def humanize_duration(seconds: float | None) -> str:
    """초를 '1시간 2분 3초' 형태로."""
    if seconds is None or seconds < 0:
        return "-"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if secs or not parts:
        parts.append(f"{secs}초")
    return " ".join(parts)
