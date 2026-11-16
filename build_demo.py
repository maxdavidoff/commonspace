"""Build the static demo (docs/) for GitHub Pages.

    python3 build_demo.py

Copies web/ into docs/, injects demo.js into every page so api() runs the Python backend
in the browser through Pyodide, bundles the Python sources into docs/backend.json and
writes docs/index.html as the demo home.
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
OUT = os.path.join(HERE, "docs")

PAGES = [
    ("app.html", "Worker app", "Pre-book flow: find a space, rules, pick a table, phone, confirm, arrive, check in, locker, session, away, wrap up."),
    ("t.html?desk=4", "Walk-in (table mat QR)", "No app, no booking: scan the mat, see free-until, hold with a phone number, SMS when a booking is coming."),
    ("floor.html?space=2", "Floor overview display", "Entrance and elevator screen: open desks, booked soon, scan to hold."),
    ("room.html?space=2", "Room display", "Replaces the whiteboard on each pod and room."),
    ("provider.html?space=2", "Provider", "Dashboard, tag bookable zones, set rules and publish, staff desk hand-off and coaster return."),
    ("pucks.html", "Coaster wall", "Every coaster's light and countdown; tap or hold as any user."),
]


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(WEB, OUT)
    for name in os.listdir(OUT):
        if name.endswith(".html"):
            p = os.path.join(OUT, name)
            s = open(p, encoding="utf-8").read()
            s = s.replace('<script src="api.js"></script>', '<script src="demo.js"></script>\n<script src="api.js"></script>', 1)
            open(p, "w", encoding="utf-8").write(s)
    files = {}
    for name in ("server.py",):
        files[name] = open(os.path.join(HERE, name), encoding="utf-8").read()
    for name in sorted(os.listdir(os.path.join(HERE, "commonspace"))):
        if name.endswith(".py"):
            files["commonspace/" + name] = open(os.path.join(HERE, "commonspace", name), encoding="utf-8").read()
    json.dump(files, open(os.path.join(OUT, "backend.json"), "w", encoding="utf-8"))
    cards = "\n".join(
        f'<a class="card" href="{href}"><h2>{title}</h2><p class="muted">{desc}</p></a>' for href, title, desc in PAGES)
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CommonSpace demo</title><link rel="stylesheet" href="common.css"><link rel="icon" href="data:,">
<style>main{{max-width:900px;margin:0 auto;padding:40px 20px;display:grid;gap:14px}} a.card{{text-decoration:none;display:block}} a.card:hover{{border-color:var(--ink)}}</style>
</head><body><main>
<h1>CommonSpace</h1>
<p class="muted">Find and hold a desk in cafes, libraries and coworking spaces. A coaster on every table shows whether it is free; every search and booking feeds demand insights that tell each space which add-on to trial.</p>
<p class="small muted">Every page below runs the real Python backend inside your browser (Pyodide), seeded with six Pittsburgh spaces and six weeks of history. Nothing is shared between visitors and data resets on reload. The same code runs as a normal server with <code>python3 server.py</code>.</p>
{cards}
<p class="small muted">Source: <a href="https://github.com/maxdavidoff/commonspace">github.com/maxdavidoff/commonspace</a></p>
</main></body></html>""")
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    print(f"Built {OUT}: {len(files)} Python files bundled, {len(PAGES)} pages.")


if __name__ == "__main__":
    main()
