"""Desk search (R-SEARCH-01/02), the space map (R-MAP-01/02) and saved preferences."""
from datetime import timedelta

from . import clock, config, db
from .errors import ApiError


# ---------- preferences (E-PREF) ----------

HOURS = ("morning", "midday", "afternoon", "evening")
PREF_SOURCES = ("onboarding", "search", "booking", "settings")


def get_prefs(conn, user_id):
    """A user's saved defaults. has_profile is False until the preference funnel has run."""
    row = conn.execute("SELECT * FROM preferences WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        return {"amenities": [], "work_mode": "focus", "max_km": config.DEFAULT_MAX_KM, "hours": [],
                "has_profile": False, "source": None, "updated_at": None}
    return {"amenities": sorted(db.split(row["amenities"])), "work_mode": row["work_mode"],
            "max_km": row["max_km"], "hours": sorted(db.split(row["hours"]), key=HOURS.index),
            "has_profile": row["source"] is not None, "source": row["source"], "updated_at": row["updated_at"]}


def save_prefs(conn, user_id, data):
    """PUT /api/users/{id}/preferences. Any field may be left out; source says which step of the
    funnel wrote it (onboarding, search, booking, settings)."""
    db.user(conn, user_id)
    if not isinstance(data, dict):
        raise ApiError("body must be a JSON object")
    current = get_prefs(conn, user_id)
    amenities = _amenities(data.get("amenities", current["amenities"]))
    mode = _mode(data.get("work_mode", current["work_mode"]))
    max_km = db.req_float(data, "max_km", current["max_km"])
    if not 0 < max_km <= 50:
        raise ApiError("max_km must be between 0 and 50")
    hours = data.get("hours", current["hours"])
    if not isinstance(hours, list) or any(h not in HOURS for h in hours):
        raise ApiError(f"hours must be a list of {', '.join(HOURS)}")
    source = data.get("source", "settings")
    if source not in PREF_SOURCES:
        raise ApiError(f"source must be one of {', '.join(PREF_SOURCES)}")
    conn.execute(
        "INSERT INTO preferences(user_id, amenities, work_mode, max_km, hours, source, updated_at)"
        " VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET amenities=excluded.amenities, work_mode=excluded.work_mode,"
        " max_km=excluded.max_km, hours=excluded.hours, source=excluded.source, updated_at=excluded.updated_at",
        (user_id, db.join(amenities), mode, max_km, db.join(hours), source, clock.fmt(clock.now())))
    return get_prefs(conn, user_id)


def _amenities(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError("amenities must be a list")
    unknown = [a for a in value if a not in config.AMENITIES]
    if unknown:
        raise ApiError(f"unknown amenity: {unknown[0]}")
    return list(dict.fromkeys(value))


def _mode(value):
    if value not in config.WORK_MODES:
        raise ApiError(f"work_mode must be one of {', '.join(config.WORK_MODES)}")
    return value


# ---------- query parsing ----------

def parse_query(conn, q):
    if not isinstance(q, dict):
        raise ApiError("body must be a JSON object")
    user = db.user(conn, db.req_int(q, "user_id"))
    prefs = get_prefs(conn, user["id"])
    origin = db.neighborhood(conn, q.get("neighborhood"))
    start = db.req_time(q, "start", default=clock.now())
    duration = db.req_float(q, "duration_hours", default=2)
    if not 0 < duration <= config.MAX_DURATION_HOURS:
        raise ApiError(f"duration_hours must be more than 0 and at most {config.MAX_DURATION_HOURS}")
    space_id = q.get("space_id")
    if space_id not in (None, ""):
        space_id = db.space(conn, db.req_int(q, "space_id"))["id"]
    else:
        space_id = None
    return {
        "user_id": user["id"], "prefs": prefs,
        "neighborhood": origin["name"], "lat": origin["lat"], "lon": origin["lon"],
        "start": start, "end": start + timedelta(hours=duration), "duration": duration,
        "amenities": _amenities(q.get("amenities", [])),
        "work_mode": _mode(q.get("work_mode") or prefs["work_mode"]),
        "max_km": prefs["max_km"], "space_id": space_id,
    }


# ---------- scoring (R-SEARCH-02) ----------

def score(distance_km, max_km, amenities, work_mode, noise, cost, profile_amenities):
    w = config.SCORE_WEIGHTS
    mode = config.WORK_MODES[work_mode]
    parts = {
        "distance": w["distance"] * max(0.0, 1 - distance_km / max_km),
        "mode": w["mode"] * len(set(mode["amenities"]) & amenities) / len(mode["amenities"]),
        "noise": w["noise"] * (1 - abs(noise - mode["noise"]) / 2),
        "cost": w["cost"] * (1 - min(cost, config.COST_CEILING) / config.COST_CEILING),
        "profile": (w["profile"] if not profile_amenities else
                    w["profile"] * len(set(profile_amenities) & amenities) / len(profile_amenities)),
    }
    parts = {k: round(v, 1) for k, v in parts.items()}
    return round(sum(parts.values()), 1), parts


# ---------- search ----------

def search(conn, q, log=True):
    """Hard filters (R-SEARCH-01), then rank 0-100 (R-SEARCH-02). Logs the search as demand."""
    qq = parse_query(conn, q)
    must = set(qq["amenities"])
    results = []
    for sp in conn.execute("SELECT * FROM spaces ORDER BY id").fetchall():
        if qq["space_id"] and sp["id"] != qq["space_id"]:
            continue
        dist = db.haversine_km(qq["lat"], qq["lon"], sp["lat"], sp["lon"])
        if not qq["space_id"] and dist > qq["max_km"]:
            continue
        if not db.space_open_for(sp, qq["start"], qq["end"]):
            continue
        extra = db.rental_amenities(conn, sp["id"])
        if qq["duration"] > sp["max_hours"]:                                 # R-SPACE-01
            continue
        for d in conn.execute("SELECT * FROM desks WHERE space_id=? AND bookable=1 ORDER BY id",
                              (sp["id"],)):                                    # R-SPACE-03
            am = db.desk_amenities(conn, d, extra)
            if not must <= am:
                continue
            if not db.desk_free(conn, d["id"], qq["start"], qq["end"]):
                continue
            total, parts = score(dist, qq["max_km"], am, qq["work_mode"], sp["noise"],
                                 sp["cost_per_hour"], qq["prefs"]["amenities"])
            results.append({
                "desk_id": d["id"], "desk_label": d["label"], "kind": d["kind"],
                "capacity": d["capacity"], "space_id": sp["id"],
                "space_name": sp["name"], "neighborhood": sp["neighborhood"],
                "distance_km": round(dist, 2), "amenities": sorted(am),
                "noise": sp["noise"], "cost_per_hour": sp["cost_per_hour"],
                "score": total, "score_parts": parts,
            })
    results.sort(key=lambda r: (-r["score"], r["distance_km"], r["desk_id"]))
    if log:
        conn.execute(
            "INSERT INTO searches(user_id, ts, neighborhood, lat, lon, start_at, duration_hours,"
            " amenities, work_mode, result_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (qq["user_id"], clock.fmt(clock.now()), qq["neighborhood"], qq["lat"], qq["lon"],
             clock.fmt(qq["start"]), qq["duration"], db.join(must), qq["work_mode"], len(results)))
    return {"start": clock.fmt(qq["start"]), "end": clock.fmt(qq["end"]),
            "work_mode": qq["work_mode"], "count": len(results), "results": results}


# ---------- map (R-MAP-01/02) ----------

def space_map(conn, q):
    """Every space with a status for the window and must-haves. Not logged as demand (R-MAP-02)."""
    qq = parse_query(conn, q)
    must = set(qq["amenities"])
    now = clock.now()
    out = []
    for sp in conn.execute("SELECT * FROM spaces ORDER BY id").fetchall():
        dist = db.haversine_km(qq["lat"], qq["lon"], sp["lat"], sp["lon"])
        extra = db.rental_amenities(conn, sp["id"])
        desks = conn.execute("SELECT * FROM desks WHERE space_id=? AND bookable=1", (sp["id"],)).fetchall()
        matching = [d for d in desks if must <= db.desk_amenities(conn, d, extra)]
        open_window = db.space_open_for(sp, qq["start"], qq["end"]) and qq["duration"] <= sp["max_hours"]
        free = [d for d in matching if open_window
                and db.desk_free(conn, d["id"], qq["start"], qq["end"])]
        open_now = db.space_open_for(sp, now, now + timedelta(minutes=1))
        free_now = [d for d in matching if open_now
                    and db.desk_free(conn, d["id"], now, now + timedelta(minutes=1))]
        if not open_window:
            status = "closed"
        elif not matching:
            status = "no_match"
        elif len(free) >= config.MAP_AVAILABLE_MIN:
            status = "available"
        elif len(free) == 1:
            status = "limited"
        else:
            status = "full"
        out.append({
            "space_id": sp["id"], "name": sp["name"], "kind": sp["kind"],
            "neighborhood": sp["neighborhood"], "lat": sp["lat"], "lon": sp["lon"],
            "open_hour": sp["open_hour"], "close_hour": sp["close_hour"],
            "noise": sp["noise"], "cost_per_hour": sp["cost_per_hour"],
            "desks": len(desks), "matching_desks": len(matching),
            "max_hours": sp["max_hours"], "food": bool(sp["food"]),
            "amenities": sorted(set().union(*[db.desk_amenities(conn, d, extra) for d in desks]) if desks else []),
            "free": len(free), "free_now": len(free_now), "status": status,
            "distance_km": round(dist, 2), "in_range": dist <= qq["max_km"],
        })
    return {"origin": {"name": qq["neighborhood"], "lat": qq["lat"], "lon": qq["lon"]},
            "max_km": qq["max_km"], "start": clock.fmt(qq["start"]),
            "end": clock.fmt(qq["end"]), "spaces": out}
