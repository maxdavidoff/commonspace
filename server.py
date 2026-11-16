"""CommonSpace HTTP server: REST API, the three web pages, and a background timer thread.

    python3 server.py             # seeds sample data on first run, serves on :8000
    python3 server.py --reset     # wipe and re-seed
"""
import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from commonspace import bookings, clock, config, db, insights, matching, puck, seed, spaces
from commonspace.errors import ApiError

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
PAGES = {"/app": "app.html", "/provider": "provider.html", "/pucks": "pucks.html",
         "/floor": "floor.html", "/room": "room.html", "/t": "t.html"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript"}


class App:
    """Routes a (method, path, body) to the right module. Thread-safe via one lock."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.RLock()
        c = conn
        self.routes = [
            ("GET", r"/api/meta", lambda m, b: self.meta()),
            ("POST", r"/api/search", lambda m, b: matching.search(c, b)),
            ("POST", r"/api/map", lambda m, b: matching.space_map(c, b)),
            ("POST", r"/api/bookings", lambda m, b: bookings.create(c, b)),
            ("POST", r"/api/bookings/(\d+)/(checkin|away|return|extend|end)",
             lambda m, b: bookings.act(c, int(m[1]), m[2], b)),
            ("GET", r"/api/users/(\d+)/bookings", lambda m, b: bookings.user_bookings(c, int(m[1]))),
            ("GET", r"/api/users/(\d+)/preferences",
             lambda m, b: matching.get_prefs(c, db.user(c, int(m[1]))["id"])),
            ("PUT", r"/api/users/(\d+)/preferences", lambda m, b: matching.save_prefs(c, int(m[1]), b)),
            ("POST", r"/api/users/phone", lambda m, b: spaces.phone_user(c, b)),
            ("GET", r"/api/desks/(\d+)/status", lambda m, b: spaces.desk_status(c, int(m[1]))),
            ("POST", r"/api/desks/(\d+)/hold", lambda m, b: spaces.hold_desk(c, int(m[1]), b)),
            ("PUT", r"/api/desks/(\d+)", lambda m, b: spaces.tag_desk(c, int(m[1]), b)),
            ("POST", r"/api/bookings/(\d+)/handoff", lambda m, b: spaces.hand_off(c, int(m[1]), b)),
            ("POST", r"/api/bookings/(\d+)/return_coaster", lambda m, b: spaces.coaster_return(c, int(m[1]), b)),
            ("GET", r"/api/spaces/(\d+)", lambda m, b: insights.live_floor(c, int(m[1]))),
            ("GET", r"/api/spaces/(\d+)/settings", lambda m, b: spaces.settings(c, int(m[1]))),
            ("PUT", r"/api/spaces/(\d+)/settings", lambda m, b: spaces.save_settings(c, int(m[1]), b)),
            ("GET", r"/api/spaces/(\d+)/floor", lambda m, b: spaces.floor_plan(c, int(m[1]))),
            ("GET", r"/api/spaces/(\d+)/dashboard", lambda m, b: spaces.dashboard(c, int(m[1]), (b or {}).get("period", "today"))),
            ("GET", r"/api/spaces/(\d+)/staff/bookings", lambda m, b: spaces.staff_lookup(c, int(m[1]), (b or {}).get("q"))),
            ("GET", r"/api/spaces/(\d+)/insights", lambda m, b: insights.space_insights(c, int(m[1]))),
            ("GET", r"/api/spaces/(\d+)/rentals", lambda m, b: insights.list_rentals(c, int(m[1]))),
            ("POST", r"/api/spaces/(\d+)/rentals", lambda m, b: insights.start_rental(c, int(m[1]), b)),
            ("GET", r"/api/pucks", lambda m, b: puck.all_pucks(c)),
            ("GET", r"/api/device/pucks/(\d+)", lambda m, b: puck.led(c, int(m[1]))),
            ("POST", r"/api/device/pucks/(\d+)/heartbeat", lambda m, b: puck.heartbeat(c, int(m[1]), b)),
            ("POST", r"/api/device/pucks/(\d+)/gesture", lambda m, b: puck.gesture(c, int(m[1]), b)),
            ("GET", r"/api/device/pucks/(\d+)/events", lambda m, b: puck.events(c, int(m[1]))),
            ("POST", r"/api/dev/advance", lambda m, b: self.advance(b)),
        ]
        self.routes = [(meth, re.compile(pat + r"/?$"), fn) for meth, pat, fn in self.routes]

    def meta(self):
        c = self.conn
        return {
            "now": clock.fmt(clock.now()), "sim_offset_minutes": clock.offset_minutes(),
            "neighborhoods": [dict(r) for r in c.execute("SELECT * FROM neighborhoods")],
            "amenities": config.AMENITIES, "work_modes": list(config.WORK_MODES),
            "users": [dict(r) for r in c.execute("SELECT id, name FROM users ORDER BY id")],
            "spaces": [dict(r) for r in c.execute(
                "SELECT id, name, kind, neighborhood, open_hour, close_hour, max_hours, food, walkins,"
                " lockers, pickup, floor_label, cost_per_hour FROM spaces ORDER BY id")],
            "config": {"away_minutes": config.AWAY_MINUTES, "no_show_minutes": config.NO_SHOW_MINUTES,
                       "extend_minutes": config.EXTEND_MINUTES, "max_extensions": config.MAX_EXTENSIONS,
                       "checkin_early_minutes": config.CHECKIN_EARLY_MINUTES},
            "features": [dict(r) for r in c.execute("SELECT * FROM features ORDER BY id")],
        }

    def advance(self, body):
        minutes = db.req_float(body if isinstance(body, dict) else {}, "minutes")
        if not 0 < minutes <= 24 * 60:
            raise ApiError("minutes must be between 0 and 1440")
        clock.advance(minutes)
        released = bookings.tick(self.conn)
        return {"now": clock.fmt(clock.now()), "sim_offset_minutes": clock.offset_minutes(),
                "released": [{"booking_id": b, "reason": r} for b, r in released]}

    def dispatch(self, method, path, body):
        if "?" in path:
            from urllib.parse import parse_qsl
            path, qs = path.split("?", 1)
            if method == "GET":
                body = dict(parse_qsl(qs))
        matched_path = False
        for meth, rx, fn in self.routes:
            m = rx.match(path)
            if not m:
                continue
            matched_path = True
            if meth != method:
                continue
            with self.lock:
                try:
                    return 200, fn(m, body)
                except ApiError as e:
                    return e.status, {"error": str(e)}
        if matched_path:
            return 405, {"error": f"{method} not allowed on {path}"}
        return 404, {"error": f"no route for {path}"}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise ApiError("body is not valid JSON")

        def _api(self, method):
            try:
                body = self._body() if method in ("POST", "PUT") else {}
            except ApiError as e:
                return self._send(400, {"error": str(e)})
            status, payload = app.dispatch(method, self.path, body)
            self._send(status, payload)

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path.startswith("/api/"):
                return self._api("GET")
            m = re.match(r"^/t/(\d+)/?$", path)
            if m:
                return self._redirect(f"/web/t.html?desk={m[1]}")
            if path == "/":
                return self._redirect("/web/app.html")
            if path in PAGES:
                return self._redirect(f"/web/{PAGES[path]}" + (f"?{qs}" if qs else ""))
            name = path[len("/web/"):] if path.startswith("/web/") else None
            if name and "/" not in name and ".." not in name:
                full = os.path.join(WEB, name)
                if os.path.isfile(full):
                    with open(full, "rb") as f:
                        return self._send(200, f.read(),
                                          TYPES.get(os.path.splitext(name)[1], "text/plain"))
            self._send(404, {"error": f"no page at {path}"})

        def _redirect(self, to):
            self.send_response(302)
            self.send_header("Location", to)
            self.end_headers()

        def do_POST(self):
            self._api("POST")

        def do_PUT(self):
            self._api("PUT")

    return Handler


def make_server(conn, host="127.0.0.1", port=8000):
    app = App(conn)
    return ThreadingHTTPServer((host, port), make_handler(app)), app


def start_timer(app, interval=2.0):
    def loop():
        while True:
            time.sleep(interval)
            with app.lock:
                bookings.tick(app.conn)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description="CommonSpace backend")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=os.path.join(HERE, "commonspace.db"))
    ap.add_argument("--reset", action="store_true", help="wipe the database and re-seed")
    args = ap.parse_args()
    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
    conn = db.connect(args.db)
    if db.is_empty(conn):
        seed.seed(conn)
        print("Seeded sample data.")
    httpd, app = make_server(conn, args.host, args.port)
    start_timer(app)
    base = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    print(f"CommonSpace running.\n  Worker app:  {base}/app\n  Provider:    {base}/provider\n"
          f"  Puck wall:   {base}/pucks\nCtrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
