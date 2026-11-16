// In-browser demo: runs the real Python backend through Pyodide, no server needed.
//
// build_demo.py copies web/ into docs/ and writes docs/backend.json with every Python source
// file. This script loads Pyodide, writes those files into its virtual filesystem, creates the
// App from server.py, and routes api() calls to App.dispatch() instead of fetch().
// Bookings live in this tab only and reset on reload.

window.CS_STATIC = true;
(function () {
  const banner = document.createElement("div");
  banner.id = "demo-banner";
  banner.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:100;background:#141414;color:#fff;font:12px/1.4 Poppins,sans-serif;padding:6px 14px;display:flex;gap:12px;align-items:center";
  banner.innerHTML = '<b>CommonSpace demo</b><span id="demo-status">Starting the backend in your browser (Python via Pyodide, about 10 MB on first load)…</span><a href="index.html" style="color:#cfcfd8;margin-left:auto">Demo home</a>';
  document.addEventListener("DOMContentLoaded", () => { document.body.prepend(banner); document.body.style.paddingTop = "30px"; });

  let ready;
  window.CS_DISPATCH = async (method, path, body) => {
    const app = await ready;
    const r = app.dispatch_json(method, path, body === null ? "null" : JSON.stringify(body));
    const [status, text] = JSON.parse(r);
    return { status, body: JSON.parse(text) };
  };

  ready = (async () => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js";
    document.head.append(s);
    await new Promise((res, rej) => { s.onload = res; s.onerror = () => rej(new Error("Could not load Pyodide from the CDN")); });
    const py = await loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/" });
    await py.loadPackage("sqlite3");   // sqlite3 is unvendored from Pyodide's standard library
    const files = await fetch("backend.json").then(r => r.json());
    py.FS.mkdirTree("/app/commonspace");
    for (const [name, text] of Object.entries(files)) py.FS.writeFile("/app/" + name, text);
    const app = await py.runPythonAsync(`
import sys, json
from datetime import datetime
sys.path.insert(0, "/app")
from commonspace import db, seed, clock
import server
# Start the demo clock at 10:30 on today's date so every space is open; the sim-time buttons move it.
clock.set_fixed(datetime.now().replace(hour=10, minute=30, second=0, microsecond=0))
conn = db.connect(":memory:")
seed.seed(conn)
_app = server.App(conn)

class Bridge:
    def dispatch_json(self, method, path, body):
        status, payload = _app.dispatch(method, path, json.loads(body) if body else {})
        return json.dumps([status, json.dumps(payload)])
Bridge()
`);
    const st = document.getElementById("demo-status");
    if (st) st.textContent = "Backend running in this tab. Data resets on reload; the sim-time buttons fast-forward the clock.";
    return app;
  })();
  ready.catch(e => { const st = document.getElementById("demo-status"); if (st) st.textContent = "Demo failed to start: " + e.message; });
})();
