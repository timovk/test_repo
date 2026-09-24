/**
 * Lightweight SVG charts following the dataviz rules: thin marks, 2px gaps, recessive grid,
 * one y-axis, hover tooltips, legends for ≥2 series, text in text tokens (never series colour).
 */
import { h, svg } from "../dom.js";
import { hideTip, showTip } from "./tooltip.js";

const niceTicks = (min, max, n = 4) => {
  if (max <= min) return [min];
  const span = max - min;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || mag * 10;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
};

export function legend(items) {
  return h(
    "div",
    { class: "legend" },
    items.map((it) =>
      h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": it.color } }), it.label),
    ),
  );
}

/**
 * Histogram / column chart.
 * @param {{bins:Array<{x:number, y:number}>, color:string, marker?:{x:number,label:string},
 *          xLabel?:string, yFormat?:(v)=>string, tip?:(bin)=>Node|string, width?:number, height?:number,
 *          highlight?:(bin)=>boolean}} p
 */
export function histogram({ bins, color = "var(--seq-4)", marker, xLabel, yFormat = (v) => `${(v * 100).toFixed(1)}%`, tip, width = 640, height = 200, highlight }) {
  const m = { t: 18, r: 8, b: 26, l: 40 };
  const iw = width - m.l - m.r;
  const ih = height - m.t - m.b;
  if (!bins.length) return h("div", { class: "state" }, "No data");
  const xs = bins.map((b) => b.x);
  const xmin = Math.min(...xs);
  const xmax = Math.max(...xs);
  const ymax = Math.max(...bins.map((b) => b.y)) || 1;
  const n = xmax - xmin + 1;
  const bw = iw / n;
  const X = (x) => m.l + (x - xmin) * bw;
  const Y = (y) => m.t + ih - (y / ymax) * ih;
  const yt = niceTicks(0, ymax, 4);
  const xt = niceTicks(xmin, xmax, 8).filter((v) => Number.isInteger(v));
  const bars = bins.map((b) => {
    const hgt = Math.max(0, m.t + ih - Y(b.y));
    return svg("rect", {
      x: X(b.x) + (bw > 3 ? 1 : 0),
      y: Y(b.y),
      width: Math.max(0.6, bw - (bw > 3 ? 2 : 0)),
      height: hgt,
      rx: bw > 6 ? 2 : 0,
      fill: highlight && !highlight(b) ? "var(--uncalled)" : color,
      onmousemove: (e) => showTip(e, tip ? tip(b) : `${b.x}: ${yFormat(b.y)}`),
      onmouseleave: hideTip,
    });
  });
  return h(
    "div",
    { class: "chart" },
    svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": xLabel || "histogram" },
      ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
      svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, yFormat(v)))),
      svg("g", { class: "axis" }, ...xt.map((v) => svg("text", { x: X(v) + bw / 2, y: height - 8, "text-anchor": "middle" }, String(v)))),
      ...bars,
      marker
        ? svg(
            "g",
            null,
            svg("line", { x1: X(marker.x), x2: X(marker.x), y1: m.t - 6, y2: m.t + ih, stroke: "var(--text-primary)", "stroke-width": 1.5, "stroke-dasharray": "4 3" }),
            svg("text", { x: X(marker.x) + 4, y: m.t - 6, "font-size": 10.5, "font-weight": 800, fill: "var(--text-primary)" }, marker.label),
          )
        : null,
    ),
  );
}

/**
 * Multi-series line chart with optional bands and a crosshair tooltip.
 * series: [{key,label,color,points:[{x:Date|number,y:number,lo?:number,hi?:number}]}]
 */
export function lineChart({ series, width = 720, height = 260, yFormat = (v) => `${v.toFixed(0)}%`, xFormat = (x) => (x instanceof Date ? x.toISOString().slice(5, 10) : String(x)), yDomain, xTicks, markers = false, label }) {
  const m = { t: 12, r: 12, b: 26, l: 42 };
  const iw = width - m.l - m.r;
  const ih = height - m.t - m.b;
  const all = series.flatMap((s) => s.points);
  if (!all.length) return h("div", { class: "state" }, "No data");
  const xv = (x) => (x instanceof Date ? x.getTime() : x);
  const xmin = Math.min(...all.map((p) => xv(p.x)));
  const xmax = Math.max(...all.map((p) => xv(p.x)));
  const ylo = yDomain ? yDomain[0] : Math.min(...all.map((p) => p.lo ?? p.y));
  const yhi = yDomain ? yDomain[1] : Math.max(...all.map((p) => p.hi ?? p.y));
  const pad = (yhi - ylo) * 0.08 || 1;
  const y0 = yDomain ? ylo : Math.max(0, ylo - pad);
  const y1 = yDomain ? yhi : yhi + pad;
  const X = (x) => m.l + ((xv(x) - xmin) / Math.max(1, xmax - xmin)) * iw;
  const Y = (y) => m.t + ih - ((y - y0) / (y1 - y0)) * ih;
  const yt = niceTicks(y0, y1, 4);
  const paths = [];
  for (const s of series) {
    const pts = s.points;
    if (pts.some((p) => p.lo !== undefined)) {
      const up = pts.map((p) => `${X(p.x)},${Y(p.hi ?? p.y)}`).join(" L");
      const down = [...pts].reverse().map((p) => `${X(p.x)},${Y(p.lo ?? p.y)}`).join(" L");
      paths.push(svg("path", { d: `M${up} L${down} Z`, fill: s.color, "fill-opacity": 0.12, stroke: "none" }));
    }
    paths.push(svg("path", { d: `M${pts.map((p) => `${X(p.x)},${Y(p.y)}`).join(" L")}`, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round" }));
    // Optional end/point markers (r=4 with a 2px surface ring) for sparse series.
    if (markers) for (const p of pts) paths.push(svg("circle", { cx: X(p.x), cy: Y(p.y), r: 4, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 }));
  }
  const cross = svg("line", { y1: m.t, y2: m.t + ih, stroke: "var(--text-muted)", "stroke-width": 1, visibility: "hidden" });
  const overlay = svg("rect", {
    x: m.l,
    y: m.t,
    width: iw,
    height: ih,
    fill: "transparent",
    onmousemove: (e) => {
      const rect = e.currentTarget.ownerSVGElement.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * width;
      const xval = xmin + ((px - m.l) / iw) * (xmax - xmin);
      cross.setAttribute("x1", px);
      cross.setAttribute("x2", px);
      cross.setAttribute("visibility", "visible");
      const rows = series.map((s) => {
        let best = s.points[0];
        for (const p of s.points) if (Math.abs(xv(p.x) - xval) < Math.abs(xv(best.x) - xval)) best = p;
        return { s, p: best };
      });
      showTip(
        e,
        h(
          "div",
          null,
          h("div", { class: "muted", style: { marginBottom: "4px" } }, xFormat(rows[0].p.x)),
          rows
            .sort((a, b) => b.p.y - a.p.y)
            .map(({ s, p }) => h("div", { class: "legend__item", style: { justifyContent: "space-between", display: "flex", gap: "12px" } }, h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": s.color } }), s.label), h("b", { class: "num" }, yFormat(p.y)))),
        ),
      );
    },
    onmouseleave: () => {
      cross.setAttribute("visibility", "hidden");
      hideTip();
    },
  });
  const xt = xTicks ? xTicks.map(xv) : [xmin, xmin + (xmax - xmin) / 3, xmin + (2 * (xmax - xmin)) / 3, xmax];
  const sampleX = all[0].x instanceof Date;
  return h(
    "div",
    { class: "chart" },
    series.length > 1 ? legend(series.map((s) => ({ label: s.label, color: s.color }))) : null,
    svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label || "line chart" },
      ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
      svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, yFormat(v)))),
      svg("g", { class: "axis" }, ...xt.map((v) => svg("text", { x: X(sampleX ? new Date(v) : v), y: height - 8, "text-anchor": "middle" }, xFormat(sampleX ? new Date(v) : v)))),
      ...paths,
      cross,
      overlay,
    ),
  );
}

/**
 * Horizontal bars (optionally diverging around zero), e.g. swing by party or vote shares.
 * rows: [{label, value, color, note?}]
 */
export function barChart({ rows, format = (v) => v.toFixed(1), diverging = false, width = 520, rowH = 26 }) {
  const m = { l: 120, r: 56 };
  const height = rows.length * rowH + 8;
  const vals = rows.map((r) => r.value);
  const max = Math.max(...vals.map(Math.abs), 1e-9);
  const iw = width - m.l - m.r;
  const zeroX = diverging ? m.l + iw / 2 : m.l;
  const scale = diverging ? iw / 2 / max : iw / Math.max(...vals, 1e-9);
  return h(
    "div",
    { class: "chart" },
    svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "bar chart" },
      diverging ? svg("line", { x1: zeroX, x2: zeroX, y1: 0, y2: height, stroke: "var(--border-strong)" }) : null,
      ...rows.map((r, i) => {
        const y = i * rowH + 4;
        const w = Math.abs(r.value) * scale;
        const x = r.value >= 0 ? zeroX : zeroX - w;
        return svg(
          "g",
          { onmousemove: (e) => showTip(e, `${r.label}: ${format(r.value)}${r.note ? ` · ${r.note}` : ""}`), onmouseleave: hideTip },
          svg("text", { x: m.l - 8, y: y + rowH / 2, "text-anchor": "end", "font-size": 12, fill: "var(--text-secondary)" }, r.label),
          svg("rect", { x, y: y + 4, width: Math.max(1, w), height: rowH - 10, rx: 3, fill: r.color || "var(--seq-4)" }),
          svg("text", { x: r.value >= 0 ? x + w + 6 : x - 6, y: y + rowH / 2 + 1, "text-anchor": r.value >= 0 ? "start" : "end", "font-size": 12, "font-weight": 700, fill: "var(--text-primary)" }, format(r.value)),
        );
      }),
    ),
  );
}

/** Tiny inline sparkline (no axes). */
export function sparkline(values, { color = "var(--seq-5)", width = 90, height = 24 } = {}) {
  if (!values.length) return h("span", { class: "muted" }, "–");
  const min = Math.min(...values);
  const max = Math.max(...values);
  const X = (i) => (i / Math.max(1, values.length - 1)) * (width - 2) + 1;
  const Y = (v) => height - 2 - ((v - min) / Math.max(1e-9, max - min)) * (height - 4);
  return svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height }, svg("path", { d: `M${values.map((v, i) => `${X(i)},${Y(v)}`).join(" L")}`, fill: "none", stroke: color, "stroke-width": 2 }));
}
