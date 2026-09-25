/**
 * #/data — where every number comes from.
 *  · Provenance: REAL sources (publisher, URL, licence, SHA-256, retrieval date), the processed
 *    store, DERIVED transforms, FICTIONAL constructs and SIMULATED outputs.
 *  · Apportionment: method, per-province population / quota / seats / EV, the seat priority list,
 *    the first provinces out and a comparison of apportionment methods.
 *  · District plan: generation (seed, method, config hash), validation, deviation and
 *    compactness summaries and the per-province table.
 *  · Validation: the constitutional / data-integrity checks (optionally including the exact
 *    reconciliation of every stored election).
 *  · Exports: every dataset of the selected election as CSV or JSON (stable schemas).
 * Read-only: all figures come from the API.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { fmt2, fmtInt, fmtPP } from "../format.js";
import { card, currentElectionId, links, pageHeader } from "./_shared.js";
import { dataTable } from "../components/table.js";
import { provBadge } from "../components/badges.js";
import { callout, downloadLink, fmtBytes, fmtDateTime, humanize, kpi, kv, provLegend, provinceName, refresh, statusBadge, tabBar } from "../components/ana-ui.js";
import { getState } from "../store.js";

const TABS = [
  { key: "provenance", label: "Provenance" },
  { key: "apportionment", label: "Apportionment" },
  { key: "plan", label: "District plan" },
  { key: "validation", label: "Validation" },
  { key: "exports", label: "Exports" },
];

export async function render(el, _params, ctx) {
  const ctrl = new AbortController();
  const signal = ctrl.signal;
  let active = TABS.some((t) => t.key === ctx?.query?.tab) ? ctx.query.tab : "provenance";
  const body = h("div", { class: "data-body" });

  mount(
    el,
    pageHeader({ eyebrow: "Data", title: "Data & provenance", categories: ["REAL", "DERIVED", "FICTIONAL", "SIMULATED"] }),
    provLegend(["REAL", "DERIVED", "FICTIONAL", "SIMULATED"]),
    tabBar(TABS, active, (key) => {
      active = key;
      setQuery({ tab: key === "provenance" ? null : key });
      show();
    }, { label: "Data sections" }),
    body,
  );

  function show() {
    const fn = { provenance, apportionment, plan, validation, exports }[active];
    fn();
  }

  /* ------------------------------------------------------------------ provenance */
  function provenance() {
    refresh(body, api.get("/api/data/provenance", { signal, cache: true }), (p) => {
      const real = p.real || {};
      const store = real.store || {};
      const counts = store.counts || {};
      const imp = store.imputation?.units_by_field || {};
      return h(
        "div",
        { class: "grid" },
        p.notice ? callout("info", p.notice) : null,
        card(
          "Real source data",
          h(
            "div",
            null,
            h("p", { class: "secondary", style: { marginTop: 0 } }, real.attribution || ""),
            dataTable(
              [
                { key: "name", label: "Dataset", format: (v, r) => h("span", null, h("b", null, v), h("br"), h("span", { class: "muted" }, r.publisher || "")) },
                { key: "license", label: "Licence" },
                { key: "size_bytes", label: "Size", align: "r", format: (v) => fmtBytes(v) },
                { key: "retrieved_at", label: "Retrieved", format: (v) => fmtDateTime(v) },
                { key: "sha256", label: "SHA-256", sort: false, format: (v) => (v ? h("span", { class: "mono", title: v }, `${String(v).slice(0, 16)}…`) : "–") },
                { key: "url", label: "Source", sort: false, format: (v) => (v ? h("a", { href: v, target: "_blank", rel: "noopener noreferrer", class: "ana-link" }, "open") : "–") },
              ],
              real.sources || [],
              { sortKey: "name", sortDir: "asc" },
            ),
            real.precinct_note ? h("p", { class: "muted", style: { marginBottom: 0 } }, real.precinct_note) : null,
          ),
          { categories: ["REAL"], flush: false },
        ),
        h(
          "div",
          { class: "grid grid--2" },
          card(
            "Processed geography store",
            kv([
              ["Vintage", real.vintage?.label || store.year],
              ["Provinces", fmtInt(counts.provinces)],
              ["Municipalities", `${fmtInt(counts.municipalities)} (CBS layer: ${fmtInt(counts.municipalities_generalized_layer)})`],
              ["Neighbourhoods (precincts)", `${fmtInt(counts.units)} · ${fmtInt(counts.units_zero_population)} without residents`],
              ["Population", `${fmtInt(counts.population)} (official ${fmtInt(counts.population_official)})`],
              ["Estimated eligible voters", fmtInt(counts.eligible_voters_est)],
              ["Adjacency", `${fmtInt(counts.unit_edges)} neighbourhood borders · ${fmtInt(counts.water_links)} water links`],
              ["Built", fmtDateTime(store.built_at)],
              ["Fingerprint", store.fingerprint ? h("span", { class: "mono", title: store.fingerprint }, `${store.fingerprint.slice(0, 16)}…`) : "–"],
              ["Coordinate systems", `${store.crs || "–"} (analysis) · ${store.web_crs || "–"} (maps)`],
            ], { cols: 1 }),
            { categories: ["REAL", "DERIVED"] },
          ),
          card(
            "Imputed values (flagged)",
            h(
              "div",
              null,
              h("p", { class: "secondary", style: { marginTop: 0 } }, `${fmtInt(counts.units_with_imputed_fields)} neighbourhoods have at least one indicator imputed from their wijk, municipality or province (CBS suppresses small cells). Imputed values are DERIVED and flagged on every page that shows them.`),
              dataTable(
                [
                  { key: "field", label: "Indicator", format: (v) => humanize(v) },
                  { key: "units", label: "Neighbourhoods imputed", align: "r", format: (v) => fmtInt(v) },
                ],
                Object.entries(imp).map(([field, units]) => ({ field, units })),
                { sortKey: "units", sortDir: "desc", maxHeight: 300 },
              ),
            ),
            { categories: ["DERIVED"] },
          ),
        ),
        card(
          "Derived transforms",
          h("ul", { class: "data-list" }, (p.derived?.transforms || []).map((t) => h("li", null, h("b", null, t.name), " — ", t.definition))),
          { categories: ["DERIVED"] },
        ),
        h(
          "div",
          { class: "grid grid--2" },
          card(
            "Fictional constructs",
            h(
              "div",
              null,
              h("ul", { class: "data-list" }, (p.fictional?.constructs || []).map((c) => h("li", null, h("b", null, c.name), " — ", c.description, c.where ? h("span", { class: "muted" }, ` (${c.where})`) : null))),
              kv(Object.entries(p.fictional?.counts || {}).map(([k, v]) => [humanize(k), fmtInt(v)]), { cols: 2 }),
            ),
            { categories: ["FICTIONAL"] },
          ),
          card(
            "Simulated outputs",
            h(
              "div",
              null,
              h("ul", { class: "data-list" }, (p.simulated?.outputs || []).map((c) => h("li", null, h("b", null, c.name), " — ", c.description, c.where ? h("span", { class: "muted" }, ` (${c.where})`) : null))),
              kv(Object.entries(p.simulated?.counts || {}).map(([k, v]) => [humanize(k), fmtInt(v)]), { cols: 2 }),
            ),
            { categories: ["SIMULATED"] },
          ),
        ),
      );
    });
  }

  /* ------------------------------------------------------------------ apportionment */
  function apportionment() {
    refresh(body, api.get("/api/apportionment", { signal, cache: true }), (a) => {
      const provs = a.provinces || [];
      const methods = a.method_comparison?.length ? Object.keys(a.method_comparison[0]).filter((k) => !["province", "population"].includes(k)) : [];
      return h(
        "div",
        { class: "grid" },
        h(
          "div",
          { class: "grid grid--4" },
          kpi("House seats", fmtInt(a.total_seats), `${humanize(a.method)} · minimum ${fmtInt(a.min_seats_per_province)} per province`),
          kpi("Electoral votes", fmtInt(a.total_electoral_votes), `seats + ${fmtInt(a.senators_per_province)} per province`),
          kpi("Population basis", fmtInt(a.total_population), "REAL CBS population"),
          kpi("Average per seat", fmtInt(a.average_persons_per_seat), "persons per House seat"),
        ),
        card(
          "Seats and electoral votes by province",
          dataTable(
            [
              { key: "name", label: "Province", format: (v, r) => h("a", { href: links.province(r.code), class: "ana-link" }, v) },
              { key: "population", label: "Population", align: "r", format: (v) => fmtInt(v) },
              { key: "quota", label: "Exact quota", align: "r", format: (v) => fmt2(v) },
              { key: "seats", label: "House seats", align: "r", format: (v) => h("b", null, fmtInt(v)) },
              { key: "senators", label: "Senators", align: "r", format: (v) => fmtInt(v) },
              { key: "electoral_votes", label: "Electoral votes", align: "r", format: (v) => h("b", null, fmtInt(v)) },
              { key: "persons_per_seat", label: "Persons per seat", align: "r", format: (v) => fmtInt(v) },
            ],
            provs,
            { sortKey: "population", sortDir: "desc" },
          ),
          { categories: ["REAL", "FICTIONAL"], flush: true, foot: `Population is REAL (CBS ${getState().meta?.geography?.year || ""}); seats and electoral votes are FICTIONAL. Total: ${fmtInt(a.total_seats)} seats · ${fmtInt(a.total_electoral_votes)} EV.` },
        ),
        h(
          "div",
          { class: "grid grid--2" },
          card(
            "Seat priority order",
            h(
              "div",
              null,
              h("p", { class: "secondary", style: { marginTop: 0 } }, "After every province receives its minimum seat, the remaining seats go one by one to the highest priority value (Huntington–Hill: population ÷ √(n·(n+1)))."),
              dataTable(
                [
                  { key: "rank", label: "Seat #", align: "r" },
                  { key: "province", label: "Province", format: (v) => provinceName(v) },
                  { key: "province_seat", label: "Province seat", align: "r" },
                  { key: "priority", label: "Priority value", align: "r", format: (v) => fmtInt(v) },
                ],
                a.priority_list || [],
                { sortKey: "rank", sortDir: "asc", maxHeight: 360 },
              ),
            ),
            { categories: ["FICTIONAL"] },
          ),
          card(
            "Next in line",
            h(
              "div",
              null,
              h("p", { class: "secondary", style: { marginTop: 0 } }, "The provinces that would receive seat 151, 152, … if the House were larger."),
              dataTable(
                [
                  { key: "rank", label: "Seat #", align: "r" },
                  { key: "province", label: "Province", format: (v) => provinceName(v) },
                  { key: "province_seat", label: "Would be seat", align: "r" },
                  { key: "priority", label: "Priority value", align: "r", format: (v) => fmtInt(v) },
                ],
                a.first_out || [],
                { sortKey: "rank", sortDir: "asc" },
              ),
            ),
            { categories: ["FICTIONAL"] },
          ),
        ),
        methods.length
          ? card(
              "Apportionment methods compared",
              dataTable(
                [
                  { key: "province", label: "Province", format: (v) => provinceName(v) },
                  { key: "population", label: "Population", align: "r", format: (v) => fmtInt(v) },
                  ...methods.map((m) => ({ key: m, label: humanize(m), align: "r", format: (v, r) => (v !== r[a.method] ? h("b", { title: `differs from ${humanize(a.method)}` }, fmtInt(v)) : fmtInt(v)) })),
                ],
                a.method_comparison,
                { sortKey: "population", sortDir: "desc" },
              ),
              { categories: ["FICTIONAL"], flush: true, foot: `Bold = differs from the active method (${humanize(a.method)}). The method is configurable in config/constitution.yaml.` },
            )
          : null,
      );
    });
  }

  /* ------------------------------------------------------------------ district plan */
  function plan() {
    refresh(body, api.get("/api/districts/plan", { signal, cache: true }), (p) => {
      const v = p.validation || {};
      const dev = p.deviation || {};
      const cmp = p.compactness || {};
      return h(
        "div",
        { class: "grid" },
        h(
          "div",
          { class: "grid grid--4" },
          kpi("Districts", fmtInt(v.total_districts), v.ok ? statusBadge("ok", "plan valid") : statusBadge("fail", "plan invalid")),
          kpi("Max deviation", `${fmt2(dev.max_abs_pct)}%`, `mean ${fmt2(dev.mean_abs_pct)}% · range ${fmtPP(dev.min_pct, 2)} to ${fmtPP(dev.max_pct, 2)}%`),
          kpi("Split municipalities", fmtInt(v.split_municipalities), `${fmtInt(v.noncontiguous_districts)} non-contiguous districts`),
          kpi("Compactness", fmt2(cmp.mean_polsby_popper), `mean Polsby–Popper (min ${fmt2(cmp.min_polsby_popper)})`),
        ),
        h(
          "div",
          { class: "grid grid--2" },
          card(
            "Plan",
            kv([
              ["Name", p.name],
              ["Chamber", humanize(p.chamber)],
              ["Method", humanize(p.method)],
              ["Seed", fmtInt(p.seed)],
              ["Config hash", h("span", { class: "mono" }, p.config_hash || "–")],
              ["Generated", fmtDateTime(p.created_at)],
              ["Generation time", p.generation_seconds !== null && p.generation_seconds !== undefined ? `${fmt2(p.generation_seconds)} s` : "–"],
              ["Manual overrides", fmtInt(p.overrides_applied)],
              ["Active", p.is_active ? "yes" : "no"],
            ], { cols: 1 }),
            { categories: ["FICTIONAL"], foot: "Reproducible: the same seed and configuration regenerate an identical plan (python -m app districts generate --seed …)." },
          ),
          card(
            "Validation",
            h(
              "div",
              null,
              v.ok ? callout("ok", "All plan invariants hold: 150 districts, each inside one province, every neighbourhood assigned exactly once, contiguity and population bounds.") : callout("warn", "The plan has problems:"),
              (v.problems || []).length ? h("ul", null, v.problems.map((x) => h("li", null, x))) : null,
              h("p", null, h("a", { href: "#/house", class: "ana-link" }, "Open the House map of all 150 districts →")),
            ),
            { categories: ["FICTIONAL"] },
          ),
        ),
        card(
          "Districts by province",
          dataTable(
            [
              { key: "name", label: "Province", format: (vv, r) => h("a", { href: links.province(r.code), class: "ana-link" }, vv) },
              { key: "districts", label: "Districts", align: "r" },
              { key: "population", label: "Population", align: "r", format: (x) => fmtInt(x) },
              { key: "target_population", label: "Target per district", align: "r", format: (x) => fmtInt(x) },
              { key: "max_abs_deviation_pct", label: "Max |deviation|", align: "r", format: (x) => `${fmt2(x)}%` },
              { key: "split_municipalities", label: "Split municipalities", align: "r" },
            ],
            p.provinces || [],
            { sortKey: "districts", sortDir: "desc" },
          ),
          { categories: ["REAL", "FICTIONAL"], flush: true },
        ),
      );
    });
  }

  /* ------------------------------------------------------------------ validation */
  function validation(withElections = false) {
    refresh(body, api.get(`/api/data/validation?elections=${withElections}`, { signal, timeout: 180000 }), (v) =>
      h(
        "div",
        { class: "grid" },
        v.ok ? callout("ok", `All ${fmtInt(v.checks?.length)} checks passed${v.elections_checked ? ", including the exact reconciliation of every stored election" : ""}.`) : callout("warn", `${fmtInt(v.errors?.length)} errors, ${fmtInt(v.warnings?.length)} warnings.`),
        card(
          "Constitutional and data-integrity checks",
          dataTable(
            [
              { key: "ok", label: "Result", value: (r) => (r.ok ? 1 : 0), format: (x, r) => (r.ok ? statusBadge("ok", "pass") : statusBadge(r.severity === "warning" ? "warn" : "fail", r.severity === "warning" ? "warning" : "fail")) },
              { key: "name", label: "Check", format: (x) => h("b", null, humanize(x)) },
              { key: "detail", label: "Detail" },
            ],
            v.checks || [],
            { sortKey: "ok", sortDir: "asc" },
          ),
          {
            flush: true,
            categories: ["FICTIONAL", "REAL"],
            actions: v.elections_checked
              ? null
              : h("button", { class: "btn btn--sm", onclick: () => validation(true) }, "Also reconcile every election (≈ 10 s)"),
          },
        ),
      ),
    );
  }

  /* ------------------------------------------------------------------ exports */
  function exports() {
    const id = currentElectionId();
    const election = (getState().meta?.elections || []).find((e) => e.id === id);
    const reported = ["final", "certified"].includes(String(election?.status || "").toLowerCase());
    const hasForecast = api
      .get(`/api/forecast/${id}/latest`, { signal })
      .then(() => true)
      .catch(() => false);
    const payload = Promise.all([api.get("/api/export/schemas", { signal, cache: true }), hasForecast]).then(([s, fc]) => ({ ...s, hasForecast: fc }));
    const swingRace = String(election?.election_type || "general") === "general" ? "PRES" : "HOUSE";
    const unavailable = (name, s) => {
      if (name.startsWith("montecarlo") && !s.hasForecast) return "no forecast run for this election";
      if (name === "swing" && !election?.previous_election_id) return "no earlier election to compare with";
      return null;
    };
    refresh(body, payload, (s) =>
      h(
        "div",
        { class: "grid" },
        h(
          "div",
          { class: "page-head__meta" },
          h("span", { class: "muted" }, "Election:"),
          h(
            "select",
            {
              class: "select",
              "aria-label": "Election to export",
              onchange: (e) => (location.hash = `#/data?tab=exports&e=${e.target.value}`),
            },
            (getState().meta?.elections || []).map((e) => h("option", { value: e.id, selected: e.id === id }, `${e.year} · ${e.name} (${e.status})`)),
          ),
          election ? provBadge(reported ? "SIMULATED" : "FICTIONAL", reported ? "results reported" : "results not reported yet") : null,
        ),
        !reported ? callout("info", "Result, timeline and call datasets become available once the election's night has finished (they would reveal hidden results). Forecasts, polls, districts and apportionment are always available.") : null,
        card(
          `Datasets · ${election ? `${election.year} ${election.name}` : ""}`,
          dataTable(
            [
              { key: "name", label: "Dataset", format: (v, r) => h("span", null, h("b", null, humanize(r.aliases?.[0] || v)), h("br"), h("span", { class: "muted mono" }, v)) },
              { key: "description", label: "Contents" },
              { key: "data_category", label: "Category", format: (v) => provBadge(v) },
              { key: "columns", label: "Columns", align: "r", value: (r) => r.columns?.length || 0 },
              {
                key: "dl",
                label: "Download",
                sort: false,
                format: (_v, r) => {
                  const name = r.aliases?.[0] || r.name;
                  const blocked = r.requires_reported_election && !reported;
                  if (blocked) return h("span", { class: "muted" }, "after the night");
                  const why = unavailable(name, s);
                  if (why) return h("span", { class: "muted" }, why);
                  const q = name === "units" ? "?race_type=PRESIDENT_PROVINCE" : name === "swing" ? `?race=${swingRace}` : "";
                  return h("span", { class: "page-head__meta" }, downloadLink(`/api/export/${id}/${name}.csv${q}`, "CSV"), downloadLink(`/api/export/${id}/${name}.json${q}`, "JSON"));
                },
              },
            ],
            s.datasets || [],
            { sortKey: "name", sortDir: "asc" },
          ),
          { flush: true, foot: "Stable, versioned column schemas (docs/ANALYTICS.md). JSON files carry an envelope with the schema version, data category, generation time and election metadata. The same exports are available from the CLI: python -m app export." },
        ),
      ),
    );
  }

  show();
  return () => ctrl.abort();
}
