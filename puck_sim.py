"""Simulated puck firmware.

    python3 puck_sim.py --demo                 # scripted walk through every puck state (no server needed)
    python3 puck_sim.py --space 3              # fleet of pucks in space 3 heartbeating to a running server
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime

from commonspace import bookings, clock, db, puck, seed

ICON = {"free": "🟢", "reserved": "🟠", "occupied": "🔴", "away": "🔵"}


def show(step, led):
    cd = led["countdown_s"]
    timer = f"  {cd // 60}m{cd % 60:02d}s" if cd is not None else ""
    print(f"  {ICON[led['state']]} {led['state']:<9} {led['color']:>6} {led['pattern']:<6}{timer:<10} {step}")


def demo():
    """Run the whole lifecycle in-process with a fixed clock, so it always works."""
    clock.set_fixed(datetime(2026, 11, 18, 10, 0))
    conn = db.connect(":memory:")
    seed.seed(conn)
    desk = conn.execute(
        "SELECT d.id FROM desks d WHERE d.space_id=3 AND d.id NOT IN "
        "(SELECT desk_id FROM bookings WHERE state IN ('reserved','checked_in','away')) "
        "ORDER BY d.id LIMIT 1").fetchone()["id"]
    pid = conn.execute("SELECT id FROM pucks WHERE desk_id=?", (desk,)).fetchone()["id"]
    user = 40
    hb = lambda: puck.heartbeat(conn, pid, {"battery": 88})
    print(f"Puck {pid} on desk {desk} at Lawrenceville Commons, user {user}. Sim time 10:00.\n")

    show("no booking", hb())
    b = bookings.create(conn, {"user_id": user, "desk_id": desk,
                               "start": "2026-11-18T10:05", "duration_hours": 1})
    show("booked 10:05-11:05 in the app (inside the 10 min check-in window)", hb())
    show("tap -> " + puck.gesture(conn, pid, {"user_id": user, "kind": "tap"})["action"], hb())
    show("tap -> " + puck.gesture(conn, pid, {"user_id": user, "kind": "tap"})["action"], hb())
    clock.advance(10)
    show("10 minutes later, still away", hb())
    show("tap -> " + puck.gesture(conn, pid, {"user_id": user, "kind": "tap"})["action"], hb())
    puck.gesture(conn, pid, {"user_id": user, "kind": "tap"})
    clock.advance(21)
    show("stepped away again and did not come back for 21 min -> released (away timeout)", hb())

    bookings.create(conn, {"user_id": user, "desk_id": desk,
                           "start": clock.fmt(clock.now()), "duration_hours": 1})
    show("new booking starting now", hb())
    clock.advance(16)
    show("16 minutes, never checked in -> released (no-show)", hb())

    b = bookings.create(conn, {"user_id": user, "desk_id": desk,
                               "start": clock.fmt(clock.now()), "duration_hours": 1})
    puck.gesture(conn, pid, {"user_id": user, "kind": "tap"})
    show("booked and checked in", hb())
    r = bookings.act(conn, b["id"], "extend", {"user_id": user})
    show(f"extended in the app (+30 min, {r['extensions_left']} left)", hb())
    show("hold -> " + puck.gesture(conn, pid, {"user_id": user, "kind": "hold"})["action"], hb())
    print("\nDevice log (newest first):")
    for e in puck.events(conn, pid, 6)["events"]:
        print(f"  {e['ts']}  {e['kind']:<9} {e['detail']}")


def _call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def fleet(base, space_id, interval):
    floor = _call(base, "GET", f"/api/spaces/{space_id}")
    pucks = [d["puck_id"] for d in floor["desks"] if d["puck_id"]]
    battery = {p: random.randint(60, 100) for p in pucks}
    last = {}
    print(f"{len(pucks)} pucks in {floor['space']['name']} heartbeating every {interval}s. Ctrl+C to stop.")
    while True:
        for p in pucks:
            battery[p] = max(0, battery[p] - random.choice([0, 0, 0, 1]))
            led = _call(base, "POST", f"/api/device/pucks/{p}/heartbeat", {"battery": battery[p]})
            if last.get(p) != led["state"]:
                print(f"{datetime.now():%H:%M:%S} puck {p:>3}: {ICON[led['state']]} {led['state']}"
                      f"{'  LOW BATTERY' if led['low_battery'] else ''}")
                last[p] = led["state"]
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Simulated CommonSpace pucks")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--space", type=int)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--interval", type=float, default=5)
    args = ap.parse_args()
    if args.demo:
        demo()
    elif args.space:
        try:
            fleet(args.server, args.space, args.interval)
        except urllib.error.URLError as e:
            print(f"Could not reach {args.server}: {e}. Is server.py running?")
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
