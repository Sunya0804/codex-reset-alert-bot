"""Deterministic time evidence checks. Unsupported expressions stay unconfirmed."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc
NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))


def relative_minutes(phrase):
    text = phrase.lower().strip().rstrip(".! ")
    match = re.fullmatch(r"in\s+(.+)", text)
    if not match:
        return None
    total = Decimal(0)
    parts = re.split(r"\s+and\s+|,\s*", match[1])
    for part in parts:
        term = re.fullmatch(r"(\d+(?:\.\d+)?|[a-z]+)\s+(minutes?|mins?|hours?|hrs?|days?)", part)
        if not term:
            return None
        number = term[1]
        if number in NUMBERS:
            number = str(NUMBERS[number])
        elif not re.fullmatch(r"\d+(?:\.\d+)?", number):
            return None
        total += Decimal(number) * (1 if term[2].startswith("min") else 60 if term[2].startswith("h") else 1440)
    return int(total) if total == total.to_integral_value() and 0 <= total <= 43200 else None


def explicit_time(phrase, created):
    """Resolve ISO or dated English clocks. Reject conflicting/ambiguous evidence."""
    if created.tzinfo is None:
        return None
    text = phrase.lower()
    iso = re.findall(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:\d{2})\b", text)
    if iso:
        if len(iso) != 1:
            return None
        return datetime.fromisoformat(iso[0].upper().replace("Z", "+00:00"))
    clocks = list(re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(utc|gmt|pst|pdt|pt|est|edt|et|kst|jst|cet|cest|bst)\b", text))
    if len(clocks) != 1:
        return None
    clock = clocks[0]
    hour, minute = int(clock[1]), int(clock[2] or 0)
    if minute > 59 or hour > 23 or (clock[3] and not 1 <= hour <= 12):
        return None
    if clock[3]:
        hour = hour % 12 + (12 if clock[3] == "pm" else 0)
    zones = {"pt": "America/Los_Angeles", "et": "America/New_York"}
    offsets = {"utc": 0, "gmt": 0, "pst": -8, "pdt": -7, "est": -5, "edt": -4, "kst": 9, "jst": 9, "cet": 1, "cest": 2, "bst": 1}
    zone = ZoneInfo(zones[clock[4]]) if clock[4] in zones else timezone(timedelta(hours=offsets[clock[4]]))
    base = created.astimezone(zone)
    dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    relative_dates = re.findall(r"\b(today|tomorrow)\b", text)
    if len(dates) + len(relative_dates) != 1:
        return None
    if dates:
        year, month, day = map(int, dates[0])
        base = base.replace(year=year, month=month, day=day)
    elif relative_dates[0] == "tomorrow":
        base += timedelta(days=1)
    result = base.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if isinstance(zone, ZoneInfo):
        if result.utcoffset() != result.replace(fold=1).utcoffset() or result.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != result.replace(tzinfo=None):
            return None
    return result
