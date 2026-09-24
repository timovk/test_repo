/**
 * Results & geography kit (prefix `res-`): helpers shared by the provinces, municipalities,
 * House, Senate and governor pages.
 *
 * Everything here *displays* what the API returns — no winners, calls, seat counts, margins or
 * swings are computed.  Colour helpers only mix a party colour toward the surface for shading.
 */
import { api } from "../api.js";
import { h, keyed, mount, svg } from "../dom.js";
import { fmtInt, fmtPct, fmtPP, STATUS_LABELS } from "../format.js";
import { getState, partyColor, subscribe } from "../store.js";
import { resAvatar as avatar } from "./res-avatar.js";
import { partyChip, provBadge, statusPill } from "./badges.js";
import { icon } from "./icons.js";

/* ------------------------------------------------------------------ status helpers */
export const DECIDED = new Set(["CALLED", "FINAL", "PROJECTED_WINNER"]);
export const isDecided = (s) => DECIDED.has(String(s || "").toUpperCase());

/** Party colour honouring user overrides (Settings); falls back to the payload colour, then meta. */
export const pc = (party, color) => {
  if (!party) return color || "var(--uncalled)";
  return partyColor(party, color || (getState().meta?.parties || []).find((p) => p.code === party)?.color);
};

/** The colour a race is shown in: its winner when decided, else its counted leader. */
export function raceColor(r) {
  if (!r) return null;
  if (r.winner_party || r.winner_color) return pc(r.winner_party, r.winner_color);
  if (r.leader_party || r.leader_color) return pc(r.leader_party, r.leader_color);
  return null;
}

export function racePill(r, opts = {}) {
  return statusPill(r?.status || "SCHEDULED", { color: raceColor(r) || undefined, ...opts });
}

/** Map/tile visual state of a race from its API status. */
export function raceState(r) {
  const s = String(r?.status || "SCHEDULED").toUpperCase();
  if (s === "RECOUNT") return "recount";
  if (s === "CALLED" || s === "FINAL") return "called";
  if (s === "PROJECTED_WINNER") return "projected";
  if (s === "TOO_CLOSE_TO_CALL") return "close";
  if (s === "LEAN") return "lean";
  if ((r?.reporting_pct ?? 0) > 0 && (r?.leader || r?.leader_party)) return "leading";
  return "uncalled";
}

/* ------------------------------------------------------------------ results source */
const SOURCE = {
  hidden: { label: "Pre-election · results hidden", ico: "lock" },
  live: { label: "Live count", ico: null },
  final: { label: "Final result", ico: "check" },
};

export function sourceBadge(source) {
  const s = SOURCE[source] ? source : "hidden";
  return h(
    "span",
    { class: `res-src res-src--${s}`, title: s === "hidden" ? "Results are revealed on election night or once the election is final" : SOURCE[s].label },
    s === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : icon(SOURCE[s].ico, { size: 12 }),
    SOURCE[s].label,
  );
}

/** Notice for hidden elections (the API's own wording) — shows what *is* available. */
export function hiddenNotice(payload, what = "Candidates, ballots, incumbents and geography are shown.") {
  if (payload?.results_source !== "hidden") return null;
  return h(
    "div",
    { class: "notice res-notice", role: "note" },
    icon("lock", { size: 16 }),
    h("div", null, h("strong", null, "Results not revealed yet. "), payload.notice || "Results appear live during the election night or once the election is final.", " ", h("span", { class: "muted" }, what)),
  );
}

/**
 * Live strip bound to store.night: "LIVE · 23:56 · 50.6% of expected vote counted".
 * Returns an element that updates itself; call `.stop()` on cleanup.
 */
export function nightTicker(electionId) {
  const el = h("div", { class: "res-ticker", "aria-live": "polite" });
  const paint = (night) => {
    if (!night || night.election_id !== electionId || night.election_status !== "live") {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    const rep = night.snapshot?.reporting || {};
    const clk = night.clock || {};
    const key = `${clk.seq}|${clk.status}`;
    keyed(el, key, () => [
      h("span", { class: "res-ticker__live" }, clk.status === "running" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : icon("pause", { size: 12 }), clk.status === "running" ? "LIVE" : String(clk.status || "").toUpperCase()),
      h("span", { class: "res-ticker__clock mono" }, `${clk.clock || "–"} CET`),
      h("span", null, h("b", null, fmtPct(rep.pct_expected_ballots)), " of expected vote counted"),
      h("span", null, h("b", null, `${fmtInt(rep.municipalities_reporting)}/${fmtInt(rep.municipalities_total)}`), " municipalities reporting"),
      h("span", { class: "muted" }, `event ${fmtInt(clk.seq)} of ${fmtInt(clk.total_events)}`),
    ]);
  };
  const unsub = subscribe("night", paint);
  paint(getState().night);
  el.stop = unsub;
  return el;
}

/**
 * Refresh a live page when the night advances (new seq) or the election changes state
 * (scheduled → live → final).  Throttled to `interval` ms; one request in flight at a time.
 */
export function liveRefresh(electionId, refresh, { interval = 4000 } = {}) {
  let lastSeq;
  let lastStatus;
  let timer = null;
  let inflight = false;
  let pending = false;
  let lastRun = Date.now();
  let dead = false;
  const run = async () => {
    timer = null;
    if (dead) return;
    if (inflight) {
      pending = true;
      return;
    }
    inflight = true;
    lastRun = Date.now();
    try {
      await refresh();
    } catch (err) {
      if (err?.name !== "AbortError" && !dead) console.warn("live refresh failed", err);
    }
    inflight = false;
    if (pending && !dead) {
      pending = false;
      schedule(false);
    }
  };
  const schedule = (now) => {
    if (timer || dead) return;
    const wait = now ? 150 : Math.max(0, lastRun + interval - Date.now());
    timer = setTimeout(run, wait);
  };
  const onNight = (night) => {
    if (!night || night.election_id !== electionId) return;
    const seq = night.clock?.seq ?? night.snapshot?.seq;
    const st = night.election_status;
    if (lastSeq === undefined) {
      lastSeq = seq;
      lastStatus = st;
      return;
    }
    if (seq === lastSeq && st === lastStatus) return;
    const statusChanged = st !== lastStatus;
    lastSeq = seq;
    lastStatus = st;
    if (statusChanged && timer) {
      clearTimeout(timer);
      timer = null;
    }
    schedule(statusChanged);
  };
  const unsub = subscribe("night", onNight);
  onNight(getState().night);
  return () => {
    dead = true;
    clearTimeout(timer);
    unsub();
  };
}

/* ------------------------------------------------------------------ small display atoms */
export function flipTag(flip, { prev } = {}) {
  if (!flip) return h("span", { class: "muted" }, "–");
  const f = String(flip).toLowerCase();
  const label = f === "flip" ? "Flip" : f === "hold" ? "Hold" : f === "new" ? "New" : f;
  return h(
    "span",
    { class: `res-flip res-flip--${f}`, title: f === "flip" ? `Flipped${prev ? ` from ${prev}` : ""}` : f === "hold" ? `Held${prev ? ` by ${prev}` : ""}` : "No previous holder" },
    f === "flip" ? icon("flip", { size: 11 }) : null,
    label,
  );
}

export function partyTag(party, color, name) {
  if (!party && !name) return h("span", { class: "muted" }, "–");
  return h("span", { class: "res-who" }, partyChip(party || "IND", { color: pc(party, color) }), name ? h("span", { class: "res-who__name" }, name) : null);
}

export function fmtMargin(pp, digits = 1) {
  if (pp === null || pp === undefined || Number.isNaN(pp)) return "–";
  return `+${fmtPP(pp, digits).replace(/^[+±−]/, "")}`;
}

/** Margin with the leading party code: "PA +4.6". */
export function marginLabel(party, pp) {
  if (pp === null || pp === undefined) return h("span", { class: "muted" }, "–");
  return h("span", { class: "res-margin" }, party ? h("span", { class: "res-margin__party" }, party) : null, h("span", { class: "num" }, fmtMargin(pp)));
}

export function reportingMeter(pct, { width = 64 } = {}) {
  if (pct === null || pct === undefined) return h("span", { class: "muted" }, "–");
  const v = Math.max(0, Math.min(100, pct));
  return h(
    "span",
    { class: "res-meter", title: `${fmtPct(pct)} reporting` },
    h("span", { class: "res-meter__track", style: { width: `${width}px` } }, h("span", { class: "res-meter__fill", style: { width: `${v}%` } })),
    h("span", { class: "num" }, fmtPct(pct, 1)),
  );
}

export function signed(pp, digits = 1) {
  if (pp === null || pp === undefined) return h("span", { class: "muted" }, "–");
  return h("span", { class: "num" }, fmtPP(pp, digits));
}

export const statusLabel = (s) => STATUS_LABELS[String(s || "").toUpperCase()] || s || "–";

/* ------------------------------------------------------------------ candidate result rows */
function lineInfo(l) {
  const person = l.candidate || l.president || null;
  return {
    key: l.key,
    name: person?.name || l.name,
    fullName: l.name,
    mate: l.running_mate?.name || l.running_mate_name || null,
    portrait: person?.portrait_key || l.portrait_key || null,
    candidateId: person?.id || l.candidate_id || null,
    party: l.party,
    color: pc(l.party, l.color),
    incumbent: !!l.incumbent,
    votes: l.votes,
    pct: l.pct,
    winner: !!l.winner,
    isList: !person && !l.candidate_id && l.key === l.party,
  };
}

/**
 * Candidate rows: avatar, name (+ running mate), party, share bar, % and votes.
 * `leaderKey` highlights the counted leader when no winner is declared yet.
 * The returned list has `.update(lines, opts)`: when the order of lines is unchanged the bars,
 * percentages and votes update in place (bars animate), otherwise the list is rebuilt.
 */
export function resultRows(lines = [], opts = {}) {
  const list = h("ol", { class: ["res-lines", opts.compact && "res-lines--compact"] });
  let sig = null;
  const build = (ls, o) => {
    const { leaderKey, max = 12, avatars = true, mateLabel = "with", compact = false, seats } = o;
    const items = ls.map(lineInfo);
    const hasVotes = items.some((i) => i.votes !== null && i.votes !== undefined);
    if (hasVotes) items.sort((a, b) => (b.votes ?? -1) - (a.votes ?? -1));
    const top = items.slice(0, max);
    const maxPct = Math.max(1, ...top.map((i) => i.pct || 0));
    const anyWinner = items.some((x) => x.winner);
    const nextSig = `${top.map((i) => i.key).join("|")}#${hasVotes}#${anyWinner}#${leaderKey}#${JSON.stringify(seats || {})}`;
    if (sig === nextSig) {
      // Same order and state: update numbers in place so the bars animate.
      top.forEach((i, idx) => {
        const row = list.children[idx];
        if (!row) return;
        row.querySelector(".res-lines__fill").style.width = hasVotes ? `${((i.pct || 0) / maxPct) * 100}%` : "0%";
        row.querySelector(".res-lines__pct").textContent = hasVotes ? fmtPct(i.pct) : "–";
        row.querySelector(".res-lines__votes").textContent = hasVotes ? fmtInt(i.votes) : "";
      });
      return;
    }
    sig = nextSig;
    mount(
      list,
      top.map((i) => {
        const lead = i.winner || (!anyWinner && leaderKey && i.key === leaderKey);
        const nameNode = i.candidateId && !i.isList ? h("a", { href: `#/candidates/${i.candidateId}`, class: "res-lines__name" }, i.name) : h("span", { class: "res-lines__name" }, i.name);
        return h(
          "li",
          { class: ["res-lines__row", i.winner && "is-winner", lead && !i.winner && "is-leader"], style: { "--party": i.color } },
          avatars && !compact
            ? h("span", { class: "res-lines__avatar" }, i.isList ? h("span", { class: "res-lines__list" }, i.party) : avatar(i.name, { color: i.color, key: i.portrait || i.name, size: 36 }))
            : null,
          h(
            "span",
            { class: "res-lines__who" },
            h("span", { class: "res-lines__top" }, nameNode, i.winner ? h("span", { class: "res-lines__check", title: "Winner" }, icon("check", { size: 12 })) : null, i.incumbent ? h("span", { class: "res-tag", title: "Incumbent" }, "INC") : null),
            h("span", { class: "res-lines__sub" }, partyChip(i.party || "IND", { color: i.color }), i.mate ? h("span", { class: "muted" }, `${mateLabel} ${i.mate}`) : null, seats && seats[i.party] !== undefined ? h("span", { class: "res-tag res-tag--seats" }, `${seats[i.party]} seats`) : null),
          ),
          h("span", { class: "res-lines__bar", "aria-hidden": "true" }, h("span", { class: "res-lines__fill", style: { width: hasVotes ? `${((i.pct || 0) / maxPct) * 100}%` : "0%" } })),
          h("span", { class: "res-lines__pct num" }, hasVotes ? fmtPct(i.pct) : "–"),
          h("span", { class: "res-lines__votes num" }, hasVotes ? fmtInt(i.votes) : ""),
        );
      }),
      items.length > max ? h("li", { class: "res-lines__more muted" }, `+ ${items.length - max} more on the ballot`) : null,
    );
  };
  build(lines, opts);
  list.update = (ls, o = opts) => build(ls || [], o);
  return list;
}

/* ------------------------------------------------------------------ facts */
/** Definition grid of facts; each item {label, value, cat?, note?, flag?}. */
export function facts(items, { cols = 2 } = {}) {
  return h(
    "dl",
    { class: "res-facts", style: { "--cols": cols } },
    items
      .filter(Boolean)
      .map((it) =>
        h(
          "div",
          { class: "res-facts__item" },
          h("dt", null, it.label, it.cat ? provBadge(it.cat) : null, it.flag ? h("span", { class: "res-tag res-tag--imputed", title: it.flagTitle || "Imputed value (DERIVED)" }, it.flag) : null),
          h("dd", null, it.value ?? "–", it.note ? h("span", { class: "res-facts__note" }, it.note) : null),
        ),
      ),
  );
}

/* ------------------------------------------------------------------ controls */
export function segmented(options, value, onChange, { label = "Options", size } = {}) {
  const wrap = h("div", { class: ["segmented", "res-seg", size && `res-seg--${size}`], role: "group", "aria-label": label });
  const paint = (v) => {
    value = v;
    mount(
      wrap,
      options.map((o) =>
        h(
          "button",
          {
            type: "button",
            class: o.value === value ? "is-active" : "",
            "aria-pressed": o.value === value ? "true" : "false",
            disabled: o.disabled,
            title: o.title || o.label,
            onclick: () => {
              if (o.value === value) return;
              paint(o.value);
              onChange(o.value);
            },
          },
          o.label,
        ),
      ),
    );
  };
  paint(value);
  wrap.set = paint;
  return wrap;
}

export function selectControl(options, value, onChange, { label = "Select", className } = {}) {
  return h(
    "select",
    { class: ["select", className], "aria-label": label, onchange: (e) => onChange(e.target.value) },
    options.map((o) => h("option", { value: o.value, selected: String(o.value) === String(value) }, o.label)),
  );
}

export function searchInput(placeholder, onInput, { label = "Search", value = "" } = {}) {
  let t = null;
  return h("input", {
    class: "input res-search",
    type: "search",
    placeholder,
    value,
    "aria-label": label,
    oninput: (e) => {
      clearTimeout(t);
      const v = e.target.value;
      t = setTimeout(() => onInput(v.trim().toLowerCase()), 120);
    },
  });
}

/** Label/value control group used in toolbars. */
export function field(label, control) {
  return h("label", { class: "res-field" }, h("span", { class: "res-field__label" }, label), control);
}

/* ------------------------------------------------------------------ colour (map shading) */
const varCache = new Map();
let varTheme = null;
export function cssVar(name) {
  const theme = document.documentElement.dataset.theme;
  if (theme !== varTheme) {
    varCache.clear();
    varTheme = theme;
  }
  if (!varCache.has(name)) varCache.set(name, getComputedStyle(document.documentElement).getPropertyValue(name).trim());
  return varCache.get(name);
}

function toRgb(c) {
  if (!c) return null;
  let s = String(c).trim();
  if (s.startsWith("var(")) s = cssVar(s.slice(4, -1).trim());
  if (s.startsWith("#")) {
    let x = s.slice(1);
    if (x.length === 3) x = x.split("").map((ch) => ch + ch).join("");
    const n = parseInt(x.slice(0, 6), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const m = s.match(/rgba?\(([^)]+)\)/);
  if (m) return m[1].split(",").slice(0, 3).map((v) => parseFloat(v));
  return null;
}

/** sRGB mix of `a` into `b` (t = weight of a, 0–1). Canvas-safe (no color-mix()). */
export function mix(a, b, t) {
  const A = toRgb(a);
  const B = toRgb(b);
  if (!A) return b;
  if (!B) return a;
  const k = Math.max(0, Math.min(1, t));
  const c = A.map((v, i) => Math.round(v * k + B[i] * (1 - k)));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** Relative luminance → readable ink on a fill. */
export function inkOn(color) {
  const c = toRgb(color);
  if (!c) return "#fff";
  const [r, g, b] = c.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "#0a0f1c" : "#ffffff";
}

export const seqRamp = () => [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--seq-${i}`));
export const divRamp = () => ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"].map(cssVar);

/** Binned sequential colour (one hue) for value in [lo, hi]. */
export function seqColor(value, lo, hi) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  const ramp = seqRamp();
  const t = Math.max(0, Math.min(0.9999, (value - lo) / Math.max(1e-9, hi - lo)));
  return ramp[Math.floor(t * ramp.length)];
}

/** Binned diverging colour (two hues + neutral midpoint), symmetric around 0. */
export function divColor(value, maxAbs) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  const ramp = divRamp();
  const t = Math.max(-1, Math.min(1, value / Math.max(1e-9, maxAbs)));
  return ramp[Math.round((t + 1) * 3)];
}

/** Party hue from faint to full over 5 bins (a single-hue sequential ramp per party). */
export function partyShade(color, t) {
  const steps = [0.22, 0.4, 0.58, 0.78, 1];
  const k = steps[Math.max(0, Math.min(4, Math.floor(Math.max(0, Math.min(0.9999, t)) * 5)))];
  return mix(color, cssVar("--surface-2"), k);
}

/* ------------------------------------------------------------------ legends */
export function swatchLegend(items, { title } = {}) {
  return h(
    "div",
    { class: "legend res-legend" },
    title ? h("span", { class: "res-legend__title" }, title) : null,
    items.map((it) =>
      h(
        "span",
        { class: "legend__item" },
        h("span", { class: ["res-swatch", it.kind && `res-swatch--${it.kind}`], style: { "--sw": it.color || "var(--uncalled)", "--sw-edge": it.edge || "transparent" } }),
        it.label,
        it.count !== undefined ? h("span", { class: "res-legend__count num" }, String(it.count)) : null,
      ),
    ),
  );
}

/** Stepped ramp legend: colours + edge labels. */
export function rampLegend(colors, labels, { title } = {}) {
  return h(
    "div",
    { class: "legend res-legend" },
    title ? h("span", { class: "res-legend__title" }, title) : null,
    h(
      "span",
      { class: "res-ramp" },
      h("span", { class: "res-ramp__bar" }, colors.map((c) => h("span", { style: { background: c } }))),
      h("span", { class: "res-ramp__labels" }, labels.map((l) => h("span", null, l))),
    ),
  );
}

/* ------------------------------------------------------------------ data helpers */
let partiesPromise = null;
/** Party identities (FICTIONAL) with a left→right display order for hemicycles. */
export function parties() {
  if (!partiesPromise)
    partiesPromise = api
      .get("/api/parties", { cache: true })
      .then((d) => {
        const list = [...(d.parties || [])];
        list.sort((a, b) => (a.ideology?.economic ?? 0) + 0.35 * (a.ideology?.social ?? 0) - ((b.ideology?.economic ?? 0) + 0.35 * (b.ideology?.social ?? 0)));
        return { list, byCode: Object.fromEntries(list.map((p) => [p.code, p])), order: list.map((p) => p.code) };
      })
      .catch(() => {
        partiesPromise = null;
        const meta = getState().meta;
        const list = meta?.parties || [];
        return { list, byCode: Object.fromEntries(list.map((p) => [p.code, p])), order: list.map((p) => p.code) };
      });
  return partiesPromise;
}

let electionsPromise = null;
/** Election list with `contents` (which race families an election holds). */
export function electionList() {
  if (!electionsPromise) electionsPromise = api.get("/api/elections").catch((e) => {
    electionsPromise = null;
    throw e;
  });
  return electionsPromise;
}

export async function electionContents(id) {
  try {
    const d = await electionList();
    return (d.elections || []).find((e) => e.id === id)?.contents || null;
  } catch {
    return null;
  }
}

/** Race families present in an election, in display order. */
export function familiesOf(contents) {
  if (!contents) return ["PRES"];
  const rc = contents.race_counts || {};
  const out = [];
  if (contents.president || rc.PRESIDENT_PROVINCE) out.push("PRES");
  if (contents.governors || rc.GOVERNOR) out.push("GOV");
  if ((contents.senate_classes || []).length || rc.SENATE) out.push("SEN");
  if (contents.house || rc.HOUSE) out.push("HOUSE");
  if (contents.provincial_legislatures || rc.PROVINCIAL_LEGISLATURE) out.push("PROVLEG");
  if (rc.MAYOR) out.push("MAYOR");
  if (rc.MUNICIPAL_COUNCIL) out.push("COUNCIL");
  return out;
}

export const FAMILY_LABEL = {
  PRES: "President",
  GOV: "Governor",
  SEN: "Senate",
  HOUSE: "House",
  PROVLEG: "Legislature",
  MAYOR: "Mayor",
  COUNCIL: "Council",
};

/** Tiny inline SVG ring for a single percentage (used in stat tiles). */
export function ring(pct, { size = 40, color = "var(--seq-5)" } = {}) {
  const r = 16;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(100, pct || 0));
  return svg(
    "svg",
    { viewBox: "0 0 40 40", width: size, height: size, "aria-hidden": "true", class: "res-ring" },
    svg("circle", { cx: 20, cy: 20, r, fill: "none", stroke: "var(--uncalled-soft)", "stroke-width": 5 }),
    svg("circle", { cx: 20, cy: 20, r, fill: "none", stroke: color, "stroke-width": 5, "stroke-dasharray": `${(v / 100) * c} ${c}`, transform: "rotate(-90 20 20)", "stroke-linecap": "round" }),
  );
}

/** Stat tile (label, value, optional sub) — proportional figures for the value. */
export function tile(label, value, sub, { accent } = {}) {
  return h(
    "div",
    { class: "res-tile", style: accent ? { "--party": accent } : undefined },
    h("span", { class: "res-tile__label" }, label),
    h("span", { class: "res-tile__value" }, value ?? "–"),
    sub ? h("span", { class: "res-tile__sub" }, sub) : null,
  );
}

/** Render `fn()` into `el` only when `key` changes (thin wrapper so views read clearly). */
export function slot(el, key, fn) {
  return keyed(el, String(key), fn);
}
