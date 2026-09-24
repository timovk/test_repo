/**
 * #/municipalities/:code — municipality page: REAL facts and CBS demographics (imputed values
 * flagged as DERIVED), every race touching the municipality (President, House district
 * fragment(s) with the municipality's share in each, Senate, Governor, Legislature, Mayor,
 * Council seats), the neighbourhood (precinct) map and table, the live reporting log and the
 * municipality's history across reported elections.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmt1, fmtInt, fmtPct, fmtPP, fmtShare } from "../format.js";
import { setQuery } from "../router.js";
import { partyChip, provBadge } from "../components/badges.js";
import { lineChart } from "../components/charts.js";
import { dataTable } from "../components/table.js";
import { bandBar } from "../components/res-charts.js";
import { FAMILY_LABEL, facts, flipTag, liveRefresh, marginLabel, pc, raceColor, racePill, rampLegend, reportingMeter, resultRows, segmented, seqColor, seqRamp, swatchLegend } from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const TYPE_LABEL = {
  PRESIDENT: "President · national",
  PRESIDENT_PROVINCE: "President · province EV",
  HOUSE: "House",
  SENATE: "Senate",
  GOVERNOR: "Governor",
  PROVINCIAL_LEGISLATURE: "Provincial legislature",
  MAYOR: "Mayor",
  MUNICIPAL_COUNCIL: "Municipal council",
};
const URBANITY = { 1: "very strongly urban", 2: "strongly urban", 3: "moderately urban", 4: "hardly urban", 5: "not urban" };
const HIST_FAMILIES = ["PRES", "HOUSE", "SEN", "GOV", "MAYOR", "COUNCIL", "PROVLEG"];

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const code = String(params.code || "").toUpperCase();
  const ctrl = new AbortController();
  const ui = { prace: ctx.query.prace || null, hist: ctx.query.hist || "PRES" };
  const frame = pageFrame(el, { eyebrow: `Municipality · ${code}`, title: code, categories: ["REAL", "SIMULATED"], electionId: id });

  let geo = null; // REAL facts
  let data = null; // election payload
  let map = null;
  let precTable = null;
  let lastSource = null;

  const factsCard = liveCard("Facts", { id: "res-mu-facts", categories: [provBadge("REAL")] });
  const demoCard = liveCard("Demographics", { id: "res-mu-demo", categories: [provBadge("REAL", "CBS")] });
  const racesHead = h("div", { class: "res-section-title" });
  const racesHost = h("div", { class: "res-grid res-grid--races" });
  const mapCard = liveCard("Neighbourhoods", { id: "res-mu-map", categories: [provBadge("REAL", "CBS buurten")] });
  const mapTools = h("div", { class: "res-card-tools" });
  const mapHost = h("div", { class: "res-map-host" });
  const mapLegend = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapTools, mapHost, mapLegend);
  const precCard = liveCard("Neighbourhood (precinct) results", { id: "res-mu-prec", flush: true });
  const eventsCard = liveCard("Reporting log", { id: "res-mu-events", flush: true });
  const histCard = liveCard("History", { id: "res-mu-hist", categories: [provBadge("SIMULATED")] });
  const histTools = h("div", { class: "res-card-tools" });
  const histBody = h("div");
  histCard.body.append(histTools, histBody);

  // ------------------------------------------------------------ facts & demographics (REAL)
  function paintFacts() {
    const g = geo;
    const imp = new Set(g.imputed_fields || []);
    const dist = g.districts || [];
    mount(
      factsCard.body,
      facts(
        [
          { label: "Population", value: fmtInt(g.population), cat: "REAL", note: g.population_official ? `official ${fmtInt(g.population_official)}` : null },
          { label: "Area", value: `${fmt1(g.area_km2)} km²`, cat: "REAL", note: `${fmt1(g.land_area_km2)} km² land` },
          { label: "Density", value: `${fmtInt(g.density)} /km²`, cat: "DERIVED" },
          { label: "Urbanity", value: g.urbanity_class ? `class ${g.urbanity_class}` : "–", cat: "REAL", note: URBANITY[g.urbanity_class] || null },
          { label: "Address density", value: `${fmtInt(g.address_density)} /km²`, cat: "REAL" },
          { label: "Eligible voters", value: fmtInt(g.eligible_voters_est), cat: "DERIVED", note: "estimated" },
          { label: "Neighbourhoods", value: fmtInt(g.unit_count), cat: "REAL", note: "precincts (CBS buurten)" },
          { label: "Province", value: h("a", { href: links.province(g.province_code), class: "res-link" }, data?.municipality?.province_name || g.province_code), cat: "REAL" },
          { label: "Mayor", value: g.mayor ? h("span", { class: "res-facts__person" }, partyChip(g.mayor.party || "IND", { color: pc(g.mayor.party) }), g.mayor.name) : "–", cat: "FICTIONAL", note: g.mayor?.term_end ? `term to ${String(g.mayor.term_end).slice(0, 4)}` : null },
          { label: "Council", value: g.council ? `${g.council.seats} seats` : "–", cat: "FICTIONAL", note: g.council ? String(g.council.electoral_system || "").replace(/_/g, " ") : null },
        ],
        { cols: 2 },
      ),
      h("div", { class: "res-subhead" }, "House districts ", provBadge("FICTIONAL")),
      h(
        "ul",
        { class: "res-rank" },
        dist.map((d) =>
          h(
            "li",
            null,
            h("a", { href: links.district(d.code), class: "res-rank__name" }, h("span", { class: "res-code" }, d.code), " ", d.name),
            h("span", { class: "res-meter", title: `${fmtShare(d.share_of_municipality)} of ${g.name}'s population lives in ${d.code}` }, h("span", { class: "res-meter__track", style: { width: "60px" } }, h("span", { class: "res-meter__fill", style: { width: `${(d.share_of_municipality || 0) * 100}%` } })), h("span", { class: "num" }, fmtShare(d.share_of_municipality))),
          ),
        ),
      ),
      (g.lineage || []).length ? h("p", { class: "res-muted-note" }, `Municipal lineage: ${g.lineage.map((l) => l.description || JSON.stringify(l)).join("; ")}`) : null,
    );
    const d = g.demographics || {};
    const cat = (k) => (imp.has(k) ? "DERIVED" : null);
    const flag = (k) => (imp.has(k) ? "IMPUTED" : null);
    const anyImp = (...ks) => ks.some((k) => imp.has(k));
    mount(
      demoCard.body,
      h(
        "div",
        { class: "res-demo" },
        h("div", { class: "res-demo__block" }, h("div", { class: "res-subhead" }, "Age", anyImp("pct_age_0_15", "pct_age_65_plus") ? h("span", { class: "res-tag res-tag--imputed" }, "IMPUTED") : null), bandBar([{ label: "0–15", value: d.pct_age_0_15 }, { label: "15–25", value: d.pct_age_15_25 }, { label: "25–45", value: d.pct_age_25_45 }, { label: "45–65", value: d.pct_age_45_65 }, { label: "65+", value: d.pct_age_65_plus }], { label: "Age distribution" })),
        h("div", { class: "res-demo__block" }, h("div", { class: "res-subhead" }, "Origin", anyImp("pct_origin_nl") ? h("span", { class: "res-tag res-tag--imputed" }, "IMPUTED") : null), bandBar([{ label: "Dutch", value: d.pct_origin_nl }, { label: "European", value: d.pct_origin_europe }, { label: "Non-European", value: d.pct_origin_non_europe }], { label: "Origin" })),
        h("div", { class: "res-demo__block" }, h("div", { class: "res-subhead" }, "Education", anyImp("pct_education_low", "pct_education_mid", "pct_education_high") ? h("span", { class: "res-tag res-tag--imputed", title: "Imputed (DERIVED) — no CBS value for this municipality" }, "IMPUTED") : null), bandBar([{ label: "Low", value: d.pct_education_low }, { label: "Middle", value: d.pct_education_mid }, { label: "High", value: d.pct_education_high }], { label: "Education level" })),
        facts(
          [
            { label: "Income / capita", value: d.income_per_capita_keur !== undefined && d.income_per_capita_keur !== null ? `€${fmt1(d.income_per_capita_keur)}k` : "–", cat: cat("income_per_capita_keur"), flag: flag("income_per_capita_keur") },
            { label: "Owner-occupied", value: fmtPct(d.pct_owner_occupied), cat: cat("pct_owner_occupied"), flag: flag("pct_owner_occupied") },
            { label: "Single households", value: fmtPct(d.pct_single_households), cat: cat("pct_single_households"), flag: flag("pct_single_households") },
            { label: "With children", value: fmtPct(d.pct_households_with_children), cat: cat("pct_households_with_children"), flag: flag("pct_households_with_children") },
            { label: "Household size", value: fmt1(d.avg_household_size), cat: cat("avg_household_size"), flag: flag("avg_household_size") },
          ],
          { cols: 3 },
        ),
        h("p", { class: "res-muted-note" }, `CBS kerncijfers ${g.demographics_source_years?.core || ""}${g.demographics_source_years?.supplement ? ` (supplement ${g.demographics_source_years.supplement})` : ""}. `, imp.size ? h("span", null, h("b", null, `${imp.size} imputed value${imp.size > 1 ? "s" : ""}`), " (DERIVED) are flagged IMPUTED.") : "No imputed values."),
      ),
    );
  }

  // ------------------------------------------------------------ races
  function shareIn(districtCode) {
    return (geo?.districts || []).find((d) => d.code === districtCode)?.share_of_municipality;
  }
  function raceCard(r) {
    const m = r.municipal;
    const src = data.results_source;
    const house = r.type === "HOUSE";
    const overall = raceColor(r);
    const muniColor = m?.leader_party ? pc(m.leader_party) : null;
    return h(
      "section",
      { class: "card res-race-card", style: { "--party": muniColor || overall || "var(--uncalled)" } },
      h("div", { class: "card__head" }, h("h2", { class: "card__title" }, TYPE_LABEL[r.type] || r.type), h("div", { class: "page-head__meta" }, racePill(r))),
      h(
        "div",
        { class: "card__body" },
        h("a", { class: "res-race-card__name", href: house ? links.district(r.district_code) : links.race(r.code) }, r.name),
        h(
          "div",
          { class: "res-race-card__meta muted" },
          [
            house && shareIn(r.district_code) !== undefined ? `${fmtShare(shareIn(r.district_code))} of ${geo.name} lives in ${r.district_code}` : null,
            r.electoral_votes ? `${r.electoral_votes} EV` : null,
            r.incumbent ? `inc. ${r.incumbent.name} (${r.incumbent.party || "IND"})${r.incumbent.running === false ? ", retiring" : ""}` : r.open_seat ? "open seat" : null,
          ]
            .filter(Boolean)
            .join(" · "),
        ),
        m
          ? [
              h("div", { class: "res-subhead res-subhead--tight" }, `In ${geo.name}`, src === "live" && m.reporting_pct !== null && m.reporting_pct !== undefined ? reportingMeter(m.reporting_pct, { width: 50 }) : null),
              resultRows(m.lines || [], { compact: true, max: 4, leaderKey: m.leader, mateLabel: r.type === "GOVERNOR" ? "Lt. Gov." : "with", seats: m.seats_won }),
              h(
                "div",
                { class: "res-race-card__kv" },
                m.margin_pp !== null && m.margin_pp !== undefined ? h("span", null, "Local margin ", h("b", null, marginLabel(m.leader_party, m.margin_pp))) : null,
                m.turnout_pct !== null && m.turnout_pct !== undefined ? h("span", null, "Turnout ", h("b", null, fmtPct(m.turnout_pct))) : null,
                m.total_votes ? h("span", null, "Votes ", h("b", null, fmtInt(m.total_votes))) : null,
              ),
            ]
          : src === "hidden"
            ? resultRows(r.lines || [], { compact: true, max: 4, mateLabel: r.type === "GOVERNOR" ? "Lt. Gov." : "with" })
            : h("p", { class: "muted res-empty" }, "No municipal breakdown for the national race; see the province EV race."),
      ),
      h(
        "div",
        { class: "card__foot res-race-card__foot" },
        h("span", null, r.type === "PRESIDENT" ? "Nationwide: " : "Race-wide: ", r.winner_party || r.leader_party ? [partyChip(r.winner_party || r.leader_party, { color: overall }), " ", r.winner_name || r.leader_name || ""] : h("span", { class: "muted" }, src === "hidden" ? "results hidden" : "–")),
        r.margin_pp !== null && r.margin_pp !== undefined ? h("span", null, marginLabel(null, r.margin_pp)) : null,
        r.flip_status ? flipTag(r.flip_status, { prev: r.previous_party }) : null,
      ),
      m?.seats_won ? h("div", { class: "card__foot" }, "Council seats: ", Object.entries(m.seats_won).map(([p, n]) => h("span", { class: "res-seatchip" }, partyChip(p, { color: pc(p) }), ` ${n}`))) : null,
    );
  }
  function paintRaces() {
    const races = (data.races || []).filter((r) => !(r.type === "PRESIDENT" && data.results_source !== "hidden" && !r.municipal));
    mount(racesHead, h("h2", null, `On the ballot in ${geo.name}`), h("span", { class: "res-muted-note" }, `${races.length} race${races.length === 1 ? "" : "s"} · ${data.election?.name || ""}`));
    keyed(racesHost, JSON.stringify((data.races || []).map((r) => [r.code, r.status, r.leader, r.winner, r.municipal?.reporting_pct, r.municipal?.total_votes])), () => races.map(raceCard));
  }

  // ------------------------------------------------------------ precincts (neighbourhoods)
  function unitRows() {
    const byCode = Object.fromEntries((data.precincts || []).map((p) => [p.code, p]));
    return (geo.units || []).map((u) => ({ ...u, p: byCode[u.code] || null }));
  }
  function precLines() {
    const r = (data.races || []).find((x) => x.code === data.precinct_race);
    return Object.fromEntries((r?.municipal?.lines || r?.lines || []).map((l) => [l.key, l]));
  }
  function paintPrecincts(rebuild) {
    const src = data.results_source;
    const avail = data.precincts_available && (data.precincts || []).length;
    // PRES-<PV> has the same municipal count as PRES, so the national race stands for both.
    const hasPres = (data.races || []).some((r) => r.type === "PRESIDENT");
    const short = (r) =>
      ({ PRESIDENT: "President", PRESIDENT_PROVINCE: "President", HOUSE: r.district_code, SENATE: `Senate ${String(r.code).split("-").pop()}`, GOVERNOR: "Governor", PROVINCIAL_LEGISLATURE: "Legislature", MAYOR: "Mayor", MUNICIPAL_COUNCIL: "Council" })[r.type] || r.code;
    const opts = (data.races || []).filter((r) => !(hasPres && r.type === "PRESIDENT_PROVINCE")).map((r) => ({ value: r.code, label: short(r), title: r.name }));
    if (!opts.some((o) => o.value === data.precinct_race) && data.precinct_race) opts.unshift({ value: data.precinct_race, label: data.precinct_race });
    keyed(mapTools, `${src}|${data.precinct_race}|${avail}|${opts.map((o) => o.value).join()}`, () => [
      avail ? h("span", { class: "res-toolbar__label" }, "Precinct race") : null,
      avail
        ? segmented(opts, data.precinct_race, (v) => {
            ui.prace = v;
            setQuery({ prace: v });
            refetch(true);
          }, { label: "Race shown in the precinct map and table" })
        : h("span", { class: "res-muted-note" }, src === "live" ? "Precinct results are published once the count is final; the map shows REAL population density." : src === "hidden" ? "Precinct results are hidden until reported; the map shows REAL population density." : "No precinct results for this election."),
    ]);
    const lines = precLines();
    const rows = unitRows();
    const byUnit = Object.fromEntries(rows.map((r) => [r.code, r]));
    const spec = (f) => {
      const u = byUnit[f.properties.code];
      if (!u) return { none: true };
      if (avail && u.p?.leader_party) return { fill: pc(u.p.leader_party, u.p.leader_color), state: "called" };
      if (avail) return {};
      return u.density ? { fill: seqColor(Math.log10(Math.max(1, u.density)), 2, 4) } : {};
    };
    const tip = (f) => {
      const u = byUnit[f.properties.code];
      if (!u) return h("div", { class: "res-tip" }, h("strong", null, f.properties.name));
      const top = u.p ? Object.entries(u.p.pct || {}).sort((a, b) => b[1] - a[1]).slice(0, 4) : [];
      return h(
        "div",
        { class: "res-tip" },
        h("div", { class: "res-tip__head" }, h("strong", null, u.name), h("span", { class: "res-code" }, u.code)),
        h("div", { class: "res-tip__sub muted" }, `pop. ${fmtInt(u.population)} · ${fmtInt(u.density)}/km² · district ${u.district_code || "–"}`, (u.imputed_fields || []).length ? " · imputed values" : ""),
        top.length
          ? h("div", { class: "res-tip__rows" }, top.map(([k, v]) => h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(lines[k]?.party, lines[k]?.color) } }), h("span", { class: "res-tip__name" }, `${lines[k]?.name || k} (${lines[k]?.party || ""})`), h("b", { class: "num" }, fmtPct(v)), h("span", { class: "res-tip__votes num" }, fmtInt(u.p.votes?.[k])))))
          : null,
        u.p ? h("div", { class: "res-tip__kv" }, h("span", null, "Margin ", h("b", null, marginLabel(u.p.leader_party, u.p.margin_pp))), h("span", null, "Turnout ", h("b", null, fmtPct(u.p.turnout_pct))), h("span", null, "Votes ", h("b", null, fmtInt(u.p.total_votes)))) : null,
      );
    };
    const partiesIn = [...new Set(rows.map((r) => r.p?.leader_party).filter(Boolean))];
    mount(mapLegend, avail ? swatchLegend(partiesIn.map((p) => ({ color: pc(p), label: p })), { title: `Precinct leader · ${data.precinct_race}` }) : rampLegend(seqRamp(), ["100", "1,000", "10,000 /km²"], { title: "Population density · REAL (CBS)" }));
    if (!map) {
      map = resMap(mapHost, {
        layer: `units/${geo.province_code}`,
        filter: (f) => !!byUnitCodes[f.properties.code],
        outline: "municipalities",
        outlineFilter: (f) => f.properties.code === code,
        outlineWeight: 2,
        height: 460,
        label: `Neighbourhood map of ${geo.name}; the precinct table lists the same values`,
        spec,
        tooltip: tip,
      });
      map.ready.catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    } else map.update(spec);
    // table
    const cols = [
      { key: "name", label: "Neighbourhood", format: (v, r) => h("span", null, h("span", { class: "res-code" }, r.code.slice(-4)), " ", v, (r.imputed_fields || []).length ? h("span", { class: "res-tag res-tag--imputed", title: `Imputed: ${r.imputed_fields.join(", ")}` }, "IMP") : null) },
      { key: "population", label: "Population", align: "r", format: (v) => fmtInt(v) },
      avail ? null : { key: "density", label: "Density", align: "r", format: (v) => fmtInt(v) },
      { key: "district_code", label: "District", format: (v) => (v ? h("a", { href: links.district(v), class: "res-code res-link" }, v) : "–") },
      avail ? { key: "votes", label: "Votes", align: "r", value: (r) => r.p?.total_votes ?? null, format: (v) => fmtInt(v) } : null,
      avail ? { key: "lead", label: "Leader", value: (r) => r.p?.leader_party || "", format: (v, r) => (v ? partyChip(v, { color: pc(v, r.p?.leader_color) }) : "–") } : null,
      avail ? { key: "margin", label: "Margin", align: "r", value: (r) => r.p?.margin_pp ?? null, format: (v) => marginLabel(null, v) } : null,
      avail ? { key: "turnout", label: "Turnout", align: "r", value: (r) => r.p?.turnout_pct ?? null, format: (v) => fmtPct(v) } : null,
    ].filter(Boolean);
    if (rebuild || !precTable) {
      precTable = dataTable(cols, rows, { sortKey: "population", sortDir: "desc", maxHeight: 460, caption: "Neighbourhoods (precincts)" });
      mount(precCard.body, precTable);
    } else precTable.update(rows);
    precCard.setTitle(avail ? `Precinct results · ${data.precinct_race} · ${rows.length} neighbourhoods` : `Neighbourhoods · ${rows.length}`);
  }
  let byUnitCodes = {};

  // ------------------------------------------------------------ live reporting log
  function paintEvents() {
    const ev = data.events || [];
    eventsCard.hidden = data.results_source !== "live";
    if (eventsCard.hidden) return;
    keyed(eventsCard.body, JSON.stringify(ev.map((e) => e.seq)), () =>
      ev.length
        ? h(
            "ol",
            { class: "feed res-calls" },
            [...ev].reverse().map((e) =>
              h("li", { class: "feed__item" }, h("span", { class: "feed__time" }, e.clock), h("span", null, h("b", null, `Batch ${e.batch_index}/${e.batches}`), h("span", { class: "muted" }, ` · ${fmtInt(e.units)} neighbourhoods · ${fmtInt(e.ballots)} ballots`)), h("span", { class: "num muted res-calls__meta" }, `${fmtShare(e.municipality_fraction_after)} counted`)),
            ),
          )
        : h("p", { class: "muted res-empty", style: { padding: "16px" } }, "No votes reported here yet."),
    );
    const m = data.municipality || {};
    eventsCard.setTitle(`Reporting log · ${fmtPct(m.reporting_pct)} counted`);
  }

  // ------------------------------------------------------------ history
  async function paintHistory() {
    keyed(histTools, ui.hist, () =>
      segmented(HIST_FAMILIES.map((f) => ({ value: f, label: FAMILY_LABEL[f] })), ui.hist, (v) => {
        ui.hist = v;
        setQuery({ hist: v });
        paintHistory();
      }, { label: "Race family for the history" }),
    );
    mount(histBody, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "50%", height: "12px" } })));
    let d;
    try {
      d = await api.get(`/api/history/municipality/${code}?race=${ui.hist}`, { signal: ctrl.signal });
    } catch (err) {
      if (err?.name === "AbortError") return;
      mount(histBody, h("p", { class: "muted res-empty" }, err?.status === 404 ? `No reported ${FAMILY_LABEL[ui.hist]} results for ${geo.name}.` : String(err.message || err)));
      return;
    }
    const pts = d.points || [];
    if (!pts.length) {
      mount(histBody, h("p", { class: "muted res-empty" }, `No reported ${FAMILY_LABEL[ui.hist]} results for ${geo.name} yet.`));
      return;
    }
    const partiesIn = [...new Set(pts.flatMap((p) => Object.keys(p.shares || {})))];
    partiesIn.sort((a, b) => (pts[pts.length - 1].shares?.[b] ?? 0) - (pts[pts.length - 1].shares?.[a] ?? 0));
    const years = pts.map((p) => p.election.year);
    const series = partiesIn.map((p) => ({ key: p, label: p, color: pc(p), points: pts.filter((x) => x.shares?.[p] !== undefined).map((x) => ({ x: x.election.year, y: x.shares[p] })) }));
    mount(
      histBody,
      pts.length > 1
        ? lineChart({ series, height: 220, yFormat: (v) => `${Math.round(v)}%`, xFormat: (x) => String(Math.round(x)), xTicks: years, markers: true, label: `${FAMILY_LABEL[ui.hist]} vote share by party in ${geo.name}, ${years.join(", ")}` })
        : h("p", { class: "res-muted-note" }, `Only one reported election (${years[0]}) — the trend appears after the next one.`),
      dataTable(
        [
          { key: "year", label: "Election", value: (p) => p.election.year, format: (v, p) => h("span", null, h("b", null, String(v)), " ", h("span", { class: "muted" }, p.election.election_type)) },
          { key: "winner", label: "Winner", format: (v) => (v ? partyChip(v, { color: pc(v) }) : "–") },
          { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) },
          { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
          ...partiesIn.slice(0, 6).map((p) => ({ key: `s_${p}`, label: p, align: "r", value: (x) => x.shares?.[p] ?? null, format: (v, x) => (v === null ? "–" : h("span", { title: x.change_pp?.[p] !== undefined ? `${fmtPP(x.change_pp[p])} pp vs previous` : "" }, fmtPct(v), x.change_pp?.[p] !== undefined ? h("span", { class: "muted res-hist-chg" }, ` ${fmtPP(x.change_pp[p])}`) : null)) })),
          { key: "flipped", label: "Change", value: (x) => (x.flipped === true ? "flip" : x.flipped === false ? "hold" : ""), format: (v) => flipTag(v || null) },
        ],
        pts,
        { sortKey: "year", sortDir: "desc", caption: `History of ${FAMILY_LABEL[ui.hist]} results` },
      ),
      h("p", { class: "res-muted-note" }, "Reported elections only; older municipal boundaries are remapped onto today's municipality (lineage-aware). Shares in %, change in pp."),
    );
  }

  // ------------------------------------------------------------ data flow
  const url = () => `/api/elections/${id}/municipalities/${code}${ui.prace ? `?race=${encodeURIComponent(ui.prace)}` : ""}`;
  async function refetch(rebuildPrecincts) {
    try {
      const d = await api.get(url(), { signal: ctrl.signal });
      const changed = d.results_source !== lastSource;
      lastSource = d.results_source;
      data = d;
      frame.setSource(d);
      paintRaces();
      paintPrecincts(rebuildPrecincts || changed);
      paintEvents();
    } catch (err) {
      if (err?.name !== "AbortError") console.warn(err);
    }
  }

  try {
    const [g, d] = await Promise.all([api.get(`/api/municipalities/${code}`, { cache: true }), api.get(url(), { signal: ctrl.signal }).catch((err) => (err?.status === 404 && ui.prace ? ((ui.prace = null), api.get(url(), { signal: ctrl.signal })) : Promise.reject(err)))]);
    geo = g;
    data = d;
    byUnitCodes = Object.fromEntries((g.units || []).map((u) => [u.code, true]));
    lastSource = d.results_source;
    frame.setTitle(g.name, `Municipality · ${code} · ${d.municipality?.province_name || g.province_code} · ${d.election?.name || ""}`);
    mount(
      frame.body,
      h("div", { class: "res-grid res-grid--2" }, factsCard, demoCard),
      racesHead,
      racesHost,
      h("div", { class: "res-grid res-grid--2" }, mapCard, h("div", { class: "res-stack" }, precCard, eventsCard)),
      histCard,
    );
    frame.setSource(d);
    paintFacts();
    paintRaces();
    paintPrecincts(true);
    paintEvents();
    paintHistory();
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, () => refetch(false));

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}

