/**
 * Application bootstrap: theme, meta (constitution + elections), shell (national strip + nav),
 * router with lazy views, and the live election-night poller.
 */
import { api } from "./api.js";
import { $, h, mount } from "./dom.js";
import { fmtPct } from "./format.js";
import { icon } from "./components/icons.js";
import { evBar } from "./components/evbar.js";
import { provBadge } from "./components/badges.js";
import { ROUTES } from "./routes.js";
import { parseHash, startRouter } from "./router.js";
import { getState, setState, subscribe, partyColor } from "./store.js";
import { nightControl, watchNight } from "./night-poller.js";

const THEME_KEY = "nlfed.theme";
const ELECTION_KEY = "nlfed.election";

function safeStorage(fn, fallback = null) {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  safeStorage(() => localStorage.setItem(THEME_KEY, theme));
  setState({ settings: { ...getState().settings, theme } });
}

/* ------------------------------------------------------------------ shell */
function renderNav() {
  const groups = {};
  for (const r of ROUTES.filter((x) => x.nav)) (groups[r.nav] ||= []).push(r);
  return h(
    "nav",
    { class: "nav", "aria-label": "Main" },
    Object.entries(groups).map(([name, items]) =>
      h(
        "div",
        { class: "nav__group" },
        h("div", { class: "nav__heading" }, name),
        items.map((r) => h("a", { class: "nav__link", href: `#${r.path}`, "data-path": r.path }, icon(r.icon, { className: "nav__icon" }), r.title)),
      ),
    ),
    h(
      "div",
      { class: "nav__footer" },
      h("div", null, provBadge("FICTIONAL", "Fictional system")),
      h("p", { style: { margin: "8px 0 0" } }, "Real Dutch geography (CBS/PDOK). Constitution, parties, candidates and all results are fictional simulations."),
    ),
  );
}

function renderStrip() {
  return h(
    "header",
    { class: "strip" },
    h(
      "a",
      { class: "brand", href: "#/night" },
      h("div", { class: "brand__mark" }, "NL"),
      h("div", null, h("div", { class: "brand__title", id: "strip-title" }, "Federal Election"), h("div", { class: "brand__sub", id: "strip-sub" }, "Simulator")),
    ),
    h("div", { class: "strip__slot strip__ev", id: "strip-ev" }),
    h("div", { class: "strip__counter", id: "strip-house" }),
    h("div", { class: "strip__counter", id: "strip-senate" }),
    h("div", { class: "strip__counter", id: "strip-report" }),
    h("div", { class: "strip__slot", id: "strip-controls" }),
  );
}

function counter(label, value, sub) {
  return [h("span", { class: "strip__label" }, label), h("span", { class: "strip__value" }, value), sub ? h("span", { class: "strip__label" }, sub) : null];
}

/** Update the national strip from the live night snapshot (or the static election summary). */
function updateStrip() {
  const { meta, night, electionId } = getState();
  if (!meta) return;
  const c = meta.constitution || {};
  const election = (meta.elections || []).find((e) => e.id === electionId);
  $("#strip-title").textContent = election ? election.name : "Federal Election";
  mount($("#strip-sub"), election ? `${election.year} · ${String(election.status).toUpperCase()}` : "Simulator");

  const snap = night?.snapshot;
  const pres = snap?.president;
  const total = pres?.ev_total ?? c.electoral_votes ?? 174;
  const needed = pres?.ev_needed ?? c.presidential_majority ?? 88;
  const tickets = (pres?.tickets || []).map((t) => ({
    key: t.key,
    label: t.party || t.label,
    color: partyColor(t.party, t.color),
    decided: t.ev_decided || 0,
    leading: t.ev_leading || 0,
  }));
  tickets.sort((a, b) => b.decided + b.leading * 0.001 - (a.decided + a.leading * 0.001));
  mount($("#strip-ev"), evBar({ total, majority: needed, tickets, showLegend: true }));

  const house = snap?.house;
  const hLead = house?.by_party?.slice().sort((a, b) => b.called + b.leading - (a.called + a.leading))[0];
  mount(
    $("#strip-house"),
    counter("House · " + (house?.majority ?? c.house_majority ?? 76) + " for control", hLead ? `${hLead.party} ${hLead.called}` : "0", house ? `${house.called}/${house.seats_total} called` : `${c.house_seats ?? 150} seats`),
  );
  const senate = snap?.senate;
  const sLead = senate?.by_party?.slice().sort((a, b) => b.total_decided - a.total_decided)[0];
  mount(
    $("#strip-senate"),
    counter("Senate · " + (senate?.majority ?? c.senate_majority ?? 13) + " for control", sLead ? `${sLead.party} ${sLead.total_decided}` : "–", senate ? `${senate.up} up` : `${c.senate_seats ?? 24} seats`),
  );
  const rep = snap?.reporting;
  mount($("#strip-report"), counter("Reporting", rep ? fmtPct(rep.pct_expected_ballots) : "–", night?.clock?.clock ? `${night.clock.clock} CET` : ""));
  renderControls(night);
}

function renderControls(night) {
  const el = $("#strip-controls");
  if (!night?.clock) {
    mount(el);
    return;
  }
  const clk = night.clock;
  const running = clk.status === "running";
  const finished = clk.status === "finished";
  const btn = (name, action, title, extra = {}) =>
    h("button", { class: "btn btn--sm btn--ghost", title, "aria-label": title, disabled: finished, onclick: () => nightControl(action, extra.speed), ...extra.attrs }, icon(name, { size: 14 }));
  mount(
    el,
    h(
      "div",
      { class: "playback" },
      running ? h("span", { class: "live-dot", title: "Live" }) : null,
      running ? btn("pause", "pause", "Pause") : btn("play", clk.status === "ready" ? "start" : "resume", clk.status === "ready" ? "Start election night" : "Resume"),
      btn("step", "step", "Advance one reporting event"),
      h(
        "div",
        { class: "segmented", role: "group", "aria-label": "Playback speed" },
        (clk.speeds || [1, 2, 5, 10, 25]).map((s) =>
          h("button", { class: clk.speed === s ? "is-active" : "", disabled: finished, onclick: () => nightControl("speed", s) }, `${s}×`),
        ),
      ),
      btn("finish", "finish", "Instant finish"),
    ),
  );
}

function highlightNav(path) {
  document.querySelectorAll(".nav__link").forEach((a) => {
    const p = a.dataset.path;
    a.classList.toggle("is-active", path === p || (p !== "/night" && path.startsWith(`${p}/`)));
  });
}

/* ------------------------------------------------------------------ routing */
let cleanup = null;
let renderToken = 0;

async function onRoute({ route, params, query, path }) {
  const main = $("#main");
  if (typeof cleanup === "function") {
    try {
      cleanup();
    } catch (e) {
      console.error(e);
    }
  }
  cleanup = null;
  highlightNav(path);
  if (query.e && Number(query.e) !== getState().electionId) selectElection(Number(query.e));
  if (!route) {
    mount(main, h("div", { class: "state" }, h("h2", null, "Page not found"), h("a", { class: "btn", href: "#/night" }, "Go to Election Night")));
    return;
  }
  document.title = `${route.title} · NL Federal Election Simulator`;
  const token = ++renderToken;
  mount(main, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "240px", height: "18px" } })));
  try {
    const mod = await import(`./views/${route.view}.js`);
    if (token !== renderToken) return;
    const el = h("div", { class: "view" });
    mount(main, el);
    cleanup = await mod.render(el, params, { query, electionId: getState().electionId, meta: getState().meta });
    window.scrollTo(0, 0);
  } catch (err) {
    console.error(err);
    if (token !== renderToken) return;
    mount(main, h("div", { class: "state" }, h("h2", null, "Could not load this page"), h("p", { class: "muted" }, String(err.message || err))));
  }
}

export function selectElection(id) {
  setState({ electionId: id });
  safeStorage(() => localStorage.setItem(ELECTION_KEY, String(id)));
  watchNight(id);
}

async function boot() {
  const saved = safeStorage(() => localStorage.getItem(THEME_KEY));
  applyTheme(saved || "dark");
  const app = $("#app");
  mount(app, renderStrip(), renderNav(), h("main", { class: "main", id: "main" }));
  subscribe("night", updateStrip);
  subscribe("meta", updateStrip);
  subscribe("electionId", updateStrip);
  try {
    const [meta, settings] = await Promise.all([api.get("/api/meta"), api.get("/api/settings").catch(() => null)]);
    if (settings?.party_colors) setState({ settings: { ...getState().settings, partyColors: settings.party_colors } });
    setState({ meta });
    const stored = Number(safeStorage(() => localStorage.getItem(ELECTION_KEY)));
    const ids = (meta.elections || []).map((e) => e.id);
    const initial = Number(parseHash().query.e) || (ids.includes(stored) ? stored : null) || meta.demo_election_id || ids[ids.length - 1] || null;
    if (initial) selectElection(initial);
  } catch (err) {
    console.error(err);
    mount($("#main"), h("div", { class: "state" }, h("h2", null, "Backend not reachable"), h("p", { class: "muted" }, "Start the server with `python -m app run` after `python -m app demo`.")));
    return;
  }
  startRouter(onRoute);
}

boot();
