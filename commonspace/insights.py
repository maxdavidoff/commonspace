"""Demand-gap analysis, add-on ROI and feature rentals (R-INSIGHT-01..03)."""
from datetime import timedelta

from . import bookings, clock, config, db
from .errors import ApiError


def _nearby_searches(conn, sp, since):
    rows = conn.execute("SELECT * FROM searches WHERE ts >= ?", (clock.fmt(since),)).fetchall()
    return [r for r in rows
            if r["lat"] is not None and
            db.haversine_km(r["lat"], r["lon"], sp["lat"], sp["lon"]) <= config.INSIGHT_RADIUS_KM]


def estimate(searches_per_week, demand_share, supply_share, zero_per_week):
    """R-INSIGHT-03 estimated extra bookings per week."""
    return (searches_per_week * demand_share * config.CAPTURE_RATE * (1 - supply_share)
            + config.ZERO_RESULT_CONVERSION * zero_per_week)


def demand_table(conn, space_id):
    sp = db.space(conn, space_id)
    now = clock.now()
    weeks = config.INSIGHT_WINDOW_DAYS / 7
    near = _nearby_searches(conn, sp, now - timedelta(days=config.INSIGHT_WINDOW_DAYS))
    n = len(near)
    desks = conn.execute("SELECT * FROM desks WHERE space_id=?", (space_id,)).fetchall()
    extra = db.rental_amenities(conn, space_id)
    desk_am = [db.desk_amenities(conn, d, extra) for d in desks]
    rows = []
    for a in config.AMENITIES:
        wanted = [r for r in near if a in db.split(r["amenities"])]
        zero = [r for r in wanted if r["result_count"] == 0]
        demand = len(wanted) / n if n else 0.0
        supply = sum(1 for am in desk_am if a in am) / len(desks) if desks else 0.0
        rows.append({
            "amenity": a, "searches": len(wanted), "zero_result_searches": len(zero),
            "demand_share": round(demand, 3), "supply_share": round(supply, 3),
            "gap": round(demand - supply, 3),                                   # R-INSIGHT-02
            "est_bookings_per_week": round(estimate(n / weeks, demand, supply, len(zero) / weeks), 2),
        })
    rows.sort(key=lambda r: -r["gap"])
    return sp, n, near, rows


def occupancy_by_hour(conn, space_id, days=config.INSIGHT_WINDOW_DAYS):
    """Average share of desks in use for each hour of the day over the window."""
    now = clock.now()
    since = now - timedelta(days=days)
    n_desks = conn.execute("SELECT COUNT(*) FROM desks WHERE space_id=?", (space_id,)).fetchone()[0]
    minutes = [0.0] * 24
    rows = conn.execute(
        "SELECT b.* FROM bookings b JOIN desks d ON d.id=b.desk_id WHERE d.space_id=? "
        "AND b.end_at > ? AND b.start_at < ? AND NOT (b.state='released' AND b.end_reason='no_show')",
        (space_id, clock.fmt(since), clock.fmt(now))).fetchall()
    for b in rows:
        s = max(clock.parse(b["start_at"]), since)
        e = min(clock.parse(b["ended_at"]) if b["ended_at"] else clock.parse(b["end_at"]),
                clock.parse(b["end_at"]), now)
        t = s
        while t < e:
            nxt = min(e, t.replace(minute=0, second=0) + timedelta(hours=1))
            minutes[t.hour] += (nxt - t).total_seconds() / 60
            t = nxt
    denom = n_desks * days * 60 if n_desks else 1
    return [{"hour": h, "occupancy": round(minutes[h] / denom, 3)} for h in range(24)]


def space_insights(conn, space_id):
    bookings.tick(conn)
    sp, n, near, table = demand_table(conn, space_id)
    by_amenity = {r["amenity"]: r for r in table}
    active = list_rentals(conn, space_id)["active"]
    active_features = {r["feature_id"] for r in active}
    recs = []
    for f in conn.execute("SELECT * FROM features ORDER BY id"):
        row = by_amenity.get(f["amenity"])
        if row is None or row["gap"] <= 0 or f["id"] in active_features:
            continue
        est = row["est_bookings_per_week"]
        recs.append({
            "feature_id": f["id"], "feature": f["name"], "amenity": f["amenity"],
            "gap": row["gap"], "est_bookings_per_week": est, "weekly_cost": f["weekly_cost"],
            "cost_per_extra_booking": round(f["weekly_cost"] / est, 2) if est > 0 else None,
        })
    recs.sort(key=lambda r: (-r["est_bookings_per_week"], r["weekly_cost"]))
    zero = sum(1 for r in near if r["result_count"] == 0)
    occ = occupancy_by_hour(conn, space_id)
    open_hours = [o for o in occ if sp["open_hour"] <= o["hour"] < sp["close_hour"]]
    return {
        "space_id": sp["id"], "space_name": sp["name"],
        "window_days": config.INSIGHT_WINDOW_DAYS, "radius_km": config.INSIGHT_RADIUS_KM,
        "kpis": {
            "nearby_searches": n,
            "zero_result_share": round(zero / n, 3) if n else 0.0,
            "avg_occupancy_open_hours": round(
                sum(o["occupancy"] for o in open_hours) / len(open_hours), 3) if open_hours else 0.0,
        },
        "demand_vs_supply": table,
        "occupancy_by_hour": occ,
        "recommendations": recs,
        "active_rentals": active,
    }


def list_rentals(conn, space_id):
    rows = conn.execute(
        "SELECT r.*, f.name, f.amenity, f.weekly_cost FROM rentals r "
        "JOIN features f ON f.id=r.feature_id WHERE r.space_id=? ORDER BY r.id", (space_id,)).fetchall()
    fmt = lambda r: {"id": r["id"], "feature_id": r["feature_id"], "feature": r["name"],
                     "amenity": r["amenity"], "weekly_cost": r["weekly_cost"],
                     "started_at": r["started_at"], "ended_at": r["ended_at"]}
    return {"active": [fmt(r) for r in rows if r["ended_at"] is None],
            "past": [fmt(r) for r in rows if r["ended_at"] is not None]}


def start_rental(conn, space_id, data):
    sp = db.space(conn, space_id)
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    feature = db.get(conn, "features", db.req_int(data, "feature_id"), "feature")
    if conn.execute("SELECT 1 FROM rentals WHERE space_id=? AND feature_id=? AND ended_at IS NULL",
                    (sp["id"], feature["id"])).fetchone():
        raise ApiError(f"{feature['name']} is already on trial here")
    cur = conn.execute("INSERT INTO rentals(space_id, feature_id, started_at) VALUES (?,?,?)",
                       (sp["id"], feature["id"], clock.fmt(clock.now())))
    return {"id": cur.lastrowid, "space_id": sp["id"], "feature_id": feature["id"],
            "feature": feature["name"], "amenity": feature["amenity"],
            "weekly_cost": feature["weekly_cost"], "started_at": clock.fmt(clock.now()),
            "note": f"every desk in {sp['name']} now counts as having {feature['amenity']}"}


def live_floor(conn, space_id):
    """Provider view: desks with puck state, today's bookings, utilization. No user identities."""
    from . import puck
    bookings.tick(conn)
    sp = db.space(conn, space_id)
    now = clock.now()
    extra = db.rental_amenities(conn, space_id)
    desks = []
    for d in conn.execute("SELECT * FROM desks WHERE space_id=? ORDER BY id", (space_id,)):
        p = conn.execute("SELECT * FROM pucks WHERE desk_id=?", (d["id"],)).fetchone()
        state = puck.state_for(conn, p)[0] if p else None
        desks.append({"desk_id": d["id"], "label": d["label"],
                      "amenities": sorted(db.desk_amenities(conn, d, extra)),
                      "puck_id": p["id"] if p else None,
                      "state": state["state"] if state else "no_puck",
                      "countdown_s": state["countdown_s"] if state else None,
                      "battery": p["battery"] if p else None})
    day = now.replace(hour=0, minute=0, second=0)
    opens = day + timedelta(hours=sp["open_hour"])
    closes = day + timedelta(hours=sp["close_hour"])
    today = conn.execute(
        "SELECT b.*, d.label FROM bookings b JOIN desks d ON d.id=b.desk_id "
        "WHERE d.space_id=? AND b.start_at < ? AND b.end_at > ? ORDER BY b.start_at",
        (space_id, clock.fmt(day + timedelta(days=1)), clock.fmt(day))).fetchall()
    used = 0.0
    for b in today:
        if b["state"] == "released" and b["end_reason"] == "no_show":
            continue
        s = max(clock.parse(b["start_at"]), opens)
        e = min(clock.parse(b["ended_at"]) if b["ended_at"] else clock.parse(b["end_at"]),
                clock.parse(b["end_at"]), closes)
        used += max(0.0, (e - s).total_seconds())
    capacity = len(desks) * max(1.0, (closes - opens).total_seconds())
    counts = {}
    for d in desks:
        counts[d["state"]] = counts.get(d["state"], 0) + 1
    return {
        "space": {k: sp[k] for k in sp.keys()},
        "now": clock.fmt(now),
        "open_now": db.space_open_for(sp, now, now + timedelta(minutes=1)),
        "desks": desks,
        "state_counts": counts,
        "today_bookings": [{"booking_id": b["id"], "desk_label": b["label"], "start": b["start_at"],
                            "end": b["end_at"], "state": b["state"], "end_reason": b["end_reason"]}
                           for b in today],
        "utilization_today": round(used / capacity, 3),
        "no_shows_today": sum(1 for b in today if b["end_reason"] == "no_show"),
    }
