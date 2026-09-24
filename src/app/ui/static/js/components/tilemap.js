/**
 * Twelve-province tile cartogram for the Electoral College.  Tiles show code + EV and encode the
 * race state: uncalled (grey), leading (hatched party colour), called/final (solid), too close
 * (amber border + alert icon), recount (dashed orange border + label), flip badge.  Status is
 * never colour alone: every non-trivial state carries an icon or a text label.
 *
 * `tileMap(provinces, onSelect, opts)` returns the grid element; `el.update(provinces)` updates
 * the existing tiles in place (keyed by province code) so live changes transition smoothly.
 */
import { h } from "../dom.js";
import { icon } from "./icons.js";
import { hideTip, showTip } from "./tooltip.js";

/** Approximate geographic arrangement on a 4 × 5 grid (row, col). */
export const TILE_POSITIONS = {
  FR: [0, 2], GR: [0, 3],
  NH: [1, 1], FL: [1, 2], DR: [1, 3],
  ZH: [2, 1], UT: [2, 2], OV: [2, 3],
  ZE: [3, 0], NB: [3, 1], GE: [3, 2],
  LI: [4, 2],
};

const STATE_WORD = { uncalled: "Uncalled", leading: "Leading", close: "Too close to call", called: "Called", final: "Final", recount: "Recount" };

function luminanceInk(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  const lin = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const L = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  return 1.05 / (L + 0.05) >= ((L + 0.05) / 0.0565) * 0.82 ? "#ffffff" : "#0b1220";
}

/**
 * @param {Array<{code, ev, state:'uncalled'|'leading'|'called'|'final'|'close'|'recount', color, flip?:boolean,
 *                 title?:string, name?:string, reporting?:number|null, sub?:string, highlight?:boolean}>} provinces
 * @param {(code:string)=>void} onSelect
 * @param {{tooltip?:(p)=>Node|string, showReporting?:boolean, compact?:boolean}} [opts]
 */
export function tileMap(provinces, onSelect, opts = {}) {
  const grid = h("div", { class: ["tilemap", opts.compact && "tilemap--compact"], role: "group", "aria-label": opts.label || "Electoral College tile map: 12 provinces" });
  const tiles = new Map();

  function tileFor(p) {
    let t = tiles.get(p.code);
    if (!t) {
      const [r, c] = TILE_POSITIONS[p.code] || [0, 0];
      const flags = h("span", { class: "tile__flags" });
      const code = h("span", { class: "tile__code" }, p.code);
      const ev = h("span", { class: "tile__ev" });
      const sub = h("span", { class: "tile__sub" });
      const el = h(
        "button",
        {
          type: "button",
          class: "tile",
          style: { gridRow: `${r + 1}`, gridColumn: `${c + 1}` },
          onclick: () => onSelect && onSelect(p.code),
        },
        flags,
        code,
        ev,
        sub,
      );
      t = { el, flags, ev, sub, p };
      el.addEventListener("mousemove", (e) => opts.tooltip && showTip(e, opts.tooltip(t.p)));
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("focus", () => {
        if (!opts.tooltip) return;
        const b = el.getBoundingClientRect();
        showTip({ clientX: b.right, clientY: b.top }, opts.tooltip(t.p));
      });
      el.addEventListener("blur", hideTip);
      tiles.set(p.code, t);
      grid.appendChild(el);
    }
    return t;
  }

  function paint(p) {
    const t = tileFor(p);
    t.p = p;
    const state = p.state || "uncalled";
    const called = state === "called" || state === "final";
    t.el.className = [
      "tile",
      state === "leading" && "tile--leading",
      called && "tile--called",
      state === "close" && "tile--close",
      state === "recount" && "tile--recount",
      p.highlight && "tile--highlight",
      p.dim && "tile--dim",
    ]
      .filter(Boolean)
      .join(" ");
    t.el.style.setProperty("--party", p.color || "var(--uncalled)");
    const ink = called ? luminanceInk(p.color) : null;
    if (ink) t.el.style.setProperty("--tile-ink", ink);
    else t.el.style.removeProperty("--tile-ink");
    t.ev.textContent = p.ev === null || p.ev === undefined ? "" : `${p.ev} EV`;
    const subText = p.sub !== undefined ? p.sub : opts.showReporting && p.reporting !== null && p.reporting !== undefined && !called ? `${Math.floor(p.reporting)}% in` : "";
    if (t.sub.textContent !== subText) t.sub.textContent = subText;
    // flags: FLIP badge, called check, close / recount markers (icon + label, never colour alone)
    const flagKey = `${state}|${p.flip ? 1 : 0}|${p.recounted ? 1 : 0}`;
    if (t.flags.dataset.key !== flagKey) {
      t.flags.dataset.key = flagKey;
      t.flags.replaceChildren(
        ...[
          p.flip ? h("span", { class: "tile__flip" }, "FLIP") : null,
          called ? h("span", { class: "tile__mark", title: STATE_WORD[state] }, icon(state === "final" ? "lock" : "check", { size: 11 })) : null,
          state === "close" ? h("span", { class: "tile__mark tile__mark--warn", title: "Too close to call" }, icon("alert", { size: 11 })) : null,
          state === "recount" ? h("span", { class: "tile__tag" }, "RECOUNT") : null,
          called && p.recounted ? h("span", { class: "tile__tag tile__tag--soft", title: "Decided after a recount" }, "R") : null,
        ].filter(Boolean),
      );
    }
    const label = p.title || `${p.name || p.code}: ${p.ev} electoral votes, ${STATE_WORD[state] || state}`;
    t.el.setAttribute("aria-label", label);
    if (!opts.tooltip) t.el.title = label;
  }

  function update(list) {
    const seen = new Set();
    for (const p of list || []) {
      paint(p);
      seen.add(p.code);
    }
    for (const [code, t] of tiles) if (!seen.has(code)) (t.el.remove(), tiles.delete(code));
    return grid;
  }

  update(provinces);
  grid.update = update;
  return grid;
}
