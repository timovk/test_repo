/**
 * Election night of a LOCAL election (rendered by views/night.js when the selected election is
 * local): there is no President, House or Senate, so the broadcast shows
 *   * headline tiles: races called x / total, measures passing / passed, reporting %,
 *     municipalities reporting — plus a progress bar of the night (called · counting · awaiting),
 *   * the live race list (the Local results row renderer, refetched from /races on every night
 *     update, throttled),
 *   * the race-call feed (/calls),
 *   * the strict date-order banner when earlier elections must be finished first.
 * The playback controls stay in the national strip.  Live numbers come from the night snapshot
 * (store.night); no result is computed here (only API flags are counted).
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { provBadge } from "../components/badges.js";
import { icon } from "../components/icons.js";
import { callFeed } from "../components/live-feed.js";
import { isFinalElection, phaseChip, phaseOf, setText, throttledFetch } from "../components/live-util.js";
import { earlierBanner, fmtDate, isDecidedStatus, loadProvinceNames, provinceName, raceBrowser, scheduledBanner, tally } from "../components/local-kit.js";
import { getElection, nightFor, subscribe } from "../store.js";
import { card, fictionalNotice } from "./_shared.js";

const FEED_TYPES = [
  ["", "All races"],
  ["SCHOOL_BOARD,WATER_BOARD", "Boards"],
  ["BALLOT_MEASURE", "Measures"],
  ["MAYOR,COUNCIL_SEAT,RECALL", "Specials & recalls"],
];

/** Colour of a nonpartisan call: Yes / No for questions, the API's neutral colour otherwise. */
function callColor(c) {
  if (c.race_type === "BALLOT_MEASURE" || c.race_type === "RECALL") return c.line_key === "YES" ? "var(--yes)" : c.line_key === "NO" ? "var(--no)" : null;
  return c.line_key ? c.color || "var(--uncalled)" : null;
}

/** "Yes"/"No" calls of questions read as outcomes. */
function describeCall(c) {
  if (c.race_type === "BALLOT_MEASURE") return c.line_key === "YES" ? "Passes (Yes)" : c.line_key === "NO" ? "Fails (No)" : null;
  if (c.race_type === "RECALL") return c.line_key === "YES" ? "Recalled (Yes)" : c.line_key === "NO" ? "Retained (No)" : null;
  return null;
}

export async function renderLocalNight(el, election) {
  const id = election.id;
  await loadProvinceNames();
  const prov = provinceName(election.province_code, election);
  const S = { snapshot: null, clock: null, electionStatus: election.status, rows: null, payload: null, calls: [], lastSeq: null, lastCallSeq: null, destroyed: false };

  /* ---------------------------------------------------------------- layout */
  const phaseHost = h("span");
  const clockHost = h("span", { class: "lv-headclock mono" });
  const earlier = earlierBanner(id);
  const sched = scheduledBanner(id);
  const bannerHost = h("div", { class: "lv-banners" });
  const noticeHost = h("div", { class: "lv-notices" });

  const stCalled = statTile("Races called", "called or projected");
  const stMeasures = statTile("Measures passing", "ballot questions");
  const stReporting = statTile("Reporting", "of the expected ballots");
  const stMunis = statTile("Municipalities", "reporting");
  const progress = h("div", { class: "lc-progress", role: "img" });
  const progressLegend = h("div", { class: "lc-progress__legend" });
  const board = h("div", { class: "lc-board" }, h("div", { class: "lc-board__bar" }, progress, progressLegend), h("div", { class: "lv-board__stats lc-board__stats" }, stCalled.el, stMeasures.el, stReporting.el, stMunis.el));

  const browser = raceBrowser({ electionId: id, local: true, pageSize: 80 });
  const feed = callFeed({ typeOptions: FEED_TYPES, describe: describeCall, colorOf: callColor });

  const heroCard = card(`Tonight in ${prov}`, board, { className: "lc-area-hero", categories: ["SIMULATED"], actions: h("a", { class: "btn btn--sm btn--ghost", href: `#/local?e=${id}` }, "Local results page →") });
  const racesCard = card("Races", browser.el, { className: "lc-area-races lc-night-races", flush: true });
  const feedCard = card("Race calls", feed.el, { className: "lc-area-feed lv-feedcard", actions: feed.toolbar, flush: true });

  mount(
    el,
    h(
      "header",
      { class: "page-head lv-head" },
      h("div", null, h("div", { class: "page-head__eyebrow" }, `Local election night · ${prov} · ${fmtDate(election.election_date, "long")}`), h("h1", { class: "page-head__title" }, election.name)),
      h("div", { class: "page-head__meta" }, phaseHost, clockHost, provBadge("SIMULATED"), provBadge("FICTIONAL", "Fictional system")),
    ),
    earlier,
    sched,
    bannerHost,
    noticeHost,
    h("div", { class: "lc-night" }, heroCard, racesCard, feedCard),
  );

  /* ---------------------------------------------------------------- model */
  const phase = () => phaseOf(getElection(id) || election, nightFor(id));

  function counts() {
    const snap = S.snapshot;
    if (snap?.races?.length) {
      const races = snap.races;
      const measures = races.filter((r) => r.type === "BALLOT_MEASURE");
      const decided = races.filter((r) => isDecidedStatus(r.status)).length;
      const counting = races.filter((r) => !isDecidedStatus(r.status) && (r.reporting_pct ?? 0) > 0).length;
      const rep = snap.reporting || {};
      return {
        total: races.length,
        decided,
        projected: races.filter((r) => r.status === "PROJECTED_WINNER").length,
        counting,
        measures: measures.length,
        yes: measures.filter((r) => r.passing === true && (r.reporting_pct ?? 0) > 0).length,
        no: measures.filter((r) => r.passing === false && (r.reporting_pct ?? 0) > 0).length,
        reporting: rep.pct_expected_ballots ?? 0,
        munisReporting: rep.municipalities_reporting ?? 0,
        munisComplete: rep.municipalities_complete ?? 0,
        munisTotal: rep.municipalities_total ?? null,
        recounts: races.filter((r) => r.status === "RECOUNT"),
      };
    }
    if (S.rows) {
      const src = S.payload?.results_source || "hidden";
      const t = tally(S.rows, src);
      const final = src === "final";
      return {
        total: t.total,
        decided: t.decided,
        projected: S.rows.filter((r) => r.status === "PROJECTED_WINNER").length,
        counting: S.rows.filter((r) => !r.decided && (r.reporting_pct ?? 0) > 0).length,
        measures: t.measures,
        yes: t.measuresYes,
        no: t.measuresNo,
        reporting: final ? 100 : 0,
        munisReporting: final ? t.municipalityCount : 0,
        munisComplete: final ? t.municipalityCount : 0,
        munisTotal: t.municipalityCount,
        recounts: S.rows.filter((r) => r.status === "RECOUNT").map((r) => ({ key: r.code, name: r.name })),
      };
    }
    return null;
  }

  /* ---------------------------------------------------------------- renderers */
  function renderHeader() {
    const ph = phase();
    if (phaseHost.dataset.key !== ph) {
      phaseHost.dataset.key = ph;
      mount(phaseHost, phaseChip(ph));
    }
    const clk = S.clock;
    setText(clockHost, clk?.clock ? `${clk.clock} CET${ph === "running" ? ` · ${clk.speed}×` : ""}` : "");
  }

  function renderBoard(c) {
    const ph = phase();
    const final = ph === "finished" || ph === "final";
    const hidden = ph === "hidden" || ph === "ready";
    if (!c) {
      stCalled.set("–", "no night state yet");
      return;
    }
    stCalled.set(`${fmtInt(c.decided)} / ${fmtInt(c.total)}`, final ? "every race decided" : c.projected ? `${fmtInt(c.projected)} projected · ${fmtInt(c.counting)} counting` : `${fmtInt(c.counting)} counting`, c.total ? (c.decided / c.total) * 100 : 0);
    stMeasures.label(final ? "Measures passed" : "Measures passing");
    stMeasures.set(c.measures ? `${fmtInt(c.yes)} / ${fmtInt(c.measures)}` : "–", c.measures ? (hidden ? `${fmtInt(c.measures)} ballot questions` : `${fmtInt(c.no)} ${final ? "failed" : "failing"}`) : "no measures on this ballot", c.measures ? (c.yes / c.measures) * 100 : undefined);
    stReporting.set(fmtPct(c.reporting), final ? "all ballots counted" : "of the expected ballots", c.reporting);
    stMunis.set(c.munisTotal ? `${fmtInt(c.munisReporting)} / ${fmtInt(c.munisTotal)}` : "–", final ? "every municipality complete" : `reporting · ${fmtInt(c.munisComplete)} complete`, c.munisTotal ? (c.munisReporting / c.munisTotal) * 100 : 0);
    const awaiting = Math.max(0, c.total - c.decided - c.counting);
    const seg = (cls, n, label) => (n > 0 ? h("span", { class: `lc-progress__seg lc-progress__seg--${cls}`, style: { flexGrow: n }, title: `${fmtInt(n)} ${label}` }) : null);
    const key = JSON.stringify([c.decided, c.counting, awaiting]);
    if (progress.dataset.key !== key) {
      progress.dataset.key = key;
      progress.setAttribute("aria-label", `${c.decided} races called, ${c.counting} counting, ${awaiting} awaiting results`);
      mount(progress, seg("called", c.decided, "called or projected"), seg("counting", c.counting, "counting"), seg("awaiting", awaiting, "awaiting results"));
      mount(
        progressLegend,
        h("span", { class: "legend__item" }, h("span", { class: "lc-progress__key lc-progress__key--called" }), h("b", { class: "num" }, fmtInt(c.decided)), " called"),
        h("span", { class: "legend__item" }, h("span", { class: "lc-progress__key lc-progress__key--counting" }), h("b", { class: "num" }, fmtInt(c.counting)), " counting"),
        h("span", { class: "legend__item" }, h("span", { class: "lc-progress__key lc-progress__key--awaiting" }), h("b", { class: "num" }, fmtInt(awaiting)), hidden ? " awaiting the count" : " awaiting results"),
        h("span", { class: "lc-progress__total muted" }, `${fmtInt(c.total)} races on the ballot`),
      );
    }
  }

  function renderBanners(c) {
    const ph = phase();
    const banners = [];
    if (c?.recounts?.length) {
      banners.push({
        key: `recount|${c.recounts.map((r) => r.key).join(",")}`,
        kind: "recount",
        icon: "alert",
        title: `Recount: ${c.recounts.map((r) => r.name).slice(0, 3).join(", ")}${c.recounts.length > 3 ? ` +${c.recounts.length - 3}` : ""}`,
        sub: "The margin is inside the automatic recount threshold.",
      });
    }
    if (ph === "finished" || ph === "final") {
      banners.push({
        key: `final|${c?.decided}|${c?.yes}`,
        kind: "final",
        icon: "lock",
        title: "FINAL — certified result",
        sub: c ? `${fmtInt(c.total)} races decided${c.measures ? ` · ${fmtInt(c.yes)} of ${fmtInt(c.measures)} measures passed` : ""}.` : "Every race is certified.",
        link: [`#/local?e=${id}`, "All results →"],
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
          { class: ["banner", "lv-banner", b.kind && `lv-banner--${b.kind}`], role: "status" },
          h("span", { class: "lv-banner__icon" }, icon(b.icon, { size: 22 })),
          h("div", { class: "lv-banner__text" }, h("div", { class: "banner__title" }, b.title), b.sub ? h("div", { class: "lv-banner__sub" }, b.sub) : null),
          b.link ? h("a", { class: "btn btn--sm lv-banner__link", href: b.link[0] }, b.link[1]) : null,
        ),
      ),
    );
  }

  function renderNotices() {
    const ph = phase();
    const scheduled = (getElection(id) || election).status === "scheduled";
    if (noticeHost.dataset.key === `${ph}|${scheduled}`) return;
    noticeHost.dataset.key = `${ph}|${scheduled}`;
    const items = [];
    if ((ph === "ready" || ph === "hidden") && !scheduled) {
      items.push(
        h(
          "div",
          { class: "notice lv-notice" },
          icon("night", { size: 16 }),
          h(
            "span",
            null,
            h("strong", null, `Polls closed at ${S.clock?.polls_close_local || "21:00"} CET. `),
            "No results are revealed before they are counted: ballots, candidates and ballot questions only. Use ",
            h("strong", null, "Start"),
            " in the top bar to run the simulated count, or advance one reporting event at a time.",
          ),
        ),
      );
    }
    items.push(
      fictionalNotice(
        "A fictional local election on real geography: provinces, municipalities and water authorities are real (CBS/PDOK); school boards, ballot measures, special elections, recalls, candidates and all results are simulated.",
      ),
    );
    mount(noticeHost, items);
  }

  function refresh() {
    if (S.destroyed) return;
    renderHeader();
    const c = counts();
    renderBoard(c);
    renderBanners(c);
    renderNotices();
    const ph = phase();
    feed.setEmptyText(ph === "ready" || ph === "hidden" ? "No race calls yet — the count has not started." : "No race calls yet.");
    feed.update(S.calls, {});
  }

  /* ---------------------------------------------------------------- data */
  const racesFetch = throttledFetch(
    async (signal) => {
      const d = await api.get(`/api/elections/${id}/races`, { signal });
      S.payload = d;
      S.rows = d.races || [];
      browser.update(d);
      refresh();
    },
    { minInterval: 2500 },
  );

  const callsFetch = throttledFetch(
    async (signal) => {
      const ph = phase();
      const final = ph === "finished" || ph === "final";
      const res = await api.get(`/api/elections/${id}/calls${final ? "" : "?limit=400"}`, { signal, cache: final && isFinalElection(getElection(id)) });
      S.calls = res.calls || [];
      refresh();
    },
    { minInterval: 3000 },
  );

  function onNight() {
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
    if (prevStatus !== S.electionStatus) {
      racesFetch.now();
      S.calls = [];
      callsFetch.now();
      earlier.refresh();
    } else if (seqChanged) {
      racesFetch.request();
    }
    const topCall = n.snapshot?.recent_calls?.[0]?.seq ?? null;
    if (topCall !== S.lastCallSeq) {
      S.lastCallSeq = topCall;
      if (topCall !== null) callsFetch.request();
    }
    refresh();
  }

  const unsubNight = subscribe("night", onNight);
  const unsubMeta = subscribe("meta", () => {
    renderHeader();
    earlier.refresh();
    sched.refresh();
  });
  racesFetch.now();
  if (isFinalElection(election) || election.status === "live") callsFetch.now();
  if (nightFor(id)) onNight();
  else refresh();

  return () => {
    S.destroyed = true;
    unsubNight();
    unsubMeta();
    earlier.destroy();
    sched.destroy();
    racesFetch.stop();
    callsFetch.stop();
  };
}

/* ------------------------------------------------------------------ stat tile */
function statTile(labelText, sub) {
  const label = h("span", { class: "stat__label" }, labelText);
  const value = h("span", { class: "stat__value" }, "–");
  const delta = h("span", { class: "stat__delta" }, sub);
  const bar = h("span", { class: "lv-stat__bar", "aria-hidden": "true" }, h("span"));
  const el = h("div", { class: "stat lv-stat" }, label, value, delta);
  return {
    el,
    label(t) {
      setText(label, t);
    },
    set(v, s, pct) {
      setText(value, v);
      if (s !== undefined) setText(delta, s);
      if (pct !== undefined) {
        if (!bar.isConnected) el.appendChild(bar);
        bar.firstChild.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      } else if (bar.isConnected) bar.remove();
    },
  };
}
