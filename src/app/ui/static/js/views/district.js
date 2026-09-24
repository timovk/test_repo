/**
 * #/house/:code — House district page: candidates (votes, %, incumbency, winner), status,
 * margin, reporting / turnout, the call desk (live model evidence) and call log, the
 * municipality breakdown, district facts (FICTIONAL district over REAL population: deviation,
 * compactness, split municipalities), a district map fitted to the district, neighbours and the
 * district's history.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmt2, fmtInt, fmtPct, fmtPP, fmtProb, fmtShare } from "../format.js";
import { partyChip, provBadge, statusPill } from "../components/badges.js";
import { dataTable } from "../components/table.js";
import { cssVar, facts, flipTag, liveRefresh, marginLabel, pc, raceColor, racePill, reportingMeter, resultRows, tile } from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const code = String(params.code || "").toUpperCase();
  const ctrl = new AbortController();
  const frame = pageFrame(el, { eyebrow: `House district · ${code}`, title: code, categories: ["FICTIONAL", "SIMULATED"], electionId: id });

  let data = null;
  let facts0 = null;
  let map = null;
  let muniTable = null;
  let lastSource = null;

  const raceCard = liveCard("Race", { id: "res-d-race" });
  const raceTiles = h("div", { class: "res-tiles res-hero-tiles" });
  const raceLines = h("div");
  raceCard.body.append(raceTiles, raceLines);
  const raceFoot = h("div", { class: "card__foot res-hero-foot" });
  raceCard.append(raceFoot);
  const factsCard = liveCard("District facts", { id: "res-d-facts", categories: [provBadge("FICTIONAL", "Fictional district"), provBadge("REAL", "Real population")] });
  const mapCard = liveCard("District map", { id: "res-d-map", categories: [provBadge("FICTIONAL", "Fictional boundary")] });
  const mapHost = h("div", { class: "res-map-host" });
  mapCard.body.append(mapHost, h("div", { class: "res-map-legend" }));
  const muniCard = liveCard("Municipality breakdown", { id: "res-d-muni", flush: true });
  const deskCard = liveCard("Decision desk", { id: "res-d-desk" });
  const callsCard = liveCard("Call log", { id: "res-d-calls", flush: true });
  const histCard = liveCard("District history", { id: "res-d-hist", flush: true, categories: [provBadge("SIMULATED")] });
  const nbCard = liveCard("Neighbouring districts", { id: "res-d-nb" });

  const lineByKey = () => Object.fromEntries((data?.race?.lines || []).map((l) => [l.key, l]));
  const lineName = (k) => {
    const l = lineByKey()[k];
    return l ? (l.candidate || l.president)?.name || l.name : k;
  };

  function paintRace() {
    const r = data.race;
    const src = data.results_source;
    keyed(raceCard.meta, `${r.status}|${raceColor(r)}`, () => racePill(r));
    raceCard.setTitle(r.name || "Race");
    const leaderParty = r.winner_party || r.leader_party;
    keyed(raceTiles, JSON.stringify([r.status, leaderParty, r.margin_pp, r.reporting_pct, r.turnout_pct, r.total_votes, r.win_probability]), () => [
      tile(r.winner ? "Winner" : "Leader", leaderParty ? partyChip(leaderParty, { color: raceColor(r) }) : "–", r.winner_name || r.leader_name || (src === "hidden" ? "Results hidden" : "No votes yet"), { accent: raceColor(r) || undefined }),
      tile("Margin", r.margin_pp !== null && r.margin_pp !== undefined ? `${fmtPP(r.margin_pp)} pp` : "–", r.margin_votes !== null && r.margin_votes !== undefined ? `${fmtInt(r.margin_votes)} votes` : "winner − runner-up"),
      src === "final" ? tile("Turnout", fmtPct(r.turnout_pct), r.ballots_cast ? `${fmtInt(r.ballots_cast)} ballots` : null) : tile("Reporting", r.reporting_pct !== null && r.reporting_pct !== undefined ? fmtPct(r.reporting_pct) : "–", "of expected vote"),
      src === "live" && r.win_probability !== null && r.win_probability !== undefined ? tile("Call model", fmtProb(r.win_probability), "leader's win probability") : tile("Votes counted", fmtInt(r.total_votes), r.eligible ? `${fmtInt(r.eligible)} eligible` : null),
    ]);
    const opts = { leaderKey: r.leader };
    if (!raceLines.firstChild?.update) mount(raceLines, resultRows(r.lines || [], opts));
    else raceLines.firstChild.update(r.lines || [], opts);
    const inc = r.incumbent;
    keyed(raceFoot, JSON.stringify([inc, r.open_seat, r.previous_party, r.flip_status, r.decided_by, data.previous_race]), () => [
      inc ? h("span", null, "Incumbent: ", h("b", null, inc.name), ` (${inc.party})`, inc.running === false ? " · retiring" : " · running") : h("span", null, "No incumbent"),
      r.open_seat ? h("span", null, " · ", h("span", { class: "res-tag res-tag--open" }, "OPEN SEAT")) : null,
      r.previous_party ? h("span", null, " · Held by ", h("b", null, r.previous_party)) : null,
      r.flip_status ? h("span", null, " · ", flipTag(r.flip_status, { prev: r.previous_party })) : null,
      r.decided_by ? h("span", null, ` · Decided by ${String(r.decided_by).replace(/_/g, " ")}`) : null,
      data.previous_race ? h("span", null, ` · ${data.previous_race.year}: ${data.previous_race.winner_name} (${data.previous_race.winner_party}) +${fmtPP(data.previous_race.margin_pp).replace(/^[+−±]/, "")} pp`) : null,
    ]);
  }

  function paintFacts() {
    const d = facts0 || {};
    const dd = data.district || {};
    mount(
      factsCard.body,
      facts(
        [
          { label: "Population", value: fmtInt(d.population ?? dd.population), cat: "REAL" },
          { label: "Eligible voters", value: fmtInt(d.eligible_voters_est ?? dd.eligible_voters_est), cat: "DERIVED", note: "estimated" },
          { label: "Target population", value: fmtInt(d.target_population), cat: "DERIVED", note: "province quota per seat" },
          { label: "Deviation", value: `${fmtPP(d.deviation_pct ?? dd.deviation_pct, 2)}%`, cat: "DERIVED", note: "from target" },
          { label: "Area", value: `${fmtInt(d.area_km2 ?? dd.area_km2)} km²`, cat: "DERIVED" },
          { label: "Urban share", value: fmtShare(d.urban_share ?? dd.urban_share), cat: "DERIVED", note: d.rural_share !== undefined ? `${fmtShare(d.rural_share)} rural` : null },
          { label: "Polsby–Popper", value: fmt2(d.polsby_popper), cat: "DERIVED", note: "compactness 0–1" },
          { label: "Reock", value: fmt2(d.reock), cat: "DERIVED", note: `convex hull ${fmt2(d.convex_hull_ratio)}` },
          { label: "Municipalities", value: fmtInt(d.municipality_count ?? dd.municipality_count), cat: "REAL", note: `${fmtInt(d.split_municipalities)} split with other districts` },
          { label: "Neighbourhoods", value: fmtInt(d.unit_count ?? dd.unit_count), cat: "REAL", note: d.is_contiguous === false ? `${d.components} parts (non-contiguous)` : "contiguous" },
          { label: "Current holder", value: d.holder ? h("span", { class: "res-facts__person" }, partyChip(d.holder.party, { color: pc(d.holder.party) }), d.holder.name) : "Vacant", cat: "FICTIONAL", note: d.holder?.term_end ? `term to ${String(d.holder.term_end).slice(0, 4)}` : null },
          { label: "Plan", value: d.plan?.name || "–", cat: "FICTIONAL", note: d.plan?.seed ? `seed ${d.plan.seed}` : null },
        ],
        { cols: 2 },
      ),
    );
  }

  function paintMap() {
    if (map) {
      map.update();
      return;
    }
    const nb = new Set((facts0?.neighbours || []).map((n) => n.code));
    const spec = (f) => {
      const c = f.properties.code;
      if (c === code) return { fill: raceColor(data.race) || cssVar("--text-muted"), state: raceColor(data.race) ? "called" : "lean", emphasis: true };
      if (nb.has(c)) return { fill: cssVar("--uncalled") };
      return { none: true };
    };
    map = resMap(mapHost, {
      layer: "districts",
      outline: "municipalities",
      outlineWeight: 0.8,
      height: 420,
      label: `Map of district ${code} with its neighbours; municipal boundaries drawn as thin lines`,
      spec,
      tooltip: (f) => h("div", { class: "res-tip" }, h("div", { class: "res-tip__head" }, h("strong", null, f.properties.name), h("span", { class: "res-code" }, f.properties.code)), h("div", { class: "res-tip__sub muted" }, `pop. ${fmtInt(f.properties.population)} · deviation ${fmtPP(f.properties.deviation_pct, 2)}%`), f.properties.code !== code ? h("div", { class: "muted", style: { fontSize: "12px" } }, "Click to open this district") : null),
      onClick: (f) => f.properties.code !== code && (location.hash = links.district(f.properties.code)),
    });
    map.ready.then(() => map.fitTo((f) => f.properties.code === code)).catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    mount(mapCard.body.querySelector(".res-map-legend"), h("div", { class: "legend res-legend" }, h("span", { class: "legend__item" }, h("span", { class: "res-swatch", style: { "--sw": raceColor(data.race) || cssVar("--text-muted") } }), `${code} (${data.results_source === "hidden" ? "results hidden" : "leader / winner colour"})`), h("span", { class: "legend__item" }, h("span", { class: "res-swatch", style: { "--sw": cssVar("--uncalled") } }), "Neighbouring districts")));
  }

  function paintMunis(rebuild) {
    const src = data.results_source;
    const frag = Object.fromEntries((facts0?.municipalities || []).map((m) => [m.code, m]));
    const res = Object.fromEntries((data.municipalities || []).map((m) => [m.code, m]));
    const codes = [...new Set([...(facts0?.municipalities || []).map((m) => m.code), ...Object.keys(res)])];
    const lines = [...(data.race?.lines || [])].sort((a, b) => (b.votes ?? 0) - (a.votes ?? 0)).slice(0, 3);
    const rows = codes.map((c) => ({ code: c, name: frag[c]?.name || res[c]?.name || c, f: frag[c] || {}, r: res[c] || null }));
    if (rebuild || !muniTable) {
      muniTable = dataTable(
        [
          { key: "name", label: "Municipality", format: (v, x) => h("a", { href: links.municipality(x.code), class: "res-link" }, v) },
          { key: "pop", label: "Population here", align: "r", value: (x) => x.f.population ?? null, format: (v) => fmtInt(v) },
          { key: "sod", label: "Of district", align: "r", value: (x) => x.f.share_of_district ?? null, format: (v) => fmtShare(v) },
          { key: "som", label: "Of municipality", align: "r", value: (x) => x.f.share_of_municipality ?? null, format: (v) => (v !== null && v < 0.999 ? h("span", { title: "Municipality split between districts" }, fmtShare(v), " ", h("span", { class: "res-tag" }, "SPLIT")) : fmtShare(v)) },
          src === "live" ? { key: "rep", label: "Reporting", value: (x) => x.r?.reporting_pct ?? null, format: (v) => reportingMeter(v, { width: 46 }) } : null,
          src === "final" ? { key: "votes", label: "Votes", align: "r", value: (x) => x.r?.total_votes ?? null, format: (v) => fmtInt(v) } : null,
          src === "final" ? { key: "lead", label: "Leader", value: (x) => x.r?.leader_party || "", format: (v, x) => (v ? partyChip(v, { color: pc(v, x.r?.leader_color) }) : "–") } : null,
          src === "final" ? { key: "margin", label: "Margin", align: "r", value: (x) => x.r?.margin_pp ?? null, format: (v) => marginLabel(null, v) } : null,
          ...(src !== "hidden"
            ? lines.map((l) => ({
                key: `l_${l.key}`,
                label: `${l.candidate?.name || l.name} (${l.party})`,
                align: "r",
                value: (x) => (src === "final" ? x.r?.pct?.[l.key] ?? null : x.r?.votes?.[l.key] ?? null),
                format: (v) => (v === null || v === undefined ? "–" : src === "final" ? fmtPct(v) : fmtInt(v)),
              }))
            : []),
          src === "final" ? { key: "turnout", label: "Turnout", align: "r", value: (x) => x.r?.turnout_pct ?? null, format: (v) => fmtPct(v) } : null,
        ].filter(Boolean),
        rows,
        { sortKey: "pop", sortDir: "desc", rowHref: (x) => links.municipality(x.code), caption: "Municipalities in this district" },
      );
      mount(muniCard.body, muniTable);
    } else muniTable.update(rows);
    muniCard.setTitle(`Municipality breakdown · ${rows.length}`);
    if (src === "live") keyed(muniCard.meta, "live", () => h("span", { class: "res-muted-note" }, "live: counted votes"));
  }

  function paintDesk() {
    const src = data.results_source;
    const dec = data.decision;
    deskCard.hidden = src !== "live" || !dec;
    if (deskCard.hidden) return;
    const keys = Object.keys(dec.projected_share_mean || dec.win_probability || {}).sort((a, b) => (dec.projected_share_mean?.[b] ?? 0) - (dec.projected_share_mean?.[a] ?? 0)).slice(0, 5);
    const max = Math.max(0.01, ...keys.map((k) => dec.projected_share_p95?.[k] ?? dec.projected_share_mean?.[k] ?? 0));
    keyed(deskCard.body, JSON.stringify([dec.seq, dec.status]), () =>
      h(
        "div",
        { class: "res-desk" },
        h("div", { class: "res-desk__status" }, statusPill(dec.status, { color: raceColor(data.race) || undefined }), h("span", { class: "muted" }, `model evaluation at event ${fmtInt(dec.seq)}`)),
        h(
          "div",
          { class: "res-desk__rows", role: "img", "aria-label": `Projected final shares: ${keys.map((k) => `${lineName(k)} ${fmtShare(dec.projected_share_mean?.[k])}`).join(", ")}` },
          keys.map((k) => {
            const l = lineByKey()[k] || {};
            const lo = dec.projected_share_p05?.[k];
            const hi = dec.projected_share_p95?.[k];
            const mean = dec.projected_share_mean?.[k];
            const col = pc(l.party, l.color);
            return h(
              "div",
              { class: "res-desk__row" },
              h("span", { class: "res-desk__name" }, h("span", { class: "chip__swatch", style: { "--party": col } }), lineName(k)),
              h(
                "span",
                { class: "res-desk__track", title: `90% interval ${fmtShare(lo)} – ${fmtShare(hi)}` },
                lo !== undefined && hi !== undefined ? h("span", { class: "res-desk__band", style: { left: `${(lo / max) * 100}%`, width: `${Math.max(0.5, ((hi - lo) / max) * 100)}%`, "--party": col } }) : null,
                mean !== undefined ? h("span", { class: "res-desk__mean", style: { left: `${(mean / max) * 100}%` } }) : null,
              ),
              h("span", { class: "res-desk__val num" }, fmtShare(mean)),
              h("span", { class: "res-desk__prob num" }, fmtProb(dec.win_probability?.[k])),
            );
          }),
        ),
        h("div", { class: "res-muted-note" }, "Projected final share (bar = 90% interval, tick = mean) and win probability from the night's calling model. SIMULATED."),
      ),
    );
  }

  function paintCalls() {
    const calls = [...(data.calls || [])].reverse();
    keyed(callsCard.body, JSON.stringify(calls.map((c) => [c.seq, c.status])), () =>
      calls.length
        ? h(
            "ol",
            { class: "feed res-calls" },
            calls.map((c) => {
              const key = c.line_key || c.key;
              const l = lineByKey()[key];
              const col = pc(c.party || l?.party, c.color || l?.color);
              return h(
                "li",
                { class: ["feed__item", c.superseded && "is-superseded"] },
                h("span", { class: "feed__time" }, c.clock || String(c.timestamp || "").slice(11, 16)),
                h("span", null, statusPill(c.status, { color: col }), " ", key ? h("span", { class: "res-calls__who" }, c.candidate || lineName(key)) : null, c.is_manual ? h("span", { class: "res-tag" }, "MANUAL") : null),
                h("span", { class: "muted num res-calls__meta" }, `${fmtPct(c.reporting_pct)} in`),
              );
            }),
          )
        : h("p", { class: "muted res-empty", style: { padding: "16px" } }, data.results_source === "hidden" ? "Calls appear during the election night." : "No calls recorded for this race."),
    );
  }

  function paintHistory() {
    const hist = data.history || facts0?.history || [];
    keyed(histCard.body, JSON.stringify(hist), () =>
      hist.length
        ? dataTable(
            [
              { key: "year", label: "Year", format: (v) => h("b", null, String(v)) },
              { key: "winner", label: "Winner", format: (v, r) => h("span", { class: "res-who" }, partyChip(r.winner_party, { color: pc(r.winner_party) }), h("span", { class: "res-who__name" }, v)) },
              { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) },
              { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) },
              { key: "flipped", label: "Result", value: (r) => (r.flipped === true ? "flip" : r.flipped === false ? "hold" : r.open_seat ? "new" : ""), format: (v, r) => flipTag(v || null, { prev: r.incumbent_party }) },
              { key: "open_seat", label: "Open", format: (v) => (v ? h("span", { class: "res-tag res-tag--open" }, "OPEN") : "") },
            ],
            hist,
            { sortKey: "year", sortDir: "desc", caption: "Reported House results in this district" },
          )
        : h("p", { class: "muted res-empty", style: { padding: "16px" } }, "No reported House results for this district yet."),
    );
    const nb = facts0?.neighbours || [];
    keyed(nbCard.body, JSON.stringify(nb), () =>
      nb.length
        ? h("ul", { class: "res-rank" }, nb.map((n) => h("li", null, h("a", { href: links.district(n.code), class: "res-rank__name" }, h("span", { class: "res-code" }, n.code), " ", n.name), h("span", { class: "muted num" }, `${fmtInt(n.shared_border_km)} km border`), n.via_water_link ? h("span", { class: "res-tag" }, "WATER LINK") : null)))
        : h("p", { class: "muted res-empty" }, "–"),
    );
  }

  function paint(first) {
    frame.setSource(data);
    paintRace();
    paintDesk();
    paintMunis(first);
    paintCalls();
    if (first) {
      paintFacts();
      paintHistory();
    }
    paintMap();
  }

  const fetchData = () => api.get(`/api/elections/${id}/house/${code}`, { signal: ctrl.signal });
  try {
    const [d, f] = await Promise.all([fetchData(), api.get(`/api/districts/${code}`, { cache: true }).catch(() => null)]);
    data = d;
    facts0 = f;
    lastSource = d.results_source;
    const dname = d.district?.name || f?.name || code;
    frame.setTitle(dname, `House district · ${code} · ${f?.province_name || d.race?.province_code || ""} · ${d.election?.name || ""}`);
    mount(
      frame.body,
      h("div", { class: "res-grid res-grid--hero" }, h("div", { class: "res-stack" }, raceCard, deskCard), factsCard),
      h("div", { class: "res-grid res-grid--map" }, mapCard, nbCard),
      muniCard,
      h("div", { class: "res-grid res-grid--2" }, histCard, callsCard),
    );
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
      map?.destroy();
      map = null;
      mount(mapHost);
    }
    paint(changed);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}
