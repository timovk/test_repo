/**
 * Analysis charts (forecast distributions, interval plots, poll trends, compositions, scatter,
 * tile heat maps).  Dataviz rules: thin marks (2px lines, ≤24px bars, 4px rounded data ends),
 * one y-axis, hairline solid grid, legends for ≥2 series, text in text tokens (never the
 * series colour), hover + keyboard tooltips on every chart, colour follows the entity.
 *
 * Charts are *fluid*: the SVG is re-rendered at the container's pixel width (ResizeObserver)
 * so axis text keeps its size instead of being scaled by the viewBox.
 */
import { h, svg, mount } from "../dom.js";
import { hideTip, showTip } from "./tooltip.js";
import { TILE_POSITIONS } from "./tilemap.js";
import { legend } from "./charts.js";

/* ------------------------------------------------------------------ helpers */

export function niceTicks(min, max, n = 4) {
  if (!(max > min)) return [min];
  const span = max - min;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || mag * 10;
  const out = [];
  for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

const pctFmt = (v) => `${(v * 100).toFixed(v >= 0.1 || v === 0 ? 0 : 1)}%`;

/** Tooltip anchored to an element (keyboard focus shows the same content as hover). */
function tipAt(el, content) {
  const r = el.getBoundingClientRect();
  showTip({ clientX: r.left + r.width / 2, clientY: r.top + Math.min(r.height, 24) }, content);
}

function tipRow(color, label, value, { line = true } = {}) {
  return h(
    "div",
    { class: "ana-tip__row" },
    h("span", { class: "ana-tip__key" }, color ? h("span", { class: line ? "ana-tip__line" : "chip__swatch", style: { "--party": color } }) : null, label),
    h("b", { class: "num" }, value),
  );
}

export function tipBox(title, rows, note) {
  return h("div", { class: "ana-tip" }, title ? h("div", { class: "ana-tip__title" }, title) : null, ...rows, note ? h("div", { class: "ana-tip__note" }, note) : null);
}

/**
 * Fluid wrapper: calls render(width) whenever the container width changes and mounts the
 * returned node.  Returns the container; `container.rerender()` forces a redraw.
 */
export function fluid(render, { className = "chart", minWidth = 240, ariaLabel } = {}) {
  const el = h("div", { class: ["ana-fluid", className], role: ariaLabel ? "figure" : undefined, "aria-label": ariaLabel });
  let last = 0;
  const draw = (w) => {
    const width = Math.max(minWidth, Math.floor(w));
    if (width === last) return;
    last = width;
    mount(el, render(width));
  };
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width;
      if (w) requestAnimationFrame(() => draw(w));
    });
    ro.observe(el);
  } else {
    requestAnimationFrame(() => draw(el.clientWidth || 640));
  }
  el.rerender = () => {
    last = 0;
    draw(el.clientWidth || 640);
  };
  return el;
}

/* ------------------------------------------------------------------ distribution */

/**
 * Probability distribution over integer outcomes (EV 0…174, seats 0…150): P(X = k) columns,
 * a majority line, solid colour at/above the majority and a lighter wash below it.
 */
export function distChart({ probs, color, majority, majorityLabel, unit = "EV", height = 190, ariaLabel, summary }) {
  const n = probs.length;
  const ymax = Math.max(...probs, 1e-9);
  return fluid(
    (width) => {
      const m = { t: 22, r: 10, b: 24, l: 40 };
      const iw = width - m.l - m.r;
      const ih = height - m.t - m.b;
      const bw = iw / n;
      const X = (k) => m.l + k * bw;
      const Y = (p) => m.t + ih - (p / ymax) * ih;
      const yt = niceTicks(0, ymax, 3);
      const xt = niceTicks(0, n - 1, Math.max(3, Math.floor(iw / 70))).filter(Number.isInteger);
      const bars = [];
      probs.forEach((p, k) => {
        if (!(p > 0)) return;
        const gap = bw > 4 ? 1 : 0;
        bars.push(
          svg("rect", {
            x: X(k) + gap / 2,
            y: Y(p),
            width: Math.max(0.8, bw - gap),
            height: Math.max(0.5, m.t + ih - Y(p)),
            fill: color,
            "fill-opacity": majority != null && k < majority ? 0.38 : 1,
          }),
        );
      });
      const hl = svg("rect", { y: m.t, height: ih, width: Math.max(2, bw), fill: "var(--text-primary)", "fill-opacity": 0.08, visibility: "hidden" });
      let cur = null;
      const tipFor = (k) =>
        tipBox(`${k} ${unit}`, [tipRow(color, "Share of simulations", `${((probs[k] || 0) * 100).toFixed(2)}%`)], majority != null ? (k >= majority ? `At or above ${majority}: outright majority` : `Below ${majority}`) : null);
      const setCur = (k, evt) => {
        cur = Math.max(0, Math.min(n - 1, k));
        hl.setAttribute("x", X(cur));
        hl.setAttribute("visibility", "visible");
        if (evt) showTip(evt, tipFor(cur));
      };
      const svgEl = svg(
        "svg",
        {
          viewBox: `0 0 ${width} ${height}`,
          width,
          height,
          role: "img",
          "aria-label": ariaLabel || `Distribution of ${unit}`,
          tabindex: 0,
          onkeydown: (e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            e.preventDefault();
            let k = cur ?? probs.indexOf(ymax);
            const dir = e.key === "ArrowRight" ? 1 : -1;
            do k += dir;
            while (k > 0 && k < n - 1 && !(probs[k] > 0));
            setCur(k);
            const r = e.currentTarget.getBoundingClientRect();
            showTip({ clientX: r.left + ((X(cur) + bw / 2) / width) * r.width, clientY: r.top + 20 }, tipFor(cur));
          },
          onblur: () => {
            hl.setAttribute("visibility", "hidden");
            hideTip();
          },
        },
        ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
        svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, pctFmt(v)))),
        svg("g", { class: "axis" }, ...xt.map((v) => svg("text", { x: X(v) + bw / 2, y: height - 6, "text-anchor": "middle" }, String(v)))),
        svg("line", { x1: m.l, x2: width - m.r, y1: m.t + ih, y2: m.t + ih, stroke: "var(--border-strong)" }),
        ...bars,
        hl,
        majority != null
          ? svg(
              "g",
              { "pointer-events": "none" },
              svg("line", { x1: X(majority), x2: X(majority), y1: m.t - 8, y2: m.t + ih, stroke: "var(--text-primary)", "stroke-width": 1.5 }),
              svg("text", { x: X(majority) + 5, y: m.t - 10, "font-size": 10.5, "font-weight": 800, "letter-spacing": "0.06em", fill: "var(--text-primary)" }, majorityLabel || `${majority}`),
            )
          : null,
        svg("rect", {
          x: m.l,
          y: m.t,
          width: iw,
          height: ih,
          fill: "transparent",
          onmousemove: (e) => {
            const r = e.currentTarget.ownerSVGElement.getBoundingClientRect();
            const px = ((e.clientX - r.left) / r.width) * width;
            setCur(Math.floor((px - m.l) / bw), e);
          },
          onmouseleave: () => {
            hl.setAttribute("visibility", "hidden");
            hideTip();
          },
        }),
      );
      return svgEl;
    },
    { ariaLabel: summary },
  );
}

/* ------------------------------------------------------------------ interval plot */

/**
 * Interval ("forest") plot: one row per entity, 5–95 % thin bar, 25–75 % thick bar, median dot,
 * optional threshold line (e.g. 88 TO WIN / 76 FOR CONTROL).
 * rows: [{key, label, color, q: {p05, p25, median, p75, p95, mean}, note}]
 */
export function rangeChart({ rows, domain, marker, format = (v) => String(Math.round(v)), ariaLabel, rowH = 30, labelW = 118, unit = "" }) {
  const [d0, d1] = domain;
  const height = rows.length * rowH + 44;
  return fluid(
    (width) => {
      const m = { t: 24, r: 16, b: 20, l: labelW };
      const iw = width - m.l - m.r;
      const X = (v) => m.l + ((Math.max(d0, Math.min(d1, v)) - d0) / (d1 - d0 || 1)) * iw;
      const xt = niceTicks(d0, d1, Math.max(3, Math.floor(iw / 80)));
      const g = [];
      rows.forEach((r, i) => {
        const cy = m.t + i * rowH + rowH / 2;
        const q = r.q || {};
        const tip = () =>
          r.tip ? r.tip() : tipBox(r.label, [
            tipRow(r.color, "Median", `${format(q.median)}${unit}`),
            tipRow(null, "Mean", `${format(q.mean)}${unit}`),
            tipRow(null, "50% interval", `${format(q.p25)}–${format(q.p75)}${unit}`),
            tipRow(null, "90% interval", `${format(q.p05)}–${format(q.p95)}${unit}`),
          ], r.note);
        g.push(
          svg(
            "g",
            {
              tabindex: 0,
              role: "img",
              "aria-label": r.aria || `${r.label}: median ${format(q.median)}${unit}, 90% interval ${format(q.p05)} to ${format(q.p95)}`,
              class: "ana-range-row",
              onmousemove: (e) => showTip(e, tip()),
              onmouseleave: hideTip,
              onfocus: (e) => tipAt(e.currentTarget, tip()),
              onblur: hideTip,
            },
            svg("rect", { x: 0, y: cy - rowH / 2, width, height: rowH, fill: "transparent" }),
            svg("rect", { x: m.l - labelW + 2, y: cy - 5, width: 10, height: 10, rx: 2, fill: r.color }),
            svg("text", { x: m.l - labelW + 18, y: cy + 4, "font-size": 12, "font-weight": 700, fill: "var(--text-primary)" }, r.label),
            svg("line", { x1: m.l, x2: m.l + iw, y1: cy, y2: cy, stroke: "var(--grid)" }),
            q.p05 != null ? svg("rect", { x: X(q.p05), y: cy - 2, width: Math.max(1.5, X(q.p95) - X(q.p05)), height: 4, rx: 2, fill: r.color, "fill-opacity": 0.45 }) : null,
            q.p25 != null ? svg("rect", { x: X(q.p25), y: cy - 6, width: Math.max(3, X(q.p75) - X(q.p25)), height: 12, rx: 4, fill: r.color }) : null,
            q.median != null ? svg("circle", { cx: X(q.median), cy, r: q.p25 != null ? 5 : 5.5, fill: q.p25 != null ? "var(--text-primary)" : r.color, stroke: "var(--surface-1)", "stroke-width": 2 }) : null,
          ),
        );
      });
      return svg(
        "svg",
        { viewBox: `0 0 ${width} ${height}`, width, height, role: "group", "aria-label": ariaLabel || "Interval plot" },
        ...xt.map((v) => svg("line", { class: "gridline", x1: X(v), x2: X(v), y1: m.t - 4, y2: height - m.b })),
        svg("g", { class: "axis" }, ...xt.map((v) => svg("text", { x: X(v), y: height - 5, "text-anchor": "middle" }, `${format(v)}`))),
        ...g,
        marker
          ? svg(
              "g",
              { "pointer-events": "none" },
              svg("line", { x1: X(marker.x), x2: X(marker.x), y1: m.t - 10, y2: height - m.b, stroke: "var(--text-primary)", "stroke-width": 1.5 }),
              svg("text", { x: X(marker.x), y: m.t - 13, "text-anchor": "middle", "font-size": 10.5, "font-weight": 800, "letter-spacing": "0.06em", fill: "var(--text-primary)" }, marker.label),
            )
          : null,
      );
    },
    { className: "chart ana-range" },
  );
}

/* ------------------------------------------------------------------ multi-line */

/**
 * Multi-series line chart with optional uncertainty bands, area wash, markers and a scatter
 * layer (individual polls) under the lines.  Crosshair tooltip lists every series at the X.
 * series: [{key, label, color, points: [{x, y, lo?, hi?}], area?, dashed?}]
 * scatter: [{x, y, color, title?}]
 */
export function multiLine({
  series,
  scatter = [],
  height = 280,
  xType = "number",
  xFormat,
  yFormat = (v) => `${v.toFixed(0)}%`,
  tipFormat,
  yDomain,
  ariaLabel = "Line chart",
  showDots = false,
  showLegend = true,
  marker,
  xTicks,
  bandLabel,
  zeroLine = false,
}) {
  const xv = (x) => (x instanceof Date ? x.getTime() : x);
  const fmtX = xFormat || ((x) => (x instanceof Date ? x.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : String(x)));
  const tipF = tipFormat || yFormat;
  const all = series.flatMap((s) => s.points);
  if (!all.length) return h("div", { class: "state" }, "No data");
  const xsAll = [...new Set([...all.map((p) => xv(p.x)), ...scatter.map((p) => xv(p.x))])].sort((a, b) => a - b);
  const xmin = xsAll[0];
  const xmax = xsAll[xsAll.length - 1];
  const ys = [...all.flatMap((p) => [p.lo ?? p.y, p.hi ?? p.y]), ...scatter.map((p) => p.y)].filter((v) => v != null && !Number.isNaN(v));
  const lo = yDomain ? yDomain[0] : Math.min(...ys);
  const hi = yDomain ? yDomain[1] : Math.max(...ys);
  const pad = (hi - lo) * 0.08 || 1;
  const y0 = yDomain ? lo : zeroLine ? Math.min(0, lo - pad) : Math.max(0, lo - pad);
  const y1 = yDomain ? hi : hi + pad;
  const lineXs = [...new Set(all.map((p) => xv(p.x)))].sort((a, b) => a - b);

  const chart = fluid(
    (width) => {
      const m = { t: 14, r: marker ? 18 : 14, b: 26, l: 44 };
      const iw = width - m.l - m.r;
      const ih = height - m.t - m.b;
      const single = xmax === xmin;
      const X = (x) => (single ? m.l + iw / 2 : m.l + ((xv(x) - xmin) / (xmax - xmin)) * iw);
      const Y = (y) => m.t + ih - ((y - y0) / (y1 - y0 || 1)) * ih;
      const yt = niceTicks(y0, y1, 4);
      let xt;
      if (xTicks) xt = xTicks;
      else if (xType === "date") {
        const k = Math.max(2, Math.min(6, Math.floor(iw / 110)));
        xt = Array.from({ length: k + 1 }, (_, i) => new Date(xmin + ((xmax - xmin) * i) / k));
      } else xt = lineXs.length <= 8 ? lineXs : niceTicks(xmin, xmax, Math.floor(iw / 90));
      const layers = [];
      for (const s of series) {
        const pts = s.points.filter((p) => p.y != null);
        if (pts.some((p) => p.lo != null && p.hi != null) && pts.length > 1) {
          const up = pts.map((p) => `${X(p.x)},${Y(p.hi ?? p.y)}`).join(" L");
          const down = [...pts].reverse().map((p) => `${X(p.x)},${Y(p.lo ?? p.y)}`).join(" L");
          layers.push(svg("path", { d: `M${up} L${down} Z`, fill: s.color, "fill-opacity": 0.1, stroke: "none" }));
        }
        if (s.area && pts.length > 1) {
          const base = Y(Math.max(y0, 0));
          layers.push(svg("path", { d: `M${X(pts[0].x)},${base} L${pts.map((p) => `${X(p.x)},${Y(p.y)}`).join(" L")} L${X(pts[pts.length - 1].x)},${base} Z`, fill: s.color, "fill-opacity": 0.1 }));
        }
      }
      const dots = scatter.map((p) => svg("circle", { cx: X(p.x), cy: Y(p.y), r: 2.6, fill: p.color, "fill-opacity": 0.42 }));
      const lines = series.map((s) => {
        const pts = s.points.filter((p) => p.y != null);
        return svg(
          "g",
          null,
          pts.length > 1 ? svg("path", { d: `M${pts.map((p) => `${X(p.x)},${Y(p.y)}`).join(" L")}`, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dashed ? "5 4" : undefined }) : null,
          showDots || pts.length === 1 ? pts.map((p) => svg("circle", { cx: X(p.x), cy: Y(p.y), r: 4.5, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 })) : null,
        );
      });
      const cross = svg("line", { y1: m.t, y2: m.t + ih, stroke: "var(--text-muted)", "stroke-width": 1, visibility: "hidden" });
      const focusDots = svg("g", { visibility: "hidden" });
      let curIdx = null;
      const tipFor = (xval) => {
        const rows = series
          .map((s) => {
            let best = null;
            for (const p of s.points) if (p.y != null && (best === null || Math.abs(xv(p.x) - xval) < Math.abs(xv(best.x) - xval))) best = p;
            return best && Math.abs(xv(best.x) - xval) <= (xmax - xmin) / 40 + 1e-9 + (lineXs.length < 12 ? Infinity : 0) ? { s, p: best } : null;
          })
          .filter(Boolean)
          .sort((a, b) => b.p.y - a.p.y);
        const near = scatter.filter((p) => xv(p.x) === xval);
        return {
          rows,
          node: tipBox(
            fmtX(xType === "date" ? new Date(xval) : xval),
            rows.map(({ s, p }) => tipRow(s.color, s.label, p.lo != null && p.hi != null && bandLabel ? `${tipF(p.y)} (${tipF(p.lo)}–${tipF(p.hi)})` : tipF(p.y))),
            near.length ? `${near.length} individual poll result${near.length > 1 ? "s" : ""} on this date` : bandLabel ? `Band: ${bandLabel}` : null,
          ),
        };
      };
      const setX = (xval, evt, anchor) => {
        const px = X(xval);
        cross.setAttribute("x1", px);
        cross.setAttribute("x2", px);
        cross.setAttribute("visibility", "visible");
        const { rows, node } = tipFor(xval);
        mount(focusDots, ...rows.map(({ s, p }) => svg("circle", { cx: X(p.x), cy: Y(p.y), r: 4, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 })));
        focusDots.setAttribute("visibility", "visible");
        if (evt) showTip(evt, node);
        else if (anchor) {
          const r = anchor.getBoundingClientRect();
          showTip({ clientX: r.left + (px / width) * r.width, clientY: r.top + 24 }, node);
        }
      };
      const hide = () => {
        cross.setAttribute("visibility", "hidden");
        focusDots.setAttribute("visibility", "hidden");
        hideTip();
      };
      return svg(
        "svg",
        {
          viewBox: `0 0 ${width} ${height}`,
          width,
          height,
          role: "img",
          "aria-label": ariaLabel,
          tabindex: 0,
          onkeydown: (e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            e.preventDefault();
            curIdx = curIdx === null ? lineXs.length - 1 : Math.max(0, Math.min(lineXs.length - 1, curIdx + (e.key === "ArrowRight" ? 1 : -1)));
            setX(lineXs[curIdx], null, e.currentTarget);
          },
          onblur: hide,
        },
        ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
        zeroLine && y0 < 0 ? svg("line", { x1: m.l, x2: width - m.r, y1: Y(0), y2: Y(0), stroke: "var(--border-strong)" }) : null,
        svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, yFormat(v)))),
        svg("g", { class: "axis" }, ...xt.map((v, i) => svg("text", { x: X(v), y: height - 7, "text-anchor": xt.length > 1 && i === 0 && xType === "date" ? "start" : xt.length > 1 && i === xt.length - 1 && xType === "date" ? "end" : "middle" }, fmtX(v)))),
        ...layers,
        ...dots,
        ...lines,
        marker
          ? svg(
              "g",
              { "pointer-events": "none" },
              svg("line", { x1: m.l, x2: width - m.r, y1: Y(marker.y), y2: Y(marker.y), stroke: "var(--text-primary)", "stroke-width": 1.2 }),
              svg("text", { x: width - m.r, y: Y(marker.y) - 5, "text-anchor": "end", "font-size": 10.5, "font-weight": 800, fill: "var(--text-primary)" }, marker.label),
            )
          : null,
        cross,
        focusDots,
        svg("rect", {
          x: m.l,
          y: m.t,
          width: iw,
          height: ih,
          fill: "transparent",
          onmousemove: (e) => {
            const r = e.currentTarget.ownerSVGElement.getBoundingClientRect();
            const px = ((e.clientX - r.left) / r.width) * width;
            const xval = single ? xmin : xmin + ((px - m.l) / iw) * (xmax - xmin);
            let best = lineXs[0];
            for (const x of lineXs) if (Math.abs(x - xval) < Math.abs(best - xval)) best = x;
            curIdx = lineXs.indexOf(best);
            setX(best, e);
          },
          onmouseleave: hide,
        }),
      );
    },
    { ariaLabel },
  );
  if (!showLegend || series.length < 2) return chart;
  return h("div", { class: "ana-chartbox" }, legend(series.map((s) => ({ label: s.label, color: s.color }))), chart);
}

/* ------------------------------------------------------------------ bars */

/**
 * Horizontal bars (HTML, responsive): label · bar · value.  Diverging when values have both
 * signs (`diverging: true`), bars grow from a shared zero line.
 * rows: [{key, label, labelNode?, value, color, tip?, href?}]
 */
export function hbars({ rows, format = (v) => String(v), max, diverging = false, ariaLabel = "Bar chart", labelWidth = 128, emptyText = "No data" }) {
  if (!rows.length) return h("div", { class: "state" }, emptyText);
  const mx = max ?? Math.max(...rows.map((r) => Math.abs(r.value || 0)), 1e-9);
  return h(
    "div",
    { class: ["ana-hbars", diverging && "ana-hbars--div"], role: "list", "aria-label": ariaLabel, style: { "--label-w": `${labelWidth}px` } },
    rows.map((r) => {
      const v = r.value || 0;
      const w = `${(Math.abs(v) / mx) * (diverging ? 50 : 100)}%`;
      const tip = () => r.tip || tipBox(r.label, [tipRow(r.color, r.tipLabel || "Value", format(v))], r.note);
      const bar = h("div", { class: ["ana-hbars__bar", v < 0 && "is-neg"], style: { width: w, "--party": r.color || "var(--seq-4)" } });
      return h(
        r.href ? "a" : "div",
        {
          class: "ana-hbars__row",
          role: "listitem",
          href: r.href,
          tabindex: r.href ? undefined : 0,
          "aria-label": `${r.label}: ${format(v)}`,
          onmousemove: (e) => showTip(e, tip()),
          onmouseleave: hideTip,
          onfocus: (e) => tipAt(e.currentTarget, tip()),
          onblur: hideTip,
        },
        h("span", { class: "ana-hbars__label" }, r.labelNode || r.label),
        h("span", { class: "ana-hbars__track" }, diverging ? h("span", { class: "ana-hbars__zero" }) : null, bar),
        h("span", { class: "ana-hbars__val num" }, format(v)),
      );
    }),
  );
}

/**
 * Vertical columns for categorical / binned x (e.g. spend per week, deviation bins).
 * bins: [{label, y, color?, tip?}]
 */
export function columns({ bins, color = "var(--seq-4)", yFormat = (v) => String(v), height = 180, ariaLabel = "Column chart", xLabelEvery, marker }) {
  if (!bins.length) return h("div", { class: "state" }, "No data");
  const ymax = Math.max(...bins.map((b) => b.y), 1e-9);
  return fluid((width) => {
    const m = { t: 16, r: 8, b: 26, l: 40 };
    const iw = width - m.l - m.r;
    const ih = height - m.t - m.b;
    const bw = iw / bins.length;
    const Y = (v) => m.t + ih - (v / ymax) * ih;
    const yt = niceTicks(0, ymax, 3);
    const every = xLabelEvery || Math.max(1, Math.ceil(bins.length / Math.max(2, Math.floor(iw / 48))));
    const barW = Math.min(24, Math.max(1, bw - 2));
    return svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": ariaLabel },
      ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
      svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, yFormat(v)))),
      svg("line", { x1: m.l, x2: width - m.r, y1: m.t + ih, y2: m.t + ih, stroke: "var(--border-strong)" }),
      ...bins.map((b, i) => {
        const x = m.l + i * bw + (bw - barW) / 2;
        const hgt = Math.max(b.y > 0 ? 1 : 0, m.t + ih - Y(b.y));
        const r = Math.min(4, barW / 2, hgt);
        const y = m.t + ih - hgt;
        const tip = () => b.tip || tipBox(b.label, [tipRow(b.color || color, "Value", yFormat(b.y))]);
        return svg(
          "g",
          { onmousemove: (e) => showTip(e, tip()), onmouseleave: hideTip, class: "ana-col" },
          svg("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }),
          hgt > 0 ? svg("path", { d: `M${x},${y + hgt} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + barW - r},${y} Q${x + barW},${y} ${x + barW},${y + r} L${x + barW},${y + hgt} Z`, fill: b.color || color }) : null,
          i % every === 0 ? svg("text", { class: "ana-col__x", x: m.l + i * bw + bw / 2, y: height - 7, "text-anchor": "middle", "font-size": 10.5, fill: "var(--text-muted)" }, b.label) : null,
        );
      }),
      marker ? svg("line", { x1: m.l + marker.index * bw, x2: m.l + marker.index * bw, y1: m.t - 4, y2: m.t + ih, stroke: "var(--text-primary)", "stroke-width": 1.2 }) : null,
      marker ? svg("text", { x: m.l + marker.index * bw + 4, y: m.t + 6, "font-size": 10.5, "font-weight": 800, fill: "var(--text-primary)" }, marker.label) : null,
    );
  }, { ariaLabel });
}

/* ------------------------------------------------------------------ composition */

/**
 * Part-to-whole bar (seats / compositions) with 2px surface gaps and an optional majority mark.
 * segments: [{key, label, color, value}] in a stable (party) order.
 */
export function stackBar({ segments, total, marker, height = 18, ariaLabel, labels = true, unit = "seats" }) {
  const sum = total || segments.reduce((a, s) => a + (s.value || 0), 0) || 1;
  const segs = segments
    .filter((s) => s.value > 0)
    .map((s) => {
      const tip = () => tipBox(s.label, [tipRow(s.color, s.tipLabel || unit, String(s.display ?? s.value), { line: false })]);
      return h(
        "div",
        {
          class: "ana-stack__seg",
          style: { flex: `0 0 calc(${(s.value / sum) * 100}% - 2px)`, "--party": s.color },
          tabindex: 0,
          "aria-label": `${s.label}: ${s.display ?? s.value} ${unit}`,
          onmousemove: (e) => showTip(e, tip()),
          onmouseleave: hideTip,
          onfocus: (e) => tipAt(e.currentTarget, tip()),
          onblur: hideTip,
        },
        labels && s.value / sum >= 0.085 ? h("span", { class: "ana-stack__lbl" }, s.short ?? s.key) : null,
      );
    });
  return h(
    "div",
    { class: "ana-stack", style: { "--h": `${height}px` }, role: "group", "aria-label": ariaLabel || segments.map((s) => `${s.label} ${s.value}`).join(", ") },
    h("div", { class: "ana-stack__track" }, segs),
    marker ? h("div", { class: "ana-stack__marker", style: { left: `${(marker.at / sum) * 100}%` }, title: marker.label }) : null,
  );
}

/* ------------------------------------------------------------------ scatter */

/** Labelled scatter (few points, e.g. parties): dots with surface ring + direct labels. */
export function scatterChart({ points, xDomain, yDomain, xFormat = (v) => String(v), yFormat = (v) => String(v), xLabel, yLabel, diagonal = false, diagonalLabel = "x = y", height = 300, ariaLabel = "Scatter plot", tip }) {
  if (!points.length) return h("div", { class: "state" }, "No data");
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const [x0, x1] = xDomain || [Math.min(0, ...xs), Math.max(...xs) * 1.08 || 1];
  const [y0, y1] = yDomain || [Math.min(0, ...ys), Math.max(...ys) * 1.08 || 1];
  return fluid((width) => {
    const m = { t: 14, r: 18, b: 40, l: 52 };
    const iw = width - m.l - m.r;
    const ih = height - m.t - m.b;
    const X = (v) => m.l + ((v - x0) / (x1 - x0 || 1)) * iw;
    const Y = (v) => m.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;
    const xt = niceTicks(x0, x1, Math.max(3, Math.floor(iw / 90)));
    const yt = niceTicks(y0, y1, 4);
    const lo = Math.max(x0, y0);
    const hi = Math.min(x1, y1);
    // Greedy label placement: nudge a direct label down when it would collide with a placed one.
    const labelY = new Map();
    const placed = [];
    for (const p of [...points].sort((a, b) => Y(a.y) - Y(b.y) || X(a.x) - X(b.x))) {
      let y = Y(p.y) + 4;
      const x = X(p.x) + 9;
      const w = String(p.short || p.label).length * 6.6;
      for (let guard = 0; guard < 8 && placed.some((q) => Math.abs(q.y - y) < 12 && x < q.x + q.w && q.x < x + w); guard++) y += 12;
      placed.push({ x, y, w });
      labelY.set(p, y);
    }
    return svg(
      "svg",
      { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": ariaLabel },
      ...yt.map((v) => svg("line", { class: "gridline", x1: m.l, x2: width - m.r, y1: Y(v), y2: Y(v) })),
      ...xt.map((v) => svg("line", { class: "gridline", x1: X(v), x2: X(v), y1: m.t, y2: m.t + ih })),
      svg("g", { class: "axis" }, ...yt.map((v) => svg("text", { x: m.l - 6, y: Y(v) + 3, "text-anchor": "end" }, yFormat(v)))),
      svg("g", { class: "axis" }, ...xt.map((v) => svg("text", { x: X(v), y: m.t + ih + 15, "text-anchor": "middle" }, xFormat(v)))),
      xLabel ? svg("text", { x: m.l + iw / 2, y: height - 4, "text-anchor": "middle", "font-size": 11, fill: "var(--text-secondary)" }, xLabel) : null,
      yLabel ? svg("text", { x: 12, y: m.t + ih / 2, "text-anchor": "middle", "font-size": 11, fill: "var(--text-secondary)", transform: `rotate(-90 12 ${m.t + ih / 2})` }, yLabel) : null,
      diagonal && hi > lo ? svg("line", { x1: X(lo), y1: Y(lo), x2: X(hi), y2: Y(hi), stroke: "var(--border-strong)", "stroke-width": 1.2 }) : null,
      diagonal && hi > lo ? svg("text", { x: X(lo + (hi - lo) * 0.62) + 6, y: Y(lo + (hi - lo) * 0.62) + 16, "text-anchor": "start", "font-size": 10.5, fill: "var(--text-muted)" }, diagonalLabel) : null,
      ...points.map((p) => {
        const content = () => (tip ? tip(p) : tipBox(p.label, [tipRow(p.color, xLabel || "x", xFormat(p.x), { line: false }), tipRow(null, yLabel || "y", yFormat(p.y))]));
        return svg(
          "g",
          { tabindex: 0, role: "img", "aria-label": `${p.label}: ${xFormat(p.x)}, ${yFormat(p.y)}`, onmousemove: (e) => showTip(e, content()), onmouseleave: hideTip, onfocus: (e) => tipAt(e.currentTarget, content()), onblur: hideTip },
          svg("circle", { cx: X(p.x), cy: Y(p.y), r: 12, fill: "transparent" }),
          svg("circle", { cx: X(p.x), cy: Y(p.y), r: 5.5, fill: p.color, stroke: "var(--surface-1)", "stroke-width": 2 }),
          svg("text", { x: X(p.x) + 9, y: labelY.get(p) ?? Y(p.y) + 4, "font-size": 11, "font-weight": 700, fill: "var(--text-secondary)" }, p.short || p.label),
        );
      }),
    );
  }, { ariaLabel });
}

/* ------------------------------------------------------------------ tile maps */

/**
 * Province tile heat map (same geography as the shared tile cartogram).
 * rows: [{code, top, bottom, fill, ink:'light'|'dark', title, tip?}]
 */
export function heatTiles(rows, { onSelect, ariaLabel = "Province tile map", size = "md" } = {}) {
  return h(
    "div",
    { class: ["ana-tiles", `ana-tiles--${size}`], role: "group", "aria-label": ariaLabel },
    rows.map((r) => {
      const [row, col] = TILE_POSITIONS[r.code] || [0, 0];
      const tip = () => r.tip || tipBox(r.title || r.code, []);
      return h(
        onSelect ? "button" : "div",
        {
          type: onSelect ? "button" : undefined,
          class: ["ana-tile", r.ink === "light" && "ana-tile--light", r.outline && "ana-tile--outline"],
          style: { gridRow: `${row + 1}`, gridColumn: `${col + 1}`, "--fill": r.fill || "var(--uncalled-soft)", "--party": r.outline || "transparent" },
          tabindex: onSelect ? undefined : 0,
          "aria-label": r.title || r.code,
          onclick: onSelect ? () => onSelect(r.code) : undefined,
          onmousemove: (e) => showTip(e, tip()),
          onmouseleave: hideTip,
          onfocus: (e) => tipAt(e.currentTarget, tip()),
          onblur: hideTip,
        },
        h("span", { class: "ana-tile__code" }, r.code),
        r.bottom != null ? h("span", { class: "ana-tile__sub" }, r.bottom) : null,
      );
    }),
  );
}

/** Tiny tile cartogram (e.g. most common Electoral College maps). winners: {code: color} */
export function miniTiles(winners, { title } = {}) {
  return h(
    "div",
    { class: "ana-mini", role: "img", "aria-label": title || "Electoral College map" },
    Object.entries(TILE_POSITIONS).map(([code, [r, c]]) =>
      h("span", { class: "ana-mini__t", style: { gridRow: `${r + 1}`, gridColumn: `${c + 1}`, "--fill": winners[code]?.color || "var(--uncalled)" }, title: `${code}: ${winners[code]?.label || "–"}` }, code),
    ),
  );
}

/* ------------------------------------------------------------------ table cells */

/** Diverging value cell: centred bar (two hues around a neutral zero) + value in ink. */
export function divCell(value, maxAbs, format = (v) => v.toFixed(1)) {
  if (value === null || value === undefined || Number.isNaN(value)) return h("span", { class: "muted" }, "–");
  const w = Math.min(1, Math.abs(value) / (maxAbs || 1)) * 50;
  return h(
    "span",
    { class: "ana-div" },
    h("span", { class: "ana-div__track" }, h("span", { class: "ana-div__zero" }), h("span", { class: ["ana-div__bar", value < 0 ? "is-neg" : "is-pos"], style: { width: `${w}%` } })),
    h("span", { class: "ana-div__val num" }, format(value)),
  );
}

/** Sequential heat cell: swatch in the sequential ramp + value in ink. */
export function seqCell(value, max, format = (v) => String(v)) {
  if (value === null || value === undefined) return h("span", { class: "muted" }, "–");
  const t = Math.max(0, Math.min(1, value / (max || 1)));
  const step = Math.min(6, 1 + Math.floor(t * 6));
  return h("span", { class: "ana-seq" }, h("span", { class: "ana-seq__sw", style: { background: `var(--seq-${step})` } }), h("span", { class: "num" }, format(value)));
}

export { tipRow };
