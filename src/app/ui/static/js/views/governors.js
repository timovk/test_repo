/**
 * #/governors — the 12 governor races (with lieutenant-governor running mates): governorships
 * before vs after, a province map, one card per race (status, leader, margin, reporting,
 * incumbent, flip) and a table.  When the election holds municipal races (the 2026 midterm), the
 * 342 mayor races follow as a map and a searchable, sortable table.  Elections without governor
 * races show the serving governors (FICTIONAL office holders) instead.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtInt, fmtPct, fmtPP } from "../format.js";
import { partyChip, provBadge } from "../components/badges.js";
import { dataTable } from "../components/table.js";
import { stackBar } from "../components/res-charts.js";
import { cssVar, electionList, flipTag, liveRefresh, marginLabel, parties, partyShade, partyTag, pc, raceColor, racePill, raceState, reportingMeter, resultRows, searchInput, swatchLegend } from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const ctrl = new AbortController();
  const frame = pageFrame(el, {
    eyebrow: "Results · Governors & Mayors",
    title: "Governors",
    categories: ["SIMULATED", "FICTIONAL"],
    electionId: id,
    noticeText: "Candidates, running mates (lieutenant governors) and incumbents are shown.",
  });
  const ui = { q: "", mq: "" };

  let gov = null;
  let may = null;
  let order = [];
  let provinces = [];
  let govMap = null;
  let mayMap = null;
  let govTable = null;
  let mayTable = null;
  let lastSource = null;

  const sumCard = liveCard("Governorships", { id: "res-g-sum" });
  const mapCard = liveCard("Governor races", { id: "res-g-map", categories: [provBadge("REAL", "Real boundaries")] });
  const mapHost = h("div", { class: "res-map-host" });
  const mapLegend = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapHost, mapLegend);
  const racesHost = h("div", { class: "res-govgrid" });
  const tableCard = liveCard("All governor races", { id: "res-g-table", flush: true });
  const mayHead = h("div", { class: "res-section-title" });
  const maySum = liveCard("Mayors", { id: "res-m-sum" });
  const mayMapCard = liveCard("Mayor races", { id: "res-m-map", categories: [provBadge("REAL", "Real boundaries")] });
  const mayMapHost = h("div", { class: "res-map-host" });
  const mayLegend = h("div", { class: "res-map-legend" });
  mayMapCard.body.append(mayMapHost, mayLegend);
  const mayTableCard = liveCard("All mayor races", { id: "res-m-table", flush: true, categories: [provBadge("REAL", "Municipalities REAL"), provBadge("SIMULATED", "Results SIMULATED")] });
  const mayTools = h("div", { class: "res-card-tools res-card-tools--pad" });
  const mayTableHost = h("div");
  mayTableCard.body.append(mayTools, mayTableHost);

  const provName = (c) => provinces.find((p) => p.code === c)?.name || c;
  const ordered = (obj) => {
    const k = Object.keys(obj || {});
    return [...order.filter((c) => k.includes(c)), ...k.filter((c) => !order.includes(c))];
  };
  const barFrom = (obj, total, label) =>
    stackBar(
      ordered(obj).map((p) => ({ label: p === "independent" ? "Independent" : p, value: obj[p], color: pc(p === "independent" ? null : p, p === "independent" ? cssVar("--text-muted") : undefined) })),
      { total, label, format: (v) => fmtInt(v) },
    );

  // ------------------------------------------------------------ governors
  function paintGovSummary() {
    const src = gov.results_source;
    const bp = gov.by_party || {};
    keyed(sumCard.body, JSON.stringify([src, bp, gov.flips]), () =>
      h(
        "div",
        { class: "res-govsum" },
        h("div", { class: "res-govsum__bar" }, h("div", { class: "res-subhead" }, "Before the election"), Object.keys(bp.previous || {}).length ? barFrom(bp.previous, 12, "Governorships before the election") : h("p", { class: "muted res-empty" }, "No previous governors (founding election).")),
        h(
          "div",
          { class: "res-govsum__bar" },
          h("div", { class: "res-subhead" }, src === "live" ? "Decided so far" : "After the election"),
          bp.won ? barFrom(bp.won, 12, "Governorships after the election") : h("p", { class: "muted res-empty" }, src === "hidden" ? "Results hidden until reported." : "No race decided yet."),
        ),
        h("div", { class: "res-govsum__flips" }, h("span", { class: "res-out__big num" }, src === "hidden" ? "–" : String(gov.flips ?? 0)), h("span", { class: "muted" }, src === "live" ? "flips so far" : "governorships changed party")),
      ),
    );
  }

  function specGov(f) {
    const r = (gov.governors || []).find((g) => g.province_code === f.properties.code);
    if (!r) return { none: true };
    const col = raceColor(r);
    if (col) return { fill: col, state: raceState(r) };
    return r.incumbent?.party ? { fill: pc(r.incumbent.party), state: "lean" } : r.previous_party ? { fill: pc(r.previous_party), state: "lean" } : {};
  }
  function tipGov(f) {
    const r = (gov.governors || []).find((g) => g.province_code === f.properties.code);
    if (!r) return h("div", { class: "res-tip" }, h("strong", null, f.properties.name), h("div", { class: "muted" }, "No governor race"));
    const top = [...(r.lines || [])].sort((a, b) => (b.votes ?? 0) - (a.votes ?? 0)).slice(0, 4);
    return h(
      "div",
      { class: "res-tip" },
      h("div", { class: "res-tip__head" }, h("strong", null, f.properties.name), racePill(r)),
      h("div", { class: "res-tip__sub muted" }, r.incumbent ? `Incumbent ${r.incumbent.name} (${r.incumbent.party})${r.incumbent.running === false ? " · retiring" : ""}` : "Open seat"),
      h(
        "div",
        { class: "res-tip__rows" },
        top.map((l) => h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(l.party, l.color) } }), h("span", { class: "res-tip__name" }, `${l.name} / ${l.running_mate_name || "–"} (${l.party})`), h("b", { class: "num" }, l.pct !== null && l.pct !== undefined ? fmtPct(l.pct) : ""), h("span", { class: "res-tip__votes num" }, l.votes ? fmtInt(l.votes) : ""))),
      ),
      h("div", { class: "res-tip__kv" }, r.margin_pp !== null && r.margin_pp !== undefined ? h("span", null, "Margin ", h("b", null, marginLabel(r.winner_party || r.leader_party, r.margin_pp))) : null, r.reporting_pct !== null && r.reporting_pct !== undefined && gov.results_source === "live" ? h("span", null, "Reporting ", h("b", null, fmtPct(r.reporting_pct))) : null, r.flip_status ? flipTag(r.flip_status, { prev: r.previous_party }) : null),
    );
  }
  function paintGovMap() {
    const src = gov.results_source;
    const present = ordered(Object.fromEntries((gov.governors || []).map((r) => [r.winner_party || r.leader_party || (src === "hidden" ? r.incumbent?.party || r.previous_party : null), 1]).filter(([k]) => k && k !== "null")));
    mount(
      mapLegend,
      h(
        "div",
        { class: "res-legend-stack" },
        swatchLegend(present.map((p) => ({ color: src === "hidden" ? partyShade(pc(p), 0.6) : pc(p), label: p })), { title: src === "hidden" ? "Incumbent party" : src === "final" ? "Winner" : "Leader" }),
        src === "live" ? swatchLegend([{ color: cssVar("--text-secondary"), label: "Called" }, { color: partyShade(cssVar("--text-secondary"), 0.3), label: "Leading" }, { kind: "close", label: "Too close" }]) : null,
      ),
    );
    if (govMap) return govMap.update();
    govMap = resMap(mapHost, { layer: "provinces", outline: null, height: 520, label: "Map of the 12 governor races; the race cards and table list the same results", spec: specGov, tooltip: tipGov, onClick: (f) => (location.hash = links.province(f.properties.code)) });
    govMap.ready.catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
  }

  function govCard(r) {
    const src = gov.results_source;
    const card = h(
      "article",
      { class: "res-gov", style: { "--party": raceColor(r) || "var(--uncalled)" } },
      h("header", { class: "res-gov__head" }, h("a", { href: links.province(r.province_code), class: "res-gov__prov" }, h("span", { class: "res-code" }, r.province_code), provName(r.province_code)), racePill(r)),
      h(
        "div",
        { class: "res-gov__meta muted" },
        [src === "live" && r.reporting_pct !== null && r.reporting_pct !== undefined ? `${fmtPct(r.reporting_pct)} reporting` : null, r.margin_pp !== null && r.margin_pp !== undefined ? `margin ${fmtPP(r.margin_pp)} pp` : null, src === "final" && r.turnout_pct ? `turnout ${fmtPct(r.turnout_pct)}` : null].filter(Boolean).join(" · ") || (r.incumbent ? `Incumbent ${r.incumbent.name} (${r.incumbent.party})${r.incumbent.running === false ? " · retiring" : " · running"}` : "Open seat"),
      ),
      h("div", { class: "res-gov__lines" }),
      h("footer", { class: "res-gov__foot" }, r.open_seat ? h("span", { class: "res-tag res-tag--open" }, "OPEN SEAT") : r.incumbent ? h("span", { class: "muted" }, "Inc. ", h("b", null, r.incumbent.name)) : null, r.flip_status ? flipTag(r.flip_status, { prev: r.previous_party }) : r.previous_party ? h("span", { class: "muted" }, `held by ${r.previous_party}`) : null),
    );
    card.querySelector(".res-gov__lines").append(resultRows(r.lines || [], { compact: true, max: 3, leaderKey: r.leader, mateLabel: "Lt. Gov." }));
    return card;
  }
  function paintGovRaces() {
    const list = [...(gov.governors || [])].sort((a, b) => provName(a.province_code).localeCompare(provName(b.province_code), "nl"));
    keyed(racesHost, JSON.stringify(list.map((r) => [r.code, r.status, r.leader, r.winner, r.reporting_pct, (r.lines || []).map((l) => l.votes)])), () => list.map(govCard));
    const src = gov.results_source;
    const rows = list.map((r) => ({ ...r, _leader: r.winner_party || r.leader_party || "", _lg: (r.lines || []).find((l) => l.key === (r.winner || r.leader))?.running_mate_name || "" }));
    if (!govTable || govTable._src !== src) {
      govTable = dataTable(
        [
          { key: "province_code", label: "Province", value: (r) => provName(r.province_code), format: (v, r) => h("a", { href: links.province(r.province_code), class: "res-link" }, h("span", { class: "res-code" }, r.province_code), v) },
          { key: "status", label: "Status", format: (v, r) => racePill(r) },
          { key: "_leader", label: src === "final" ? "Governor-elect" : "Leader", format: (v, r) => (v ? partyTag(v, raceColor(r), r.winner_name || r.leader_name) : h("span", { class: "muted" }, "–")) },
          { key: "_lg", label: "Lt. Governor", format: (v) => v || h("span", { class: "muted" }, "–") },
          { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) },
          src === "live" ? { key: "reporting_pct", label: "Reporting", format: (v) => reportingMeter(v, { width: 46 }) } : { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
          { key: "incumbent", label: "Incumbent", value: (r) => r.incumbent?.name || "", format: (v, r) => (r.incumbent ? h("span", { class: "res-who" }, partyChip(r.incumbent.party, { color: pc(r.incumbent.party) }), h("span", { class: "res-who__name" }, r.incumbent.name), r.incumbent.running === false ? h("span", { class: "res-tag" }, "RETIRING") : null) : h("span", { class: "muted" }, "Open")) },
          { key: "flip_status", label: "Flip", format: (v, r) => flipTag(v, { prev: r.previous_party }) },
        ],
        rows,
        { sortKey: "province_code", sortDir: "asc", rowHref: (r) => links.province(r.province_code), rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: "Governor races" },
      );
      govTable._src = src;
      mount(tableCard.body, govTable);
    } else govTable.update(rows);
  }

  /** Elections without governor races: the serving governors (office holders, FICTIONAL). */
  function paintServing(el2) {
    const other = electionsWith("GOVERNOR");
    mount(
      el2,
      h(
        "div",
        { class: "notice res-notice" },
        provBadge("FICTIONAL"),
        h("span", null, h("strong", null, "No governor races in this election. "), "Governors serve four-year terms; the serving governors and lieutenant governors are listed below.", other.length ? [" Governor races: ", other.map((e, i) => [i ? ", " : "", h("a", { href: `#/governors?e=${e.id}`, class: "res-link" }, `${e.year}`)])] : null),
      ),
      h(
        "div",
        { class: "res-govgrid" },
        provinces.map((p) =>
          h(
            "article",
            { class: "res-gov", style: { "--party": pc(p.governor?.party) } },
            h("header", { class: "res-gov__head" }, h("a", { href: links.province(p.code), class: "res-gov__prov" }, h("span", { class: "res-code" }, p.code), p.name), h("span", { class: "res-tag" }, "SERVING")),
            p.governor ? h("div", { class: "res-gov__serving" }, partyTag(p.governor.party, pc(p.governor.party), p.governor.name), h("span", { class: "muted" }, `term ${String(p.governor.term_start || "").slice(0, 4)}–${String(p.governor.term_end || "").slice(0, 4)}`)) : h("span", { class: "muted" }, "Vacant"),
          ),
        ),
      ),
    );
  }

  let elections = [];
  const electionsWith = (type) => elections.filter((e) => e.contents?.race_counts?.[type] && e.id !== id);

  // ------------------------------------------------------------ mayors
  function paintMayors(first) {
    const src = may.results_source;
    const rows = may.mayors || [];
    mount(mayHead, h("h2", null, `Mayors · ${fmtInt(may.races)} municipal races`), h("span", { class: "res-muted-note" }, "Mayors are elected in every municipality in midterm years (FICTIONAL office)."));
    keyed(maySum.body, JSON.stringify([src, may.by_party, may.flips]), () =>
      h(
        "div",
        { class: "res-govsum" },
        h("div", { class: "res-govsum__bar res-govsum__bar--wide" }, h("div", { class: "res-subhead" }, src === "live" ? "Mayoralties decided so far" : "Mayoralties won"), may.by_party?.won ? barFrom(may.by_party.won, may.races, "Mayoralties won by party") : h("p", { class: "muted res-empty" }, "Results hidden until reported.")),
        h("div", { class: "res-govsum__flips" }, h("span", { class: "res-out__big num" }, src === "hidden" ? "–" : String(may.flips ?? 0)), h("span", { class: "muted" }, "mayoralties changed party")),
      ),
    );
    const byCode = Object.fromEntries(rows.map((r) => [r.code, r]));
    const spec = (f) => {
      const r = byCode[f.properties.code];
      if (!r) return { none: true };
      const col = raceColor(r);
      return col ? { fill: col, state: raceState(r) } : r.incumbent?.party ? { fill: pc(r.incumbent.party), state: "lean" } : {};
    };
    const tip = (f) => {
      const r = byCode[f.properties.code];
      if (!r) return h("div", { class: "res-tip" }, h("strong", null, f.properties.name));
      return h(
        "div",
        { class: "res-tip" },
        h("div", { class: "res-tip__head" }, h("strong", null, r.name), racePill(r)),
        h("div", { class: "res-tip__sub muted" }, `${provName(r.province_code)} · ${r.open_seat ? "open seat" : r.incumbent ? `incumbent ${r.incumbent.name} (${r.incumbent.party})` : ""}`),
        h("div", { class: "res-tip__rows" }, (r.top || []).map((l) => h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(l.party, l.color) } }), h("span", { class: "res-tip__name" }, `${l.name} (${l.party || "IND"})`), h("b", { class: "num" }, l.pct !== null && l.pct !== undefined ? fmtPct(l.pct) : ""), h("span", { class: "res-tip__votes num" }, l.votes ? fmtInt(l.votes) : "")))),
        h("div", { class: "res-tip__kv" }, r.margin_pp !== null && r.margin_pp !== undefined ? h("span", null, "Margin ", h("b", null, marginLabel(r.winner_party || r.leader_party, r.margin_pp))) : null, r.turnout_pct ? h("span", null, "Turnout ", h("b", null, fmtPct(r.turnout_pct))) : null, r.flip_status ? flipTag(r.flip_status, { prev: r.previous_party }) : null),
      );
    };
    const present = ordered(Object.fromEntries(rows.map((r) => [r.winner_party || r.leader_party, 1]).filter(([k]) => k && k !== "null" && k !== "undefined")));
    mount(mayLegend, swatchLegend(present.map((p) => ({ color: p === "independent" ? cssVar("--text-muted") : pc(p), label: p === "independent" ? "Independent" : p })), { title: src === "final" ? "Winner" : "Leader" }));
    if (first || !mayMap) {
      mayMap?.destroy();
      mount(mayMapHost);
      mayMap = resMap(mayMapHost, { layer: "municipalities", outline: "provinces", height: 560, label: "Map of the 342 mayor races; the table below lists the same results", spec, tooltip: tip, onClick: (f) => (location.hash = links.municipality(f.properties.code)) });
      mayMap.ready.catch((err) => mount(mayMapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    } else mayMap.update(spec);
    paintMayTable(first);
  }
  function paintMayTable(rebuild) {
    const src = may.results_source;
    const q = ui.mq;
    const rows = (may.mayors || [])
      .filter((r) => !q || `${r.name} ${r.code} ${provName(r.province_code)} ${r.winner_name || ""} ${r.leader_name || ""} ${r.incumbent?.name || ""}`.toLowerCase().includes(q))
      .map((r) => ({ ...r, _leader: r.winner_party || r.leader_party || "", _ru: (r.top || []).find((l) => l.key !== (r.winner || r.leader)) || null }));
    if (rebuild || !mayTable) {
      mayTable = dataTable(
        [
          { key: "name", label: "Municipality", format: (v, r) => h("a", { href: links.municipality(r.code), class: "res-link" }, v) },
          { key: "province_code", label: "Prov.", format: (v) => h("span", { class: "res-code", title: provName(v) }, v) },
          { key: "status", label: "Status", format: (v, r) => racePill(r) },
          { key: "_leader", label: src === "final" ? "Mayor-elect" : "Leader", format: (v, r) => (v || r.leader_name ? partyTag(v || "IND", v === "independent" ? cssVar("--text-muted") : raceColor(r), r.winner_name || r.leader_name) : h("span", { class: "muted" }, "–")) },
          { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) },
          { key: "_ru", label: "Runner-up", value: (r) => r._ru?.name || "", format: (v, r) => (r._ru ? h("span", { class: "res-who" }, partyChip(r._ru.party || "IND", { color: pc(r._ru.party, r._ru.color) }), h("span", { class: "res-who__name" }, v)) : h("span", { class: "muted" }, "–")) },
          src === "live" ? { key: "reporting_pct", label: "Reporting", format: (v) => reportingMeter(v, { width: 46 }) } : { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
          { key: "incumbent", label: "Incumbent", value: (r) => r.incumbent?.name || "", format: (v, r) => (r.incumbent ? h("span", { class: "res-who" }, partyChip(r.incumbent.party || "IND", { color: pc(r.incumbent.party) }), h("span", { class: "res-who__name" }, r.incumbent.name)) : h("span", { class: "res-tag res-tag--open" }, "OPEN")) },
          { key: "flip_status", label: "Flip", format: (v, r) => flipTag(v, { prev: r.previous_party }) },
        ],
        rows,
        { sortKey: "name", sortDir: "asc", maxHeight: 620, rowHref: (r) => links.municipality(r.code), rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: "Mayor races" },
      );
      mount(mayTableHost, mayTable);
    } else mayTable.update(rows);
    const n = mayTools.querySelector(".res-muted-note");
    if (n) n.textContent = `${fmtInt(rows.length)} of ${fmtInt((may.mayors || []).length)} races`;
  }

  // ------------------------------------------------------------ data
  const fetchGov = () => api.get(`/api/elections/${id}/governors`, { signal: ctrl.signal });
  const fetchMay = () => api.get(`/api/elections/${id}/mayors`, { signal: ctrl.signal }).catch((err) => (err?.name === "AbortError" ? Promise.reject(err) : null));

  function layout() {
    const hasGov = (gov?.races || 0) > 0;
    const hasMay = (may?.races || 0) > 0;
    const parts = [];
    if (hasGov) parts.push(h("div", { class: "res-grid res-grid--map" }, mapCard, sumCard), racesHost, tableCard);
    else {
      const serving = h("div", { class: "res-stack" });
      paintServing(serving);
      parts.push(serving);
    }
    if (hasMay) parts.push(mayHead, h("div", { class: "res-grid res-grid--map" }, mayMapCard, maySum), mayTableCard);
    else {
      const other = electionsWith("MAYOR");
      parts.push(h("div", { class: "notice res-notice" }, provBadge("FICTIONAL"), h("span", null, h("strong", null, "No mayor races in this election. "), "Mayors and municipal councils are elected in midterm years.", other.length ? [" See ", other.map((e, i) => [i ? ", " : "", h("a", { href: `#/governors?e=${e.id}`, class: "res-link" }, `the ${e.year} ${e.election_type} mayors`)])] : null)));
    }
    mount(frame.body, ...parts);
  }

  function paint(first) {
    frame.setSource(gov?.races ? gov : may || gov);
    if (gov?.races) {
      paintGovSummary();
      paintGovMap();
      paintGovRaces();
    }
    if (may?.races) paintMayors(first);
  }

  try {
    const [g, m, p, pv, el2] = await Promise.all([fetchGov(), fetchMay(), parties(), api.get("/api/provinces", { cache: true }), electionList().catch(() => ({ elections: [] }))]);
    gov = g;
    may = m;
    order = p.order;
    provinces = pv.provinces || [];
    elections = el2.elections || [];
    lastSource = g.results_source;
    frame.setTitle(g.races ? "Governors" : "Governors & Mayors", `Results · Governors & Mayors · ${g.election?.name || ""}`);
    mount(mayTools, searchInput("Search municipality, candidate or incumbent…", (q) => {
      ui.mq = q;
      paintMayTable(false);
    }, { label: "Search mayor races" }), h("span", { class: "res-muted-note" }));
    layout();
    paint(true);
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, async () => {
    const [g, m] = await Promise.all([fetchGov(), may?.races ? fetchMay() : Promise.resolve(may)]);
    const changed = g.results_source !== lastSource;
    lastSource = g.results_source;
    gov = g;
    may = m;
    if (changed) {
      govMap?.destroy();
      mayMap?.destroy();
      govMap = null;
      mayMap = null;
      govTable = null;
      mayTable = null;
      mount(mapHost);
      layout();
    }
    paint(changed);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    govMap?.destroy();
    mayMap?.destroy();
  };
}
