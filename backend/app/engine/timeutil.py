"""Dates are code's job (SPEC: "Code owns policy, arithmetic, dates").

The planner gets a calendar block and concrete ISO windows for the time expressions in the request
("next week" → Mon 09:00 … Fri 17:00 local), so it never does date arithmetic itself.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WORK_START = time(9, 0)
WORK_END = time(17, 0)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def zone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def now_for(profile: dict[str, Any]) -> datetime:
    """The workspace clock: the sandbox's ``now_iso`` when present, else real time in the user's timezone."""
    tz = zone(profile.get("timezone"))
    raw = profile.get("now_iso")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)  # 3.11 parses "Z" and offsets
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
        except ValueError:
            pass
    return datetime.now(tz)


def _at(d: date, t: time, tz: tzinfo) -> str:
    return datetime.combine(d, t, tzinfo=tz).isoformat(timespec="seconds")


def _window(start: date, end: date, tz: tzinfo, label: str) -> dict[str, str]:
    return {"start": _at(start, WORK_START, tz), "end": _at(end, WORK_END, tz), "label": label}


def _next_workday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def resolve_time_ref(text: str, now: datetime) -> dict[str, str] | None:
    """Business-hours window for common expressions; None when not understood (the planner then uses the
    calendar block)."""
    tz = now.tzinfo or ZoneInfo("UTC")
    today = now.date()
    t = text.strip().lower()
    monday = today - timedelta(days=today.weekday())
    if re.search(r"\btoday\b|\bthis (morning|afternoon|evening)\b|\btonight\b|\beod\b|end of (the )?day", t):
        return _window(today, today, tz, "today")
    if re.search(r"\btomorrow\b", t):
        d = today + timedelta(days=1)
        return _window(d, d, tz, "tomorrow")
    if re.search(r"\bnext week\b", t):
        start = monday + timedelta(days=7)
        return _window(start, start + timedelta(days=4), tz, "next week (Mon-Fri)")
    if re.search(r"\bthis week\b|\bend of (the )?week\b|\beow\b|\bby friday\b", t):
        start = max(today, monday)
        return _window(start, monday + timedelta(days=4), tz, "rest of this week")
    m = re.search(r"\b(?:in|within|next) (\d{1,2}) (?:business |working )?days?\b", t)
    if m:
        end = today
        for _ in range(int(m.group(1))):
            end = _next_workday(end)
        return _window(_next_workday(today) if now.time() > WORK_END else today, end, tz, f"next {m.group(1)} days")
    if re.search(r"\bnext month\b", t):
        first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return _window(first, last, tz, "next month")
    for idx, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t) or re.search(rf"\b{name[:3]}\b", t):
            if re.search(rf"\bnext {name[:3]}", t):
                d = monday + timedelta(days=7 + idx)  # "next Friday" = Friday of next week
            else:
                d = today + timedelta(days=(idx - today.weekday()) % 7 or 7)  # the upcoming one
            return _window(d, d, tz, f"{name.title()} {d.isoformat()}")
    return None


def date_context(now: datetime, time_refs: list[str] | None = None) -> str:
    tz_name = getattr(now.tzinfo, "key", None) or now.strftime("%Z") or "UTC"
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    lines = [
        f"Now: {now.isoformat(timespec='seconds')} ({now.strftime('%A')}), timezone {tz_name}",
        f"Working hours: {WORK_START:%H:%M}-{WORK_END:%H:%M} Mon-Fri",
        f"This week: Mon {monday.isoformat()} to Fri {(monday + timedelta(days=4)).isoformat()}",
        f"Next week: Mon {(monday + timedelta(days=7)).isoformat()} to Fri {(monday + timedelta(days=11)).isoformat()}",
        "Next 10 days: " + ", ".join(f"{(today + timedelta(days=i)).strftime('%a')} {(today + timedelta(days=i)).isoformat()}"
                                     for i in range(1, 11)),
    ]
    resolved = []
    for ref in time_refs or []:
        w = resolve_time_ref(ref, now)
        if w:
            resolved.append(f'  "{ref}" -> window_start {w["start"]}, window_end {w["end"]} ({w["label"]})')
    if resolved:
        lines.append("Resolved time references (computed by code; use these exact values):")
        lines.extend(resolved)
    return "\n".join(lines)
