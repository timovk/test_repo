/**
 * #/polling — FICTIONAL pollsters, SIMULATED poll numbers.  Weighted polling averages with trend
 * lines and uncertainty bands, the per-poll weight table (transparency), house effects, the
 * generic House ballot and sub-national (province / district / Senate / governor) averages, the
 * poll database with filters, and a form to add a manual fictional poll (POST).
 * Averages, weights and house effects come from the API; the UI only formats them.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { fmtInt, fmt1, fmt2 } from "../format.js";
import { card, currentElectionId, electionPicker, pageHeader } from "./_shared.js";
import { dataTable, barCell } from "../components/table.js";
import { provBadge } from "../components/badges.js";
import {
  PROVINCES, callout, errorBox, field, filterRow, fmtDate, fmtDateShort, humanize, kpi, labelled, partyInfo, pchip,
  pool, provinceName, refresh, segmented, selectBox, sortByPartyOrder, tabBar, toast, downloadLink, statusBadge,
} from "../components/ana-ui.js";
import { divCell, hbars, multiLine, rangeChart, tipBox, tipRow } from "../components/ana-charts.js";

const TYPE_LABELS = {
  national_president: "President · national",
  generic_house: "Generic House ballot",
  province_president: "President · by province",
  governor: "Governor",
  senate: "Senate",
  house_district: "House district",
  favorability: "Favourability",
};
const TYPE_ORDER = ["national_president", "generic_house", "province_president", "governor", "senate", "house_district", "favorability"];
const NATIONAL = new Set(["national_president", "generic_house"]);
const Z90 = 1.645;

export async function render(el, _params, ctx) {
  const id = ctx.electionId || currentElectionId();
  const st = {
    tab: ctx.query.tab || "averages",
    type: ctx.query.type || null,
    geo: ctx.query.geo || null,
    asOf: ctx.query.as_of || "",
    base: null,
    districts: null,
    avgCache: new Map(),
    hidden: new Set(),
    db: { type: "", geo: "", pollster: "" },
  };
  const bodyHost = h("div");
  mount(
    el,
    pageHeader({ eyebrow: "Analysis · Polling", title: "Polling", categories: ["SIMULATED", "FICTIONAL"], meta: [electionPicker()] }),
    callout(
      "sim",
      h("strong", null, "Fictional pollsters, simulated numbers. "),
      "Polls are generated from the simulator's pre-election expectation of a fictional election and aggregated with recency, sample-size, pollster-rating, population and method weights. Pollster names are invented; the numbers are not real Dutch polls.",
    ),
    h("div", { style: { height: "16px" } }),
    bodyHost,
  );
  if (!id) return;

  const geoLabel = (type, geo) => {
    if (!geo || geo === "NL") return "Netherlands";
    if (/^[A-Z]{2}-\d+$/.test(geo) && type === "senate") return `${provinceName(geo.slice(0, 2))} · seat ${geo.split("-")[1]}`;
    if (/^[A-Z]{2}-\d{2}$/.test(geo)) return `${geo} · ${st.districts?.[geo]?.name || provinceName(geo.slice(0, 2))}`;
    if (PROVINCES.includes(geo)) return provinceName(geo);
    return geo;
  };

  async function loadBase() {
    const [base, districts] = await Promise.all([api.get(`/api/polls/${id}?limit=2000`), api.get("/api/districts", { cache: true }).catch(() => null)]);
    st.base = base;
    if (districts) st.districts = Object.fromEntries(districts.districts.map((d) => [d.code, d]));
    const types = TYPE_ORDER.filter((t) => base.groups.some((g) => g.poll_type === t));
    if (!st.type || !types.includes(st.type)) st.type = types[0] || "national_president";
    return base;
  }

  function renderBody() {
    const base = st.base;
    const content = h("div");
    const tabs = [
      { key: "averages", label: "Polling averages" },
      { key: "polls", label: "Poll database", badge: fmtInt(base.total) },
      { key: "pollsters", label: "Pollsters & house effects", badge: base.pollsters.length },
      { key: "add", label: "Add a poll" },
    ];
    if (!tabs.some((t) => t.key === st.tab)) st.tab = "averages";
    const show = (k) => {
      st.tab = k;
      setQuery({ tab: k === "averages" ? null : k });
      mount(content, k === "averages" ? averagesTab() : k === "polls" ? pollsTab() : k === "pollsters" ? pollstersTab() : addTab());
    };
    const bar = tabBar(tabs, st.tab, show, { label: "Polling sections" });
    show(st.tab);
    return h("div", null, bar, content);
  }

  /* -------------------------------------------------------------- averages */
  function avgUrl(type, geo) {
    const q = new URLSearchParams({ type, geo: geo || "NL" });
    if (st.asOf) q.set("as_of", st.asOf);
    return `/api/polls/${id}/average?${q}`;
  }
  function getAvg(type, geo) {
    const url = avgUrl(type, geo);
    if (!st.avgCache.has(url)) {
      const p = api.get(url);
      st.avgCache.set(url, p);
      p.catch(() => st.avgCache.delete(url));
    }
    return st.avgCache.get(url);
  }

  function averagesTab() {
    const base = st.base;
    const types = TYPE_ORDER.filter((t) => base.groups.some((g) => g.poll_type === t) && t !== "favorability");
    const groups = base.groups.filter((g) => g.poll_type === st.type);
    const national = NATIONAL.has(st.type);
    if (national) st.geo = "NL";
    else if (!st.geo || !groups.some((g) => g.geo_code === st.geo)) st.geo = [...groups].sort((a, b) => b.polls - a.polls)[0]?.geo_code || null;

    const detailHost = h("div");
    const overviewHost = h("div");
    const election = base.election;
    const maxDate = election?.election_date || "";
    const asOfInput = h("input", { class: "input", type: "date", value: st.asOf, max: maxDate, "aria-label": "Average as of date", onchange: (e) => { st.asOf = e.target.value; setQuery({ as_of: st.asOf || null }); redraw(); } });
    const geoSelect = national
      ? null
      : selectBox(
          [...groups].sort((a, b) => a.geo_code.localeCompare(b.geo_code)).map((g) => ({ value: g.geo_code, label: `${geoLabel(st.type, g.geo_code)} (${g.polls})` })),
          st.geo,
          (v) => { st.geo = v; setQuery({ geo: v }); drawDetail(); },
          { "aria-label": "Geography" },
        );
    const filters = filterRow(
      labelled("Poll type", segmented(types.map((t) => ({ value: t, label: TYPE_LABELS[t] || humanize(t) })), st.type, (v) => { st.type = v; st.geo = null; setQuery({ type: v, geo: null }); mount(host, averagesTab()); }, { label: "Poll type" })),
      geoSelect ? labelled("Geography", geoSelect) : null,
      labelled("As of", asOfInput),
      st.asOf ? h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: () => { st.asOf = ""; setQuery({ as_of: null }); mount(host, averagesTab()); } }, "Reset date") : null,
    );
    function drawDetail() {
      if (!st.geo) {
        mount(detailHost, h("div", { class: "state" }, "No polls of this type."));
        return;
      }
      refresh(detailHost, getAvg(st.type, st.geo), (d) => averagePanel(d), { onError: (e) => errorBox(e, e.status === 404 ? "Too few polls for an average" : undefined) });
    }
    function redraw() {
      drawDetail();
      if (!national) drawOverview();
    }
    function drawOverview() {
      const gs = [...groups].sort((a, b) => a.geo_code.localeCompare(b.geo_code));
      mount(overviewHost, card(`All ${TYPE_LABELS[st.type] || st.type} averages`, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "40%", height: "14px" } })), { flush: true }));
      pool(gs, 4, (g) => getAvg(st.type, g.geo_code)).then((res) => {
        const keys = sortByPartyOrder([...new Set(res.flatMap((r) => (r?.average?.keys || [])))]);
        const rows = gs.map((g, i) => {
          const a = res[i]?.average;
          const ranked = a ? Object.entries(a.mean).sort((x, y) => y[1] - x[1]) : [];
          return { geo: g.geo_code, name: geoLabel(st.type, g.geo_code), polls: g.polls, latest: g.latest_end_date, lead: ranked[0]?.[0] || null, top: ranked.slice(0, 2), mean: a?.mean || null, error: res[i]?.error };
        });
        const table = dataTable(
          [
            { key: "name", label: "Geography", format: (v, r) => h("button", { class: "ana-linkbtn", type: "button", onclick: () => { st.geo = r.geo; if (geoSelect) geoSelect.value = r.geo; setQuery({ geo: r.geo }); drawDetail(); detailHost.scrollIntoView({ behavior: "smooth", block: "start" }); } }, v) },
            { key: "polls", label: "Polls", align: "r" },
            { key: "latest", label: "Latest", format: (v) => fmtDate(v) },
            { key: "lead", label: "Highest average", format: (v, r) => (v ? h("span", { class: "ana-inl" }, pchip(v), h("span", { class: "muted num" }, r.top.map(([k, m]) => `${k} ${fmt1(m)}`).join(" · "))) : h("span", { class: "muted" }, r.error ? "too few polls" : "–")) },
            ...keys.map((k) => ({ key: `m_${k}`, label: h("span", { class: "ana-party-th" }, pchip(k)), align: "r", value: (r) => r.mean?.[k] ?? null, format: (v, r) => (v == null ? h("span", { class: "muted" }, "–") : h("span", { class: r.lead === k ? "ana-num-strong" : "" }, fmt1(v))) })),
          ],
          rows,
          { sortKey: "name", sortDir: "asc", maxHeight: 460 },
        );
        mount(overviewHost, card(`All ${TYPE_LABELS[st.type] || st.type} averages (${rows.length})`, table, { flush: true, categories: ["SIMULATED"], foot: "Weighted average of each group's polls (percent of respondents). Select a geography for its trend, weights and house effects." }));
      });
    }
    const host = h("div", { class: "ana-stack-v" }, filters, national ? null : overviewHost, detailHost);
    redraw();
    return host;
  }

  function averagePanel(d) {
    const a = d.average;
    const keys = sortByPartyOrder(a.keys);
    const color = (k) => partyInfo(k, d.colors?.[k]).color;
    const byMean = [...keys].sort((x, y) => (a.mean[y] ?? 0) - (a.mean[x] ?? 0));
    const maxUpper = Math.max(...keys.map((k) => a.upper?.[k] ?? a.mean[k] ?? 0), 10);
    const standings = rangeChart({
      rows: byMean.map((k) => ({
        key: k,
        label: k,
        color: color(k),
        q: { p05: a.lower?.[k], p95: a.upper?.[k], median: a.mean[k], mean: a.mean[k] },
        aria: `${k}: ${fmt1(a.mean[k])}%, 90% interval ${fmt1(a.lower?.[k])} to ${fmt1(a.upper?.[k])}`,
        tip: () => tipBox(partyInfo(k).name, [tipRow(color(k), "Average", `${fmt1(a.mean[k])}%`), tipRow(null, `${Math.round((a.interval_level || 0.9) * 100)}% interval`, `${fmt1(a.lower?.[k])}–${fmt1(a.upper?.[k])}%`), tipRow(null, "Standard error", `${fmt2(a.se?.[k])} pp`), tipRow(null, "Total SE (incl. house/industry)", `${fmt2(a.total_se?.[k])} pp`), tipRow(null, "Trend (latest)", `${fmt1(a.trend_latest?.[k])}%`)]),
      })),
      domain: [0, Math.ceil(maxUpper / 5) * 5 + 5],
      format: (v) => (v == null ? "–" : `${Math.round(v)}`),
      unit: "%",
      ariaLabel: "Polling average per party with 90% interval",
      rowH: 28,
      labelW: 70,
    });
    const standingsTable = dataTable(
      [
        { key: "k", label: "Party", format: (v) => pchip(v) },
        { key: "mean", label: "Average", align: "r", format: (v) => h("b", null, `${fmt1(v)}%`) },
        { key: "int", label: "90% interval", align: "r", sort: false, value: (r) => `${fmt1(r.lower)}–${fmt1(r.upper)}` },
        { key: "trend", label: "Trend", align: "r", format: (v) => `${fmt1(v)}%` },
        { key: "total_se", label: "Total SE", align: "r", format: (v) => fmt2(v) },
      ],
      byMean.map((k) => ({ k, mean: a.mean[k], lower: a.lower?.[k], upper: a.upper?.[k], trend: a.trend_latest?.[k], total_se: a.total_se?.[k] })),
      { sortKey: "mean" },
    );

    // Trend with 90% bands (±1.645 × trend SE) and the individual polls of this group.
    const dates = (a.trend?.dates || []).map((s) => new Date(`${s}T12:00:00`));
    const polls = (st.base.polls || []).filter((p) => p.poll_type === d.poll_type && p.geo_code === d.geo_code && (!st.asOf || p.end_date <= d.as_of));
    const toggles = h(
      "div",
      { class: "ana-toggles", role: "group", "aria-label": "Show or hide parties" },
      keys.map((k) =>
        h(
          "button",
          { type: "button", class: ["ana-toggle", st.hidden.has(k) && "is-off"], "aria-pressed": st.hidden.has(k) ? "false" : "true", style: { "--party": color(k) }, onclick: () => { st.hidden.has(k) ? st.hidden.delete(k) : st.hidden.add(k); mount(trendHost, trendCard()); } },
          h("span", { class: "chip__swatch" }),
          k,
        ),
      ),
    );
    const trendHost = h("div");
    const trendCard = () =>
      card(
        "Trend",
        h(
          "div",
          { class: "ana-stack-v" },
          toggles,
          dates.length || polls.length
            ? multiLine({ series: rebuildSeries(), scatter: rebuildScatter(), xType: "date", xFormat: (x) => fmtDateShort(x), yFormat: (v) => `${Math.round(v)}%`, tipFormat: (v) => `${fmt1(v)}%`, height: 300, ariaLabel: `Polling trend, ${TYPE_LABELS[d.poll_type]} ${geoLabel(d.poll_type, d.geo_code)}`, bandLabel: "90% band (±1.645 × trend SE)", showLegend: false })
            : h("div", { class: "state" }, "No trend available"),
        ),
        { categories: ["SIMULATED"], foot: `Lines: smoothed trend; shaded: 90% band (±1.645 × trend standard error); dots: individual polls (${polls.length}). Toggle parties above; use ← → on the focused chart.` },
      );
    function rebuildSeries() {
      return keys
        .filter((k) => a.trend?.series?.[k] && !st.hidden.has(k))
        .map((k) => ({ key: k, label: k, color: color(k), points: dates.map((dt, i) => { const y = a.trend.series[k][i]; const se = a.trend.se?.[k]?.[i]; return { x: dt, y, lo: se != null && y != null ? y - Z90 * se : undefined, hi: se != null && y != null ? y + Z90 * se : undefined }; }) }));
    }
    function rebuildScatter() {
      return polls.flatMap((p) => keys.filter((k) => p.results?.[k] != null && !st.hidden.has(k)).map((k) => ({ x: new Date(`${p.end_date}T12:00:00`), y: p.results[k], color: color(k) })));
    }
    mount(trendHost, trendCard());

    // House effects (pollster × party, estimated pp)
    const he = a.house_effects || [];
    const pollsters = [...new Set(he.map((x) => x.pollster))];
    const heMax = Math.max(1, ...he.map((x) => Math.abs(x.estimated_pp || 0)));
    const heRows = pollsters.map((p) => {
      const row = { pollster: p, n: he.find((x) => x.pollster === p)?.n_polls };
      for (const x of he.filter((y) => y.pollster === p)) row[x.key] = x;
      return row;
    });
    const heTable = he.length
      ? dataTable(
          [
            { key: "pollster", label: "Pollster" },
            { key: "n", label: "Polls", align: "r" },
            ...keys.map((k) => ({ key: k, label: h("span", { class: "ana-party-th" }, pchip(k)), align: "r", value: (r) => r[k]?.estimated_pp ?? null, format: (v, r) => h("span", { title: `prior ${fmt1(r[k]?.prior_pp)} pp · estimated ${fmt2(v)} pp` }, divCell(v, heMax, (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}`)) })),
          ],
          heRows,
          { sortKey: "n" },
        )
      : h("div", { class: "state" }, "No house effects estimated (too few polls per pollster)");

    // Weights transparency
    const w = a.weights || [];
    const wMax = Math.max(...w.map((x) => x.weight || 0), 1e-9);
    const f3 = (v) => (v == null ? "–" : Number(v).toFixed(3));
    const wTable = dataTable(
      [
        { key: "poll_id", label: "Poll", format: (v) => h("span", { class: "mono" }, `#${v}`) },
        { key: "pollster", label: "Pollster" },
        { key: "end_date", label: "End", format: (v) => fmtDate(v) },
        { key: "age_days", label: "Age (d)", align: "r" },
        { key: "sample_size", label: "Sample", align: "r", format: (v) => fmtInt(v) },
        { key: "population", label: "Pop." },
        { key: "method", label: "Method" },
        { key: "w_recency", label: "Recency", align: "r", format: f3 },
        { key: "w_sample", label: "Sample w", align: "r", format: f3 },
        { key: "w_rating", label: "Rating w", align: "r", format: f3 },
        { key: "w_population", label: "Pop. w", align: "r", format: f3 },
        { key: "w_method", label: "Method w", align: "r", format: f3 },
        { key: "w_volume", label: "Volume w", align: "r", format: f3 },
        { key: "weight", label: "Final weight", format: (v) => barCell(v / wMax, "var(--seq-4)", `${(v * 100).toFixed(1)}%`) },
      ],
      w,
      { sortKey: "weight", maxHeight: 420 },
    );

    const acc = a.accuracy;
    const accBlock = acc
      ? card(
          "Accuracy against the result",
          h(
            "div",
            { class: "ana-stack-v" },
            h("div", { class: "ana-kpis" }, kpi("Mean absolute error", `${fmt2(acc.mean_abs_error_pp)} pp`, "final average vs. reported result")),
            hbars({ rows: keys.filter((k) => acc.error_pp?.[k] != null).map((k) => ({ key: k, label: k, labelNode: pchip(k), value: acc.error_pp[k], color: color(k), tip: tipBox(partyInfo(k).name, [tipRow(color(k), "Average", `${fmt1(a.mean[k])}%`), tipRow(null, "Result", `${fmt1(acc.actual_pct?.[k])}%`), tipRow(null, "Error", `${fmt2(acc.error_pp[k])} pp`)]) })), diverging: true, format: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)} pp`, labelWidth: 70, ariaLabel: "Polling error per party" }),
          ),
          { categories: ["SIMULATED"], foot: "Positive = the average overstated the party. Only available once the election is reported." },
        )
      : null;

    return h(
      "div",
      { class: "ana-stack-v" },
      h(
        "div",
        { class: "ana-row ana-row--between" },
        h("div", null, h("h2", { class: "ana-h2" }, `${TYPE_LABELS[d.poll_type] || d.poll_type} · ${geoLabel(d.poll_type, d.geo_code)}`), h("div", { class: "ana-note" }, `Weighted average as of ${fmtDate(d.as_of)} · latest poll ${fmtDate(a.latest_poll_date)} · undecided ${fmt1(a.undecided_mean)}% (${a.undecided_mode})`)),
        h("div", { class: "ana-row" }, provBadge("SIMULATED"), downloadLink(`/api/export/${id}/polling_averages.csv${st.asOf ? `?as_of=${st.asOf}` : ""}`, "Averages CSV")),
      ),
      h(
        "div",
        { class: "ana-kpis" },
        kpi("Polls in average", fmtInt(a.n_polls), `effective ${fmt1(a.effective_polls)} polls`),
        kpi("Effective sample", fmtInt(a.effective_n), "respondents after weighting"),
        kpi("Highest average", h("span", { class: "ana-inl" }, pchip(byMean[0]), `${fmt1(a.mean[byMean[0]])}%`), byMean[1] ? `next: ${byMean[1]} ${fmt1(a.mean[byMean[1]])}%` : null, { accent: color(byMean[0]) }),
        kpi("Interval", `${Math.round((a.interval_level || 0.9) * 100)}%`, "credible interval shown"),
      ),
      h("div", { class: "ana-split" }, trendHost, card("Current average", h("div", { class: "ana-stack-v" }, standings, standingsTable), { foot: "Dot = weighted average; bar = 90% interval (sampling + house + industry error)." })),
      card("House effects (estimated, pp)", heTable, { flush: true, foot: "How far each pollster's results sit from the average for a party (+ = overstates). Hover a cell for the prior." }),
      accBlock || callout("info", h("strong", null, "Accuracy hidden until the election is reported. "), "Poll accuracy compares the final average with the result, which is not revealed yet."),
      card(`Poll weights (${w.length})`, wTable, { flush: true, foot: "Every poll's weight factors: recency decay, sample size, pollster rating, population (LV/RV/A), method and pollster volume. Final weight is normalised to 100%." }),
    );
  }

  /* -------------------------------------------------------------- poll database */
  function pollsTab() {
    const base = st.base;
    const f = st.db;
    const tableHost = h("div");
    const types = TYPE_ORDER.filter((t) => base.groups.some((g) => g.poll_type === t));
    const geos = () => [...new Set(base.groups.filter((g) => !f.type || g.poll_type === f.type).map((g) => g.geo_code))].sort();
    const geoSel = selectBox([{ value: "", label: "All geographies" }, ...geos().map((g) => ({ value: g, label: geoLabel(f.type, g) }))], f.geo, (v) => { f.geo = v; load(); }, { "aria-label": "Geography" });
    const filters = filterRow(
      labelled("Type", selectBox([{ value: "", label: "All types" }, ...types.map((t) => ({ value: t, label: TYPE_LABELS[t] || humanize(t) }))], f.type, (v) => {
        f.type = v;
        f.geo = "";
        mount(geoSel, ...[{ value: "", label: "All geographies" }, ...geos().map((g) => ({ value: g, label: geoLabel(f.type, g) }))].map((o) => h("option", { value: o.value }, o.label)));
        load();
      }, { "aria-label": "Poll type" })),
      labelled("Geography", geoSel),
      labelled("Pollster", selectBox([{ value: "", label: "All pollsters" }, ...base.pollsters.map((p) => ({ value: p.name, label: p.name }))], f.pollster, (v) => { f.pollster = v; load(); }, { "aria-label": "Pollster" })),
      h("div", { class: "ana-grow" }),
      downloadLink(`/api/export/${id}/polls.csv`, "All polls CSV"),
    );
    function load() {
      const q = new URLSearchParams({ limit: "500" });
      if (f.type) q.set("type", f.type);
      if (f.geo) q.set("geo", f.geo);
      if (f.pollster) q.set("pollster", f.pollster);
      refresh(tableHost, api.get(`/api/polls/${id}?${q}`), (d) => {
        const keys = sortByPartyOrder([...new Set(d.polls.flatMap((p) => Object.keys(p.results || {})))]);
        const rating = Object.fromEntries(d.pollsters.map((p) => [p.name, p.rating_label]));
        const rows = d.polls.map((p) => ({ ...p, lead: Object.entries(p.results || {}).sort((a, b) => b[1] - a[1])[0]?.[0] }));
        return h(
          "div",
          null,
          h("div", { class: "ana-table-tools" }, h("span", { class: "ana-count" }, `Showing ${fmtInt(rows.length)} of ${fmtInt(d.total)} polls`), h("div", { class: "ana-row" }, provBadge("FICTIONAL", "Fictional pollsters"), provBadge("SIMULATED"))),
          dataTable(
            [
              { key: "end_date", label: "End date", format: (v) => fmtDate(v) },
              { key: "pollster", label: "Pollster", format: (v) => h("span", null, v, rating[v] ? h("span", { class: "ana-rating" }, rating[v]) : null) },
              { key: "poll_type", label: "Type", format: (v) => TYPE_LABELS[v] || humanize(v) },
              { key: "geo_code", label: "Geo", format: (v, r) => h("span", { title: geoLabel(r.poll_type, v) }, v) },
              { key: "start_date", label: "Fieldwork", format: (v, r) => `${fmtDateShort(v)}–${fmtDateShort(r.end_date)}` },
              { key: "sample_size", label: "Sample", align: "r", format: (v) => fmtInt(v) },
              { key: "population", label: "Pop." },
              { key: "method", label: "Method" },
              { key: "margin_of_error", label: "MoE", align: "r", format: (v) => (v == null ? "–" : `±${fmt1(v)}`) },
              { key: "undecided_pct", label: "Undec.", align: "r", format: (v) => (v == null ? "–" : `${fmt1(v)}%`) },
              ...keys.map((k) => ({ key: `r_${k}`, label: h("span", { class: "ana-party-th" }, pchip(k)), align: "r", value: (r) => r.results?.[k] ?? null, format: (v, r) => (v == null ? h("span", { class: "muted" }, "–") : h("span", { class: r.lead === k ? "ana-num-strong" : "" }, fmt1(v))) })),
            ],
            rows,
            { sortKey: "end_date", maxHeight: 640 },
          ),
        );
      });
    }
    load();
    return h("div", null, filters, card(null, tableHost, { flush: true }), h("p", { class: "ana-note" }, "Results are percent of all respondents (undecided excluded from the party columns). Bold = highest value in that poll. MoE = reported margin of error (±pp)."));
  }

  /* -------------------------------------------------------------- pollsters */
  function pollstersTab() {
    const base = st.base;
    const counts = {};
    for (const p of base.polls) counts[p.pollster] = (counts[p.pollster] || 0) + 1;
    const avgType = base.groups.some((g) => g.poll_type === "national_president" && g.geo_code === "NL") ? "national_president" : "generic_house";
    const heHost = h("div");
    refresh(heHost, getAvg(avgType, "NL"), (d) => {
      const he = d.average.house_effects || [];
      const keys = sortByPartyOrder([...new Set(he.map((x) => x.key))]);
      const mx = Math.max(1, ...he.map((x) => Math.abs(x.estimated_pp || 0)));
      const rows = [...new Set(he.map((x) => x.pollster))].map((p) => {
        const r = { pollster: p };
        for (const x of he.filter((y) => y.pollster === p)) r[x.key] = x;
        return r;
      });
      return dataTable(
        [{ key: "pollster", label: "Pollster" }, ...keys.map((k) => ({ key: k, label: h("span", { class: "ana-party-th" }, pchip(k)), align: "r", value: (r) => r[k]?.estimated_pp ?? null, format: (v) => divCell(v, mx, (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}`) }))],
        rows,
        { sortKey: "pollster", sortDir: "asc" },
      );
    }, { onError: (e) => errorBox(e) });
    const table = dataTable(
      [
        { key: "name", label: "Pollster", format: (v) => h("b", null, v) },
        { key: "rating_label", label: "Grade", format: (v) => h("span", { class: "ana-rating ana-rating--lg" }, v || "–") },
        { key: "rating", label: "Rating", align: "r", format: (v) => fmt2(v) },
        { key: "method", label: "Method" },
        { key: "polls", label: "Polls", align: "r", value: (r) => counts[r.name] || 0 },
        { key: "house_effects", label: "Configured lean (prior, pp)", sort: false, format: (v) => (v && Object.keys(v).length ? h("span", { class: "ana-row", style: { gap: "10px" } }, Object.entries(v).map(([k, x]) => h("span", { class: "ana-inl" }, pchip(k), h("span", { class: "num" }, `${x > 0 ? "+" : "−"}${Math.abs(x).toFixed(1)}`)))) : h("span", { class: "muted" }, "none")) },
      ],
      base.pollsters,
      { sortKey: "rating" },
    );
    return h(
      "div",
      { class: "ana-stack-v" },
      card("Pollsters", table, { flush: true, categories: ["FICTIONAL"], foot: "All pollsters are invented. Rating feeds the average's weights; the configured lean is the simulator's built-in house effect (FICTIONAL)." }),
      card(`Estimated house effects · ${TYPE_LABELS[avgType]}`, heHost, { flush: true, categories: ["SIMULATED"], foot: "Estimated from each pollster's deviation from the national average (pp; + = overstates the party)." }),
    );
  }

  /* -------------------------------------------------------------- add poll */
  function addTab() {
    const base = st.base;
    const election = base.election;
    const reported = election?.reported || base.results_source === "final";
    const live = election?.live || base.results_source === "live";
    const parties = sortByPartyOrder((getParties() || []).map((p) => p.code));
    const types = TYPE_ORDER.filter((t) => t !== "favorability");
    const form = { poll_type: "national_president", geo_code: "NL" };
    const msgHost = h("div", { "aria-live": "polite" });
    const pollster = h("input", { class: "input", required: true, list: "ana-pollster-list", placeholder: "e.g. Peilpunt Test Research", maxlength: 80 });
    const datalist = h("datalist", { id: "ana-pollster-list" }, base.pollsters.map((p) => h("option", { value: p.name })));
    const geoSel = h("select", { class: "select", "aria-label": "Geography" });
    const geoOptions = () => {
      const t = form.poll_type;
      let opts;
      if (NATIONAL.has(t)) opts = [{ value: "NL", label: "Netherlands" }];
      else if (t === "house_district") opts = Object.values(st.districts || {}).map((d) => ({ value: d.code, label: `${d.code} · ${d.name}` }));
      else if (t === "senate") opts = PROVINCES.flatMap((p) => [1, 2].map((n) => ({ value: `${p}-${n}`, label: `${provinceName(p)} · seat ${n}` })));
      else opts = PROVINCES.map((p) => ({ value: p, label: provinceName(p) }));
      mount(geoSel, opts.map((o) => h("option", { value: o.value }, o.label)));
      form.geo_code = opts[0]?.value;
    };
    geoSel.onchange = (e) => (form.geo_code = e.target.value);
    geoOptions();
    const dayBefore = election?.election_date || "";
    const start = h("input", { class: "input", type: "date", required: true, max: dayBefore });
    const end = h("input", { class: "input", type: "date", required: true, max: dayBefore });
    const sample = h("input", { class: "input", type: "number", min: 50, max: 100000, step: 1, value: 1200, required: true });
    const population = selectBox([{ value: "LV", label: "Likely voters (LV)" }, { value: "RV", label: "Registered voters (RV)" }, { value: "A", label: "All adults (A)" }], "LV", () => {});
    const method = selectBox(["online", "probability panel", "online opt-in", "IVR", "mixed mode", "telephone"], "online", () => {});
    const undecided = h("input", { class: "input", type: "number", min: 0, max: 60, step: 0.1, placeholder: "optional" });
    const moe = h("input", { class: "input", type: "number", min: 0, max: 20, step: 0.1, placeholder: "optional" });
    const notes = h("input", { class: "input", maxlength: 200, placeholder: "optional" });
    const resInputs = Object.fromEntries(parties.map((k) => [k, h("input", { class: "input input--sm", type: "number", min: 0, max: 100, step: 0.1, placeholder: "–", "aria-label": `${k} percent` })]));
    const sumOut = h("span", { class: "num" }, "0.0%");
    const updSum = () => {
      const s = Object.values(resInputs).reduce((acc, i) => acc + (Number(i.value) || 0), 0) + (Number(undecided.value) || 0);
      sumOut.textContent = `${s.toFixed(1)}%`;
      sumOut.classList.toggle("is-over", s > 100.05);
    };
    Object.values(resInputs).forEach((i) => i.addEventListener("input", updSum));
    undecided.addEventListener("input", updSum);
    const submit = h("button", { class: "btn btn--primary", type: "submit", disabled: reported }, "Add poll");
    const formEl = h(
      "form",
      {
        class: "ana-stack-v",
        novalidate: false,
        onsubmit: async (e) => {
          e.preventDefault();
          const results = {};
          for (const [k, i] of Object.entries(resInputs)) if (i.value !== "") results[k] = Number(i.value);
          if (!Object.keys(results).length) {
            mount(msgHost, callout("warn", "Enter at least one party result."));
            return;
          }
          const body = {
            pollster: pollster.value.trim(),
            poll_type: form.poll_type,
            geo_code: form.geo_code,
            start_date: start.value,
            end_date: end.value,
            sample_size: Number(sample.value),
            results,
            population: population.value,
            method: method.value,
          };
          if (undecided.value !== "") body.undecided_pct = Number(undecided.value);
          if (moe.value !== "") body.margin_of_error = Number(moe.value);
          if (notes.value.trim()) body.notes = notes.value.trim();
          submit.disabled = true;
          try {
            const res = await api.post(`/api/polls/${id}`, body);
            mount(msgHost, callout("ok", h("strong", null, `Poll #${res.id} added. `), `${res.pollster} · ${TYPE_LABELS[res.poll_type] || res.poll_type} · ${res.geo_code} · n=${fmtInt(res.sample_size)}. Averages now include it.`));
            toast(`Poll #${res.id} added`, "success");
            st.avgCache.clear();
            await loadBase();
          } catch (err) {
            mount(msgHost, callout("warn", h("strong", null, err.status === 409 ? "Not allowed: " : "Rejected: "), err.message));
          } finally {
            submit.disabled = reported;
          }
        },
      },
      reported
        ? callout("warn", h("strong", null, "This election is already reported. "), "Manual polls can only be added to elections whose results are not revealed yet (the API answers 409). Select the 2028 election to try it.")
        : live
          ? callout("warn", "The election night is running; the API may refuse new polls.")
          : callout("sim", "The poll is stored as a FICTIONAL poll by a fictional pollster and immediately enters the SIMULATED averages. Pollster names that resemble real polling organisations are rejected."),
      h(
        "div",
        { class: "ana-form-grid" },
        field("Pollster", pollster, "Fictional name"),
        datalist,
        field("Poll type", selectBox(types.map((t) => ({ value: t, label: TYPE_LABELS[t] })), form.poll_type, (v) => { form.poll_type = v; geoOptions(); }, {})),
        field("Geography", geoSel),
        field("Fieldwork start", start),
        field("Fieldwork end", end, dayBefore ? `No later than ${fmtDate(dayBefore)}` : null),
        field("Sample size", sample),
        field("Population", population),
        field("Method", method),
        field("Undecided %", undecided),
        field("Margin of error (±pp)", moe),
        field("Notes", notes),
      ),
      h("div", { class: "ana-field__label" }, "Results (% of respondents)"),
      h("div", { class: "ana-results-grid" }, parties.map((k) => h("label", { class: "ana-result" }, pchip(k), resInputs[k]))),
      h("div", { class: "ana-row ana-row--between" }, h("span", { class: "ana-note" }, "Total incl. undecided: ", sumOut, " (must not exceed 100%)"), submit),
      msgHost,
    );
    return card("Add a manual poll", formEl, { categories: ["FICTIONAL", "SIMULATED"] });
  }

  function getParties() {
    const colors = st.base?.colors || {};
    return Object.keys(colors).map((code) => ({ code }));
  }

  /* -------------------------------------------------------------- start */
  await refresh(bodyHost, loadBase(), () => renderBody());
  return () => {};
}
