/**
 * #/today — the world clock (docs/CLOCK.md), the home page.  The FICTIONAL world has one "today";
 * it moves from election day to election day and on each one the user watches its night or counts
 * it instantly.
 *
 *   * header: today's date (GET /api/clock `today_label`) and what the clock is;
 *   * the Today card: today's election (name, provinces, what is on the ballot, status) with
 *     **Watch the night** / **Count instantly** while unfinished, its headline result once final,
 *     and **Next election day ▶** (POST /api/clock/next) → the arrival dialog (watch or count?);
 *   * the running count / skip job (GET /api/clock/job, polled by clock.js) with its progress;
 *   * Skip ahead (POST /api/clock/skip, confirmed), the news (GET /api/clock/news), the agenda
 *     (GET /api/clock/agenda, grouped by month) and the user's own people (GET /api/people).
 *
 * Nothing is decided here: today, what is pending, the next day, results and news are API values.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtInt } from "../format.js";
import { resAvatar as avatar } from "../components/res-avatar.js";
import { provBadge } from "../components/badges.js";
import { icon } from "../components/icons.js";
import { toast } from "../components/live-toast.js";
import { statusInfo } from "../components/election-picker.js";
import { electionProvinces, fmtDate, loadProvinceNames, provinceName, provinceTags, provincesText, typeLabel } from "../components/local-kit.js";
import { pc } from "../components/res-kit.js";
import { arrivalDialog, confirmDialog, dateLeaf, dayBallot, dayLabel, daysBetween, inDays, kindTag, newsItem, resultsHref } from "../components/clock-ui.js";
import { clockIsBusy, clockIsFresh, countToday, goToNextElectionDay, jobSummary, onClockJobDone, openAndStartNight, refreshClock, skipTo, watchToday } from "../clock.js";
import { getElection, getState, subscribe } from "../store.js";

const NEWS_FILTERS = [
  ["", "All"],
  ["person", "Your people"],
  ["result", "Results"],
  ["event", "Vacancies & recalls"],
];
const CHANCE_WORDS = [
  [0, "never"],
  [0.1, "rarely"],
  [0.3, "sometimes"],
  [0.6, "often"],
  [1, "always"],
];
const iso = (d) => d.toISOString().slice(0, 10);
const addDays = (isoDate, n) => iso(new Date(new Date(`${isoDate}T00:00:00Z`).getTime() + n * 86400000));
const plural = (n, one, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;

function chanceWord(c) {
  if (c === null || c === undefined) return null;
  let best = CHANCE_WORDS[0];
  for (const w of CHANCE_WORDS) if (Math.abs(w[0] - c) < Math.abs(best[0] - c)) best = w;
  return best[1];
}

export async function render(el) {
  const ctrl = new AbortController();
  await loadProvinceNames();
  const S = {
    agenda: null,
    agendaRange: { start: null, end: null },
    news: null,
    newsFilter: "",
    newsLimit: 30,
    people: null,
    lastJob: null, // a job that finished while this page was open (its summary card)
    seq: { agenda: 0, news: 0, people: 0 }, // a newer request supersedes an older one
    loading: { agenda: false, news: false, people: false },
    destroyed: false,
  };

  /* ---------------------------------------------------------------- layout */
  const titleEl = h("h1", { class: "page-head__title td-title" }, getState().clock?.today_label || "Today");
  const header = h(
    "header",
    { class: "page-head td-head" },
    h(
      "div",
      null,
      h("div", { class: "page-head__eyebrow" }, "World clock · Today"),
      titleEl,
      h(
        "p",
        { class: "td-lede" },
        "One date for the whole simulated world. On an election day you ",
        h("b", null, "watch"),
        " its night or ",
        h("b", null, "count"),
        " it instantly; then the clock moves on to the next election day.",
      ),
    ),
    h("div", { class: "page-head__meta" }, provBadge("FICTIONAL", "Fictional world"), provBadge("REAL", "Real geography")),
  );
  const jobHost = h("div", { class: "td-jobslot" });
  const todayPane = h("div", { class: "td-pane td-pane--today" });
  const nextPane = h("div", { class: "td-pane td-pane--next" });
  const hero = h("section", { class: "card td-hero", "aria-label": "Today and the next election day" }, todayPane, nextPane);

  const agendaBody = h("div", { class: "td-agenda" }, skeleton());
  const agendaMeta = h("span", { class: "muted td-count" });
  const agendaCard = h(
    "section",
    { class: "card td-card td-agenda-card" },
    h("div", { class: "card__head" }, h("h2", { class: "card__title" }, "Agenda"), h("div", { class: "page-head__meta" }, agendaMeta, h("a", { class: "btn btn--sm btn--ghost", href: "#/calendar" }, icon("calendar", { size: 12 }), "Local calendar"))),
    agendaBody,
  );

  const skipBody = h("div", { class: "card__body td-skip" });
  const skipCard = h("section", { class: "card td-card" }, h("div", { class: "card__head" }, h("h2", { class: "card__title" }, "Skip ahead"), h("div", { class: "page-head__meta" }, icon("fastForward", { size: 14 }))), skipBody);

  const newsChips = h("div", { class: "lc-chips td-newschips", role: "group", "aria-label": "News filter" });
  const newsBody = h("div", { class: "td-newsbody" }, skeleton());
  const newsCard = h(
    "section",
    { class: "card td-card td-news-card" },
    h("div", { class: "card__head" }, h("h2", { class: "card__title" }, "News"), h("div", { class: "page-head__meta" }, provBadge("SIMULATED"))),
    h("div", { class: "td-newstools" }, newsChips),
    newsBody,
  );

  const peopleBody = h("div", { class: "td-people" }, skeleton());
  const peopleMeta = h("span", { class: "muted td-count" });
  const peopleCard = h(
    "section",
    { class: "card td-card td-people-card" },
    h("div", { class: "card__head" }, h("h2", { class: "card__title" }, "Your people"), h("div", { class: "page-head__meta" }, peopleMeta, provBadge("FICTIONAL"))),
    peopleBody,
  );

  mount(
    el,
    header,
    jobHost,
    hero,
    h("div", { class: "td-grid" }, h("div", { class: "td-col td-col--main" }, agendaCard), h("div", { class: "td-col td-col--side" }, skipCard, newsCard, peopleCard)),
    h(
      "div",
      { class: "notice notice--fictional td-foot" },
      provBadge("FICTIONAL"),
      h("span", null, "The calendar, every election, candidate, vacancy, recall and result are fictional simulations; your own people come from ", h("code", null, "config/people.yaml"), ". Provinces and municipalities are real (CBS / PDOK)."),
    ),
  );

  /* ---------------------------------------------------------------- helpers */
  function skeleton() {
    return h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "60%", height: "14px" } }));
  }

  const busyNow = () => clockIsBusy();
  const todayDay = () => (S.agenda?.days || []).find((d) => d.when === "today") || null;

  function spinnerLabel(text, ink) {
    return [h("span", { class: ["lc-spinner", ink && "lc-spinner--ink"], "aria-hidden": "true" }), text];
  }

  function resultHeadline(id) {
    const it = (S.news?.items || []).find((n) => n.kind === "result" && Number(n.election_id) === Number(id));
    return it ? it.text.replace(/^[^:]+:\s*/, "") : null;
  }

  function statusText(e) {
    const s = e?.status;
    if (s === "scheduled") return ["scheduled", "Not simulated yet"];
    if (s === "simulated") return ["ready", "Polls closed · not counted"];
    if (s === "live") return ["live", "Night in progress"];
    if (s === "final" || s === "certified") return ["final", "Final"];
    return statusInfo(e);
  }

  /* ---------------------------------------------------------------- actions */
  async function act(kind, fn) {
    try {
      return await fn();
    } catch (err) {
      toast(err?.message || `Could not ${kind}`, { kind: "error", timeout: 9000 });
      paintHero();
      return null;
    }
  }

  const doWatch = () => act("start the night", () => watchToday());
  /** A new count / skip replaces the summary card of the previous job right away. */
  function clearLastJob() {
    S.lastJob = null;
    paintJob();
  }

  const doCount = () =>
    act("count the election", () => {
      clearLastJob();
      return countToday();
    });

  async function doNext() {
    const prevNext = getState().clock?.next || null;
    paintHero();
    const res = await act("go to the next election day", () => goToNextElectionDay());
    if (!res?.arrived || S.destroyed) {
      paintHero();
      return;
    }
    const a = res.arrived;
    selectToday(a.election_id);
    loadSide();
    openArrival({
      label: res.today_label,
      date: a.today,
      election: a.election,
      day: prevNext && prevNext.date === a.today && prevNext.counts && Object.keys(prevNext.counts).length ? prevNext : null,
      news: a.news || [],
      previous: a.previous,
    });
  }

  /** Show the day's election in the top bar (the election picker follows the world clock). */
  async function selectToday(id) {
    if (!id || Number(getState().electionId) === Number(id)) return;
    const { selectElection } = await import("../app.js");
    selectElection(Number(id));
  }

  function openArrival(opts) {
    arrivalDialog({
      ...opts,
      onWatch: () => watchToday(),
      onCount: () => {
        clearLastJob();
        return countToday();
      },
      onLater: () => paintHero(),
    });
  }

  async function doSkip(date) {
    const c = getState().clock;
    if (!c || !date) return;
    if (date <= c.today) {
      toast(`The clock only moves forward — choose a date after ${fmtDate(c.today, "long")}.`, { kind: "error" });
      return;
    }
    const between = (S.agenda?.days || []).filter((d) => d.date >= c.today && d.date < date && !d.finished);
    const known = S.agenda && S.agenda.end >= date;
    const span = daysBetween(c.today, date);
    const ok = await confirmDialog({
      title: `Skip ahead to ${fmtDate(date, "long")}?`,
      iconName: "fastForward",
      confirmLabel: "Skip and count",
      body: [
        h(
          "p",
          null,
          "Every election day before that date is ",
          h("b", null, "counted instantly"),
          ", in date order — no election nights. ",
          known ? h("b", null, `${plural(between.length, "election day")} `) : null,
          known ? "lie in between. " : null,
          "If the date is itself an election day, its election is created and waits for you.",
        ),
        h("p", { class: "muted" }, span > 400 ? `About ${Math.round(span / 365)} years: this can take several minutes.` : "This can take a minute or two on the real geography. You can keep browsing; the top bar shows the progress."),
      ],
    });
    if (!ok) return;
    await act("skip ahead", () => {
      clearLastJob();
      return skipTo(date);
    });
  }

  /* ---------------------------------------------------------------- the Today card */
  function paintHero() {
    if (S.destroyed) return;
    const c = getState().clock;
    if (!c) {
      mount(todayPane, skeleton());
      mount(nextPane);
      return;
    }
    if (titleEl.textContent !== c.today_label) titleEl.textContent = c.today_label;
    // The clock is being re-read after a count / skip: the hero shows the previous day meanwhile.
    hero.classList.toggle("td-hero--stale", getState().clockBusy === "refresh");
    paintTodayPane(c);
    paintNextPane(c);
  }

  function paintTodayPane(c) {
    const busy = busyNow();
    const todays = (c.elections_today || []).map((e) => ({ ...e, ...(getElection(e.id) || {}), provinces: e.provinces?.length ? e.provinces : getElection(e.id)?.provinces || [] }));
    const e = todays[0] || null;
    const earlier = (c.pending || []).filter((p) => p.election_date < c.today);
    const unfinished = e && !["final", "certified"].includes(e.status);
    hero.classList.toggle("td-hero--day", !!unfinished);
    hero.classList.toggle("td-hero--done", !unfinished);
    const day = todayDay();
    const key = JSON.stringify([c.today, e?.id, e?.status, busy, earlier.map((x) => x.id), !!day, !!S.news, S.loading.news, day?.date]);
    keyed(todayPane, key, () => {
      if (!e) {
        return [
          h("div", { class: "td-pane__kicker" }, icon("calendar", { size: 12 }), "Today"),
          h("div", { class: "td-today" }, dateLeaf(c.today, { size: "lg" }), h("div", { class: "td-today__main" }, h("h2", { class: "td-today__name" }, "No election today"), h("p", { class: "td-today__text" }, `Nothing is on the ballot on ${c.today_label}.`))),
          earlier.length ? earlierNote(earlier, busy) : null,
        ];
      }
      const codes = electionProvinces(e);
      const [scls, sword] = statusText(e);
      const live = e.status === "live";
      const items = [
        h("div", { class: "td-pane__kicker" }, unfinished ? h("span", { class: "td-pulse", "aria-hidden": "true" }) : icon("check", { size: 12 }), unfinished ? "Election day — today" : "Election day — counted"),
        h(
          "div",
          { class: "td-today" },
          dateLeaf(c.today, { size: "lg" }),
          h(
            "div",
            { class: "td-today__main" },
            h("h2", { class: "td-today__name" }, e.name),
            h(
              "div",
              { class: "td-today__tags" },
              kindTag(e.local ? "local" : e.election_type),
              codes.length ? provinceTags(codes) : null,
              h("span", { class: `ep-status ep-status--${scls}` }, scls === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, sword),
              day?.races ? h("span", { class: "muted num" }, plural(day.races, "race")) : null,
            ),
            codes.length ? h("p", { class: "td-today__text" }, `${provincesText(codes, { names: true })} ${codes.length === 1 ? "votes" : "vote"} today.`) : null,
            day ? dayBallot(day, { max: 6 }) : null,
          ),
        ),
      ];
      if (unfinished) {
        const watchDisabled = !!busy || earlier.length > 0;
        items.push(
          h(
            "div",
            { class: "td-actions" },
            live
              ? h("button", { class: "btn btn--primary td-btn td-btn--lg", type: "button", disabled: !!busy, onclick: () => openAndStartNight(e.id) }, icon("night", { size: 16 }), "Back to the night")
              : h(
                  "button",
                  { class: ["btn", "btn--primary", "td-btn", "td-btn--lg", busy === "watch" && "is-busy"], type: "button", disabled: watchDisabled, title: earlier.length ? "Finish the earlier elections first (Count instantly counts them all)" : "Simulate the hidden result and open the election night at polls closing", onclick: doWatch },
                  busy === "watch" ? spinnerLabel("Simulating the election…", true) : [icon("eye", { size: 16 }), "Watch the night"],
                ),
            h(
              "button",
              { class: ["btn", "td-btn", "td-btn--lg", "td-btn--count", busy === "count" && "is-busy"], type: "button", disabled: !!busy, title: "Run the whole night at once: every call is stored", onclick: doCount },
              busy === "count" ? spinnerLabel("Counting…") : [icon("zap", { size: 16 }), live ? "Count the rest instantly" : earlier.length ? `Count all ${earlier.length + 1} instantly` : "Count instantly"],
            ),
          ),
          h(
            "p",
            { class: "td-hint" },
            live
              ? "The election night is under way. Return to it, or finish the count at once."
              : busy === "watch"
                ? "Simulating the hidden result and preparing the election night — this can take up to a minute on the real geography. The night opens and starts by itself."
                : "Watch: the hidden result is simulated and the night opens at polls closing (21:00 CET) — calls come in as the votes are counted. Count instantly: the whole night runs at once; every call is stored, so the results look the same.",
          ),
        );
        if (earlier.length) items.push(earlierNote(earlier, busy));
      } else {
        const head = resultHeadline(e.id);
        items.push(
          head ? h("p", { class: "td-headline" }, icon("check", { size: 14 }), head) : S.news && !S.loading.news ? null : h("div", { class: "skeleton td-headline-skel" }),
          h(
            "div",
            { class: "td-links" },
            h("a", { class: "btn td-btn", href: resultsHref(e) }, icon(e.local ? "ballot" : e.election_type === "general" ? "president" : "house", { size: 14 }), e.local ? "Local results" : e.election_type === "general" ? "President" : "House results"),
            e.local ? null : h("a", { class: "btn btn--ghost td-btn", href: `#/local?e=${e.id}` }, icon("ballot", { size: 14 }), "Every race"),
            h("a", { class: "btn btn--ghost td-btn", href: `#/night?e=${e.id}` }, icon("night", { size: 14 }), "Election night"),
          ),
        );
      }
      return items;
    });
  }

  function earlierNote(earlier, busy) {
    const f = earlier[0];
    return h(
      "div",
      { class: "notice td-earlier" },
      icon("alert", { size: 16 }),
      h(
        "span",
        null,
        h("b", null, `${plural(earlier.length, "earlier election")} not finished yet`),
        " — elections are certified in strict date order. ",
        "Next: ",
        h("a", { class: "res-link", href: `#/night?e=${f.id}` }, f.name),
        String(f.name).includes(fmtDate(f.election_date, "long")) ? ". " : ` (${fmtDate(f.election_date)}). `,
        h(
          "button",
          { class: "btn btn--sm", type: "button", disabled: !!busy, onclick: doCount },
          icon("zap", { size: 12 }),
          `Count ${earlier.length === 1 ? "it" : "them"} instantly`,
        ),
      ),
    );
  }

  function paintNextPane(c) {
    const n = c.next;
    const busy = busyNow();
    const key = JSON.stringify([c.today, n?.date, n?.status, n?.races, c.can_advance, busy]);
    keyed(nextPane, key, () => {
      if (!n) return [h("div", { class: "td-pane__kicker" }, icon("calendar", { size: 12 }), "Next election day"), h("p", { class: "td-today__text" }, "No election day ahead in the calendar.")];
      const codes = n.provinces || [];
      const gap = daysBetween(c.today, n.date);
      return [
        h("div", { class: "td-pane__kicker" }, icon("calendar", { size: 12 }), "Next election day", h("span", { class: "td-pane__when" }, inDays(gap))),
        h(
          "div",
          { class: "td-next" },
          dateLeaf(n.date, { size: "md" }),
          h(
            "div",
            { class: "td-next__main" },
            h("div", { class: "td-next__label" }, n.label),
            h("div", { class: "td-next__name" }, n.name),
            h("div", { class: "td-today__tags" }, kindTag(n.kind), codes.length ? provinceTags(codes) : null, n.races ? h("span", { class: "muted num" }, plural(n.races, "race")) : null),
            codes.length ? h("p", { class: "td-next__provs" }, provincesText(codes, { names: true })) : null,
            dayBallot(n, { max: 4 }),
          ),
        ),
        h(
          "div",
          { class: "td-next__go" },
          h(
            "button",
            { class: ["btn", "td-btn", "td-btn--lg", "td-btn--next", c.can_advance && "btn--primary", busy === "next" && "is-busy"], type: "button", disabled: !c.can_advance || !!busy, onclick: doNext },
            busy === "next" ? spinnerLabel(n.kind === "local" ? "Creating the day's election…" : "Creating the election… (a few seconds)", c.can_advance) : ["Next election day", icon("fastForward", { size: 15 })],
          ),
          c.can_advance ? h("span", { class: "td-hint" }, `Moves the world to ${fmtDate(n.date, "long")} and asks: watch or count?`) : h("span", { class: "td-hint" }, icon("lock", { size: 11 }), " Finish today's election first — watch its night or count it instantly."),
        ),
      ];
    });
  }

  /* ---------------------------------------------------------------- job progress */
  function paintJob() {
    if (S.destroyed) return;
    const job = getState().clockJob;
    const running = job?.running;
    const starting = !running && (getState().clockBusy === "count" || getState().clockBusy === "skip");
    // While the count / skip request is on its way, a "starting" card stands in for the job.
    const show = running ? job : starting ? { id: "starting", kind: getState().clockBusy, status: "running", running: true, done: 0, total: 0, current: null, seconds: 0 } : S.lastJob;
    if (!show) {
      mount(jobHost);
      return;
    }
    const pct = show.total ? Math.min(100, (show.done / show.total) * 100) : show.running ? 4 : 100;
    const kindWord = show.kind === "skip" ? "Skipping ahead" : "Counting instantly";
    const key = JSON.stringify([show.id, show.status, show.done, show.total, show.current, running ? Math.floor(show.seconds / 5) : show.seconds]);
    keyed(jobHost, key, () => {
      if (show.status === "error") {
        return h(
          "section",
          { class: "card td-job td-job--error", role: "alert" },
          h("span", { class: "td-job__icon" }, icon("alert", { size: 18 })),
          h("div", { class: "td-job__main" }, h("div", { class: "td-job__title" }, `${kindWord} stopped`), h("div", { class: "td-job__sub" }, show.error || "Unknown error — see the server terminal.")),
          dismissBtn(),
        );
      }
      if (show.running) {
        return h(
          "section",
          { class: "card td-job", role: "status", "aria-live": "polite" },
          h("span", { class: "td-job__icon" }, h("span", { class: "lc-spinner", "aria-hidden": "true" })),
          h(
            "div",
            { class: "td-job__main" },
            h("div", { class: "td-job__title" }, kindWord, h("span", { class: "td-job__count num" }, show.total > 1 ? `${fmtInt(show.done)} / ${fmtInt(show.total)} elections` : "")),
            // One election (or a job still sizing itself up): an indeterminate bar.
            h(
              "div",
              { class: ["td-job__bar", show.total <= 1 && "is-indeterminate"], role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(show.total || 1), "aria-valuenow": show.total > 1 ? String(show.done || 0) : undefined, "aria-label": kindWord },
              h("span", { style: { width: `${pct}%` } }),
            ),
            h("div", { class: "td-job__sub" }, show.current ? ["Now counting ", h("b", null, show.current)] : "Starting…", h("span", { class: "muted num" }, ` · ${fmtInt(Math.round(show.seconds || 0))} s`), h("span", { class: "muted" }, " · you can keep browsing; the top bar shows the progress")),
          ),
        );
      }
      const counted = show.result?.counted || [];
      const lastId = counted[counted.length - 1];
      const lastEl = lastId ? getElection(lastId) : null;
      return h(
        "section",
        { class: "card td-job td-job--done", role: "status" },
        h("span", { class: "td-job__icon" }, icon("check", { size: 18 })),
        h(
          "div",
          { class: "td-job__main" },
          h("div", { class: "td-job__title" }, show.kind === "skip" ? "Skipped ahead" : "Counted"),
          h("div", { class: "td-job__sub" }, jobSummary(show), show.kind === "skip" && show.result?.news ? h("span", { class: "muted" }, ` ${plural(show.result.news.length, "news item")} below.`) : null),
        ),
        lastEl && show.kind === "count" ? h("a", { class: "btn btn--sm td-job__link", href: resultsHref(lastEl) }, "Results", icon("chevronRight", { size: 12 })) : null,
        dismissBtn(),
      );
    });
  }

  function dismissBtn() {
    return h(
      "button",
      {
        class: "btn btn--ghost btn--sm td-job__x",
        type: "button",
        "aria-label": "Dismiss",
        onclick: () => {
          S.lastJob = null;
          paintJob();
        },
      },
      icon("x", { size: 13 }),
    );
  }

  /* ---------------------------------------------------------------- skip ahead */
  function paintSkip() {
    const c = getState().clock;
    const busy = busyNow();
    const key = JSON.stringify([c?.today, busy, c?.can_advance, S.agenda?.end || null]);
    if (skipBody.dataset.key === key) return;
    skipBody.dataset.key = key;
    if (!c) {
      mount(skipBody, skeleton());
      return;
    }
    const min = addDays(c.today, 1);
    const y = Number(c.today.slice(0, 4));
    const nextNov = (() => {
      // the first regular (November) election day after today, from the agenda when known
      const reg = (S.agenda?.days || []).find((d) => d.kind !== "local" && d.date > c.today);
      return reg ? reg.date : null;
    })();
    const input = h("input", { class: "input td-skip__date", type: "date", min, value: addDays(c.today, 91), "aria-label": "Skip to date", disabled: !!busy });
    const presets = [
      ["+3 months", addDays(c.today, 91)],
      ["+6 months", addDays(c.today, 182)],
      ["+1 year", addDays(c.today, 365)],
      nextNov ? [`To ${fmtDate(nextNov)}`, nextNov] : null,
      [`1 Jan ${y + 1}`, `${y + 1}-01-01`],
    ].filter(Boolean);
    mount(
      skipBody,
      h("p", { class: "td-skip__text" }, "Count every election day before a date instantly and move the clock there. The clock only moves forward."),
      h(
        "form",
        {
          class: "td-skip__form",
          onsubmit: (ev) => {
            ev.preventDefault();
            doSkip(input.value);
          },
        },
        input,
        h("button", { class: ["btn", "td-btn", busy === "skip" && "is-busy"], type: "submit", disabled: !!busy }, busy === "skip" ? spinnerLabel("Starting…") : [icon("fastForward", { size: 14 }), "Skip"]),
      ),
      h(
        "div",
        { class: "td-skip__presets", role: "group", "aria-label": "Quick dates" },
        presets.map(([label, date]) =>
          h(
            "button",
            {
              type: "button",
              class: "lc-chip td-preset",
              disabled: !!busy || date <= c.today,
              title: fmtDate(date, "long"),
              onclick: () => {
                input.value = date;
                doSkip(date);
              },
            },
            label,
          ),
        ),
      ),
    );
  }

  /* ---------------------------------------------------------------- agenda */
  function paintAgenda() {
    const a = S.agenda;
    if (!a) return;
    const days = a.days || [];
    agendaMeta.textContent = `${fmtDate(a.start)} – ${fmtDate(a.end)} · ${plural(days.length, "election day")}`;
    const byMonth = new Map();
    for (const d of days) {
      const k = d.date.slice(0, 7);
      if (!byMonth.has(k)) byMonth.set(k, []);
      byMonth.get(k).push(d);
    }
    // Where today falls when it is not an election day itself: a divider before the first upcoming day.
    const hasToday = days.some((d) => d.when === "today");
    const firstUpcoming = hasToday ? null : days.find((d) => d.when === "upcoming") || null;
    const nowMarker = () =>
      h(
        "li",
        { class: "td-now", "aria-label": `Today, ${getState().clock?.today_label || fmtDate(a.today, "long")}` },
        h("span", { class: "td-now__tag" }, "Today"),
        h("span", { class: "td-now__label" }, `${getState().clock?.today_label || fmtDate(a.today, "long")} · no election`),
      );
    const span = (Date.parse(a.end) - Date.parse(a.start)) / 86400000;
    const canEarlier = span + 182 <= 3 * 366;
    const canLater = span + 365 <= 3 * 366;
    mount(
      agendaBody,
      h(
        "div",
        { class: "td-agenda__more td-agenda__more--top" },
        h("button", { class: "btn btn--sm btn--ghost", type: "button", disabled: !canEarlier, onclick: () => extendAgenda(-182, 0), title: canEarlier ? "Show six more months before" : "The agenda shows at most three years" }, icon("chevronDown", { size: 12, className: "td-flip" }), "Earlier"),
      ),
      days.length
        ? [...byMonth.entries()].map(([k, list]) => {
            // Today's own month heads the divider's group; otherwise it stands between the months.
            const here = firstUpcoming && list.includes(firstUpcoming);
            const inMonth = here && String(a.today).slice(0, 7) === k;
            return [
              here && !inMonth ? h("ol", { class: "td-days td-days--now" }, nowMarker()) : null,
              h(
                "section",
                { class: "td-month" },
                h("h3", { class: "td-month__title" }, fmtDate(`${k}-01`, "month"), h("span", { class: "muted num" }, plural(list.length, "election day"))),
                h("ol", { class: "td-days" }, list.map((d) => (inMonth && d === firstUpcoming ? [nowMarker(), agendaRow(d)] : agendaRow(d)))),
              ),
            ];
          })
        : h("div", { class: "state" }, icon("calendar", { size: 24 }), "No election days in this window."),
      days.length && !hasToday && !firstUpcoming ? h("ol", { class: "td-days" }, nowMarker()) : null,
      h(
        "div",
        { class: "td-agenda__more" },
        h("button", { class: "btn btn--sm btn--ghost", type: "button", disabled: !canLater, onclick: () => extendAgenda(0, 365), title: canLater ? "Show one more year" : "The agenda shows at most three years" }, icon("chevronDown", { size: 12 }), "Later"),
      ),
    );
    // Bring today into view inside the agenda (not the page).
    const t = agendaBody.querySelector(".td-day.is-today, .td-now");
    if (t && !S.agendaScrolled) {
      S.agendaScrolled = true;
      agendaBody.scrollTop = Math.max(0, t.offsetTop - agendaBody.offsetTop - 60);
    }
  }

  function agendaRow(d) {
    const codes = d.provinces || [];
    const today = d.when === "today";
    const past = d.when === "past";
    const e = d.election_id ? getElection(d.election_id) : null;
    const status = e?.status || d.status;
    let side;
    if (today) side = h("span", { class: "td-when td-when--today" }, "Today");
    else if (d.planned) side = h("span", { class: "td-when td-when--planned", title: "Created when the clock reaches this date" }, "Planned");
    else {
      const [cls, word] = statusInfo({ status });
      side = h("span", { class: `ep-status ep-status--${cls}` }, cls === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, word);
    }
    const finished = status === "final" || status === "certified";
    const link = d.election_id ? (finished ? h("a", { class: "td-day__go", href: resultsHref(e || { local: d.kind === "local", election_type: d.kind }, d.election_id), title: "Results" }, "Results", icon("chevronRight", { size: 11 })) : h("a", { class: "td-day__go", href: `#/night?e=${d.election_id}`, title: "Election night" }, "Night", icon("chevronRight", { size: 11 }))) : null;
    return h(
      "li",
      { class: ["td-day", `td-day--${d.kind}`, today && "is-today", past && "is-past", d.planned && "is-planned"] },
      dateLeaf(d.date, { size: "sm", year: false }),
      h(
        "div",
        { class: "td-day__main" },
        h("div", { class: "td-day__head" }, h("span", { class: "td-day__name" }, d.kind === "local" ? "Local elections" : d.name), kindTag(d.kind), codes.length ? provinceTags(codes) : null),
        h("div", { class: "td-day__ballot" }, d.races ? h("span", { class: "td-day__races num" }, plural(d.races, "race")) : null, dayBallot(d, { max: 3 })),
      ),
      h("div", { class: "td-day__side" }, side, link),
    );
  }

  async function extendAgenda(before, after) {
    const a = S.agenda;
    if (!a) return;
    S.agendaRange = { start: addDays(a.start, before), end: addDays(a.end, after) };
    await loadAgenda();
  }

  /* ---------------------------------------------------------------- news */
  const isEvent = (n) => n.kind === "vacancy" || n.kind === "recall";
  function paintNews() {
    const nw = S.news;
    if (!nw) return;
    const items = nw.items || [];
    const count = (f) => (f === "" ? items.length : f === "event" ? items.filter(isEvent).length : items.filter((n) => n.kind === f).length);
    mount(
      newsChips,
      NEWS_FILTERS.map(([k, label]) =>
        h(
          "button",
          {
            type: "button",
            class: ["lc-chip", S.newsFilter === k && "is-active", k === "person" && "td-chip--person"],
            "aria-pressed": String(S.newsFilter === k),
            disabled: k !== "" && !count(k),
            onclick: () => {
              S.newsFilter = k;
              S.newsLimit = 30;
              paintNews();
            },
          },
          k === "person" ? icon("star", { size: 11 }) : null,
          label,
          h("span", { class: "lc-chip__n num" }, fmtInt(count(k))),
        ),
      ),
    );
    const list = items.filter((n) => !S.newsFilter || (S.newsFilter === "event" ? isEvent(n) : n.kind === S.newsFilter));
    if (!list.length) {
      mount(newsBody, h("div", { class: "state td-empty" }, icon("news", { size: 22 }), h("span", null, items.length ? "Nothing in this selection." : `No news since ${fmtDate(nw.since, "long")}.`)));
      return;
    }
    const shown = list.slice(0, S.newsLimit);
    const byDate = new Map();
    for (const n of shown) {
      if (!byDate.has(n.date)) byDate.set(n.date, []);
      byDate.get(n.date).push(n);
    }
    mount(
      newsBody,
      [...byDate.entries()].map(([date, ns]) => h("section", { class: "td-newsday" }, h("h3", { class: "td-newsday__date" }, fmtDate(date, "long")), h("ul", { class: "td-news" }, ns.map((n) => newsItem(n))))),
      list.length > shown.length
        ? h(
            "div",
            { class: "td-agenda__more" },
            h(
              "button",
              {
                class: "btn btn--sm",
                type: "button",
                onclick: () => {
                  S.newsLimit += 40;
                  paintNews();
                },
              },
              `Show ${fmtInt(Math.min(40, list.length - shown.length))} more of ${fmtInt(list.length - shown.length)}`,
            ),
          )
        : h("p", { class: "td-newsfoot muted" }, `Since ${fmtDate(nw.since, "long")}.`),
    );
  }

  /* ---------------------------------------------------------------- people */
  function paintPeople() {
    const p = S.people;
    if (!p) return;
    const people = p.people || [];
    peopleMeta.textContent = people.length ? plural(people.length, "person", "people") : "";
    if (p.error) {
      mount(
        peopleBody,
        h(
          "div",
          { class: "card__body" },
          h("div", { class: "notice td-error" }, icon("alert", { size: 16 }), h("span", null, h("b", null, "config/people.yaml does not validate: "), p.error)),
          h("p", { class: "muted td-people__file" }, "File: ", h("code", null, p.file || "config/people.yaml"), " · check it with ", h("code", null, "python -m app people")),
        ),
      );
      return;
    }
    if (!people.length) {
      mount(
        peopleBody,
        h(
          "div",
          { class: "td-people__empty" },
          h("span", { class: "td-people__emptyicon" }, icon("users", { size: 22 })),
          h(
            "p",
            null,
            "Add friends and family to ",
            h("code", null, "config/people.yaml"),
            " (see ",
            h("code", null, "docs/PEOPLE.md"),
            ") — they will run for office where they live.",
          ),
        ),
      );
      return;
    }
    mount(peopleBody, h("ul", { class: "td-people__list" }, people.map(personRow)));
  }

  function personRow(p) {
    const runs = p.runs || [];
    const won = runs.filter((r) => r.won === true).length;
    const lost = runs.filter((r) => r.won === false).length;
    const pending = runs.filter((r) => r.won === null || r.won === undefined).length;
    const color = p.party ? pc(p.party) : "#5b6b85";
    const nameEl = p.candidate_id ? h("a", { class: "td-person__name res-link", href: `#/candidates/${p.candidate_id}` }, p.name) : h("span", { class: "td-person__name" }, p.name);
    const recent = runs.slice(-3).reverse();
    return h(
      "li",
      { class: ["td-person", p.holds?.length && "is-holder"] },
      h("span", { class: "td-person__avatar" }, avatar(p.name, { color, key: p.key, size: 40 })),
      h(
        "div",
        { class: "td-person__main" },
        h(
          "div",
          { class: "td-person__head" },
          nameEl,
          p.party
            ? h("span", { class: ["chip", "td-person__party", !p.party_known && "is-unknown"], style: { "--party": color }, title: p.party_known ? p.party : `${p.party} is not a party of this world (config/parties)` }, h("span", { class: "chip__swatch" }), p.party, !p.party_known ? " ?" : null)
            : h("span", { class: "td-person__np muted" }, "No party"),
        ),
        h(
          "div",
          { class: "td-person__meta" },
          icon("mapPin", { size: 11 }),
          h("span", null, p.home_name || p.home),
          p.province ? h("span", { class: "res-code", title: provinceName(p.province) }, p.province) : null,
          p.born ? h("span", { class: "muted" }, `· born ${p.born.slice(0, 4)}`) : null,
          chanceWord(p.chance) ? h("span", { class: "muted" }, `· runs ${chanceWord(p.chance)}`) : null,
        ),
        p.holds?.length
          ? h("div", { class: "td-person__holds" }, p.holds.map((o) => h("span", { class: "td-office", title: o.office }, icon("trophy", { size: 11 }), o.name)))
          : null,
        h(
          "div",
          { class: "td-person__record" },
          runs.length
            ? [
                h("b", { class: "num" }, plural(runs.length, "race")),
                won ? h("span", { class: "td-out td-out--won" }, `${fmtInt(won)} won`) : null,
                lost ? h("span", { class: "td-out td-out--lost" }, `${fmtInt(lost)} lost`) : null,
                pending ? h("span", { class: "td-out td-out--pending" }, `${fmtInt(pending)} on the ballot`) : null,
              ]
            : h("span", { class: "muted" }, p.offices?.length ? `Has not run yet · only ${p.offices.map((o) => o.replace(/_/g, " ")).join(", ")}` : "Has not run yet"),
        ),
        recent.length
          ? h(
              "ul",
              { class: "td-person__runs" },
              recent.map((r) =>
                h(
                  "li",
                  null,
                  h("span", { class: "num muted" }, fmtDate(r.date)),
                  h("a", { class: "res-link", href: `#/races/${encodeURIComponent(r.race_code)}?e=${r.election_id}` }, r.race),
                  r.won === true
                    ? h("span", { class: "td-out td-out--won" }, icon("check", { size: 10 }), "Won")
                    : r.won === false
                      ? h("span", { class: "td-out td-out--lost" }, icon("x", { size: 10 }), "Lost")
                      : h("span", { class: "td-out td-out--pending", title: typeLabel(r.race_type) }, "On the ballot"),
                ),
              ),
            )
          : null,
      ),
    );
  }

  /* ---------------------------------------------------------------- data */
  /**
   * Fetch one side panel.  The clock reads take a second or two each (more while a job runs), so
   * the card is dimmed while it reloads and a response that a newer request superseded is
   * dropped — stale data never replaces fresh data.
   */
  async function loadPanel(name, url, cardEl, bodyEl, paint, what, timeout = 120000) {
    const seq = ++S.seq[name];
    S.loading[name] = true;
    cardEl.classList.add("td-card--loading");
    cardEl.setAttribute("aria-busy", "true");
    try {
      const data = await api.get(url, { signal: ctrl.signal, timeout });
      if (S.destroyed || seq !== S.seq[name]) return null;
      S[name] = data;
      S.loading[name] = false;
      paint();
      return data;
    } catch (err) {
      if (err?.name === "AbortError" || seq !== S.seq[name]) return null;
      S.loading[name] = false;
      mount(bodyEl, h("div", { class: "state" }, h("strong", null, `Could not load ${what}`), h("span", { class: "muted" }, err.message)));
      return null;
    } finally {
      if (seq === S.seq[name]) {
        cardEl.classList.remove("td-card--loading");
        cardEl.removeAttribute("aria-busy");
      }
    }
  }

  function loadAgenda() {
    const p = new URLSearchParams();
    if (S.agendaRange.start) p.set("start", S.agendaRange.start);
    if (S.agendaRange.end) p.set("end", S.agendaRange.end);
    return loadPanel("agenda", `/api/clock/agenda${p.toString() ? `?${p}` : ""}`, agendaCard, agendaBody, () => {
      paintAgenda();
      paintHero();
      paintSkip();
    }, "the agenda");
  }

  function loadNews() {
    const r = loadPanel("news", "/api/clock/news", newsCard, newsBody, () => {
      paintNews();
      paintHero();
    }, "the news");
    paintHero();
    return r;
  }

  const loadPeople = () => loadPanel("people", "/api/people", peopleCard, peopleBody, paintPeople, "your people", 60000);

  /** Reload agenda, news and people (after the clock moved); resolves with the agenda. */
  function loadSide() {
    S.agendaScrolled = false;
    S.agendaRange = { start: null, end: null };
    const agenda = loadAgenda();
    loadNews();
    loadPeople();
    return agenda;
  }

  /* ---------------------------------------------------------------- wiring */
  const unsubs = [
    subscribe("clock", () => {
      paintHero();
      paintSkip();
    }),
    subscribe("clockBusy", () => {
      paintJob();
      paintHero();
      paintSkip();
    }),
    subscribe("clockJob", () => {
      paintJob();
      paintHero();
      paintSkip();
    }),
    subscribe("meta", () => paintHero()),
    onClockJobDone((job) => {
      if (S.destroyed) return;
      S.lastJob = job;
      paintJob();
      const agenda = loadSide();
      const r = job.result || {};
      // A skip that lands on an election day: the arrival moment (watch or count?).
      if (job.kind === "skip" && job.status === "done" && r.election_id) {
        // meta is fresh here (the clock may still be refreshing): the election brief comes from it.
        const e = getElection(r.election_id);
        if (!e || ["final", "certified"].includes(e.status)) return;
        selectToday(r.election_id);
        const dayPromise = agenda.then((a) => (a?.days || []).find((d) => d.date === r.today) || null);
        openArrival({ label: dayLabel(r.today), date: r.today, election: e, day: null, dayPromise, news: r.news || [], previous: r.previous, counted: r.counted || [] });
      }
    }),
  ];

  paintHero();
  paintJob();
  paintSkip();
  // The clock read at boot (or by the last action) is reused when it is a few seconds old at most.
  if (!getState().clock || !clockIsFresh(8000))
    refreshClock().catch((err) => {
      if (!getState().clock) mount(todayPane, h("div", { class: "state" }, h("strong", null, "Could not read the world clock"), h("span", { class: "muted" }, err?.message || String(err))));
    });
  loadSide();

  return () => {
    S.destroyed = true;
    ctrl.abort();
    unsubs.forEach((u) => u());
  };
}
