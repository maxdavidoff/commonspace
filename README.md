# CommonSpace backend

CommonSpace lets independent workers find and hold a desk in existing cafes, libraries and coworking spaces. An IoT **desk puck** on every desk shows whether the desk is free and enforces the hold. Every search and booking feeds **demand insights** that tell each space which add-ons to trial.

Everything runs on the Python 3.10+ standard library: no `pip install` needed.

The shared spec lives in the **CommonSpace Hub** in Notion (workspace "CommonSpace Common Context"). Rule IDs in code comments and test names (`R-BOOK-02`, `R-INSIGHT-03`, ...) match rows in its Rules database. All tunable constants are in `commonspace/config.py`, each tagged with its rule ID.

## Run it

```bash
python3 server.py             # seeds sample data on first run, serves on :8000
python3 server.py --reset     # wipe and re-seed
python3 server.py --port 8080 --db /tmp/cs.db
```

Then open:

| URL | Who it's for | Wireframe |
|---|---|---|
| http://localhost:8000/app | Independent worker, phone app: find a space → rules → pick a table and time → phone for updates → confirmed → arrive → tap coaster to check in → locker → in session (extend, step away, away prompt) → wrap-up checklist | Pre-book flow |
| http://localhost:8000/t/4 | Walk-in, phone browser after scanning the table mat QR: free until, hold with a phone number, SMS when a booking is coming, wrap up | Walk-in |
| http://localhost:8000/floor?space=2 | Floor overview display at the entrance: open desks, booked soon, scan to hold | Floor display |
| http://localhost:8000/room?space=2 | Room and pod displays that replace the whiteboard | Room display |
| http://localhost:8000/provider?space=2 | Space provider: dashboard (check-ins, no-shows, walk-ins, unmet searches, needs attention), tag bookable zones, set rules and publish, staff desk hand-off and coaster return | Provider flow |
| http://localhost:8000/pucks | Coaster wall: every coaster's light and countdown; tap or hold as any user | Physical touchpoints |

The sim-time buttons in the header (+5m, +15m, +1h) fast-forward the clock so you can watch away prompts, no-show releases and coaster timers happen.

Sample data is generated relative to the current time. Only Lawrenceville Commons is open 24 hours, so late at night it is the only space with live bookings; during the day all six spaces are active.

Lockers, food orders and NFC are simulated in the UI (there is no hardware). "Tap coaster" calls the puck gesture endpoint, so the coaster wall, floor display and provider dashboard all update from the same event.

## Live demo (no install)

https://commonspace-app.vercel.app (Vercel, deploys on every push to `main`) and https://maxdavidoff.github.io/commonspace/ run the same Python backend inside the browser through Pyodide, seeded with the sample data. Bookings live in that tab only. Rebuild it after changing code:

```bash
python3 build_demo.py     # writes docs/, which GitHub Pages serves
```

In a second terminal:

```bash
python3 puck_sim.py --demo         # scripted walk through every puck state (no server needed)
python3 puck_sim.py --space 3      # fleet of simulated pucks heartbeating to the running server
python3 -m unittest -v             # 79 tests: search, map, booking rules, timers, puck, insights, seed, HTTP API
```

## Layout

```
server.py                 HTTP server, REST routes, background timer thread
puck_sim.py               simulated puck firmware (heartbeat + gestures) and scripted demo
commonspace/
  config.py               every constant, tagged with its Notion rule ID
  errors.py               ApiError (-> HTTP 400) and NotFound (-> 404)
  clock.py                simulated clock (real time or fixed base, plus offset)
  db.py                   SQLite schema and shared helpers
  matching.py             search filter + 0-100 score, space map, saved preferences
  bookings.py             booking rules and lifecycle state machine (with extend), timers
  puck.py                 LED state derivation and tap/hold interpretation
  insights.py             demand gap, add-on ROI, rentals, live floor, occupancy by hour
  spaces.py               space rules (B2), desk tagging, phone sign-in, walk-in holds, staff desk, dashboard
  seed.py                 deterministic sample data (6 spaces, 50 desks/pucks, 40 users, 6 weeks history)
web/                      app, t (walk-in), floor, room, provider, pucks pages; common.css, api.js, demo.js
build_demo.py             builds docs/ (static demo for GitHub Pages)
tests/test_commonspace.py unittest suite
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/meta` | sim time, neighborhoods, amenities, work modes, users, spaces, add-on catalog |
| POST | `/api/search` | `{user_id, neighborhood, start, duration_hours, amenities[], work_mode, space_id?}` → ranked desks |
| POST | `/api/map` | same inputs → every space with status, free for the window, free now, distance, in range |
| POST | `/api/bookings` | `{user_id, desk_id, start, duration_hours}` → reserved booking |
| POST | `/api/bookings/{id}/checkin \| away \| return \| extend \| end` | lifecycle actions `{user_id}` |
| GET | `/api/users/{id}/bookings` | open bookings with live countdowns |
| GET / PUT | `/api/users/{id}/preferences` | default must-haves, work mode, max distance |
| GET | `/api/spaces/{id}` | live floor (puck states), today's bookings, utilization. No user identities. |
| GET | `/api/spaces/{id}/insights` | demand vs supply, occupancy by hour, recommendations |
| GET / POST | `/api/spaces/{id}/rentals` | list trials / start one `{feature_id}` |
| GET / PUT | `/api/spaces/{id}/settings` | space rules: max hours, food, walk-ins, lockers, pickup point, hours, min purchase (R-SPACE-01..04) |
| GET | `/api/spaces/{id}/floor` | every shape on the floor plan with live state (desks, pods, rooms, pickup, lockers) |
| PUT | `/api/desks/{id}` | tag a shape: kind, capacity, bookable, amenities |
| GET | `/api/desks/{id}/status` | table mat page: free until, next booking, rules |
| POST | `/api/desks/{id}/hold` | `{phone}` walk-in hold until the next booking or closing |
| POST | `/api/users/phone` | `{phone, code?}` sign in or create an account by phone (demo code 000000) |
| GET | `/api/spaces/{id}/staff/bookings?q=` | staff desk: today's bookings by name or table |
| POST | `/api/bookings/{id}/handoff` | `{coaster}` staff hands over a coaster and checks the worker in |
| POST | `/api/bookings/{id}/return_coaster` | returning the coaster ends the session |
| GET | `/api/spaces/{id}/dashboard?period=today\|week\|month` | check-ins, no-shows, walk-ins, bookings vs check-ins by hour, unmet searches, needs attention |
| GET | `/api/pucks` | every puck's current LED state (device wall) |
| GET | `/api/device/pucks/{id}` | one puck's LED state |
| POST | `/api/device/pucks/{id}/heartbeat` | `{battery}` → what the LED should show |
| POST | `/api/device/pucks/{id}/gesture` | `{user_id, kind: tap\|hold}` |
| GET | `/api/device/pucks/{id}/events` | device audit log |
| POST | `/api/dev/advance` | `{minutes}` fast-forward the sim clock; returns released bookings |

Times are `YYYY-MM-DDTHH:MM` (or with a space). Invalid input returns HTTP 400 with `{"error": "..."}`; unknown IDs return 404.

## Key rules

* **Search (R-SEARCH-01/02):** hard filters first (space open for the whole window, desk free, every must-have present, within max distance), then a 0-100 score: distance 30 · work-mode amenity fit 30 · noise fit 20 · cost 10 · saved-profile match 10.
* **Holds (R-BOOK-01..04):** no-show release 15 min after start; away allowance 20 min; max 2 open bookings per user; extend +30 min up to twice if nobody has the desk next and the space is still open. Check-in opens 10 min before the start.
* **Puck:** free = green solid, reserved = amber pulse (no-show countdown), occupied = red solid (time left), away = blue blink (away countdown). Tap: check in → step away → return. Hold: end or cancel. Only the booking's owner can act; the server holds booking truth (R-PUCK-07).
* **Map (R-MAP-01/02):** each space gets available (2+ free), limited (1), full, no_match or closed. Map views are not logged as demand. "Show desks here" searches one space even outside the radius.
* **Preference intake funnel:** a new user's first open starts a three-step set-up (how you work, must-haves, distance and hours), skippable at every step; it is offered again after their first booking, pre-filled from that search. Each step is logged (`POST /api/users/{id}/funnel {step, action, entry}`) and `GET /api/funnel` reports distinct users reaching each step, skips per step, completion rate, entry point and profiles by source. `PUT /api/users/{id}/preferences` records which path wrote the profile (`source`: onboarding, search, booking, settings) and `GET` returns `has_profile`.
* **Space rules (R-SPACE-01..04):** each space sets max booking hours, food, walk-ins, lockers, pickup point and hours. Search hides spaces whose max is shorter than the requested window; walk-in-only shapes never appear in search; walk-ins are held only until the next booking.
* **Insights (R-INSIGHT-01..03):** searches within 2 km over 28 days; gap = demand share − supply share per amenity; est. bookings/week = searches/week × demand share × 25% capture × (1 − supply share) + 50% of zero-result searches needing it per week. An active add-on trial counts its amenity as present on every desk in the space.

## History

The first version of this backend (HW9) existed only in an earlier Claude chat and was lost. This is a rebuild from its README (Notion decision D-07), checked against the hub's rule rows.
