/**
 * #/provinces/:code — province election-night page: headline race (EV, leader/winner, status,
 * reporting, candidate totals, margin, turnout), municipality map + sortable table, largest
 * remaining municipalities and estimated outstanding vote (live), swing and previous result,
 * the province's House / Senate / Governor / Legislature races, and REAL province facts.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtCompact, fmt1, fmtInt, fmtPct, fmtPP } from "../format.js";
import { setQuery } from "../router.js";
import { partyChip, provBadge } from "../components/badges.js";
import { divBars } from "../components/res-charts.js";
import { dataTable } from "../components/table.js";
import {
  facts,
  flipTag,
  liveRefresh,
  marginLabel,
  partyTag,
  pc,
  raceColor,
  racePill,
  reportingMeter,
  resultRows,
  searchInput,
  segmented,
  tile,
} from "../components/res-kit.js";
import { matches, muniColumns, muniLegend, muniMetrics, muniSpec, muniTooltip, scaleContext } from "../components/res-muni.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const code = String(params.code || "").toUpperCase();
  const ctrl = new AbortController();
  const ui = { metric: ctx.query.map || null, q: "", party: ctx.query.party || null };
  const frame = pageFrame(el, { eyebrow: `Province · ${code}`, title: code, categories: ["SIMULATED"], electionId: id });

  let data = null;
  let geo = null;
  let map = null;
  let table = null;
  let lastSource = null;
  let sctx = null;
  let density = {};

  // ---- slots
  const heroCard = liveCard("Headline race", { id: "res-pv-hero" });
  const heroTiles = h("div", { class: "res-tiles res-hero-tiles" });
  const heroLines = h("div", { class: "res-hero-lines" });
  heroCard.body.append(heroTiles, heroLines);
  const heroFoot = h("div", { class: "card__foot res-hero-foot" });
  heroCard.append(heroFoot);
  const factsCard = liveCard("Province facts", { id: "res-pv-facts", categories: [provBadge("REAL")] });
  const mapCard = liveCard("Municipalities", { id: "res-pv-map", categories: [provBadge("REAL", "Real boundaries")] });
  const mapTools = h("div", { class: "res-card-tools" });
  const mapHost = h("div", { class: "res-map-host" });
  const legendEl = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapTools, mapHost, legendEl);
  const swingCard = liveCard("Swing & previous result", { id: "res-pv-swing" });
  const outCard = liveCard("Outstanding vote", { id: "res-pv-out" });
  const tableCard = liveCard("Municipality results", { id: "res-pv-table", flush: true });
  const tableTools = h("div", { class: "res-card-tools res-card-tools--pad" });
  const tableHost = h("div");
  tableCard.body.append(tableTools, tableHost);
  const houseCard = liveCard("House races", { id: "res-pv-house", flush: true, categories: [provBadge("FICTIONAL", "Fictional districts")] });
  const statewideHost = h("div", { class: "res-grid res-grid--races" });

  const rows = () => data?.municipalities?.rows || [];
  const byCode = () => Object.fromEntries(rows().map((r) => [r.code, r]));
  /** party → candidate name on the headline ballot (display lookup only). */
  const namesByParty = () => Object.fromEntries((data?.headline?.lines || []).filter((l) => l.party).map((l) => [l.party, (l.candidate || l.president)?.name || l.name]));

  function buildBody() {
    mount(
      frame.body,
      h("div", { class: "res-grid res-grid--hero" }, heroCard, factsCard),
      h("div", { class: "res-grid res-grid--map" }, mapCard, h("div", { class: "res-stack" }, outCard, swingCard)),
      tableCard,
      h("div", { class: "res-section-title" }, h("h2", null, "Other races in ", h("span", { class: "res-pv-name" }, geo?.name || code))),
      statewideHost,
      houseCard,
    );
  }

  // ---- hero
  function paintHero() {
    const r = data.headline;
    const src = data.results_source;
    if (!r) {
      heroCard.setTitle("Headline race");
      mount(heroTiles);
      mount(heroLines, h("p", { class: "muted res-empty" }, "No province-wide race in this election."));
      return;
    }
    heroCard.setTitle(r.name);
    keyed(heroCard.meta, `${r.status}|${raceColor(r)}`, () => racePill(r));
    const leaderParty = r.winner_party || r.leader_party;
    const leaderName = r.winner_name || r.leader_name;
    const t = [
      r.electoral_votes ? tile("Electoral votes", String(r.electoral_votes), "winner-take-all · FICTIONAL") : null,
      tile(src === "final" || r.winner ? "Winner" : "Leader", leaderParty ? partyChip(leaderParty, { color: raceColor(r) }) : "–", leaderName || (src === "hidden" ? "Results hidden" : "No votes counted"), { accent: raceColor(r) || undefined }),
      tile("Margin", r.margin_pp !== null && r.margin_pp !== undefined ? `${fmtPP(r.margin_pp)} pp` : "–", r.margin_votes !== null && r.margin_votes !== undefined ? `${fmtInt(r.margin_votes)} votes` : "winner − runner-up"),
      src === "final" ? tile("Turnout", fmtPct(r.turnout_pct), r.ballots_cast ? `${fmtInt(r.ballots_cast)} ballots` : null) : tile("Reporting", r.reporting_pct !== null && r.reporting_pct !== undefined ? fmtPct(r.reporting_pct) : "–", "of expected vote"),
      src === "live" ? tile("Est. outstanding", data.outstanding?.votes_est !== undefined ? fmtCompact(data.outstanding.votes_est) : "–", "ballots still to count") : tile("Votes counted", fmtCompact(r.total_votes), r.eligible ? `${fmtCompact(r.eligible)} eligible` : null),
    ].filter(Boolean);
    keyed(heroTiles, JSON.stringify([r.status, leaderParty, leaderName, r.margin_pp, r.reporting_pct, r.turnout_pct, r.total_votes, data.outstanding?.votes_est]), () => t);
    const rowsOpts = { leaderKey: r.leader, mateLabel: r.type === "GOVERNOR" ? "Lt. Gov." : "with", seats: r.seats_won };
    if (!heroLines.firstChild || !heroLines.firstChild.update) mount(heroLines, resultRows(r.lines || [], rowsOpts));
    else heroLines.firstChild.update(r.lines || [], rowsOpts);
    const inc = r.incumbent;
    keyed(heroFoot, JSON.stringify([inc, r.previous_party, r.decided_by, r.called_at, r.flip_status, r.open_seat]), () => [
      inc ? h("span", null, "Incumbent: ", h("b", null, inc.name), ` (${inc.party})`, inc.running === false ? " · not running" : "") : h("span", null, "No incumbent"),
      r.previous_party ? h("span", null, " · Previously held by ", h("b", null, r.previous_party)) : null,
      r.flip_status ? h("span", null, " · ", flipTag(r.flip_status, { prev: r.previous_party })) : null,
      r.decided_by ? h("span", null, ` · Decided by ${String(r.decided_by).replace(/_/g, " ")}`) : null,
      r.called_at ? h("span", null, ` · Called ${String(r.called_at).slice(11, 16)}`) : null,
    ]);
  }

  // ---- facts (REAL / DERIVED / FICTIONAL)
  function paintFacts() {
    const g = geo;
    if (!g) return;
    const d = g.demographics || {};
    mount(
      factsCard.body,
      facts(
        [
          { label: "Population", value: fmtInt(g.population), cat: "REAL", note: `CBS ${g.population_official ? `official ${fmtInt(g.population_official)}` : ""}` },
          { label: "Area", value: `${fmtInt(g.area_km2)} km²`, cat: "REAL", note: `${fmtInt(g.land_area_km2)} km² land` },
          { label: "Density", value: `${fmtInt(g.density)} /km²`, cat: "DERIVED" },
          { label: "Municipalities", value: fmtInt(g.municipalities), cat: "REAL", note: `${fmtInt(g.unit_count)} neighbourhoods` },
          { label: "Eligible voters", value: fmtInt(g.eligible_voters_est), cat: "DERIVED", note: "estimated" },
          { label: "Capital", value: g.capital || "–", cat: "REAL" },
        ],
        { cols: 3 },
      ),
      h("div", { class: "res-divider" }),
      facts(
        [
          { label: "Electoral votes", value: fmtInt(g.electoral_votes), cat: "FICTIONAL" },
          { label: "House seats", value: fmtInt(g.house_seats), cat: "FICTIONAL" },
          { label: "Senators", value: fmtInt(g.senators), cat: "FICTIONAL" },
          { label: "Governor", value: g.governor ? h("span", { class: "res-facts__person" }, partyChip(g.governor.party, { color: pc(g.governor.party) }), g.governor.name) : "–", note: g.governor ? `term to ${String(g.governor.term_end || "").slice(0, 4)}` : null },
          { label: "Lt. Governor", value: g.lieutenant_governor ? h("span", { class: "res-facts__person" }, partyChip(g.lieutenant_governor.party, { color: pc(g.lieutenant_governor.party) }), g.lieutenant_governor.name) : "–" },
          { label: "Legislature", value: g.legislature ? `${g.legislature.seats} seats` : "–", note: g.legislature ? String(g.legislature.electoral_system || "").replace(/_/g, " ") : null },
        ],
        { cols: 3 },
      ),
      h("div", { class: "res-divider" }),
      facts(
        [
          { label: "Aged 65+", value: fmtPct(d.pct_age_65_plus), cat: "DERIVED" },
          { label: "Non-European origin", value: fmtPct(d.pct_origin_non_europe), cat: "DERIVED" },
          { label: "Higher education", value: fmtPct(d.pct_education_high), cat: "DERIVED" },
          { label: "Income / capita", value: d.income_per_capita_keur !== undefined ? `€${fmt1(d.income_per_capita_keur)}k` : "–", cat: "DERIVED" },
          { label: "Owner-occupied", value: fmtPct(d.pct_owner_occupied), cat: "DERIVED" },
          { label: "Single households", value: fmtPct(d.pct_single_households), cat: "DERIVED" },
        ],
        { cols: 3 },
      ),
      h("p", { class: "res-muted-note" }, "Demographics are population-weighted means of municipal CBS indicators (DERIVED). Offices and seats are FICTIONAL."),
    );
  }

  // ---- map
  function specFor(f) {
    return muniSpec(byCode()[f.properties.code], ui.metric, { ...sctx, party: ui.party, density });
  }
  function tooltip(f) {
    const r = byCode()[f.properties.code];
    return muniTooltip(f.properties.name, r, { ...sctx, density }, { provinceName: geo?.name, names: namesByParty(), population: r?.population ?? f.properties.population });
  }
  function paintMap(first) {
    const src = data.results_source;
    const metrics = muniMetrics(src);
    if (!ui.metric || !metrics.some((m) => m.value === ui.metric)) ui.metric = metrics[0].value;
    const partiesIn = sctx.parties.length ? sctx.parties : [];
    if (!ui.party || !partiesIn.includes(ui.party)) ui.party = data.headline?.leader_party && partiesIn.includes(data.headline.leader_party) ? data.headline.leader_party : partiesIn[0];
    keyed(mapTools, `${src}|${ui.metric}|${ui.party}|${partiesIn.join()}`, () => [
      segmented(metrics, ui.metric, (v) => {
        ui.metric = v;
        setQuery({ map: v });
        paintMap(false);
      }, { label: "Map metric" }),
      ui.metric === "share"
        ? segmented(partiesIn.map((p) => ({ value: p, label: p })), ui.party, (v) => {
            ui.party = v;
            setQuery({ party: v });
            paintMap(false);
          }, { label: "Party" })
        : null,
    ]);
    mount(legendEl, muniLegend(ui.metric, rows(), { ...sctx, party: ui.party, density }, src));
    if (first || !map) {
      map?.destroy();
      mount(mapHost);
      map = resMap(mapHost, {
        layer: "municipalities",
        filter: (f) => f.properties.province_code === code,
        outline: "provinces",
        outlineFilter: (f) => f.properties.code === code,
        outlineWeight: 2,
        height: 500,
        label: `Municipality map of ${geo?.name || code}; the municipality table below lists the same results`,
        spec: specFor,
        tooltip,
        onClick: (f) => (location.hash = links.municipality(f.properties.code)),
      });
      map.ready.catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    } else map.update();
  }

  // ---- side: outstanding + swing
  function paintSide() {
    const src = data.results_source;
    const out = data.outstanding;
    keyed(outCard.body, JSON.stringify([src, out, (data.largest_remaining || []).map((r) => [r.code, r.reporting_pct, r.outstanding_est])]), () => {
      if (src === "hidden") return h("p", { class: "muted res-empty" }, "Estimates of the vote still to be counted appear once the night starts.");
      if (src === "final" || !out) return h("div", { class: "res-out" }, h("div", { class: "res-out__big" }, "Count complete"), h("div", { class: "muted" }, `${fmtPct(out?.reporting_pct ?? 100)} of the expected vote reported`));
      return h(
        "div",
        { class: "res-out" },
        h("div", { class: "res-out__row" }, h("div", null, h("div", { class: "res-out__big num" }, `≈ ${fmtInt(out.votes_est)}`), h("div", { class: "muted res-out__cap" }, "ballots still to count (estimate)")), reportingMeter(out.reporting_pct, { width: 70 })),
        h("div", { class: "res-muted-note" }, `${fmtInt(out.expected_ballots)} expected ballots · ${out.basis || "pre-election expectation"}`),
        (data.largest_remaining || []).length
          ? h(
              "div",
              null,
              h("div", { class: "res-subhead" }, "Largest remaining municipalities"),
              h(
                "ol",
                { class: "res-rank" },
                data.largest_remaining.map((r) =>
                  h(
                    "li",
                    null,
                    h("a", { href: links.municipality(r.code), class: "res-rank__name" }, r.name),
                    r.leader_party ? partyChip(r.leader_party, { color: pc(r.leader_party, r.leader_color) }) : h("span", { class: "muted" }, "–"),
                    reportingMeter(r.reporting_pct, { width: 36 }),
                    h("span", { class: "res-rank__val num", title: "Estimated outstanding ballots" }, fmtCompact(r.outstanding_est)),
                  ),
                ),
              ),
            )
          : h("p", { class: "muted res-empty" }, "Every municipality has reported in full."),
      );
    });
    keyed(swingCard.body, JSON.stringify([src, data.previous, data.swing]), () => {
      const prev = data.previous;
      const swing = data.swing;
      if (!prev) return h("p", { class: "muted res-empty" }, src === "hidden" ? "The previous result and swing are shown once this election's results are reported." : "No previous election holds this race.");
      const sw = Object.entries(swing || {}).sort((a, b) => b[1] - a[1]);
      return h(
        "div",
        { class: "res-prev" },
        h(
          "div",
          { class: "res-prev__head" },
          h("span", { class: "res-prev__year" }, String(prev.year)),
          h("div", null, h("div", { class: "res-prev__who" }, partyChip(prev.winner_party, { color: pc(prev.winner_party, data.colors?.[prev.winner_party]) }), h("span", null, prev.winner_name)), h("div", { class: "muted res-prev__meta" }, `won ${prev.race_code} by ${fmtPP(prev.margin_pp)} pp · turnout ${fmtPct(prev.turnout_pct)}`)),
        ),
        sw.length
          ? h(
              "div",
              null,
              h("div", { class: "res-subhead" }, `Swing since ${prev.year} (pp, share of the vote)`),
              divBars(sw.map(([p, v]) => ({ label: p, value: v, color: pc(p, data.colors?.[p]), note: `${fmtPct(prev.pct_by_party?.[p])} in ${prev.year}` })), { format: (v) => fmtPP(v), unit: " pp", label: `Swing since ${prev.year}` }),
            )
          : null,
      );
    });
  }

  // ---- municipality table
  function paintTable(rebuild) {
    const src = data.results_source;
    const partiesIn = (sctx.parties || []).slice().sort((a, b) => (sctx.maxShare[b] || 0) - (sctx.maxShare[a] || 0));
    const visible = rows().filter((r) => matches(r, ui.q));
    if (rebuild || !table) {
      table = dataTable(muniColumns(src, sctx, { link: links.municipality, parties: partiesIn }), visible, { sortKey: "population", sortDir: "desc", maxHeight: 520, rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: `Municipality results in ${geo?.name || code}` });
      mount(tableHost, table);
    } else table.update(visible);
    tableCard.setTitle(`Municipality results · ${rows().length}`);
  }

  // ---- other races
  function paintRaces() {
    const races = data.races || {};
    const src = data.results_source;
    const statewide = [races.governor ? ["Governor", races.governor, "Lt. Gov."] : null, ...(races.senate || []).map((s) => ["Senate", s, "with"]), races.legislature ? ["Legislature", races.legislature, "with"] : null]
      .filter(Boolean)
      .filter(([, r]) => r.code !== data.headline?.code); // the headline race is shown above
    keyed(statewideHost, JSON.stringify(statewide.map(([, r]) => [r.code, r.status, r.reporting_pct, r.leader, (r.lines || []).map((l) => l.votes)])), () =>
      statewide.length
        ? statewide.map(([kind, r, mate]) =>
            h(
              "section",
              { class: "card res-race-card" },
              h("div", { class: "card__head" }, h("h2", { class: "card__title" }, kind), h("div", { class: "page-head__meta" }, racePill(r))),
              h(
                "div",
                { class: "card__body" },
                h("a", { class: "res-race-card__name", href: links.race(r.code) }, r.name),
                h("div", { class: "res-race-card__meta muted" }, [r.reporting_pct !== null && r.reporting_pct !== undefined && src === "live" ? `${fmtPct(r.reporting_pct)} reporting` : null, r.margin_pp !== null && r.margin_pp !== undefined ? `margin ${fmtPP(r.margin_pp)} pp` : null, r.incumbent ? `incumbent ${r.incumbent.name} (${r.incumbent.party})${r.incumbent.running === false ? ", retiring" : ""}` : r.open_seat ? "open seat" : null].filter(Boolean).join(" · ")),
                resultRows(r.lines || [], { compact: true, max: 4, leaderKey: r.leader, mateLabel: mate, seats: r.seats_won }),
              ),
              r.seats_won ? h("div", { class: "card__foot" }, "Seats: ", Object.entries(r.seats_won).map(([p, n]) => h("span", { class: "res-seatchip" }, partyChip(p, { color: pc(p, data.colors?.[p]) }), ` ${n}`))) : null,
            ),
          )
        : h("p", { class: "muted res-empty" }, "No other province-wide race in this province this election."),
    );
    const house = races.house || [];
    const hrows = house.map((r) => ({ ...r, _leader: r.winner_party || r.leader_party || "" }));
    if (!houseCard._table) {
      houseCard._table = dataTable(
        [
          { key: "district_code", label: "District", format: (v, r) => h("a", { href: links.district(v), class: "res-link" }, h("span", { class: "res-code" }, v), r.district_name) },
          { key: "status", label: "Status", format: (v, r) => racePill(r) },
          { key: "_leader", label: src === "final" ? "Winner" : "Leader", format: (v, r) => (v ? partyTag(v, raceColor(r), r.winner_name || r.leader_name) : h("span", { class: "muted" }, "–")) },
          { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) },
          src === "live" ? { key: "reporting_pct", label: "Reporting", format: (v) => reportingMeter(v, { width: 46 }) } : { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
          { key: "incumbent", label: "Incumbent", value: (r) => r.incumbent?.name || "", format: (v, r) => (r.incumbent ? h("span", { class: "res-who" }, partyChip(r.incumbent.party, { color: pc(r.incumbent.party) }), h("span", { class: "res-who__name" }, r.incumbent.name), r.incumbent.running === false ? h("span", { class: "res-tag" }, "RETIRING") : null) : h("span", { class: "muted" }, "–")) },
          { key: "open_seat", label: "Open", value: (r) => (r.open_seat ? 1 : 0), format: (v) => (v ? h("span", { class: "res-tag res-tag--open" }, "OPEN") : "") },
          { key: "flip_status", label: "Flip", format: (v, r) => flipTag(v, { prev: r.previous_party }) },
        ],
        hrows,
        { sortKey: "district_code", sortDir: "asc", rowHref: (r) => links.district(r.district_code), rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: "House races in this province" },
      );
      mount(houseCard.body, houseCard._table);
    } else houseCard._table.update(hrows);
    houseCard.setTitle(`House races · ${house.length} districts`);
  }

  function paint(first) {
    frame.setSource(data);
    sctx = scaleContext(rows(), data.colors || {});
    paintHero();
    paintSide();
    paintMap(first);
    paintTable(first);
    paintRaces();
  }

  const fetchData = () => api.get(`/api/elections/${id}/provinces/${code}`, { signal: ctrl.signal });

  try {
    const [d, g, mun] = await Promise.all([fetchData(), api.get(`/api/provinces/${code}`, { cache: true }), api.get(`/api/municipalities?province=${code}`, { cache: true }).catch(() => null)]);
    data = d;
    geo = g;
    density = Object.fromEntries((mun?.municipalities || []).map((m) => [m.code, m.density]));
    lastSource = d.results_source;
    frame.setTitle(g.name, `Province · ${code} · ${d.election?.name || ""}`);
    buildBody();
    paintFacts();
    mount(tableTools, searchInput("Search municipalities…", (q) => {
      ui.q = q;
      paintTable(false);
    }, { label: "Search municipalities" }));
    paint(true);
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, async () => {
    const d = await fetchData();
    const changed = d.results_source !== lastSource;
    lastSource = d.results_source;
    data = d;
    if (changed) {
      houseCard._table = null;
      paint(true);
    } else paint(false);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}
