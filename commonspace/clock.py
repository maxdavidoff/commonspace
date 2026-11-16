"""Simulated clock: real time (or a fixed base, for tests) plus an offset.

Every timer in the system reads clock.now(), so /api/dev/advance can fast-forward
no-show and away releases for a demo (D-05).
"""
from datetime import datetime, timedelta

_offset = timedelta(0)
_fixed = None

FMT = "%Y-%m-%d %H:%M:%S"


def now():
    base = _fixed if _fixed is not None else datetime.now().replace(microsecond=0)
    return base + _offset


def advance(minutes):
    global _offset
    _offset += timedelta(minutes=float(minutes))
    return now()


def set_fixed(dt):
    """Freeze the clock at dt (tests and the scripted demo)."""
    global _fixed, _offset
    _fixed = dt.replace(microsecond=0)
    _offset = timedelta(0)


def use_real_time():
    global _fixed, _offset
    _fixed = None
    _offset = timedelta(0)


def offset_minutes():
    return _offset.total_seconds() / 60


def fmt(dt):
    return dt.strftime(FMT)


def parse(value):
    """Parse 'YYYY-MM-DD HH:MM[:SS]' or ISO 'YYYY-MM-DDTHH:MM'. Raises ValueError."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("not a time")
    dt = datetime.fromisoformat(value.strip().replace("Z", ""))
    return dt.replace(tzinfo=None, microsecond=0)
