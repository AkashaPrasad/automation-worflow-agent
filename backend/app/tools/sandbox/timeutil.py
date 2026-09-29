"""Time and interval arithmetic for the sandbox calendar (done in code, never by an LLM)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

TIMEZONE = "Asia/Kolkata"
WORK_START = 9  # 09:00 local
WORK_END = 18  # 18:00 local
GRID_MIN = 30


def workspace_tz() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TIMEZONE)
    except Exception:  # tzdata missing: India has no DST, so a fixed offset is exact
        from datetime import timezone

        return timezone(timedelta(hours=5, minutes=30), TIMEZONE)


def parse_dt(value: str, tz: tzinfo) -> datetime:
    """ISO 8601 -> aware datetime. Naive values are interpreted in the workspace timezone."""
    s = str(value).strip()
    if not s:
        raise ValueError("empty datetime")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def iso(dt: datetime, tz: tzinfo | None = None) -> str:
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.isoformat(timespec="seconds")


def merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _ceil_grid(dt: datetime, grid_min: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0) if dt.second == 0 and dt.microsecond == 0 else (dt.replace(second=0, microsecond=0) + timedelta(minutes=1))
    rem = (dt.minute) % grid_min
    return dt if rem == 0 else dt + timedelta(minutes=grid_min - rem)


def free_slots(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    tz: tzinfo,
    *,
    now: datetime | None = None,
    limit: int = 8,
    per_day: int = 3,
) -> list[dict]:
    """Return up to `limit` earliest slots of `duration` inside working hours (09:00-18:00 workspace tz,
    Mon-Fri) that are free for everyone (`busy` is the union of all attendees' busy intervals).

    Each result is the first grid-aligned (30 min) start of a free gap plus how long the gap lasts."""
    merged = merge([(s, e) for s, e in busy])
    lo_bound = window_start if now is None else max(window_start, now)
    day: date = window_start.astimezone(tz).date()
    last_day: date = window_end.astimezone(tz).date()
    slots: list[dict] = []
    while day <= last_day and len(slots) < limit:
        if day.weekday() < 5:
            d_start = datetime.combine(day, time(WORK_START), tz)
            d_end = datetime.combine(day, time(WORK_END), tz)
            lo, hi = max(d_start, lo_bound), min(d_end, window_end)
            cursor = lo
            gaps: list[tuple[datetime, datetime]] = []
            for bs, be in merged:
                if be <= cursor:
                    continue
                if bs >= hi:
                    break
                if bs > cursor:
                    gaps.append((cursor, bs))
                cursor = max(cursor, be)
            if cursor < hi:
                gaps.append((cursor, hi))
            taken = 0
            for gs, ge in gaps:
                start = _ceil_grid(gs, GRID_MIN)
                if start + duration <= ge and taken < per_day and len(slots) < limit:
                    slots.append({
                        "start": iso(start, tz),
                        "end": iso(start + duration, tz),
                        "weekday": start.astimezone(tz).strftime("%A"),
                        "free_until": iso(ge, tz),
                    })
                    taken += 1
        day += timedelta(days=1)
    return slots
