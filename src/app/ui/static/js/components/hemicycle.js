/**
 * Parliament hemicycle (House 150 / Senate 24).  Seats are laid out on concentric arcs and
 * filled left→right by group order.  Each group: {key, label, color, seats, style}, where style
 * ∈ "called" (solid) | "leading" (light) | "notup" (holdover, outlined) | "uncalled" (grey).
 */
import { svg, h } from "../dom.js";

function layout(n, rows) {
  const inner = 0.42;
  const radii = Array.from({ length: rows }, (_, i) => inner + ((1 - inner) * (i + 0.5)) / rows);
  const total = radii.reduce((a, r) => a + r, 0);
  let counts = radii.map((r) => Math.floor((n * r) / total));
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
  return { pts, seatR: Math.min(0.5 / rows, (Math.PI * radii[0]) / Math.max(1, counts[0]) / 2) * 0.82 };
}

/**
 * @param {{total:number, majority:number, groups:Array, rows?:number, label?:string}} props
 */
export function hemicycle({ total, majority, groups, rows, centerLabel, centerSub }) {
  const nRows = rows || (total > 100 ? 8 : total > 40 ? 5 : 3);
  const { pts, seatR } = layout(total, nRows);
  const W = 2.2;
  const seats = [];
  let i = 0;
  for (const g of groups) {
    for (let k = 0; k < g.seats && i < pts.length; k++, i++) {
      const p = pts[i];
      const style = g.style || "called";
      seats.push(
        svg("circle", {
          cx: (p.x + 1.1).toFixed(4),
          cy: (1.05 - p.y).toFixed(4),
          r: seatR.toFixed(4),
          fill: style === "uncalled" ? "var(--uncalled)" : style === "notup" ? "var(--not-up)" : g.color,
          "fill-opacity": style === "leading" ? 0.45 : 1,
          stroke: style === "notup" ? g.color : "none",
          "stroke-width": style === "notup" ? seatR * 0.35 : 0,
        }, svg("title", null, `${g.label}${style !== "called" ? ` (${style})` : ""}`)),
      );
    }
  }
  while (i < pts.length) {
    const p = pts[i++];
    seats.push(svg("circle", { cx: (p.x + 1.1).toFixed(4), cy: (1.05 - p.y).toFixed(4), r: seatR.toFixed(4), fill: "var(--uncalled)" }));
  }
  return h(
    "div",
    { class: "hemicycle", role: "img", "aria-label": `${total} seats, ${majority} for control` },
    svg(
      "svg",
      { viewBox: `0 0 ${W} 1.15` },
      ...seats,
      svg("line", { x1: 1.1, y1: 0.02, x2: 1.1, y2: 0.62, stroke: "var(--text-primary)", "stroke-width": 0.006, "stroke-dasharray": "0.02 0.015" }),
      svg("text", { x: 1.1, y: 0.93, "text-anchor": "middle", "font-size": 0.2, class: "hemicycle__center", fill: "var(--text-primary)" }, centerLabel ?? ""),
      svg("text", { x: 1.1, y: 1.08, "text-anchor": "middle", "font-size": 0.07, "font-weight": 800, "letter-spacing": 0.01, fill: "var(--text-muted)" }, centerSub ?? `${majority} FOR CONTROL`),
    ),
  );
}
