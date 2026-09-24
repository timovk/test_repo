/**
 * Twelve-province tile cartogram for the Electoral College.  Tiles show code + EV and encode the
 * race state: uncalled (grey), leading (hatched party colour), called/final (solid), too close
 * (amber border), recount (dashed), flip badge.
 */
import { h } from "../dom.js";

/** Approximate geographic arrangement on a 4 × 5 grid (row, col). */
export const TILE_POSITIONS = {
  FR: [0, 2], GR: [0, 3],
  NH: [1, 1], FL: [1, 2], DR: [1, 3],
  ZH: [2, 1], UT: [2, 2], OV: [2, 3],
  ZE: [3, 0], NB: [3, 1], GE: [3, 2],
  LI: [4, 2],
};

/**
 * @param {Array<{code, ev, state:'uncalled'|'leading'|'called'|'close'|'recount', color, flip?:boolean, title?:string}>} provinces
 * @param {(code:string)=>void} onSelect
 */
export function tileMap(provinces, onSelect) {
  const tiles = provinces.map((p) => {
    const [r, c] = TILE_POSITIONS[p.code] || [0, 0];
    const cls = ["tile", p.state === "leading" && "tile--leading", (p.state === "called" || p.state === "final") && "tile--called", p.state === "close" && "tile--close", p.state === "recount" && "tile--recount"];
    return h(
      "button",
      {
        class: cls,
        style: { gridRow: `${r + 1}`, gridColumn: `${c + 1}`, "--party": p.color || "var(--uncalled)" },
        title: p.title || `${p.code}: ${p.ev} EV`,
        onclick: () => onSelect && onSelect(p.code),
        "aria-label": p.title || `${p.code} ${p.ev} electoral votes`,
      },
      p.flip ? h("span", { class: "tile__flip" }, "FLIP") : null,
      h("span", { class: "tile__code" }, p.code),
      h("span", { class: "tile__ev" }, `${p.ev} EV`),
    );
  });
  return h("div", { class: "tilemap" }, tiles);
}
