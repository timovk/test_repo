/**
 * #/municipalities — the 342-municipality election map with a metric switcher (winner, margin,
 * swing, turnout, reporting, party share, change vs previous, REAL density), a race selector
 * (President default; Governor, Senate, House, Legislature, Mayor, Council where the election
 * holds them), province zoom, hover tooltips, click-through, and a searchable sortable table.
 * Live elections re-poll the municipality rows every ~5 s while the night runs.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtCompact, fmtInt, fmtPP } from "../format.js";
import { setQuery } from "../router.js";
import { partyChip, provBadge } from "../components/badges.js";
import { dataTable } from "../components/table.js";
import { FAMILY_LABEL, electionContents, familiesOf, liveRefresh, marginLabel, parties, pc, reportingMeter, searchInput, segmented, selectControl, swatchLegend } from "../components/res-kit.js";
import { matches, muniColumns, muniLegend, muniMetrics, muniSpec, muniTooltip, scaleContext } from "../components/res-muni.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const MUNI_FAMILIES = ["PRES", "GOV", "SEN", "HOUSE", "PROVLEG", "MAYOR", "COUNCIL"];

/** Candidate names by (municipality row) → party for the family; display lookups only. */
async function nameResolver(id, fam, signal) {
  try {
    if (fam === "PRES") {
      const d = await api.get(`/api/elections/${id}/president`, { signal });
      const byParty = Object.fromEntries((d.tickets || []).map((t) => [t.party, t.president?.name || t.name]));
      const byKey = Object.fromEntries((d.tickets || []).map((t) => [t.key, { name: t.president?.name || t.name, party: t.party, color: t.color }]));
      return { of: () => byParty, byKey };
    }
    if (fam === "GOV") {
      const d = await api.get(`/api/elections/${id}/governors`, { signal });
      const m = {};
      for (const g of d.governors || []) m[g.province_code] = Object.fromEntries((g.lines || []).map((l) => [l.party, l.name]));
      return { of: (r) => m[r.province_code] || {} };
    }
    if (fam === "SEN") {
      const d = await api.get(`/api/elections/${id}/senate`, { signal });
      const m = {};
      for (const s of d.seats || []) if (s.race) m[s.province_code] = { ...(m[s.province_code] || {}), ...Object.fromEntries((s.race.lines || []).map((l) => [l.party, l.name])) };
      return { of: (r) => m[r.province_code] || {} };
    }
    if (fam === "MAYOR") {
      const d = await api.get(`/api/elections/${id}/mayors`, { signal });
      const m = {};
      for (const x of d.mayors || []) m[x.code] = Object.fromEntries((x.top || []).map((l) => [l.party, l.name]));
      return { of: (r) => m[r.code] || {} };
    }
    if (fam === "PROVLEG" || fam === "COUNCIL") {
      const p = await parties();
      const m = Object.fromEntries(p.list.map((x) => [x.code, x.name]));
      return { of: () => m };
    }
  } catch (err) {
    if (err?.name === "AbortError") throw err;
  }
  return { of: () => ({}) };
}

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const ctrl = new AbortController();
  const ui = { race: ctx.query.race, metric: ctx.query.map, party: ctx.query.party, prov: ctx.query.prov || "", q: "" };
  const frame = pageFrame(el, {
    eyebrow: "Results · Municipalities",
    title: "Municipalities",
    categories: ["SIMULATED"],
    electionId: id,
    noticeText: "The map shows REAL population density until results are reported.",
  });

  const contents = await electionContents(id);
  const fams = familiesOf(contents).filter((f) => MUNI_FAMILIES.includes(f));
  if (!fams.length) fams.push("PRES");
  if (!fams.includes(ui.race)) ui.race = fams[0];

  let data = null;
  let geo = null; // REAL municipality facts (density etc.)
  let provinces = [];
  let resolver = { of: () => ({}) };
  let lineVotes = null;
  let sctx = null;
  let map = null;
  let table = null;
  let lastSource = null;

  const provNames = () => Object.fromEntries(provinces.map((p) => [p.code, p.name]));
  const rows = () => data?.municipalities || [];
  const byCode = () => {
    if (!byCode.cache || byCode.data !== data) {
      byCode.cache = Object.fromEntries(rows().map((r) => [r.code, r]));
      byCode.data = data;
    }
    return byCode.cache;
  };
  const density = () => Object.fromEntries((geo || []).map((m) => [m.code, m.density]));
  let densityMap = {};

  // ---- slots
  const raceSlot = h("span");
  const metricSlot = h("span");
  const partySlot = h("span");
  const provSelect = selectControl([{ value: "", label: "All provinces" }], ui.prov, () => {}, { label: "Province" });
  mount(
    frame.toolbar,
    h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Race"), raceSlot),
    h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Map"), metricSlot),
    partySlot,
    h("span", { class: "res-toolbar__spacer" }),
    h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Zoom"), provSelect),
  );
  const mapCard = liveCard("Map", { id: "res-mun-map", categories: [provBadge("REAL", "Real boundaries")] });
  const mapHost = h("div", { class: "res-map-host" });
  const legendEl = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapHost, legendEl);
  const sideA = liveCard("Closest municipalities", { id: "res-mun-close" });
  const sideB = liveCard("Biggest swings", { id: "res-mun-swing" });
  const sideC = liveCard("Largest remaining", { id: "res-mun-rem" });
  const tableCard = liveCard("All municipalities", { id: "res-mun-table", flush: true, categories: [provBadge("REAL", "Population REAL"), provBadge("SIMULATED", "Results SIMULATED")] });
  const tableTools = h("div", { class: "res-card-tools res-card-tools--pad" });
  const tableHost = h("div");
  tableCard.body.append(tableTools, tableHost);
  mount(tableTools, searchInput("Search municipality, code or province…", (q) => {
    ui.q = q;
    paintTable(false);
  }, { label: "Search municipalities" }), h("span", { class: "res-muted-note", id: "res-mun-count" }));

  function buildBody() {
    mount(frame.body, h("div", { class: "res-grid res-grid--map" }, mapCard, h("div", { class: "res-stack" }, sideC, sideA, sideB)), tableCard);
  }

  // ---- map
  const fullCtx = () => ({ ...sctx, party: ui.party, density: densityMap });
  function specFor(f) {
    const r = byCode()[f.properties.code];
    if (!r) return ui.metric === "density" ? muniSpec({ code: f.properties.code }, "density", fullCtx()) : { none: true };
    const spec = muniSpec(r, ui.metric, fullCtx());
    if (ui.prov && f.properties.province_code !== ui.prov) spec.dim = true;
    return spec;
  }
  function tooltip(f) {
    const r = byCode()[f.properties.code];
    const pn = provNames()[f.properties.province_code];
    if (!r) return h("div", { class: "res-tip" }, h("div", { class: "res-tip__head" }, h("strong", null, f.properties.name)), h("div", { class: "res-tip__sub muted" }, pn || ""), h("div", { class: "muted", style: { fontSize: "12px" } }, `No ${FAMILY_LABEL[ui.race]} race here in this election.`));
    return muniTooltip(f.properties.name, r, fullCtx(), { provinceName: pn, names: resolver.of(r), lineVotes, population: r.population });
  }

  function paintControls() {
    const src = data.results_source;
    keyed(raceSlot, `${ui.race}|${fams.join()}`, () =>
      segmented(fams.map((f) => ({ value: f, label: FAMILY_LABEL[f] })), ui.race, (v) => {
        ui.race = v;
        setQuery({ race: v === fams[0] ? null : v });
        reload();
      }, { label: "Race" }),
    );
    const metrics = muniMetrics(src);
    if (!ui.metric || !metrics.some((m) => m.value === ui.metric)) ui.metric = metrics[0].value;
    keyed(metricSlot, `${src}|${ui.metric}`, () =>
      segmented(metrics, ui.metric, (v) => {
        ui.metric = v;
        setQuery({ map: v });
        paintControls();
        paintMap(false);
      }, { label: "Map metric" }),
    );
    const ps = (sctx.parties || []).slice().sort((a, b) => (sctx.maxShare[b] || 0) - (sctx.maxShare[a] || 0));
    if (!ui.party || !ps.includes(ui.party)) ui.party = ps[0];
    keyed(partySlot, `${ui.metric}|${ui.party}|${ps.join()}`, () =>
      ui.metric === "share" && ps.length
        ? h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Party"), segmented(ps.map((p) => ({ value: p, label: p })), ui.party, (v) => {
            ui.party = v;
            setQuery({ party: v });
            paintControls();
            paintMap(false);
          }, { label: "Party for the share map" }))
        : null,
    );
  }

  function paintMap(first) {
    const src = data.results_source;
    const legend = muniLegend(ui.metric, rows(), fullCtx(), src);
    const missing = rows().length && rows().length < 342 && ui.metric !== "density";
    mount(legendEl, legend, missing ? swatchLegend([{ kind: "none", label: `No ${FAMILY_LABEL[ui.race]} race` }]) : null);
    mapCard.setTitle(`${FAMILY_LABEL[ui.race]} · ${metricTitle()}`);
    if (first || !map) {
      map?.destroy();
      mount(mapHost);
      map = resMap(mapHost, {
        layer: "municipalities",
        outline: "provinces",
        height: 660,
        label: "Map of the 342 municipalities; the table below lists the same results",
        spec: specFor,
        tooltip,
        onClick: (f) => (location.hash = links.municipality(f.properties.code)),
      });
      map.ready.then(() => zoom()).catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    } else map.update();
  }
  function metricTitle() {
    const src = data?.results_source;
    return { winner: src === "final" ? "Winner" : "Leader", margin: "Margin", swing: "Swing", turnout: "Turnout", reporting: "Reporting", share: `${ui.party} share`, change: "Change vs previous", density: "Population density (REAL)" }[ui.metric] || ui.metric;
  }
  function zoom() {
    if (!map) return;
    if (ui.prov) map.fitTo((f) => f.properties.province_code === ui.prov);
    else map.fitTo(() => true);
    map.update();
  }

  // ---- side lists (sorting API values only)
  function rankList(items, value, empty) {
    return items.length
      ? h(
          "ol",
          { class: "res-rank" },
          items.map((r) =>
            h(
              "li",
              null,
              h("a", { href: links.municipality(r.code), class: "res-rank__name" }, r.name),
              h("span", { class: "res-code" }, r.province_code),
              r.leader_party ? partyChip(r.leader_party, { color: pc(r.leader_party, r.leader_color) }) : null,
              h("span", { class: "res-rank__val num" }, value(r)),
            ),
          ),
        )
      : h("p", { class: "muted res-empty" }, empty);
  }
  function paintSide() {
    const src = data.results_source;
    const vis = rows().filter((r) => !ui.prov || r.province_code === ui.prov);
    const withVotes = vis.filter((r) => r.votes);
    const closest = withVotes.filter((r) => r.margin_pp !== null && r.leader_party).sort((a, b) => a.margin_pp - b.margin_pp).slice(0, 6);
    const swings = withVotes.filter((r) => r.swing_pp !== null && r.swing_pp !== undefined).sort((a, b) => Math.abs(b.swing_pp) - Math.abs(a.swing_pp)).slice(0, 6);
    const remaining = vis.filter((r) => r.outstanding_est > 0).sort((a, b) => b.outstanding_est - a.outstanding_est).slice(0, 6);
    const hiddenMsg = "Shown once results are reported.";
    keyed(sideA.body, JSON.stringify(closest.map((r) => [r.code, r.margin_pp])) + src, () => rankList(closest, (r) => marginLabel(null, r.margin_pp), src === "hidden" ? hiddenMsg : "No votes counted yet."));
    keyed(sideB.body, JSON.stringify(swings.map((r) => [r.code, r.swing_pp])) + src, () => rankList(swings, (r) => `${fmtPP(r.swing_pp)} pp`, src === "hidden" ? hiddenMsg : "No comparable previous result."));
    sideC.hidden = src !== "live";
    if (src === "live")
      keyed(sideC.body, JSON.stringify(remaining.map((r) => [r.code, r.outstanding_est])), () =>
        remaining.length
          ? h(
              "ol",
              { class: "res-rank" },
              remaining.map((r) =>
                h("li", null, h("a", { href: links.municipality(r.code), class: "res-rank__name" }, r.name), r.leader_party ? partyChip(r.leader_party, { color: pc(r.leader_party, r.leader_color) }) : h("span", { class: "muted" }, "–"), reportingMeter(r.reporting_pct, { width: 36 }), h("span", { class: "res-rank__val num", title: "Estimated outstanding ballots" }, fmtCompact(r.outstanding_est))),
              ),
            )
          : h("p", { class: "muted res-empty" }, "Every municipality has reported in full."),
      );
  }

  // ---- table
  function paintTable(rebuild) {
    const src = data.results_source;
    const ps = (sctx.parties || []).slice().sort((a, b) => (sctx.maxShare[b] || 0) - (sctx.maxShare[a] || 0));
    const pn = provNames();
    const vis = rows().filter((r) => (!ui.prov || r.province_code === ui.prov) && matches(r, ui.q, pn));
    if (rebuild || !table) {
      const cols = muniColumns(src, sctx, { link: links.municipality, provinceNames: pn, showProvince: true, parties: ps });
      if (src === "hidden") cols.push({ key: "density", label: "Density", align: "r", value: (r) => densityMap[r.code] ?? null, format: (v) => (v ? `${fmtInt(v)}/km²` : "–") });
      table = dataTable(cols, vis, { sortKey: "population", sortDir: "desc", maxHeight: 620, rowHref: (r) => links.municipality(r.code), rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: "Municipality results" });
      mount(tableHost, table);
    } else table.update(vis);
    const note = tableTools.querySelector("#res-mun-count");
    if (note) note.textContent = `${fmtInt(vis.length)} of ${fmtInt(rows().length)} municipalities · click a row for details`;
    tableCard.setTitle(`All municipalities · ${FAMILY_LABEL[ui.race]}`);
  }

  function paint(first) {
    frame.setSource(data);
    sctx = scaleContext(rows(), data.colors || {});
    paintControls();
    paintMap(first);
    paintSide();
    paintTable(first);
  }

  // ---- data
  const fetchRows = () => api.get(`/api/elections/${id}/municipalities?race=${ui.race}`, { signal: ctrl.signal });
  async function fetchLineVotes(src) {
    if (ui.race !== "PRES" || src === "hidden") return null;
    try {
      if (src === "live") {
        const n = await api.get(`/api/night/${id}/municipalities?race=PRES`, { signal: ctrl.signal });
        const out = {};
        for (const m of n.municipalities || [])
          out[m.code] = Object.entries(m.votes || {}).map(([k, v]) => ({ name: resolver.byKey?.[k]?.name || k, party: resolver.byKey?.[k]?.party, color: resolver.byKey?.[k]?.color, votes: v, pct: m.shares?.[k] !== undefined ? m.shares[k] * 100 : null }));
        return out;
      }
      const d = await api.get(`/api/elections/${id}/races/PRES`, { signal: ctrl.signal });
      const lines = Object.fromEntries((d.race?.lines || []).map((l) => [l.key, { name: l.president?.name || l.candidate?.name || l.name, party: l.party, color: l.color }]));
      const out = {};
      for (const m of d.municipalities || []) out[m.code] = Object.entries(m.votes || {}).map(([k, v]) => ({ name: lines[k]?.name || k, party: lines[k]?.party, color: lines[k]?.color, votes: v, pct: m.pct?.[k] ?? null }));
      return out;
    } catch (err) {
      if (err?.name === "AbortError") throw err;
      return null;
    }
  }

  let token = 0;
  async function reload() {
    const my = ++token;
    try {
      const [d, g, p, res] = await Promise.all([fetchRows(), api.get("/api/municipalities", { cache: true }).catch(() => null), api.get("/api/provinces", { cache: true }), nameResolver(id, ui.race, ctrl.signal)]);
      if (my !== token) return;
      data = d;
      geo = g?.municipalities || [];
      densityMap = density();
      provinces = p.provinces || [];
      resolver = res;
      lastSource = d.results_source;
      frame.setTitle("Municipalities", `Results · ${d.election?.name || ""}`);
      if (provSelect.options.length <= 1) {
        for (const pr of provinces) provSelect.append(h("option", { value: pr.code, selected: pr.code === ui.prov }, pr.name));
        provSelect.onchange = (e) => {
          ui.prov = e.target.value;
          setQuery({ prov: ui.prov || null });
          zoom();
          paintSide();
          paintTable(false);
        };
      }
      if (!frame.body.contains(mapCard)) buildBody();
      table = null;
      paint(true);
      lineVotes = await fetchLineVotes(d.results_source);
    } catch (err) {
      if (err?.name !== "AbortError") frame.error(err);
    }
  }

  await reload();
  const stopLive = liveRefresh(
    id,
    async () => {
      const d = await fetchRows();
      const changed = d.results_source !== lastSource;
      lastSource = d.results_source;
      data = d;
      paint(changed);
      lineVotes = await fetchLineVotes(d.results_source);
    },
    { interval: 5000 },
  );

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}

