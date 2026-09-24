/**
 * Helpers shared by the live broadcast pages (night, president, electoral college, race).
 * Nothing here performs election mathematics: states, winners, margins and totals always come
 * from the API; these helpers only classify API statuses, pick colours and schedule refreshes.
 */
import { api } from "../api.js";
import { h } from "../dom.js";
import { decidedStatuses, getElection, getState, nightFor, partyColor } from "../store.js";

/* ------------------------------------------------------------------ colour */

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex || "").trim());
  if (!m) return null;
  let s = m[1];
  if (s.length === 3) s = s.split("").map((c) => c + c).join("");
  const n = parseInt(s, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Ink (white or near-black) that clears contrast on a solid fill of `hex`. */
export function inkOn(hex) {
  const rgb = hexToRgb(hex);
  if (!rgb) return "#ffffff";
  const lin = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const L = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  // contrast with white vs with #0b1220
  const cw = 1.05 / (L + 0.05);
  const cb = (L + 0.05) / (0.0065 + 0.05);
  return cw >= cb * 0.82 ? "#ffffff" : "#0b1220";
}

/** Resolved value of a CSS custom property (Leaflet canvas cannot resolve var()). */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Party colour honouring user overrides. */
export const colorFor = (party, fallback) => partyColor(party, fallback);

/* ------------------------------------------------------------------ statuses */

export function isDecided(status) {
  return decidedStatuses().includes(String(status || "").toUpperCase());
}

/**
 * Map-state of a province/race row, from the API status only:
 * uncalled · leading · close (too close to call) · called · recount.
 */
export function mapState(status, hasLeader) {
  const s = String(status || "").toUpperCase();
  if (s === "RECOUNT") return "recount";
  if (isDecided(s)) return "called";
  if (s === "TOO_CLOSE_TO_CALL") return "close";
  if (hasLeader) return "leading";
  return "uncalled";
}

export const MAP_STATE_LABELS = {
  uncalled: "Uncalled / no votes yet",
  leading: "Leading (not called)",
  close: "Too close to call",
  called: "Called",
  recount: "Recount",
};

/* ------------------------------------------------------------------ election phase */

/**
 * Phase of an election for the live pages:
 * `hidden` (scheduled/simulated, night not started), `ready`, `running`, `paused`, `finished`
 * (night done), `final` (reported, no night state).  Derived from the API statuses.
 */
export function phaseOf(election, night) {
  const clk = night?.clock?.status;
  if (clk === "running" || clk === "paused" || clk === "finished") return clk;
  if (clk === "ready") return "ready";
  const st = election?.status;
  if (st === "final" || st === "certified") return "final";
  if (st === "live") return "running";
  return "hidden";
}

export function isFinalElection(election) {
  return election?.status === "final" || election?.status === "certified";
}

/** Current results_source for the selected election, preferring the night status when fresher. */
export function resultsSourceOf(id) {
  const n = nightFor(id);
  if (n?.election_status) {
    const s = n.election_status;
    return s === "final" || s === "certified" ? "final" : s === "live" ? "live" : "hidden";
  }
  return getElection(id)?.results_source || "hidden";
}

/* ------------------------------------------------------------------ names */

/** "Charlotte Verbeek / Joost Brinkman" → "Charlotte Verbeek". */
export function ticketHead(label) {
  return String(label || "").split(" / ")[0];
}

export function lastName(name) {
  const s = String(name || "").trim();
  const m = /\b((?:van |de |der |den |ter |ten |te |van der |van den |de la )*[^\s]+)$/i.exec(s);
  return m ? m[1] : s;
}

/**
 * Key → {label, party, color} index for ballot lines, fed from any payload that carries them
 * (tickets, race lines, night full snapshots).  Per election.
 */
export function createLineIndex() {
  const map = new Map();
  const put = (key, v) => {
    if (!key) return;
    const old = map.get(key) || {};
    map.set(key, { ...old, ...Object.fromEntries(Object.entries(v).filter(([, x]) => x !== undefined && x !== null)) });
  };
  return {
    map,
    get: (key) => map.get(key) || null,
    label: (key) => map.get(key)?.label || key || "–",
    party: (key) => map.get(key)?.party || null,
    color: (key) => {
      const v = map.get(key);
      return v ? partyColor(v.party, v.color) : "var(--uncalled)";
    },
    addTickets(tickets = []) {
      for (const t of tickets) put(t.key, { label: t.name || t.label, party: t.party, color: t.color, portrait: t.president?.portrait_key, head: t.president?.name });
    },
    addLines(lines = []) {
      for (const l of lines) put(l.key, { label: l.candidate?.name || l.name || l.label, party: l.party, color: l.color });
    },
    addFullRaces(races = []) {
      for (const r of races) {
        const labels = r.line_labels || {};
        for (const k of Object.keys(labels)) put(k, { label: labels[k], party: r.line_parties?.[k], color: r.line_colors?.[k] });
      }
    },
    size: () => map.size,
  };
}

/* ------------------------------------------------------------------ scheduling */

/**
 * Run `fn` every `ms` while the page is visible; returns stop().  `fn` may be async; a slow run
 * never overlaps the next one.
 */
export function every(ms, fn, { immediate = false } = {}) {
  let stopped = false;
  let t = null;
  let busy = false;
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden && !busy) {
      busy = true;
      try {
        await fn();
      } catch (e) {
        if (e?.name !== "AbortError") console.warn(e);
      } finally {
        busy = false;
      }
    }
    if (!stopped) t = setTimeout(tick, ms);
  };
  t = setTimeout(tick, immediate ? 0 : ms);
  return () => {
    stopped = true;
    clearTimeout(t);
  };
}

/**
 * A throttled refetcher: `request()` asks for a refresh; at most one request is in flight and
 * consecutive runs are at least `minInterval` ms apart.  Aborted on `stop()`.
 */
export function throttledFetch(fn, { minInterval = 3000 } = {}) {
  let last = 0;
  let pending = false;
  let timer = null;
  let inflight = null;
  let ctrl = null;
  let stopped = false;
  const run = async () => {
    if (stopped) return;
    pending = false;
    last = Date.now();
    ctrl = new AbortController();
    inflight = (async () => {
      try {
        await fn(ctrl.signal);
      } catch (e) {
        if (e?.name !== "AbortError" && !stopped) console.warn(e);
      }
    })();
    await inflight;
    inflight = null;
    if (pending && !stopped) schedule();
  };
  const schedule = () => {
    clearTimeout(timer);
    const wait = Math.max(0, minInterval - (Date.now() - last));
    timer = setTimeout(run, wait);
  };
  return {
    request() {
      if (stopped) return;
      pending = true;
      if (!inflight) schedule();
    },
    now() {
      if (stopped) return;
      pending = true;
      if (!inflight) {
        clearTimeout(timer);
        run();
      }
    },
    stop() {
      stopped = true;
      clearTimeout(timer);
      ctrl?.abort();
    },
  };
}

/** GET with an abort signal (api.get wrapper). */
export const fetchJSON = (path, signal) => api.get(path, { signal });

/* ------------------------------------------------------------------ small DOM helpers */

/** Set text only when it changed (keeps focus/selection and avoids layout churn). */
export function setText(el, text) {
  const s = text === null || text === undefined ? "" : String(text);
  if (el && el.textContent !== s) el.textContent = s;
  return el;
}

/** Briefly flash an element (value changed). */
export function flash(el, cls = "lv-flash") {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth; // restart the animation
  el.classList.add(cls);
}

/** Section heading inside a card body. */
export function subhead(text, extra) {
  return h("div", { class: "lv-subhead" }, h("span", null, text), extra || null);
}

/** Phase chip: LIVE / PAUSED / READY / FINAL / SCHEDULED. */
export function phaseChip(phase) {
  const map = {
    running: ["live", "LIVE"],
    paused: ["paused", "PAUSED"],
    ready: ["ready", "POLLS CLOSED · READY"],
    finished: ["final", "FINAL"],
    final: ["final", "FINAL"],
    hidden: ["ready", "NOT REPORTED"],
  };
  const [cls, label] = map[phase] || map.hidden;
  return h("span", { class: `lv-phase lv-phase--${cls}` }, cls === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, label);
}

/** meta constitution with the documented defaults. */
export function constitution() {
  const c = getState().meta?.constitution || {};
  return {
    ev: c.electoral_votes ?? 174,
    evMajority: c.presidential_majority ?? 88,
    house: c.house_seats ?? 150,
    houseMajority: c.house_majority ?? 76,
    senate: c.senate_seats ?? 24,
    senateMajority: c.senate_majority ?? 13,
    labels: c.labels || { president: "88 TO WIN", house: "76 FOR CONTROL", senate: "13 FOR CONTROL" },
  };
}

/** Human label for decided_by values. */
export function decidedByLabel(v) {
  return (
    {
      popular_vote: "Popular vote",
      recount: "Recount",
      lot: "Drawing of lots",
      electoral_college: "Electoral College",
      contingent: "Contingent election",
      house_delegations: "House province delegations",
      senate: "Senate",
    }[v] || (v ? String(v).replace(/_/g, " ") : "–")
  );
}

/** Keyboard + pointer hover helper: show a tooltip node for an element. */
export function hoverTip(el, content, showTip, hideTip) {
  el.addEventListener("mousemove", (e) => showTip(e, typeof content === "function" ? content() : content));
  el.addEventListener("mouseleave", hideTip);
  el.addEventListener("focus", () => {
    const r = el.getBoundingClientRect();
    showTip({ clientX: r.left + r.width / 2, clientY: r.bottom }, typeof content === "function" ? content() : content);
  });
  el.addEventListener("blur", hideTip);
  return el;
}
