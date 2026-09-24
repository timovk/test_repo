/**
 * Electoral College analysis charts (display of API values only):
 *
 * * `marginChart` — province margins (winner − runner-up, pp) as sorted horizontal bars coloured
 *   by the province winner / leader; undecided provinces are drawn lighter with their status;
 *   tipping point / closest / largest victory are tagged with text labels (never colour alone).
 * * `pathStrip`   — the winner's path to 88: the API `path` (provinces ordered by the winner's
 *   margin, with API `cumulative_ev`) as one 0–174 strip with the 88 line; the tipping-point
 *   province is outlined and labelled.
 */
import { h, mount, svg } from "../dom.js";
import { fmtInt } from "../format.js";
import { hideTip, showTip } from "./tooltip.js";

const fmtPP = (v) => (v === null || v === undefined ? "–" : `${v.toFixed(2)} pp`);

/**
 * rows: [{code, name, ev, margin, color, party, who, decided:boolean, statusLabel, tags:[string]}]
 */
export function marginChart(rows, { width = 640, rowH = 26, label = "Province margins" } = {}) {
  const host = h("div", { class: "chart lv-margins" });
  if (!rows.length) {
    mount(host, h("div", { class: "state" }, "No margins yet — provinces appear as votes are counted."));
    return host;
  }
  const m = { l: 150, r: 150, t: 22, b: 8 };
  const iw = width - m.l - m.r;
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.margin || 0)));
  const step = max > 20 ? 5 : max > 8 ? 2 : 1;
  const ticks = [];
  for (let v = 0; v <= max + 1e-9; v += step) ticks.push(v);
  const X = (v) => m.l + (Math.abs(v) / (ticks[ticks.length - 1] || max)) * iw;
  const H = m.t + rows.length * rowH + m.b;
  const nodes = [
    ...ticks.map((v) => svg("line", { class: "gridline", x1: X(v), x2: X(v), y1: m.t - 4, y2: H - m.b })),
    svg("g", { class: "axis" }, ...ticks.map((v) => svg("text", { x: X(v), y: 12, "text-anchor": "middle" }, `${v}`))),
    svg("text", { x: X(ticks[ticks.length - 1]) + 6, y: 12, class: "lv-margins__unit" }, "pp"),
    svg("text", { x: 22, y: 12, "text-anchor": "end", class: "lv-margins__unit" }, "EV"),
  ];
  rows.forEach((r, i) => {
    const y = m.t + i * rowH;
    const bh = Math.min(14, rowH - 10);
    const by = y + (rowH - bh) / 2;
    const w = Math.max(2, X(r.margin || 0) - m.l);
    const tip = () =>
      h(
        "div",
        { class: "lv-tip" },
        h("div", { class: "lv-tip__head" }, h("strong", null, r.name), h("span", { class: "muted" }, ` · ${r.ev} EV`)),
        h("div", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": r.color } }), `${r.who || "–"}${r.party ? ` (${r.party})` : ""}`, h("span", { class: "muted" }, r.decided ? " · winner" : " · leads")),
        h("div", { class: "num" }, h("b", null, fmtPP(r.margin)), h("span", { class: "muted" }, " margin over the runner-up")),
        r.statusLabel ? h("div", { class: "muted" }, r.statusLabel) : null,
        (r.tags || []).length ? h("div", { class: "muted" }, r.tags.join(" · ")) : null,
      );
    nodes.push(
      svg(
        "g",
        {
          class: ["lv-margins__row", (r.tags || []).length && "is-tagged"].filter(Boolean).join(" "),
          tabindex: "0",
          role: "listitem",
          "aria-label": `${r.name}, ${r.ev} EV: ${r.who || "no leader"} ${r.decided ? "won" : "leads"} by ${fmtPP(r.margin)}${(r.tags || []).length ? `, ${r.tags.join(", ")}` : ""}`,
          onmousemove: (e) => showTip(e, tip()),
          onmouseleave: hideTip,
          onfocus: (e) => {
            const b = e.currentTarget.getBoundingClientRect();
            showTip({ clientX: b.left + 160, clientY: b.bottom }, tip());
          },
          onblur: hideTip,
          onclick: () => (location.hash = `#/provinces/${r.code}`),
        },
        svg("rect", { x: 0, y, width, height: rowH, fill: "transparent" }),
        svg("text", { x: 22, y: y + rowH / 2 + 4, "text-anchor": "end", class: "lv-margins__ev" }, `${r.ev}`),
        svg("text", { x: 32, y: y + rowH / 2 + 4, class: "lv-margins__label" }, r.name),
        svg("rect", { x: m.l, y: by, width: w, height: bh, rx: 3, fill: r.color || "var(--uncalled)", "fill-opacity": r.decided ? 1 : 0.4, class: "lv-margins__bar" }),
        (r.tags || []).includes("Tipping point") ? svg("rect", { x: m.l - 3, y: by - 3, width: w + 6, height: bh + 6, rx: 5, fill: "none", stroke: "var(--text-primary)", "stroke-width": 1.5 }) : null,
        svg("text", { x: m.l + w + 6, y: y + rowH / 2 + 4, class: "lv-margins__val" }, fmtPP(r.margin)),
        (r.tags || []).length ? svg("text", { x: m.l + w + 64, y: y + rowH / 2 + 4, class: "lv-margins__tag" }, r.tags.join(" · ").toUpperCase()) : null,
      ),
    );
  });
  mount(host, svg("svg", { viewBox: `0 0 ${width} ${H}`, role: "list", "aria-label": label }, ...nodes));
  return host;
}

/**
 * path: API `path` rows [{code, name, ev, winner_party, winner_color, margin_pp, cumulative_ev, is_tipping_point}]
 */
export function pathStrip(path, { total = 174, majority = 88, colorOf = (r) => r.winner_color, winnerParty } = {}) {
  const W = 900;
  const H = 86;
  const top = 26;
  const bh = 30;
  const X = (ev) => (ev / total) * W;
  const nodes = [];
  let start = 0;
  for (const r of path) {
    const x = X(start);
    const w = X(r.ev);
    const tip = () =>
      h(
        "div",
        { class: "lv-tip" },
        h("div", { class: "lv-tip__head" }, h("strong", null, r.name), h("span", { class: "muted" }, ` · ${r.ev} EV`)),
        h("div", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": colorOf(r) } }), `Won by ${r.winner_party}`, h("span", { class: "muted" }, ` by ${fmtPP(r.margin_pp)}`)),
        h("div", { class: "num" }, `Cumulative: `, h("b", null, `${fmtInt(r.cumulative_ev)} EV`)),
        r.is_tipping_point ? h("div", null, h("b", null, "Tipping point"), h("span", { class: "muted" }, ` — crosses ${majority}`)) : null,
      );
    const own = !winnerParty || r.winner_party === winnerParty;
    nodes.push(
      svg(
        "g",
        { class: "lv-path__seg", tabindex: "0", onmousemove: (e) => showTip(e, tip()), onmouseleave: hideTip, onclick: () => (location.hash = `#/provinces/${r.code}`), "aria-label": `${r.name} ${r.ev} EV, won by ${r.winner_party}, cumulative ${r.cumulative_ev}${r.is_tipping_point ? ", tipping point" : ""}` },
        svg("rect", { x: x + 1, y: top, width: Math.max(1, w - 2), height: bh, rx: 3, fill: colorOf(r), "fill-opacity": own ? 1 : 0.38 }),
        w > 26 ? svg("text", { x: x + w / 2, y: top + bh / 2 + 4, "text-anchor": "middle", class: "lv-path__code", fill: own ? "#fff" : "var(--text-primary)" }, r.code) : null,
        r.is_tipping_point ? svg("rect", { x: x - 1, y: top - 3, width: w + 2, height: bh + 6, rx: 4, fill: "none", stroke: "var(--text-primary)", "stroke-width": 2 }) : null,
        r.is_tipping_point ? svg("text", { x: x + w / 2, y: top + bh + 18, "text-anchor": "middle", class: "lv-path__tp" }, `TIPPING POINT · ${r.code}`) : null,
      ),
    );
    start += r.ev;
  }
  nodes.push(
    svg("line", { x1: X(majority), x2: X(majority), y1: top - 10, y2: top + bh + 4, stroke: "var(--text-primary)", "stroke-width": 2 }),
    svg("text", { x: X(majority), y: top - 14, "text-anchor": "middle", class: "lv-path__maj" }, `${majority} TO WIN`),
    svg("text", { x: 0, y: top - 14, class: "lv-path__axis" }, "0"),
    svg("text", { x: W, y: top - 14, "text-anchor": "end", class: "lv-path__axis" }, `${total}`),
  );
  return h("div", { class: "chart lv-path" }, svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Path to ${majority}: ${path.map((r) => `${r.code} ${r.ev} (cumulative ${r.cumulative_ev})`).join(", ")}` }, ...nodes));
}
