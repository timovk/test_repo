/**
 * #/provinces — the 12 provinces: geographic map + sortable table for one province-wide race
 * family (President by default; Governor / Senate / Legislature when the election holds them).
 * Hidden elections show the pre-election state (EV, seats, previous holders); live elections
 * refresh from the night at a modest cadence; final elections show the stored result.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtCompact, fmtInt, fmtPct, fmtProb } from "../format.js";
import { setQuery } from "../router.js";
import { partyChip, provBadge, statusPill } from "../components/badges.js";
import { dataTable } from "../components/table.js";
import {
  FAMILY_LABEL,
  cssVar,
  electionContents,
  familiesOf,
  flipTag,
  liveRefresh,
  marginLabel,
  parties,
  partyShade,
  partyTag,
  pc,
  raceColor,
  racePill,
  raceState,
  rampLegend,
  reportingMeter,
  segmented,
  seqColor,
  seqRamp,
  swatchLegend,
} from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const PROVINCE_FAMILIES = ["PRES", "GOV", "SEN", "PROVLEG"];
const STATUS_ORDER = ["FINAL", "CALLED", "PROJECTED_WINNER", "LEAN", "TOO_CLOSE_TO_CALL", "RECOUNT", "TOO_EARLY_TO_CALL", "POLLS_CLOSED", "SCHEDULED"];

/** key → display name for the family's ballot lines (names never depend on results). */
async function nameMap(id, fam, signal) {
  const out = {};
  try {
    if (fam === "PRES") {
      const d = await api.get(`/api/elections/${id}/president`, { signal });
      for (const t of d.tickets || []) out[t.key] = { name: t.president?.name || t.name, full: t.name, party: t.party };
    } else if (fam === "GOV") {
      const d = await api.get(`/api/elections/${id}/governors`, { signal });
      for (const g of d.governors || []) for (const l of g.lines || []) out[l.key] = { name: l.name, full: l.running_mate_name ? `${l.name} / ${l.running_mate_name}` : l.name, party: l.party };
    } else if (fam === "SEN") {
      const d = await api.get(`/api/elections/${id}/senate`, { signal });
      for (const s of d.seats || []) for (const l of s.race?.lines || []) out[l.key] = { name: l.name, full: l.name, party: l.party };
    } else {
      const p = await parties();
      for (const x of p.list) out[x.code] = { name: x.name, full: x.name, party: x.code };
    }
  } catch (err) {
    if (err?.name === "AbortError") throw err;
  }
  return out;
}

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const ctrl = new AbortController();
  const ui = { race: ctx.query.race, mode: ctx.query.map };
  const frame = pageFrame(el, {
    eyebrow: "Results · Provinces",
    title: "Provinces",
    categories: ["SIMULATED"],
    electionId: id,
    noticeText: "The map and table show electoral votes, House seats, real population and the previous holders.",
  });
  let map = null;
  let stopLive = () => {};
  let table = null;
  let data = null;
  let names = {};
  let geo = null;

  const contents = await electionContents(id);
  const fams = familiesOf(contents).filter((f) => PROVINCE_FAMILIES.includes(f));
  if (!fams.length) fams.push("PRES");
  if (!fams.includes(ui.race)) ui.race = fams[0];

  // Slots (built once, updated in place).
  const mapCard = liveCard("Map", { categories: [provBadge("REAL", "Real boundaries")], id: "res-prov-map" });
  const mapHost = h("div", { class: "res-map-host" });
  const legendEl = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapHost, legendEl);
  const statusCard = liveCard("Race status", { id: "res-prov-status" });
  const closeCard = liveCard("Closest provinces", { id: "res-prov-close" });
  const flipCard = liveCard("Flips", { id: "res-prov-flips" });
  const tableCard = liveCard("All provinces", { flush: true, id: "res-prov-table", categories: [provBadge("REAL", "Population REAL"), provBadge("FICTIONAL", "EV & seats FICTIONAL")] });
  const modeSlot = h("span");
  const raceSeg = segmented(
    fams.map((f) => ({ value: f, label: FAMILY_LABEL[f] })),
    ui.race,
    (v) => {
      ui.race = v;
      setQuery({ race: v === fams[0] ? null : v });
      reload();
    },
    { label: "Race" },
  );
  mount(frame.toolbar, h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Race"), raceSeg), h("div", { class: "res-toolbar__group" }, h("span", { class: "res-toolbar__label" }, "Map"), modeSlot));

  const modesFor = (src) => {
    const m = [
      { value: "winner", label: src === "final" ? "Winner" : "Leader" },
      { value: "margin", label: "Margin" },
      { value: "turnout", label: "Turnout" },
      { value: "reporting", label: "Reporting" },
      { value: "flips", label: "Flips" },
      { value: "previous", label: "Previous" },
    ];
    if (src === "hidden") return m.filter((x) => x.value === "previous" || x.value === "winner");
    if (src === "final") return m.filter((x) => x.value !== "reporting");
    return m.filter((x) => x.value !== "turnout"); // live: turnout is published with the final result
  };

  const rowsByCode = () => Object.fromEntries((data?.provinces || []).map((r) => [r.code, r]));
  const nameOf = (key) => names[key]?.name || key || "";

  function specFor(f) {
    const code = f.properties.code;
    const r = rowsByCode()[code];
    if (!r) return { none: true };
    const col = raceColor(r);
    switch (ui.mode) {
      case "margin":
        return col && r.margin_pp !== null ? { fill: partyShade(col, (r.margin_pp || 0) / 20) } : {};
      case "turnout":
        return r.turnout_pct !== null ? { fill: seqColor(r.turnout_pct, turnoutRange()[0], turnoutRange()[1]) } : {};
      case "reporting":
        return r.reporting_pct !== null ? { fill: seqColor(r.reporting_pct, 0, 100) } : {};
      case "flips":
        if (r.flip_status === "flip") return { fill: col };
        if (r.flip_status === "hold") return { fill: cssVar("--uncalled"), state: "called" };
        return {};
      case "previous":
        return r.previous_party ? { fill: pc(r.previous_party, data?.colors?.[r.previous_party]), state: "lean" } : {};
      default:
        return col ? { fill: col, state: raceState(r) } : {};
    }
  }

  function turnoutRange() {
    const v = (data?.provinces || []).map((r) => r.turnout_pct).filter((x) => x !== null && x !== undefined);
    if (!v.length) return [0, 100];
    return [Math.floor(Math.min(...v)), Math.ceil(Math.max(...v))];
  }

  function tooltip(f) {
    const code = f.properties.code;
    const r = rowsByCode()[code];
    const g = geo?.byCode?.[code];
    const lines = r?.pct ? Object.entries(r.pct).sort((a, b) => b[1] - a[1]).slice(0, 4) : [];
    return h(
      "div",
      { class: "res-tip" },
      h("div", { class: "res-tip__head" }, h("strong", null, f.properties.name), r ? racePill(r) : h("span", { class: "muted" }, "No race")),
      h("div", { class: "res-tip__sub muted" }, [r?.ev ? `${r.ev} EV` : null, g ? `${g.house_seats} House seats` : null, g ? `pop. ${fmtCompact(g.population)}` : null].filter(Boolean).join(" · ")),
      lines.length
        ? h(
            "div",
            { class: "res-tip__rows" },
            lines.map(([k, v]) =>
              h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(names[k]?.party || k, data?.colors?.[names[k]?.party || k]) } }), h("span", { class: "res-tip__name" }, nameOf(k)), h("b", { class: "num" }, fmtPct(v))),
            ),
          )
        : null,
      r
        ? h(
            "div",
            { class: "res-tip__kv" },
            r.margin_pp !== null ? h("span", null, "Margin ", h("b", null, marginLabel(r.winner_party || r.leader_party, r.margin_pp))) : null,
            r.turnout_pct !== null ? h("span", null, "Turnout ", h("b", null, fmtPct(r.turnout_pct))) : null,
            r.reporting_pct !== null ? h("span", null, "Reporting ", h("b", null, fmtPct(r.reporting_pct))) : null,
            r.win_probability !== null && r.win_probability !== undefined && !["FINAL"].includes(r.status) ? h("span", null, "Call model ", h("b", null, fmtProb(r.win_probability))) : null,
            r.previous_party ? h("span", null, "Previously ", h("b", null, r.previous_party), " ", r.flip_status ? flipTag(r.flip_status) : null) : null,
          )
        : null,
    );
  }

  function legendFor() {
    const src = data?.results_source;
    const rows = data?.provinces || [];
    const colors = data?.colors || {};
    const partiesIn = [...new Set(rows.map((r) => r.winner_party || r.leader_party).filter(Boolean))];
    switch (ui.mode) {
      case "margin":
        return rampLegend([0.1, 0.3, 0.5, 0.7, 0.9].map((t) => partyShade(cssVar("--text-secondary"), t)), ["0", "4", "8", "12", "16", "20+ pp"], { title: "Margin (party hue, darker = wider)" });
      case "turnout": {
        const [lo, hi] = turnoutRange();
        return rampLegend(seqRamp(), [`${lo}%`, `${hi}%`], { title: "Turnout" });
      }
      case "reporting":
        return rampLegend(seqRamp(), ["0%", "50%", "100%"], { title: "Expected vote reporting" });
      case "flips":
        return swatchLegend([...partiesIn.map((p) => ({ color: pc(p, colors[p]), label: `Flipped to ${p}` })), { color: cssVar("--uncalled"), label: "Held" }]);
      case "previous": {
        const prev = [...new Set(rows.map((r) => r.previous_party).filter(Boolean))];
        return swatchLegend(prev.map((p) => ({ color: partyShade(pc(p, colors[p]), 0.5), label: p })), { title: "Previous holder" });
      }
      default:
        return h(
          "div",
          { class: "res-legend-stack" },
          swatchLegend(partiesIn.map((p) => ({ color: pc(p, colors[p]), label: p }))),
          src === "live"
            ? swatchLegend(
                [
                  { color: cssVar("--text-secondary"), label: "Called / final" },
                  { color: partyShade(cssVar("--text-secondary"), 0.3), label: "Leading" },
                  { kind: "close", label: "Too close (amber edge)" },
                  { color: cssVar("--uncalled"), label: "No result yet" },
                ],
                { title: "State" },
              )
            : null,
        );
    }
  }

  function renderSide() {
    const rows = data?.provinces || [];
    const src = data?.results_source;
    // Status breakdown (grouping of API statuses).
    const counts = {};
    for (const r of rows) counts[r.status] = (counts[r.status] || 0) + 1;
    keyed(statusCard.body, JSON.stringify(counts) + ui.race, () =>
      h(
        "ul",
        { class: "res-statuslist" },
        STATUS_ORDER.filter((s) => counts[s]).map((s) => h("li", null, statusPill(s), h("span", { class: "res-statuslist__n num" }, String(counts[s])))),
        rows.length < 12 ? h("li", null, h("span", { class: "pill pill--closed" }, "No race"), h("span", { class: "res-statuslist__n num" }, String(12 - rows.length))) : null,
      ),
    );
    const closest = rows.filter((r) => r.margin_pp !== null && r.margin_pp !== undefined && (r.winner_party || r.leader_party) && (r.reporting_pct ?? 100) > 0).sort((a, b) => a.margin_pp - b.margin_pp).slice(0, 5);
    keyed(closeCard.body, JSON.stringify(closest.map((r) => [r.code, r.margin_pp, r.status])), () =>
      closest.length
        ? h(
            "ol",
            { class: "res-rank" },
            closest.map((r) =>
              h(
                "li",
                null,
                h("a", { href: links.province(r.code), class: "res-rank__name" }, r.name),
                partyChip(r.winner_party || r.leader_party, { color: raceColor(r) }),
                h("span", { class: "res-rank__val num" }, marginLabel(null, r.margin_pp)),
                racePill(r),
              ),
            ),
          )
        : h("p", { class: "muted res-empty" }, src === "hidden" ? "Margins appear once votes are counted." : "No votes counted yet."),
    );
    const flips = rows.filter((r) => r.flip_status === "flip");
    keyed(flipCard.body, JSON.stringify(flips.map((r) => [r.code, r.winner_party || r.leader_party])), () =>
      flips.length
        ? h(
            "ul",
            { class: "res-rank" },
            flips.map((r) => h("li", null, h("a", { href: links.province(r.code), class: "res-rank__name" }, r.name), h("span", { class: "muted" }, r.previous_party || "–"), h("span", { "aria-hidden": "true", class: "muted" }, "→"), partyChip(r.winner_party || r.leader_party, { color: raceColor(r) }))),
          )
        : h("p", { class: "muted res-empty" }, src === "hidden" ? "Flips are shown once races are decided." : "No province has changed hands" + (src === "live" ? " so far." : ".")),
    );
  }

  function tableRows() {
    const rows = rowsByCode();
    return (geo?.list || []).map((g) => ({ g, r: rows[g.code] || null, code: g.code, name: g.name }));
  }

  function columns() {
    const src = data?.results_source;
    const cols = [
      { key: "name", label: "Province", format: (v, x) => h("a", { href: links.province(x.code), class: "res-link" }, h("span", { class: "res-code" }, x.code), v) },
      ui.race === "PRES" ? { key: "ev", label: "EV", align: "r", value: (x) => x.g.electoral_votes, format: (v) => h("b", null, fmtInt(v)) } : null,
      { key: "seats", label: "House", align: "r", value: (x) => x.g.house_seats },
      { key: "pop", label: "Population", align: "r", value: (x) => x.g.population, format: (v) => fmtInt(v) },
      {
        key: "leader",
        label: src === "final" ? "Winner" : "Leader",
        value: (x) => (x.r ? x.r.winner_party || x.r.leader_party || "" : ""),
        format: (v, x) => (x.r && (x.r.winner || x.r.leader) ? partyTag(x.r.winner_party || x.r.leader_party, raceColor(x.r), nameOf(x.r.winner || x.r.leader)) : h("span", { class: "muted" }, x.r ? "–" : "No race")),
      },
      { key: "status", label: "Status", value: (x) => (x.r ? STATUS_ORDER.length - STATUS_ORDER.indexOf(x.r.status) : -1), format: (v, x) => (x.r ? racePill(x.r) : h("span", { class: "muted" }, "–")) },
      { key: "margin", label: "Margin", align: "r", value: (x) => x.r?.margin_pp ?? null, format: (v, x) => marginLabel(null, x.r?.margin_pp) },
      src !== "live" ? { key: "turnout", label: "Turnout", align: "r", value: (x) => x.r?.turnout_pct ?? null, format: (v) => fmtPct(v) } : null,
      src !== "final" ? { key: "reporting", label: "Reporting", value: (x) => x.r?.reporting_pct ?? null, format: (v) => reportingMeter(v) } : null,
      { key: "flip", label: "Flip / hold", value: (x) => x.r?.flip_status || "", format: (v, x) => (x.r?.flip_status ? flipTag(x.r.flip_status, { prev: x.r.previous_party }) : h("span", { class: "muted" }, "–")) },
      { key: "prev", label: "Previous", value: (x) => x.r?.previous_party || "", format: (v, x) => (x.r?.previous_party ? partyChip(x.r.previous_party, { color: pc(x.r.previous_party, data?.colors?.[x.r.previous_party]) }) : h("span", { class: "muted" }, "–")) },
    ];
    return cols.filter(Boolean);
  }

  function buildBody() {
    mount(
      frame.body,
      h("div", { class: "res-grid res-grid--map" }, mapCard, h("div", { class: "res-stack" }, statusCard, closeCard, flipCard)),
      tableCard,
    );
    table = null;
  }

  function paint(first) {
    frame.setSource(data);
    const src = data.results_source;
    const modes = modesFor(src);
    if (!ui.mode || !modes.some((m) => m.value === ui.mode)) ui.mode = src === "hidden" ? "previous" : "winner";
    keyed(modeSlot, `${src}|${ui.mode}`, () =>
      segmented(modes, ui.mode, (v) => {
        ui.mode = v;
        setQuery({ map: v });
        map?.update();
        mount(legendEl, legendFor());
        mapCard.setTitle(mapTitle());
      }, { label: "Map metric" }),
    );
    mapCard.setTitle(mapTitle());
    renderSide();
    mount(legendEl, legendFor());
    if (!table) {
      table = dataTable(columns(), tableRows(), { sortKey: ui.race === "PRES" ? "ev" : "pop", sortDir: "desc", rowHref: (x) => links.province(x.code), caption: "Province results" });
      mount(tableCard.body, table);
    } else table.update(tableRows());
    tableCard.setTitle(`All provinces · ${FAMILY_LABEL[ui.race]}`);
    if (first || !map) {
      map?.destroy();
      mount(mapHost);
      map = resMap(mapHost, {
        layer: "provinces",
        outline: null,
        height: 520,
        label: `Map of the 12 provinces — ${FAMILY_LABEL[ui.race]}; the table below lists the same data`,
        spec: specFor,
        tooltip,
        onClick: (f) => (location.hash = links.province(f.properties.code)),
      });
      map.ready.catch((err) => mount(mapHost, h("div", { class: "state" }, "Map unavailable: ", err.message)));
    } else map.update();
  }

  function mapTitle() {
    const m = { winner: data?.results_source === "final" ? "Winner" : "Leader", margin: "Margin", turnout: "Turnout", reporting: "Reporting", flips: "Flips", previous: "Previous holder" }[ui.mode];
    return `${FAMILY_LABEL[ui.race]} · ${m}`;
  }

  async function fetchData() {
    return api.get(`/api/elections/${id}/provinces?race=${ui.race}`, { signal: ctrl.signal });
  }

  let lastSource = null;
  async function reload() {
    try {
      const [d, n, g] = await Promise.all([fetchData(), nameMap(id, ui.race, ctrl.signal), api.get("/api/provinces", { cache: true })]);
      data = d;
      names = n;
      geo = { list: g.provinces, byCode: Object.fromEntries(g.provinces.map((p) => [p.code, p])) };
      frame.setTitle("Provinces", `Results · ${d.election?.name || ""}`);
      if (!frame.body.contains(mapCard)) buildBody();
      table = null;
      lastSource = d.results_source;
      paint(true);
    } catch (err) {
      if (err?.name !== "AbortError") frame.error(err);
    }
  }

  await reload();
  stopLive = liveRefresh(id, async () => {
    const d = await fetchData();
    if (d.results_source !== lastSource) {
      lastSource = d.results_source;
      data = d;
      table = null;
      paint(false);
      return;
    }
    data = d;
    paint(false);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}
