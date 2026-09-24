/**
 * #/electoral-college — the Electoral College: 174 EV / 88 needed, EV by ticket (segmented bar +
 * EV arc), province winners (tile + geographic map), province margins (sorted, party-coloured),
 * the winner's path to 88 with the tipping-point province explained, closest province, largest
 * victory, EV/PV comparison, divergence (descriptive) and the EC history of every presidential
 * election (browse with the picker or the history table).
 *
 * Sources: /api/elections/{id}/electoral-college (+ /president for names, portraits, PV %),
 * /api/elections and /api/history/divergence for the history.  Live: refetched at most every
 * 3 s while the night advances; updated in place.  No election mathematics.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt, fmtPct, STATUS_LABELS } from "../format.js";
import { provBadge } from "../components/badges.js";
import { evBar } from "../components/evbar.js";
import { hemicycle } from "../components/hemicycle.js";
import { icon } from "../components/icons.js";
import { ecMap } from "../components/live-ecmap.js";
import { marginChart, pathStrip } from "../components/live-margins.js";
import { electionSelect } from "../components/live-picker.js";
import { pvEvChart } from "../components/live-pvev.js";
import { colorFor, constitution, decidedByLabel, isDecided, phaseChip, phaseOf, throttledFetch, ticketHead } from "../components/live-util.js";
import { getElection, nightFor, subscribe } from "../store.js";
import { card, currentElectionId, errorState, fictionalNotice } from "./_shared.js";

export async function render(el, params, ctx) {
  const id = ctx?.electionId || currentElectionId();
  const election = getElection(id);
  const C = constitution();
  const phaseHost = h("span");
  const body = h("div", { class: "lv-page" }, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "60%", height: "14px" } })));
  mount(
    el,
    h(
      "header",
      { class: "page-head lv-head" },
      h("div", null, h("div", { class: "page-head__eyebrow" }, `Electoral College · ${election?.name || ""}`), h("h1", { class: "page-head__title" }, "Electoral College")),
      h("div", { class: "page-head__meta" }, electionSelect({ presidentialOnly: true }), phaseHost, provBadge("SIMULATED"), provBadge("FICTIONAL", "Fictional rules")),
    ),
    body,
  );
  if (!election) {
    mount(body, errorState({ message: "No election selected." }));
    return;
  }

  let data = null;
  let pres = null;
  let history = null;
  let built = null;
  let destroyed = false;
  let lastSeq = null;
  let lastStatus = election.status;

  const phase = () => phaseOf(getElection(id) || election, nightFor(id));

  function nameOf(key) {
    const t = (pres?.tickets || []).find((x) => x.key === key);
    if (t) return t.president?.name || ticketHead(t.name);
    const e = (data?.tickets || []).find((x) => x.key === key);
    return e ? ticketHead(e.name) : key;
  }
  function ticketOf(key) {
    return (data?.tickets || []).find((x) => x.key === key) || (pres?.tickets || []).find((x) => x.key === key) || null;
  }
  function person(key) {
    if (!key) return null;
    const t = ticketOf(key);
    return { key, name: nameOf(key), party: t?.party || null, color: colorFor(t?.party, t?.color) };
  }

  async function load(signal) {
    const [e, p] = await Promise.all([api.get(`/api/elections/${id}/electoral-college`, { signal }), api.get(`/api/elections/${id}/president`, { signal }).catch(() => null)]);
    if (destroyed) return;
    const changed = data && data.results_source !== e.results_source;
    data = e;
    pres = p;
    if (!built || changed) build();
    else apply();
  }

  async function loadHistory() {
    try {
      const [list, div] = await Promise.all([api.get("/api/elections"), api.get("/api/history/divergence").catch(() => null)]);
      history = { list: list.elections || [], div: new Map((div?.elections || []).map((d) => [d.election_id, d])) };
      if (built) renderHistory();
    } catch (err) {
      if (err?.name !== "AbortError") console.warn(err);
    }
  }

  function build() {
    const src = data.results_source;
    const total = data.electoral_votes_total || C.ev;
    const needed = data.majority || C.evMajority;
    const notices = [];
    if (src === "hidden") notices.push(h("div", { class: "notice lv-notice" }, icon("lock", { size: 16 }), h("span", null, h("strong", null, "Not reported yet. "), data.notice || "", " Nothing is allocated before the night: 0 EV allocated, 174 available, 88 to win.")));
    if (src === "live") notices.push(h("div", { class: "notice lv-notice" }, h("span", { class: "live-dot" }), h("span", null, h("strong", null, "Live. "), "Allocated EV = provinces called by the SIMULATED calling model. The path to 88 and the tipping point are determined once the election is FINAL.")));
    notices.push(fictionalNotice("Fictional Electoral College: each of the 12 real provinces casts House seats + 2 electoral votes (174 in total), winner-take-all; 88 wins. If nobody reaches 88, the House elects the President by province delegations. All results are simulated."));

    const kpis = h("div", { class: "lv-kpis" });
    const bar = evBar({ total, majority: needed, tickets: [], large: true });
    const arc = hemicycle({ total, majority: needed, groups: [], centerLabel: "0", centerSub: `${needed} TO WIN`, tooltip: (g) => h("div", null, h("div", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": g.color } }), h("b", null, g.label)), h("div", { class: "muted" }, g.style === "leading" ? "leading, not called" : g.style === "uncalled" ? "uncalled" : "electoral votes won"), g.seats ? h("div", { class: "num" }, `${g.seats} EV`) : null) });
    const ticketTable = h("div", { class: "lv-ec__tickets" });
    const map = ecMap({ height: 380, onSelect: (code) => (location.hash = `#/provinces/${code}`) });
    const marginsHost = h("div");
    const pathHost = h("div", { class: "lv-ec__path" });
    const pvev = pvEvChart({ total, majority: needed });
    const divHost = h("div");
    const historyHost = h("div");
    const contingentHost = h("div");
    mount(
      body,
      h("div", { class: "lv-notices" }, notices),
      kpis,
      h(
        "div",
        { class: "grid lv-ec" },
        card("Electoral votes by ticket", h("div", { class: "lv-ec__votes" }, h("div", { class: "lv-ec__bar" }, bar), h("div", { class: "lv-ec__split" }, h("div", { class: "lv-ec__arc" }, arc), ticketTable)), { className: "span-all", categories: ["SIMULATED"] }),
        card("Province winners", map.el, { className: "lv-ec__map", actions: map.toolbar }),
        card("Province margins", marginsHost, { className: "lv-ec__margins", foot: "Winner (or current leader, lighter) minus runner-up, in percentage points of valid votes (API). Sorted from the largest to the smallest margin." }),
        card("Path to 88 · tipping point", pathHost, { className: "span-all" }),
        card("Popular vote vs electoral votes", h("div", { class: "lv-pres__pv" }, divHost, pvev.el), { className: "span-all" }),
        contingentHost,
        card("Electoral College history", historyHost, { className: "span-all", flush: true, categories: ["SIMULATED"], foot: "Every presidential election in the database. Select a row to browse its Electoral College." }),
      ),
    );
    built = { kpis, bar, arc, ticketTable, map, marginsHost, pathHost, pvev, divHost, historyHost, contingentHost };
    apply();
    renderHistory();
  }

  function kpi(label, value, sub, extra) {
    return h("div", { class: "stat lv-stat lv-kpi" }, h("span", { class: "stat__label" }, label), h("span", { class: "stat__value" }, value), sub ? h("span", { class: "stat__delta" }, sub) : null, extra || null);
  }

  function apply() {
    if (!built || !data) return;
    const ph = phase();
    mount(phaseHost, phaseChip(ph));
    const src = data.results_source;
    const total = data.electoral_votes_total || C.ev;
    const needed = data.majority || C.evMajority;
    const tickets = (data.tickets || []).map((t) => {
      const p = (pres?.tickets || []).find((x) => x.key === t.key);
      return {
        key: t.key,
        name: p?.president?.name || ticketHead(t.name),
        mate: p?.running_mate?.name,
        party: t.party,
        color: colorFor(t.party, t.color),
        ev: t.electoral_votes ?? 0,
        leading: t.ev_leading ?? 0,
        max: t.ev_max_possible,
        votes: t.popular_votes ?? p?.votes ?? null,
        pct: p?.pct ?? (t.pv_share !== undefined && t.pv_share !== null ? t.pv_share * 100 : null),
        evShare: t.ev_share ?? null,
        pvShare: t.pv_share ?? null,
        efficiency: t.efficiency_pp ?? null,
        won: t.provinces_won || null,
      };
    });
    const ordered = [...tickets].sort((a, b) => b.ev - a.ev || b.leading - a.leading || (b.votes || 0) - (a.votes || 0));
    const winner = data.winner;
    const allocated = data.ev_decided_total ?? (src === "final" ? total : 0);
    const uncalled = data.ev_uncalled ?? (src === "final" ? 0 : total);
    mount(
      built.kpis,
      kpi("Electoral votes", fmtInt(total), "12 provinces · House seats + 2"),
      kpi("Needed to win", fmtInt(needed), data.label || `${needed} TO WIN`),
      kpi("Allocated", fmtInt(allocated), src === "final" ? "certified" : src === "live" ? "called provinces" : "nothing before the night"),
      kpi("Uncalled", fmtInt(uncalled), src === "live" ? "EV still open" : src === "hidden" ? "all available" : "none"),
      winner
        ? kpi(data.contingent ? "Elected (contingent)" : "Winner", winner.president?.name || ticketHead(winner.name), `${winner.party} · ${fmtInt(winner.electoral_votes)} EV`, h("span", { class: "lv-kpi__swatch", style: { "--party": colorFor(winner.party, winner.color) } }))
        : kpi("Leader", ordered[0] && ordered[0].ev ? ordered[0].name : "–", ordered[0] && ordered[0].ev ? `${ordered[0].party} · ${ordered[0].ev} EV decided` : data.contingent_likely ? "contingent election likely" : "no EV allocated"),
    );
    built.bar.update({ total, majority: needed, tickets: ordered.map((t) => ({ key: t.key, label: t.party, name: t.name, color: t.color, decided: src === "hidden" ? 0 : t.ev, leading: t.leading })) });
    const groups = [];
    for (const t of ordered) if (t.ev && src !== "hidden") groups.push({ key: `${t.key}:c`, label: `${t.name} (${t.party})`, color: t.color, seats: t.ev, style: "called" });
    for (const t of ordered) if (t.leading) groups.push({ key: `${t.key}:l`, label: `${t.name} (${t.party})`, color: t.color, seats: t.leading, style: "leading" });
    const lead = ordered[0];
    built.arc.update({ groups, centerLabel: lead && lead.ev && src !== "hidden" ? `${lead.party} ${lead.ev}` : "0", centerSub: `${needed} TO WIN`, ariaLabel: `Electoral votes arc: ${ordered.map((t) => `${t.name} ${t.ev}${t.leading ? ` plus ${t.leading} leading` : ""}`).join(", ")}; ${needed} needed of ${total}` });
    mount(
      built.ticketTable,
      h(
        "table",
        { class: "data lv-ec__table" },
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", null, "Ticket"),
            h("th", { class: "r" }, "EV"),
            src === "live" ? h("th", { class: "r", title: "EV of provinces led but not called" }, "Leading") : null,
            src === "live" ? h("th", { class: "r", title: "Maximum still possible (API)" }, "Max") : null,
            h("th", { class: "r" }, "Votes"),
            src === "final" ? h("th", { class: "r" }, "EV share") : null,
            src === "final" ? h("th", { class: "r" }, "PV share") : null,
            src === "final" ? h("th", { class: "r", title: "EV share − PV share (API)" }, "Efficiency") : null,
            src === "final" ? h("th", null, "Provinces won") : null,
          ),
        ),
        h(
          "tbody",
          null,
          ordered.map((t) =>
            h(
              "tr",
              { class: winner?.key === t.key ? "lv-row-win" : undefined },
              h("td", null, h("span", { class: "chip", style: { "--party": t.color } }, h("span", { class: "chip__swatch" }), t.name, h("span", { class: "muted" }, ` ${t.party}`)), winner?.key === t.key ? h("span", { class: "lv-flag lv-flag--win", style: { "--party": t.color, marginLeft: "8px" } }, icon("check", { size: 10 }), "WINNER") : null),
              h("td", { class: "r" }, h("b", null, src === "hidden" ? "0" : fmtInt(t.ev))),
              src === "live" ? h("td", { class: "r muted" }, t.leading ? `+${fmtInt(t.leading)}` : "–") : null,
              src === "live" ? h("td", { class: "r muted" }, fmtInt(t.max)) : null,
              h("td", { class: "r" }, fmtInt(t.votes)),
              src === "final" ? h("td", { class: "r" }, t.evShare === null ? "–" : fmtPct(t.evShare * 100)) : null,
              src === "final" ? h("td", { class: "r" }, t.pvShare === null ? "–" : fmtPct(t.pvShare * 100)) : null,
              src === "final" ? h("td", { class: "r" }, t.efficiency === null ? "–" : `${t.efficiency > 0 ? "+" : ""}${t.efficiency.toFixed(1)} pp`) : null,
              src === "final" ? h("td", { class: "muted" }, (t.won || []).join(" · ") || "–") : null,
            ),
          ),
        ),
      ),
    );

    // map rows
    const tp = data.tipping_point;
    const provRows = (data.provinces || []).map((p) => ({
      code: p.code,
      name: p.name,
      ev: p.ev,
      status: p.status,
      leader: p.leader ? person(p.leader) : null,
      winner: p.winner ? person(p.winner) : null,
      margin: p.margin_pp,
      reporting: p.reporting_pct,
      winProb: p.win_probability,
      flip: p.flip_status,
      previousParty: p.previous_party,
      highlight: tp?.province_code === p.code,
    }));
    built.map.update(provRows);

    // margins chart (sorted by API margin; the tags are API facts)
    const closest = data.closest?.province_code;
    const largest = data.largest_victory?.province_code;
    const mrows = provRows
      .filter((r) => r.margin !== null && r.margin !== undefined && (r.winner || r.leader))
      .map((r) => {
        const who = r.winner || r.leader;
        const tags = [];
        if (tp?.province_code === r.code) tags.push("Tipping point");
        if (closest === r.code) tags.push("Closest");
        if (largest === r.code) tags.push("Largest victory");
        return { code: r.code, name: r.name, ev: r.ev, margin: r.margin, color: who.color, party: who.party, who: who.name, decided: isDecided(r.status), statusLabel: STATUS_LABELS[r.status] || r.status, tags };
      })
      .sort((a, b) => b.margin - a.margin);
    const mk = JSON.stringify(mrows.map((r) => [r.code, r.margin, r.color, r.decided, r.tags]));
    if (built.marginsHost.dataset.key !== mk) {
      built.marginsHost.dataset.key = mk;
      mount(built.marginsHost, marginChart(mrows, { label: "Province margins, sorted" }));
    }

    // path to 88 + tipping point
    const pk = JSON.stringify([src, tp?.province_code, (data.path || []).length]);
    if (built.pathHost.dataset.key !== pk) {
      built.pathHost.dataset.key = pk;
      if (data.path && data.path.length && tp) {
        const w = person(tp.key);
        const tpRow = data.path.find((r) => r.is_tipping_point);
        const lost = tp.margin_pp < 0;
        mount(
          built.pathHost,
          h("p", { class: "lv-ec__lede" }, `Provinces ordered from ${w.name}'s strongest to weakest margin (API path). Each block is a province's electoral votes, coloured by its winner; the running total crosses ${needed} at the tipping-point province.`),
          pathStrip(data.path, { total, majority: needed, colorOf: (r) => colorFor(r.winner_party, r.winner_color), winnerParty: w.party }),
          h(
            "div",
            { class: "lv-ec__tp" },
            h(
              "div",
              { class: "lv-ec__tpcard", style: { "--party": w.color } },
              h("span", { class: "stat__label" }, "Tipping-point province"),
              h("span", { class: "lv-ec__tpname" }, `${tp.province_name} (${tpRow?.ev ?? "–"} EV)`),
              h(
                "p",
                null,
                `${tp.province_name} brings ${w.name}'s cumulative total to ${fmtInt(tp.cumulative_ev)} of ${needed} needed. `,
                lost ? `${w.name} lost it by ${Math.abs(tp.margin_pp).toFixed(2)} pp` : `${w.name} won it by ${tp.margin_pp.toFixed(2)} pp`,
                ` — against a national popular-vote margin of ${tp.national_margin_pp.toFixed(2)} pp. `,
                `Electoral College bias: ${tp.ec_bias_pp > 0 ? "+" : ""}${tp.ec_bias_pp.toFixed(2)} pp `,
                tp.ec_bias_pp < 0 ? "(the map was less favourable to the winner than the national vote)." : tp.ec_bias_pp > 0 ? "(the map was more favourable to the winner than the national vote)." : "(neutral).",
                data.contingent ? " No ticket reached 88, so the election went to the House." : "",
              ),
            ),
            h(
              "div",
              { class: "lv-ec__facts" },
              factTile("Closest province", data.closest, provRows),
              factTile("Largest victory", data.largest_victory, provRows),
              h("div", { class: "stat lv-stat" }, h("span", { class: "stat__label" }, "EV margin"), h("span", { class: "stat__value" }, fmtInt(pres?.electoral_vote_margin)), h("span", { class: "stat__delta" }, pres?.majority_reached ? "majority reached" : "no EC majority")),
            ),
          ),
        );
      } else {
        mount(built.pathHost, h("div", { class: "state" }, icon("college", { size: 24 }), src === "final" ? "No path available for this election." : "The path to 88 and the tipping point are computed by the API once the election is FINAL."));
      }
    }

    // PV vs EV + divergence
    built.pvev.update(ordered.map((t) => ({ key: t.key, name: t.name, party: t.party, color: t.color, ev: src === "hidden" ? 0 : t.ev, evLeading: t.leading, evShare: t.evShare, pct: t.pct, votes: t.votes, efficiency: t.efficiency })));
    const dv = data.divergence || pres?.divergence;
    const dk = JSON.stringify(dv || null);
    if (built.divHost.dataset.key !== dk) {
      built.divHost.dataset.key = dk;
      if (dv) {
        const text = String(dv.description || "").replace(/\b[a-z]+(?:-[a-z0-9]+)+\b/g, (k) => nameOf(k));
        mount(built.divHost, h("div", { class: ["notice", "lv-diverge", dv.diverged && "is-diverged"] }, icon(dv.diverged ? "alert" : "check", { size: 16 }), h("span", null, h("strong", null, dv.diverged ? "Divergence detected. " : "No divergence. "), text)));
      } else mount(built.divHost, h("div", { class: "notice" }, icon("college", { size: 16 }), h("span", null, "Divergence between the popular vote and the Electoral College is assessed by the API once the election is FINAL.")));
    }
    if (data.contingent && !built.contingentHost.dataset.key) {
      built.contingentHost.dataset.key = "1";
      built.contingentHost.className = "span-all";
      const w = person(data.contingent.winner);
      mount(
        built.contingentHost,
        h(
          "div",
          { class: "notice lv-ec__contingent" },
          icon("president", { size: 16 }),
          h("span", null, h("strong", null, "Contingent election. "), `No ticket won ${needed} electoral votes; the House elected ${w?.name} (${w?.party}) by province delegations after ${data.contingent.rounds} ballot${data.contingent.rounds === 1 ? "" : "s"}. `),
          h("a", { class: "btn btn--sm", href: "#/president" }, "Audit trail →"),
        ),
      );
    }
  }

  function factTile(label, fact, rows) {
    if (!fact) return h("div", { class: "stat lv-stat" }, h("span", { class: "stat__label" }, label), h("span", { class: "stat__value" }, "–"));
    const r = rows.find((x) => x.code === fact.province_code);
    const who = r?.winner || r?.leader;
    return h(
      "a",
      { class: "stat lv-stat lv-fact", href: `#/provinces/${fact.province_code}`, style: { "--party": who?.color } },
      h("span", { class: "stat__label" }, label),
      h("span", { class: "stat__value" }, `${r?.name || fact.province_code}`),
      h("span", { class: "stat__delta" }, h("span", { class: "chip__swatch", style: { "--party": who?.color } }), ` ${who?.party || ""} by ${fact.margin_pp.toFixed(2)} pp · ${r?.ev ?? "–"} EV`),
    );
  }

  function renderHistory() {
    if (!built) return;
    if (!history) {
      mount(built.historyHost, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "50%", height: "12px" } })));
      return;
    }
    const rows = history.list.filter((e) => e.contents?.president);
    mount(
      built.historyHost,
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "data lv-ec__history" },
          h("thead", null, h("tr", null, h("th", null, "Year"), h("th", null, "Election"), h("th", null, "Status"), h("th", null, "President"), h("th", { class: "r" }, "EV (winner)"), h("th", null, "Decided by"), h("th", null, "PV / EV"))),
          h(
            "tbody",
            null,
            rows.map((e) => {
              const hd = e.headline?.president;
              const dv = history.div.get(e.id);
              return h(
                "tr",
                { class: e.id === id ? "is-current" : undefined, "data-href": `#/electoral-college?e=${e.id}`, onclick: () => (location.hash = `#/electoral-college?e=${e.id}`) },
                h("td", null, h("b", null, String(e.year))),
                h("td", null, h("a", { href: `#/electoral-college?e=${e.id}` }, e.name)),
                h("td", null, e.status === "live" ? h("span", { class: "lv-phase lv-phase--live" }, h("span", { class: "live-dot" }), "LIVE") : e.reported ? h("span", { class: "lv-phase lv-phase--final" }, "FINAL") : h("span", { class: "lv-phase" }, "NOT REPORTED")),
                h("td", null, hd ? h("span", { class: "chip", style: { "--party": colorFor(hd.party) } }, h("span", { class: "chip__swatch" }), ticketHead(hd.winner_name), h("span", { class: "muted" }, ` ${hd.party}`)) : h("span", { class: "muted" }, "–")),
                h("td", { class: "r" }, hd ? fmtInt(hd.electoral_votes) : "–"),
                h("td", null, hd ? decidedByLabel(hd.decided_by) : "–"),
                h("td", null, dv ? (dv.diverged ? h("span", { class: "lv-tagwarn" }, icon("alert", { size: 11 }), "Diverged") : h("span", { class: "muted" }, "Same leader")) : h("span", { class: "muted" }, "–")),
              );
            }),
          ),
        ),
      ),
    );
  }

  const fetcher = throttledFetch(async (signal) => {
    try {
      await load(signal);
    } catch (err) {
      if (err?.name === "AbortError") return;
      if (!data) mount(body, err?.status === 404 ? h("div", { class: "grid" }, fictionalNotice(), h("div", { class: "card" }, h("div", { class: "state" }, icon("college", { size: 28 }), h("h2", { style: { margin: 0 } }, "No Electoral College vote in this election"), h("p", { class: "muted", style: { margin: 0 } }, `${election.name} has no presidential race. Choose a general election above.`)))) : errorState(err));
    }
  }, { minInterval: 3000 });

  const unsub = subscribe("night", () => {
    const n = nightFor(id);
    if (!n) return;
    const seq = n.clock?.seq ?? n.snapshot?.seq;
    if (n.election_status !== lastStatus) {
      lastStatus = n.election_status;
      fetcher.now();
      loadHistory();
    } else if (seq !== lastSeq) fetcher.request();
    lastSeq = seq;
  });
  const unsubS = subscribe("settings", () => apply());
  fetcher.now();
  loadHistory();
  return () => {
    destroyed = true;
    unsub();
    unsubS();
    fetcher.stop();
    built?.map.destroy();
  };
}
