/**
 * #/history — reported elections of the fictional system (SIMULATED results).
 *  · Elections: timeline + table (president, EV, popular vote, House / Senate control, turnout,
 *    flips, EC/PV divergence flag); hidden elections are listed without results.
 *  · Compare: lineage-aware swing between two elections (national, province, district,
 *    municipality), flips summary, swing / flip maps and tables.
 *  · Records: closest races, largest landslides, Electoral College / popular-vote divergence.
 *  · Explorer: party support in a municipality or province across elections, House district
 *    voting history.
 *  · Analytics: partisan lean, elasticity, EC efficiency, tipping point, competitiveness,
 *    efficiency gap / wasted votes and the seat–vote relationship of one election.
 * All figures are read from /api/history/* and /api/analytics/*; the UI computes nothing.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { subscribe } from "../store.js";
import { fmtInt, fmt1, fmt2, fmtPct, fmtPP, fmtShare } from "../format.js";
import { card, currentElectionId, links, pageHeader } from "./_shared.js";
import { dataTable } from "../components/table.js";
import { electionMap } from "../components/map.js";
import { diverging } from "../components/colorscale.js";
import { icon } from "../components/icons.js";
import { provBadge } from "../components/badges.js";
import {
  PROVINCES, callout, errorBox, filterRow, fmtDate, humanize, kpi, kv, labelled, partyInfo, partyOrder, pchip,
  provinceName, refresh, segmented, selectBox, sortByPartyOrder, sourcePill, statusBadge, tabBar,
} from "../components/ana-ui.js";
import { divCell, hbars, heatTiles, multiLine, scatterChart, stackBar, tipBox, tipRow } from "../components/ana-charts.js";

const TABS = [
  { key: "elections", label: "Elections" },
  { key: "compare", label: "Compare two elections" },
  { key: "records", label: "Records" },
  { key: "explorer", label: "Series explorer" },
  { key: "analytics", label: "Analytics" },
];
const FAMILIES = { PRES: "President", HOUSE: "House", SEN: "Senate", GOV: "Governor", MAYOR: "Mayor", COUNCIL: "Municipal council" };
const RACE_TYPES = ["PRESIDENT", "PRESIDENT_PROVINCE", "HOUSE", "SENATE", "GOVERNOR", "MAYOR", "MUNICIPAL_COUNCIL", "PROVINCIAL_LEGISLATURE"];
const pp = (v, d = 1) => (v == null ? "–" : `${fmtPP(v, d)} pp`);

export async function render(el, _params, ctx) {
  const q = ctx.query;
  const st = {
    tab: q.tab || "elections",
    type: q.type || "",
    maps: [],
    data: null,
    munis: null,
    districts: null,
    cmp: { a: q.a || "", b: q.b || "", race: q.race || "", level: q.level || "", metric: q.metric || "flips" },
    rec: { race_type: q.rt || "", n: Number(q.n) || 10 },
    ex: { kind: q.series || "municipality", mun: q.mun || "GM0855", prov: q.prov || "NB", dist: q.dist || "NB-07", race: q.srace || "", party: q.party || "" },
    an: { id: Number(q.ae) || null, party: q.party || "", province: q.province || "", rt: q.art || "" },
  };
  const cleanups = [];
  const destroyMaps = () => {
    st.maps.forEach((m) => {
      try {
        m.destroy();
      } catch {
        /* ignore */
      }
    });
    st.maps = [];
  };
  cleanups.push(destroyMaps);

  const bodyHost = h("div");
  mount(
    el,
    pageHeader({ eyebrow: "Analysis · History", title: "History", categories: ["SIMULATED", "FICTIONAL"], meta: [h("span", { class: "ana-note", style: { margin: 0 } }, "Reported elections only")] }),
    callout("sim", h("strong", null, "Simulated election history of a fictional system. "), "Every result below comes from simulated elections of invented parties and candidates on real Dutch geography; it is not the history of real Dutch elections."),
    h("div", { style: { height: "16px" } }),
    bodyHost,
  );

  async function loadAll() {
    const [summary, divergence, elections] = await Promise.all([
      api.get("/api/history/summary"),
      api.get("/api/history/divergence").catch(() => ({ elections: [] })),
      api.get("/api/elections"),
    ]);
    const reported = summary.elections.map((e) => e.election);
    const avail = {
      PRES: summary.elections.filter((e) => e.president).length,
      HOUSE: summary.elections.filter((e) => e.house && Object.keys(e.house.composition || {}).length).length,
      SEN: summary.elections.filter((e) => e.senate && Object.keys(e.senate.composition || {}).length).length,
      GOV: summary.elections.filter((e) => e.governors).length,
    };
    st.data = { summary, divergence, elections: elections.elections, reported, avail };
    return st.data;
  }

  function build() {
    const content = h("div");
    if (!TABS.some((t) => t.key === st.tab)) st.tab = "elections";
    const show = (k) => {
      destroyMaps();
      st.tab = k;
      setQuery({ tab: k === "elections" ? null : k });
      let node;
      try {
        node = k === "elections" ? electionsTab() : k === "compare" ? compareTab() : k === "records" ? recordsTab() : k === "explorer" ? explorerTab() : analyticsTab();
      } catch (e) {
        console.error(e);
        node = errorBox(e);
      }
      mount(content, node);
    };
    const bar = tabBar(TABS, st.tab, show, { label: "History sections" });
    show(st.tab);
    return h("div", null, bar, content);
  }

  const electionLabel = (e) => `${e.year} · ${e.name}`;
  const reportedOptions = () => st.data.reported.map((e) => ({ value: e.id, label: electionLabel(e) }));
  const segs = (composition, total) =>
    sortByPartyOrder(Object.keys(composition || {})).map((p) => ({ key: p, label: partyInfo(p).name, color: partyInfo(p).color, value: composition[p] }));

  /* ============================================================== Elections */
  function electionsTab() {
    const { summary, divergence, elections } = st.data;
    const sumBy = Object.fromEntries(summary.elections.map((s) => [s.election.id, s]));
    const divBy = Object.fromEntries((divergence.elections || []).map((d) => [d.election_id, d]));
    const list = elections.filter((e) => !st.type || e.election_type === st.type);
    const filters = filterRow(
      labelled("Election type", segmented([{ value: "", label: "All" }, { value: "general", label: "General" }, { value: "midterm", label: "Midterm" }], st.type, (v) => { st.type = v; setQuery({ type: v || null }); mount(host, electionsTab()); }, { label: "Election type" })),
      h("span", { class: "ana-count" }, `${list.length} election${list.length === 1 ? "" : "s"} · ${summary.count} reported`),
    );
    const timeline = h(
      "ol",
      { class: "ana-timeline" },
      list.map((e) => {
        const s = sumBy[e.id];
        const d = divBy[e.id];
        const pres = s?.president;
        const presParty = pres?.party;
        return h(
          "li",
          { class: ["ana-tl", !s && "ana-tl--hidden"], style: presParty ? { "--party": partyInfo(presParty).color } : undefined },
          h("div", { class: "ana-tl__year" }, String(e.year)),
          h(
            "div",
            { class: "ana-tl__body" },
            h("div", { class: "ana-row ana-row--between" }, h("div", null, h("div", { class: "ana-tl__name" }, e.name), h("div", { class: "ana-note", style: { margin: 0 } }, `${humanize(e.election_type)} · ${fmtDate(e.election_date)}`)), h("div", { class: "ana-row" }, sourcePill(e.results_source), d?.diverged ? statusBadge("warn", "EC / PV split") : null, pres?.decided_by === "contingent" ? statusBadge("info", "Contingent election") : null)),
            s
              ? h(
                  "div",
                  { class: "ana-tl__grid" },
                  pres
                    ? h(
                        "div",
                        { class: "ana-tl__cell" },
                        h("div", { class: "ana-tl__label" }, "President"),
                        h("div", { class: "ana-tl__winner" }, pchip(presParty), h("b", null, pres.winner_name)),
                        h("div", { class: "ana-tl__sub num" }, `${fmtInt(pres.electoral_votes?.[pres.winner])} EV · ${fmtPct(pres.popular_vote?.winner_pct)} of the vote · ${humanize(pres.decided_by)}`),
                      )
                    : h("div", { class: "ana-tl__cell" }, h("div", { class: "ana-tl__label" }, "President"), h("div", { class: "muted" }, "Not elected (midterm)")),
                  s.house
                    ? h("div", { class: "ana-tl__cell" }, h("div", { class: "ana-tl__label" }, `House · ${s.house.controlling_party ? `${s.house.controlling_party} control` : "no majority"}`), stackBar({ segments: segs(s.house.composition), total: 150, marker: { at: s.house.majority, label: `${s.house.majority} for control` }, height: 14 }))
                    : null,
                  s.senate
                    ? h("div", { class: "ana-tl__cell" }, h("div", { class: "ana-tl__label" }, `Senate · ${s.senate.controlling_party ? `${s.senate.controlling_party} control` : "no majority"} · ${s.senate.seats_contested} contested`), stackBar({ segments: segs(s.senate.composition), total: 24, marker: { at: s.senate.majority, label: `${s.senate.majority} for control` }, height: 14 }))
                    : null,
                  h("div", { class: "ana-tl__cell ana-tl__stats" }, kv([["Turnout", fmtPct(s.turnout_pct)], ["Flips", fmtInt(s.flips)], ["Governors", s.governors ? `${Object.keys(s.governors).length} elected` : "–"]], { cols: 1 })),
                )
              : h("div", { class: "ana-tl__hidden" }, icon("lock", { size: 14 }), "Results hidden until this election is reported (election night or finalisation)."),
          ),
        );
      }),
    );
    const rows = list.map((e) => {
      const s = sumBy[e.id];
      const d = divBy[e.id];
      return {
        id: e.id,
        year: e.year,
        name: e.name,
        type: e.election_type,
        status: e.status,
        source: e.results_source,
        president: s?.president?.winner_name || null,
        party: s?.president?.party || null,
        ev: s?.president ? s.president.electoral_votes?.[s.president.winner] ?? null : null,
        pv: s?.president?.popular_vote?.winner_pct ?? null,
        pvMargin: s?.president?.popular_vote?.margin_pp ?? null,
        decided: s?.president?.decided_by || null,
        house: s?.house ? s.house.controlling_party || "No majority" : null,
        senate: s?.senate ? s.senate.controlling_party || "No majority" : null,
        turnout: s?.turnout_pct ?? null,
        flips: s?.flips ?? null,
        diverged: d ? d.diverged : null,
      };
    });
    const table = dataTable(
      [
        { key: "year", label: "Year" },
        { key: "name", label: "Election" },
        { key: "type", label: "Type", format: (v) => humanize(v) },
        { key: "source", label: "Results", format: (v) => sourcePill(v) },
        { key: "president", label: "President", format: (v, r) => (v ? h("span", { class: "ana-inl" }, pchip(r.party), v) : h("span", { class: "muted" }, r.source === "hidden" ? "hidden" : "–")) },
        { key: "ev", label: "EV", align: "r", format: (v) => fmtInt(v) },
        { key: "pv", label: "PV %", align: "r", format: (v) => fmtPct(v) },
        { key: "pvMargin", label: "PV margin", align: "r", format: (v) => pp(v) },
        { key: "decided", label: "Decided by", format: (v) => humanize(v) || "–" },
        { key: "house", label: "House", format: (v) => (v ? (v === "No majority" ? h("span", { class: "muted" }, v) : pchip(v)) : "–") },
        { key: "senate", label: "Senate", format: (v) => (v ? (v === "No majority" ? h("span", { class: "muted" }, v) : pchip(v)) : "–") },
        { key: "turnout", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
        { key: "flips", label: "Flips", align: "r", format: (v) => fmtInt(v) },
        { key: "diverged", label: "EC/PV split", format: (v) => (v === true ? statusBadge("warn", "Split") : v === false ? h("span", { class: "muted" }, "No") : "–") },
      ],
      rows,
      { sortKey: "year", sortDir: "asc" },
    );
    const host = h(
      "div",
      { class: "ana-stack-v" },
      filters,
      timeline,
      card("All elections", table, { flush: true, categories: ["SIMULATED"], foot: "Control = a party holding at least 76 House seats / 13 Senate seats, as reported by the API. Flips = seats changing party against the previous reported election." }),
    );
    return host;
  }

  /* ============================================================== Compare */
  function compareTab() {
    const c = st.cmp;
    const { avail } = st.data;
    const fams = ["PRES", "HOUSE", "SEN", "GOV"].filter((f) => avail[f] >= 2);
    if (!fams.length) return callout("info", "At least two reported elections holding the same race family are needed for a comparison.");
    if (!fams.includes(c.race)) c.race = fams[0];
    const levels = ["national", "province", "district", "municipality"];
    if (!levels.includes(c.level)) c.level = c.race === "HOUSE" ? "district" : "province";
    const opts = [{ value: "", label: "Automatic (last two)" }, ...reportedOptions()];
    const resultHost = h("div");
    const filters = filterRow(
      labelled("Race", segmented(fams.map((f) => ({ value: f, label: FAMILIES[f] })), c.race, (v) => { c.race = v; sync(); load(); }, { label: "Race family" })),
      labelled("Earlier election (A)", selectBox(opts, c.a, (v) => { c.a = v; sync(); load(); }, { "aria-label": "Election A" })),
      labelled("Later election (B)", selectBox(opts, c.b, (v) => { c.b = v; sync(); load(); }, { "aria-label": "Election B" })),
      labelled("Level", segmented(levels.map((l) => ({ value: l, label: humanize(l) })), c.level, (v) => { c.level = v; sync(); load(); }, { label: "Geographic level" })),
    );
    function sync() {
      setQuery({ race: c.race, a: c.a || null, b: c.b || null, level: c.level, metric: c.metric });
    }
    function load() {
      destroyMaps();
      const p = new URLSearchParams({ race: c.race, level: c.level });
      if (c.a) p.set("a", c.a);
      if (c.b) p.set("b", c.b);
      refresh(resultHost, api.get(`/api/history/compare?${p}`), (d) => compareResult(d), { onError: (e) => errorBox(e, e.status === 422 ? "Choose two different elections" : undefined) });
    }
    load();
    return h("div", { class: "ana-stack-v" }, filters, resultHost);
  }

  function compareResult(d) {
    const c = st.cmp;
    const parties = sortByPartyOrder(d.national.map((n) => n.party));
    const maxSwing = Math.max(1, ...d.rows.flatMap((r) => Object.values(r.swing || {}).map(Math.abs)));
    if (c.metric !== "flips" && !parties.includes(c.metric)) c.metric = "flips";
    const natBars = hbars({
      rows: [...d.national].sort((a, b) => b.swing_pp - a.swing_pp).map((n) => ({ key: n.party, label: n.party, labelNode: pchip(n.party), value: n.swing_pp, color: partyInfo(n.party).color, tip: tipBox(partyInfo(n.party).name, [tipRow(partyInfo(n.party).color, `${d.a.year}`, fmtShare(n.share_a)), tipRow(null, `${d.b.year}`, fmtShare(n.share_b)), tipRow(null, "Swing", pp(n.swing_pp, 2))]) })),
      diverging: true,
      format: (v) => pp(v),
      labelWidth: 64,
      ariaLabel: `National swing per party from ${d.a.year} to ${d.b.year}`,
    });
    const flipsTable = dataTable(
      [
        { key: "party", label: "Party", format: (v) => pchip(v) },
        { key: "wins_prev", label: `Won ${d.a.year}`, align: "r" },
        { key: "wins_curr", label: `Won ${d.b.year}`, align: "r" },
        { key: "holds", label: "Holds", align: "r" },
        { key: "gains", label: "Gains", align: "r" },
        { key: "losses", label: "Losses", align: "r" },
        { key: "net", label: "Net", align: "r", format: (v) => h("b", null, v > 0 ? `+${v}` : v < 0 ? `−${Math.abs(v)}` : "0") },
      ],
      d.flips_summary || [],
      { sortKey: "net" },
    );
    const mapLayer = d.level === "province" ? "provinces" : d.level === "district" ? "districts" : d.level === "municipality" ? "municipalities" : null;
    const rowsBy = Object.fromEntries(d.rows.map((r) => [r.geo_code, r]));
    const mapHost = h("div");
    let mapObj = null;
    const styleFor = () => (f) => {
      const r = rowsBy[f.properties.code];
      const surf = getComputedStyle(document.documentElement).getPropertyValue("--surface-2").trim();
      if (!r) return { fillColor: getComputedStyle(document.documentElement).getPropertyValue("--uncalled-soft").trim() };
      if (c.metric === "flips") {
        const col = partyInfo(r.winner_b).color;
        return { fillColor: r.flip_status === "flip" ? col : `color-mix(in srgb, ${col} 26%, ${surf})`, fillOpacity: 0.95 };
      }
      return { fillColor: diverging(r.swing?.[c.metric] ?? null, maxSwing), fillOpacity: 0.95 };
    };
    const tipFor = (f) => {
      const r = rowsBy[f.properties.code];
      if (!r) return h("div", { class: "ana-tip" }, h("div", { class: "ana-tip__title" }, f.properties.name || f.properties.code), h("span", { class: "muted" }, "No comparison row"));
      return tipBox(
        `${r.geo_name}`,
        [
          tipRow(partyInfo(r.winner_a).color, `Winner ${d.a.year}`, r.winner_a || "–", { line: false }),
          tipRow(partyInfo(r.winner_b).color, `Winner ${d.b.year}`, r.winner_b || "–", { line: false }),
          tipRow(null, "Status", humanize(r.flip_status)),
          c.metric !== "flips" ? tipRow(partyInfo(c.metric).color, `${c.metric} swing`, pp(r.swing?.[c.metric])) : null,
          tipRow(null, "Turnout change", pp(r.turnout_change_pp)),
        ].filter(Boolean),
      );
    };
    const legendHost = h("div");
    const drawLegend = () =>
      mount(
        legendHost,
        c.metric === "flips"
          ? h("div", { class: "ana-scale" }, h("span", null, "Solid = flipped to the party shown (winner in B); faded = held"))
          : h("div", { class: "ana-scale" }, h("span", null, `${c.metric} loses`), h("span", { class: "ana-scale__ramp" }, ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"].map((v) => h("span", { style: { background: `var(${v})` } }))), h("span", null, `${c.metric} gains · ±${fmt1(maxSwing)} pp`)),
      );
    drawLegend();
    const metricSel = selectBox([{ value: "flips", label: "Flips (winner in B)" }, ...parties.map((p) => ({ value: p, label: `Swing: ${p}` }))], c.metric, (v) => {
      c.metric = v;
      setQuery({ metric: v });
      drawLegend();
      mapObj?.restyle(styleFor(), tipFor);
      drawTable();
    }, { "aria-label": "Map metric" });
    if (mapLayer) {
      requestAnimationFrame(() => {
        try {
          mapObj = electionMap(mapHost, { layer: mapLayer, height: 520, outline: mapLayer === "provinces" ? null : "provinces", style: styleFor(), tooltip: tipFor });
          mapObj.el.setAttribute("role", "img");
          mapObj.el.setAttribute("aria-label", `Map of ${d.level} changes between ${d.a.year} and ${d.b.year}`);
          st.maps.push(mapObj);
        } catch (e) {
          mount(mapHost, errorBox(e));
        }
      });
    }
    const tableRows = () => d.rows.map((r) => ({ ...r, sel: c.metric === "flips" ? null : r.swing?.[c.metric] ?? null }));
    const geoHref = (r) => (d.level === "province" ? links.province(r.geo_code) : d.level === "district" ? links.district(r.geo_code) : d.level === "municipality" ? links.municipality(r.geo_code) : null);
    const tableHost = h("div");
    const drawTable = () => mount(tableHost, dataTable(
      [
        { key: "geo_name", label: humanize(d.level), format: (v, r) => (geoHref(r) ? h("a", { class: "ana-link", href: geoHref(r) }, v) : v) },
        { key: "province_code", label: "Prov." },
        { key: "winner_a", label: `${d.a.year}`, format: (v) => (v ? pchip(v) : "–") },
        { key: "winner_b", label: `${d.b.year}`, format: (v) => (v ? pchip(v) : "–") },
        { key: "flip_status", label: "Status", format: (v) => (v === "flip" ? h("span", { class: "ana-flip" }, icon("flip", { size: 12 }), "Flip") : h("span", { class: "muted" }, humanize(v))) },
        c.metric === "flips" ? null : { key: "sel", label: `${c.metric} swing`, align: "r", format: (v) => divCell(v, maxSwing, (x) => fmtPP(x)) },
        ...parties.slice(0, 8).map((p) => ({ key: `s_${p}`, label: h("span", { class: "ana-party-th" }, pchip(p)), align: "r", value: (r) => r.swing?.[p] ?? null, format: (v) => (v == null ? "–" : fmtPP(v)) })),
        { key: "turnout_change_pp", label: "Turnout Δ", align: "r", format: (v) => pp(v) },
      ].filter(Boolean),
      tableRows(),
      { sortKey: c.metric === "flips" ? "geo_name" : "sel", sortDir: c.metric === "flips" ? "asc" : "desc", maxHeight: 520 },
    ));
    drawTable();
    const counts = d.flip_counts || {};
    return h(
      "div",
      { class: "ana-stack-v" },
      h(
        "div",
        { class: "ana-kpis" },
        kpi("Comparison", `${d.a.year} → ${d.b.year}`, `${FAMILIES[d.race] || d.race} · ${humanize(d.level)} level`),
        ...Object.entries(counts).map(([k, v]) => kpi(humanize(k), fmtInt(v), k === "flip" ? "changed party" : k === "hold" ? "same party" : "")),
        kpi("Lineage", d.lineage_applied ? "Applied" : "Not needed", "municipal mergers remapped to current codes"),
      ),
      h("div", { class: "grid grid--2" }, card("National swing (pp)", natBars, { categories: ["SIMULATED"], foot: `Change in each party's national share, ${d.a.year} → ${d.b.year}.` }), card("Flips by party", flipsTable, { flush: true })),
      mapLayer
        ? card(`${humanize(d.level)} map`, h("div", { class: "ana-stack-v" }, h("div", { class: "ana-row ana-row--between" }, labelled("Map metric", metricSel), legendHost), mapHost), { categories: ["REAL", "SIMULATED"], foot: "Boundaries CBS/PDOK (REAL); results SIMULATED. The table below lists every row." })
        : null,
      card(`${humanize(d.level)} rows (${d.rows.length})`, tableHost, { flush: true, categories: ["SIMULATED"] }),
    );
  }

  /* ============================================================== Records */
  function recordsTab() {
    const r = st.rec;
    const closeHost = h("div");
    const slideHost = h("div");
    const divHost = h("div");
    const filters = filterRow(
      labelled("Race type", selectBox([{ value: "", label: "All race types" }, ...RACE_TYPES.map((t) => ({ value: t, label: humanize(t.toLowerCase()) }))], r.race_type, (v) => { r.race_type = v; setQuery({ rt: v || null }); load(); }, { "aria-label": "Race type" })),
      labelled("Show", segmented([10, 25, 50].map((n) => ({ value: n, label: `Top ${n}` })), r.n, (v) => { r.n = Number(v); setQuery({ n: v }); load(); }, { label: "Number of records" })),
    );
    const cols = (kind) => [
      { key: "rank", label: "#", align: "r" },
      { key: "year", label: "Year" },
      { key: "race_code", label: "Race", format: (v, x) => h("a", { class: "ana-link mono", href: `${links.race(v)}?e=${x.election_id}` }, v) },
      { key: "geo_name", label: "Where", format: (v, x) => h("span", { title: x.geo_code }, v) },
      { key: "winner_candidate", label: "Winner", format: (v, x) => h("span", { class: "ana-inl" }, pchip(x.winner_party), h("span", { class: "ana-trunc" }, v)) },
      { key: "winner_share", label: "Share", align: "r", format: (v) => fmtShare(v) },
      { key: "runner_up_candidate", label: "Runner-up", format: (v, x) => (v ? h("span", { class: "ana-inl" }, pchip(x.runner_up_party), h("span", { class: "ana-trunc" }, v)) : "–") },
      { key: "margin_votes", label: "Margin (votes)", align: "r", format: (v) => fmtInt(v) },
      { key: "margin_pp", label: "Margin", align: "r", format: (v, x) => h("b", null, `${kind === "closest" ? fmt2(v) : fmt1(v)} pp${x.tied ? " (tie)" : ""}`) },
    ];
    function load() {
      const p = new URLSearchParams({ n: String(r.n) });
      if (r.race_type) p.set("race_type", r.race_type);
      refresh(closeHost, api.get(`/api/history/closest?${p}`), (d) => card(`Closest races (${d.count})`, dataTable(cols("closest"), d.races, { sortKey: "rank", sortDir: "asc" }), { flush: true, categories: ["SIMULATED"], foot: "Smallest winner − runner-up margins in percentage points of valid votes, at each race's own level." }));
      refresh(slideHost, api.get(`/api/history/landslides?${p}`), (d) => card(`Largest landslides (${d.count})`, dataTable(cols("landslides"), d.races, { sortKey: "rank", sortDir: "asc" }), { flush: true, categories: ["SIMULATED"] }));
    }
    load();
    const divList = st.data.divergence.elections || [];
    const namesP = Promise.all(divList.map((x) => api.get(`/api/elections/${x.election_id}/president`, { cache: true }).catch(() => null))).then((all) => {
      const names = {};
      for (const pr of all) for (const t of pr?.tickets || []) names[t.key] = { name: t.name, party: t.party };
      return names;
    });
    refresh(divHost, namesP, (names) => {
      const nm = (k) => names[k]?.name || k;
      const chip = (k) => (names[k] ? pchip(names[k].party) : null);
      const describe = (text) => Object.keys(names).sort((a, b) => b.length - a.length).reduce((t, k) => t.split(k).join(names[k].name), text || "");
      return card(
        "Electoral College vs. popular vote",
        divList.length
          ? h(
              "div",
              { class: "ana-stack-v" },
              divList.map((x) =>
                h(
                  "div",
                  { class: "ana-divrec" },
                  h("div", { class: "ana-row ana-row--between" }, h("b", null, `${x.year}`), x.diverged ? statusBadge("warn", "Diverged") : statusBadge("ok", "Aligned")),
                  h("p", { class: "ana-sub", style: { margin: "6px 0" } }, describe(x.description)),
                  kv([["Popular-vote leader", h("span", { class: "ana-inl" }, chip(x.pv_leader), `${nm(x.pv_leader)} · ${fmtShare(x.pv_leader_share)} (+${fmt2(x.pv_margin_pp)} pp)`)], ["Electoral-vote leader", h("span", { class: "ana-inl" }, chip(x.ev_leader), `${nm(x.ev_leader)} · ${fmtInt(x.ev_leader_votes)} EV (${fmtShare(x.ev_leader_pv_share)} of PV)`)], ["Majority reached", x.ev_majority ? `Yes (${x.majority} of ${x.total_ev})` : `No — ${x.majority} needed`]], { cols: 1 }),
                ),
              ),
            )
          : h("div", { class: "state" }, "No presidential elections reported yet"),
        { categories: ["SIMULATED"] },
      );
    });
    return h("div", { class: "ana-stack-v" }, filters, closeHost, slideHost, divHost);
  }

  /* ============================================================== Explorer */
  function explorerTab() {
    const x = st.ex;
    const host = h("div", { class: "ana-stack-v" });
    const resultHost = h("div");
    const fams = { municipality: ["PRES", "HOUSE", "SEN", "GOV", "MAYOR", "COUNCIL"], province: ["PRES", "HOUSE", "SEN", "GOV"], district: [] };
    const needs = [];
    if (!st.munis) needs.push(api.get("/api/municipalities", { cache: true }).then((d) => (st.munis = d.municipalities.map((m) => ({ code: m.code, name: m.name, province: m.province_code })).sort((a, b) => a.name.localeCompare(b.name, "nl")))));
    if (!st.districts) needs.push(api.get("/api/districts", { cache: true }).then((d) => (st.districts = d.districts.map((m) => ({ code: m.code, name: m.name, province: m.province_code })))));
    function defaultRace() {
      const a = st.data.avail;
      return a.PRES >= 2 ? "PRES" : "HOUSE";
    }
    if (!x.race) x.race = defaultRace();
    function drawFilters() {
      const kindSeg = segmented([{ value: "municipality", label: "Municipality" }, { value: "province", label: "Province" }, { value: "district", label: "House district" }], x.kind, (v) => { x.kind = v; setQuery({ series: v }); drawFilters(); load(); }, { label: "Series type" });
      let picker = null;
      if (x.kind === "municipality") {
        const input = h("input", { class: "input", list: "ana-muni-list", value: (st.munis || []).find((m) => m.code === x.mun)?.name || x.mun, placeholder: "Type a municipality", "aria-label": "Municipality", style: { width: "220px" } });
        const dl = h("datalist", { id: "ana-muni-list" }, (st.munis || []).map((m) => h("option", { value: m.name }, `${m.code} · ${provinceName(m.province)}`)));
        input.addEventListener("change", () => {
          const v = input.value.trim().toLowerCase();
          const m = (st.munis || []).find((y) => y.name.toLowerCase() === v || y.code.toLowerCase() === v);
          if (m) {
            x.mun = m.code;
            setQuery({ mun: m.code });
            load();
          }
        });
        picker = h("span", null, input, dl);
      } else if (x.kind === "province") {
        picker = selectBox(PROVINCES.map((p) => ({ value: p, label: provinceName(p) })), x.prov, (v) => { x.prov = v; setQuery({ prov: v }); load(); }, { "aria-label": "Province" });
      } else {
        picker = selectBox((st.districts || []).map((dd) => ({ value: dd.code, label: `${dd.code} · ${dd.name}` })), x.dist, (v) => { x.dist = v; setQuery({ dist: v }); load(); }, { "aria-label": "House district" });
      }
      const famList = fams[x.kind];
      if (famList.length && !famList.includes(x.race)) x.race = famList[0];
      mount(
        filtersHost,
        filterRow(
          labelled("Series", kindSeg),
          labelled(x.kind === "municipality" ? "Municipality" : x.kind === "province" ? "Province" : "District", picker),
          famList.length ? labelled("Race", selectBox(famList.map((f) => ({ value: f, label: FAMILIES[f] })), x.race, (v) => { x.race = v; setQuery({ srace: v }); load(); }, { "aria-label": "Race family" })) : null,
          x.kind !== "district" ? labelled("Highlight party", selectBox([{ value: "", label: "All parties" }, ...partyOrder().map((p) => ({ value: p, label: p }))], x.party, (v) => { x.party = v; setQuery({ party: v || null }); load(); }, { "aria-label": "Highlight party" })) : null,
        ),
      );
    }
    const filtersHost = h("div");
    function load() {
      if (x.kind === "district") {
        refresh(resultHost, api.get(`/api/history/district/${encodeURIComponent(x.dist)}`), (d) => districtSeries(d));
      } else {
        const code = x.kind === "municipality" ? x.mun : x.prov;
        refresh(resultHost, api.get(`/api/history/${x.kind}/${encodeURIComponent(code)}?race=${x.race}`), (d) => geoSeries(d), { onError: (e) => errorBox(e, e.status === 404 ? `No reported ${FAMILIES[x.race] || x.race} results here` : undefined) });
      }
    }
    Promise.all(needs).then(() => {
      drawFilters();
      load();
    });
    mount(host, filtersHost, resultHost);
    return host;
  }

  function geoSeries(d) {
    const x = st.ex;
    const pts = d.points || [];
    if (!pts.length) return h("div", { class: "state" }, "No reported results for this race here yet.");
    const parties = sortByPartyOrder([...new Set(pts.flatMap((p) => Object.keys(p.shares || {})))]);
    const hl = x.party;
    const series = parties.map((p) => ({
      key: p,
      label: p,
      color: !hl || hl === p ? partyInfo(p).color : "var(--uncalled)",
      points: pts.map((pt) => ({ x: pt.election.year, y: pt.shares?.[p] ?? null })),
    }));
    // draw the highlighted party last (on top)
    if (hl) series.sort((a, b) => (a.key === hl) - (b.key === hl));
    const years = pts.map((p) => p.election.year);
    const chart = multiLine({ series, xType: "number", xTicks: years, xFormat: (v) => String(Math.round(v)), yFormat: (v) => `${Math.round(v)}%`, tipFormat: (v) => `${fmt1(v)}%`, height: 280, showDots: true, ariaLabel: `Party vote shares in ${d.name} across elections`, showLegend: true });
    const nat = pts.length && pts[pts.length - 1].national_shares ? pts[pts.length - 1] : null;
    const table = dataTable(
      [
        { key: "year", label: "Year" },
        { key: "electionName", label: "Election" },
        { key: "winner", label: "Winner", format: (v) => (v ? pchip(v) : "–") },
        { key: "margin_pp", label: "Margin", align: "r", format: (v) => `${fmt1(v)} pp` },
        { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
        { key: "valid_votes", label: "Valid votes", align: "r", format: (v) => fmtInt(v) },
        { key: "flipped", label: "Flip", format: (v) => (v ? h("span", { class: "ana-flip" }, icon("flip", { size: 12 }), "Flip") : v === false ? h("span", { class: "muted" }, "Hold") : "–") },
        ...parties.map((p) => ({ key: `s_${p}`, label: h("span", { class: "ana-party-th" }, pchip(p)), align: "r", value: (r) => r.shares?.[p] ?? null, format: (v, r) => (v == null ? "–" : h("span", { title: `lean ${fmtPP(r.lean_pp?.[p])} pp vs national${r.change_pp?.[p] != null ? ` · change ${fmtPP(r.change_pp[p])} pp` : ""}`, class: r.winner === p ? "ana-num-strong" : "" }, fmt1(v))) })),
      ],
      pts.map((p) => ({ ...p, year: p.election.year, electionName: p.election.name })),
      { sortKey: "year", sortDir: "asc" },
    );
    const leanRows = nat
      ? parties.filter((p) => nat.lean_pp?.[p] != null).map((p) => ({ key: p, label: p, labelNode: pchip(p), value: nat.lean_pp[p], color: partyInfo(p).color, tip: tipBox(partyInfo(p).name, [tipRow(partyInfo(p).color, d.name, `${fmt1(nat.shares?.[p])}%`), tipRow(null, "National", `${fmt1(nat.national_shares?.[p])}%`), tipRow(null, "Lean", pp(nat.lean_pp[p]))]) }))
      : [];
    return h(
      "div",
      { class: "ana-stack-v" },
      h("div", { class: "ana-section-title", style: { marginTop: 0 } }, h("h2", null, `${d.name} · ${FAMILIES[d.race] || d.race}`), h("p", null, `${pts.length} reported election${pts.length === 1 ? "" : "s"} · ${d.level} ${d.code}${d.level === "municipality" ? " (older vintages remapped to current codes)" : ""}`)),
      h(
        "div",
        { class: "ana-split" },
        card("Party support across elections", chart, { categories: ["SIMULATED"], foot: hl ? `${hl} highlighted; other parties in grey. Hover or use ← → for values.` : "Share of valid votes per party. Hover or use ← → for values." }),
        card(`Lean vs. national (${pts[pts.length - 1].election.year})`, leanRows.length ? hbars({ rows: leanRows, diverging: true, format: (v) => pp(v), labelWidth: 64, ariaLabel: "Partisan lean against the national result" }) : h("div", { class: "state" }, "–"), { foot: "Local share minus national share (pp)." }),
      ),
      card("Results by election", table, { flush: true, categories: ["SIMULATED"] }),
    );
  }

  function districtSeries(d) {
    const pts = d.points || [];
    if (!pts.length) return h("div", { class: "state" }, "No reported House results for this district yet.");
    const chart = multiLine({
      series: [{ key: "share", label: "Winner's share", color: "var(--seq-5)", points: pts.map((p) => ({ x: p.year, y: p.winner_share * 100 })) }, { key: "margin", label: "Margin (pp)", color: "var(--seq-3)", points: pts.map((p) => ({ x: p.year, y: p.margin_pp })) }],
      xTicks: pts.map((p) => p.year),
      xFormat: (v) => String(Math.round(v)),
      yFormat: (v) => `${Math.round(v)}`,
      tipFormat: (v) => fmt1(v),
      showDots: true,
      height: 240,
      ariaLabel: `District ${d.code}: winner's share and margin by election`,
    });
    const table = dataTable(
      [
        { key: "year", label: "Year" },
        { key: "race_code", label: "Race", format: (v, r) => h("a", { class: "ana-link mono", href: `${links.race(v)}?e=${r.election_id}` }, v) },
        { key: "winner_candidate", label: "Winner", format: (v, r) => h("span", { class: "ana-inl" }, pchip(r.winner_party), v) },
        { key: "winner_share", label: "Share", align: "r", format: (v) => fmtShare(v) },
        { key: "runner_up_candidate", label: "Runner-up", format: (v, r) => (v ? h("span", { class: "ana-inl" }, pchip(r.runner_up_party), v) : "–") },
        { key: "margin_pp", label: "Margin", align: "r", format: (v, r) => `${fmt2(v)} pp (${fmtInt(r.margin_votes)})` },
        { key: "turnout", label: "Turnout", align: "r", format: (v) => fmtShare(v) },
        { key: "status", label: "Status", format: (v, r) => (v === "flip" ? h("span", { class: "ana-flip" }, icon("flip", { size: 12 }), `Flip from ${r.previous_winner_party}`) : h("span", { class: "muted" }, humanize(v))) },
        { key: "district_plan_id", label: "Plan", align: "r" },
      ],
      pts,
      { sortKey: "year", sortDir: "asc" },
    );
    return h(
      "div",
      { class: "ana-stack-v" },
      h("div", { class: "ana-section-title", style: { marginTop: 0 } }, h("h2", null, h("a", { class: "ana-link", href: links.district(d.code) }, d.code), ` · ${pts[pts.length - 1].geo_name}`), h("p", null, d.note)),
      h("div", { class: "ana-split" }, card("Voting history", table, { flush: true, categories: ["SIMULATED", "FICTIONAL"] }), card("Winner's share and margin", chart, { foot: "Both in percentage points; one shared axis." })),
    );
  }

  /* ============================================================== Analytics */
  function analyticsTab() {
    const a = st.an;
    const opts = st.data.elections.map((e) => ({ value: e.id, label: `${electionLabel(e)}${e.reported ? "" : " — not reported"}`, disabled: !e.reported }));
    if (!a.id || !st.data.reported.some((e) => e.id === a.id)) {
      const cur = currentElectionId();
      a.id = st.data.reported.some((e) => e.id === cur) ? cur : st.data.reported[st.data.reported.length - 1]?.id || null;
    }
    const resultHost = h("div");
    const filters = filterRow(
      labelled("Election", selectBox(opts, a.id, (v) => { a.id = Number(v); setQuery({ ae: v }); load(); }, { "aria-label": "Election" })),
      labelled("Party", selectBox([{ value: "", label: "All parties" }, ...partyOrder().map((p) => ({ value: p, label: p }))], a.party, (v) => { a.party = v; setQuery({ party: v || null }); load(); }, { "aria-label": "Party" })),
      labelled("Province", selectBox([{ value: "", label: "All provinces" }, ...PROVINCES.map((p) => ({ value: p, label: provinceName(p) }))], a.province, (v) => { a.province = v; setQuery({ province: v || null }); load(); }, { "aria-label": "Province" })),
      labelled("Race type", selectBox([{ value: "", label: "All race types" }, ...RACE_TYPES.map((t) => ({ value: t, label: humanize(t.toLowerCase()) }))], a.rt, (v) => { a.rt = v; setQuery({ art: v || null }); load(); }, { "aria-label": "Race type" })),
    );
    function load() {
      if (!a.id) {
        mount(resultHost, h("div", { class: "state" }, "No reported election yet."));
        return;
      }
      refresh(resultHost, api.get(`/api/analytics/${a.id}`, { cache: true }), (d) => analyticsBody(d));
    }
    load();
    return h("div", { class: "ana-stack-v" }, filters, resultHost);
  }

  function analyticsBody(d) {
    const a = st.an;
    if (!d.available) return callout("info", h("strong", null, "Analytics are available for reported elections only. "), d.notice || "");
    const blocks = [];
    const hl = (p) => !a.party || a.party === p;

    // Tipping point
    if (d.tipping_point) {
      const t = d.tipping_point;
      blocks.push(
        h(
          "div",
          { class: "ana-kpis" },
          kpi("Tipping-point province", h("span", { class: "ana-inl" }, h("a", { class: "ana-link", href: links.province(t.province_code) }, provinceName(t.province_code))), `decided for ${t.party}`, { accent: partyInfo(t.party).color }),
          kpi("Tipping-point margin", pp(t.margin_pp, 2), "winner's margin there"),
          kpi("National margin", pp(t.national_margin_pp, 2), "popular vote"),
          kpi("EC bias", pp(t.ec_bias_pp, 2), "tipping-point margin − national margin"),
        ),
      );
    }

    // Partisan lean
    if (d.lean?.provinces?.length) {
      const L = d.lean;
      const [pa, pb] = L.pair || [];
      const rows = L.provinces.filter((p) => !a.province || p.code === a.province);
      const parties = sortByPartyOrder([...new Set(L.provinces.flatMap((p) => Object.keys(p.lean_pp || {})))]);
      const metric = a.party && parties.includes(a.party) ? a.party : null;
      const vals = L.provinces.map((p) => (metric ? p.lean_pp?.[metric] : p.two_party_lean_pp)).filter((v) => v != null);
      const mx = Math.max(1, ...vals.map(Math.abs));
      const tiles = heatTiles(
        PROVINCES.map((code) => {
          const p = L.provinces.find((x) => x.code === code);
          const v = p ? (metric ? p.lean_pp?.[metric] : p.two_party_lean_pp) : null;
          const t = v == null ? 0 : Math.abs(v) / mx;
          return {
            code,
            fill: v == null ? "var(--uncalled-soft)" : diverging(v, mx),
            ink: t > 0.55 ? "light" : "dark",
            bottom: v == null ? "–" : fmtPP(v),
            outline: a.province === code ? "var(--text-primary)" : null,
            title: p ? `${p.name}: ${metric ? `${metric} lean` : "two-party lean"} ${fmtPP(v)} pp` : code,
            tip: p ? tipBox(p.name, [tipRow(null, metric ? `${metric} lean` : `Two-party lean (${pa}–${pb})`, pp(v)), tipRow(partyInfo(p.leans).color, "Leans", p.leans || "–", { line: false })]) : null,
          };
        }),
        { onSelect: (code) => { a.province = a.province === code ? "" : code; setQuery({ province: a.province || null }); mount(resultHostOf(), analyticsBody(d)); }, ariaLabel: "Partisan lean by province", size: "sm" },
      );
      const legend = h("div", { class: "ana-scale" }, h("span", null, metric ? `${metric} weaker` : `leans ${pb}`), h("span", { class: "ana-scale__ramp" }, ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"].map((v) => h("span", { style: { background: `var(${v})` } }))), h("span", null, metric ? `${metric} stronger` : `leans ${pa}`));
      const table = dataTable(
        [
          { key: "name", label: "Province", format: (v, r) => h("a", { class: "ana-link", href: links.province(r.code) }, v) },
          { key: "leans", label: "Leans", format: (v) => (v ? pchip(v) : "–") },
          { key: "two_party_lean_pp", label: `${pa}–${pb} lean`, align: "r", format: (v) => divCell(v, mx, (x) => fmtPP(x)) },
          ...parties.filter(hl).map((p) => ({ key: `l_${p}`, label: h("span", { class: "ana-party-th" }, pchip(p)), align: "r", value: (r) => r.lean_pp?.[p] ?? null, format: (v) => (v == null ? "–" : fmtPP(v)) })),
        ],
        rows,
        { sortKey: "two_party_lean_pp" },
      );
      blocks.push(
        h(
          "div",
          { class: "ana-split" },
          card(`Partisan lean · ${L.race} by ${L.level}`, table, { flush: true, categories: ["SIMULATED"], foot: `Province share minus national share (pp). Two-party lean: ${pa} vs ${pb}; positive = leans ${pa}.` }),
          card("Lean map", h("div", { class: "ana-stack-v" }, tiles, legend), { foot: "Select a tile to filter the table to one province." }),
        ),
      );
    }

    // Elasticity
    if (d.elasticity) {
      const E = d.elasticity;
      blocks.push(
        E.available
          ? card("Elasticity", dataTable([{ key: "name", label: "Province" }, ...sortByPartyOrder([...new Set(E.provinces.flatMap((p) => Object.keys(p.elasticity || {})))]).filter(hl).map((p) => ({ key: `e_${p}`, label: h("span", { class: "ana-party-th" }, pchip(p)), align: "r", value: (r) => r.elasticity?.[p] ?? null, format: (v) => fmt2(v) })), { key: "n_obs", label: "Obs.", align: "r" }], E.provinces.filter((p) => !a.province || p.code === a.province), { sortKey: "name", sortDir: "asc" }), { flush: true, categories: ["SIMULATED"], foot: "Slope of a province's share on the national share across reported elections (β > 1 = swings harder than the country)." })
          : callout("info", h("strong", null, "Elasticity not available: "), E.reason || "needs more reported elections."),
      );
    }

    // EC efficiency
    if (d.ec_efficiency?.length) {
      const rows = d.ec_efficiency;
      const mx = Math.max(1, ...rows.map((r) => Math.abs(r.efficiency_pp)));
      blocks.push(
        h(
          "div",
          { class: "ana-split" },
          card(
            "Electoral College efficiency",
            dataTable(
              [
                { key: "party", label: "Ticket", format: (v, r) => h("span", { class: "ana-inl" }, pchip(v), h("span", { class: "ana-trunc" }, r.label)) },
                { key: "popular_votes", label: "Votes", align: "r", format: (v) => fmtInt(v) },
                { key: "pv_share", label: "PV share", align: "r", format: (v) => fmtShare(v) },
                { key: "electoral_votes", label: "EV", align: "r" },
                { key: "ev_share", label: "EV share", align: "r", format: (v) => fmtShare(v) },
                { key: "provinces_won", label: "Prov. won", align: "r" },
                { key: "efficiency_pp", label: "EV − PV share", align: "r", format: (v) => divCell(v, mx, (x) => fmtPP(x)) },
              ],
              rows.filter((r) => hl(r.party)),
              { sortKey: "electoral_votes" },
            ),
            { flush: true, categories: ["SIMULATED"], foot: "Positive = the ticket's share of electoral votes exceeds its share of the popular vote." },
          ),
          card(
            "EV share vs. PV share",
            scatterChart({ points: rows.map((r) => ({ x: r.pv_share * 100, y: r.ev_share * 100, label: r.label, short: r.party, color: hl(r.party) ? partyInfo(r.party).color : "var(--uncalled)" })), xLabel: "Popular-vote share (%)", yLabel: "Electoral-vote share (%)", xFormat: (v) => `${Math.round(v)}`, yFormat: (v) => `${Math.round(v)}`, diagonal: true, diagonalLabel: "EV share = PV share", height: 290, ariaLabel: "Electoral-vote share against popular-vote share per ticket", tip: (p) => tipBox(p.label, [tipRow(p.color, "PV share", `${fmt1(p.x)}%`, { line: false }), tipRow(null, "EV share", `${fmt1(p.y)}%`)]) }),
            { foot: "Above the line = more electoral votes than the popular vote alone would give." },
          ),
        ),
      );
    }

    // Competitiveness
    if (d.competitiveness) {
      const C = d.competitiveness;
      const byType = (C.by_race_type || []).filter((r) => !a.rt || r.race_type === a.rt);
      const ratingSegs = (r) => [
        { key: "tossup", short: "Toss-up", label: "Toss-up", color: "var(--seq-6)", value: r.n_tossup },
        { key: "lean", short: "Lean", label: "Lean", color: "var(--seq-5)", value: r.n_lean },
        { key: "likely", short: "Likely", label: "Likely", color: "var(--seq-3)", value: r.n_likely },
        { key: "safe", short: "Safe", label: "Safe", color: "var(--seq-2)", value: r.n_safe },
      ];
      const typeTable = dataTable(
        [
          { key: "race_type", label: "Race type", format: (v) => humanize(v.toLowerCase()) },
          { key: "n_contests", label: "Contests", align: "r" },
          { key: "median_margin_pp", label: "Median margin", align: "r", format: (v) => `${fmt1(v)} pp` },
          { key: "mean_enc", label: "Mean ENC", align: "r", format: (v) => fmt2(v) },
          { key: "mean_competitiveness", label: "Competitiveness", align: "r", format: (v) => fmt2(v) },
          { key: "ratings", label: "Toss-up · Lean · Likely · Safe", sort: false, value: (r) => r, format: (r) => h("div", { style: { minWidth: "220px" } }, stackBar({ segments: ratingSegs(r), total: r.n_contests, height: 12, labels: false, unit: "contests" })) },
        ],
        byType,
        { sortKey: "n_contests" },
      );
      const most = (C.most_competitive || []).filter((r) => (!a.rt || r.race_type === a.rt) && (!a.party || r.winner_party === a.party || r.runner_up_party === a.party) && (!a.province || String(r.geo_code || "").startsWith(a.province) || r.province_code === a.province));
      const mostTable = dataTable(
        [
          { key: "race_code", label: "Race", format: (v) => h("a", { class: "ana-link mono", href: `${links.race(v)}?e=${d.election.id}` }, v) },
          { key: "race_type", label: "Type", format: (v) => humanize(String(v).toLowerCase()) },
          { key: "winner_party", label: "Winner", format: (v) => pchip(v) },
          { key: "runner_up_party", label: "Runner-up", format: (v) => (v ? pchip(v) : "–") },
          { key: "margin_pp", label: "Margin", align: "r", format: (v) => `${fmt2(v)} pp` },
          { key: "enc", label: "ENC", align: "r", format: (v) => fmt2(v) },
          { key: "competitiveness", label: "Score", align: "r", format: (v) => fmt2(v) },
          { key: "rating", label: "Rating", format: (v) => humanize(v) },
        ],
        most,
        { sortKey: "competitiveness", maxHeight: 420 },
      );
      blocks.push(
        h(
          "div",
          { class: "ana-stack-v" },
          card("Competitiveness by race type", h("div", null, typeTable, h("div", { class: "ana-scale", style: { padding: "10px 16px" } }, ratingSegs({}).map((s) => h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": s.color } }), s.label)))), { flush: true, categories: ["SIMULATED"], foot: "ENC = effective number of candidates. Ratings bucket the final margin (toss-up closest)." }),
          card(`Most competitive contests (${most.length})`, mostTable, { flush: true }),
        ),
      );
    }

    // Seat–vote & efficiency gap
    if (d.seat_vote?.length || d.efficiency_gap?.length) {
      const sv = (d.seat_vote || []).filter((r) => r.party !== "independent" || r.seats > 0);
      const eg = d.efficiency_gap || [];
      const mxBonus = Math.max(1, ...sv.map((r) => Math.abs(r.seat_bonus_pp)));
      blocks.push(
        eg.length
          ? h(
              "div",
              { class: "ana-kpis" },
              eg.map((g) => [
                kpi("Efficiency gap (House)", `${fmtPP(g.efficiency_gap * 100, 2)} pp`, `pair ${g.party_a}–${g.party_b}: ${fmtPP(g.efficiency_gap_pair * 100, 2)} pp (+ favours ${g.party_a})`),
                kpi(`Wasted votes · ${g.party_a}`, fmtInt(g.wasted_a), `${fmtInt(g.seats_a)} seats from ${fmtInt(g.votes_a)} votes`, { accent: partyInfo(g.party_a).color }),
                kpi(`Wasted votes · ${g.party_b}`, fmtInt(g.wasted_b), `${fmtInt(g.seats_b)} seats from ${fmtInt(g.votes_b)} votes`, { accent: partyInfo(g.party_b).color }),
                kpi("Contests", fmtInt(g.n_contests), `${fmtInt(g.total_votes)} valid votes`),
              ]),
            )
          : null,
      );
      if (sv.length) {
        blocks.push(
          h(
            "div",
            { class: "ana-split" },
            card(
              "Seats and votes (House)",
              dataTable(
                [
                  { key: "party", label: "Party", format: (v) => pchip(v) },
                  { key: "votes", label: "Votes", align: "r", format: (v) => fmtInt(v) },
                  { key: "vote_share", label: "Vote share", align: "r", format: (v) => fmtShare(v) },
                  { key: "seats", label: "Seats", align: "r" },
                  { key: "seat_share", label: "Seat share", align: "r", format: (v) => fmtShare(v) },
                  { key: "seat_bonus_pp", label: "Seat bonus", align: "r", format: (v) => divCell(v, mxBonus, (x) => fmtPP(x)) },
                  { key: "waste_rate", label: "Wasted", align: "r", format: (v) => fmtShare(v) },
                  { key: "efficiency_gap", label: "Eff. gap", align: "r", format: (v) => fmtPP(v * 100, 2) },
                  { key: "votes_per_seat", label: "Votes / seat", align: "r", format: (v) => fmtInt(v) },
                ],
                sv.filter((r) => hl(r.party)),
                { sortKey: "seats" },
              ),
              { flush: true, categories: ["SIMULATED"], foot: "Seat bonus = seat share − vote share. Wasted = votes for losers plus winners' votes beyond what was needed." },
            ),
            card(
              "Seat–vote relationship",
              scatterChart({ points: sv.map((r) => ({ x: r.vote_share * 100, y: r.seat_share * 100, label: partyInfo(r.party).name, short: r.party, color: hl(r.party) ? partyInfo(r.party).color : "var(--uncalled)" })), xLabel: "Vote share (%)", yLabel: "Seat share (%)", xFormat: (v) => `${Math.round(v)}`, yFormat: (v) => `${Math.round(v)}`, diagonal: true, diagonalLabel: "seats = votes", height: 290, ariaLabel: "House seat share against vote share per party" }),
              { foot: "Single-member districts reward the largest parties: points above the line win more seats than votes." },
            ),
          ),
        );
      }
    }
    if (d.notes?.length) blocks.push(card("Reading guide", h("ul", { class: "ana-notes" }, d.notes.map((n) => h("li", null, n)))));
    const hostEl = h("div", { class: "ana-stack-v" }, h("div", { class: "ana-row ana-row--between" }, h("h2", { class: "ana-h2" }, `${d.election.year} · ${d.election.name}`), provBadge("SIMULATED")), ...blocks);
    currentAnalyticsHost = hostEl;
    return hostEl;
  }
  let currentAnalyticsHost = null;
  const resultHostOf = () => currentAnalyticsHost.parentElement;

  /* -------------------------------------------------------------- live → final */
  let lastStatus = null;
  cleanups.push(
    subscribe("night", (night) => {
      const s = night?.election_status || null;
      if (lastStatus && s && s !== lastStatus && (s === "final" || s === "certified")) {
        api.invalidate("/api/analytics");
        refresh(bodyHost, loadAll(), () => build());
      }
      lastStatus = s;
    }),
  );

  await refresh(bodyHost, loadAll(), () => build());
  return () => cleanups.forEach((f) => f());
}
