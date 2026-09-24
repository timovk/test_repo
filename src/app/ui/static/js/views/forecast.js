/**
 * #/forecast — Monte Carlo forecast of the selected election (SIMULATED model estimates of a
 * FICTIONAL system — never predictions).  Runs new forecasts through the job endpoints with
 * live progress and shows every distribution the API returns: EV histograms with the 88 line,
 * win probabilities and intervals, contingent-election probability, province win frequencies
 * (table + tile heat map), tipping points, most common Electoral College maps, popular-vote
 * distributions, House / Senate / governor seat distributions and per-race win probabilities.
 * The UI performs no election mathematics: every number is read from the API.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { fmtInt, fmt1, fmtProb, fmtShare } from "../format.js";
import { card, currentElectionId, electionPicker, links, pageHeader } from "./_shared.js";
import { dataTable } from "../components/table.js";
import { portrait } from "../components/ana-avatar.js";
import { provBadge } from "../components/badges.js";
import {
  PROVINCES, callout, every, errorBox, field, filterRow, kpi, kv, labelled, partyInfo, pchip, probCell,
  provinceName, refresh, segmented, selectBox, sortByPartyOrder, tabBar, toast, downloadLink, fmtDateTime,
} from "../components/ana-ui.js";
import { distChart, heatTiles, hbars, miniTiles, multiLine, rangeChart, stackBar, tipBox, tipRow } from "../components/ana-charts.js";

const SIMS = [1000, 10000, 50000, 100000];
const TABS = [
  { key: "president", label: "President" },
  { key: "provinces", label: "Provinces" },
  { key: "congress", label: "House & Senate" },
  { key: "races", label: "Race probabilities" },
  { key: "model", label: "Model & runs" },
];

export async function render(el, _params, ctx) {
  const id = ctx.electionId || currentElectionId();
  const cleanups = [];
  const st = {
    tab: ctx.query.tab || "president",
    sims: 10000,
    run: null,
    runs: [],
    envelope: null,
    tickets: {}, // line key → ticket of /president (person ids, portraits)
    racesCache: new Map(), // run id → include=races payload
    districts: null,
    raceFilter: { family: ctx.query.family || "HOUSE", province: "", q: "" },
  };

  const runPanelHost = h("div");
  const runInfoHost = h("div");
  const bodyHost = h("div", { class: "ana-fc" });
  mount(
    el,
    pageHeader({ eyebrow: "Analysis · Monte Carlo", title: "Forecast", categories: ["SIMULATED", "FICTIONAL"], meta: [electionPicker()] }),
    callout(
      "sim",
      h("strong", null, "Model-generated estimates of a fictional system, not predictions. "),
      "Each number is the share of simulated elections in which an outcome occurred, under this simulator's invented political model of an invented constitution. It says nothing about real Dutch politics.",
    ),
    h("div", { class: "ana-fc__top" }, runPanelHost, runInfoHost),
    bodyHost,
  );

  if (!id) {
    mount(bodyHost, errorBox(new Error("No election selected.")));
    return;
  }

  /* -------------------------------------------------------------- run panel */
  const seedInput = h("input", { class: "input", type: "number", min: 0, step: 1, inputmode: "numeric", placeholder: "Auto", "aria-describedby": "fc-seed-hint" });
  const usePolls = h("input", { type: "checkbox", checked: true });
  const incLocal = h("input", { type: "checkbox" });
  const progressHost = h("div", { class: "ana-fc__progress", "aria-live": "polite" });
  const runBtn = h("button", { class: "btn btn--primary", type: "button", onclick: () => startRun() }, "Run forecast");
  let stopJob = null;
  cleanups.push(() => stopJob && stopJob());

  mount(
    runPanelHost,
    card(
      "Run a new forecast",
      h(
        "div",
        { class: "ana-fc__runform" },
        field("Simulations", segmented(SIMS.map((n) => ({ value: n, label: fmtInt(n) })), st.sims, (v) => (st.sims = Number(v)), { label: "Number of simulations" })),
        field("Seed", seedInput, h("span", { id: "fc-seed-hint" }, "Blank = derived from the election seed")),
        h(
          "div",
          { class: "ana-fc__checks" },
          h("label", { class: "ana-check" }, usePolls, "Use stored polls"),
          h("label", { class: "ana-check", title: "Adds mayors, councils and provincial legislatures (slower)" }, incLocal, "Include local races"),
        ),
        h("div", { class: "ana-fc__go" }, runBtn),
      ),
      { foot: h("span", null, "Runs in the background on the server (one job at a time). 100,000 draws of the 2028 election take about 20 s."), categories: ["SIMULATED"] },
    ),
    progressHost,
  );

  function renderProgress(job) {
    const pct = Math.round((job.progress || 0) * 100);
    const label = job.status === "queued" ? "Queued" : job.status === "running" ? "Running" : job.status === "completed" ? "Completed" : "Failed";
    mount(
      progressHost,
      h(
        "div",
        { class: ["ana-progress", `is-${job.status}`] },
        h(
          "div",
          { class: "ana-progress__head" },
          h("strong", null, `${label} · ${fmtInt(job.simulations)} simulations`),
          h("span", { class: "muted num" }, job.status === "failed" ? job.error || "error" : `${fmtInt(job.done)} / ${fmtInt(job.total)} draws${job.duration_s ? ` · ${fmt1(job.duration_s)} s` : ""}`),
        ),
        h("div", { class: "ana-progress__track", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct, "aria-label": "Forecast progress" }, h("div", { class: "ana-progress__fill", style: { width: `${pct}%` } })),
        h("div", { class: "ana-note" }, `Job ${job.job_id} · seed ${job.seed ?? "auto"}${job.use_polls === false ? " · polls ignored" : ""}${job.include_local ? " · local races included" : ""}`),
      ),
    );
  }

  function trackJob(job) {
    renderProgress(job);
    runBtn.disabled = true;
    stopJob && stopJob();
    stopJob = every(450, async () => {
      let j;
      try {
        j = await api.get(`/api/forecast/jobs/${job.job_id}`);
      } catch (e) {
        stopJob();
        runBtn.disabled = false;
        toast(`Lost track of the job: ${e.message}`, "error");
        return;
      }
      renderProgress(j);
      if (j.status === "completed") {
        stopJob();
        runBtn.disabled = false;
        toast(`Forecast run #${j.run_id} completed (${fmtInt(j.simulations)} simulations)`, "success");
        await loadRuns(j.run_id);
        setTimeout(() => mount(progressHost), 2500);
      } else if (j.status === "failed") {
        stopJob();
        runBtn.disabled = false;
        toast(`Forecast failed: ${j.error || "unknown error"}`, "error");
      }
    });
  }

  async function startRun() {
    const body = { simulations: st.sims, use_polls: usePolls.checked, include_local: incLocal.checked };
    const seed = seedInput.value.trim();
    if (seed) {
      if (!/^\d+$/.test(seed)) {
        seedInput.setAttribute("aria-invalid", "true");
        toast("Seed must be a non-negative integer", "error");
        return;
      }
      body.seed = Number(seed);
    }
    seedInput.removeAttribute("aria-invalid");
    runBtn.disabled = true;
    try {
      const job = await api.post(`/api/forecast/${id}/run`, body);
      trackJob(job);
    } catch (e) {
      runBtn.disabled = false;
      toast(e.message, "error");
    }
  }

  /* -------------------------------------------------------------- data */
  async function loadRuns(selectRunId) {
    const runsP = api.get(`/api/forecast/${id}/runs`);
    const presP = Object.keys(st.tickets).length ? Promise.resolve(null) : api.get(`/api/elections/${id}/president`).catch(() => null);
    const [runs, pres] = await Promise.all([runsP, presP]);
    st.envelope = runs;
    st.runs = runs.runs || [];
    if (pres?.tickets) for (const t of pres.tickets) st.tickets[t.key] = t;
    const wanted = selectRunId || Number(ctx.query.run) || null;
    if (!st.runs.length) {
      st.run = null;
      renderAll();
      return;
    }
    const runId = wanted && st.runs.some((r) => r.run_id === wanted) ? wanted : st.runs[0].run_id;
    await showRun(runId);
  }

  async function showRun(runId) {
    await refresh(bodyHost, api.get(`/api/forecast/runs/${runId}`), (run) => {
      st.run = run;
      if (runId !== st.runs[0]?.run_id) setQuery({ run: runId });
      else setQuery({ run: null });
      return buildBody();
    });
    renderRunInfo();
  }

  function renderAll() {
    renderRunInfo();
    mount(bodyHost, buildBody());
  }

  function renderRunInfo() {
    const run = st.run?.run;
    if (!run) {
      mount(runInfoHost);
      return;
    }
    const sel = st.runs.length > 1
      ? selectBox(
          st.runs.map((r) => ({ value: r.run_id, label: `#${r.run_id} · ${fmtInt(r.n_simulations)} sims · seed ${r.seed}` })),
          run.id,
          (v) => showRun(Number(v)),
          { "aria-label": "Forecast run" },
        )
      : null;
    mount(
      runInfoHost,
      card(
        "Showing run",
        h(
          "div",
          null,
          h("div", { class: "ana-fc__runid" }, h("span", { class: "ana-fc__runno" }, `#${run.id}`), h("span", null, `${fmtInt(run.n_simulations)} simulations`), sel),
          kv(
            [
              ["Seed", h("span", { class: "mono" }, String(run.seed))],
              ["Finished", fmtDateTime(run.finished_at)],
              ["Duration", `${fmt1(run.duration_s)} s`],
              ["Polls", run.polls?.used ? `${run.polls.n_polls} (${String(run.polls.poll_type).replace(/_/g, " ")})` : "not used"],
            ],
            { cols: 2 },
          ),
        ),
        { categories: ["SIMULATED"] },
      ),
    );
  }

  /* -------------------------------------------------------------- body */
  function buildBody() {
    const fc = st.run;
    if (!fc) {
      const env = st.envelope;
      return h(
        "div",
        { class: "state ana-empty" },
        h("strong", null, "No forecast has been run for this election yet"),
        h("span", { class: "muted" }, `${env?.election?.name || "This election"}: choose the number of simulations above and press “Run forecast”.`),
      );
    }
    const hasPres = !!fc.president?.tickets?.length;
    const tabs = TABS.filter((t) => hasPres || (t.key !== "president" && t.key !== "provinces"));
    if (!tabs.some((t) => t.key === st.tab)) st.tab = tabs[0].key;
    const content = h("div", { class: "ana-fc__content" });
    const show = (key) => {
      st.tab = key;
      setQuery({ tab: key === "president" ? null : key });
      mount(content, buildTab(key));
    };
    const bar = tabBar(tabs, st.tab, show, { label: "Forecast sections" });
    show(st.tab);
    return h(
      "div",
      null,
      h("div", { class: "ana-row ana-row--between ana-fc__disc" }, h("span", { class: "ana-note" }, fc.disclaimer), provBadge("SIMULATED")),
      bar,
      content,
    );
  }

  function buildTab(key) {
    try {
      if (key === "president") return presidentTab();
      if (key === "provinces") return provincesTab();
      if (key === "congress") return congressTab();
      if (key === "races") return racesTab();
      return modelTab();
    } catch (e) {
      console.error(e);
      return errorBox(e);
    }
  }

  /* -------------------------------------------------------------- helpers */
  const tcolor = (t) => partyInfo(t.party, t.color).color;
  const ticketLabel = (key) => {
    const t = (st.run?.president?.tickets || []).find((x) => x.key === key);
    return t ? t.name : key;
  };
  const ticketParty = (key) => (st.run?.president?.tickets || []).find((x) => x.key === key)?.party;
  const ticketColor = (key) => {
    const t = (st.run?.president?.tickets || []).find((x) => x.key === key);
    return t ? tcolor(t) : "var(--uncalled)";
  };
  function personLink(key, name) {
    const pid = st.tickets[key]?.president?.id;
    return pid ? h("a", { class: "ana-link", href: links.candidate(pid) }, name) : h("span", null, name);
  }
  const contenders = () => (st.run.president.tickets || []).filter((t) => (t.prob_win || 0) > 0 || (t.ev?.mean || 0) >= 0.5 || (t.ev?.p95 || 0) > 0);

  /* -------------------------------------------------------------- President */
  function presidentTab() {
    const P = st.run.president;
    const all = P.tickets;
    const main = contenders();
    const others = all.filter((t) => !main.includes(t));
    const turnout = st.run.turnout;

    const ticketCards = h(
      "div",
      { class: "ana-fc__tickets" },
      main.map((t) => {
        const tk = st.tickets[t.key];
        const [pres, vp] = String(t.name).split(" / ");
        return h(
          "article",
          { class: "ana-ticket", style: { "--party": tcolor(t) } },
          h(
            "div",
            { class: "ana-ticket__head" },
            h("div", { class: "ana-ticket__avatar" }, portrait(pres, { color: tcolor(t), key: tk?.president?.portrait_key || t.key, size: 52 })),
            h("div", { class: "ana-ticket__who" }, h("div", { class: "ana-ticket__name" }, personLink(t.key, pres)), h("div", { class: "ana-ticket__vp" }, vp ? `with ${vp}` : ""), pchip(t.party, t.color)),
          ),
          h("div", { class: "ana-ticket__prob" }, h("span", { class: "ana-ticket__big" }, fmtProb(t.prob_win)), h("span", { class: "ana-ticket__cap" }, "wins outright (≥ 88 EV)")),
          h("div", { class: "ana-ticket__meter" }, h("div", { style: { width: `${(t.prob_win || 0) * 100}%` } })),
          kv(
            [
              ["Mean EV", fmt1(t.ev?.mean)],
              ["Median EV", fmtInt(t.ev?.median)],
              ["90% interval", `${fmtInt(t.ev?.p05)}–${fmtInt(t.ev?.p95)}`],
              ["Popular vote", fmtShare(t.pv_share?.mean)],
              ["PV 90% interval", `${fmtShare(t.pv_share?.p05)}–${fmtShare(t.pv_share?.p95)}`],
              ["Wins popular vote", fmtProb(t.prob_pv_plurality)],
            ],
            { cols: 1 },
          ),
        );
      }),
    );

    const kpis = h(
      "div",
      { class: "ana-kpis" },
      kpi("Contingent election", fmtProb(P.prob_contingent), "No ticket reaches 88 EV — the House decides"),
      kpi("EV / popular-vote split", fmtProb(P.prob_pv_ev_divergence), "EV leader is not the popular-vote leader"),
      kpi("Electoral-vote tie", fmtProb(P.prob_ev_tie), `${P.label} of ${P.total_ev} EV · ${String(P.method).replace(/_/g, " ")}`),
      turnout ? kpi("Turnout", fmtShare(turnout.mean), `90% interval ${fmtShare(turnout.p05)}–${fmtShare(turnout.p95)}`) : null,
      kpi("Simulations", fmtInt(st.run.run.n_simulations), `seed ${st.run.run.seed}`),
    );

    const evRange = rangeChart({
      rows: main.map((t) => ({ key: t.key, label: t.party, color: tcolor(t), q: t.ev, note: t.name })),
      domain: [0, P.total_ev],
      marker: { x: P.majority, label: P.label },
      unit: " EV",
      ariaLabel: "Electoral votes per ticket: median, 50% and 90% intervals",
    });

    const small = h(
      "div",
      { class: "ana-fc__multiples" },
      main.map((t) =>
        h(
          "figure",
          { class: "ana-fig" },
          h("figcaption", { class: "ana-fig__cap" }, h("span", { class: "ana-fig__title" }, pchip(t.party, t.color), " ", t.name), h("span", { class: "ana-fig__meta num" }, `P(≥ ${P.majority}) ${fmtProb(t.prob_win)} · median ${fmtInt(t.ev?.median)}`)),
          distChart({ probs: t.ev_histogram || [], color: tcolor(t), majority: P.majority, majorityLabel: P.label, unit: "EV", height: 170, ariaLabel: `Electoral-vote distribution for ${t.name}`, summary: `${t.name}: ${fmtProb(t.prob_win)} chance of 88 or more electoral votes` }),
        ),
      ),
    );

    const pvSeries = main
      .filter((t) => (t.pv_histogram || []).length)
      .map((t) => ({ key: t.key, label: `${t.party} · ${t.name.split(" / ")[0]}`, color: tcolor(t), area: true, points: t.pv_histogram.map(([x, p]) => ({ x: x * 100, y: p * 100 })) }));
    const pvChart = pvSeries.length
      ? multiLine({ series: pvSeries, xFormat: (x) => `${Number(x).toFixed(1)}%`, yFormat: (v) => `${v.toFixed(v < 1 ? 1 : 0)}%`, tipFormat: (v) => `${v.toFixed(2)}%`, height: 240, ariaLabel: "Popular-vote share distributions per ticket", xTicks: undefined })
      : h("div", { class: "state" }, "No popular-vote distribution in this run");

    const tp = Object.entries(P.tipping_point || {}).sort((a, b) => b[1] - a[1]);
    const tpChart = hbars({
      rows: tp.map(([code, p]) => ({ key: code, label: `${provinceName(code)}`, value: p, color: "var(--seq-4)", href: links.province(code), tipLabel: "Tipping-point frequency", note: "Share of simulations in which this province delivered the decisive 88th electoral vote" })),
      format: (v) => fmtProb(v),
      ariaLabel: "Tipping-point frequency by province",
      labelWidth: 120,
    });
    const tpBy = P.tipping_point_by_ticket || {};
    const tpTicketKeys = Object.keys(tpBy);
    const tpTable = dataTable(
      [
        { key: "code", label: "Province", format: (v) => h("a", { class: "ana-link", href: links.province(v) }, provinceName(v)) },
        { key: "all", label: "All", align: "r", format: (v) => fmtProb(v) },
        ...tpTicketKeys.map((k) => ({ key: k, label: h("span", { class: "ana-party-th" }, pchip(ticketParty(k), ticketColor(k))), align: "r", value: (r) => r[k], format: (v) => (v ? fmtProb(v) : "–") })),
      ],
      tp.map(([code, p]) => ({ code, all: p, ...Object.fromEntries(tpTicketKeys.map((k) => [k, tpBy[k]?.[code] ?? null])) })),
      { sortKey: "all" },
    );

    const combos = (P.combinations || []).map((c) =>
      h(
        "li",
        { class: "ana-combo" },
        h("span", { class: "ana-combo__rank" }, `#${c.rank}`),
        miniTiles(Object.fromEntries(Object.entries(c.winners || {}).map(([code, key]) => [code, { color: ticketColor(key), label: ticketLabel(key) }])), { title: `Map #${c.rank}` }),
        h(
          "div",
          { class: "ana-combo__body" },
          h("div", { class: "ana-combo__freq" }, h("b", { class: "num" }, fmtShare(c.frequency)), h("span", { class: "muted" }, " of simulations")),
          h(
            "div",
            { class: "ana-combo__ev" },
            Object.entries(c.ev || {})
              .sort((a, b) => b[1] - a[1])
              .map(([k, ev]) => h("span", { class: "ana-combo__t" }, pchip(ticketParty(k), ticketColor(k)), h("b", { class: "num" }, fmtInt(ev)))),
          ),
        ),
      ),
    );

    return h(
      "div",
      { class: "ana-stack-v" },
      ticketCards,
      others.length ? h("p", { class: "ana-note" }, `Not shown: ${others.map((t) => `${t.name} (${t.party})`).join(", ")} — never reach an electoral vote in these simulations (P(win) ${others.map((t) => fmtProb(t.prob_win)).join(" / ")}).`) : null,
      kpis,
      card("Electoral votes — median, 50% and 90% intervals", evRange, { categories: ["SIMULATED"], foot: `Dot = median; thick bar = 25–75 %; thin bar = 5–95 % of simulations. Line = ${P.label}.` }),
      card("Electoral-vote distributions", small, { foot: "Each column is the share of simulations ending with exactly that many electoral votes; solid columns reach the 88-vote majority. Use ← → on a focused chart to step through values." }),
      h(
        "div",
        { class: "grid grid--2" },
        card("Popular-vote distributions", pvChart, { foot: "Share of simulations per 0.5-point popular-vote bin." }),
        card("Tipping-point provinces", tpChart, { foot: "How often each province casts the decisive 88th electoral vote." }),
      ),
      h(
        "div",
        { class: "grid grid--2" },
        card("Most common Electoral College maps", combos.length ? h("ol", { class: "ana-combos" }, combos) : h("div", { class: "state" }, "No map frequencies in this run"), { flush: true }),
        card("Tipping point by ticket", tpTable, { flush: true, foot: "Conditional on that ticket winning the tipping province." }),
      ),
    );
  }

  /* -------------------------------------------------------------- Provinces */
  function provincesTab() {
    const P = st.run.president;
    const tickets = contenders();
    const rows = P.provinces.map((p) => {
      const fav = p.favourite;
      return { ...p, fav, favProb: p.win?.[fav] ?? null, favParty: ticketParty(fav), favColor: ticketColor(fav) };
    });
    const mix = (prob) => Math.round(18 + Math.max(0, Math.min(1, (prob - 0.5) / 0.5)) * 82);
    const tiles = heatTiles(
      PROVINCES.map((code) => {
        const r = rows.find((x) => x.code === code);
        if (!r) return { code, fill: "var(--uncalled-soft)", title: code };
        const pct = mix(r.favProb ?? 0.5);
        return {
          code,
          fill: `color-mix(in srgb, ${r.favColor} ${pct}%, var(--surface-2))`,
          ink: pct >= 62 ? "light" : "dark",
          bottom: `${r.ev} EV · ${fmtProb(r.favProb)}`,
          title: `${r.name}: ${r.ev} electoral votes; favourite ${ticketLabel(r.fav)} ${fmtProb(r.favProb)}`,
          tip: tipBox(
            `${r.name} · ${r.ev} EV`,
            Object.entries(r.win || {})
              .filter(([, p]) => p > 0)
              .sort((a, b) => b[1] - a[1])
              .map(([k, p]) => tipRow(ticketColor(k), ticketLabel(k), fmtProb(p))),
            `Tipping point in ${fmtProb(r.tipping_point)} of simulations`,
          ),
        };
      }),
      { onSelect: (code) => (location.hash = links.province(code)), ariaLabel: "Province win probability tile map" },
    );
    const scale = h(
      "div",
      { class: "ana-scale" },
      h("span", null, "Colour = favourite; intensity = its win probability"),
      h("span", { class: "ana-scale__ramp" }, [0.5, 0.65, 0.8, 0.95].map((p) => h("span", { style: { background: `color-mix(in srgb, var(--text-secondary) ${mix(p)}%, var(--surface-2))` }, title: `${Math.round(p * 100)}%` }))),
      h("span", null, "50% → 95%+"),
    );
    const table = dataTable(
      [
        { key: "name", label: "Province", format: (v, r) => h("a", { class: "ana-link", href: links.province(r.code) }, v) },
        { key: "ev", label: "EV", align: "r" },
        { key: "favParty", label: "Favourite", format: (v, r) => pchip(v, r.favColor) },
        { key: "favProb", label: "P(favourite)", format: (v, r) => probCell(v, r.favColor) },
        ...tickets.map((t) => ({ key: `w_${t.key}`, label: h("span", { class: "ana-party-th" }, pchip(t.party, t.color)), align: "r", value: (r) => r.win?.[t.key] ?? 0, format: (v) => (v > 0 ? fmtProb(v) : h("span", { class: "muted" }, "–")) })),
        ...tickets.slice(0, 3).map((t) => ({ key: `s_${t.key}`, label: `${t.party} share`, align: "r", value: (r) => r.share_mean?.[t.key] ?? null, format: (v) => fmtShare(v) })),
        { key: "tipping_point", label: "Tipping pt.", align: "r", format: (v) => fmtProb(v) },
      ],
      rows,
      { sortKey: "ev" },
    );
    return h(
      "div",
      { class: "ana-stack-v" },
      h("div", { class: "ana-split" }, card("Province win frequencies", table, { flush: true, categories: ["SIMULATED"], foot: "Share of simulations each ticket carries the province (winner-take-all). Mean share = average popular-vote share across simulations." }), card("Probability map", h("div", { class: "ana-stack-v" }, tiles, scale), { foot: "Tile cartogram of the 12 provinces; select a tile to open the province." })),
    );
  }

  /* -------------------------------------------------------------- Congress */
  function chamberBlock(ch, { title, unitLabel, short }) {
    if (!ch) return null;
    const parties = ch.parties.filter((p) => (p.seats?.mean || 0) >= 0.5 || (p.holdover || 0) > 0);
    const color = (p) => partyInfo(p.party, p.color).color;
    const marker = ch.majority ? { x: ch.majority, label: short } : null;
    const chart = rangeChart({
      rows: [...parties].sort((a, b) => (b.seats?.mean || 0) - (a.seats?.mean || 0)).map((p) => ({ key: p.party, label: p.party, color: color(p), q: p.seats, note: partyInfo(p.party).name })),
      domain: [0, ch.seats_total],
      marker,
      unit: ` ${unitLabel}`,
      ariaLabel: `${title}: seats per party, median and intervals`,
    });
    const table = dataTable(
      [
        { key: "party", label: "Party", format: (v, r) => pchip(v, r.color) },
        { key: "mean", label: "Mean", align: "r", value: (r) => r.seats?.mean, format: (v) => fmt1(v) },
        { key: "median", label: "Median", align: "r", value: (r) => r.seats?.median, format: (v) => fmtInt(v) },
        { key: "int", label: "90% interval", align: "r", sort: false, value: (r) => `${fmtInt(r.seats?.p05)}–${fmtInt(r.seats?.p95)}` },
        ch.majority ? { key: "prob_majority", label: `P(≥ ${ch.majority})`, format: (v, r) => probCell(v, color(r)) } : null,
        { key: "prob_plurality", label: "P(largest)", align: "r", format: (v) => fmtProb(v) },
        ch.seats_up !== ch.seats_total ? { key: "holdover", label: "Not up", align: "r" } : null,
      ].filter(Boolean),
      parties,
      { sortKey: "mean" },
    );
    const big = [...parties].sort((a, b) => (b.seats?.mean || 0) - (a.seats?.mean || 0)).slice(0, 4);
    const multiples = ch.majority
      ? h(
          "div",
          { class: "ana-fc__multiples" },
          big.map((p) =>
            h(
              "figure",
              { class: "ana-fig" },
              h("figcaption", { class: "ana-fig__cap" }, h("span", { class: "ana-fig__title" }, pchip(p.party, p.color), " ", partyInfo(p.party).name), h("span", { class: "ana-fig__meta num" }, `P(≥ ${ch.majority}) ${fmtProb(p.prob_majority)}`)),
              distChart({ probs: p.histogram || [], color: color(p), majority: ch.majority, majorityLabel: short, unit: unitLabel, height: 150, ariaLabel: `${title} seat distribution for ${p.party}` }),
            ),
          ),
        )
      : null;
    const compos = (ch.compositions || []).length
      ? h(
          "ol",
          { class: "ana-compos" },
          ch.compositions.map((c, i) => {
            const segs = sortByPartyOrder(Object.keys(c.seats)).map((code) => ({ key: code, label: partyInfo(code).name, color: partyInfo(code).color, value: c.seats[code] }));
            return h(
              "li",
              { class: "ana-compo" },
              h("span", { class: "ana-combo__rank" }, `#${i + 1}`),
              h("div", { class: "ana-grow" }, stackBar({ segments: segs, total: ch.seats_total, marker: ch.majority ? { at: ch.majority, label: short } : null, height: 16 }), h("div", { class: "ana-compo__legend" }, segs.map((s) => h("span", null, pchip(s.key, s.color), h("b", { class: "num" }, ` ${s.value}`))))),
              h("span", { class: "ana-compo__freq num" }, fmtShare(c.frequency)),
            );
          }),
        )
      : null;
    return h(
      "section",
      { class: "ana-stack-v ana-block" },
      h("div", { class: "ana-section-title" }, h("h2", null, title), h("p", null, `${ch.seats_up} of ${ch.seats_total} seats up${ch.majority ? ` · ${short}` : ""}`)),
      h(
        "div",
        { class: "ana-kpis" },
        ch.prob_no_majority != null ? kpi("No party in control", fmtProb(ch.prob_no_majority), `No party reaches ${ch.majority}`) : null,
        ...[...parties]
          .sort((a, b) => (b.prob_majority ?? b.prob_plurality ?? 0) - (a.prob_majority ?? a.prob_plurality ?? 0))
          .slice(0, 3)
          .map((p) => (ch.majority ? kpi(`${p.party} control`, fmtProb(p.prob_majority), `median ${fmtInt(p.seats?.median)} ${unitLabel}`, { accent: color(p) }) : kpi(`${p.party} most ${unitLabel}`, fmtProb(p.prob_plurality), `median ${fmtInt(p.seats?.median)}`, { accent: color(p) }))),
      ),
      h("div", { class: "grid grid--2" }, card(`${title} — ${unitLabel} per party`, chart, { categories: ["SIMULATED"], foot: `Dot = median; bars = 50% and 90% intervals${marker ? `; line = ${short}` : ""}.` }), card("Distribution table", table, { flush: true })),
      multiples ? card(`${title} seat distributions`, multiples, { foot: "Share of simulations per seat count; solid columns reach control." }) : null,
      compos ? card(`Most common ${title} compositions`, compos, { flush: true, foot: "Full chamber after the election (holdovers + seats won), share of simulations." }) : null,
    );
  }

  function congressTab() {
    const fc = st.run;
    return h(
      "div",
      { class: "ana-stack-v" },
      chamberBlock(fc.house, { title: "House", unitLabel: "seats", short: "76 FOR CONTROL" }),
      chamberBlock(fc.senate, { title: "Senate", unitLabel: "seats", short: "13 FOR CONTROL" }),
      chamberBlock(fc.governors, { title: "Governors", unitLabel: "governorships", short: "" }),
    );
  }

  /* -------------------------------------------------------------- Races */
  function racesTab() {
    const f = st.raceFilter;
    const families = [
      { value: "HOUSE", label: "House districts", ok: !!st.run.house?.races?.length },
      { value: "SEN", label: "Senate", ok: !!st.run.senate?.races?.length },
      { value: "GOV", label: "Governors", ok: !!st.run.governors?.races?.length },
    ].filter((x) => x.ok);
    if (!families.length) return h("div", { class: "state" }, "No per-race probabilities in this run");
    if (!families.some((x) => x.value === f.family)) f.family = families[0].value;
    const search = h("input", { class: "input", type: "search", placeholder: "Search district / candidate", value: f.q, "aria-label": "Search races", oninput: (e) => { f.q = e.target.value; draw(); } });
    const filters = filterRow(
      labelled("Race type", segmented(families, f.family, (v) => { f.family = v; setQuery({ family: v === "HOUSE" ? null : v }); draw(); }, { label: "Race type" })),
      labelled("Province", selectBox([{ value: "", label: "All provinces" }, ...PROVINCES.map((c) => ({ value: c, label: provinceName(c) }))], f.province, (v) => { f.province = v; draw(); }, { "aria-label": "Province" })),
      labelled("Search", search),
    );
    const tableHost = h("div");
    const runId = st.run.run.id;
    const needs = [];
    if (!st.racesCache.has(runId)) needs.push(api.get(`/api/forecast/runs/${runId}?include=races`).then((d) => st.racesCache.set(runId, d.races || {})).catch(() => st.racesCache.set(runId, {})));
    if (!st.districts) needs.push(api.get("/api/districts", { cache: true }).then((d) => (st.districts = Object.fromEntries(d.districts.map((x) => [x.code, x])))).catch(() => (st.districts = {})));
    function draw() {
      const races = st.racesCache.get(runId) || {};
      const src = f.family === "HOUSE" ? st.run.house.races : f.family === "SEN" ? st.run.senate.races : st.run.governors.races;
      const rows = src.map((r) => {
        const detail = races[r.race_code];
        const sorted = Object.entries(r.win || {}).sort((a, b) => b[1] - a[1]);
        const [fav, pFav] = sorted[0] || [null, null];
        const [ru, pRu] = sorted[1] || [null, null];
        const line = (party) => detail?.lines?.find((l) => l.party_code === party);
        const code = r.race_code.replace(/^(HOUSE|SEN|GOV)-/, "");
        const prov = code.slice(0, 2);
        const dist = st.districts?.[code];
        const name = f.family === "HOUSE" ? dist?.name || code : f.family === "SEN" ? `${provinceName(prov)} seat ${code.split("-")[1]}` : provinceName(prov);
        return { race: r.race_code, code, prov, name, fav, pFav, ru, pRu, favName: line(fav)?.label || "", favShare: line(fav)?.share_mean ?? null, ruName: line(ru)?.label || "", nCands: Object.keys(r.win || {}).length };
      });
      const q = f.q.trim().toLowerCase();
      const filtered = rows.filter((r) => (!f.province || r.prov === f.province) && (!q || `${r.code} ${r.name} ${r.favName} ${r.ruName}`.toLowerCase().includes(q)));
      const href = (r) => (f.family === "HOUSE" ? links.district(r.code) : links.race(r.race));
      mount(
        tableHost,
        h("div", { class: "ana-table-tools" }, h("span", { class: "ana-count" }, `${filtered.length} of ${rows.length} races · sorted by the favourite's probability (closest first)`), provBadge("SIMULATED")),
        dataTable(
          [
            { key: "code", label: f.family === "HOUSE" ? "District" : "Race", format: (v, r) => h("a", { class: "ana-link mono", href: href(r) }, v) },
            { key: "name", label: "Name", format: (v) => h("span", { class: "ana-trunc", title: v }, v) },
            { key: "fav", label: "Favourite", format: (v) => pchip(v) },
            { key: "favName", label: "Candidate", format: (v) => v || h("span", { class: "muted" }, "–") },
            { key: "pFav", label: "P(win)", format: (v, r) => probCell(v, partyInfo(r.fav).color) },
            { key: "favShare", label: "Mean share", align: "r", format: (v) => fmtShare(v) },
            { key: "ru", label: "Runner-up", format: (v) => (v ? pchip(v) : "–") },
            { key: "pRu", label: "P(runner-up)", align: "r", format: (v) => (v ? fmtProb(v) : "–") },
          ],
          filtered,
          { sortKey: "pFav", sortDir: "asc", maxHeight: 620 },
        ),
      );
    }
    mount(tableHost, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "40%", height: "14px" } })));
    Promise.all(needs).then(draw);
    return h("div", null, filters, card(null, tableHost, { flush: true }), h("p", { class: "ana-note" }, "Candidate names and mean shares come from the run's per-race summaries (include=races). Favourite = highest win probability returned by the model."));
  }

  /* -------------------------------------------------------------- Model */
  function modelTab() {
    const run = st.run.run;
    const polls = run.polls || {};
    const parties = sortByPartyOrder(Object.keys({ ...(polls.poll_mean_pct || {}), ...(polls.model_expected_pct || {}) }));
    const pollTable = polls.used
      ? dataTable(
          [
            { key: "party", label: "Party", format: (v) => pchip(v) },
            { key: "expected", label: "Model expectation", align: "r", format: (v) => (v == null ? "–" : `${fmt1(v)}%`) },
            { key: "poll", label: "Poll average", align: "r", format: (v) => (v == null ? "–" : `${fmt1(v)}%`) },
            { key: "shift", label: "Applied shift (logit)", align: "r", format: (v) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(3)}`) },
          ],
          parties.map((p) => ({ party: p, expected: polls.model_expected_pct?.[p], poll: polls.poll_mean_pct?.[p], shift: polls.shift?.[p] })),
          { sortKey: "poll" },
        )
      : h("div", { class: "state" }, polls.reason || "This run did not use polls.");
    const shiftChart = polls.used
      ? hbars({ rows: parties.map((p) => ({ key: p, label: p, labelNode: pchip(p), value: polls.shift?.[p] || 0, color: partyInfo(p).color, tipLabel: "Logit shift" })), diverging: true, format: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(3)}`, ariaLabel: "National logit shift per party from the polling average", labelWidth: 70 })
      : null;
    const t = st.run.turnout;
    const runsTable = dataTable(
      [
        { key: "run_id", label: "Run", format: (v) => h("span", { class: "mono" }, `#${v}`) },
        { key: "n_simulations", label: "Simulations", align: "r", format: (v) => fmtInt(v) },
        { key: "seed", label: "Seed", format: (v) => h("span", { class: "mono" }, String(v)) },
        { key: "finished_at", label: "Finished", format: (v) => fmtDateTime(v) },
        { key: "duration_s", label: "Duration", align: "r", format: (v) => `${fmt1(v)} s` },
        { key: "config_hash", label: "Config", format: (v) => h("span", { class: "mono muted" }, String(v || "").slice(0, 8)) },
        { key: "status", label: "Status" },
        { key: "view", label: "", sort: false, value: (r) => r.run_id, format: (v) => (v === run.id ? h("span", { class: "muted" }, "showing") : h("button", { class: "btn btn--sm", type: "button", onclick: () => showRun(v) }, "View")) },
      ],
      st.runs,
      { sortKey: "run_id" },
    );
    return h(
      "div",
      { class: "ana-stack-v" },
      h(
        "div",
        { class: "grid grid--2" },
        card("Polling input", h("div", { class: "ana-stack-v" }, kv([["Poll group", polls.used ? `${String(polls.poll_type).replace(/_/g, " ")} · ${polls.geo_code}` : "–"], ["As of", polls.as_of || "–"], ["Polls", polls.n_polls ?? "–"], ["Effective sample", fmtInt(polls.effective_n)], ["Prior SD (national)", polls.model_prior_sd ?? "–"]], { cols: 2 }), pollTable), { flush: false, categories: ["SIMULATED"] }),
        card("National shift from polls", shiftChart || h("div", { class: "state" }, "No shift applied"), { foot: "Logit-scale shift of each party's national utility, estimated from the polling average against the model's pre-election expectation." }),
      ),
      h(
        "div",
        { class: "grid grid--2" },
        card("Run metadata", kv([["Run", `#${run.id}`], ["Status", run.status], ["Seed", h("span", { class: "mono" }, String(run.seed))], ["Simulations", fmtInt(run.n_simulations)], ["Started", fmtDateTime(run.started_at)], ["Finished", fmtDateTime(run.finished_at)], ["Duration", `${fmt1(run.duration_s)} s`], ["Code version", run.code_version], ["Config hash", h("span", { class: "mono" }, run.config_hash || "–")], ["Model fingerprint", h("span", { class: "mono" }, run.model_fingerprint || "–")], ["Race types", (run.race_types || []).map((x) => x.replace(/_/g, " ").toLowerCase()).join(", ")]], { cols: 1 })),
        card("Turnout distribution", t ? kv([["Mean", fmtShare(t.mean)], ["Median", fmtShare(t.median)], ["5th percentile", fmtShare(t.p05)], ["25th percentile", fmtShare(t.p25)], ["75th percentile", fmtShare(t.p75)], ["95th percentile", fmtShare(t.p95)]], { cols: 1 }) : h("div", { class: "state" }, "–"), {
          foot: "National turnout (% of eligible voters) across simulations.",
          actions: h("div", { class: "ana-row" }, downloadLink(`/api/export/${id}/montecarlo_summary.csv?run_id=${run.id}`, "Summary CSV"), downloadLink(`/api/export/${id}/montecarlo_distribution.csv?run_id=${run.id}`, "Distributions CSV")),
        }),
      ),
      card(`All forecast runs (${st.runs.length})`, runsTable, { flush: true }),
    );
  }

  /* -------------------------------------------------------------- start */
  mount(bodyHost, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "50%", height: "14px" } })));
  try {
    await loadRuns();
  } catch (e) {
    mount(bodyHost, errorBox(e));
  }
  try {
    const jobs = await api.get(`/api/forecast/jobs?election=${id}`);
    const active = (jobs.jobs || []).find((j) => j.status === "queued" || j.status === "running");
    if (active) trackJob(active);
  } catch {
    /* job list is optional */
  }
  return () => cleanups.forEach((f) => f());
}
