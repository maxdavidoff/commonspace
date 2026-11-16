"""Puck LED state and tap/hold gestures. The server holds booking truth; the puck only shows it."""
from datetime import timedelta

from . import bookings, clock, config, db
from .errors import ApiError


def _current_booking(conn, desk_id, now):
    """The open booking that the puck should show right now, if any."""
    early = clock.fmt(now + timedelta(minutes=config.CHECKIN_EARLY_MINUTES))
    return conn.execute(
        f"SELECT * FROM bookings WHERE desk_id=? AND state IN {db.OPEN_STATES} "
        "AND start_at <= ? AND end_at > ? ORDER BY start_at LIMIT 1",
        (desk_id, early, clock.fmt(now))).fetchone()


def state_for(conn, puck_row):
    now = clock.now()
    b = _current_booking(conn, puck_row["desk_id"], now)
    if b is None:
        state, countdown = "free", None
    elif b["state"] == "reserved":
        state = "reserved"
        countdown = (clock.parse(b["start_at"]) + timedelta(minutes=config.NO_SHOW_MINUTES) - now)
    elif b["state"] == "checked_in":
        state, countdown = "occupied", clock.parse(b["end_at"]) - now
    else:
        state = "away"
        countdown = clock.parse(b["away_since"]) + timedelta(minutes=config.AWAY_MINUTES) - now
    led = config.LED[state]
    return {
        "puck_id": puck_row["id"], "desk_id": puck_row["desk_id"], "state": state,
        "color": led["color"], "pattern": led["pattern"],
        "countdown_s": None if countdown is None else max(0, int(countdown.total_seconds())),
        "booking_id": b["id"] if b else None,
        "battery": puck_row["battery"], "low_battery": puck_row["battery"] < config.LOW_BATTERY,
        "last_heartbeat": puck_row["last_heartbeat"],
    }, b


def led(conn, puck_id):
    return state_for(conn, db.puck(conn, puck_id))[0]


def _log(conn, puck_id, kind, user_id=None, detail=None):
    conn.execute("INSERT INTO device_events(puck_id, ts, kind, user_id, detail) VALUES (?,?,?,?,?)",
                 (puck_id, clock.fmt(clock.now()), kind, user_id, detail))


def heartbeat(conn, puck_id, data):
    p = db.puck(conn, puck_id)
    data = data if isinstance(data, dict) else {}
    battery = int(db.req_float(data, "battery", p["battery"]))
    if not 0 <= battery <= 100:
        raise ApiError("battery must be 0 to 100")
    bookings.tick(conn)
    conn.execute("UPDATE pucks SET battery=?, last_heartbeat=? WHERE id=?",
                 (battery, clock.fmt(clock.now()), puck_id))
    out = led(conn, puck_id)
    _log(conn, puck_id, "heartbeat", detail=f"battery={battery} state={out['state']}")
    return out


# state -> {gesture: booking action}
GESTURES = {
    "reserved": {"tap": "checkin", "hold": "end"},
    "occupied": {"tap": "away", "hold": "end"},
    "away":     {"tap": "return", "hold": "end"},
}


def gesture(conn, puck_id, data):
    p = db.puck(conn, puck_id)
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    user_id = db.user(conn, db.req_int(data, "user_id"))["id"]
    kind = data.get("kind")
    if kind not in ("tap", "hold"):
        raise ApiError("kind must be tap or hold")
    bookings.tick(conn)
    current, b = state_for(conn, p)
    if current["state"] == "free":
        _log(conn, puck_id, kind, user_id, "refused: desk is free")
        raise ApiError("this desk is free; book it in the app first")
    if b["user_id"] != user_id:
        _log(conn, puck_id, kind, user_id, "refused: not the booking owner")
        raise ApiError("this desk is held by someone else")
    action = GESTURES[current["state"]][kind]
    bookings.act(conn, b["id"], action, {"user_id": user_id})
    after = led(conn, puck_id)
    _log(conn, puck_id, kind, user_id, f"{action}: {current['state']} -> {after['state']}")
    return {"action": action, "led": after}


def events(conn, puck_id, limit=50):
    db.puck(conn, puck_id)
    rows = conn.execute("SELECT * FROM device_events WHERE puck_id=? ORDER BY id DESC LIMIT ?",
                        (puck_id, limit)).fetchall()
    return {"puck_id": puck_id, "events": [dict(r) for r in rows]}


def all_pucks(conn):
    bookings.tick(conn)
    out = []
    for p in conn.execute(
            "SELECT p.*, d.label, d.space_id, s.name AS space_name FROM pucks p "
            "JOIN desks d ON d.id=p.desk_id JOIN spaces s ON s.id=d.space_id ORDER BY p.id"):
        info = state_for(conn, p)[0]
        info.update(desk_label=p["label"], space_id=p["space_id"], space_name=p["space_name"])
        out.append(info)
    return {"pucks": out}
