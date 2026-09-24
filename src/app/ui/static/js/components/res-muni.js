/**
 * Municipality result rows (API §2.5, keyed by party) → map shading, legends, tooltips and
 * table columns.  Shared by #/provinces/:code and #/municipalities.  Pure display: every value
 * (leader, margin, swing, outstanding estimate, flip) comes from the API row.
 */
import { h } from "../dom.js";
import { fmtCompact, fmtInt, fmtPct, fmtPP } from "../format.js";
import { partyChip } from "./badges.js";
import { cssVar, divColor, divRamp, flipTag, marginLabel, partyShade, pc, rampLegend, reportingMeter, seqColor, seqRamp, signed, swatchLegend } from "./res-kit.js";

export const METRIC_LABEL = {
  winner: "Leader",
  margin: "Margin",
  swing: "Swing",
  turnout: "Turnout",
  reporting: "Reporting",
  share: "Party share",
  change: "Change",
  density: "Density",
};

/** Metric options for a results source. */
export function muniMetrics(source, { share = true, density = true } = {}) {
  const dens = density ? [{ value: "density", label: "Density", title: "Population density (REAL, CBS)" }] : [];
  if (source === "hidden") return dens.length ? dens : [{ value: "winner", label: "Pre-election" }];
  const all = [
    { value: "winner", label: source === "final" ? "Winner" : "Leader" },
    { value: "margin", label: "Margin" },
    { value: "swing", label: "Swing", title: "Leader's share change vs the previous election of the same race family" },
    { value: "turnout", label: "Turnout" },
    { value: "reporting", label: "Reporting" },
    share ? { value: "share", label: "Party share" } : null,
    { value: "change", label: "Change", title: "Municipalities that changed hands vs the previous election" },
    ...dens,
  ].filter(Boolean);
  if (source === "final") return all.filter((m) => m.value !== "reporting");
  return all.filter((m) => m.value !== "turnout");
}

const counted = (r) => r && r.votes !== null && r.votes !== undefined && r.votes > 0 && r.leader_party;

/** Scale context from the visible rows (ranges only — no results are derived). */
export function scaleContext(rows, colors = {}) {
  const t = rows.map((r) => r.turnout_pct).filter((v) => v !== null && v !== undefined);
  const parties = new Set();
  const maxShare = {};
  for (const r of rows) {
    for (const [p, v] of Object.entries(r.shares || {})) {
      parties.add(p);
      maxShare[p] = Math.max(maxShare[p] || 0, v || 0);
    }
  }
  const swings = rows.map((r) => Math.abs(r.swing_pp ?? 0)).filter((v) => v > 0).sort((a, b) => a - b);
  const p90 = swings.length ? swings[Math.floor(swings.length * 0.9)] : 10;
  return {
    colors,
    turnout: t.length ? [Math.floor(Math.min(...t) / 5) * 5, Math.ceil(Math.max(...t) / 5) * 5] : [50, 90],
    swingCap: Math.max(4, Math.ceil(p90 / 2) * 2),
    maxShare,
    parties: [...parties],
  };
}

/** {fill, state} for a municipality row under a metric. */
export function muniSpec(r, metric, ctx) {
  if (!r) return { none: true };
  const color = r.leader_party ? pc(r.leader_party, r.leader_color || ctx.colors?.[r.leader_party]) : null;
  const partial = r.reporting_pct !== null && r.reporting_pct !== undefined && r.reporting_pct < 100;
  switch (metric) {
    case "density": {
      const d = ctx.density?.[r.code];
      return d ? { fill: seqColor(Math.log10(Math.max(1, d)), 1.7, 3.7) } : {};
    }
    case "margin":
      return counted(r) && r.margin_pp !== null ? { fill: partyShade(color, (r.margin_pp || 0) / 30) } : {};
    case "swing":
      return counted(r) && r.swing_pp !== null && r.swing_pp !== undefined ? { fill: divColor(r.swing_pp, ctx.swingCap) } : {};
    case "turnout":
      return r.turnout_pct !== null && r.turnout_pct !== undefined ? { fill: seqColor(r.turnout_pct, ctx.turnout[0], ctx.turnout[1]) } : {};
    case "reporting":
      return r.reporting_pct !== null && r.reporting_pct !== undefined ? { fill: seqColor(r.reporting_pct, 0, 100) } : {};
    case "share": {
      const v = r.shares?.[ctx.party];
      if (!counted(r) || v === undefined || v === null) return {};
      return { fill: partyShade(pc(ctx.party, ctx.colors?.[ctx.party]), v / Math.max(1, ctx.maxShare[ctx.party] || 1)) };
    }
    case "change":
      if (!counted(r)) return {};
      if (r.flip_status === "flip") return { fill: color, state: partial ? "leading" : "called" };
      if (r.flip_status === "hold") return { fill: cssVar("--uncalled") };
      return { fill: cssVar("--uncalled-soft") };
    default:
      return counted(r) ? { fill: color, state: partial ? "leading" : "called" } : {};
  }
}

export function muniLegend(metric, rows, ctx, source) {
  const leaders = {};
  for (const r of rows) if (counted(r)) leaders[r.leader_party] = (leaders[r.leader_party] || 0) + 1;
  const order = Object.entries(leaders).sort((a, b) => b[1] - a[1]);
  switch (metric) {
    case "density":
      return h("div", { class: "res-legend-stack" }, rampLegend(seqRamp(), ["50", "500", "5,000 /km²"], { title: "Population density · REAL (CBS)" }));
    case "margin":
      return rampLegend([0.1, 0.3, 0.5, 0.7, 0.9].map((t) => partyShade(cssVar("--text-secondary"), t)), ["0", "6", "12", "18", "24", "30+ pp"], { title: "Margin · leader's hue, darker = wider" });
    case "swing": {
      const c = ctx.swingCap;
      return rampLegend(divRamp(), [`−${c} pp`, "0", `+${c} pp`], { title: "Leader's share change vs previous" });
    }
    case "turnout":
      return rampLegend(seqRamp(), [`${ctx.turnout[0]}%`, `${ctx.turnout[1]}%`], { title: "Turnout" });
    case "reporting":
      return rampLegend(seqRamp(), ["0%", "50%", "100%"], { title: "Expected vote counted" });
    case "share": {
      const col = pc(ctx.party, ctx.colors?.[ctx.party]);
      const mx = ctx.maxShare[ctx.party] || 0;
      return rampLegend([0.1, 0.3, 0.5, 0.7, 0.9].map((t) => partyShade(col, t)), ["0%", `${Math.round(mx)}%`], { title: `${ctx.party} vote share` });
    }
    case "change": {
      const flips = {};
      for (const r of rows) if (r.flip_status === "flip" && r.leader_party) flips[r.leader_party] = (flips[r.leader_party] || 0) + 1;
      return swatchLegend(
        [
          ...Object.entries(flips)
            .sort((a, b) => b[1] - a[1])
            .map(([p, n]) => ({ color: pc(p, ctx.colors?.[p]), label: `Flipped to ${p}`, count: n })),
          { color: cssVar("--uncalled"), label: "Held" },
          { color: cssVar("--uncalled-soft"), label: "No previous result / not counted" },
        ],
        { title: "Change vs previous" },
      );
    }
    default:
      if (source === "hidden") return h("span", { class: "muted res-muted-note" }, "Municipal results are hidden until they are reported. Hover a municipality for its population and eligible voters.");
      return h(
        "div",
        { class: "res-legend-stack" },
        swatchLegend(order.map(([p, n]) => ({ color: pc(p, ctx.colors?.[p]), label: p, count: n })), { title: source === "final" ? "Carried" : "Leading" }),
        source === "live" ? swatchLegend([{ color: partyShade(cssVar("--text-secondary"), 0.35), label: "Tint = still counting" }, { color: cssVar("--uncalled"), label: "No votes counted" }]) : null,
      );
  }
}

/**
 * Tooltip for a municipality.  `names`: party → candidate name (optional);
 * `lineVotes`: code → [{name, party, color, votes, pct}] (optional, PRES only).
 */
export function muniTooltip(name, r, ctx, { provinceName, names, lineVotes, population } = {}) {
  const lv = lineVotes?.[r?.code];
  const rows = lv
    ? [...lv].sort((a, b) => (b.votes || 0) - (a.votes || 0)).slice(0, 5)
    : Object.entries(r?.shares || {})
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([p, v]) => ({ party: p, pct: v, name: names?.[p] || null, color: pc(p, ctx.colors?.[p]) }));
  const status = !r || r.votes === null || r.votes === undefined ? "hidden" : r.reporting_pct >= 100 ? "complete" : r.reporting_pct > 0 ? "partial" : "none";
  return h(
    "div",
    { class: "res-tip" },
    h(
      "div",
      { class: "res-tip__head" },
      h("strong", null, name),
      status === "partial" ? reportingMeter(r.reporting_pct, { width: 40 }) : status === "complete" ? h("span", { class: "res-tag" }, "COMPLETE") : null,
    ),
    h("div", { class: "res-tip__sub muted" }, [provinceName, population ? `pop. ${fmtCompact(population)}` : null, ctx.density?.[r?.code] ? `${fmtInt(ctx.density[r.code])}/km²` : null, r?.eligible ? `${fmtCompact(r.eligible)} eligible` : null].filter(Boolean).join(" · ")),
    status === "hidden"
      ? h("div", { class: "muted", style: { fontSize: "12px" } }, "Results hidden until reported.")
      : status === "none"
        ? h("div", { class: "muted", style: { fontSize: "12px" } }, "No votes counted yet.")
        : h(
            "div",
            { class: "res-tip__rows" },
            rows.map((x) =>
              h(
                "div",
                { class: "res-tip__row" },
                h("span", { class: "chip__swatch", style: { "--party": pc(x.party, x.color) } }),
                h("span", { class: "res-tip__name" }, x.name ? `${x.name} (${x.party})` : x.party),
                h("b", { class: "num" }, fmtPct(x.pct)),
                x.votes !== undefined && x.votes !== null ? h("span", { class: "res-tip__votes num" }, fmtInt(x.votes)) : h("span"),
              ),
            ),
          ),
    r && status !== "hidden" && status !== "none"
      ? h(
          "div",
          { class: "res-tip__kv" },
          h("span", null, "Margin ", h("b", null, marginLabel(r.leader_party, r.margin_pp))),
          h("span", null, "Votes ", h("b", null, fmtInt(r.votes))),
          r.turnout_pct !== null && r.turnout_pct !== undefined ? h("span", null, "Turnout ", h("b", null, fmtPct(r.turnout_pct))) : null,
          h("span", null, "Reporting ", h("b", null, fmtPct(r.reporting_pct))),
          r.previous_winner ? h("span", null, "Previous ", h("b", null, r.previous_winner)) : null,
          r.swing_pp !== null && r.swing_pp !== undefined ? h("span", null, "Swing ", h("b", null, `${fmtPP(r.swing_pp)} pp`)) : null,
          r.flip_status ? flipTag(r.flip_status, { prev: r.previous_winner }) : null,
          r.outstanding_est ? h("span", null, "Outstanding ≈ ", h("b", null, fmtInt(r.outstanding_est))) : null,
        )
      : null,
  );
}

/** Table columns for municipality rows. `extra` columns are inserted after the name. */
export function muniColumns(source, ctx, { link, provinceNames, showProvince = false, parties = [] } = {}) {
  const res = source !== "hidden";
  const cols = [
    { key: "name", label: "Municipality", format: (v, r) => h("a", { href: link(r.code), class: "res-link" }, v) },
    showProvince ? { key: "province_code", label: "Prov.", format: (v) => h("span", { class: "res-code", title: provinceNames?.[v] || v }, v) } : null,
    { key: "population", label: "Population", align: "r", format: (v) => fmtInt(v) },
    res ? { key: "votes", label: "Votes", align: "r", format: (v) => fmtInt(v) } : { key: "eligible", label: "Eligible (est.)", align: "r", format: (v) => fmtInt(v) },
    source === "live" ? { key: "reporting_pct", label: "Reporting", format: (v) => reportingMeter(v, { width: 46 }) } : null,
    res ? { key: "leader_party", label: source === "final" ? "Winner" : "Leader", format: (v, r) => (v ? partyChip(v, { color: pc(v, r.leader_color || ctx.colors?.[v]) }) : h("span", { class: "muted" }, "–")) } : null,
    res ? { key: "margin_pp", label: "Margin", align: "r", format: (v) => (v === null || v === undefined ? "–" : marginLabel(null, v)) } : null,
    res ? { key: "swing_pp", label: "Swing", align: "r", format: (v) => signed(v) } : null,
    source === "final" ? { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) } : null,
    source === "live" ? { key: "outstanding_est", label: "Outstanding", align: "r", format: (v) => (v ? fmtInt(v) : v === 0 ? h("span", { class: "muted" }, "0") : "–") } : null,
    ...(res
      ? parties.map((p) => ({
          key: `share_${p}`,
          label: p,
          align: "r",
          value: (r) => r.shares?.[p] ?? null,
          format: (v) => (v === null || v === undefined ? h("span", { class: "muted" }, "–") : fmtPct(v)),
        }))
      : []),
    res ? { key: "previous_winner", label: "Prev.", format: (v) => (v ? partyChip(v, { color: pc(v, ctx.colors?.[v]) }) : h("span", { class: "muted" }, "–")) } : null,
    res ? { key: "flip_status", label: "Flip", format: (v, r) => flipTag(v, { prev: r.previous_winner }) } : null,
  ];
  return cols.filter(Boolean);
}

/** Case-insensitive search across municipality name / code / province. */
export function matches(r, q, provinceNames) {
  if (!q) return true;
  return `${r.name} ${r.code} ${r.province_code} ${provinceNames?.[r.province_code] || ""}`.toLowerCase().includes(q);
}
