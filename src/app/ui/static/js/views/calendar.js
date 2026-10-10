/**
 * #/calendar — Local election calendar: the local election days from GET /api/local/calendar.
 * Every province holds four local days a year; all provinces voting on a date form ONE local
 * election with one combined election night ("Local Elections · 7 February 2029").
 *
 * Default range: 12 months after the latest reported election (the API default).  The user steps
 * by month or year (12-month windows) and filters by province (dates on which it votes); the
 * window and province are kept in the URL (?start=YYYY-MM-01&province=GE).  Each date shows the
 * provinces voting with their own race counts (province_counts), the race counts by kind, the
 * municipalities on the ballot, what is on the ballot (grouped by province) and a link to the
 * stored election (or "not created yet" — the world clock creates it when it reaches the date).
 * The schedule and every contest are FICTIONAL; provinces and municipalities are REAL.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt } from "../format.js";
import { provBadge } from "../components/badges.js";
import { icon } from "../components/icons.js";
import { statusInfo } from "../components/election-picker.js";
import { LOCAL_KINDS, ballotChips, ballotParts, fmtDate, loadProvinceNames, parseDate, provinceName, provincesText } from "../components/local-kit.js";
import { tile } from "../components/res-kit.js";
import { setQuery } from "../router.js";
import { getElection } from "../store.js";
import { errorState, pageHeader } from "./_shared.js";

const KINDS = LOCAL_KINDS;
const KIND_LABEL = Object.fromEntries(KINDS.map(([k, one]) => [k, one]));
const KIND_SHORT = Object.fromEntries(KINDS.map(([k, , , short]) => [k, short]));
const ORD = ["", "1st", "2nd", "3rd", "4th", "5th"];
const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const iso = (d) => d.toISOString().slice(0, 10);
const monthStart = (d) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
const addMonths = (d, n) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + n, 1));
const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

export async function render(el, params, ctx) {
  const ctrl = new AbortController();
  const q = ctx?.query || {};
  const st = {
    start: /^\d{4}-\d{2}-\d{2}$/.test(q.start || "") ? q.start : null,
    province: /^[A-Z]{2}$/.test(q.province || "") ? q.province : "",
    data: null,
  };
  const names = await loadProvinceNames();

  const provSel = h(
    "select",
    {
      class: "select",
      "aria-label": "Province",
      onchange: (e) => {
        st.province = e.target.value;
        setQuery({ province: st.province || null });
        load();
      },
    },
    h("option", { value: "" }, "All provinces"),
    [...names.entries()].sort((a, b) => a[1].localeCompare(b[1], "nl")).map(([code, name]) => h("option", { value: code, selected: code === st.province }, name)),
  );
  const rangeLabel = h("span", { class: "lc-range__label num", "aria-live": "polite" }, "…");
  const navBtn = (iconName, title, delta) => h("button", { class: "btn btn--sm lc-range__btn", type: "button", title, "aria-label": title, onclick: () => step(delta) }, icon(iconName, { size: 14 }));
  const toolbar = h(
    "div",
    { class: "res-toolbar lc-range", role: "toolbar", "aria-label": "Calendar range" },
    h(
      "div",
      { class: "lc-range__nav" },
      navBtn("chevronsLeft", "Previous year", -12),
      navBtn("chevronLeft", "Previous month", -1),
      rangeLabel,
      navBtn("chevronRight", "Next month", 1),
      navBtn("chevronsRight", "Next year", 12),
    ),
    h(
      "button",
      {
        class: "btn btn--sm btn--ghost",
        type: "button",
        title: "The 12 months after the latest reported election",
        onclick: () => {
          st.start = null;
          setQuery({ start: null });
          load();
        },
      },
      "Upcoming",
    ),
    h("span", { class: "res-toolbar__spacer" }),
    h("label", { class: "res-field" }, h("span", { class: "res-field__label" }, "Province"), provSel),
  );
  const tiles = h("div", { class: "res-tiles lc-tiles" });
  const rules = h("div");
  const months = h("div", { class: "lc-months" }, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "40%", height: "14px" } })));
  mount(
    el,
    pageHeader({ eyebrow: "Local elections · calendar", title: "Local election calendar", categories: ["FICTIONAL"], meta: [h("a", { class: "btn btn--sm", href: "#/local" }, icon("ballot", { size: 13 }), "Local results")] }),
    h(
      "div",
      { class: "notice lc-provnote lc-provnote--top" },
      h(
        "span",
        null,
        provBadge("FICTIONAL"),
        " Between the November elections every province holds four local election days a year: school boards, water boards, ballot measures, special elections and mayor recalls. All provinces voting on the same date share one local election with one combined election night. The schedule and every contest are fictional; ",
        provBadge("REAL"),
        " provinces and municipalities are real (CBS).",
      ),
    ),
    toolbar,
    tiles,
    months,
    rules,
  );

  function step(delta) {
    const base = monthStart(parseDate(st.data?.start || st.start) || new Date());
    st.start = iso(addMonths(base, delta));
    setQuery({ start: st.start });
    load();
  }

  async function load() {
    const p = new URLSearchParams();
    if (st.start) {
      const s = parseDate(st.start);
      p.set("start", st.start);
      p.set("end", iso(new Date(addMonths(s, 12).getTime() - 86400000)));
    }
    if (st.province) p.set("province", st.province);
    rangeLabel.textContent = "…";
    try {
      const d = await api.get(`/api/local/calendar${p.toString() ? `?${p}` : ""}`, { signal: ctrl.signal });
      st.data = d;
      paint();
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error(err);
      mount(months, errorState(err));
    }
  }

  function paint() {
    const d = st.data;
    const days = d.days || [];
    rangeLabel.textContent = `${fmtDate(d.start)} – ${fmtDate(d.end)}`;
    // summary
    const totals = Object.fromEntries(KINDS.map(([k]) => [k, 0]));
    let races = 0;
    let stored = 0;
    let reported = 0;
    for (const day of days) {
      races += day.races || 0;
      for (const [k, v] of Object.entries(day.counts || {})) totals[k] = (totals[k] || 0) + v;
      if (day.election_id) stored += 1;
      if (day.status === "final" || day.status === "certified") reported += 1;
    }
    const provs = new Set(days.flatMap((x) => x.provinces || []));
    const own = st.province ? days.reduce((a, x) => a + Object.values(x.province_counts?.[st.province] || {}).reduce((b, n) => b + n, 0), 0) : 0;
    mount(
      tiles,
      tile("Local election days", fmtInt(days.length), st.province ? `on which ${provinceName(st.province)} votes` : `${plural(provs.size, "province", "provinces")} · one night per date`),
      tile("Races on the ballot", fmtInt(races), st.province ? `${plural(own, "contest", "contests")} in ${provinceName(st.province)} itself` : KINDS.filter(([k]) => totals[k]).map(([k, one, many]) => plural(totals[k], one, many)).slice(0, 3).join(" · ") || "–"),
      tile("Stored elections", fmtInt(stored), `${fmtInt(reported)} final · ${fmtInt(stored - reported)} not reported`),
      tile("Not created yet", fmtInt(days.length - stored), "planned days without a stored election"),
    );
    // months
    if (!days.length) {
      mount(months, h("div", { class: "card" }, h("div", { class: "state" }, icon("calendar", { size: 26 }), h("strong", null, st.province ? `${provinceName(st.province)} does not vote in this window` : "No local election days in this window"), h("span", { class: "muted" }, "Local days fall in February–June and September."))));
    } else {
      const byMonth = new Map();
      for (const day of [...days].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))) {
        const k = day.date.slice(0, 7);
        if (!byMonth.has(k)) byMonth.set(k, []);
        byMonth.get(k).push(day);
      }
      mount(
        months,
        [...byMonth.entries()].map(([k, list]) =>
          h(
            "section",
            { class: "lc-month", "aria-label": fmtDate(`${k}-01`, "month") },
            h(
              "header",
              { class: "lc-month__head" },
              h("h2", { class: "lc-month__title" }, fmtDate(`${k}-01`, "month")),
              h("span", { class: "muted num" }, `${plural(list.length, "local election day", "local election days")} · ${plural(list.reduce((a, x) => a + (x.races || 0), 0), "race", "races")}`),
            ),
            h("div", { class: "lc-days" }, list.map(dayCard)),
          ),
        ),
      );
    }
    // fixed days per province
    const weekday = days[0] ? fmtDate(days[0].date, "weekday") : null;
    const pd = Object.entries(d.province_days || {}).filter(([code]) => !st.province || code === st.province);
    pd.sort((a, b) => provinceName(a[0]).localeCompare(provinceName(b[0]), "nl"));
    mount(
      rules,
      pd.length
        ? h(
            "section",
            { class: "card lc-rules" },
            h("div", { class: "card__head" }, h("h2", { class: "card__title" }, "Fixed local election days"), h("div", { class: "page-head__meta" }, provBadge("FICTIONAL"))),
            h(
              "div",
              { class: "card__body" },
              h(
                "div",
                { class: "lc-rules__grid" },
                pd.map(([code, list]) =>
                  h(
                    "div",
                    { class: "lc-rules__row" },
                    h("span", { class: "lc-rules__prov" }, provinceName(code), h("span", { class: "res-code" }, code)),
                    h(
                      "span",
                      { class: "lc-rules__days" },
                      [...list].sort((a, b) => a.month - b.month).map((x) => h("span", { class: "lc-rules__day", title: `${ORD[x.occurrence] || `${x.occurrence}th`} ${weekday || "election day"} of ${MONTHS[x.month]}` }, h("b", null, MONTHS[x.month]), ` ${ORD[x.occurrence] || x.occurrence}${weekday ? ` ${weekday}` : ""}`)),
                    ),
                  ),
                ),
              ),
            ),
            h("div", { class: "card__foot" }, "Every year each province holds its local elections on the same fixed days (the nth weekday of the month). Each municipality has its own day among them."),
          )
        : null,
    );
  }

  function dayCard(day) {
    const date = day.date;
    const e = day.election_id ? getElection(day.election_id) : null;
    const status = day.status || e?.status || null;
    const [cls, word] = status ? statusInfo({ status }) : ["none", "Not created yet"];
    const codes = day.provinces || [];
    const munis = (day.municipalities || []).length;
    const byProv = new Map(codes.map((c) => [c, []]));
    for (const c of day.contests || []) {
      const k = c.province_code || "";
      if (!byProv.has(k)) byProv.set(k, []);
      byProv.get(k).push(c);
    }
    const contest = (c) =>
      h(
        "li",
        null,
        h("span", { class: "lc-kind lc-kind--mini", title: KIND_LABEL[c.kind] || c.kind }, KIND_SHORT[c.kind] || c.kind),
        day.election_id ? h("a", { class: "res-link", href: `#/races/${encodeURIComponent(c.code)}?e=${day.election_id}` }, c.name) : h("span", null, c.name),
      );
    return h(
      "article",
      { class: ["lc-day", `lc-day--${cls}`] },
      h(
        "div",
        { class: "lc-day__date", "aria-hidden": "true" },
        h("span", { class: "lc-day__wd" }, fmtDate(date, "weekday")),
        h("span", { class: "lc-day__num num" }, String(Number(date.slice(8, 10)))),
        h("span", { class: "lc-day__mon" }, fmtDate(date, "mon")),
      ),
      h(
        "div",
        { class: "lc-day__main" },
        h(
          "div",
          { class: "lc-day__head" },
          h("span", { class: "lc-day__prov" }, day.name || `Local Elections · ${fmtDate(date, "long")}`),
          status ? h("span", { class: `ep-status ep-status--${cls}` }, cls === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, word) : null,
        ),
        h(
          "div",
          { class: "lc-day__meta" },
          h("span", { class: "sr-only" }, `${fmtDate(date, "long")} · `),
          `${plural(day.races || 0, "race", "races")} · ${plural(munis, "municipality", "municipalities")} · ${codes.length > 1 ? `${fmtInt(codes.length)} provinces, one combined night` : plural(codes.length, "province", "provinces")}`,
        ),
        ballotChips(ballotParts(day.counts || {})),
        codes.length
          ? h(
              "ul",
              { class: "lc-day__provs", "aria-label": `Provinces voting: ${provincesText(codes, { names: true })}` },
              codes.map((code) => {
                const pc = day.province_counts?.[code] || {};
                const n = Object.values(pc).reduce((a, x) => a + x, 0);
                return h(
                  "li",
                  { class: ["lc-day__pv", st.province === code && "is-focus"] },
                  h("span", { class: "lc-provtag" }, code),
                  h("span", { class: "lc-day__pvname" }, provinceName(code)),
                  h(
                    "span",
                    { class: "lc-day__pvcounts muted" },
                    plural(n, "contest", "contests"),
                    n ? ": " : "",
                    KINDS.filter(([k]) => pc[k])
                      .map(([k, one, many]) => plural(pc[k], one, many))
                      .join(" · "),
                  ),
                );
              }),
            )
          : null,
        (day.contests || []).length
          ? h(
              "details",
              { class: "lc-day__ballot" },
              h("summary", null, "What's on the ballot"),
              [...byProv.entries()]
                .filter(([, list]) => list.length)
                .map(([code, list]) =>
                  h(
                    "div",
                    { class: "lc-day__group" },
                    byProv.size > 1 ? h("div", { class: "lc-day__grouphead" }, code ? [h("span", { class: "lc-provtag" }, code), provinceName(code)] : "Other contests") : null,
                    h("ul", { class: "lc-day__contests" }, list.map(contest)),
                  ),
                ),
            )
          : null,
      ),
      h(
        "div",
        { class: "lc-day__side" },
        day.election_id
          ? [
              h("a", { class: "btn btn--sm btn--primary", href: `#/local?e=${day.election_id}` }, "Results", icon("chevronRight", { size: 12 })),
              status !== "final" && status !== "certified" ? h("a", { class: "btn btn--sm btn--ghost", href: `#/night?e=${day.election_id}` }, icon("night", { size: 12 }), "Night") : null,
            ]
          : [
              h("span", { class: "lc-day__nc", title: "Created when the world clock reaches this date (the Today page), or with `python -m app local create`" }, "Not created yet"),
              h("a", { class: "btn btn--sm btn--ghost lc-day__today", href: "#/today", title: "The world clock moves from election day to election day" }, icon("clock", { size: 12 }), "Today"),
            ],
      ),
    );
  }

  load();
  return () => ctrl.abort();
}
