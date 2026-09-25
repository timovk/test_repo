/**
 * #/races/:code — generic race page for every race type (province presidential contests,
 * Senate, Governor, Mayor, council / provincial-legislature lists, House).
 *
 * Shows the candidates (votes, %, winner / leader, incumbency, seats won for list races), the
 * race status and headline numbers, the live decision desk (calling-model projection), the call
 * history with the exact evidence stored with every call, the recount audit trail, and the
 * municipality breakdown.  All numbers come from the API (no election mathematics here) and
 * follow the hidden / live / final results rule.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtCompact, fmtInt, fmtPct, fmtPP, fmtProb, fmtShare } from "../format.js";
import { partyChip, provBadge, statusPill } from "../components/badges.js";
import { dataTable } from "../components/table.js";
import { flipTag, liveRefresh, marginLabel, pc, raceColor, racePill, reportingMeter, resultRows, tile } from "../components/res-kit.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const TYPE_LABEL = {
  PRESIDENT: "Presidential race",
  PRESIDENT_PROVINCE: "Electoral-vote contest",
  HOUSE: "House race",
  SENATE: "Senate race",
  GOVERNOR: "Governor race",
  MAYOR: "Mayoral race",
  MUNICIPAL_COUNCIL: "Municipal council",
  PROVINCIAL_LEGISLATURE: "Provincial legislature",
};

const isList = (r) => /proportional/.test(String(r?.electoral_system || "")) || (r?.seats || 1) > 1;

export async function render(el, params) {
  const id = currentElectionId();
  const code = String(params.code || "").toUpperCase();
  if (code === "PRES") {
    location.replace("#/president");
    return undefined;
  }
  const ctrl = new AbortController();
  const frame = pageFrame(el, { eyebrow: `Race · ${code}`, title: code, categories: ["FICTIONAL", "SIMULATED"], electionId: id });

  let data = null;
  let lastSource = null;
  let selectedCall = null; // id of the call whose evidence is shown (null = latest)
  let muniTable = null;

  const raceCard = liveCard("Race", { id: "race-main" });
  const raceTiles = h("div", { class: "res-tiles res-hero-tiles" });
  const raceLines = h("div");
  raceCard.body.append(raceTiles, raceLines);
  const raceFoot = h("div", { class: "card__foot res-hero-foot" });
  raceCard.append(raceFoot);
  const linksCard = liveCard("Where this race is", { id: "race-links" });
  const deskCard = liveCard("Decision desk", { id: "race-desk", categories: [provBadge("SIMULATED")] });
  const evidenceCard = liveCard("Call evidence", { id: "race-evidence", categories: [provBadge("SIMULATED")] });
  const callsCard = liveCard("Call history", { id: "race-calls", flush: true });
  const recountCard = liveCard("Recount audit", { id: "race-recount", categories: [provBadge("SIMULATED")] });
  const muniCard = liveCard("Municipality breakdown", { id: "race-muni", flush: true });

  const lineByKey = () => Object.fromEntries((data?.race?.lines || []).map((l) => [l.key, l]));
  const lineName = (k) => {
    if (k === "invalid") return "Invalid ballots";
    if (k === "blank") return "Blank ballots";
    const l = lineByKey()[k];
    return l ? (l.candidate || l.president)?.name || l.name || k : k;
  };
  const lineColor = (k) => {
    const l = lineByKey()[k];
    return l ? pc(l.party, l.color) : "var(--uncalled)";
  };

  /* ------------------------------------------------------------------ race */
  function paintRace() {
    const r = data.race;
    const src = data.results_source;
    frame.setTitle(r.name || code, `${TYPE_LABEL[r.type] || "Race"} · ${code} · ${data.election?.name || ""}`);
    raceCard.setTitle(isList(r) ? `Party lists · ${fmtInt(r.seats)} seats` : "Candidates");
    keyed(raceCard.meta, `${r.status}|${raceColor(r)}`, () => racePill(r));
    const leaderParty = r.winner_party || r.leader_party;
    const tiles = [
      tile(r.winner ? "Winner" : "Leader", leaderParty ? partyChip(leaderParty, { color: raceColor(r) }) : "–", r.winner_name || r.leader_name || (src === "hidden" ? "Results hidden until reported" : "No votes counted yet"), { accent: raceColor(r) || undefined }),
      tile("Margin", r.margin_pp !== null && r.margin_pp !== undefined ? `${fmtPP(r.margin_pp)} pp` : "–", r.margin_votes !== null && r.margin_votes !== undefined ? `${fmtInt(r.margin_votes)} votes` : "winner − runner-up"),
      src === "final"
        ? tile("Turnout", fmtPct(r.turnout_pct), r.ballots_cast ? `${fmtInt(r.ballots_cast)} ballots of ${fmtInt(r.eligible)} eligible` : null)
        : tile("Reporting", r.reporting_pct !== null && r.reporting_pct !== undefined ? fmtPct(r.reporting_pct) : "–", "of the expected vote"),
      r.electoral_votes
        ? tile("Electoral votes", fmtInt(r.electoral_votes), "winner-take-all")
        : src === "live" && r.win_probability !== null && r.win_probability !== undefined
          ? tile("Call model", fmtProb(r.win_probability), "leader's win probability")
          : tile("Votes counted", fmtInt(r.total_votes), isList(r) ? "D'Hondt seat allocation" : "valid votes"),
    ];
    keyed(raceTiles, JSON.stringify([r.status, leaderParty, r.margin_pp, r.reporting_pct, r.turnout_pct, r.total_votes, r.win_probability]), () => tiles);
    const opts = { leaderKey: r.leader, seats: r.seats_won || undefined, mateLabel: r.type === "GOVERNOR" ? "Lt. governor" : "with" };
    if (!raceLines.firstChild?.update) mount(raceLines, resultRows(r.lines || [], opts));
    else raceLines.firstChild.update(r.lines || [], opts);
    const inc = r.incumbent;
    const prev = data.previous_race;
    keyed(raceFoot, JSON.stringify([inc, r.open_seat, r.previous_party, r.flip_status, r.decided_by, prev, r.is_special]), () => [
      inc ? h("span", null, "Incumbent: ", h("b", null, inc.name), ` (${inc.party || "independent"})`, inc.running === false ? " · not running" : " · running") : h("span", null, isList(r) ? "Party-list election" : "No incumbent"),
      r.open_seat ? h("span", null, " · ", h("span", { class: "res-tag res-tag--open" }, "OPEN SEAT")) : null,
      r.is_special ? h("span", null, " · ", h("span", { class: "res-tag" }, "SPECIAL ELECTION")) : null,
      r.previous_party ? h("span", null, " · Held by ", h("b", null, r.previous_party)) : null,
      r.flip_status ? h("span", null, " · ", flipTag(r.flip_status, { prev: r.previous_party })) : null,
      r.decided_by ? h("span", null, ` · Decided by ${String(r.decided_by).replace(/_/g, " ")}`) : null,
      prev ? h("span", null, ` · ${prev.year}: ${prev.winner_name || "–"} (${prev.winner_party || "–"})${prev.margin_pp !== null && prev.margin_pp !== undefined ? `, margin ${fmtPP(prev.margin_pp).replace(/^[+−±]/, "")} pp` : ""}`) : null,
    ]);
  }

  function paintLinks() {
    const r = data.race;
    const items = [];
    if (r.province_code) items.push(["Province", r.province_code, links.province(r.province_code)]);
    if (r.district_code) items.push(["House district", `${r.district_code}${r.district_name ? ` · ${r.district_name}` : ""}`, links.district(r.district_code)]);
    if (r.municipality_code) items.push(["Municipality", r.municipality_name || r.municipality_code, links.municipality(r.municipality_code)]);
    if (r.type === "PRESIDENT_PROVINCE") items.push(["National race", "President · Electoral College", "#/president"]);
    if (r.type === "SENATE") items.push(["Chamber", "Senate · 24 seats", "#/senate"]);
    if (r.type === "GOVERNOR") items.push(["All governors", "12 governor races", "#/governors"]);
    if (r.type === "HOUSE") items.push(["Chamber", "House · 150 seats", "#/house"]);
    keyed(linksCard.body, JSON.stringify(items), () =>
      h(
        "ul",
        { class: "res-rank" },
        items.map(([label, text, href]) => h("li", null, h("span", { class: "muted" }, label), h("a", { href, class: "res-rank__name" }, text))),
      ),
    );
  }

  /* ------------------------------------------------------------------ live decision desk */
  function paintDesk() {
    const dec = data.decision;
    deskCard.hidden = data.results_source !== "live" || !dec;
    if (deskCard.hidden) return;
    const means = dec.projected_share_mean || {};
    const keys = Object.keys(means).length ? Object.keys(means) : Object.keys(dec.win_probability || {});
    keys.sort((a, b) => (means[b] ?? 0) - (means[a] ?? 0));
    const top = keys.slice(0, 6);
    const max = Math.max(0.01, ...top.map((k) => dec.projected_share_p95?.[k] ?? means[k] ?? 0));
    keyed(deskCard.body, JSON.stringify([dec.seq, dec.status]), () =>
      h(
        "div",
        { class: "res-desk" },
        h("div", { class: "res-desk__status" }, statusPill(dec.status, { color: raceColor(data.race) || undefined }), h("span", { class: "muted" }, `model evaluation at reporting event ${fmtInt(dec.seq)}`)),
        h(
          "div",
          { class: "res-desk__rows", role: "img", "aria-label": `Projected final shares: ${top.map((k) => `${lineName(k)} ${fmtShare(means[k])}`).join(", ")}` },
          top.map((k) => {
            const lo = dec.projected_share_p05?.[k];
            const hi = dec.projected_share_p95?.[k];
            const mean = means[k];
            const col = lineColor(k);
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
        h("div", { class: "res-muted-note" }, "Projected final share (bar = 90% interval, tick = mean) and win probability from the night's calling model, using only the ballots counted so far. SIMULATED."),
      ),
    );
  }

  /* ------------------------------------------------------------------ calls + evidence */
  function currentCalls() {
    return [...(data.calls || [])];
  }

  function paintCalls() {
    const calls = currentCalls().reverse();
    keyed(callsCard.body, JSON.stringify([calls.map((c) => [c.id ?? c.seq, c.status]), selectedCall]), () =>
      calls.length
        ? h(
            "ol",
            { class: "feed res-calls" },
            calls.map((c) => {
              const key = c.line_key || c.key;
              const col = pc(c.party || lineByKey()[key]?.party, c.color || lineByKey()[key]?.color);
              const cid = c.id ?? c.seq;
              return h(
                "li",
                {
                  class: ["feed__item", c.superseded && "is-superseded", selectedCall === cid && "is-selected"],
                  style: { cursor: c.evidence ? "pointer" : "default" },
                  tabindex: c.evidence ? 0 : undefined,
                  role: c.evidence ? "button" : undefined,
                  "aria-label": c.evidence ? `Show evidence of the ${c.status} call at ${c.clock || ""}` : undefined,
                  onclick: c.evidence ? () => selectCall(cid) : undefined,
                  onkeydown: c.evidence ? (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), selectCall(cid)) : undefined,
                },
                h("span", { class: "feed__time" }, c.clock || String(c.called_at || c.timestamp || "").slice(11, 16)),
                h("span", null, statusPill(c.status, { color: col }), " ", key ? h("span", { class: "res-calls__who" }, c.candidate || lineName(key)) : null, c.is_manual ? h("span", { class: "res-tag" }, "MANUAL") : null, c.superseded ? h("span", { class: "res-tag" }, "SUPERSEDED") : null),
                h("span", { class: "muted num res-calls__meta" }, `${fmtPct(c.reporting_pct)} in${c.win_probability !== null && c.win_probability !== undefined ? ` · p ${fmtProb(c.win_probability)}` : ""}`),
              );
            }),
          )
        : h("p", { class: "muted res-empty", style: { padding: "16px" } }, data.results_source === "hidden" ? "Calls appear during the election night." : "No calls recorded for this race yet."),
    );
    callsCard.setTitle(`Call history · ${calls.length}`);
  }

  function selectCall(cid) {
    selectedCall = cid;
    paintCalls();
    paintEvidence();
  }

  function paintEvidence() {
    const calls = currentCalls().filter((c) => c.evidence);
    evidenceCard.hidden = !calls.length;
    if (evidenceCard.hidden) return;
    const call = calls.find((c) => (c.id ?? c.seq) === selectedCall) || calls.filter((c) => !c.superseded).pop() || calls[calls.length - 1];
    const ev = call.evidence || {};
    const probs = ev.win_probability || {};
    const counted = ev.counted || {};
    const proj = ev.projection || {};
    const projected = { mean: proj.share_mean, p05: proj.share_p05, p95: proj.share_p95 };
    const pkeys = Object.keys(probs).sort((a, b) => (probs[b] ?? 0) - (probs[a] ?? 0)).slice(0, 5);
    const rep = ev.reporting || {};
    keyed(evidenceCard.body, JSON.stringify([call.id ?? call.seq, call.status]), () =>
      h(
        "div",
        { class: "race-evidence" },
        h(
          "div",
          { class: "res-desk__status" },
          statusPill(call.status, { color: pc(call.party, call.color) }),
          h("span", { class: "muted" }, `${call.clock || ""} · reporting event ${fmtInt(call.seq)}${call.is_manual ? " · manual override" : ""}`),
        ),
        h(
          "div",
          { class: "res-tiles" },
          tile("Reporting", fmtPct(call.reporting_pct ?? rep.pct_expected_ballots), rep.units_total ? `${fmtInt(rep.units_reported ?? rep.units_total)} of ${fmtInt(rep.units_total)} precincts` : "of the expected vote"),
          tile("Counted", counted.valid >= 100000 ? fmtCompact(counted.valid) : fmtInt(counted.valid), counted.ballots ? `${fmtInt(counted.valid)} valid · ${fmtInt(counted.ballots)} ballots` : "valid votes"),
          tile("Margin", call.margin_pct !== null && call.margin_pct !== undefined ? `${fmtPP(call.margin_pct)} pp` : "–", ev.math_certain ? "mathematically certain" : "counted margin"),
          tile(
            "Outstanding",
            ev.outstanding?.ballots_est !== undefined ? (ev.outstanding.ballots_est >= 100000 ? fmtCompact(ev.outstanding.ballots_est) : fmtInt(ev.outstanding.ballots_est)) : ev.basis === "final" || (call.reporting_pct ?? 0) >= 100 ? "0" : "–",
            ev.outstanding?.ballots_est !== undefined ? `≈ ${fmtInt(ev.outstanding.ballots_est)} ballots still to count` : "estimated ballots still to count",
          ),
        ),
        pkeys.length
          ? h(
              "table",
              { class: "data", style: { marginTop: "10px" } },
              h("thead", null, h("tr", null, h("th", null, "Candidate"), h("th", { class: "r" }, "Counted"), h("th", { class: "r" }, "Projected share"), h("th", { class: "r" }, "Win probability"))),
              h(
                "tbody",
                null,
                pkeys.map((k) =>
                  h(
                    "tr",
                    null,
                    h("td", null, h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": lineColor(k) } }), lineName(k))),
                    h("td", { class: "r" }, fmtInt(counted.votes?.[k])),
                    h("td", { class: "r" }, projected.mean?.[k] !== undefined ? `${fmtShare(projected.mean[k])} (${fmtShare(projected.p05?.[k])}–${fmtShare(projected.p95?.[k])})` : ev.basis === "final" ? "final" : "–"),
                    h("td", { class: "r" }, fmtProb(probs[k])),
                  ),
                ),
              ),
            )
          : null,
        ev.recount ? h("p", { class: "res-muted-note" }, `Recount check: ${ev.recount.reason || (ev.recount.required ? "required" : "not required")}`) : null,
        ev.prior ? h("p", { class: "res-muted-note" }, `Previous state: ${ev.prior.status}${ev.prior.seq !== undefined ? ` (event ${fmtInt(ev.prior.seq)})` : ""}`) : null,
        h(
          "details",
          { class: "race-evidence__raw" },
          h("summary", null, "Full evidence record (JSON)"),
          h("pre", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "11.5px", maxHeight: "360px", overflow: "auto" } }, JSON.stringify(ev, null, 2)),
        ),
        h("p", { class: "res-muted-note" }, "Every call stores the exact evidence available at that moment (select a call in the history to inspect it)."),
      ),
    );
  }

  /* ------------------------------------------------------------------ recounts */
  function paintRecounts() {
    const recs = data.recounts || [];
    recountCard.hidden = !recs.length;
    if (!recs.length) return;
    keyed(recountCard.body, JSON.stringify(recs.map((r) => [r.id, r.status])), () =>
      recs.map((rc) =>
        h(
          "div",
          { class: "race-recount" },
          h(
            "div",
            { class: "res-tiles" },
            tile("Reason", String(rc.reason || "").replace(/_/g, " "), rc.threshold_pct !== null && rc.threshold_pct !== undefined ? `threshold ${fmtPP(rc.threshold_pct).replace(/^[+−±]/, "")} pp` : null),
            tile("Margin before", `${fmtPP(rc.margin_before_pct).replace(/^[+−±]/, "")} pp`, `${fmtInt(rc.margin_before_votes)} votes`),
            tile("Margin after", rc.margin_after_pct !== null && rc.margin_after_pct !== undefined ? `${fmtPP(rc.margin_after_pct).replace(/^[+−±]/, "")} pp` : "–", rc.margin_after_votes !== null && rc.margin_after_votes !== undefined ? `${fmtInt(rc.margin_after_votes)} votes` : null),
            tile("Outcome", rc.outcome_changed ? "Changed" : "Confirmed", `${fmtInt(rc.adjustments)} audited adjustments · ${rc.status}`),
          ),
          rc.net_change && Object.keys(rc.net_change).length
            ? h(
                "div",
                { class: "legend", style: { margin: "10px 0" } },
                Object.entries(rc.net_change).map(([k, v]) => h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": lineColor(k) } }), `${lineName(k)} ${v > 0 ? "+" : ""}${fmtInt(v)}`)),
              )
            : null,
          rc.audit?.length
            ? dataTable(
                [
                  { key: "line_key", label: "Ballot line", format: (v, x) => (x.pile === "line" ? lineName(v) : `${x.pile} pile`) },
                  { key: "votes_before", label: "Before", align: "r", format: (v) => fmtInt(v) },
                  { key: "votes_after", label: "After", align: "r", format: (v) => fmtInt(v) },
                  { key: "delta", label: "Δ", align: "r", format: (v) => `${v > 0 ? "+" : ""}${fmtInt(v)}` },
                  { key: "reason", label: "Reason" },
                ],
                rc.audit,
                { maxHeight: 280, sortKey: "delta", sortDir: "asc" },
              )
            : null,
          h("p", { class: "res-muted-note" }, "Recounts only apply small, audited ballot adjustments to the stored count — the election is never regenerated."),
        ),
      ),
    );
  }

  /* ------------------------------------------------------------------ municipalities */
  function paintMunis(rebuild) {
    const src = data.results_source;
    const rows = data.municipalities || [];
    muniCard.hidden = !rows.length && src === "hidden";
    const lines = [...(data.race?.lines || [])].sort((a, b) => (b.votes ?? 0) - (a.votes ?? 0)).slice(0, 4);
    if (rebuild || !muniTable) {
      muniTable = dataTable(
        [
          { key: "name", label: "Municipality", format: (v, x) => h("a", { href: links.municipality(x.code), class: "res-link" }, v || x.code) },
          src === "live" ? { key: "reporting_pct", label: "Reporting", value: (x) => x.reporting_pct ?? null, format: (v) => reportingMeter(v, { width: 46 }) } : null,
          { key: "total_votes", label: "Votes", align: "r", value: (x) => x.total_votes ?? (x.votes ? Object.values(x.votes).reduce((a, b) => a + b, 0) : null), format: (v) => fmtInt(v) },
          src === "final" ? { key: "leader_party", label: "Leader", value: (x) => x.leader_party || "", format: (v, x) => (v ? partyChip(v, { color: pc(v, x.leader_color) }) : "–") } : null,
          src === "final" ? { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) } : null,
          ...lines.map((l) => ({
            key: `l_${l.key}`,
            label: `${(l.candidate || l.president)?.name || l.name} (${l.party || "IND"})`,
            align: "r",
            value: (x) => (src === "final" ? x.pct?.[l.key] ?? null : x.votes?.[l.key] ?? null),
            format: (v) => (v === null || v === undefined ? "–" : src === "final" ? fmtPct(v) : fmtInt(v)),
          })),
          src === "final" ? { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) } : null,
        ].filter(Boolean),
        rows,
        { sortKey: "total_votes", sortDir: "desc", rowHref: (x) => links.municipality(x.code), maxHeight: 520, empty: src === "hidden" ? "Results hidden until reported" : "No municipalities have reported yet" },
      );
      mount(muniCard.body, muniTable);
    } else muniTable.update(rows);
    muniCard.setTitle(`Municipality breakdown · ${rows.length}`);
    keyed(muniCard.meta, src, () => (src === "live" ? h("span", { class: "res-muted-note" }, "live: counted votes") : provBadge("SIMULATED")));
  }

  function paint(first) {
    frame.setSource(data);
    paintRace();
    paintDesk();
    paintEvidence();
    paintCalls();
    paintRecounts();
    paintMunis(first);
    if (first) paintLinks();
  }

  const fetchData = () => api.get(`/api/elections/${id}/races/${code}`, { signal: ctrl.signal });
  try {
    data = await fetchData();
    lastSource = data.results_source;
    mount(
      frame.body,
      h("div", { class: "res-grid res-grid--hero" }, h("div", { class: "res-stack" }, raceCard, deskCard), h("div", { class: "res-stack" }, linksCard, evidenceCard)),
      recountCard,
      h("div", { class: "res-grid res-grid--2" }, muniCard, callsCard),
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
    paint(changed);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
  };
}
