/**
 * Key races panel: the closest races that are still uncalled (live) or the closest races of the
 * night (final), from the night snapshot's compact race rows.  Only sorting / filtering of API
 * values (margin_pct, status) happens here.
 */
import { h, mount } from "../dom.js";
import { fmtPct, fmtProb } from "../format.js";
import { statusPill } from "./badges.js";
import { isDecided } from "./live-util.js";

const FILTERS = [
  ["", "All"],
  ["PRESIDENT_PROVINCE", "President"],
  ["SENATE", "Senate"],
  ["GOVERNOR", "Governor"],
  ["HOUSE", "House"],
];

const SHORT_STATUS = { TOO_CLOSE_TO_CALL: "Too close", TOO_EARLY_TO_CALL: "Too early", PROJECTED_WINNER: "Projected" };

function shortName(r) {
  const n = String(r.name || r.key);
  return n.replace(/^President: /, "President · ").replace(/ \(\d+ EV\)$/, "");
}

export function keyRaces({ limit = 8 } = {}) {
  let type = "";
  let races = [];
  let opts = {};
  const buttons = FILTERS.map(([k, l]) => {
    const b = h("button", { type: "button", "aria-pressed": String(k === type), class: k === type ? "is-active" : "", onclick: () => setType(k) }, l);
    b.dataset.type = k;
    return b;
  });
  const toolbar = h("div", { class: "segmented lv-kr__filter", role: "group", "aria-label": "Race type" }, buttons);
  const list = h("ol", { class: "lv-kr" });
  const note = h("p", { class: "lv-kr__note muted" });
  const el = h("div", { class: "lv-krwrap" }, list, note);

  function setType(k) {
    type = k;
    for (const b of buttons) {
      b.classList.toggle("is-active", b.dataset.type === k);
      b.setAttribute("aria-pressed", String(b.dataset.type === k));
    }
    lastKey = "";
    render();
  }

  function pick() {
    const final = opts.final;
    let pool = races.filter((r) => (!type || r.type === type) && r.type !== "PRESIDENT");
    if (final) pool = pool.filter((r) => r.margin_pct !== null && r.margin_pct !== undefined && (r.reporting_pct ?? 0) > 0);
    else pool = pool.filter((r) => !isDecided(r.status) && r.leader && (r.reporting_pct ?? 0) > 0);
    return pool.sort((a, b) => (a.margin_pct ?? 1e9) - (b.margin_pct ?? 1e9)).slice(0, limit);
  }

  let lastKey = "";
  function render() {
    const rows = pick();
    const key = JSON.stringify([type, opts.final, opts.index?.size?.() || 0, rows.map((r) => [r.key, r.status, r.leader, r.called_key, Math.round((r.margin_pct ?? 0) * 100), Math.floor(r.reporting_pct ?? 0), r.win_probability])]);
    if (key === lastKey) return;
    lastKey = key;
    const idx = opts.index;
    mount(
      list,
      rows.map((r) => {
        const who = r.called_key || r.leader;
        const info = idx?.get(who);
        const color = idx ? idx.color(who) : "var(--uncalled)";
        const lean = r.lean_key && r.lean_key !== r.leader ? idx?.get(r.lean_key) : null;
        return h(
          "li",
          null,
          h(
            "a",
            { class: "lv-kr__row", href: `#/races/${r.key}`, style: { "--party": color } },
            h(
              "span",
              { class: "lv-kr__main" },
              h("span", { class: "lv-kr__name" }, shortName(r)),
              h(
                "span",
                { class: "lv-kr__who" },
                h("span", { class: "chip__swatch" }),
                info ? `${info.label}${info.party ? ` (${info.party})` : ""}` : who || "–",
                h("span", { class: "muted" }, r.called_key ? (opts.final ? " won" : " projected") : " leads"),
                lean ? h("span", { class: "muted" }, ` · model lean ${lean.party || lean.label}`) : null,
              ),
            ),
            h(
              "span",
              { class: "lv-kr__nums num" },
              h("b", null, `${fmtPct(r.margin_pct, 2).replace("%", "")} pp`),
              h("span", { class: "muted" }, opts.final ? "margin" : `${fmtPct(r.reporting_pct, 0)} in${r.win_probability !== null && r.win_probability !== undefined ? ` · ${fmtProb(r.win_probability)}` : ""}`),
            ),
            statusPill(r.status, { color, label: SHORT_STATUS[r.status] }),
            opts.final ? null : h("span", { class: "lv-kr__progress", "aria-hidden": "true" }, h("span", { style: { width: `${Math.min(100, r.reporting_pct || 0)}%` } })),
          ),
        );
      }),
    );
    note.textContent = rows.length
      ? opts.final
        ? "Smallest counted margins of the night (winner vs runner-up)."
        : "Closest uncalled races by counted margin. Win probability = calling model (SIMULATED)."
      : opts.emptyText || (opts.final ? "No races." : "No uncalled race with votes counted.");
  }

  function update(nextRaces, nextOpts = {}) {
    races = nextRaces || [];
    opts = nextOpts;
    render();
  }

  return { el, toolbar, update };
}
