/**
 * Application bootstrap: theme, meta (constitution + elections), shell (national strip + nav),
 * router with lazy views, and the live election-night poller.
 *
 * The national strip is always visible:
 *   brand + election picker · EV bar (decided solid / leading hatched / uncalled, 88 TO WIN) ·
 *   House counter (76 FOR CONTROL) · Senate counter (13 FOR CONTROL) · reporting % + simulated
 *   clock · playback controls (hidden / live nights) or the certified final state (FINAL elections).
 * Live numbers come from the night snapshot (store.night); final numbers from
 * /api/elections/{id}/president, /house and /senate.  The strip performs no election mathematics.
 */
import { api } from "./api.js";
import { $, h, keyed, mount } from "./dom.js";
import { fmtInt, fmtPct } from "./format.js";
import { icon } from "./components/icons.js";
import { evBar } from "./components/evbar.js";
import { provBadge } from "./components/badges.js";
import { toast } from "./components/live-toast.js";
import { ROUTES } from "./routes.js";
import { parseHash, startRouter } from "./router.js";
import { getElection, getState, nightFor, partyColor, setState, subscribe } from "./store.js";
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
  const themeBtn = h(
    "button",
    {
      class: "btn btn--sm btn--ghost lv-theme-toggle",
      type: "button",
      onclick: () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"),
    },
  );
  const paintTheme = () => {
    const light = document.documentElement.dataset.theme === "light";
    mount(themeBtn, icon(light ? "moon" : "sun", { size: 13 }), light ? "Dark theme" : "Light theme");
    themeBtn.setAttribute("aria-label", light ? "Switch to the dark theme" : "Switch to the light theme");
  };
  subscribe("settings", paintTheme);
  paintTheme();
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
      h("p", { style: { margin: "8px 0 10px" } }, "Real Dutch geography (CBS/PDOK). Constitution, parties, candidates and all results are fictional simulations."),
      themeBtn,
    ),
  );
}

function renderStrip() {
  return h(
    "header",
    { class: "strip lv-strip" },
    h(
      "div",
      { class: "brand" },
      h("a", { class: "brand__mark", href: "#/night", "aria-label": "Election Night home" }, "NL"),
      h("div", { class: "lv-brand__text" }, h("div", { class: "brand__sub" }, "NL Federal Election"), h("div", { id: "strip-picker", class: "lv-strip-picker" })),
    ),
    h("div", { class: "strip__slot strip__ev", id: "strip-ev" }),
    h("div", { class: "strip__counter lv-counter", id: "strip-house" }),
    h("div", { class: "strip__counter lv-counter", id: "strip-senate" }),
    h("div", { class: "strip__counter lv-counter lv-counter--report", id: "strip-report" }),
    h("div", { class: "strip__slot lv-strip-controls", id: "strip-controls" }),
  );
}

/* ------------------------------------------------------------------ election picker */
function statusSuffix(e) {
  if (e.status === "live") return " · LIVE";
  if (e.status === "final" || e.status === "certified") return " · final";
  return " · not reported";
}

function renderPicker() {
  const { meta, electionId } = getState();
  const els = meta?.elections || [];
  const key = JSON.stringify([electionId, els.map((e) => [e.id, e.status])]);
  keyed($("#strip-picker"), key, () =>
    h(
      "select",
      {
        class: "lv-picker",
        "aria-label": "Election",
        title: "Choose the election shown on every page",
        onchange: (e) => {
          const [path] = location.hash.replace(/^#/, "").split("?");
          location.hash = `#${path || "/night"}?e=${e.target.value}`;
        },
      },
      els
        .slice()
        .reverse()
        .map((e) => h("option", { value: e.id, selected: e.id === electionId }, `${e.name}${statusSuffix(e)}`)),
    ),
  );
  const sel = $("#strip-picker select");
  const cur = els.find((e) => e.id === electionId);
  if (sel && cur) sel.title = `${cur.name} (${cur.status}) — choose the election shown on every page`;
}

/* ------------------------------------------------------------------ final-state cache */
const finalCache = new Map(); // id -> {pres, house, senate} | "loading"

function loadFinal(id) {
  if (finalCache.has(id)) return;
  finalCache.set(id, "loading");
  const soft = (p) => p.catch(() => null);
  Promise.all([soft(api.get(`/api/elections/${id}/president`)), soft(api.get(`/api/elections/${id}/house`)), soft(api.get(`/api/elections/${id}/senate`))]).then(([pres, house, senate]) => {
    finalCache.set(id, { pres, house, senate });
    updateStrip();
  });
}

/* ------------------------------------------------------------------ strip model */
function tallyTickets(list, decidedKey, leadingKey) {
  const tickets = (list || []).map((t) => ({
    key: t.key,
    label: t.party || t.label || t.key,
    name: t.name || t.label,
    color: partyColor(t.party, t.color),
    decided: t[decidedKey] || 0,
    leading: t[leadingKey] || 0,
    votes: t.votes || 0,
  }));
  // Leader first: by decided EV, then leading EV, then counted votes (sorting only).
  tickets.sort((a, b) => b.decided - a.decided || b.leading - a.leading || b.votes - a.votes);
  return tickets;
}

function stripModel() {
  const { meta, electionId } = getState();
  const c = meta?.constitution || {};
  const election = getElection(electionId);
  const final = election && (election.status === "final" || election.status === "certified");
  const base = {
    election,
    total: c.electoral_votes ?? 174,
    needed: c.presidential_majority ?? 88,
    houseMajority: c.house_majority ?? 76,
    houseSeats: c.house_seats ?? 150,
    senateMajority: c.senate_majority ?? 13,
    senateSeats: c.senate_seats ?? 24,
  };
  const night = nightFor(electionId);
  if (final && night?.clock?.status !== "running") {
    loadFinal(electionId);
    const f = finalCache.get(electionId);
    if (!f || f === "loading") return { ...base, kind: "loading" };
    const pres = f.pres;
    return {
      ...base,
      kind: "final",
      noPresident: !pres,
      ev: pres
        ? {
            total: pres.electoral_votes_total ?? base.total,
            needed: pres.majority ?? base.needed,
            tickets: tallyTickets(pres.tickets, "electoral_votes", "__none"),
            winner: pres.winner,
            decidedBy: pres.decided_by,
          }
        : null,
      house: f.house && {
        majority: f.house.majority,
        seats: f.house.seats_total,
        parties: (f.house.by_party || []).map((p) => ({ party: p.party, color: partyColor(p.party, p.color), called: p.won ?? p.total ?? 0, leading: 0 })),
        control: f.house.control?.controlling_party || null,
        largest: f.house.control?.largest_party || null,
        sub: f.house.control?.controlling_party ? `${f.house.control.controlling_party} control` : "No majority",
      },
      senate: f.senate && {
        majority: f.senate.majority,
        seats: f.senate.seats_total,
        parties: (f.senate.by_party || []).map((p) => ({ party: p.party, color: partyColor(p.party, p.color), called: p.total_projected ?? p.total_decided ?? 0, leading: 0, holdover: p.holdover || 0 })),
        control: f.senate.control?.controlling_party || null,
        sub: f.senate.control?.controlling_party ? `${f.senate.control.controlling_party} control` : "No majority",
      },
      turnout: pres?.popular_vote?.turnout_pct ?? null,
    };
  }
  const snap = night?.snapshot;
  if (!snap) return { ...base, kind: "empty" };
  const pres = snap.president;
  const house = snap.house;
  const senate = snap.senate;
  return {
    ...base,
    kind: "live",
    clock: night.clock,
    noPresident: !pres,
    ev: pres
      ? {
          total: pres.ev_total ?? base.total,
          needed: pres.ev_needed ?? base.needed,
          tickets: tallyTickets(pres.tickets, "ev_decided", "ev_leading"),
          decidedTotal: pres.ev_decided_total ?? 0,
          uncalled: pres.ev_uncalled ?? base.total,
          winner: pres.winner,
          contingentLikely: pres.contingent_likely,
          decidedBy: pres.decided_by,
        }
      : null,
    house: house && {
      majority: house.majority,
      seats: house.seats_total,
      parties: (house.by_party || []).map((p) => ({ party: p.party, color: partyColor(p.party, p.color), called: p.called || 0, leading: p.leading || 0 })),
      control: house.control,
      sub: house.control ? `${house.control} control` : `${fmtInt(house.called)}/${fmtInt(house.seats_total)} called`,
    },
    senate: senate && {
      majority: senate.majority,
      seats: senate.seats_total,
      parties: (senate.by_party || []).map((p) => ({ party: p.party, color: partyColor(p.party, p.color), called: p.total_decided || 0, leading: p.leading || 0, holdover: p.holdover || 0 })),
      control: senate.control,
      sub: senate.control ? `${senate.control} control` : `${fmtInt(senate.up)} up · ${fmtInt(senate.called)} called`,
    },
    reporting: snap.reporting?.pct_expected_ballots ?? 0,
  };
}

/* ------------------------------------------------------------------ strip rendering */
let stripEvBar = null;

function winnerName(ev) {
  const w = ev?.winner;
  if (!w) return null;
  if (typeof w === "object") return w.president?.name || String(w.name || "").split(" / ")[0];
  const t = ev.tickets.find((x) => x.key === w);
  return t ? String(t.name || "").split(" / ")[0] : null;
}

function renderEv(m) {
  const el = $("#strip-ev");
  if (m.kind === "loading" || m.kind === "empty") {
    stripEvBar = null;
    keyed(el, `${m.kind}`, () => evBlock("President", `0 EV allocated · ${m.total} available`, m.needed, evBar({ total: m.total, majority: m.needed, tickets: [], compact: true, markerLabel: false, showLegend: false })));
    return;
  }
  if (m.noPresident) {
    stripEvBar = null;
    keyed(el, "nopres", () =>
      h("div", { class: "lv-strip-ev lv-strip-ev--none" }, h("span", { class: "strip__label" }, "President"), h("span", { class: "lv-strip-ev__none" }, "Midterm election — no presidential race")),
    );
    return;
  }
  const ev = m.ev;
  let status;
  if (m.kind === "final") {
    const name = winnerName(ev);
    status = name ? `${name} elected${ev.decidedBy === "contingent" ? " by contingent election" : ""}` : "Final";
  } else if (ev.winner) {
    status = `${winnerName(ev) || "Winner"} secures ${m.needed}`;
  } else if (ev.contingentLikely) {
    status = "No ticket can reach 88 — contingent likely";
  } else status = `${fmtInt(ev.decidedTotal)} of ${fmtInt(ev.total)} EV allocated`;
  const props = { total: ev.total, majority: ev.needed, tickets: ev.tickets, compact: true, markerLabel: false, legendMax: 3 };
  if (!stripEvBar || !el.contains(stripEvBar) || el.dataset.key !== "ev") {
    el.dataset.key = "ev";
    stripEvBar = evBar(props);
    mount(el, evBlock("President", status, ev.needed, stripEvBar));
  } else {
    stripEvBar.update(props);
    const st = el.querySelector(".lv-strip-ev__status");
    if (st && st.textContent !== status) st.textContent = status;
  }
  el.classList.toggle("is-won", !!ev.winner);
  el.classList.toggle("is-contingent", !!ev.contingentLikely && !ev.winner);
}

function evBlock(label, status, needed, bar) {
  return h(
    "div",
    { class: "lv-strip-ev" },
    h(
      "div",
      { class: "lv-strip-ev__head" },
      h("span", { class: "strip__label" }, label),
      h("span", { class: "lv-strip-ev__status" }, status),
      h("span", { class: "lv-strip-ev__towin" }, `${needed} TO WIN`),
    ),
    bar,
  );
}

function seatCounter(title, c, fallbackMajority, fallbackSeats) {
  if (!c) {
    return [h("span", { class: "strip__label" }, `${title} · ${fallbackMajority} for control`), h("span", { class: "strip__value" }, "–"), h("span", { class: "strip__label" }, `${fallbackSeats} seats`)];
  }
  const parties = c.parties
    .filter((p) => p.called + p.leading > 0)
    .sort((a, b) => b.called - a.called || b.leading - a.leading);
  const top = parties.slice(0, 2);
  const seats = c.seats || fallbackSeats;
  const bar = h(
    "div",
    { class: "lv-minibar", "aria-hidden": "true" },
    ...parties.map((p) => h("span", { class: "lv-minibar__seg", style: { "--party": p.color, flexGrow: p.called } })),
    ...parties.filter((p) => p.leading > 0).map((p) => h("span", { class: "lv-minibar__seg lv-minibar__seg--lead", style: { "--party": p.color, flexGrow: p.leading } })),
    h("span", { class: "lv-minibar__rest", style: { flexGrow: Math.max(0, seats - parties.reduce((a, p) => a + p.called + p.leading, 0)) } }),
    h("span", { class: "lv-minibar__tick", style: { left: `${(c.majority / seats) * 100}%` } }),
  );
  return [
    h("span", { class: "strip__label" }, `${title} · ${c.majority} for control`),
    h(
      "span",
      { class: "strip__value lv-counter__value" },
      top.length
        ? top.map((p) =>
            h(
              "span",
              { class: "lv-counter__party", title: `${p.party}: ${p.called} decided${p.leading ? `, leading ${p.leading}` : ""}` },
              h("span", { class: "chip__swatch", style: { "--party": p.color } }),
              `${p.party} ${p.called}`,
              p.leading ? h("span", { class: "lv-counter__lead" }, `+${p.leading}`) : null,
            ),
          )
        : "0",
    ),
    bar,
    h("span", { class: ["strip__label", c.control && "lv-counter__control"] }, c.control ? [icon("check", { size: 10 }), ` ${c.sub}`] : c.sub),
  ];
}

function renderCounters(m) {
  const houseKey = JSON.stringify(m.house ? [m.house.parties.map((p) => [p.party, p.called, p.leading, p.color]), m.house.sub, m.house.control] : null);
  keyed($("#strip-house"), houseKey, () => seatCounter("House", m.house, m.houseMajority, m.houseSeats));
  const senKey = JSON.stringify(m.senate ? [m.senate.parties.map((p) => [p.party, p.called, p.leading, p.color]), m.senate.sub, m.senate.control] : null);
  keyed($("#strip-senate"), senKey, () => seatCounter("Senate", m.senate, m.senateMajority, m.senateSeats));

  const rep = $("#strip-report");
  if (m.kind === "final") {
    keyed(rep, `final|${m.turnout}`, () => [
      h("span", { class: "strip__label" }, m.turnout !== null && m.turnout !== undefined ? "Turnout" : "Reporting"),
      h("span", { class: "strip__value" }, m.turnout !== null && m.turnout !== undefined ? fmtPct(m.turnout) : "100%"),
      h("span", { class: "strip__label" }, "Certified result"),
    ]);
    return;
  }
  const pct = m.reporting ?? 0;
  const clock = m.clock?.clock;
  const running = m.clock?.status === "running";
  keyed(rep, `${pct}|${clock}|${running}`, () => [
    h("span", { class: "strip__label" }, "Reporting"),
    h("span", { class: "strip__value" }, fmtPct(pct)),
    h("div", { class: "lv-minibar lv-minibar--report", "aria-hidden": "true" }, h("span", { class: "lv-minibar__fill", style: { width: `${Math.min(100, pct)}%` } })),
    h("span", { class: "strip__label lv-clock" }, running ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, clock ? `${clock} CET` : ""),
  ]);
}

/* two-step confirm buttons for irreversible actions */
function confirmButton(label, iconName, title, onConfirm, disabled) {
  const b = h("button", { class: "btn btn--sm btn--ghost lv-ctl", type: "button", title, "aria-label": title, disabled }, icon(iconName, { size: 14 }));
  let armed = null;
  b.addEventListener("click", () => {
    if (armed) {
      clearTimeout(armed);
      armed = null;
      b.classList.remove("is-armed");
      onConfirm();
      return;
    }
    b.classList.add("is-armed");
    mount(b, icon(iconName, { size: 14 }), `${label}?`);
    b.setAttribute("aria-label", `Confirm: ${title}`);
    armed = setTimeout(() => {
      armed = null;
      b.classList.remove("is-armed");
      mount(b, icon(iconName, { size: 14 }));
      b.setAttribute("aria-label", title);
    }, 4000);
  });
  return b;
}

async function control(action, speed) {
  try {
    await nightControl(action, speed);
  } catch (err) {
    toast(err?.message || `Could not ${action} the night`, { kind: "error" });
  }
}

function renderControls(m) {
  const el = $("#strip-controls");
  if (m.kind === "final") {
    const key = `final|${m.ev?.decidedBy}|${m.election?.id}`;
    keyed(el, key, () =>
      h(
        "div",
        { class: "lv-final" },
        h("span", { class: "pill pill--final lv-final__pill", style: { "--party": "var(--surface-3)" } }, icon("lock", { size: 11, className: "pill__icon" }), "Final"),
        h("div", { class: "lv-final__text" }, h("span", { class: "strip__label" }, "Certified result"), h("span", { class: "lv-final__sub" }, m.ev?.decidedBy === "contingent" ? "President chosen by contingent election" : m.noPresident ? "Midterm · House, Senate, local" : "Historic election · no live controls")),
      ),
    );
    return;
  }
  const clk = m.clock;
  if (!clk) {
    keyed(el, "none", () => h("span", { class: "strip__label" }, m.kind === "loading" ? "" : "No election night available"));
    return;
  }
  const status = clk.status;
  const key = `${status}|${clk.speed}|${(clk.speeds || []).join(",")}`;
  keyed(el, key, () => {
    const finished = status === "finished";
    const ready = status === "ready";
    const running = status === "running";
    const iconBtn = (name, action, title) =>
      h("button", { class: "btn btn--sm btn--ghost lv-ctl", type: "button", title, "aria-label": title, disabled: finished, onclick: () => control(action) }, icon(name, { size: 14 }));
    const primary = running
      ? h("button", { class: "btn btn--sm lv-ctl lv-ctl--main", type: "button", title: "Pause the count", onclick: () => control("pause") }, icon("pause", { size: 13 }), "Pause")
      : h(
          "button",
          { class: "btn btn--sm btn--primary lv-ctl lv-ctl--main", type: "button", disabled: finished, title: ready ? "Start election night" : "Resume the count", onclick: () => control(ready ? "start" : "resume") },
          icon("play", { size: 13 }),
          ready ? "Start" : "Resume",
        );
    return h(
      "div",
      { class: "playback lv-playback" },
      primary,
      iconBtn("step", "step", "Advance one reporting event"),
      h(
        "div",
        { class: "segmented lv-speeds", role: "group", "aria-label": "Playback speed" },
        (clk.speeds || [1, 2, 5, 10, 25]).map((s) =>
          h("button", { type: "button", class: clk.speed === s ? "is-active" : "", "aria-pressed": clk.speed === s ? "true" : "false", disabled: finished, onclick: () => control("speed", s), title: `Playback speed ${s}×` }, `${s}×`),
        ),
      ),
      confirmButton("Finish", "finish", "Instant finish: reveal every remaining result and certify the election", () => control("finish"), finished),
      ready || finished ? null : confirmButton("Reset", "reset", "Reset the night to polls closing", () => control("reset"), false),
    );
  });
}

/** Update the national strip from the live night snapshot (or the final election summary). */
function updateStrip() {
  const { meta, electionId } = getState();
  if (!meta) return;
  renderPicker();
  const m = stripModel();
  renderEv(m);
  renderCounters(m);
  renderControls(m);
  const liveLink = document.querySelector('.nav__link[data-path="/night"]');
  if (liveLink) liveLink.classList.toggle("is-live", m.clock?.status === "running");
  void electionId;
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
  const loading = connectingState("Loading…");
  mount(main, loading);
  const slowHint = setTimeout(() => {
    if (token === renderToken && loading.isConnected)
      mount(loading, h("div", { class: "skeleton", style: { width: "240px", height: "18px" } }), h("span", { class: "muted" }, "Still loading — the first visit to an election night prepares it on the server, which can take up to a minute on a slow machine."));
  }, 6000);
  try {
    const mod = await import(`./views/${route.view}.js`);
    if (token !== renderToken) return;
    const el = h("div", { class: "view" });
    mount(main, el);
    const result = await mod.render(el, params, { query, electionId: getState().electionId, meta: getState().meta });
    if (token !== renderToken) {
      if (typeof result === "function") result();
      return;
    }
    cleanup = result;
    window.scrollTo(0, 0);
  } catch (err) {
    console.error(err);
    if (token !== renderToken) return;
    mount(main, h("div", { class: "state" }, h("h2", null, "Could not load this page"), h("p", { class: "muted" }, String(err.message || err))));
  } finally {
    clearTimeout(slowHint);
  }
}

export function selectElection(id) {
  setState({ electionId: id });
  safeStorage(() => localStorage.setItem(ELECTION_KEY, String(id)));
  watchNight(id);
}

/** Visible banner for unexpected errors (so a failure is never a silent endless spinner). */
function showErrorBanner(message) {
  let bar = document.getElementById("error-banner");
  if (!bar) {
    bar = h("div", { id: "error-banner", class: "error-banner", role: "alert" });
    document.body.appendChild(bar);
  }
  mount(
    bar,
    h("strong", null, "Something went wrong: "),
    h("span", null, String(message).slice(0, 400)),
    h("button", { class: "btn btn--sm", onclick: () => bar.remove(), "aria-label": "Dismiss" }, "Dismiss"),
  );
}

window.addEventListener("error", (e) => {
  if (e?.message) showErrorBanner(e.message);
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e?.reason;
  if (r?.name === "AbortError") return;
  showErrorBanner(r?.message || r || "unknown error");
});

function connectingState(text) {
  return h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "240px", height: "18px" } }), h("span", { class: "muted" }, text));
}

async function boot() {
  window.__nlfedBooted = true;
  const saved = safeStorage(() => localStorage.getItem(THEME_KEY));
  applyTheme(saved || "dark");
  const app = $("#app");
  mount(app, renderStrip(), renderNav(), h("main", { class: "main", id: "main" }, connectingState("Connecting to the simulator…")));
  subscribe("night", updateStrip);
  subscribe("meta", () => {
    // statuses changed (e.g. a night finished): final summaries must be refetched
    for (const [id, v] of finalCache) if (v !== "loading" && getElection(id)?.status !== "final") finalCache.delete(id);
    updateStrip();
  });
  subscribe("electionId", updateStrip);
  subscribe("settings", updateStrip);
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
    mount(
      $("#main"),
      h(
        "div",
        { class: "state" },
        h("h2", null, "Cannot load the simulator's data"),
        h("p", { class: "muted" }, String(err?.message || err)),
        h("p", { class: "muted" }, "Is `python -m app run` still running? Did `python -m app demo` finish with “validation ok”?"),
        h("button", { class: "btn btn--primary", onclick: () => location.reload() }, "Retry"),
      ),
    );
    return;
  }
  startRouter(onRoute);
}

boot();
