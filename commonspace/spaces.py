"""Space-side controls (R-SPACE-01..04, build item B2), walk-in holds, phone sign-in,
floor tagging and the provider dashboard."""
import re
from datetime import timedelta

from . import bookings, clock, config, db
from .errors import ApiError

DESK_KINDS = ("desk", "pod", "room", "pickup", "locker", "none")
BOOKABLE_KINDS = ("desk", "pod", "room")


# ---------- rules ----------

def rules(sp):
    """The rules a worker sees before booking (wireframe: Space detail + rules)."""
    return {
        "max_hours": sp["max_hours"], "away_minutes": config.AWAY_MINUTES,
        "no_show_minutes": config.NO_SHOW_MINUTES, "food": bool(sp["food"]),
        "walkins": bool(sp["walkins"]), "lockers": bool(sp["lockers"]), "pickup": sp["pickup"],
        "min_purchase_per_hour": sp["cost_per_hour"],
    }


def settings(conn, space_id):
    sp = db.space(conn, space_id)
    return {"space_id": sp["id"], "name": sp["name"], "floor_label": sp["floor_label"],
            "open_hour": sp["open_hour"], "close_hour": sp["close_hour"],
            "published": bool(sp["published"]), "rules": rules(sp)}


def save_settings(conn, space_id, data):
    """PUT /api/spaces/{id}/settings  (A-SPACE-SETTINGS)."""
    sp = db.space(conn, space_id)
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    fields = {}
    if "max_hours" in data:
        v = db.req_float(data, "max_hours")
        if not 0.5 <= v <= config.MAX_DURATION_HOURS:
            raise ApiError(f"max_hours must be between 0.5 and {config.MAX_DURATION_HOURS}")
        fields["max_hours"] = v
    for flag in ("food", "walkins", "lockers", "published"):
        if flag in data:
            fields[flag] = 1 if data[flag] else 0
    if "pickup" in data:
        fields["pickup"] = str(data["pickup"])[:60] or sp["pickup"]
    if "floor_label" in data:
        fields["floor_label"] = str(data["floor_label"])[:40] or sp["floor_label"]
    if "min_purchase_per_hour" in data:
        v = db.req_float(data, "min_purchase_per_hour")
        if v < 0:
            raise ApiError("min_purchase_per_hour cannot be negative")
        fields["cost_per_hour"] = v
    for h in ("open_hour", "close_hour"):
        if h in data:
            v = db.req_int(data, h)
            if not 0 <= v <= 24:
                raise ApiError(f"{h} must be 0 to 24")
            fields[h] = v
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE spaces SET {sets} WHERE id=?", (*fields.values(), space_id))
    return settings(conn, space_id)


def tag_desk(conn, desk_id, data):
    """PUT /api/desks/{id}: tag a shape on the scanned floor plan (wireframe: Tag bookable zones)."""
    d = db.desk(conn, desk_id)
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    fields = {}
    if "kind" in data:
        if data["kind"] not in DESK_KINDS:
            raise ApiError(f"kind must be one of {', '.join(DESK_KINDS)}")
        fields["kind"] = data["kind"]
        fields["bookable"] = 1 if data["kind"] in BOOKABLE_KINDS else 0
    if "bookable" in data:
        fields["bookable"] = 1 if data["bookable"] else 0
    if "capacity" in data:
        v = db.req_int(data, "capacity")
        if not 1 <= v <= 50:
            raise ApiError("capacity must be 1 to 50")
        fields["capacity"] = v
    if "label" in data:
        fields["label"] = str(data["label"])[:20] or d["label"]
    if "amenities" in data:
        if not isinstance(data["amenities"], list):
            raise ApiError("amenities must be a list")
        bad = [a for a in data["amenities"] if a not in config.AMENITIES]
        if bad:
            raise ApiError(f"unknown amenity: {bad[0]}")
        fields["amenities"] = db.join(data["amenities"])
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE desks SET {sets} WHERE id=?", (*fields.values(), desk_id))
    return dict(db.desk(conn, desk_id))


# ---------- phone sign-in (wireframe: Phone # for updates) ----------

def normalize_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ApiError("enter a 10-digit phone number")
    return digits


def phone_user(conn, data):
    """POST /api/users/phone {phone, code?, name?}. Creates the account on first use.

    There is no SMS gateway in this build: any 6-digit code is accepted and the demo code
    is 000000. The response says so, so the app can show it.
    """
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    phone = normalize_phone(data.get("phone"))
    code = re.sub(r"\D", "", str(data.get("code") or ""))
    if code and len(code) != 6:
        raise ApiError("the code is 6 digits")
    row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    created = False
    if row is None:
        name = str(data.get("name") or "").strip()[:40] or f"Worker ({phone[-4:]})"
        cur = conn.execute("INSERT INTO users(name, phone) VALUES (?,?)", (name, phone))
        row = db.user(conn, cur.lastrowid)
        created = True
    return {"user_id": row["id"], "name": row["name"], "phone_last4": phone[-4:],
            "created": created, "verified": bool(code), "demo_code": "000000"}


# ---------- walk-in (wireframe: scan the mat) ----------

def desk_status(conn, desk_id):
    """GET /api/desks/{id}/status: what the table mat page shows."""
    bookings.tick(conn)
    d = db.desk(conn, desk_id)
    sp = db.space(conn, d["space_id"])
    now = clock.now()
    current = conn.execute(
        f"SELECT * FROM bookings WHERE desk_id=? AND state IN {db.OPEN_STATES} "
        "AND start_at <= ? AND end_at > ? ORDER BY start_at LIMIT 1",
        (desk_id, clock.fmt(now), clock.fmt(now))).fetchone()
    nxt = conn.execute(
        f"SELECT * FROM bookings WHERE desk_id=? AND state IN {db.OPEN_STATES} AND start_at > ? "
        "ORDER BY start_at LIMIT 1", (desk_id, clock.fmt(now))).fetchone()
    day = now.replace(hour=0, minute=0, second=0)
    closes = day + timedelta(hours=sp["close_hour"])
    free_until = min(clock.parse(nxt["start_at"]), closes) if nxt else closes
    open_now = db.space_open_for(sp, now, now + timedelta(minutes=1))
    if current:
        status = "in_use" if current["state"] != "reserved" else "booked"
    elif not open_now:
        status = "closed"
    elif free_until - now < timedelta(minutes=15):
        status = "booked_soon"
    else:
        status = "free"
    return {
        "desk_id": d["id"], "label": d["label"], "kind": d["kind"], "capacity": d["capacity"],
        "space_id": sp["id"], "space_name": sp["name"], "floor_label": sp["floor_label"],
        "status": status, "now": clock.fmt(now),
        "free_until": clock.fmt(free_until) if status in ("free", "booked_soon") else None,
        "free_minutes": max(0, int((free_until - now).total_seconds() // 60)) if not current else 0,
        "next_booking_at": nxt["start_at"] if nxt else None,
        "current_until": current["end_at"] if current else None,
        "current_state": current["state"] if current else None,
        "walkins_allowed": bool(sp["walkins"]) and bool(d["bookable"]),
        "rules": rules(sp),
        "amenities": sorted(db.desk_amenities(conn, d)),
        "hold_url": f"/t/{d['id']}",
    }


def hold_desk(conn, desk_id, data):
    """POST /api/desks/{id}/hold {phone}: walk-in hold until the next booking or closing.

    Walk-ins are allowed when the desk is free and bookings always take priority (R-SPACE-01).
    """
    st = desk_status(conn, desk_id)
    if not st["walkins_allowed"]:
        raise ApiError("walk-ins are not allowed at this table")
    if st["status"] not in ("free", "booked_soon"):
        raise ApiError("this table is not free right now")
    if st["free_minutes"] < 10:
        raise ApiError("a booking starts here in under 10 minutes")
    user = phone_user(conn, data)
    sp = db.space(conn, st["space_id"])
    now = clock.now()
    end = clock.parse(st["free_until"])
    end = min(end, now + timedelta(hours=sp["max_hours"]))
    if bookings.open_count(conn, user["user_id"]) >= config.MAX_OPEN_BOOKINGS:
        raise ApiError(f"you already have {config.MAX_OPEN_BOOKINGS} open bookings")
    cur = conn.execute(
        "INSERT INTO bookings(user_id, desk_id, start_at, end_at, state, created_at, checked_in_at, kind)"
        " VALUES (?,?,?,?,'checked_in',?,?,'walkin')",
        (user["user_id"], desk_id, clock.fmt(now), clock.fmt(end), clock.fmt(now), clock.fmt(now)))
    b = bookings.serialize(conn, db.get(conn, "bookings", cur.lastrowid))
    b["user"] = user
    b["next_step"] = f"Pick up a coaster at the {sp['pickup'].lower()} to lock in the table" + \
                     (" and unlock a locker." if sp["lockers"] else ".")
    return b


# ---------- floor plan and displays ----------

def floor_plan(conn, space_id):
    """GET /api/spaces/{id}/floor: every shape with live state, for the app, displays and tagging."""
    from . import puck
    bookings.tick(conn)
    sp = db.space(conn, space_id)
    now = clock.now()
    shapes = []
    for d in conn.execute("SELECT * FROM desks WHERE space_id=? ORDER BY row, col, id", (space_id,)):
        st = desk_status(conn, d["id"]) if d["bookable"] else None
        p = conn.execute("SELECT * FROM pucks WHERE desk_id=?", (d["id"],)).fetchone()
        shapes.append({
            "desk_id": d["id"], "label": d["label"], "kind": d["kind"], "capacity": d["capacity"],
            "col": d["col"], "row": d["row"], "bookable": bool(d["bookable"]),
            "amenities": sorted(db.desk_amenities(conn, d)),
            "status": st["status"] if st else "none",
            "free_until": st["free_until"] if st else None,
            "current_until": st["current_until"] if st else None,
            "next_booking_at": st["next_booking_at"] if st else None,
            "puck_id": p["id"] if p else None,
            "puck": puck.state_for(conn, p)[0]["state"] if p else None,
        })
    bookable = [s for s in shapes if s["bookable"]]
    return {
        "space_id": sp["id"], "space_name": sp["name"], "floor_label": sp["floor_label"],
        "now": clock.fmt(now), "open_now": db.space_open_for(sp, now, now + timedelta(minutes=1)),
        "cols": max([s["col"] for s in shapes] + [0]) + 1, "rows": max([s["row"] for s in shapes] + [0]) + 1,
        "shapes": shapes,
        "summary": {
            "open": sum(1 for s in bookable if s["status"] == "free"),
            "booked_soon": sum(1 for s in bookable if s["status"] == "booked_soon"),
            "in_use": sum(1 for s in bookable if s["status"] in ("in_use", "booked")),
            "tagged": sum(1 for s in shapes if s["kind"] != "none"),
            "to_confirm": sum(1 for s in shapes if s["kind"] == "none"),
        },
        "rules": rules(sp),
    }


# ---------- staff desk ----------

def staff_lookup(conn, space_id, q):
    """GET /api/spaces/{id}/staff/bookings?q=: today's bookings by name or table for the hand-off."""
    bookings.tick(conn)
    db.space(conn, space_id)
    now = clock.now()
    day = now.replace(hour=0, minute=0, second=0)
    rows = conn.execute(
        "SELECT b.*, d.label, u.name, u.phone FROM bookings b JOIN desks d ON d.id=b.desk_id "
        "JOIN users u ON u.id=b.user_id WHERE d.space_id=? AND b.start_at < ? AND b.end_at > ? "
        "ORDER BY b.start_at", (space_id, clock.fmt(day + timedelta(days=1)), clock.fmt(day))).fetchall()
    q = (q or "").strip().lower()
    out = []
    for b in rows:
        if q and q not in b["name"].lower() and q not in b["label"].lower():
            continue
        out.append({"booking_id": b["id"], "name": b["name"], "phone_verified": bool(b["phone"]),
                    "desk_label": b["label"], "start": b["start_at"], "end": b["end_at"],
                    "state": b["state"], "kind": b["kind"], "coaster": b["coaster"],
                    "end_reason": b["end_reason"]})
    return {"bookings": out}


def hand_off(conn, booking_id, data):
    """POST /api/bookings/{id}/handoff {coaster}: staff gives a coaster and checks the worker in."""
    bookings.tick(conn)
    b = db.get(conn, "bookings", booking_id, "booking")
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    coaster = str(data.get("coaster") or "").strip()[:8]
    if not coaster:
        raise ApiError("coaster number is required")
    if b["state"] not in db.OPEN_STATES:
        raise ApiError(f"booking is already {b['state']}")
    conn.execute("UPDATE bookings SET coaster=? WHERE id=?", (coaster, booking_id))
    if b["state"] == "reserved":
        now = clock.now()
        start = clock.parse(b["start_at"])
        if now < start - timedelta(minutes=config.CHECKIN_EARLY_MINUTES):
            raise ApiError(f"check-in opens {config.CHECKIN_EARLY_MINUTES} minutes before the start")
        conn.execute("UPDATE bookings SET state='checked_in', checked_in_at=? WHERE id=?",
                     (clock.fmt(now), booking_id))
    return bookings.serialize(conn, db.get(conn, "bookings", booking_id))


def coaster_return(conn, booking_id, data):
    """POST /api/bookings/{id}/return_coaster: returning the coaster ends the session."""
    b = db.get(conn, "bookings", booking_id, "booking")
    if b["state"] in db.OPEN_STATES:
        bookings._close(conn, booking_id, "ended", "user")
    return bookings.serialize(conn, db.get(conn, "bookings", booking_id))


# ---------- dashboard (wireframe: Dashboard) ----------

def dashboard(conn, space_id, period="today"):
    """GET /api/spaces/{id}/dashboard?period=today|week|month"""
    from . import insights
    bookings.tick(conn)
    sp = db.space(conn, space_id)
    now = clock.now()
    days = {"today": 1, "week": 7, "month": 30}.get(period)
    if days is None:
        raise ApiError("period must be today, week or month")
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    rows = conn.execute(
        "SELECT b.* FROM bookings b JOIN desks d ON d.id=b.desk_id WHERE d.space_id=? "
        "AND b.start_at >= ? AND b.start_at < ?",
        (space_id, clock.fmt(since), clock.fmt(now + timedelta(days=1)))).fetchall()
    checkins = [b for b in rows if b["checked_in_at"]]
    no_shows = [b for b in rows if b["end_reason"] == "no_show"]
    walkins = [b for b in rows if b["kind"] == "walkin"]
    by_hour = []
    for h in range(24):
        bk = sum(1 for b in rows if clock.parse(b["start_at"]).hour == h and b["kind"] == "prebook")
        ci = sum(1 for b in checkins if clock.parse(b["checked_in_at"]).hour == h)
        by_hour.append({"hour": h, "bookings": bk, "checkins": ci})
    ins = insights.space_insights(conn, space_id)
    unmet = [{"amenity": r["amenity"], "zero_result_searches": r["zero_result_searches"],
              "gap": r["gap"]} for r in ins["demand_vs_supply"] if r["zero_result_searches"] > 0]
    unmet.sort(key=lambda r: -r["zero_result_searches"])
    plan = floor_plan(conn, space_id)
    attention = []
    for s in plan["shapes"]:
        if s["puck"] == "away":
            attention.append({"kind": "away", "text": f"{s['label']} owner away, coaster on mat"})
    for p in conn.execute("SELECT p.*, d.label FROM pucks p JOIN desks d ON d.id=p.desk_id "
                          "WHERE d.space_id=? AND p.battery < ?", (space_id, config.LOW_BATTERY)):
        attention.append({"kind": "battery", "text": f"Coaster {p['id']} low battery ({p['battery']}%)"})
    for b in rows:
        if b["state"] in ("checked_in", "away") and b["coaster"] and \
                clock.parse(b["end_at"]) < now:
            attention.append({"kind": "coaster", "text": f"Coaster #{b['coaster']} overdue"})
    live = plan["summary"]
    return {
        "space_id": sp["id"], "space_name": sp["name"], "period": period, "now": clock.fmt(now),
        "kpis": {"in_use": live["in_use"], "desks": live["in_use"] + live["open"] + live["booked_soon"],
                 "checkins": len(checkins), "no_shows": len(no_shows), "walkins": len(walkins),
                 "bookings": len(rows)},
        "by_hour": by_hour,
        "unmet_searches": unmet[:5],
        "needs_attention": attention[:6],
        "recommendations": ins["recommendations"][:3],
    }
