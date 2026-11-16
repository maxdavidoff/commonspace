// Shared helpers for every CommonSpace page.
//
// api() talks to the Python server over HTTP. When the page is served as the static demo
// (GitHub Pages), demo.js sets window.CS_DISPATCH to a function that runs the same Python
// backend in the browser through Pyodide, and api() calls that instead.

async function api(method, path, body) {
  if (window.CS_DISPATCH) {
    const r = await window.CS_DISPATCH(method, path, body === undefined ? null : body);
    if (r.status >= 400) throw new Error(r.body.error || `HTTP ${r.status}`);
    return r.body;
  }
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const k of kids.flat(3)) if (k !== null && k !== undefined && k !== false && k !== "") e.append(k.nodeType ? k : String(k));
  return e;
}

function mmss(s) {
  if (s === null || s === undefined) return "";
  const m = Math.floor(s / 60);
  if (m >= 60) return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}
function minutesLeft(s) { const m = Math.max(0, Math.round(s / 60)); return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim() : `${m} min`; }
function hhmm(t) { // "2026-11-18 14:30:00" -> "2:30 PM"
  if (!t) return "";
  const h = +t.slice(11, 13), m = t.slice(14, 16);
  return `${((h + 11) % 12) + 1}:${m} ${h < 12 ? "AM" : "PM"}`;
}
function hourLabel(h) { return `${((h + 11) % 12) + 1} ${h < 12 ? "AM" : "PM"}`; }
function amen(a) { return a.replace("_", " "); }

function toast(msg, isErr) {
  let t = document.querySelector(".toast");
  if (!t) { t = el("div", { class: "toast" }); document.body.append(t); }
  t.textContent = msg; t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = "toast"), 2800);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

let META = null;
const onTick = [];

async function loadMeta() { META = await api("GET", "/api/meta"); return META; }

async function topbar(active, extra) {
  await loadMeta();
  const time = el("span", { id: "simtime" });
  const adv = (m) => el("button", { onclick: () => guard(async () => {
    const r = await api("POST", "/api/dev/advance", { minutes: m });
    if (r.released.length) toast(`Released ${r.released.length}: ` + r.released.map(x => x.reason.replace("_", " ")).join(", "));
    await refreshTime(); onTick.forEach(f => f());
  }) }, `+${m < 60 ? m + "m" : m / 60 + "h"}`);
  const link = (href, label) => el("a", { href: pageHref(href), class: active === href ? "on" : "" }, label);
  document.body.prepend(el("div", { class: "topbar" },
    el("span", { class: "brand" }, "CommonSpace"),
    el("nav", {}, link("/app", "Worker app"), link("/t/2", "Walk-in (mat QR)"), link("/floor", "Floor display"),
      link("/room", "Room display"), link("/provider", "Provider"), link("/pucks", "Coasters")),
    extra || null,
    el("div", { class: "sim" }, el("span", { class: "muted" }, "sim time"), time, adv(5), adv(15), adv(60))));
  await refreshTime();
  setInterval(() => guard(refreshTime), 5000);
}

// Static demo pages live at ./app.html etc.; the server serves them at /app etc.
function pageHref(path) {
  const m = path.match(/^\/t\/(\d+)$/);
  if (m) return `t.html?desk=${m[1]}`;
  return path.replace(/^\//, "") + ".html";
}

async function refreshTime() {
  const m = await api("GET", "/api/meta");
  META.now = m.now; META.sim_offset_minutes = m.sim_offset_minutes;
  const t = document.getElementById("simtime");
  if (t) t.textContent = hhmm(m.now) + (m.sim_offset_minutes ? ` (+${Math.round(m.sim_offset_minutes)}m)` : "");
  return m;
}

function nowInput(offsetMin = 0, roundTo = 15) {
  const d = new Date(META.now.replace(" ", "T"));
  d.setMinutes(Math.ceil((d.getMinutes() + offsetMin) / roundTo) * roundTo, 0, 0);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
function param(name, fallback) { return new URLSearchParams(location.search).get(name) || fallback; }
