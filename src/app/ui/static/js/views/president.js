/**
 * #/president — the presidential race: tickets (portraits, running mates), electoral votes,
 * popular vote (votes, %), PV margin, national reporting, results by province (EV, status,
 * leader / winner, margin, reporting, flip / hold), popular vote vs electoral votes, the PV/EV
 * divergence notice and the contingent-election audit trail.
 *
 * Source: /api/elections/{id}/president (+ /electoral-college for the API's EV/PV shares and
 * efficiency).  Live: refetched at most every 3 s while the night advances (store.night seq),
 * updated in place.  Hidden: ballot only, with the notice.  No election mathematics.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt, fmtPct, fmtProb, STATUS_LABELS } from "../format.js";
import { flipBadge, provBadge, statusPill } from "../components/badges.js";
import { evBar } from "../components/evbar.js";
import { icon } from "../components/icons.js";
import { contingentPanel } from "../components/live-contingent.js";
import { ecMap } from "../components/live-ecmap.js";
import { pvEvChart } from "../components/live-pvev.js";
import { ticketCard } from "../components/live-ticket.js";
import { colorFor, constitution, decidedByLabel, phaseChip, phaseOf, setText, throttledFetch, ticketHead } from "../components/live-util.js";
import { dataTable } from "../components/table.js";
import { getElection, nightFor, subscribe } from "../store.js";
import { card, currentElectionId, errorState, fictionalNotice } from "./_shared.js";
import { electionSelect } from "../components/live-picker.js";

export async function render(el, params, ctx) {
  const id = ctx?.electionId || currentElectionId();
  const election = getElection(id);
  const C = constitution();
  const phaseHost = h("span");
  const head = h(
    "header",
    { class: "page-head lv-head" },
    h("div", null, h("div", { class: "page-head__eyebrow" }, `President · ${election?.name || ""}`), h("h1", { class: "page-head__title" }, "Presidential race")),
    h("div", { class: "page-head__meta" }, electionSelect({ presidentialOnly: true }), phaseHost, provBadge("SIMULATED"), provBadge("FICTIONAL", "Fictional system")),
  );
  const body = h("div", { class: "lv-page" }, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "60%", height: "14px" } })));
  mount(el, head, body);
  if (!election) {
    mount(body, errorState({ message: "No election selected." }));
    return;
  }

  let data = null;
  let ec = null;
  let built = null; // live DOM handles
  let lastSeq = null;
  let lastStatus = election.status;
  let destroyed = false;

  async function load(signal) {
    const [p, e] = await Promise.all([api.get(`/api/elections/${id}/president`, { signal }), api.get(`/api/elections/${id}/electoral-college`, { signal }).catch(() => null)]);
    if (destroyed) return;
    const sourceChanged = data && data.results_source !== p.results_source;
    data = p;
    ec = e;
    if (!built || sourceChanged) build();
    else apply();
  }

  function phase() {
    return phaseOf(getElection(id) || election, nightFor(id));
  }

  function build() {
    const src = data.results_source;
    const ph = phase();
    mount(phaseHost, phaseChip(ph));
    const bar = evBar({ total: data.electoral_votes_total || C.ev, majority: data.majority || C.evMajority, tickets: [], large: true });
    const cards = h("div", { class: "lv-tickets lv-tickets--pres" });
    const cardMap = new Map();
    const summary = h("div", { class: "lv-board__summary" });
    const stats = h("div", { class: "lv-pres__stats" });
    const bannerHost = h("div", { class: "lv-banners" });
    const pvev = pvEvChart({ total: data.electoral_votes_total || C.ev, majority: data.majority || C.evMajority });
    const map = ecMap({ height: 360, onSelect: (code) => (location.hash = `#/provinces/${code}`) });
    const table = dataTable(provinceColumns(src), [], { sortKey: "ev", sortDir: "desc", rowHref: (r) => `#/provinces/${r.code}` });
    const notices = [];
    if (src === "hidden") notices.push(h("div", { class: "notice lv-notice" }, icon("lock", { size: 16 }), h("span", null, h("strong", null, "Results not revealed yet. "), data.notice || "They appear live during the election night or once the election is finalized.", " Showing the ballot: tickets, running mates and incumbents.")));
    if (src === "live") notices.push(h("div", { class: "notice lv-notice" }, h("span", { class: "live-dot" }), h("span", null, h("strong", null, "Live count. "), "Numbers are the votes counted so far; electoral votes are those of called provinces (projections by the SIMULATED calling model). Updates every few seconds.")));
    notices.push(fictionalNotice());

    const contingentHost = h("div");
    mount(
      body,
      bannerHost,
      h("div", { class: "lv-notices" }, notices),
      h(
        "div",
        { class: "grid lv-pres" },
        card("Electoral votes", h("div", { class: "lv-board" }, h("div", { class: "lv-board__bar" }, summary, bar), cards), { className: "span-all", categories: ["SIMULATED"] }),
        card("National popular vote", h("div", { class: "lv-pres__pv" }, stats, pvev.el), { className: "lv-pres__pvev", foot: "Small multiples: electoral votes on the 0–174 scale (88 marker) beside the popular-vote share on a 0–100 % scale. EC efficiency = EV share − PV share (API)." }),
        card("Province map", map.el, { className: "lv-pres__map", actions: map.toolbar }),
        card("Results by province", table, { className: "span-all", flush: true, foot: "Select a row for the province page. Margin = leader − runner-up in percentage points of valid votes (API). Flip / hold compares with the previous holder." }),
        contingentHost,
      ),
    );
    built = { bar, cards, cardMap, summary, stats, bannerHost, pvev, map, table, contingentHost, src };
    apply();
  }

  function person(key) {
    const t = (data?.tickets || []).find((x) => x.key === key);
    return t ? { key, name: t.president?.name || ticketHead(t.name), party: t.party, color: colorFor(t.party, t.color) } : key ? { key, name: key, party: null, color: "var(--uncalled)" } : null;
  }

  function apply() {
    if (!built || !data) return;
    const ph = phase();
    mount(phaseHost, phaseChip(ph));
    const src = data.results_source;
    const total = data.electoral_votes_total || C.ev;
    const needed = data.majority || C.evMajority;
    const tickets = data.tickets || [];
    const ecByKey = new Map((ec?.tickets || []).map((t) => [t.key, t]));
    const ordered = [...tickets].sort((a, b) => (b.electoral_votes || 0) - (a.electoral_votes || 0) || (b.ev_leading || 0) - (a.ev_leading || 0) || (b.votes || 0) - (a.votes || 0));
    built.bar.update({
      total,
      majority: needed,
      tickets: ordered.map((t) => ({ key: t.key, label: t.party, name: t.name, color: colorFor(t.party, t.color), decided: t.electoral_votes || 0, leading: t.ev_leading || 0 })),
    });
    const decidedTotal = data.ev_decided_total ?? (src === "final" ? total : 0);
    mount(
      built.summary,
      h("span", { class: "lv-board__ev num" }, h("b", null, fmtInt(decidedTotal)), ` of ${fmtInt(total)} electoral votes allocated`),
      data.ev_uncalled !== undefined && data.ev_uncalled !== null ? h("span", { class: "muted num" }, ` · ${fmtInt(data.ev_uncalled)} uncalled`) : null,
      h("span", { class: "lv-board__rule muted" }, `${data.label || `${needed} TO WIN`} · ${String(data.ev_allocation || "winner_take_all").replace(/_/g, "-")}`),
    );
    const winnerKey = data.winner?.key || null;
    const contingent = data.decided_by === "contingent";
    const finalists = new Set(data.contingent?.finalists || []);
    ordered.forEach((t, i) => {
      let c = built.cardMap.get(t.key);
      if (!c) {
        c = ticketCard(
          { key: t.key, name: t.president?.name || ticketHead(t.name), mate: t.running_mate?.name, party: t.party, partyName: t.party_name, color: colorFor(t.party, t.color), portrait: t.president?.portrait_key, incumbent: t.incumbent },
          { total, needed },
        );
        built.cardMap.set(t.key, c);
        built.cards.appendChild(c);
      }
      c.style.order = String(i);
      c.update({
        decided: src === "hidden" ? 0 : t.electoral_votes ?? 0,
        leading: t.ev_leading || 0,
        maxPossible: t.ev_max_possible,
        showMax: src === "live",
        votes: t.votes,
        pct: t.pct,
        projectedPct: src === "live" && (data.popular_vote?.reporting_pct ?? 0) < 100 ? t.projected_pct : null,
        winner: winnerKey === t.key,
        winnerLabel: contingent ? "ELECTED" : src === "final" ? "ELECTED" : "WINS 88",
        finalist: contingent && finalists.has(t.key) && winnerKey !== t.key,
        out: src === "final" && winnerKey && winnerKey !== t.key && !t.electoral_votes && !finalists.has(t.key),
      });
    });
    // national stats
    const pv = data.popular_vote;
    const statItems = [];
    if (src === "final" && pv) {
      const lead = person(pv.leader);
      const ru = person(pv.runner_up);
      statItems.push(
        ["Valid votes", fmtInt(pv.total_valid), `${fmtInt(pv.ballots_cast)} ballots cast`],
        ["Turnout", fmtPct(pv.turnout_pct), `of ${fmtInt(pv.eligible)} eligible (est.)`],
        ["Popular-vote margin", `${fmtPct(pv.margin_pp, 2).replace("%", "")} pp`, `${lead?.name || "–"} over ${ru?.name || "–"} · ${fmtInt(pv.margin_votes)} votes`],
        ["Electoral-vote margin", fmtInt(data.electoral_vote_margin), `${data.majority_reached ? "majority reached" : "no majority"} · ${decidedByLabel(data.decided_by)}`],
      );
    } else if (src === "live" && pv) {
      statItems.push(
        ["Reporting", fmtPct(pv.reporting_pct), "expected presidential ballots counted"],
        ["Votes counted", fmtInt(pv.total_valid), "valid votes so far"],
        ["Est. outstanding", fmtInt(pv.outstanding_ballots_est), "ballots (model estimate)"],
        ["Projected valid", fmtInt(pv.projected_valid), "total valid votes (model)"],
      );
    } else {
      statItems.push(["Status", STATUS_LABELS[data.status] || data.status, "no votes revealed"], ["Electoral votes", `${fmtInt(total)}`, `${needed} needed to win`], ["Tickets", fmtInt(tickets.length), "on the ballot"], ["Allocation", "Winner-take-all", "per province (EV = House seats + 2)"]);
    }
    mount(built.stats, statItems.map(([l, v, s]) => h("div", { class: "stat lv-stat" }, h("span", { class: "stat__label" }, l), h("span", { class: "stat__value" }, v), h("span", { class: "stat__delta" }, s))));

    // banners: winner / contingent / divergence
    const banners = [];
    if (winnerKey) {
      const w = person(winnerKey);
      banners.push(
        h(
          "div",
          { class: "banner lv-banner", style: { "--party": w.color } },
          h("span", { class: "lv-banner__icon" }, icon(contingent ? "president" : "check", { size: 22 })),
          h(
            "div",
            { class: "lv-banner__text" },
            h("div", { class: "banner__title" }, contingent ? `${w.name} elected President by contingent election` : src === "final" ? `${w.name} elected President` : `${w.name} secures an Electoral College majority`),
            h("div", { class: "lv-banner__sub" }, `${data.winner?.name || w.name} · ${w.party} · ${fmtInt(data.winner?.electoral_votes)} electoral votes · decided by ${decidedByLabel(data.decided_by || "electoral_college").toLowerCase()}`),
          ),
        ),
      );
    } else if (data.contingent_likely) {
      banners.push(h("div", { class: "banner lv-banner lv-banner--alert" }, h("span", { class: "lv-banner__icon" }, icon("alert", { size: 22 })), h("div", { class: "lv-banner__text" }, h("div", { class: "banner__title" }, `No ticket can reach ${needed} electoral votes`), h("div", { class: "lv-banner__sub" }, "A contingent election in the House (province delegations) is likely."))));
    }
    const dv = data.divergence;
    if (dv) {
      const names = new Map(tickets.map((t) => [t.key, t.president?.name || ticketHead(t.name)]));
      const text = String(dv.description || "").replace(/\b[a-z]+(?:-[a-z0-9]+)+\b/g, (k) => names.get(k) || k);
      banners.push(
        h(
          "div",
          { class: ["notice", "lv-diverge", dv.diverged && "is-diverged"] },
          icon(dv.diverged ? "alert" : "check", { size: 16 }),
          h("span", null, h("strong", null, dv.diverged ? "Popular vote and Electoral College diverge. " : "No divergence. "), text),
        ),
      );
    }
    mount(built.bannerHost, banners);

    // PV vs EV
    built.pvev.update(
      ordered.map((t) => {
        const e = ecByKey.get(t.key);
        return {
          key: t.key,
          name: t.president?.name || ticketHead(t.name),
          party: t.party,
          color: colorFor(t.party, t.color),
          ev: src === "hidden" ? 0 : t.electoral_votes || 0,
          evLeading: t.ev_leading || 0,
          evShare: e?.ev_share ?? null,
          pct: t.pct,
          votes: t.votes,
          efficiency: e?.efficiency_pp ?? null,
        };
      }),
    );
    // map + table
    const rows = (data.provinces || []).map((p) => ({
      code: p.code,
      name: p.name,
      ev: p.ev,
      status: p.status,
      leader: p.leader ? person(p.leader) : null,
      winner: p.winner ? person(p.winner) : null,
      margin: p.margin_pp,
      reporting: p.reporting_pct,
      turnout: p.turnout_pct,
      winProb: p.win_probability,
      flip: p.flip_status,
      previousParty: p.previous_party,
      highlight: data.tipping_point?.province_code === p.code,
    }));
    built.map.update(rows);
    built.table.update(rows.map((r) => ({ ...r, who: (r.winner || r.leader)?.name || "" })));
    // contingent
    if (data.contingent && !built.contingentHost.dataset.key) {
      built.contingentHost.dataset.key = "1";
      built.contingentHost.className = "span-all";
      mount(built.contingentHost, card("Contingent election — audit trail", contingentPanel(data.contingent, { tickets, provinces: data.provinces }), { categories: ["FICTIONAL", "SIMULATED"] }));
    }
  }

  function provinceColumns(src) {
    const cols = [
      { key: "name", label: "Province", format: (v, r) => h("a", { href: `#/provinces/${r.code}`, class: "lv-strong" }, v) },
      { key: "ev", label: "EV", align: "r" },
      { key: "status", label: "Status", value: (r) => r.status, format: (v, r) => statusPill(r.status, { color: (r.winner || r.leader)?.color }) },
      {
        key: "who",
        label: src === "final" ? "Winner" : "Winner / leader",
        format: (v, r) => {
          const w = r.winner || r.leader;
          return w ? h("span", { class: "chip", style: { "--party": w.color } }, h("span", { class: "chip__swatch" }), `${w.name}`, h("span", { class: "muted" }, ` ${w.party || ""}${!r.winner ? " · leads" : ""}`)) : h("span", { class: "muted" }, "–");
        },
      },
      { key: "margin", label: "Margin", align: "r", format: (v) => (v === null || v === undefined ? "–" : `${v.toFixed(2)} pp`) },
      { key: "reporting", label: "Reporting", align: "r", format: (v) => fmtPct(v) },
    ];
    if (src === "final") cols.push({ key: "turnout", label: "Turnout", align: "r", format: (v) => fmtPct(v) });
    if (src === "live") cols.push({ key: "winProb", label: "Win prob.", align: "r", format: (v, r) => (r.winner ? "–" : fmtProb(v)) });
    cols.push({ key: "flip", label: "Flip / hold", format: (v, r) => (v ? h("span", { class: "lv-fliphold" }, flipBadge(v), r.previousParty ? h("span", { class: "muted" }, ` from ${r.previousParty}`) : null) : r.previousParty ? h("span", { class: "muted" }, `prev. ${r.previousParty}`) : "–") });
    return cols;
  }

  const fetcher = throttledFetch(async (signal) => {
    try {
      await load(signal);
    } catch (err) {
      if (err?.name === "AbortError") return;
      if (!data) mount(body, err?.status === 404 ? noPresident() : errorState(err));
    }
  }, { minInterval: 3000 });

  function noPresident() {
    return h("div", { class: "grid" }, fictionalNotice(), h("div", { class: "card" }, h("div", { class: "state" }, icon("president", { size: 28 }), h("h2", { style: { margin: 0 } }, "No presidential race in this election"), h("p", { class: "muted", style: { margin: 0 } }, `${election.name} is a ${election.election_type} election. Choose a general election in the picker above.`))));
  }

  const unsub = subscribe("night", () => {
    const n = nightFor(id);
    if (!n) return;
    const seq = n.clock?.seq ?? n.snapshot?.seq;
    const status = n.election_status;
    if (status !== lastStatus) {
      lastStatus = status;
      fetcher.now();
    } else if (seq !== lastSeq && n.clock?.status === "running") fetcher.request();
    else if (seq !== lastSeq) fetcher.request();
    lastSeq = seq;
    if (built) mount(phaseHost, phaseChip(phase()));
  });
  const unsubS = subscribe("settings", () => apply());
  fetcher.now();
  return () => {
    destroyed = true;
    unsub();
    unsubS();
    fetcher.stop();
    built?.map.destroy();
    void setText;
  };
}
