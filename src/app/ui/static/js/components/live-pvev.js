/**
 * Popular vote vs Electoral College comparison as small multiples (one scale per panel, never a
 * dual axis): per ticket, electoral votes on the 0–174 scale (88 marker) next to the popular-vote
 * share on a 0–100 % scale, plus the API's EC efficiency (EV share − PV share) when reported.
 * Bars are thin (≤ 14 px) with value labels at the tip; each row has a hover tooltip; a table
 * view is the accessible alternative.  `update(rows)` re-renders in place.
 *
 * rows: [{key, name, party, color, ev, evLeading?, evShare?, pct, votes, efficiency?}]
 */
import { h, mount, svg } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { hideTip, showTip } from "./tooltip.js";

export function pvEvChart({ total = 174, majority = 88 } = {}) {
  const host = h("div", { class: "lv-pvev chart" });
  const tableHost = h("div");
  const el = h("div", { class: "lv-pvev-wrap" }, host, h("details", { class: "lv-tableview" }, h("summary", null, "Table view"), tableHost));

  function update(rows) {
    rows = rows || [];
    const W = 760;
    const rowH = 34;
    const top = 46;
    const nameW = 170;
    const gap = 26;
    const panelW = (W - nameW - gap * 2 - 70) / 2;
    const x1 = nameW + gap;
    const x2 = x1 + panelW + gap + 40;
    const H = top + rows.length * rowH + 6;
    const hasEff = rows.some((r) => r.efficiency !== null && r.efficiency !== undefined);
    const evX = (v) => x1 + (Math.max(0, v) / total) * panelW;
    const pvX = (v) => x2 + (Math.max(0, v) / 100) * panelW;
    const nodes = [
      svg("text", { x: x1, y: 11, class: "lv-pvev__head" }, `Electoral votes (of ${total})`),
      svg("text", { x: x2, y: 11, class: "lv-pvev__head" }, "Popular vote (% of valid votes)"),
      // baselines + majority marker + 50 % gridline
      svg("line", { x1: x1, x2: x1, y1: top - 6, y2: H - 4, class: "lv-pvev__base" }),
      svg("line", { x1: x2, x2: x2, y1: top - 6, y2: H - 4, class: "lv-pvev__base" }),
      svg("line", { x1: evX(majority), x2: evX(majority), y1: top - 8, y2: H - 2, class: "lv-pvev__marker" }),
      svg("text", { x: evX(majority), y: top - 13, "text-anchor": "middle", class: "lv-pvev__mlabel" }, `${majority} TO WIN`),
      svg("line", { x1: pvX(50), x2: pvX(50), y1: top - 6, y2: H - 4, class: "gridline" }),
      svg("text", { x: pvX(50), y: top - 13, "text-anchor": "middle", class: "lv-pvev__mlabel lv-pvev__mlabel--soft" }, "50%"),
    ];
    rows.forEach((r, i) => {
      const y = top + i * rowH;
      const bh = 12;
      const by = y + (rowH - bh) / 2 - 2;
      const evW = Math.max(0, evX(r.ev || 0) - x1);
      const leadW = Math.max(0, evX((r.ev || 0) + (r.evLeading || 0)) - evX(r.ev || 0));
      const pvW = Math.max(0, pvX(r.pct || 0) - x2);
      const tip = () =>
        h(
          "div",
          null,
          h("div", { class: "legend__item", style: { marginBottom: "4px" } }, h("span", { class: "chip__swatch", style: { "--party": r.color } }), h("b", null, r.name), h("span", { class: "muted" }, ` (${r.party || "IND"})`)),
          h("div", { class: "num" }, h("b", null, `${fmtInt(r.ev)} EV`), r.evShare !== null && r.evShare !== undefined ? h("span", { class: "muted" }, ` · ${fmtPct(r.evShare * 100)} of EV`) : null, r.evLeading ? h("span", { class: "muted" }, ` · +${fmtInt(r.evLeading)} leading`) : null),
          h("div", { class: "num" }, h("b", null, fmtPct(r.pct)), h("span", { class: "muted" }, ` popular vote · ${fmtInt(r.votes)} votes`)),
          r.efficiency !== null && r.efficiency !== undefined ? h("div", { class: "num muted" }, `EC efficiency ${r.efficiency > 0 ? "+" : ""}${r.efficiency.toFixed(1)} pp (EV share − PV share)`) : null,
        );
      nodes.push(
        svg(
          "g",
          { class: "lv-pvev__row", tabindex: "0", onmousemove: (e) => showTip(e, tip()), onmouseleave: hideTip, onfocus: (e) => {
            const b = e.currentTarget.getBoundingClientRect();
            showTip({ clientX: b.left + 200, clientY: b.bottom }, tip());
          }, onblur: hideTip },
          svg("rect", { x: 0, y, width: W, height: rowH, fill: "transparent" }),
          svg("rect", { x: 0, y: y + 9, width: 4, height: 12, rx: 1, fill: r.color }),
          svg("text", { x: 12, y: y + 19, class: "lv-pvev__name" }, r.name.length > 22 ? `${r.name.slice(0, 21)}…` : r.name),
          evW > 0 ? svg("rect", { x: x1, y: by, width: evW, height: bh, rx: 2, fill: r.color, class: "lv-pvev__bar" }) : null,
          leadW > 0 ? svg("rect", { x: x1 + evW + (evW > 0 ? 1 : 0), y: by, width: Math.max(0, leadW - 1), height: bh, rx: 2, fill: r.color, "fill-opacity": 0.35, class: "lv-pvev__bar" }) : null,
          svg("text", { x: x1 + evW + leadW + 6, y: by + 10, class: "lv-pvev__val" }, `${fmtInt(r.ev)}${r.evLeading ? ` +${fmtInt(r.evLeading)}` : ""}`),
          pvW > 0 ? svg("rect", { x: x2, y: by, width: pvW, height: bh, rx: 2, fill: r.color, class: "lv-pvev__bar" }) : null,
          svg("text", { x: x2 + pvW + 6, y: by + 10, class: "lv-pvev__val" }, r.pct === null || r.pct === undefined ? "–" : fmtPct(r.pct)),
          hasEff && r.efficiency !== null && r.efficiency !== undefined
            ? svg("text", { x: W - 2, y: by + 10, "text-anchor": "end", class: "lv-pvev__eff" }, `${r.efficiency > 0 ? "+" : r.efficiency < 0 ? "−" : ""}${Math.abs(r.efficiency).toFixed(1)}`)
            : null,
        ),
      );
    });
    if (hasEff) nodes.push(svg("text", { x: W - 2, y: 11, "text-anchor": "end", class: "lv-pvev__head" }, "Eff. pp"));
    mount(host, svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Popular vote versus electoral votes: ${rows.map((r) => `${r.name} ${r.ev} EV, ${r.pct ?? "no"} percent`).join("; ")}` }, ...nodes));
    mount(
      tableHost,
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "data" },
          h("thead", null, h("tr", null, h("th", null, "Ticket"), h("th", { class: "r" }, "EV"), h("th", { class: "r" }, "EV share"), h("th", { class: "r" }, "Votes"), h("th", { class: "r" }, "PV %"), hasEff ? h("th", { class: "r" }, "Efficiency (pp)") : null)),
          h(
            "tbody",
            null,
            rows.map((r) =>
              h(
                "tr",
                null,
                h("td", null, h("span", { class: "chip", style: { "--party": r.color } }, h("span", { class: "chip__swatch" }), `${r.name} (${r.party || "IND"})`)),
                h("td", { class: "r" }, fmtInt(r.ev)),
                h("td", { class: "r" }, r.evShare === null || r.evShare === undefined ? "–" : fmtPct(r.evShare * 100)),
                h("td", { class: "r" }, fmtInt(r.votes)),
                h("td", { class: "r" }, fmtPct(r.pct)),
                hasEff ? h("td", { class: "r" }, r.efficiency === null || r.efficiency === undefined ? "–" : r.efficiency.toFixed(2)) : null,
              ),
            ),
          ),
        ),
      ),
    );
  }

  return { el, update };
}
