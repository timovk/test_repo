/**
 * Parliament hemicycle (House 150 / Senate 24 / Electoral College 174).  Seats are laid out on
 * concentric arcs and filled left→right by group order.  Each group: {key, label, color, seats,
 * style}, where style ∈ "called" (solid) | "leading" (light) | "notup" (holdover, outlined) |
 * "uncalled" (grey).  The dashed centre line marks control (majority).
 *
 * The returned element has `update({groups, centerLabel, centerSub})`, which recolours the
 * existing seats in place (same total) so live changes fade instead of re-rendering.
 */
import { svg, h } from "../dom.js";
import { hideTip, showTip } from "./tooltip.js";

function layout(n, rows) {
  const inner = 0.42;
  const radii = Array.from({ length: rows }, (_, i) => inner + ((1 - inner) * (i + 0.5)) / rows);
  const total = radii.reduce((a, r) => a + r, 0);
  const counts = radii.map((r) => Math.floor((n * r) / total));
  let rem = n - counts.reduce((a, b) => a + b, 0);
  for (let i = rows - 1; rem > 0; i = (i - 1 + rows) % rows, rem--) counts[i]++;
  const pts = [];
  radii.forEach((r, i) => {
    const c = counts[i];
    for (let j = 0; j < c; j++) {
      const a = Math.PI * (1 - (c === 1 ? 0.5 : j / (c - 1)));
      pts.push({ a, r, x: r * Math.cos(a), y: r * Math.sin(a) });
    }
  });
  pts.sort((p, q) => q.a - p.a || p.r - q.r);
  // Seats never overlap: the radius is bounded by the row spacing and the tightest arc spacing.
  const rowGap = (1 - inner) / rows;
  const arcGap = Math.min(...radii.map((r, i) => (counts[i] > 1 ? (Math.PI * r) / (counts[i] - 1) : 1)));
  return { pts, seatR: (Math.min(rowGap, arcGap) / 2) * 0.86 };
}

/** Rows that make arc spacing ≈ row spacing (n ≈ 3.85 · rows² for the 0.42 inner radius). */
function autoRows(n) {
  return Math.max(2, Math.round(Math.sqrt(n / 3.85)));
}

const STYLE_WORD = { called: "called", leading: "leading", notup: "not up (holdover)", uncalled: "uncalled" };

/**
 * @param {{total:number, majority:number, groups:Array, rows?:number, centerLabel?:string, centerSub?:string,
 *          ariaLabel?:string, tooltip?:(group)=>Node|string}} props
 */
export function hemicycle({ total, majority, groups, rows, centerLabel, centerSub, ariaLabel, tooltip }) {
  const nRows = rows || autoRows(total);
  const { pts, seatR } = layout(total, nRows);
  const W = 2.2;
  const circles = pts.map((p) => {
    const c = svg("circle", { cx: (p.x + 1.1).toFixed(4), cy: (1.05 - p.y).toFixed(4), r: seatR.toFixed(4), class: "hemicycle__seat" });
    c.addEventListener("mousemove", (e) => c._group && showTip(e, tooltip ? tooltip(c._group) : `${c._group.label} · ${STYLE_WORD[c._group.style || "called"] || ""}`));
    c.addEventListener("mouseleave", hideTip);
    return c;
  });
  const label = svg("text", { x: 1.1, y: 0.93, "text-anchor": "middle", "font-size": 0.17, class: "hemicycle__center", fill: "var(--text-primary)" });
  const sub = svg("text", { x: 1.1, y: 1.08, "text-anchor": "middle", "font-size": 0.07, "font-weight": 800, "letter-spacing": 0.01, fill: "var(--text-muted)" });
  const root = h(
    "div",
    { class: "hemicycle", role: "img" },
    svg(
      "svg",
      { viewBox: `0 0 ${W} 1.15` },
      ...circles,
      svg("line", { x1: 1.1, y1: 0.02, x2: 1.1, y2: 0.62, stroke: "var(--text-primary)", "stroke-width": 0.006, "stroke-dasharray": "0.02 0.015", class: "hemicycle__line" }),
      label,
      sub,
    ),
  );

  function paint(next) {
    let i = 0;
    for (const g of next.groups || []) {
      const style = g.style || "called";
      for (let k = 0; k < g.seats && i < circles.length; k++, i++) {
        const c = circles[i];
        c._group = g;
        c.style.fill = style === "uncalled" ? "var(--uncalled)" : style === "notup" ? "var(--not-up)" : g.color;
        c.style.fillOpacity = style === "leading" ? "0.42" : "1";
        c.style.stroke = style === "notup" ? g.color : "none";
        c.style.strokeWidth = style === "notup" ? String(seatR * 0.35) : "0";
      }
    }
    while (i < circles.length) {
      const c = circles[i++];
      c._group = { label: "Uncalled", style: "uncalled", color: "var(--uncalled)" };
      c.style.fill = "var(--uncalled)";
      c.style.fillOpacity = "1";
      c.style.stroke = "none";
      c.style.strokeWidth = "0";
    }
    if (label.textContent !== String(next.centerLabel ?? "")) label.textContent = next.centerLabel ?? "";
    const s = next.centerSub ?? `${majority} FOR CONTROL`;
    if (sub.textContent !== s) sub.textContent = s;
    root.setAttribute(
      "aria-label",
      next.ariaLabel ||
        `${total} seats, ${majority} for control: ` +
          (next.groups || [])
            .filter((g) => g.seats > 0)
            .map((g) => `${g.label} ${g.seats} ${STYLE_WORD[g.style || "called"] || ""}`)
            .join(", "),
    );
  }

  paint({ groups, centerLabel, centerSub, ariaLabel });
  root.update = (next) => {
    paint({ centerLabel, centerSub, ...next });
    return root;
  };
  return root;
}
