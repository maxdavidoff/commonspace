"""Deterministic sample data: 6 Pittsburgh spaces, 50 desks and pucks, 40 users, 6 weeks of history.

History is generated relative to clock.now(), so the insights screen always has a full
28-day window. A planted gap (phone booths and printers wanted, rarely offered) gives the
recommendations something to find.
"""
import random
from datetime import timedelta

from . import clock, config, db

NEIGHBORHOODS = [
    ("Bloomfield", 40.4610, -79.9490), ("Oakland", 40.4418, -79.9561),
    ("Lawrenceville", 40.4651, -79.9606), ("Shadyside", 40.4517, -79.9335),
    ("Downtown", 40.4406, -79.9959), ("Squirrel Hill", 40.4384, -79.9228),
    ("Strip District", 40.4521, -79.9790),
]

# name, kind, neighborhood, lat, lon, open, close, noise, $/h, bookable desks, max_hours, food, floor
SPACES = [
    ("Creative Chem Co.", "cafe", "Bloomfield", 40.4615, -79.9480, 7, 18, 2, 3.0, 10, 4, 1, "Main floor"),
    ("Carnegie Library Oakland", "library", "Oakland", 40.4433, -79.9500, 9, 20, 1, 0.0, 9, 4, 1, "Floor 2"),
    ("Lawrenceville Commons", "coworking", "Lawrenceville", 40.4660, -79.9590, 0, 24, 2, 6.0, 8, 8, 1, "Floor 1"),
    ("Walnut Street Cafe", "cafe", "Shadyside", 40.4520, -79.9330, 7, 17, 3, 3.0, 8, 3, 1, "Main floor"),
    ("Market Square Hub", "coworking", "Downtown", 40.4410, -79.9965, 8, 22, 2, 5.0, 8, 6, 0, "Floor 3"),
    ("Squirrel Hill Reading Room", "library", "Squirrel Hill", 40.4380, -79.9220, 9, 21, 1, 2.0, 7, 4, 0, "Floor 1"),
]

# bookable shapes per space, laid out on a 4-column grid: (kind, capacity) in order; the rest are desks
LAYOUT_EXTRAS = {1: [("pod", 2)], 2: [("room", 6), ("pod", 2), ("pod", 2)], 3: [("room", 8), ("pod", 2)],
                 4: [], 5: [("room", 10), ("pod", 2)], 6: [("pod", 2)]}

FEATURES = [
    ("Phone booth pod", "phone_booth", 45.0),
    ("Printer and scanner station", "printer", 25.0),
    ("Acoustic desk dividers", "quiet", 30.0),
    ("Rolling whiteboards", "whiteboard", 15.0),
    ("Monitor kit", "monitor", 35.0),
    ("Standing desk converters", "standing", 20.0),
    ("Power strip kit", "outlet", 10.0),
]

# probability a search asks for each amenity (the planted demand)
DEMAND = {"outlet": 0.55, "quiet": 0.30, "phone_booth": 0.22, "printer": 0.12,
          "large_table": 0.12, "whiteboard": 0.10, "monitor": 0.12, "standing": 0.05}

FIRST = ["Ana", "Ben", "Cy", "Dana", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun", "Kai", "Lena",
         "Max", "Nia", "Oto", "Pia", "Quin", "Rae", "Sol", "Tia"]
LAST = ["Park", "Ng", "Ortiz", "Shah", "Kim", "Reyes", "Cole", "Haas"]


def _desk_amenities(rng, kind, noise, i):
    am = set()
    if rng.random() < 0.75:
        am.add("outlet")
    if noise == 1 or (noise == 2 and rng.random() < 0.3):
        am.add("quiet")
    if rng.random() < 0.25:
        am.add("large_table")
    if rng.random() < 0.10:
        am.add("whiteboard")
    if rng.random() < 0.15:
        am.add("monitor")
    if rng.random() < 0.08:
        am.add("standing")
    if kind == "coworking" and i < 1:
        am.add("phone_booth")
    if kind == "library" and rng.random() < 0.5:
        am.add("printer")
    return am


def seed(conn, rng_seed=42):
    conn.execute("BEGIN")
    try:
        _seed(conn, rng_seed)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _seed(conn, rng_seed):
    rng = random.Random(rng_seed)
    now = clock.now()
    conn.executemany("INSERT INTO neighborhoods VALUES (?,?,?)", NEIGHBORHOODS)
    for f in FEATURES:
        conn.execute("INSERT INTO features(name, amenity, weekly_cost) VALUES (?,?,?)", f)

    desk_id = 0
    for sid, (name, kind, hood, lat, lon, o, c, noise, cost, n, maxh, food, floor) in enumerate(SPACES, start=1):
        conn.execute(
            "INSERT INTO spaces(id, name, kind, neighborhood, lat, lon, open_hour, close_hour, noise,"
            " cost_per_hour, max_hours, food, walkins, lockers, pickup, floor_label)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, name, kind, hood, lat, lon, o, c, noise, cost, maxh, food, 1, 1 if kind != "cafe" else 0,
             "Reference desk" if kind == "library" else "Front desk", floor))
        extras = LAYOUT_EXTRAS[sid]
        n_desks = n - len(extras)
        shapes = [("desk", 1)] * n_desks + extras
        # lay shapes on a 4-column grid; a room takes 2 columns
        col = row = 0
        positions = []
        for dk, cap in shapes:
            width = 2 if dk == "room" else 1
            if col + width > 4:
                col, row = 0, row + 1
            positions.append((col, row))
            col += width
        for i, (dk, cap) in enumerate(shapes):
            desk_id += 1
            label = (f"T{i + 1}" if dk == "desk" else
                     f"Pod {chr(65 + sum(1 for x in shapes[:i] if x[0] == 'pod'))}" if dk == "pod" else
                     f"Room {i + 1}")
            am = _desk_amenities(rng, kind, noise, i)
            if dk == "room":
                am |= {"large_table", "whiteboard", "phone_booth"}
            if dk == "pod":
                am |= {"phone_booth", "outlet"}
            conn.execute("INSERT INTO desks(id, space_id, label, amenities, kind, capacity, col, row)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (desk_id, sid, label, db.join(am), dk, cap, *positions[i]))
            conn.execute("INSERT INTO pucks(id, desk_id, battery, last_heartbeat) VALUES (?,?,?,?)",
                         (desk_id, desk_id, rng.randint(35, 100), clock.fmt(now)))
        # non-bookable shapes from the scan: pickup point and lockers (no pucks)
        last_row = positions[-1][1] + 1
        desk_id += 1
        conn.execute("INSERT INTO desks(id, space_id, label, kind, capacity, col, row, bookable)"
                     " VALUES (?,?,?,?,?,?,?,0)", (desk_id, sid, "Pickup", "pickup", 1, 0, last_row))
        if kind != "cafe":
            desk_id += 1
            conn.execute("INSERT INTO desks(id, space_id, label, kind, capacity, col, row, bookable)"
                         " VALUES (?,?,?,?,?,?,?,0)", (desk_id, sid, "Lockers", "locker", 1, 1, last_row))

    for uid in range(1, 41):
        conn.execute("INSERT INTO users(id, name, phone) VALUES (?,?,?)",
                     (uid, f"{FIRST[(uid - 1) % 20]} {LAST[(uid - 1) // 5 % 8]}",
                      f"412555{uid:04d}" if uid % 3 else None))
        prefs = [a for a in ("outlet", "quiet", "phone_booth") if rng.random() < DEMAND[a]]
        conn.execute("INSERT INTO preferences VALUES (?,?,?,?)",
                     (uid, db.join(prefs), rng.choice(list(config.WORK_MODES)),
                      rng.choice([2.0, 3.0, 3.0, 5.0])))

    spaces = conn.execute("SELECT * FROM spaces").fetchall()
    desks = conn.execute("SELECT * FROM desks WHERE bookable=1").fetchall()
    _history_searches(conn, rng, now, spaces, desks)
    _history_bookings(conn, rng, now, spaces, desks)
    _current_bookings(conn, now, spaces)


def _history_searches(conn, rng, now, spaces, desks):
    by_space = {}
    for d in desks:
        by_space.setdefault(d["space_id"], []).append(db.split(d["amenities"]))
    hoods = NEIGHBORHOODS
    rows = []
    for day in range(42, 0, -1):
        date = (now - timedelta(days=day)).replace(minute=0, second=0)
        for _ in range(rng.randint(24, 36)):
            name, lat, lon = rng.choice(hoods)
            start = date.replace(hour=rng.choice([8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19]))
            dur = rng.choice([1, 2, 2, 3, 4])
            must = {a for a, p in DEMAND.items() if rng.random() < p * 0.8}
            mode = rng.choice(list(config.WORK_MODES))
            end = start + timedelta(hours=dur)
            count = 0
            for sp in spaces:
                if db.haversine_km(lat, lon, sp["lat"], sp["lon"]) > config.DEFAULT_MAX_KM:
                    continue
                if not db.space_open_for(sp, start, end):
                    continue
                count += sum(1 for am in by_space[sp["id"]] if must <= am)
            ts = start - timedelta(minutes=rng.randint(5, 90))
            rows.append((rng.randint(1, 40), clock.fmt(ts), name, lat, lon, clock.fmt(start),
                         dur, db.join(must), mode, count))
    conn.executemany(
        "INSERT INTO searches(user_id, ts, neighborhood, lat, lon, start_at, duration_hours,"
        " amenities, work_mode, result_count) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _history_bookings(conn, rng, now, spaces, desks):
    sp_by_id = {s["id"]: s for s in spaces}
    rows = []
    for day in range(42, 0, -1):
        date = (now - timedelta(days=day)).replace(hour=0, minute=0, second=0)
        weekday = date.weekday() < 5
        for d in desks:
            sp = sp_by_id[d["space_id"]]
            o, c = max(sp["open_hour"], 7), min(sp["close_hour"], 21)
            t = date + timedelta(hours=o)
            close = date + timedelta(hours=c)
            busy = 0.55 if weekday else 0.3
            while t < close - timedelta(hours=1):
                if rng.random() < busy:
                    dur = timedelta(hours=rng.choice([1, 2, 2, 3]))
                    end = min(t + dur, close)
                    roll = rng.random()
                    if roll < 0.08:
                        state, reason, ended = "released", "no_show", t + timedelta(minutes=15)
                    elif roll < 0.12:
                        state, reason, ended = "released", "away_timeout", end - timedelta(minutes=30)
                    else:
                        state, reason, ended = "ended", rng.choice(["user", "expired"]), end
                    walkin = roll >= 0.12 and rng.random() < 0.3
                    rows.append((rng.randint(1, 40), d["id"], clock.fmt(t), clock.fmt(end), state,
                                 clock.fmt(t - timedelta(hours=1)), reason, clock.fmt(ended),
                                 "walkin" if walkin else "prebook",
                                 None if reason == "no_show" else clock.fmt(t + timedelta(minutes=rng.randint(0, 9))),
                                 f"{rng.randint(1, 24):02d}" if not walkin and reason != "no_show" else None))
                    t = end + timedelta(minutes=rng.choice([0, 30, 60]))
                else:
                    t += timedelta(hours=1)
    conn.executemany(
        "INSERT INTO bookings(user_id, desk_id, start_at, end_at, state, created_at, end_reason,"
        " ended_at, kind, checked_in_at, coaster) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def _current_bookings(conn, now, spaces):
    """A few live bookings so the floor and device wall show every state right away."""
    plans = [(-40, 120, "checked_in", None), (5, 90, "reserved", None),
             (-30, 120, "away", -6), (-60, 180, "checked_in", None)]
    user = 1
    for sp in spaces:
        if not db.space_open_for(sp, now - timedelta(hours=1), now + timedelta(hours=3)):
            continue
        sp_desks = conn.execute("SELECT id FROM desks WHERE space_id=? AND kind='desk' ORDER BY id LIMIT 4",
                                (sp["id"],)).fetchall()
        for d, (s_off, mins, state, away_off) in zip(sp_desks, plans):
            start = now + timedelta(minutes=s_off)
            conn.execute(
                "INSERT INTO bookings(user_id, desk_id, start_at, end_at, state, created_at,"
                " checked_in_at, away_since) VALUES (?,?,?,?,?,?,?,?)",
                (user, d["id"], clock.fmt(start), clock.fmt(start + timedelta(minutes=mins)), state,
                 clock.fmt(start - timedelta(hours=1)),
                 clock.fmt(start) if state != "reserved" else None,
                 clock.fmt(now + timedelta(minutes=away_off)) if away_off else None))
            if state != "reserved":
                conn.execute("UPDATE bookings SET coaster=? WHERE rowid=last_insert_rowid()", (f"{user:02d}",))
            user += 1
