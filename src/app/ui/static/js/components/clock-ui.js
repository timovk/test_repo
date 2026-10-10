/**
 * World-clock UI kit (prefix `td-`): the modal dialog, the arrival dialog ("Election day ·
 * <date> — watch the night or count it instantly?"), the confirm dialog of Skip ahead, news items
 * and the small date block.  Built with h() only (API text is never parsed as HTML).
 *
 * Nothing here decides anything: dates, names, ballots and news are API values.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt } from "../format.js";
import { getElection } from "../store.js";
import { provBadge } from "./badges.js";
import { icon } from "./icons.js";
import { ballotChips, ballotParts, electionProvinces, fmtDate, provinceTags, provincesText } from "./local-kit.js";

/* ------------------------------------------------------------------ links & labels */
/** Results page of an election: Local results (local), President (general), House (midterm). */
export function resultsHref(e, id = e?.id) {
  if (!id) return null;
  const el = e || getElection(id);
  if (!el || el.local || el.election_type === "local") return `#/local?e=${id}`;
  return el.election_type === "general" ? `#/president?e=${id}` : `#/house?e=${id}`;
}

export const KIND_LABEL = { local: "Local", general: "General", midterm: "Midterm" };

/** What a regular election day holds while it is only planned (docs/CLOCK.md). */
export const REGULAR_PLAN = {
  general: "President, the House, a Senate class and the governors",
  midterm: "the House, a Senate class, mayors and municipal councils",
};

/** Kind tag: LOCAL / GENERAL / MIDTERM. */
export function kindTag(kind) {
  const k = String(kind || "").toLowerCase();
  return h("span", { class: `td-kind td-kind--${k || "other"}` }, KIND_LABEL[k] || k || "Election");
}

/** Ballot parts of a day ({kind, counts}) — local kinds or regular offices. */
export function dayParts(day) {
  const local = String(day?.kind || (day?.local ? "local" : "")).toLowerCase() === "local";
  return ballotParts(day?.counts || {}, { local });
}

/** Ballot line of a day: chips when counts are known, the planned offices of a regular day otherwise. */
export function dayBallot(day, { max = 0 } = {}) {
  const parts = dayParts(day);
  if (parts.length) return ballotChips(parts, { max });
  const plan = REGULAR_PLAN[String(day?.kind || "").toLowerCase()];
  return plan ? h("span", { class: "td-plan muted" }, `On the ballot: ${plan}`) : null;
}

const DF_WD = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" });
const DF_MON = new Intl.DateTimeFormat("en-GB", { month: "short", timeZone: "UTC" });

/** Calendar-leaf date block: WED · 8 · NOV 2028. */
export function dateLeaf(iso, { size = "md", year = true } = {}) {
  const d = iso ? new Date(`${String(iso).slice(0, 10)}T00:00:00Z`) : null;
  if (!d || Number.isNaN(d.getTime())) return h("span", { class: `td-leaf td-leaf--${size}` }, "–");
  return h(
    "span",
    { class: `td-leaf td-leaf--${size}`, "aria-hidden": "true" },
    h("span", { class: "td-leaf__wd" }, DF_WD.format(d)),
    h("span", { class: "td-leaf__num num" }, String(d.getUTCDate())),
    h("span", { class: "td-leaf__mon" }, `${DF_MON.format(d)}${year ? ` ${d.getUTCFullYear()}` : ""}`),
  );
}

/** Whole days from `a` to `b` (ISO dates). */
export function daysBetween(a, b) {
  const da = new Date(`${a}T00:00:00Z`);
  const db = new Date(`${b}T00:00:00Z`);
  return Math.round((db - da) / 86400000);
}

const DF_WDLONG = new Intl.DateTimeFormat("en-GB", { weekday: "long", timeZone: "UTC" });

/** "Wednesday 13 June 2029" (the clock's own label format) from an ISO date. */
export function dayLabel(iso) {
  const d = iso ? new Date(`${String(iso).slice(0, 10)}T00:00:00Z`) : null;
  return d && !Number.isNaN(d.getTime()) ? `${DF_WDLONG.format(d)} ${fmtDate(iso, "long")}` : iso || "";
}

export function inDays(n) {
  if (n === 0) return "today";
  if (n === 1) return "tomorrow";
  if (n < 0) return `${fmtInt(-n)} days ago`;
  if (n < 60) return `in ${fmtInt(n)} days`;
  const m = Math.round(n / 30.4);
  return m < 24 ? `in ${m} months` : `in ${Math.round(n / 365)} years`;
}

/* ------------------------------------------------------------------ news */
const NEWS_KIND = {
  result: ["ballot", "Result"],
  person: ["star", "Your people"],
  vacancy: ["userMinus", "Vacancy"],
  recall: ["alert", "Recall petition"],
};

/** One news item.  `person` items (the user's own people) are highlighted with a star. */
export function newsItem(item, { showDate = false } = {}) {
  const [ico, label] = NEWS_KIND[item.kind] || ["news", item.kind || "News"];
  let link = null;
  if (item.kind === "person" && item.candidate_id) link = h("a", { class: "td-news__link", href: `#/candidates/${item.candidate_id}` }, "Candidate page", icon("chevronRight", { size: 11 }));
  else if (item.election_id) link = h("a", { class: "td-news__link", href: resultsHref(null, item.election_id) }, "Results", icon("chevronRight", { size: 11 }));
  const outcome =
    item.kind === "person" && item.won !== undefined && item.won !== null
      ? h("span", { class: ["td-out", item.won ? "td-out--won" : "td-out--lost"] }, icon(item.won ? "check" : "x", { size: 10 }), item.won ? "Won" : "Lost")
      : null;
  return h(
    "li",
    { class: ["td-news__item", `td-news__item--${item.kind}`] },
    h("span", { class: "td-news__icon", title: label }, icon(ico, { size: 13 })),
    h(
      "div",
      { class: "td-news__body" },
      h("p", { class: "td-news__text" }, item.text),
      h(
        "div",
        { class: "td-news__meta" },
        h("span", { class: "td-news__kind" }, label),
        outcome,
        showDate && item.date ? h("span", { class: "num" }, fmtDate(item.date)) : null,
        item.election_date ? h("span", null, `Election ${fmtDate(item.election_date)}`) : null,
        link,
      ),
    ),
  );
}

/* ------------------------------------------------------------------ modal */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Modal dialog: backdrop, focus trap, Esc / backdrop click close it (unless not dismissible).
 * Returns {panel, close(reason), setDismissible(bool)}; `onClose(reason)` fires once.
 */
export function openModal({ label, content, className, onClose, dismissible = true }) {
  const prevFocus = document.activeElement;
  let canDismiss = dismissible;
  let closed = false;
  const panel = h("div", { class: ["td-modal__panel", className], role: "dialog", "aria-modal": "true", "aria-label": label, tabindex: "-1" }, content);
  const backdrop = h("div", { class: "td-modal" }, panel);
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop && canDismiss) close("backdrop");
  });
  function onKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      if (canDismiss) close("escape");
    } else if (e.key === "Tab") {
      const items = [...panel.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
  function close(reason) {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    backdrop.classList.add("is-leaving");
    document.documentElement.classList.remove("td-modal-open");
    setTimeout(() => backdrop.remove(), 180);
    if (prevFocus && typeof prevFocus.focus === "function" && document.contains(prevFocus)) prevFocus.focus();
    onClose?.(reason);
  }
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(backdrop);
  document.documentElement.classList.add("td-modal-open");
  requestAnimationFrame(() => (panel.querySelector("[data-autofocus]") || panel).focus());
  return {
    panel,
    close,
    get closed() {
      return closed;
    },
    setDismissible(v) {
      canDismiss = !!v;
    },
  };
}

/** Confirm dialog → Promise<boolean>. */
export function confirmDialog({ title, body, confirmLabel = "Continue", cancelLabel = "Cancel", iconName = "alert" }) {
  return new Promise((resolve) => {
    let answer = false;
    const ok = h(
      "button",
      {
        class: "btn btn--primary td-btn",
        type: "button",
        "data-autofocus": true,
        onclick: () => {
          answer = true;
          m.close("ok");
        },
      },
      confirmLabel,
    );
    const m = openModal({
      label: title,
      className: "td-confirm",
      onClose: () => resolve(answer),
      content: [
        h("div", { class: "td-dialog__head" }, h("span", { class: "td-dialog__icon td-dialog__icon--warn" }, icon(iconName, { size: 20 })), h("h2", { class: "td-dialog__title" }, title)),
        h("div", { class: "td-dialog__body" }, body),
        h("div", { class: "td-dialog__actions" }, h("button", { class: "btn td-btn", type: "button", onclick: () => m.close("cancel") }, cancelLabel), ok),
      ],
    });
  });
}

/* ------------------------------------------------------------------ arrival dialog */
/**
 * "Election day · Wednesday 7 February 2029": the election (name, provinces, what is on the
 * ballot), the news since the previous day (the user's own people highlighted) and the choice —
 * Watch the night / Count instantly / Decide later.
 *
 * `election`: brief with `provinces`; `day`: {kind, counts, races} when known, or `dayPromise`
 * resolving to it (else — or when it resolves to null — fetched from the agenda); `news`: items
 * oldest first; `previous`: ISO date; `counted`: ids counted on the way (skip).  `onWatch()` / `onCount()` return promises; the dialog stays open (busy) until they
 * settle and shows their error inline.
 */
export function arrivalDialog({ label, date, election, day, dayPromise, news = [], previous, counted, onWatch, onCount, onLater }) {
  const codes = electionProvinces(election);
  const local = !!election?.local;
  const ballotHost = h("div", { class: "td-arrive__ballot" }, day ? dayBallot(day) : h("span", { class: "skeleton td-arrive__skel" }));
  const racesHost = h("span", { class: "td-arrive__races num" }, day?.races ? `${fmtInt(day.races)} races` : "");
  const errorHost = h("div", { class: "td-arrive__error", role: "alert" });
  const busyHost = h("div", { class: "td-arrive__busy", role: "status", "aria-live": "polite" });
  const person = news.filter((n) => n.kind === "person");
  // What happened since the previous day, in date order (the API's order).  A long skip keeps
  // every item about the user's own people plus the most recent others.
  const NEWS_MAX = 40;
  const room = Math.max(0, NEWS_MAX - person.length);
  const shownNews = news.length > NEWS_MAX ? news.filter((n, i) => n.kind === "person" || i >= news.length - room) : news;

  const watchBtn = h("button", { class: "btn btn--primary td-btn td-btn--lg", type: "button", "data-autofocus": true }, icon("eye", { size: 16 }), "Watch the night");
  const countBtn = h("button", { class: "btn td-btn td-btn--lg td-btn--count", type: "button" }, icon("zap", { size: 16 }), "Count instantly");
  const laterBtn = h("button", { class: "btn btn--ghost td-btn", type: "button" }, "Decide later");

  const m = openModal({
    label: `Election day ${label || ""}`,
    className: "td-arrive",
    onClose: (reason) => {
      if (reason !== "watch" && reason !== "count") onLater?.();
    },
    content: [
      h(
        "header",
        { class: "td-arrive__head" },
        dateLeaf(date, { size: "lg" }),
        h(
          "div",
          { class: "td-arrive__titles" },
          h("div", { class: "td-arrive__kicker" }, h("span", { class: "td-pulse", "aria-hidden": "true" }), counted?.length ? "Skipped ahead · election day" : "Election day"),
          h("h2", { class: "td-arrive__label" }, label || fmtDate(date, "long")),
          h("div", { class: "td-arrive__name" }, election?.name || "", provBadge("FICTIONAL")),
        ),
        h("button", { class: "btn btn--ghost btn--sm td-arrive__x", type: "button", "aria-label": "Decide later (close)", onclick: () => m.close("later") }, icon("x", { size: 14 })),
      ),
      h(
        "div",
        { class: "td-arrive__body" },
        local && codes.length
          ? h("div", { class: "td-arrive__provs" }, provinceTags(codes), h("span", null, `${provincesText(codes, { names: true })} vote${codes.length === 1 ? "s" : ""} today`), provBadge("REAL", "Real provinces"))
          : null,
        h("div", { class: "td-arrive__ballotrow" }, racesHost, ballotHost),
        counted?.length
          ? h("p", { class: "td-arrive__counted" }, icon("fastForward", { size: 13 }), `${fmtInt(counted.length)} election${counted.length === 1 ? "" : "s"} counted instantly on the way${previous ? ` from ${fmtDate(previous, "long")}` : ""}.`)
          : null,
        h(
          "section",
          { class: "td-arrive__news" },
          h(
            "h3",
            { class: "td-arrive__newshead" },
            icon("news", { size: 13 }),
            previous ? `Since ${fmtDate(previous, "long")}` : "News",
            h("span", { class: "muted num" }, ` · ${fmtInt(news.length)} item${news.length === 1 ? "" : "s"}`),
            person.length ? h("span", { class: "td-arrive__mine" }, icon("star", { size: 11 }), `${fmtInt(person.length)} about your people`) : null,
          ),
          news.length
            ? h("ul", { class: "td-news td-news--compact" }, shownNews.map((n) => newsItem(n, { showDate: true })))
            : h("p", { class: "muted td-arrive__quiet" }, "A quiet stretch: no vacancies, recalls or results in between."),
          news.length > shownNews.length ? h("p", { class: "muted td-arrive__more" }, `${fmtInt(news.length - shownNews.length)} earlier items are on the Today page (News).`) : null,
        ),
        errorHost,
        busyHost,
      ),
      h(
        "footer",
        { class: "td-arrive__foot" },
        h("p", { class: "td-arrive__hint" }, h("b", null, "Watch"), " simulates the hidden result and opens the election night at polls closing. ", h("b", null, "Count instantly"), " runs the whole night at once — every call is stored, so the results look the same."),
        h("div", { class: "td-dialog__actions" }, laterBtn, countBtn, watchBtn),
      ),
    ],
  });

  function busy(which) {
    const on = !!which;
    m.setDismissible(!on);
    watchBtn.disabled = on;
    countBtn.disabled = on;
    laterBtn.disabled = on;
    watchBtn.classList.toggle("is-busy", which === "watch");
    countBtn.classList.toggle("is-busy", which === "count");
    mount(watchBtn, which === "watch" ? [h("span", { class: "lc-spinner lc-spinner--ink", "aria-hidden": "true" }), "Simulating the election…"] : [icon("eye", { size: 16 }), "Watch the night"]);
    mount(countBtn, which === "count" ? [h("span", { class: "lc-spinner", "aria-hidden": "true" }), "Starting the count…"] : [icon("zap", { size: 16 }), "Count instantly"]);
    mount(
      busyHost,
      which === "watch"
        ? h("div", { class: "notice td-busynote" }, h("span", { class: "lc-spinner", "aria-hidden": "true" }), h("span", null, "Simulating the hidden result and preparing the election night at polls closing — this can take up to a minute on the real geography. The night starts by itself."))
        : null,
    );
  }

  async function run(which, fn) {
    mount(errorHost);
    busy(which);
    try {
      await fn?.();
      busy(null);
      m.close(which);
    } catch (err) {
      busy(null);
      mount(errorHost, h("div", { class: "notice td-error" }, icon("alert", { size: 16 }), h("span", null, err?.message || String(err))));
    }
  }
  watchBtn.addEventListener("click", () => run("watch", onWatch));
  countBtn.addEventListener("click", () => run("count", onCount));
  laterBtn.addEventListener("click", () => m.close("later"));

  // The day's ballot (race counts) when the caller did not have it: from the caller's pending
  // agenda request, else ask the agenda for that one date.
  const fill = (d) => {
    if (m.closed) return;
    mount(ballotHost, d ? dayBallot(d) : null);
    if (d?.races) racesHost.textContent = `${fmtInt(d.races)} races`;
  };
  const fetchDay = () =>
    api
      .get(`/api/clock/agenda?start=${date}&end=${date}`, { timeout: 60000 })
      .then((a) => fill((a.days || []).find((x) => x.date === date) || null))
      .catch(() => fill(null));
  if (!day && date) {
    if (dayPromise) Promise.resolve(dayPromise).then((d) => (d ? fill(d) : fetchDay()), fetchDay);
    else fetchDay();
  }
  return m;
}
