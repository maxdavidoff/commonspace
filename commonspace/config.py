"""Every tunable constant, with the Notion rule ID it implements."""

# Booking holds
NO_SHOW_MINUTES = 15          # R-BOOK-01 release a reserved desk this long after start
AWAY_MINUTES = 20             # R-BOOK-02 away allowance
MAX_OPEN_BOOKINGS = 2         # R-BOOK-03 open bookings per user
EXTEND_MINUTES = 30           # R-BOOK-04 one extension
MAX_EXTENSIONS = 2            # R-BOOK-04
CHECKIN_EARLY_MINUTES = 10    # a booking can be checked in (and shows as reserved on the puck) this early
MAX_DURATION_HOURS = 8
MAX_PAST_START_MINUTES = 15   # a new booking may start at most this far in the past

# Search scoring, R-SEARCH-02 (weights sum to 100)
SCORE_WEIGHTS = {"distance": 30, "mode": 30, "noise": 20, "cost": 10, "profile": 10}
COST_CEILING = 10.0           # $/hour at which the cost score reaches 0
DEFAULT_MAX_KM = 3.0          # R-SEARCH-01 default max distance

# Map, R-MAP-01
MAP_AVAILABLE_MIN = 2         # 2+ free desks = available, 1 = limited

# Insights
INSIGHT_RADIUS_KM = 2.0       # R-INSIGHT-01
INSIGHT_WINDOW_DAYS = 28      # R-INSIGHT-01
CAPTURE_RATE = 0.25           # R-INSIGHT-03 assumption
ZERO_RESULT_CONVERSION = 0.5  # R-INSIGHT-03 assumption

# Pucks
LOW_BATTERY = 15              # percent; heartbeat response flags low_battery below this

AMENITIES = ["outlet", "quiet", "phone_booth", "whiteboard", "large_table",
             "monitor", "printer", "standing"]

# Work mode -> amenities that make a desk fit, and preferred noise (1 quiet .. 3 lively)
WORK_MODES = {
    "focus":   {"amenities": ["outlet", "quiet"], "noise": 1},
    "calls":   {"amenities": ["outlet", "phone_booth"], "noise": 1},
    "meeting": {"amenities": ["large_table", "whiteboard"], "noise": 2},
    "casual":  {"amenities": ["outlet"], "noise": 3},
}

# What each puck state looks like on the LED ring
LED = {
    "free":     {"color": "green", "pattern": "solid"},
    "reserved": {"color": "amber", "pattern": "pulse"},
    "occupied": {"color": "red",   "pattern": "solid"},
    "away":     {"color": "blue",  "pattern": "blink"},
}
