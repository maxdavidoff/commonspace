"""Test suite. Test names carry the Notion rule ID they check, where there is one.

    python3 -m unittest -v
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from commonspace import bookings, clock, config, db, insights, matching, puck, seed, spaces
from commonspace.errors import ApiError, NotFound

T0 = datetime(2026, 11, 18, 10, 0)  # a Wednesday


def world():
    """Small hand-built world so expected numbers are easy to check by hand."""
    clock.set_fixed(T0)
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO neighborhoods VALUES (?,?,?)", [
        ("Bloomfield", 40.4610, -79.9490), ("Downtown", 40.4406, -79.9959)])
    conn.executemany("INSERT INTO spaces(id, name, kind, neighborhood, lat, lon, open_hour, close_hour,"
                     " noise, cost_per_hour, max_hours) VALUES (?,?,?,?,?,?,?,?,?,?,8)", [
        (1, "Cafe A", "cafe", "Bloomfield", 40.4612, -79.9488, 7, 18, 2, 3.0),
        (2, "Library B", "library", "Bloomfield", 40.4630, -79.9500, 9, 20, 1, 0.0),
        (3, "Far Hub", "coworking", "Downtown", 40.4410, -79.9960, 0, 24, 2, 6.0),
    ])
    conn.executemany("INSERT INTO desks(id, space_id, label, amenities) VALUES (?,?,?,?)", [
        (1, 1, "A-1", "outlet,quiet"), (2, 1, "A-2", "outlet"), (3, 1, "A-3", "large_table,whiteboard"),
        (4, 2, "B-1", "outlet,printer,quiet"), (5, 3, "F-1", "outlet,phone_booth"),
    ])
    conn.executemany("INSERT INTO pucks(id, desk_id, battery) VALUES (?,?,?)",
                     [(i, i, 90) for i in range(1, 6)])
    conn.executemany("INSERT INTO users(id, name) VALUES (?,?)", [(1, "Ana"), (2, "Ben"), (3, "Cy")])
    conn.executemany("INSERT INTO features(id, name, amenity, weekly_cost) VALUES (?,?,?,?)", [
        (1, "Phone booth pod", "phone_booth", 40.0), (2, "Printer station", "printer", 25.0),
        (3, "Acoustic dividers", "quiet", 30.0)])
    return conn


def q(**kw):
    base = {"user_id": 1, "neighborhood": "Bloomfield", "start": "2026-11-18T10:00",
            "duration_hours": 2, "amenities": [], "work_mode": "focus"}
    base.update(kw)
    return base


def book(conn, desk=1, user=1, start="2026-11-18T10:00", hours=2):
    return bookings.create(conn, {"user_id": user, "desk_id": desk, "start": start,
                                  "duration_hours": hours})


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = world()

    def tearDown(self):
        self.conn.close()
        clock.use_real_time()


# ---------------------------------------------------------------- search

class SearchTests(Base):
    def test_R_SEARCH_02_results_ranked_descending(self):
        r = matching.search(self.conn, q())
        scores = [d["score"] for d in r["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(r["count"], 0)

    def test_R_SEARCH_02_scores_between_0_and_100(self):
        for mode in config.WORK_MODES:
            for d in matching.search(self.conn, q(work_mode=mode))["results"]:
                self.assertTrue(0 <= d["score"] <= 100)
                self.assertAlmostEqual(d["score"], sum(d["score_parts"].values()), places=0)

    def test_R_SEARCH_02_weights_sum_to_100(self):
        self.assertEqual(sum(config.SCORE_WEIGHTS.values()), 100)

    def test_R_SEARCH_02_quiet_desk_wins_for_focus(self):
        top = matching.search(self.conn, q(work_mode="focus"))["results"][0]
        self.assertIn("quiet", top["amenities"])

    def test_R_SEARCH_01_must_haves_filter(self):
        r = matching.search(self.conn, q(amenities=["quiet"]))
        self.assertEqual({d["desk_id"] for d in r["results"]}, {1, 4})

    def test_R_SEARCH_01_closed_space_excluded(self):
        r = matching.search(self.conn, q(start="2026-11-18T08:00", duration_hours=2))
        self.assertNotIn(2, {d["space_id"] for d in r["results"]})  # library opens at 9

    def test_R_SEARCH_01_window_must_fit_whole_opening(self):
        r = matching.search(self.conn, q(start="2026-11-18T17:00", duration_hours=2))
        self.assertNotIn(1, {d["space_id"] for d in r["results"]})  # cafe closes at 18

    def test_R_SEARCH_01_outside_radius_excluded(self):
        r = matching.search(self.conn, q())
        self.assertNotIn(3, {d["space_id"] for d in r["results"]})

    def test_R_MAP_02_space_id_searches_outside_radius(self):
        r = matching.search(self.conn, q(space_id=3))
        self.assertEqual({d["space_id"] for d in r["results"]}, {3})
        self.assertEqual(r["results"][0]["score_parts"]["distance"], 0)

    def test_R_SEARCH_01_booked_desk_excluded(self):
        book(self.conn, desk=1, user=2)
        r = matching.search(self.conn, q())
        self.assertNotIn(1, {d["desk_id"] for d in r["results"]})

    def test_search_is_logged_with_result_count(self):
        matching.search(self.conn, q(amenities=["quiet"]))
        row = self.conn.execute("SELECT * FROM searches").fetchone()
        self.assertEqual(row["result_count"], 2)
        self.assertEqual(row["amenities"], "quiet")

    def test_zero_result_search_is_logged(self):
        r = matching.search(self.conn, q(amenities=["phone_booth"]))
        self.assertEqual(r["count"], 0)
        self.assertEqual(self.conn.execute("SELECT result_count FROM searches").fetchone()[0], 0)

    def test_invalid_inputs_rejected(self):
        for bad in [q(duration_hours=0), q(duration_hours=9), q(neighborhood="Mars"),
                    q(amenities=["jacuzzi"]), q(work_mode="napping"), q(start="tomorrow")]:
            with self.assertRaises(ApiError):
                matching.search(self.conn, bad)
        with self.assertRaises(NotFound):
            matching.search(self.conn, q(user_id=99))

    def test_preference_funnel_marks_profile(self):
        self.assertFalse(matching.get_prefs(self.conn, 1)["has_profile"])
        p = matching.save_prefs(self.conn, 1, {"work_mode": "calls", "amenities": ["outlet"], "hours": ["morning"], "source": "onboarding"})
        self.assertTrue(p["has_profile"])
        self.assertEqual((p["source"], p["hours"]), ("onboarding", ["morning"]))
        p = matching.save_prefs(self.conn, 1, {"max_km": 5, "source": "booking"})
        self.assertEqual((p["work_mode"], p["max_km"], p["source"]), ("calls", 5, "booking"))
        for bad in [{"hours": ["night"]}, {"source": "magic"}]:
            with self.assertRaises(ApiError):
                matching.save_prefs(self.conn, 1, bad)

    def test_saved_profile_used_for_score(self):
        matching.save_prefs(self.conn, 1, {"amenities": ["printer"], "max_km": 3})
        r = {d["desk_id"]: d for d in matching.search(self.conn, q())["results"]}
        self.assertEqual(r[4]["score_parts"]["profile"], 10)
        self.assertEqual(r[2]["score_parts"]["profile"], 0)

    def test_saved_max_distance_widens_radius(self):
        matching.save_prefs(self.conn, 1, {"max_km": 10})
        r = matching.search(self.conn, q())
        self.assertIn(3, {d["space_id"] for d in r["results"]})

    def test_rental_amenity_makes_desks_match(self):
        insights.start_rental(self.conn, 1, {"feature_id": 2})  # printer at Cafe A
        r = matching.search(self.conn, q(amenities=["printer"]))
        self.assertEqual({d["space_id"] for d in r["results"]}, {1, 2})


# ---------------------------------------------------------------- map

class MapTests(Base):
    def status(self, **kw):
        return {s["space_id"]: s for s in matching.space_map(self.conn, q(**kw))["spaces"]}

    def test_R_MAP_01_available_limited_full(self):
        self.assertEqual(self.status()[1]["status"], "available")       # 3 free
        book(self.conn, desk=1, user=1)
        book(self.conn, desk=2, user=2)
        self.assertEqual(self.status()[1]["status"], "limited")
        book(self.conn, desk=3, user=3)
        self.assertEqual(self.status()[1]["status"], "full")

    def test_R_MAP_01_no_match(self):
        self.assertEqual(self.status(amenities=["phone_booth"])[1]["status"], "no_match")

    def test_R_MAP_01_closed(self):
        self.assertEqual(self.status(start="2026-11-18T19:00")[1]["status"], "closed")

    def test_map_reports_distance_and_range(self):
        s = self.status()
        self.assertTrue(s[1]["in_range"])
        self.assertFalse(s[3]["in_range"])
        self.assertGreater(s[3]["distance_km"], 3)

    def test_R_MAP_02_map_not_logged_as_demand(self):
        matching.space_map(self.conn, q())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0], 0)


# ---------------------------------------------------------------- bookings

class BookingTests(Base):
    def test_create_reserved(self):
        b = book(self.conn)
        self.assertEqual(b["state"], "reserved")
        self.assertEqual(b["end"], "2026-11-18 12:00:00")
        self.assertEqual(b["no_show_in_s"], 15 * 60)

    def test_overlap_rejected(self):
        book(self.conn, user=1)
        with self.assertRaises(ApiError):
            book(self.conn, user=2, start="2026-11-18T11:00")

    def test_back_to_back_allowed(self):
        book(self.conn, user=1)
        self.assertEqual(book(self.conn, user=2, start="2026-11-18T12:00")["state"], "reserved")

    def test_R_BOOK_03_max_two_open(self):
        book(self.conn, desk=1)
        book(self.conn, desk=2)
        with self.assertRaises(ApiError):
            book(self.conn, desk=3)

    def test_closed_window_rejected(self):
        with self.assertRaises(ApiError):
            book(self.conn, desk=1, start="2026-11-18T17:00", hours=2)

    def test_past_start_rejected(self):
        with self.assertRaises(ApiError):
            book(self.conn, start="2026-11-18T09:00")

    def test_checkin_and_wrong_user(self):
        b = book(self.conn)
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "checkin", {"user_id": 2})
        self.assertEqual(bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})["state"], "checked_in")

    def test_checkin_too_early_rejected(self):
        b = book(self.conn, start="2026-11-18T11:00")
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        clock.advance(51)
        self.assertEqual(bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})["state"], "checked_in")

    def test_away_and_return(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        a = bookings.act(self.conn, b["id"], "away", {"user_id": 1})
        self.assertEqual((a["state"], a["away_left_s"]), ("away", 20 * 60))
        self.assertEqual(bookings.act(self.conn, b["id"], "return", {"user_id": 1})["state"], "checked_in")

    def test_R_BOOK_04_extend_twice_then_refused(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        bookings.act(self.conn, b["id"], "extend", {"user_id": 1})
        e = bookings.act(self.conn, b["id"], "extend", {"user_id": 1})
        self.assertEqual((e["end"], e["extensions_left"]), ("2026-11-18 13:00:00", 0))
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "extend", {"user_id": 1})

    def test_R_BOOK_04_extend_blocked_by_next_booking(self):
        b = book(self.conn, user=1)
        book(self.conn, user=2, start="2026-11-18T12:15", hours=1)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "extend", {"user_id": 1})

    def test_extend_blocked_by_closing_time(self):
        b = book(self.conn, start="2026-11-18T10:00", hours=8)  # cafe closes 18:00
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "extend", {"user_id": 1})

    def test_end_frees_desk(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "end", {"user_id": 1})
        self.assertIn(1, {d["desk_id"] for d in matching.search(self.conn, q())["results"]})
        self.assertEqual(bookings.open_count(self.conn, 1), 0)

    def test_R_BOOK_01_no_show_released_after_15_min(self):
        b = book(self.conn)
        clock.advance(14)
        bookings.tick(self.conn)
        self.assertEqual(db.get(self.conn, "bookings", b["id"])["state"], "reserved")
        clock.advance(1)
        self.assertEqual(bookings.tick(self.conn), [(b["id"], "no_show")])
        self.assertEqual(db.get(self.conn, "bookings", b["id"])["state"], "released")

    def test_R_BOOK_02_away_timeout_after_20_min(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        bookings.act(self.conn, b["id"], "away", {"user_id": 1})
        clock.advance(19)
        self.assertEqual(bookings.tick(self.conn), [])
        clock.advance(1)
        self.assertEqual(bookings.tick(self.conn), [(b["id"], "away_timeout")])

    def test_expiry_ends_booking(self):
        b = book(self.conn, hours=1)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        clock.advance(60)
        bookings.tick(self.conn)
        row = db.get(self.conn, "bookings", b["id"])
        self.assertEqual((row["state"], row["end_reason"]), ("ended", "expired"))

    def test_user_bookings_with_countdowns(self):
        b = book(self.conn)
        clock.advance(5)
        mine = bookings.user_bookings(self.conn, 1)["bookings"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["no_show_in_s"], 10 * 60)
        self.assertEqual(mine[0]["seconds_left"], 115 * 60)
        bookings.act(self.conn, b["id"], "end", {"user_id": 1})
        self.assertEqual(bookings.user_bookings(self.conn, 1)["bookings"], [])

    def test_action_on_finished_booking_rejected(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "end", {"user_id": 1})
        with self.assertRaises(ApiError):
            bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})


# ---------------------------------------------------------------- puck

class PuckTests(Base):
    def test_free_state(self):
        led = puck.led(self.conn, 1)
        self.assertEqual((led["state"], led["color"], led["pattern"]), ("free", "green", "solid"))

    def test_reserved_shows_no_show_countdown(self):
        book(self.conn, start="2026-11-18T10:05")
        led = puck.led(self.conn, 1)
        self.assertEqual((led["state"], led["color"], led["countdown_s"]), ("reserved", "amber", 20 * 60))

    def test_future_booking_not_shown_yet(self):
        book(self.conn, start="2026-11-18T11:00")
        self.assertEqual(puck.led(self.conn, 1)["state"], "free")

    def test_tap_cycle_checkin_away_return(self):
        book(self.conn)
        g = lambda: puck.gesture(self.conn, 1, {"user_id": 1, "kind": "tap"})
        self.assertEqual(g()["led"]["state"], "occupied")
        away = g()
        self.assertEqual((away["action"], away["led"]["state"], away["led"]["color"]), ("away", "away", "blue"))
        self.assertEqual(g()["led"]["state"], "occupied")

    def test_hold_ends_booking(self):
        book(self.conn)
        puck.gesture(self.conn, 1, {"user_id": 1, "kind": "tap"})
        r = puck.gesture(self.conn, 1, {"user_id": 1, "kind": "hold"})
        self.assertEqual((r["action"], r["led"]["state"]), ("end", "free"))

    def test_gesture_refused_for_other_user_and_free_desk(self):
        with self.assertRaises(ApiError):
            puck.gesture(self.conn, 1, {"user_id": 1, "kind": "tap"})
        book(self.conn, user=1)
        with self.assertRaises(ApiError):
            puck.gesture(self.conn, 1, {"user_id": 2, "kind": "tap"})
        with self.assertRaises(ApiError):
            puck.gesture(self.conn, 1, {"user_id": 1, "kind": "swipe"})

    def test_heartbeat_records_battery_and_applies_timers(self):
        book(self.conn)
        clock.advance(15)
        led = puck.heartbeat(self.conn, 1, {"battery": 12})
        self.assertEqual(led["state"], "free")             # no-show released by the heartbeat's tick
        self.assertTrue(led["low_battery"])
        self.assertEqual(self.conn.execute("SELECT battery FROM pucks WHERE id=1").fetchone()[0], 12)

    def test_events_logged(self):
        book(self.conn)
        puck.heartbeat(self.conn, 1, {"battery": 80})
        puck.gesture(self.conn, 1, {"user_id": 1, "kind": "tap"})
        kinds = [e["kind"] for e in puck.events(self.conn, 1)["events"]]
        self.assertEqual(kinds, ["tap", "heartbeat"])

    def test_R_PUCK_07_server_holds_booking_truth(self):
        b = book(self.conn)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        # no heartbeats at all: the desk is still held
        self.assertNotIn(1, {d["desk_id"] for d in matching.search(self.conn, q())["results"]})


# ---------------------------------------------------------------- insights

def add_search(conn, amenities, result_count, days_ago=1, hood=("Bloomfield", 40.4610, -79.9490), user=1):
    ts = clock.fmt(clock.now() - timedelta(days=days_ago))
    conn.execute("INSERT INTO searches(user_id, ts, neighborhood, lat, lon, start_at, duration_hours,"
                 " amenities, work_mode, result_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (user, ts, hood[0], hood[1], hood[2], ts, 2, ",".join(amenities), "focus", result_count))


class InsightTests(Base):
    def fill(self):
        for _ in range(2):
            add_search(self.conn, ["phone_booth"], 0)
        for _ in range(2):
            add_search(self.conn, ["phone_booth"], 1)
        for _ in range(2):
            add_search(self.conn, ["quiet"], 2)
        for _ in range(2):
            add_search(self.conn, [], 3)

    def test_R_INSIGHT_02_gap_is_demand_minus_supply(self):
        self.fill()
        rows = {r["amenity"]: r for r in insights.space_insights(self.conn, 1)["demand_vs_supply"]}
        self.assertEqual(rows["phone_booth"]["demand_share"], 0.5)
        self.assertEqual(rows["phone_booth"]["supply_share"], 0.0)
        self.assertEqual(rows["phone_booth"]["gap"], 0.5)
        self.assertAlmostEqual(rows["quiet"]["gap"], 0.25 - 1 / 3, places=3)

    def test_R_INSIGHT_03_estimate_formula(self):
        self.fill()
        rows = {r["amenity"]: r for r in insights.space_insights(self.conn, 1)["demand_vs_supply"]}
        # 8 searches / 4 weeks = 2/wk; 2 x 0.5 x 0.25 x (1-0) = 0.25; + 0.5 x 2 zero / 4 wk = 0.25
        self.assertEqual(rows["phone_booth"]["est_bookings_per_week"], 0.5)

    def test_R_INSIGHT_01_window_and_radius(self):
        add_search(self.conn, ["phone_booth"], 0, days_ago=29)                       # too old
        add_search(self.conn, ["phone_booth"], 0, hood=("Downtown", 40.4406, -79.9959))  # too far
        self.assertEqual(insights.space_insights(self.conn, 1)["kpis"]["nearby_searches"], 0)

    def test_recommendations_sorted_and_skip_supplied(self):
        self.fill()
        add_search(self.conn, ["printer"], 0)
        recs = insights.space_insights(self.conn, 1)["recommendations"]
        names = [r["feature"] for r in recs]
        self.assertEqual(names[0], "Phone booth pod")
        self.assertNotIn("Acoustic dividers", names)    # quiet gap is negative here
        ests = [r["est_bookings_per_week"] for r in recs]
        self.assertEqual(ests, sorted(ests, reverse=True))

    def test_rental_closes_gap_and_duplicate_rejected(self):
        self.fill()
        insights.start_rental(self.conn, 1, {"feature_id": 1})
        ins = insights.space_insights(self.conn, 1)
        self.assertNotIn("Phone booth pod", [r["feature"] for r in ins["recommendations"]])
        self.assertEqual(len(ins["active_rentals"]), 1)
        with self.assertRaises(ApiError):
            insights.start_rental(self.conn, 1, {"feature_id": 1})

    def test_occupancy_by_hour(self):
        b = book(self.conn, desk=1, start="2026-11-18T10:00", hours=1)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        clock.advance(60)
        occ = insights.occupancy_by_hour(self.conn, 1, days=1)
        self.assertAlmostEqual(occ[10]["occupancy"], 1 / (3 * 1 * 60) * 60, places=3)
        self.assertEqual(occ[11]["occupancy"], 0)

    def test_live_floor_hides_user_identities(self):
        book(self.conn, desk=1, user=1)
        floor = insights.live_floor(self.conn, 1)
        self.assertEqual(floor["state_counts"], {"reserved": 1, "free": 2})
        self.assertNotIn("user_id", json.dumps(floor))


# ---------------------------------------------------------------- space rules, walk-in, staff (B2)

class SpaceTests(Base):
    def test_R_SPACE_01_max_hours_enforced(self):
        spaces.save_settings(self.conn, 1, {"max_hours": 2})
        with self.assertRaises(ApiError):
            book(self.conn, desk=1, hours=3)
        self.assertEqual(book(self.conn, desk=1, hours=2)["state"], "reserved")
        r = matching.search(self.conn, q(duration_hours=3))
        self.assertNotIn(1, {d["space_id"] for d in r["results"]})

    def test_R_SPACE_03_walkin_only_desk_hidden_from_search(self):
        spaces.tag_desk(self.conn, 1, {"bookable": False})
        self.assertNotIn(1, {d["desk_id"] for d in matching.search(self.conn, q())["results"]})
        with self.assertRaises(ApiError):
            book(self.conn, desk=1)

    def test_settings_roundtrip_and_validation(self):
        s = spaces.save_settings(self.conn, 1, {"food": False, "pickup": "Reference desk", "max_hours": 3})
        self.assertEqual((s["rules"]["food"], s["rules"]["pickup"], s["rules"]["max_hours"]),
                         (False, "Reference desk", 3))
        for bad in [{"max_hours": 0}, {"open_hour": 25}, {"min_purchase_per_hour": -1}]:
            with self.assertRaises(ApiError):
                spaces.save_settings(self.conn, 1, bad)

    def test_tag_desk_kinds(self):
        d = spaces.tag_desk(self.conn, 3, {"kind": "room", "capacity": 6})
        self.assertEqual((d["kind"], d["capacity"], d["bookable"]), ("room", 6, 1))
        self.assertEqual(spaces.tag_desk(self.conn, 3, {"kind": "pickup"})["bookable"], 0)
        with self.assertRaises(ApiError):
            spaces.tag_desk(self.conn, 3, {"kind": "garden"})

    def test_phone_user_created_once(self):
        a = spaces.phone_user(self.conn, {"phone": "(412) 555-0199"})
        b = spaces.phone_user(self.conn, {"phone": "4125550199", "code": "000000"})
        self.assertEqual(a["user_id"], b["user_id"])
        self.assertTrue(a["created"] and not b["created"] and b["verified"])
        with self.assertRaises(ApiError):
            spaces.phone_user(self.conn, {"phone": "12345"})

    def test_desk_status_free_until_next_booking(self):
        st = spaces.desk_status(self.conn, 1)
        self.assertEqual((st["status"], st["free_until"]), ("free", "2026-11-18 18:00:00"))
        book(self.conn, desk=1, user=2, start="2026-11-18T13:00", hours=2)
        st = spaces.desk_status(self.conn, 1)
        self.assertEqual((st["status"], st["free_until"], st["free_minutes"]), ("free", "2026-11-18 13:00:00", 180))

    def test_walkin_hold_until_next_booking(self):
        book(self.conn, desk=1, user=2, start="2026-11-18T13:00", hours=2)
        h = spaces.hold_desk(self.conn, 1, {"phone": "4125550100"})
        self.assertEqual((h["state"], h["kind"], h["end"]), ("checked_in", "walkin", "2026-11-18 13:00:00"))
        self.assertEqual(spaces.desk_status(self.conn, 1)["status"], "in_use")
        with self.assertRaises(ApiError):
            spaces.hold_desk(self.conn, 1, {"phone": "4125550101"})

    def test_walkin_hold_capped_by_max_hours(self):
        spaces.save_settings(self.conn, 1, {"max_hours": 1})
        h = spaces.hold_desk(self.conn, 1, {"phone": "4125550100"})
        self.assertEqual(h["end"], "2026-11-18 11:00:00")

    def test_walkin_refused_when_booking_imminent(self):
        book(self.conn, desk=1, user=2, start="2026-11-18T10:05", hours=1)
        with self.assertRaises(ApiError):
            spaces.hold_desk(self.conn, 1, {"phone": "4125550100"})

    def test_floor_plan_states(self):
        book(self.conn, desk=1, user=1)
        plan = spaces.floor_plan(self.conn, 1)
        by = {s["label"]: s for s in plan["shapes"]}
        self.assertEqual(by["A-1"]["status"], "booked")
        self.assertEqual(by["A-2"]["status"], "free")
        self.assertEqual(plan["summary"]["open"], 2)

    def test_staff_handoff_checks_in_and_return_ends(self):
        b = book(self.conn, desk=1, user=1)
        found = spaces.staff_lookup(self.conn, 1, "ana")["bookings"]
        self.assertEqual(found[0]["booking_id"], b["id"])
        h = spaces.hand_off(self.conn, b["id"], {"coaster": "07"})
        self.assertEqual((h["state"], h["coaster"]), ("checked_in", "07"))
        self.assertEqual(spaces.coaster_return(self.conn, b["id"], {})["state"], "ended")

    def test_dashboard_counts(self):
        b = book(self.conn, desk=1, user=1)
        bookings.act(self.conn, b["id"], "checkin", {"user_id": 1})
        spaces.hold_desk(self.conn, 2, {"phone": "4125550100"})
        b3 = book(self.conn, desk=3, user=2)
        clock.advance(16)
        d = spaces.dashboard(self.conn, 1)
        self.assertEqual((d["kpis"]["checkins"], d["kpis"]["walkins"], d["kpis"]["no_shows"]), (2, 1, 1))
        self.assertEqual(sum(h["bookings"] for h in d["by_hour"]), 2)
        with self.assertRaises(ApiError):
            spaces.dashboard(self.conn, 1, "year")


# ---------------------------------------------------------------- seed

class SeedTests(unittest.TestCase):
    def setUp(self):
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()
        clock.use_real_time()

    def connect(self):
        c = db.connect()
        self.conns.append(c)
        return c

    def test_seed_counts_and_determinism(self):
        clock.set_fixed(T0)
        a, b = self.connect(), self.connect()
        seed.seed(a)
        seed.seed(b)
        count = lambda c, t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        self.assertEqual([count(a, t) for t in ("spaces", "pucks", "users")], [6, 50, 40])
        self.assertEqual(a.execute("SELECT COUNT(*) FROM desks WHERE bookable=1").fetchone()[0], 50)
        self.assertGreater(count(a, "searches"), 1000)
        for t in ("desks", "searches", "bookings"):
            self.assertEqual(a.execute(f"SELECT * FROM {t}").fetchall(),
                             b.execute(f"SELECT * FROM {t}").fetchall())

    def test_seed_has_a_planted_phone_booth_gap(self):
        clock.set_fixed(T0)
        c = self.connect()
        seed.seed(c)
        recs = insights.space_insights(c, 1)["recommendations"]
        self.assertIn("Phone booth pod", [r["feature"] for r in recs[:3]])

    def test_seed_shows_every_puck_state(self):
        clock.set_fixed(T0)
        c = self.connect()
        seed.seed(c)
        states = {p["state"] for p in puck.all_pucks(c)["pucks"]}
        self.assertEqual(states, {"free", "reserved", "occupied", "away"})


# ---------------------------------------------------------------- HTTP API

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.conn = world()
        cls.httpd, cls.app = server.make_server(cls.conn, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.conn.close()
        clock.use_real_time()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_search_over_http(self):
        status, r = self.call("POST", "/api/search", q())
        self.assertEqual(status, 200)
        self.assertGreater(r["count"], 0)

    def test_400_on_bad_input_and_bad_json(self):
        self.assertEqual(self.call("POST", "/api/search", q(duration_hours=-1))[0], 400)
        status, r = self.call("POST", "/api/search", raw=b"{not json")
        self.assertEqual((status, "error" in r), (400, True))

    def test_404_and_405(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/spaces/99")[0], 404)
        self.assertEqual(self.call("GET", "/api/search")[0], 405)

    def test_booking_lifecycle_over_http(self):
        s, b = self.call("POST", "/api/bookings", {"user_id": 3, "desk_id": 4,
                                                   "start": "2026-11-18T10:00", "duration_hours": 1})
        self.assertEqual((s, b["state"]), (200, "reserved"))
        for action, state in [("checkin", "checked_in"), ("away", "away"), ("return", "checked_in"),
                              ("extend", "checked_in"), ("end", "ended")]:
            s, r = self.call("POST", f"/api/bookings/{b['id']}/{action}", {"user_id": 3})
            self.assertEqual((s, r["state"]), (200, state), action)

    def test_provider_and_device_endpoints(self):
        self.assertEqual(self.call("GET", "/api/spaces/1")[0], 200)
        self.assertEqual(self.call("GET", "/api/spaces/1/insights")[0], 200)
        s, led = self.call("POST", "/api/device/pucks/5/heartbeat", {"battery": 70})
        self.assertEqual((s, led["state"]), (200, "free"))
        self.assertEqual(self.call("GET", "/api/device/pucks/5/events")[1]["events"][0]["kind"], "heartbeat")
        self.assertEqual(self.call("PUT", "/api/users/2/preferences", {"max_km": 4})[1]["max_km"], 4)

    def test_walkin_and_settings_over_http(self):
        s, st = self.call("GET", "/api/desks/2/status")
        self.assertEqual((s, st["status"]), (200, "free"))
        s, h = self.call("POST", "/api/desks/2/hold", {"phone": "412-555-0150"})
        self.assertEqual((s, h["kind"]), (200, "walkin"))
        s, d = self.call("GET", "/api/spaces/1/dashboard?period=week")
        self.assertEqual((s, d["period"]), (200, "week"))
        self.assertEqual(self.call("PUT", "/api/spaces/1/settings", {"food": False})[1]["rules"]["food"], False)
        self.assertEqual(self.call("GET", "/api/spaces/1/floor")[0], 200)
        self.assertEqual(self.call("GET", "/api/spaces/1/staff/bookings?q=x")[0], 200)

    def test_dev_advance_releases_no_show(self):
        s, b = self.call("POST", "/api/bookings", {"user_id": 2, "desk_id": 5,
                                                   "start": clock.fmt(clock.now()), "duration_hours": 2})
        self.assertEqual(s, 200)
        s, r = self.call("POST", "/api/dev/advance", {"minutes": 15})
        self.assertIn({"booking_id": b["id"], "reason": "no_show"}, r["released"])


if __name__ == "__main__":
    unittest.main()
