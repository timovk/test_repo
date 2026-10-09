/**
 * Local elections kit (prefix `lc-`): race kinds and labels, the results-listing row used by the
 * Local results page and the local election night, the Yes/No bar of ballot measures and recalls,
 * the race browser (filter chips, search, municipality filter, two-column grid) and the strict
 * date-order helpers (the "earlier elections must be finished first" banner and the one-click
 * finish-earlier flow).
 *
 * Nothing here decides a result: winners, passed / passing flags, Yes shares, thresholds and
 * statuses are API values.  The kit only filters, counts flags and formats them.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { decidedStatuses, getElection, getState, setState } from "../store.js";
import { stopNight, watchNight } from "../night-poller.js";
import { icon } from "./icons.js";
import { toast } from "./live-toast.js";
import { pc } from "./res-kit.js";

/* ------------------------------------------------------------------ race kinds */
export const LOCAL_RACE_TYPES = ["SCHOOL_BOARD", "WATER_BOARD", "BALLOT_MEASURE", "RECALL", "MAYOR", "COUNCIL_SEAT"];

/** [singular, plural] display labels of race types. */
export const TYPE_LABELS = {
  SCHOOL_BOARD: ["School board", "School boards"],
  WATER_BOARD: ["Water board", "Water boards"],
  BALLOT_MEASURE: ["Ballot measure", "Measures"],
  RECALL: ["Recall", "Recalls"],
  MAYOR: ["Mayor", "Mayors"],
  COUNCIL_SEAT: ["Council seat", "Council seats"],
  PRESIDENT: ["President", "President"],
  PRESIDENT_PROVINCE: ["Electoral votes", "President by province"],
  SENATE: ["Senate", "Senate"],
  HOUSE: ["House", "House"],
  GOVERNOR: ["Governor", "Governors"],
  MUNICIPAL_COUNCIL: ["Municipal council", "Councils"],
  PROVINCIAL_LEGISLATURE: ["Provincial legislature", "Legislatures"],
};
const TYPE_ORDER = ["PRESIDENT", "PRESIDENT_PROVINCE", "SENATE", "HOUSE", "GOVERNOR", "PROVINCIAL_LEGISLATURE", "MAYOR", "MUNICIPAL_COUNCIL", ...LOCAL_RACE_TYPES];

/** Filter chips of the local listing: [key, label, race types]. */
export const LOCAL_GROUPS = [
  ["school", "School boards", ["SCHOOL_BOARD"]],
  ["measures", "Measures", ["BALLOT_MEASURE"]],
  ["water", "Water boards", ["WATER_BOARD"]],
  ["specials", "Specials & recalls", ["MAYOR", "COUNCIL_SEAT", "RECALL"]],
];

export function typeLabel(type, plural = false) {
  const l = TYPE_LABELS[String(type || "").toUpperCase()];
  if (l) return l[plural ? 1 : 0];
  const s = String(type || "").replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Chip groups with counts: the local grouping for local elections, one chip per type otherwise. */
export function raceGroups(counts = {}, local = true) {
  if (local) return LOCAL_GROUPS.map(([key, label, types]) => ({ key, label, types, count: types.reduce((a, t) => a + (counts[t] || 0), 0) }));
  const idx = (t) => (TYPE_ORDER.indexOf(t) < 0 ? 99 : TYPE_ORDER.indexOf(t));
  return Object.keys(counts)
    .sort((a, b) => idx(a) - idx(b))
    .map((t) => ({ key: t, label: typeLabel(t, true), types: [t], count: counts[t] }));
}

export const isQuestion = (r) => r?.type === "BALLOT_MEASURE" || r?.type === "RECALL" || r?.question === true;
export const isDecidedStatus = (s) => decidedStatuses().includes(String(s || "").toUpperCase());

/* ------------------------------------------------------------------ provinces & dates */
const provNames = new Map();
let provPromise = null;

/** Province code → name map from /api/provinces (REAL), loaded once. */
export function loadProvinceNames() {
  if (!provPromise)
    provPromise = api
      .get("/api/provinces", { cache: true })
      .then((d) => {
        for (const p of d.provinces || []) provNames.set(p.code, p.name);
        return provNames;
      })
      .catch(() => {
        provPromise = null;
        return provNames;
      });
  return provPromise;
}

/** Province name of a code (falls back to the name inside a local election's own name). */
export function provinceName(code, election) {
  if (!code) return "";
  if (provNames.has(code)) return provNames.get(code);
  const m = /^(.+?) Local Elections/.exec(election?.name || "");
  return m ? m[1] : code;
}

const DF = {
  short: new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }),
  long: new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }),
  weekday: new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" }),
  month: new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" }),
  mon: new Intl.DateTimeFormat("en-GB", { month: "short", timeZone: "UTC" }),
};

export function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(`${String(iso).slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "2027-07-08" → "8 Jul 2027" (style short) / "8 July 2027" (long) / "Thu" / "July 2027" / "Jul". */
export function fmtDate(iso, style = "short") {
  const d = parseDate(iso);
  if (!d) return iso || "–";
  return (DF[style] || DF.short).format(d);
}

/* ------------------------------------------------------------------ names & thresholds */
/** "Mees van Beek" → "M. van Beek" (listing style). */
export function shortName(name) {
  const s = String(name || "").trim();
  if (!s || /^(yes|no)$/i.test(s)) return s;
  const parts = s.split(/\s+/);
  if (parts.length < 2) return s;
  return `${parts[0].charAt(0)}. ${parts.slice(1).join(" ")}`;
}

const pctOf = (t) => `${Math.round(t * 1000) / 10}%`;

/** "needs 60%" / "needs >50%" (a tie fails a simple-majority question). */
export function thresholdText(t) {
  if (t === null || t === undefined) return "";
  if (t <= 0.5) return "needs >50%";
  return `needs ${pctOf(t)}`;
}

/** Longer rule text for the race page. */
export function thresholdRule(t) {
  if (t === null || t === undefined) return "–";
  if (t <= 0.5) return "Simple majority: more than 50% Yes (a tie fails)";
  if (Math.abs(t - 2 / 3) < 0.005) return `Two-thirds supermajority: at least ${pctOf(t)} Yes`;
  return `Supermajority: at least ${pctOf(t)} Yes`;
}

export function thresholdShort(t) {
  if (t === null || t === undefined) return "–";
  if (t <= 0.5) return "Majority";
  if (Math.abs(t - 2 / 3) < 0.005) return "Two-thirds";
  return pctOf(t);
}

/* ------------------------------------------------------------------ outcomes */
/**
 * Outcome of a Yes/No question from the API flags: passed (final) / failed, else passing /
 * failing while counting.  `r` is a race row or {type, passed, passing}.
 */
export function outcomeOf(r) {
  const recall = r?.type === "RECALL";
  const words = recall
    ? { passed: "Recalled", failed: "Retained", passing: "Recall passing", failing: "Recall failing" }
    : { passed: "Passed", failed: "Failed", passing: "Passing", failing: "Failing" };
  let state = null;
  if (r?.passed === true) state = "passed";
  else if (r?.passed === false) state = "failed";
  else if (r?.passing === true) state = "passing";
  else if (r?.passing === false) state = "failing";
  return { state, label: state ? words[state] : null };
}

export function outcomeTag(r, { size } = {}) {
  const o = outcomeOf(r);
  if (!o.state) return null;
  const final = o.state === "passed" || o.state === "failed";
  const ok = o.state === "passed" || o.state === "passing";
  return h(
    "span",
    { class: ["lc-out", `lc-out--${o.state}`, size && `lc-out--${size}`], title: final ? "Certified outcome" : "Current count — not final" },
    icon(ok ? "check" : "x", { size: size === "lg" ? 14 : 11 }),
    final ? o.label : `${o.label}…`,
  );
}

/** Accent colour of a race row: outcome for questions, the leader's party for candidate races. */
function accentOf(r, counted) {
  if (isQuestion(r)) {
    const s = outcomeOf(r).state;
    if (!counted || !s) return "var(--uncalled)";
    return s === "passed" || s === "passing" ? "var(--yes)" : "var(--no)";
  }
  if (!counted) return "var(--uncalled)";
  return r.leader_party ? pc(r.leader_party, r.leader_color) : r.leader_color || "var(--uncalled)";
}

/* ------------------------------------------------------------------ Yes/No bar */
/**
 * Yes/No bar with a marker at the threshold.  `size`: sm (listing rows) | lg (race page, with
 * labels).  `yesPct` 0–100 (null = nothing counted yet).
 */
export function yesNoBar(yesPct, threshold, { size = "sm", noPct, yesVotes, noVotes } = {}) {
  const has = yesPct !== null && yesPct !== undefined;
  const t = threshold ?? 0.5;
  const no = has ? (noPct ?? 100 - yesPct) : null;
  const label = has ? `Yes ${fmtPct(yesPct)}, No ${fmtPct(no)}; ${thresholdText(t)}` : `No votes counted yet; ${thresholdText(t)}`;
  return h(
    "span",
    { class: ["lc-yn", `lc-yn--${size}`, !has && "is-empty"], role: "img", "aria-label": label, title: size === "sm" ? label : undefined },
    size === "lg"
      ? h(
          "span",
          { class: "lc-yn__legend" },
          h("span", { class: "lc-yn__side lc-yn__side--yes" }, h("b", null, "Yes"), h("span", { class: "num" }, has ? fmtPct(yesPct) : "–"), yesVotes !== undefined && yesVotes !== null ? h("span", { class: "muted num" }, `${fmtInt(yesVotes)} votes`) : null),
          h("span", { class: "lc-yn__side lc-yn__side--no" }, noVotes !== undefined && noVotes !== null ? h("span", { class: "muted num" }, `${fmtInt(noVotes)} votes`) : null, h("span", { class: "num" }, has ? fmtPct(no) : "–"), h("b", null, "No")),
        )
      : null,
    h(
      "span",
      { class: "lc-yn__track" },
      h("span", { class: "lc-yn__yes", style: { width: has ? `${Math.max(0, Math.min(100, yesPct))}%` : "0%" } }),
      h("span", { class: "lc-yn__no", style: { width: has ? `${Math.max(0, Math.min(100, no))}%` : "0%" } }),
      h("span", { class: "lc-yn__mark", style: { left: `${t * 100}%` } }, size === "lg" ? h("span", { class: "lc-yn__marklabel" }, t <= 0.5 ? "50% + 1" : `${pctOf(t)} needed`) : null),
    ),
  );
}

/* ------------------------------------------------------------------ status tag */
/** Listing status tag: LIVE / Projected / Called / Final / Recount / Polls closed / Hidden / Moot. */
export function statusTag(r, source) {
  const s = String(r?.status || "").toUpperCase();
  const tag = (kind, content, title) => h("span", { class: `lc-tag lc-tag--${kind}`, title }, content);
  if (source === "hidden") return tag("hidden", [icon("lock", { size: 10 }), "Hidden"], "Results hidden until election night");
  if (r?.moot) return tag("moot", "Moot", "The recall failed: this replacement race does not take effect");
  if (s === "RECOUNT") return tag("recount", [icon("alert", { size: 10 }), "Recount"], "Automatic recount");
  if (s === "FINAL" || source === "final") return tag("final", [icon("lock", { size: 10 }), "Final"], "Certified result");
  if (s === "CALLED") return tag("called", [icon("check", { size: 10 }), "Called"], "Called by the decision desk (SIMULATED)");
  if (s === "PROJECTED_WINNER") return tag("projected", "Projected", "Projected by the calling model (SIMULATED)");
  if ((r?.reporting_pct ?? 0) > 0) return tag("live", [h("span", { class: "live-dot", "aria-hidden": "true" }), "Live"], "Counting");
  return tag("closed", "Polls closed", "No votes counted yet");
}

/* ------------------------------------------------------------------ listing row */
const sep = () => h("span", { class: "lc-sep", "aria-hidden": "true" }, "·");
const plural = (n, one, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;
export const raceHref = (code, electionId) => `#/races/${encodeURIComponent(code)}${electionId ? `?e=${electionId}` : ""}`;

function sortedTop(r) {
  return [...(r.top || [])].sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
}

function candidateDetail(r, src, counted) {
  const n = r.candidates ?? (r.top || []).length;
  const voteFor = r.vote_for > 1 ? h("span", { class: "lc-votefor" }, `vote for ${r.vote_for}`) : null;
  if (!counted) {
    return [
      h("span", null, plural(n, "candidate")),
      voteFor ? [sep(), voteFor] : null,
      src === "live" ? [sep(), h("span", { class: "muted" }, "No votes counted yet")] : null,
    ];
  }
  const top = sortedTop(r);
  const winners = r.winners || [];
  const decided = r.decided && winners.length > 0;
  if (r.vote_for > 1) {
    const names = decided ? winners : top.slice(0, r.vote_for).map((t) => t.name);
    const others = Math.max(0, n - names.length);
    // Large boards (a water board elects up to 11 at once): name the first three.
    const shown = names.length > 4 ? names.slice(0, 3) : names;
    return [
      decided ? h("span", { class: "lc-check", title: r.status === "PROJECTED_WINNER" ? "Projected winners" : "Elected" }, icon("check", { size: 11 })) : null,
      h("span", { class: "lc-row__lab" }, !decided ? "Leading" : r.status === "PROJECTED_WINNER" ? "Projected" : "Elected"),
      h("b", { class: "lc-row__who" }, shown.map(shortName).join(", ")),
      names.length > shown.length ? h("span", { class: "muted" }, `+${fmtInt(names.length - shown.length)} more`) : null,
      others ? h("span", { class: "muted" }, `| +${plural(others, "candidate")}`) : null,
      voteFor ? [sep(), voteFor] : null,
    ];
  }
  const lead = top.find((t) => t.name === r.leader) || top[0];
  if (!lead) return h("span", null, plural(n, "candidate"));
  const won = decided && winners.includes(lead.name);
  const color = lead.party ? pc(lead.party, lead.color) : lead.color || "var(--uncalled)";
  const others = Math.max(0, n - 1);
  return [
    h("span", { class: "chip__swatch", style: { "--party": color }, title: lead.party || "Nonpartisan" }),
    won ? h("span", { class: "lc-check", title: r.status === "FINAL" ? "Winner" : "Projected winner" }, icon("check", { size: 11 })) : null,
    h("b", { class: "lc-row__who" }, shortName(lead.name)),
    lead.party ? h("span", { class: "muted" }, lead.party) : null,
    lead.pct !== null && lead.pct !== undefined ? h("span", { class: "num lc-row__pct" }, fmtPct(lead.pct)) : null,
    others ? h("span", { class: "muted" }, `| +${plural(others, "candidate")}`) : null,
  ];
}

function questionDetail(r, src, counted, ctx) {
  const yes = counted ? r.yes_pct : null;
  const parts = [];
  if (r.type === "RECALL" && r.target_name)
    parts.push(
      h("span", { class: "chip__swatch", style: { "--party": r.target_party ? pc(r.target_party) : "var(--uncalled)" } }),
      h("span", null, "Mayor ", h("b", { class: "lc-row__who" }, r.target_name), r.target_party ? h("span", { class: "muted" }, ` ${r.target_party}`) : null),
      sep(),
    );
  parts.push(yesNoBar(yes, r.threshold, { size: "sm" }));
  parts.push(
    yes !== null && yes !== undefined
      ? h("span", { class: "num" }, h("b", null, `Yes ${fmtPct(yes)}`), h("span", { class: "muted" }, ` · ${thresholdText(r.threshold)}`))
      : h("span", { class: "muted" }, `Yes / No · ${thresholdText(r.threshold)}`),
  );
  if (counted) parts.push(outcomeTag(r));
  if (r.type === "RECALL" && counted && outcomeOf(r).state) {
    const rep = r.replacement_race && ctx.rowsByCode?.get(r.replacement_race);
    if (rep && (r.passed === true || r.passing === true) && rep.winners?.length) parts.push(h("span", { class: "muted" }, `→ ${shortName(rep.winners[0])}${rep.leader_party ? ` (${rep.leader_party})` : ""}`));
  }
  return parts;
}

/** One listing row (link to the race page). `ctx`: {source, electionId, rowsByCode}. */
export function raceRow(r, ctx = {}) {
  const src = ctx.source || "hidden";
  const counted = src !== "hidden" && (r.reporting_pct ?? 0) > 0 && (r.leader_pct !== null && r.leader_pct !== undefined);
  const question = isQuestion(r);
  const kind = question ? (r.type === "RECALL" ? "recall" : "measure") : r.vote_for > 1 ? "board" : "candidate";
  let detail;
  if (r.moot) detail = [h("span", { class: "lc-moot" }, "Moot: the recall failed"), sep(), h("span", { class: "muted" }, "no replacement mayor is elected")];
  else if (question) detail = questionDetail(r, src, counted, ctx);
  else detail = candidateDetail(r, src, counted);
  const live = src === "live" && (r.reporting_pct ?? 0) > 0 && (r.reporting_pct ?? 0) < 100 && !(r.status === "FINAL");
  return h(
    "a",
    { class: ["lc-row", `lc-row--${kind}`, r.moot && "is-moot"], href: raceHref(r.code, ctx.electionId), style: { "--party": accentOf(r, counted) }, "data-code": r.code },
    h(
      "span",
      { class: "lc-row__main" },
      h(
        "span",
        { class: "lc-row__name" },
        h("span", { class: "lc-row__title" }, r.name),
        r.is_special && r.type !== "COUNCIL_SEAT" && !/special|replacement/i.test(r.name) ? h("span", { class: "res-tag" }, "SPECIAL") : null,
      ),
      h("span", { class: "lc-row__detail" }, detail),
    ),
    h(
      "span",
      { class: "lc-row__side" },
      statusTag(r, src),
      live ? h("span", { class: "lc-row__in num" }, `${fmtPct(r.reporting_pct, 0)} in`) : null,
    ),
    live ? h("span", { class: "lc-row__progress", "aria-hidden": "true" }, h("span", { style: { width: `${Math.min(100, r.reporting_pct)}%` } })) : null,
  );
}

/* ------------------------------------------------------------------ tallies */
/** Count API flags of a races payload (no results are computed). */
export function tally(rows = [], source = "hidden") {
  const t = { total: rows.length, decided: 0, measures: 0, measuresYes: 0, measuresNo: 0, boards: 0, seatsUp: 0, seatsFilled: 0, specials: 0, recalls: 0, municipalities: new Set(), counting: 0 };
  for (const r of rows) {
    if (r.municipality_code) t.municipalities.add(r.municipality_code);
    if (r.decided || r.moot) t.decided += 1;
    if ((r.reporting_pct ?? 0) > 0) t.counting += 1;
    if (r.type === "BALLOT_MEASURE") {
      t.measures += 1;
      // passing / failing only mean something once votes are counted (passed / failed: final).
      const s = r.passed === true || r.passed === false || (r.reporting_pct ?? 0) > 0 ? outcomeOf(r).state : null;
      if (source !== "hidden" && (s === "passed" || s === "passing")) t.measuresYes += 1;
      if (source !== "hidden" && (s === "failed" || s === "failing")) t.measuresNo += 1;
    }
    if (r.type === "SCHOOL_BOARD" || r.type === "WATER_BOARD") {
      t.boards += 1;
      t.seatsUp += r.vote_for || 1;
      if (r.decided) t.seatsFilled += (r.winners || []).length;
    }
    if (r.type === "RECALL") t.recalls += 1;
    if (r.is_special) t.specials += 1;
  }
  t.municipalityCount = t.municipalities.size;
  return t;
}

/* ------------------------------------------------------------------ race browser */
/**
 * Filter chips + search + municipality filter + the two-column listing.
 * `update(payload)` takes a /races payload; the browser keeps its filters across updates.
 */
export function raceBrowser({ electionId, local = true, pageSize = 120, municipalityFilter = true, onChange } = {}) {
  const st = { group: "", q: "", muni: "", rows: [], counts: {}, source: "hidden", limit: pageSize };
  const chips = h("div", { class: "lc-chips", role: "group", "aria-label": "Race type" });
  let searchTimer = null;
  const search = h("input", {
    class: "input lc-search__input",
    type: "search",
    placeholder: "Search for a race…",
    "aria-label": "Search for a race, place or candidate",
    oninput: (e) => {
      clearTimeout(searchTimer);
      const v = e.target.value;
      searchTimer = setTimeout(() => {
        st.q = v.trim().toLowerCase();
        st.limit = pageSize;
        paint();
      }, 160);
    },
  });
  const searchBox = h("label", { class: "lc-search" }, icon("search", { size: 14 }), search);
  const muniSel = h("select", {
    class: "select lc-muni",
    "aria-label": "Municipality",
    onchange: (e) => {
      st.muni = e.target.value;
      st.limit = pageSize;
      paint();
    },
  });
  const toolbar = h("div", { class: "lc-toolbar" }, chips, h("div", { class: "lc-toolbar__right" }, municipalityFilter ? muniSel : null, searchBox));
  const grid = h("div", { class: "lc-grid", role: "list" });
  const more = h("div", { class: "lc-more" });
  const empty = h("div", { class: "state lc-empty" });
  const el = h("div", { class: "lc-browser" }, toolbar, grid, empty, more);

  function groupTypes() {
    if (!st.group) return null;
    const g = raceGroups(st.counts, local).find((x) => x.key === st.group);
    return g ? g.types : null;
  }

  function paintChips() {
    const groups = raceGroups(st.counts, local);
    const total = st.rows.length;
    const key = JSON.stringify([st.group, total, groups.map((g) => [g.key, g.count])]);
    if (chips.dataset.key === key) return;
    chips.dataset.key = key;
    const chip = (k, label, count) =>
      h(
        "button",
        {
          type: "button",
          class: ["lc-chip", st.group === k && "is-active"],
          "aria-pressed": String(st.group === k),
          disabled: k && !count,
          onclick: () => {
            st.group = k;
            st.limit = pageSize;
            paintChips();
            paint();
          },
        },
        label,
        h("span", { class: "lc-chip__n num" }, fmtInt(count)),
      );
    mount(chips, chip("", "All", total), groups.map((g) => chip(g.key, g.label, g.count)));
  }

  function paintMunis() {
    const m = new Map();
    for (const r of st.rows) if (r.municipality_code) m.set(r.municipality_code, r.municipality_name || r.municipality_code);
    const list = [...m.entries()].sort((a, b) => a[1].localeCompare(b[1], "nl"));
    const key = JSON.stringify([st.muni, list.map((x) => x[0])]);
    if (muniSel.dataset.key === key) return;
    muniSel.dataset.key = key;
    if (st.muni && !m.has(st.muni)) st.muni = "";
    muniSel.hidden = list.length < 2;
    mount(muniSel, h("option", { value: "" }, `All municipalities (${list.length})`), list.map(([code, name]) => h("option", { value: code, selected: code === st.muni }, name)));
  }

  function haystack(r) {
    return [r.name, r.code, r.municipality_name, r.label, r.title, ...(r.top || []).map((t) => t.name), ...(r.winners || [])].filter(Boolean).join(" ").toLowerCase();
  }

  function filtered() {
    const types = groupTypes();
    return st.rows.filter((r) => (!types || types.includes(r.type)) && (!st.muni || r.municipality_code === st.muni) && (!st.q || haystack(r).includes(st.q)));
  }

  let lastSig = "";
  function paint() {
    const rows = filtered();
    const shown = rows.slice(0, st.limit);
    const sig = JSON.stringify([st.source, st.limit, shown.map((r) => [r.code, r.status, r.reporting_pct, r.leader, r.leader_pct, r.winners, r.passed, r.passing, r.yes_pct, r.moot])]);
    if (sig !== lastSig) {
      lastSig = sig;
      const rowsByCode = new Map(st.rows.map((r) => [r.code, r]));
      mount(grid, shown.map((r) => raceRow(r, { source: st.source, electionId, rowsByCode })));
    }
    grid.hidden = !shown.length;
    empty.hidden = shown.length > 0;
    if (!shown.length)
      mount(
        empty,
        st.rows.length
          ? [h("span", null, st.q ? `No races match “${st.q}”.` : "No races in this selection."), h("button", { class: "btn btn--sm", type: "button", onclick: reset }, "Clear filters")]
          : h("span", null, "No races on this ballot."),
      );
    const rest = rows.length - shown.length;
    mount(
      more,
      rest > 0
        ? h(
            "button",
            {
              class: "btn btn--sm",
              type: "button",
              onclick: () => {
                st.limit += pageSize;
                paint();
              },
            },
            `Show ${fmtInt(Math.min(rest, pageSize))} more of ${fmtInt(rest)}`,
          )
        : null,
    );
    more.hidden = rest <= 0;
    onChange?.({ shown: shown.length, matching: rows.length, total: st.rows.length });
  }

  function reset() {
    st.group = "";
    st.q = "";
    st.muni = "";
    search.value = "";
    muniSel.value = "";
    st.limit = pageSize;
    paintChips();
    paintMunis();
    paint();
  }

  function update(payload) {
    st.rows = payload?.races || [];
    st.counts = payload?.counts || {};
    st.source = payload?.results_source || "hidden";
    paintChips();
    paintMunis();
    paint();
  }

  return { el, toolbar, update, reset };
}

/* ------------------------------------------------------------------ strict date order */
let running = null; // {id, promise}
const runListeners = new Set();

/** Subscribe to finish-earlier progress (start / end); returns unsubscribe. */
export function onFinishEarlier(fn) {
  runListeners.add(fn);
  return () => runListeners.delete(fn);
}

export const finishingEarlier = () => (running ? running.id : null);

/**
 * POST /api/elections/{id}/finish-earlier (minutes for many elections: 600 s timeout), then
 * refresh /api/meta and re-render the current view.  Concurrent calls share one request.
 */
export function finishEarlier(id) {
  if (running) return running.promise;
  const busy = toast("Finishing the earlier elections in date order… This can take a few minutes.", { busy: true, timeout: 0 });
  const promise = (async () => {
    try {
      const res = await api.post(`/api/elections/${id}/finish-earlier`, {}, { timeout: 600000 });
      api.invalidate("/api/");
      try {
        setState({ meta: await api.get("/api/meta") });
      } catch {
        /* the poller refreshes it later */
      }
      const n = res?.finished?.length ?? 0;
      const left = res?.count ?? 0;
      toast(
        left > 0
          ? `Finished ${plural(n, "earlier election")}; ${plural(left, "election")} still to go.`
          : n
            ? `Finished ${plural(n, "earlier election")}. This election can be counted now.`
            : "Every earlier election was already finished.",
        { kind: left > 0 ? "error" : "info", timeout: 6000 },
      );
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      return res;
    } catch (err) {
      toast(err?.message || "Could not finish the earlier elections", { kind: "error", timeout: 9000 });
      throw err;
    } finally {
      busy.dismiss();
      running = null;
      runListeners.forEach((fn) => fn(null));
    }
  })();
  running = { id, promise };
  runListeners.forEach((fn) => fn(id));
  return promise;
}

/** Is a 409 "must be finished first" refusal (strict date order)? */
export const isEarlierRefusal = (err) => err?.status === 409 && /finished first/i.test(String(err?.message || ""));

/**
 * Banner shown on the night page of an unreported election whose earlier elections are not all
 * finished: "N earlier elections must be finished first (next: …)" + "Finish earlier elections".
 * Returns an element with `.refresh()` and `.destroy()`.
 */
export function earlierBanner(electionId) {
  const el = h("div", { class: "lc-earlier-slot" });
  let info = null;
  let dead = false;
  const reported = () => {
    const e = getElection(electionId);
    return !e || e.reported || e.status === "final" || e.status === "certified" || e.status === "live";
  };
  async function refresh() {
    if (reported()) {
      info = null;
      mount(el);
      return;
    }
    try {
      info = await api.get(`/api/elections/${electionId}/earlier`);
    } catch {
      info = null;
    }
    if (!dead) paint();
  }
  function paint() {
    if (!info || !info.count || reported()) {
      mount(el);
      return;
    }
    const busy = finishingEarlier() !== null;
    const f = info.first || {};
    const detail = [];
    if (info.unreported_count) detail.push(`${plural(info.unreported_count, "stored election")} not yet reported`);
    if (info.not_created_count) detail.push(`${plural(info.not_created_count, "planned local election")} not created yet`);
    mount(
      el,
      h(
        "div",
        { class: ["banner", "lv-banner", "lv-banner--alert", "lc-earlier"], role: "status" },
        h("span", { class: "lv-banner__icon" }, icon("calendar", { size: 20 })),
        h(
          "div",
          { class: "lv-banner__text" },
          h("div", { class: "banner__title" }, `${plural(info.count, "earlier election")} must be finished first`),
          h(
            "div",
            { class: "lv-banner__sub" },
            "Elections are certified in strict date order",
            f.name ? [" — next: ", h("b", null, f.name), f.date && !String(f.name).includes(fmtDate(f.date, "long")) ? `, ${fmtDate(f.date, "long")}` : ""] : "",
            ". ",
            detail.length ? `${detail.join(" · ")}. ` : "",
            busy ? h("span", { class: "lc-earlier__busy" }, h("span", { class: "lc-spinner", "aria-hidden": "true" }), "Finishing them now — this can take a few minutes.") : "Finishing them certifies each one instantly, in date order.",
          ),
        ),
        h(
          "button",
          { class: "btn btn--primary lv-banner__link", type: "button", disabled: busy, onclick: () => finishEarlier(electionId).catch(() => null) },
          busy ? [h("span", { class: "lc-spinner lc-spinner--ink", "aria-hidden": "true" }), "Finishing…"] : "Finish earlier elections",
        ),
      ),
    );
  }
  const unsub = onFinishEarlier(() => paint());
  refresh();
  el.refresh = refresh;
  el.destroy = () => {
    dead = true;
    unsub();
  };
  return el;
}

/* ------------------------------------------------------------------ scheduled → ready */
let preparing = null; // election id being simulated

/**
 * A SCHEDULED election has no hidden result and no night yet: POST /api/elections/{id}/simulate
 * creates both (results stay hidden), then the night page shows "polls closed · ready".
 */
export async function prepareNight(id) {
  if (preparing) return null;
  preparing = id;
  runListeners.forEach((fn) => fn(null));
  const busy = toast("Simulating the hidden result and the election-night timeline…", { busy: true, timeout: 0 });
  try {
    await api.post(`/api/elections/${id}/simulate`, {}, { timeout: 300000 });
    api.invalidate("/api/");
    try {
      setState({ meta: await api.get("/api/meta") });
    } catch {
      /* the poller refreshes it later */
    }
    stopNight();
    setState({ night: null });
    watchNight(id);
    toast("The night is ready at polls closing — press Start in the top bar.", { timeout: 6000 });
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    return true;
  } catch (err) {
    toast(err?.message || "Could not simulate the election", { kind: "error", timeout: 9000 });
    return false;
  } finally {
    busy.dismiss();
    preparing = null;
    runListeners.forEach((fn) => fn(null));
  }
}

/** Banner for a SCHEDULED election: "not simulated yet" + "Prepare the night". */
export function scheduledBanner(electionId) {
  const el = h("div", { class: "lc-earlier-slot" });
  function paint() {
    const e = getElection(electionId);
    if (!e || e.status !== "scheduled") {
      mount(el);
      return;
    }
    const busy = preparing === electionId;
    mount(
      el,
      h(
        "div",
        { class: ["banner", "lv-banner", "lc-sched"], role: "status" },
        h("span", { class: "lv-banner__icon" }, icon("night", { size: 20 })),
        h(
          "div",
          { class: "lv-banner__text" },
          h("div", { class: "banner__title" }, "Scheduled — not simulated yet"),
          h("div", { class: "lv-banner__sub" }, "The ballot is set, but the hidden result and the election-night timeline have not been generated. Preparing the night simulates them; every result stays hidden until the count runs."),
        ),
        h(
          "button",
          { class: "btn lv-banner__link", type: "button", disabled: busy, onclick: () => prepareNight(electionId) },
          busy ? [h("span", { class: "lc-spinner", "aria-hidden": "true" }), "Preparing…"] : [icon("play", { size: 13 }), "Prepare the night"],
        ),
      ),
    );
  }
  const unsub = onFinishEarlier(paint);
  paint();
  el.refresh = paint;
  el.destroy = unsub;
  return el;
}

/** Latest regular (non-local) election brief, preferring the demo election. */
export function latestRegular() {
  const { meta } = getState();
  const els = meta?.elections || [];
  const demo = els.find((e) => e.id === meta?.demo_election_id && !e.local);
  return demo || [...els].reverse().find((e) => !e.local) || null;
}
