/**
 * Live call feed (ticker of race calls): time, race, winner, status, EV — newest first.  Rows are
 * the API call log (/api/elections/{id}/calls).  New items are prepended with a subtle entrance;
 * existing items are kept (keyed by call id) so the feed never flickers.
 */
import { h } from "../dom.js";
import { fmtPct } from "../format.js";
import { statusPill } from "./badges.js";
import { colorFor } from "./live-util.js";

const DECISIVE = new Set(["PROJECTED_WINNER", "CALLED", "RECOUNT"]);
const TYPES = [
  ["", "All races"],
  ["PRESIDENT_PROVINCE", "President"],
  ["SENATE", "Senate"],
  ["HOUSE", "House"],
  ["GOVERNOR", "Governor"],
  ["MAYOR", "Mayor"],
  ["PROVINCIAL_LEGISLATURE", "Prov. legislature"],
  ["COUNCIL", "Council"],
];

export function callFeed({ max = 60, types } = {}) {
  let mode = "calls";
  let type = "";
  let calls = [];
  let ctx = {};
  const items = new Map();
  const list = h("ol", { class: "feed lv-feed", "aria-live": "polite", "aria-relevant": "additions" });
  const empty = h("div", { class: "state lv-feed__empty" }, "No race calls yet.");
  const typeSel = h(
    "select",
    { class: "select lv-feed__type", "aria-label": "Race type", onchange: (e) => ((type = e.target.value), render(true)) },
    TYPES.filter(([k]) => !types || !k || types.includes(k)).map(([k, l]) => h("option", { value: k }, l)),
  );
  const bCalls = h("button", { type: "button", class: "is-active", "aria-pressed": "true", onclick: () => setMode("calls") }, "Calls");
  const bAll = h("button", { type: "button", "aria-pressed": "false", onclick: () => setMode("all") }, "All updates");
  const toolbar = h("div", { class: "lv-feed__tools" }, h("div", { class: "segmented", role: "group", "aria-label": "Feed content" }, bCalls, bAll), typeSel);
  const el = h("div", { class: "lv-feedwrap" }, list, empty);

  function setMode(m) {
    mode = m;
    bCalls.classList.toggle("is-active", m === "calls");
    bAll.classList.toggle("is-active", m === "all");
    bCalls.setAttribute("aria-pressed", String(m === "calls"));
    bAll.setAttribute("aria-pressed", String(m === "all"));
    render(true);
  }

  function visible() {
    const out = [];
    for (let i = calls.length - 1; i >= 0 && out.length < max; i--) {
      const c = calls[i];
      if (type && c.race_type !== type) continue;
      if (mode === "calls") {
        const s = c.status;
        const presFinal = s === "FINAL" && (c.race_type === "PRESIDENT" || c.race_type === "PRESIDENT_PROVINCE");
        if (!DECISIVE.has(s) && !presFinal && !(c.race_type === "PRESIDENT" && s !== "POLLS_CLOSED")) continue;
      }
      out.push(c);
    }
    return out;
  }

  function item(c) {
    const color = c.party ? colorFor(c.party, c.color) : null;
    const ev = ctx.evByRace?.[c.race_code];
    const pres = c.race_type === "PRESIDENT_PROVINCE" || c.race_type === "PRESIDENT";
    return h(
      "li",
      { class: ["feed__item", "lv-feed__item", pres && "lv-feed__item--pres", c.superseded && "is-superseded"], style: color ? { "--party": color } : undefined },
      h("span", { class: "feed__time" }, c.clock || ""),
      h(
        "a",
        { class: "lv-feed__body", href: `#/races/${c.race_code}` },
        h("span", { class: "lv-feed__race" }, c.race_name || c.race_code),
        h(
          "span",
          { class: "lv-feed__who" },
          c.candidate ? h("span", { class: "chip__swatch", style: { "--party": color || "var(--uncalled)" } }) : null,
          c.candidate ? `${c.candidate}${c.party ? ` (${c.party})` : ""}` : h("span", { class: "muted" }, "No projection"),
          h("span", { class: "muted num" }, ` · ${fmtPct(c.reporting_pct, 0)} in`),
          c.is_manual ? h("span", { class: "lv-inc", title: "Manual call by the producer" }, "MANUAL") : null,
        ),
      ),
      h("span", { class: "lv-feed__right" }, statusPill(c.status, { color }), ev ? h("span", { class: "lv-evchip num" }, `${ev} EV`) : null),
    );
  }

  function render(reset) {
    const vis = visible();
    if (reset) {
      items.clear();
      list.replaceChildren();
    }
    const wanted = new Set(vis.map((c) => c.id));
    for (const [id, node] of items) if (!wanted.has(id)) (node.remove(), items.delete(id));
    let prev = null;
    // vis is newest-first; insert missing nodes in order
    for (const c of vis) {
      let node = items.get(c.id);
      if (!node) {
        node = item(c);
        if (reset) node.style.animation = "none";
        items.set(c.id, node);
        if (prev) prev.after(node);
        else list.prepend(node);
      } else if (node.classList.contains("is-superseded") !== !!c.superseded) {
        node.classList.toggle("is-superseded", !!c.superseded);
      }
      prev = node;
    }
    empty.hidden = vis.length > 0;
    list.hidden = vis.length === 0;
  }

  function update(nextCalls, nextCtx = {}) {
    const first = calls.length === 0;
    calls = nextCalls || [];
    ctx = nextCtx;
    render(first);
  }

  function setEmptyText(t) {
    empty.textContent = t;
  }

  return { el, toolbar, update, setEmptyText };
}
