"""SQLite schema and shared helpers. Entity IDs (E-...) match the Notion Entities database."""
import math
import sqlite3
from datetime import timedelta

from . import clock
from .errors import ApiError, NotFound

OPEN_STATES = ("reserved", "checked_in", "away")

SCHEMA = """
CREATE TABLE IF NOT EXISTS neighborhoods(          -- search origins
    name TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE IF NOT EXISTS spaces(                 -- E-SPACE
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT, neighborhood TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL,
    open_hour INTEGER NOT NULL, close_hour INTEGER NOT NULL,
    noise INTEGER NOT NULL, cost_per_hour REAL NOT NULL,
    max_hours REAL NOT NULL DEFAULT 4,          -- R-SPACE-01 rules (B2)
    food INTEGER NOT NULL DEFAULT 1, walkins INTEGER NOT NULL DEFAULT 1,
    lockers INTEGER NOT NULL DEFAULT 1, pickup TEXT NOT NULL DEFAULT 'Front desk',
    floor_label TEXT NOT NULL DEFAULT 'Floor 1', published INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS desks(                  -- E-DESK
    id INTEGER PRIMARY KEY, space_id INTEGER NOT NULL REFERENCES spaces(id),
    label TEXT NOT NULL, amenities TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'desk',          -- desk | pod | room | pickup | locker | none
    capacity INTEGER NOT NULL DEFAULT 1, col INTEGER NOT NULL DEFAULT 0, row INTEGER NOT NULL DEFAULT 0,
    bookable INTEGER NOT NULL DEFAULT 1);       -- R-SPACE-03
CREATE TABLE IF NOT EXISTS pucks(                  -- E-PUCK
    id INTEGER PRIMARY KEY, desk_id INTEGER NOT NULL UNIQUE REFERENCES desks(id),
    battery INTEGER NOT NULL DEFAULT 100, last_heartbeat TEXT);
CREATE TABLE IF NOT EXISTS users(                  -- E-USER
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS preferences(            -- E-PREF
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    amenities TEXT NOT NULL DEFAULT '', work_mode TEXT NOT NULL DEFAULT 'focus',
    max_km REAL NOT NULL DEFAULT 3.0,
    hours TEXT NOT NULL DEFAULT '',            -- when they usually work: morning,midday,afternoon,evening
    source TEXT, updated_at TEXT);             -- preference funnel: onboarding | search | booking | settings
CREATE TABLE IF NOT EXISTS bookings(               -- E-BOOKING
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    desk_id INTEGER NOT NULL REFERENCES desks(id),
    start_at TEXT NOT NULL, end_at TEXT NOT NULL, state TEXT NOT NULL,
    created_at TEXT, checked_in_at TEXT, away_since TEXT,
    extensions INTEGER NOT NULL DEFAULT 0, ended_at TEXT, end_reason TEXT,
    kind TEXT NOT NULL DEFAULT 'prebook',       -- prebook | walkin
    coaster TEXT);                              -- coaster number handed over by staff
CREATE INDEX IF NOT EXISTS ix_bookings_desk ON bookings(desk_id, state);
CREATE INDEX IF NOT EXISTS ix_bookings_user ON bookings(user_id, state);
CREATE TABLE IF NOT EXISTS searches(               -- E-SEARCH (demand signal)
    id INTEGER PRIMARY KEY, user_id INTEGER, ts TEXT NOT NULL, neighborhood TEXT,
    lat REAL, lon REAL, start_at TEXT, duration_hours REAL,
    amenities TEXT NOT NULL DEFAULT '', work_mode TEXT, result_count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_searches_ts ON searches(ts);
CREATE TABLE IF NOT EXISTS features(               -- E-FEATURE (rentable add-on)
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, amenity TEXT NOT NULL,
    weekly_cost REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rentals(                -- E-RENTAL
    id INTEGER PRIMARY KEY, space_id INTEGER NOT NULL REFERENCES spaces(id),
    feature_id INTEGER NOT NULL REFERENCES features(id),
    started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS funnel_events(         -- preference intake funnel steps
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), ts TEXT NOT NULL,
    step TEXT NOT NULL, action TEXT NOT NULL, entry TEXT);
CREATE TABLE IF NOT EXISTS device_events(          -- E-DEVICE-EVENT
    id INTEGER PRIMARY KEY, puck_id INTEGER NOT NULL, ts TEXT NOT NULL,
    kind TEXT NOT NULL, user_id INTEGER, detail TEXT);
"""


def connect(path=":memory:"):
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def is_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0


# ---------- value helpers ----------

def split(csv):
    return {a for a in (csv or "").split(",") if a}


def join(items):
    return ",".join(sorted(set(items)))


def req_int(data, key):
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError(f"{key} must be an integer")


def req_float(data, key, default=None):
    if key not in data or data[key] is None:
        if default is None:
            raise ApiError(f"{key} is required")
        return float(default)
    try:
        return float(data[key])
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a number")


def req_time(data, key, default=None):
    if data.get(key) in (None, ""):
        if default is None:
            raise ApiError(f"{key} is required")
        return default
    try:
        return clock.parse(data[key])
    except ValueError:
        raise ApiError(f"{key} must be a time like 2026-11-18T10:00")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- lookups ----------

def get(conn, table, id_, label=None):
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (id_,)).fetchone()
    if row is None:
        raise NotFound(f"{label or table[:-1]} {id_} not found")
    return row


def space(conn, id_):
    return get(conn, "spaces", id_, "space")


def desk(conn, id_):
    return get(conn, "desks", id_, "desk")


def user(conn, id_):
    return get(conn, "users", id_, "user")


def puck(conn, id_):
    return get(conn, "pucks", id_, "puck")


def neighborhood(conn, name):
    row = conn.execute("SELECT * FROM neighborhoods WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ApiError(f"unknown neighborhood: {name}")
    return row


def rental_amenities(conn, space_id):
    """Amenities an active add-on trial adds to every desk in the space."""
    rows = conn.execute(
        "SELECT f.amenity FROM rentals r JOIN features f ON f.id=r.feature_id "
        "WHERE r.space_id=? AND r.ended_at IS NULL", (space_id,)).fetchall()
    return {r["amenity"] for r in rows}


def desk_amenities(conn, desk_row, extra=None):
    if extra is None:
        extra = rental_amenities(conn, desk_row["space_id"])
    return split(desk_row["amenities"]) | extra


def space_open_for(sp, start, end):
    """True if the space is open for the whole window [start, end)."""
    if sp["open_hour"] <= 0 and sp["close_hour"] >= 24:
        return True
    day = start.replace(hour=0, minute=0, second=0)
    opens = day + timedelta(hours=sp["open_hour"])
    closes = day + timedelta(hours=sp["close_hour"])
    return opens <= start and end <= closes


def desk_free(conn, desk_id, start, end, exclude_booking=None):
    row = conn.execute(
        f"SELECT 1 FROM bookings WHERE desk_id=? AND state IN {OPEN_STATES} "
        "AND start_at < ? AND end_at > ? AND id IS NOT ?",
        (desk_id, clock.fmt(end), clock.fmt(start), exclude_booking)).fetchone()
    return row is None
