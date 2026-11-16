"""Booking rules and lifecycle state machine.

    reserved --checkin--> checked_in <--away/return--> away
       |                      |                         |
   no-show (R-BOOK-01)    end / expiry             away timeout (R-BOOK-02)
       v                      v                         v
    released               ended                     released

Final states: ended (end_reason user | expired) and released (no_show | away_timeout).
"""
from datetime import timedelta

from . import clock, config, db
from .errors import ApiError

ACTIONS = ("checkin", "away", "return", "extend", "end")


def _row(conn, booking_id):
    return db.get(conn, "bookings", booking_id, "booking")


def open_count(conn, user_id):
    return conn.execute(
        f"SELECT COUNT(*) FROM bookings WHERE user_id=? AND state IN {db.OPEN_STATES}",
        (user_id,)).fetchone()[0]


def create(conn, data):
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    user = db.user(conn, db.req_int(data, "user_id"))
    desk = db.desk(conn, db.req_int(data, "desk_id"))
    sp = db.space(conn, desk["space_id"])
    now = clock.now()
    start = db.req_time(data, "start", default=now)
    duration = db.req_float(data, "duration_hours", default=2)
    if not 0 < duration <= config.MAX_DURATION_HOURS:
        raise ApiError(f"duration_hours must be more than 0 and at most {config.MAX_DURATION_HOURS}")
    end = start + timedelta(hours=duration)
    if start < now - timedelta(minutes=config.MAX_PAST_START_MINUTES):
        raise ApiError("start is in the past")
    if duration > sp["max_hours"]:                                          # R-SPACE-01
        raise ApiError(f"{sp['name']} allows at most {sp['max_hours']:g} hours per booking")
    if not desk["bookable"]:                                                 # R-SPACE-03
        raise ApiError("that table is walk-in only")
    if not db.space_open_for(sp, start, end):
        raise ApiError(f"{sp['name']} is closed for part of that window")
    if open_count(conn, user["id"]) >= config.MAX_OPEN_BOOKINGS:          # R-BOOK-03
        raise ApiError(f"you already have {config.MAX_OPEN_BOOKINGS} open bookings")
    if not db.desk_free(conn, desk["id"], start, end):
        raise ApiError("that desk is already booked for that window")
    cur = conn.execute(
        "INSERT INTO bookings(user_id, desk_id, start_at, end_at, state, created_at) "
        "VALUES (?,?,?,?, 'reserved', ?)",
        (user["id"], desk["id"], clock.fmt(start), clock.fmt(end), clock.fmt(now)))
    return serialize(conn, _row(conn, cur.lastrowid))


def act(conn, booking_id, action, data):
    """Apply a lifecycle action for the booking's owner."""
    if action not in ACTIONS:
        raise ApiError(f"unknown action: {action}")
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    tick(conn)
    b = _row(conn, booking_id)
    if b["user_id"] != db.req_int(data, "user_id"):
        raise ApiError("this booking belongs to someone else")
    if b["state"] not in db.OPEN_STATES:
        raise ApiError(f"booking is already {b['state']}")
    now = clock.now()
    start, end = clock.parse(b["start_at"]), clock.parse(b["end_at"])

    if action == "checkin":
        if b["state"] != "reserved":
            raise ApiError("already checked in")
        if now < start - timedelta(minutes=config.CHECKIN_EARLY_MINUTES):
            raise ApiError(f"check-in opens {config.CHECKIN_EARLY_MINUTES} minutes before the start")
        conn.execute("UPDATE bookings SET state='checked_in', checked_in_at=? WHERE id=?",
                     (clock.fmt(now), b["id"]))
    elif action == "away":
        if b["state"] != "checked_in":
            raise ApiError("you can only step away from a desk you are checked in at")
        conn.execute("UPDATE bookings SET state='away', away_since=? WHERE id=?",
                     (clock.fmt(now), b["id"]))
    elif action == "return":
        if b["state"] != "away":
            raise ApiError("you are not away")
        conn.execute("UPDATE bookings SET state='checked_in', away_since=NULL WHERE id=?",
                     (b["id"],))
    elif action == "extend":                                                 # R-BOOK-04
        if b["state"] == "reserved":
            raise ApiError("check in before extending")
        if b["extensions"] >= config.MAX_EXTENSIONS:
            raise ApiError(f"already extended {config.MAX_EXTENSIONS} times")
        new_end = end + timedelta(minutes=config.EXTEND_MINUTES)
        desk = db.desk(conn, b["desk_id"])
        if not db.space_open_for(db.space(conn, desk["space_id"]), start, new_end):
            raise ApiError("the space closes before the extended time")
        if not db.desk_free(conn, b["desk_id"], end, new_end, exclude_booking=b["id"]):
            raise ApiError("someone has this desk next")
        conn.execute("UPDATE bookings SET end_at=?, extensions=extensions+1 WHERE id=?",
                     (clock.fmt(new_end), b["id"]))
    elif action == "end":
        _close(conn, b["id"], "ended", "user")
    return serialize(conn, _row(conn, b["id"]))


def _close(conn, booking_id, state, reason):
    conn.execute("UPDATE bookings SET state=?, end_reason=?, ended_at=?, away_since=NULL "
                 "WHERE id=?", (state, reason, clock.fmt(clock.now()), booking_id))


def tick(conn):
    """Apply timers. Called by the server's background thread and before every read or action."""
    now = clock.now()
    changed = []
    rows = conn.execute(
        f"SELECT * FROM bookings WHERE state IN {db.OPEN_STATES}").fetchall()
    for b in rows:
        start, end = clock.parse(b["start_at"]), clock.parse(b["end_at"])
        if b["state"] == "reserved":
            if now >= start + timedelta(minutes=config.NO_SHOW_MINUTES) or now >= end:  # R-BOOK-01
                _close(conn, b["id"], "released", "no_show")
                changed.append((b["id"], "no_show"))
        elif b["state"] == "away" and \
                now >= clock.parse(b["away_since"]) + timedelta(minutes=config.AWAY_MINUTES):  # R-BOOK-02
            _close(conn, b["id"], "released", "away_timeout")
            changed.append((b["id"], "away_timeout"))
        elif now >= end:
            _close(conn, b["id"], "ended", "expired")
            changed.append((b["id"], "expired"))
    return changed


def serialize(conn, b, now=None):
    now = now or clock.now()
    start, end = clock.parse(b["start_at"]), clock.parse(b["end_at"])
    info = conn.execute(
        "SELECT d.label, d.space_id, s.name FROM desks d JOIN spaces s ON s.id=d.space_id "
        "WHERE d.id=?", (b["desk_id"],)).fetchone()
    out = {
        "id": b["id"], "user_id": b["user_id"], "desk_id": b["desk_id"],
        "desk_label": info["label"], "space_id": info["space_id"], "space_name": info["name"],
        "start": b["start_at"], "end": b["end_at"], "state": b["state"],
        "checked_in_at": b["checked_in_at"],
        "kind": b["kind"], "coaster": b["coaster"],
        "extensions_used": b["extensions"],
        "extensions_left": config.MAX_EXTENSIONS - b["extensions"],
        "end_reason": b["end_reason"],
    }
    if b["state"] in db.OPEN_STATES:
        out["starts_in_s"] = max(0, int((start - now).total_seconds()))
        out["seconds_left"] = max(0, int((end - now).total_seconds()))
    if b["state"] == "reserved":
        out["no_show_in_s"] = max(0, int(
            (start + timedelta(minutes=config.NO_SHOW_MINUTES) - now).total_seconds()))
    if b["state"] == "away":
        out["away_left_s"] = max(0, int(
            (clock.parse(b["away_since"]) + timedelta(minutes=config.AWAY_MINUTES)
             - now).total_seconds()))
    return out


def user_bookings(conn, user_id):
    db.user(conn, user_id)
    tick(conn)
    rows = conn.execute(
        f"SELECT * FROM bookings WHERE user_id=? AND state IN {db.OPEN_STATES} ORDER BY start_at",
        (user_id,)).fetchall()
    return {"user_id": user_id, "bookings": [serialize(conn, b) for b in rows]}
