/**
 * Small HTML charts for the results pages (prefix `res-`), following the dataviz rules: thin
 * marks with 4px rounded data-ends, 2px surface gaps between stacked segments, text in text
 * tokens (never the series colour), a legend for ≥ 2 series, hover tooltips on every mark and an
 * accessible label summarising the values.  They display API values only.
 */
import { h } from "../dom.js";
import { hideTip, showTip } from "./tooltip.js";

/**
 * Diverging horizontal bars around zero (e.g. swing by party).
 * rows: [{label, value, color, note?}]
 */
export function divBars(rows, { format = (v) => String(v), label = "Diverging bar chart", unit = "" } = {}) {
  const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value || 0)));
  return h(
    "div",
    { class: "res-dbars", role: "img", "aria-label": `${label}: ${rows.map((r) => `${r.label} ${format(r.value)}${unit}`).join(", ")}` },
    rows.map((r) => {
      const w = `${(Math.abs(r.value || 0) / maxAbs) * 78}%`;
      const bar = h("span", { class: "res-dbars__bar", style: { width: w, "--sw": r.color } });
      const val = h("span", { class: "res-dbars__val num" }, format(r.value));
      const tip = (e) => showTip(e, h("div", null, h("b", { class: "num" }, `${format(r.value)}${unit}`), " ", h("span", { class: "muted" }, r.label), r.note ? h("div", { class: "muted" }, r.note) : null));
      return h(
        "div",
        { class: "res-dbars__row", onmousemove: tip, onmouseleave: hideTip },
        h("span", { class: "res-dbars__label" }, h("span", { class: "res-dbars__key", style: { background: r.color } }), r.label),
        h("span", { class: "res-dbars__neg" }, r.value < 0 ? [val, bar] : null),
        h("span", { class: "res-dbars__pos" }, r.value >= 0 ? [bar, val] : null),
      );
    }),
  );
}

/**
 * 100 % stacked bar (composition) with 2px surface gaps, optional majority marker and legend.
 * segments: [{label, value, color, style?: 'solid'|'light'|'outline'|'empty', tip?}]
 */
export function stackBar(segments, { total, marker, markerLabel, label = "Composition", legend = true, height = 18, format = (v) => String(v) } = {}) {
  const sum = total ?? segments.reduce((a, s) => a + (s.value || 0), 0);
  const segs = segments.filter((s) => s.value > 0);
  const bar = h(
    "div",
    { class: "res-stack-bar__track", style: { height: `${height}px` } },
    segs.map((s) =>
      h("span", {
        class: ["res-stack-bar__seg", s.style && `res-stack-bar__seg--${s.style}`],
        style: { flex: `${s.value} 0 0`, "--sw": s.color },
        onmousemove: (e) => showTip(e, s.tip || h("div", null, h("b", { class: "num" }, format(s.value)), " ", h("span", { class: "muted" }, s.label))),
        onmouseleave: hideTip,
      }),
    ),
  );
  const pct = marker !== undefined && sum ? (marker / sum) * 100 : null;
  return h(
    "div",
    { class: "res-stack-bar", role: "img", "aria-label": `${label}: ${segs.map((s) => `${s.label} ${format(s.value)}`).join(", ")}${marker ? `; ${markerLabel || marker}` : ""}` },
    h("div", { class: "res-stack-bar__wrap" }, bar, pct !== null ? h("span", { class: "res-stack-bar__marker", style: { left: `${pct}%` } }, markerLabel ? h("span", { class: "res-stack-bar__marker-label" }, markerLabel) : null) : null),
    legend
      ? h(
          "div",
          { class: "legend res-legend" },
          segs.map((s) => h("span", { class: "legend__item" }, h("span", { class: ["res-swatch", s.style === "outline" && "res-swatch--outline", s.style === "light" && "res-swatch--light"], style: { "--sw": s.color } }), s.label, h("span", { class: "res-legend__count num" }, format(s.value)))),
        )
      : null,
  );
}

/**
 * Ordinal distribution bar (e.g. age bands): one hue, light→dark by band order, with labels.
 * bands: [{label, value}] (percentages)
 */
export function bandBar(bands, { label = "Distribution", ramp } = {}) {
  const n = bands.length;
  const colors = ramp || bands.map((_, i) => `var(--seq-${Math.min(6, Math.round(1 + (i * 5) / Math.max(1, n - 1)))})`);
  return h(
    "div",
    { class: "res-band", role: "img", "aria-label": `${label}: ${bands.map((b) => `${b.label} ${b.value ?? "–"}%`).join(", ")}` },
    h(
      "div",
      { class: "res-band__track" },
      bands.map((b, i) =>
        h("span", {
          class: "res-band__seg",
          style: { flex: `${b.value || 0} 0 0`, background: colors[i] },
          onmousemove: (e) => showTip(e, h("div", null, h("b", { class: "num" }, b.value === null || b.value === undefined ? "–" : `${b.value.toFixed(1)}%`), " ", h("span", { class: "muted" }, b.label))),
          onmouseleave: hideTip,
        }),
      ),
    ),
    h(
      "div",
      { class: "res-band__labels" },
      bands.map((b, i) => h("span", { class: "res-band__label" }, h("span", { class: "res-swatch", style: { "--sw": colors[i] } }), b.label, h("b", { class: "num" }, b.value === null || b.value === undefined ? "–" : `${Math.round(b.value)}%`))),
    ),
  );
}
