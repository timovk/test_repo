/**
 * #/night — the Election Night dashboard (broadcast view).
 *
 * Live numbers come from the night snapshot in the store (store.night, SSE / polling by
 * night-poller.js) and are applied in place on every tick.  Detail endpoints are fetched at a
 * modest cadence while the night runs: /president (portraits, flip/hold) every ≥4 s, the call
 * log every ≥3 s when new calls exist; the static ballot-line labels once.  FINAL elections use
 * the stored final night state; without a night the page falls back to /president, /house and
 * /senate.  No election mathematics: every EV total, status, winner, margin and control flag is
 * an API value.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtCompact, fmtInt, fmtPct } from "../format.js";
import { provBadge } from "../components/badges.js";
import { evBar } from "../components/evbar.js";
import { icon } from "../components/icons.js";
import { chamberPanel } from "../components/live-chamber.js";
import { ecMap } from "../components/live-ecmap.js";
import { callFeed } from "../components/live-feed.js";
import { keyRaces } from "../components/live-keyraces.js";
import { ticketCard } from "../components/live-ticket.js";
import { colorFor, constitution, createLineIndex, decidedByLabel, isFinalElection, phaseChip, phaseOf, setText, throttledFetch, ticketHead } from "../components/live-util.js";
import { getElection, nightFor, subscribe } from "../store.js";
import { card, currentElectionId, errorState, fictionalNotice } from "./_shared.js";

export async function render(el, params, ctx) {
  const id = ctx?.electionId || currentElectionId();
  const election = getElection(id);
  if (!election) {
    mount(el, errorState({ message: "No election selected." }));
    return;
  }
  const C = constitution();
  const S = {
    id,
    election,
    info: null, // /president payload (tickets with persons, provinces with flip_status)
    infoState: "loading", // loading | ok | none (no presidential race)
    snapshot: null,
    clock: null,
    electionStatus: election.status,
    index: createLineIndex(),
    calls: [],
    lastCallSeq: null,
    lastSeq: null,
    fallback: null, // {house, senate} when no night exists
    labelsLoaded: false,
    cards: new Map(),
    destroyed: false,
  };

  /* ---------------------------------------------------------------- layout */
  const phaseHost = h("span");
  const clockHost = h("span", { class: "lv-headclock mono" });
  const bannerHost = h("div", { class: "lv-banners" });
  const noticeHost = h("div", { class: "lv-notices" });

  const bigBar = evBar({ total: C.ev, majority: C.evMajority, tickets: [], large: true });
  const cardsGrid = h("div", { class: "lv-tickets" });
  const statReporting = statTile("Reporting", "expected ballots counted");
  const statCounted = statTile("Votes counted", "valid presidential votes");
  const statOutstanding = statTile("Est. outstanding", "ballots still to count (model estimate)");
  const statLead = statTile("Lead changes", "all races tonight");
  const evSummary = h("div", { class: "lv-board__summary" });
  const board = h(
    "div",
    { class: "lv-board" },
    h("div", { class: "lv-board__bar" }, evSummary, bigBar),
    cardsGrid,
    h("div", { class: "lv-board__stats" }, statReporting.el, statCounted.el, statOutstanding.el, statLead.el),
  );

  const map = ecMap({ height: 400, onSelect: (code) => (location.hash = `#/provinces/${code}`) });
  const kr = keyRaces();
  const house = chamberPanel({ kind: "house", total: C.house, majority: C.houseMajority, href: "#/house" });
  const senate = chamberPanel({ kind: "senate", total: C.senate, majority: C.senateMajority, href: "#/senate" });
  const feed = callFeed();

  const heroCard = card("Race for the Presidency", board, { className: "lv-area-hero", categories: ["SIMULATED"], actions: h("a", { class: "btn btn--sm btn--ghost", href: "#/president" }, "President page →") });
  const mapCard = card("Electoral College", map.el, { className: "lv-area-map", actions: map.toolbar, foot: "174 electoral votes · 88 to win · winner-take-all per province. Select a province for its results." });
  const krTitle = h("span", null, "Key races");
  const krCard = card(krTitle, kr.el, { className: "lv-area-races", actions: kr.toolbar, flush: true });
  const houseCard = card("House of Representatives", house.el, { className: "lv-area-house" });
  const senateCard = card("Senate", senate.el, { className: "lv-area-senate" });
  const feedCard = card("Race calls", feed.el, { className: "lv-area-feed lv-feedcard", actions: feed.toolbar, flush: true });

  const grid = h("div", { class: "lv-night" }, heroCard, mapCard, krCard, houseCard, senateCard, feedCard);

  mount(
    el,
    h(
      "header",
      { class: "page-head lv-head" },
      h(
        "div",
        null,
        h("div", { class: "page-head__eyebrow" }, `Election night · ${election.election_date || election.year}`),
        h("h1", { class: "page-head__title" }, election.name),
      ),
      h("div", { class: "page-head__meta" }, phaseHost, clockHost, provBadge("SIMULATED"), provBadge("FICTIONAL", "Fictional system")),
    ),
    bannerHost,
    noticeHost,
    grid,
  );

  /* ---------------------------------------------------------------- helpers */
  function person(key) {
    if (!key) return null;
    const t = (S.info?.tickets || []).find((x) => x.key === key);
    const snapT = (S.snapshot?.president?.tickets || []).find((x) => x.key === key);
    const info = S.index.get(key);
    const party = t?.party || snapT?.party || info?.party || null;
    return {
      key,
      name: t?.president?.name || ticketHead(snapT?.label || info?.label || key),
      party,
      color: colorFor(party, t?.color || snapT?.color || info?.color),
    };
  }

  function phase() {
    return phaseOf(getElection(id) || election, nightFor(id));
  }

  function presModel() {
    const snap = S.snapshot;
    const p = snap?.president;
    if (p) {
      const proj = new Map((snap.popular_vote?.lines || []).map((l) => [l.key, l]));
      return {
        total: p.ev_total ?? C.ev,
        needed: p.ev_needed ?? C.evMajority,
        decidedTotal: p.ev_decided_total ?? 0,
        uncalled: p.ev_uncalled ?? C.ev,
        winner: p.winner,
        contingentLikely: !!p.contingent_likely,
        majoritySeq: p.majority_reached_at_seq,
        decidedBy: p.decided_by || S.info?.decided_by || null,
        status: p.status,
        tickets: (p.tickets || []).map((t) => ({
          key: t.key,
          label: t.party || t.label,
          party: t.party,
          color: colorFor(t.party, t.color),
          name: t.label,
          decided: t.ev_decided ?? 0,
          leading: t.ev_leading ?? 0,
          maxPossible: t.ev_max_possible,
          votes: t.votes,
          pct: t.pct,
          projectedPct: proj.get(t.key)?.projected_pct ?? null,
        })),
        reporting: snap.reporting?.pct_expected_ballots ?? 0,
        reportingDetail: snap.reporting,
        counted: snap.popular_vote?.counted_valid ?? null,
        outstanding: snap.popular_vote?.outstanding_ballots_est ?? null,
      };
    }
    const info = S.info;
    if (!info) return null;
    const final = info.results_source === "final";
    return {
      total: info.electoral_votes_total ?? C.ev,
      needed: info.majority ?? C.evMajority,
      decidedTotal: info.ev_decided_total ?? (final ? C.ev : 0),
      uncalled: info.ev_uncalled ?? (final ? 0 : C.ev),
      winner: info.winner?.key || null,
      contingentLikely: !!info.contingent_likely,
      decidedBy: info.decided_by,
      status: info.status,
      tickets: (info.tickets || []).map((t) => ({
        key: t.key,
        label: t.party,
        party: t.party,
        color: colorFor(t.party, t.color),
        name: t.name,
        decided: t.electoral_votes ?? 0,
        leading: t.ev_leading ?? 0,
        votes: t.votes,
        pct: t.pct,
        projectedPct: null,
      })),
      reporting: info.popular_vote?.reporting_pct ?? (final ? 100 : 0),
      counted: info.popular_vote?.total_valid ?? null,
      outstanding: final ? 0 : null,
    };
  }

  function provinceRows() {
    const infoProv = new Map((S.info?.provinces || []).map((p) => [p.code, p]));
    const snapProv = S.snapshot?.provinces;
    if (snapProv && S.snapshot?.president) {
      return snapProv.map((p) => {
        const ip = infoProv.get(p.code) || {};
        const winner = p.called_key ? person(p.called_key) : null;
        return {
          code: p.code,
          name: p.name,
          ev: p.ev,
          status: p.status,
          leader: p.leader ? person(p.leader) : null,
          winner,
          margin: p.margin_pct,
          reporting: p.reporting_pct,
          winProb: p.win_probability,
          flip: winner && ip.winner === p.called_key ? ip.flip_status : null,
          previousParty: ip.previous_party || null,
          recounted: p.night_status === "RECOUNT" || p.decided_by === "recount",
        };
      });
    }
    return (S.info?.provinces || []).map((p) => ({
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
      recounted: false,
    }));
  }

  /* ---------------------------------------------------------------- renderers */
  function renderHeader() {
    const ph = phase();
    const key = `${ph}`;
    if (phaseHost.dataset.key !== key) {
      phaseHost.dataset.key = key;
      mount(phaseHost, phaseChip(ph));
    }
    const clk = S.clock;
    setText(clockHost, clk?.clock ? `${clk.clock} CET${ph === "running" ? ` · ${clk.speed}×` : ""}` : "");
  }

  function renderBoard(pm) {
    if (!pm) return;
    const sorted = [...pm.tickets].sort((a, b) => b.decided - a.decided || b.leading - a.leading || (b.votes || 0) - (a.votes || 0));
    bigBar.update({ total: pm.total, majority: pm.needed, tickets: sorted });
    const ph = phase();
    const live = ph === "running" || ph === "paused";
    mount(
      evSummary,
      h("span", { class: "lv-board__ev num" }, h("b", null, fmtInt(pm.decidedTotal)), ` of ${fmtInt(pm.total)} electoral votes allocated`),
      h("span", { class: "muted num" }, ` · ${fmtInt(pm.uncalled)} uncalled`),
      h("span", { class: "lv-board__rule muted" }, `${pm.needed} of ${pm.total} needed · winner-take-all by province`),
    );
    // PV leader = most counted votes (sorting only)
    const counting = (pm.counted ?? 0) > 0;
    const pvLeader = counting ? [...pm.tickets].sort((a, b) => (b.votes || 0) - (a.votes || 0))[0]?.key : null;
    const infoTickets = new Map((S.info?.tickets || []).map((t) => [t.key, t]));
    sorted.forEach((t, i) => {
      let c = S.cards.get(t.key);
      if (!c) {
        const it = infoTickets.get(t.key);
        c = ticketCard(
          {
            key: t.key,
            name: it?.president?.name || ticketHead(t.name),
            mate: it?.running_mate?.name || String(t.name || "").split(" / ")[1] || null,
            party: t.party,
            partyName: it?.party_name,
            color: t.color,
            portrait: it?.president?.portrait_key,
            incumbent: it?.incumbent,
          },
          { total: pm.total, needed: pm.needed },
        );
        S.cards.set(t.key, c);
        cardsGrid.appendChild(c);
      }
      c.style.order = String(i);
      const hidden = ph === "hidden" || ph === "ready";
      const winnerLabel = pm.decidedBy === "contingent" ? "ELECTED" : ph === "finished" || ph === "final" ? "ELECTED" : "WINS 88";
      c.update({
        decided: hidden ? 0 : t.decided,
        leading: t.leading,
        votes: hidden ? null : t.votes,
        pct: hidden ? null : t.pct,
        projectedPct: live && (pm.reporting ?? 0) > 0 && (pm.reporting ?? 0) < 100 ? t.projectedPct : null,
        winner: pm.winner === t.key,
        winnerLabel,
        winnerTitle: pm.decidedBy === "contingent" ? "Elected by the contingent election" : "Secured an Electoral College majority",
        pvLeader: !hidden && pvLeader === t.key && (pm.reporting ?? 0) < 100,
        out: (ph === "finished" || ph === "final") && pm.winner && pm.winner !== t.key && !t.decided,
      });
    });
    // stats
    const rd = pm.reportingDetail;
    statReporting.set(fmtPct(pm.reporting), rd ? `${fmtInt(rd.municipalities_reporting)} of ${fmtInt(rd.municipalities_total)} municipalities reporting` : "expected ballots counted", pm.reporting);
    statCounted.set(pm.counted === null || pm.counted === undefined ? "–" : fmtInt(pm.counted), "valid presidential votes");
    statOutstanding.set(pm.outstanding === null || pm.outstanding === undefined ? "–" : fmtCompact(pm.outstanding), (pm.outstanding ?? 0) > 0 ? "ballots still to count (model estimate)" : "all ballots counted");
    const lc = S.snapshot?.lead_changes;
    const latest = (lc?.recent || []).find((x) => String(x.race_key).startsWith("PRES")) || lc?.recent?.[0];
    let sub = "all races tonight";
    if (latest) {
      const race = (S.snapshot?.races || []).find((r) => r.key === latest.race_key);
      const from = S.index.get(latest.from_key);
      const to = S.index.get(latest.to_key);
      const where = race ? String(race.name).replace(/^President: /, "").replace(/ \(\d+ EV\)$/, "") : latest.race_key;
      sub = `Latest: ${where} · ${from?.party || ticketHead(from?.label) || latest.from_key} → ${to?.party || ticketHead(to?.label) || latest.to_key} (${fmtPct(latest.reporting_pct, 0)} in)`;
    }
    statLead.set(lc ? fmtInt(lc.count) : "–", sub);
    if (latest && statLead.lastSeq !== undefined && latest.seq !== statLead.lastSeq) statLead.pulse();
    statLead.lastSeq = latest?.seq;
  }

  function renderBanners(pm) {
    const ph = phase();
    const banners = [];
    const certified = S.snapshot?.certified;
    if (pm && pm.winner) {
      const w = person(pm.winner);
      const t = pm.tickets.find((x) => x.key === pm.winner);
      const final = ph === "finished" || ph === "final";
      if (pm.decidedBy === "contingent") {
        banners.push({
          key: `contingent-won|${pm.winner}`,
          color: w?.color,
          icon: "president",
          title: `${w?.name} elected President by contingent election`,
          sub: `No ticket reached ${pm.needed} electoral votes; the House voted by province delegations. ${t ? `${w?.name} (${w?.party}) had ${fmtInt(t.decided)} EV.` : ""}`,
          link: ["#/president", "Contingent election audit →"],
        });
      } else {
        banners.push({
          key: `won|${pm.winner}|${final}`,
          color: w?.color,
          icon: "check",
          title: final ? `${w?.name} elected President` : `${w?.name} secures an Electoral College majority`,
          sub: `${fmtInt(t?.decided)} electoral votes decided · ${pm.needed} needed of ${pm.total}.${final ? ` Decided by ${decidedByLabel(pm.decidedBy || "electoral_college").toLowerCase()}.` : " Projection by the calling model (SIMULATED)."}`,
          link: ["#/president", "President →"],
        });
      }
    } else if (pm && pm.contingentLikely) {
      banners.push({
        key: "contingent-likely",
        kind: "alert",
        icon: "alert",
        title: `No ticket can reach ${pm.needed} electoral votes`,
        sub: "A contingent election is likely: the House would choose the President by province delegations.",
        link: ["#/electoral-college", "Electoral College →"],
      });
    }
    const recountNow = (S.snapshot?.provinces || []).filter((p) => p.status === "RECOUNT");
    if (recountNow.length) {
      banners.push({
        key: `recount|${recountNow.map((p) => p.code).join(",")}`,
        kind: "recount",
        icon: "alert",
        title: `Recount: ${recountNow.map((p) => p.name).join(", ")}`,
        sub: "The margin is inside the automatic recount threshold; the province's electoral votes stay uncalled.",
      });
    }
    if ((ph === "finished" || ph === "final") && certified?.final) {
      const rc = certified.recounts || [];
      banners.push({
        key: `final|${rc.length}`,
        kind: "final",
        icon: "lock",
        title: "FINAL — certified result",
        sub: rc.length ? `${rc.length} race${rc.length === 1 ? "" : "s"} went to a recount: ${rc.slice(0, 8).join(", ")}${rc.length > 8 ? ` +${rc.length - 8} more` : ""}.` : "No automatic recounts.",
      });
    }
    const key = banners.map((b) => b.key).join("||");
    if (bannerHost.dataset.key === key) return;
    bannerHost.dataset.key = key;
    mount(
      bannerHost,
      banners.map((b) =>
        h(
          "div",
          { class: ["banner", "lv-banner", b.kind && `lv-banner--${b.kind}`], role: "status", style: b.color ? { "--party": b.color } : undefined },
          h("span", { class: "lv-banner__icon" }, icon(b.icon || "check", { size: 22 })),
          h("div", { class: "lv-banner__text" }, h("div", { class: "banner__title" }, b.title), b.sub ? h("div", { class: "lv-banner__sub" }, b.sub) : null),
          b.link ? h("a", { class: "btn btn--sm lv-banner__link", href: b.link[0] }, b.link[1]) : null,
        ),
      ),
    );
  }

  function renderNotices() {
    const ph = phase();
    const key = `${ph}|${S.infoState}|${!!S.snapshot}|${!!S.fallback}`;
    if (noticeHost.dataset.key === key) return;
    noticeHost.dataset.key = key;
    const items = [];
    if (ph === "ready" || ph === "hidden") {
      items.push(
        h(
          "div",
          { class: "notice lv-notice" },
          icon("night", { size: 16 }),
          h(
            "span",
            null,
            h("strong", null, `Polls closed at ${S.clock?.polls_close_local || "21:00"} CET. `),
            "No results are revealed before they are counted: ballots, candidates and incumbents only. Use ",
            h("strong", null, "Start"),
            " in the top bar to run the simulated count (1×–25×), or advance one reporting event at a time.",
          ),
        ),
      );
    }
    if (S.infoState === "none") items.push(h("div", { class: "notice" }, icon("alert", { size: 16 }), h("span", null, "This election has no presidential race (midterm): House, Senate and local races only.")));
    if (S.fallback && !S.snapshot) items.push(h("div", { class: "notice" }, icon("alert", { size: 16 }), h("span", null, "No stored election-night state for this election: showing the certified results only (no call feed).")));
    items.push(fictionalNotice());
    mount(noticeHost, items);
  }

  function chamberData(kind) {
    const snap = S.snapshot;
    const ph = phase();
    const final = ph === "finished" || ph === "final";
    if (snap && snap[kind]) {
      const c = snap[kind];
      return {
        parties: (c.by_party || []).map((p) => ({
          party: p.party,
          color: colorFor(p.party, p.color),
          holdover: kind === "senate" ? p.holdover || 0 : 0,
          called: p.called || 0,
          leading: p.leading || 0,
          net: kind === "house" ? (final ? p.net_change_called : p.net_change_called) : null,
        })),
        called: c.called,
        leading: c.leading,
        uncalled: c.uncalled,
        up: c.up,
        control: c.control,
        controlLabel: final && !c.control ? "No party won a majority" : null,
        final,
      };
    }
    const f = S.fallback?.[kind];
    if (f) {
      return {
        parties: (f.by_party || []).map((p) => ({
          party: p.party,
          color: colorFor(p.party, p.color),
          holdover: kind === "senate" ? p.holdover || 0 : 0,
          called: kind === "senate" ? p.won || 0 : p.won ?? p.total ?? 0,
          leading: 0,
          net: kind === "house" ? p.net_change : null,
        })),
        called: f.called ?? f.seats_total,
        leading: 0,
        uncalled: 0,
        up: f.up,
        control: f.control?.controlling_party || null,
        controlLabel: f.control?.label || null,
        final: true,
      };
    }
    return null;
  }

  function evByRace() {
    const out = {};
    for (const p of S.snapshot?.provinces || S.info?.provinces || []) if (p.race_key || p.race_code) out[p.race_key || p.race_code] = p.ev;
    return out;
  }

  function refresh() {
    if (S.destroyed) return;
    renderHeader();
    const hasPres = S.infoState !== "none" && (S.snapshot ? !!S.snapshot.president : !!S.info);
    heroCard.hidden = !hasPres && S.infoState !== "loading";
    mapCard.hidden = heroCard.hidden;
    grid.classList.toggle("lv-night--nopres", heroCard.hidden);
    const pm = hasPres ? presModel() : null;
    renderBoard(pm);
    renderBanners(pm);
    renderNotices();
    if (hasPres) map.update(provinceRows());
    const ph = phase();
    const final = ph === "finished" || ph === "final";
    krTitle.textContent = final ? "Closest races" : "Key races";
    kr.update(S.snapshot?.races || [], {
      index: S.index,
      final,
      emptyText: ph === "ready" || ph === "hidden" ? "Races appear here as soon as votes are counted." : S.snapshot ? null : "Not available without an election-night state.",
    });
    house.update(chamberData("house"));
    senate.update(chamberData("senate"));
    senateCard.hidden = !chamberData("senate");
    houseCard.hidden = !chamberData("house");
    feed.setEmptyText(ph === "ready" || ph === "hidden" ? "No race calls yet — the count has not started." : "No race calls yet.");
    feed.update(S.calls, { evByRace: evByRace() });
  }

  /* ---------------------------------------------------------------- data */
  const presFetch = throttledFetch(
    async (signal) => {
      // Midterm / municipal elections have no presidential race: skip the request entirely.
      if (String(getElection(id)?.election_type || "general") !== "general") {
        S.infoState = "none";
        refresh();
        return;
      }
      try {
        const info = await api.get(`/api/elections/${id}/president`, { signal });
        S.info = info;
        S.infoState = "ok";
        S.index.addTickets(info.tickets);
      } catch (err) {
        if (err?.name === "AbortError") throw err;
        S.infoState = err?.status === 404 ? "none" : "error";
      }
      refresh();
    },
    { minInterval: 4000 },
  );

  const callsFetch = throttledFetch(
    async (signal) => {
      const ph = phase();
      const final = ph === "finished" || ph === "final";
      const res = await api.get(`/api/elections/${id}/calls${final ? "" : "?limit=400"}`, { signal, cache: final && isFinalElection(getElection(id)) });
      S.calls = res.calls || [];
      for (const c of S.calls) if (c.line_key && c.candidate) S.index.addLines([{ key: c.line_key, label: c.candidate, party: c.party, color: c.color }]);
      refresh();
    },
    { minInterval: 3000 },
  );

  async function loadLabels() {
    if (S.labelsLoaded) return;
    S.labelsLoaded = true;
    const ph = phase();
    if (ph === "finished" || ph === "final") return; // final: the call log provides the names
    try {
      const full = await api.get(`/api/night/${id}/state?detail=full`, { timeout: 180000 });
      S.index.addFullRaces(full?.snapshot?.races || []);
      refresh();
    } catch (err) {
      if (err?.name !== "AbortError") console.warn("labels", err);
    }
  }

  async function loadFallback() {
    if (S.snapshot || S.fallback || S.destroyed) return;
    const soft = (p) => p.catch(() => null);
    const [house, senate] = await Promise.all([soft(api.get(`/api/elections/${id}/house`)), soft(api.get(`/api/elections/${id}/senate`))]);
    if (S.snapshot || S.destroyed) return;
    S.fallback = { house, senate };
    refresh();
  }

  function onNight(night) {
    if (S.destroyed) return;
    const n = nightFor(id);
    if (!n) return;
    const prevStatus = S.electionStatus;
    S.snapshot = n.snapshot;
    S.clock = n.clock;
    S.electionStatus = n.election_status || S.electionStatus;
    const seq = n.snapshot?.seq ?? n.clock?.seq ?? 0;
    const seqChanged = seq !== S.lastSeq;
    S.lastSeq = seq;
    if (S.snapshot?.president) S.index.addTickets((S.snapshot.president.tickets || []).map((t) => ({ key: t.key, name: t.label, party: t.party, color: t.color })));
    // status transition (start / finish / reset): refresh everything now
    if (prevStatus !== S.electionStatus) {
      presFetch.now();
      S.calls = [];
      callsFetch.now();
    } else if (seqChanged && n.clock?.status === "running") {
      presFetch.request();
    }
    const topCall = n.snapshot?.recent_calls?.[0]?.seq ?? null;
    if (topCall !== S.lastCallSeq) {
      S.lastCallSeq = topCall;
      if (topCall !== null) callsFetch.request();
    }
    loadLabels();
    refresh();
    void night;
  }

  const unsubNight = subscribe("night", onNight);
  const unsubSettings = subscribe("settings", () => refresh());
  presFetch.now();
  if (nightFor(id)) onNight(nightFor(id));
  else refresh();
  const final = isFinalElection(election);
  if (final) callsFetch.now();
  const fallbackTimer = setTimeout(() => {
    if (!S.snapshot) loadFallback();
  }, 4000);

  return () => {
    S.destroyed = true;
    clearTimeout(fallbackTimer);
    unsubNight();
    unsubSettings();
    presFetch.stop();
    callsFetch.stop();
    map.destroy();
  };
}

/* ------------------------------------------------------------------ stat tile */
function statTile(label, sub) {
  const value = h("span", { class: "stat__value" }, "–");
  const delta = h("span", { class: "stat__delta" }, sub);
  const bar = h("span", { class: "lv-stat__bar", "aria-hidden": "true" }, h("span"));
  const el = h("div", { class: "stat lv-stat" }, h("span", { class: "stat__label" }, label), value, delta);
  return {
    el,
    set(v, s, pct) {
      setText(value, v);
      if (s !== undefined) setText(delta, s);
      if (pct !== undefined) {
        if (!bar.isConnected) el.appendChild(bar);
        bar.firstChild.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      }
    },
    pulse() {
      el.classList.remove("lv-pulse");
      void el.offsetWidth;
      el.classList.add("lv-pulse");
    },
  };
}
