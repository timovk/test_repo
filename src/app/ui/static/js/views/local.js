/**
 * #/local — Local results: every race on one local election's ballot, modelled on a U.S. results
 * listing ("October 6th, 2026 Alaska Election Results … for 48 races").
 *
 *   title = election name · subtitle "N races in <province> · <status>" · summary tiles ·
 *   filter chips (All / School boards / Measures / Water boards / Specials & recalls, with counts)
 *   · client-side search · municipality filter · a two-column grid of race rows linking to
 *   #/races/{code}?e={id}.
 *
 * Data: GET /api/elections/{id}/races (refetched while the night runs).  A regular election works
 * the same (one chip per race type).  Results follow the hidden / live / final rule; nothing is
 * computed here except counting API flags (decided, passed / passing).
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { provBadge } from "../components/badges.js";
import { icon } from "../components/icons.js";
import { fmtDate, provinceName, raceBrowser, tally, loadProvinceNames } from "../components/local-kit.js";
import { liveRefresh, tile } from "../components/res-kit.js";
import { pageFrame } from "../components/res-page.js";
import { getElection, getState } from "../store.js";
import { currentElectionId } from "./_shared.js";

const SOURCE_TEXT = {
  final: "Final results",
  live: "Counting live",
  hidden: "Results hidden until election night",
};

export async function render(el) {
  const id = currentElectionId();
  const election = getElection(id);
  const local = !!election?.local;
  const ctrl = new AbortController();
  await loadProvinceNames();
  const prov = local ? provinceName(election.province_code, election) : null;

  const frame = pageFrame(el, {
    eyebrow: local ? `Local results · ${prov} · ${fmtDate(election.election_date, "long")}` : "Results · every race",
    title: election?.name || "Local results",
    categories: ["FICTIONAL", "SIMULATED"],
    electionId: id,
    noticeText: "Races, candidates, ballot questions and thresholds are shown.",
  });
  frame.header.classList.add("lc-head");
  const sub = h("p", { class: "lc-sub" });
  frame.header.firstChild.append(sub);

  const tiles = h("div", { class: "res-tiles lc-tiles" });
  const countNote = h("span", { class: "lc-count muted" });
  const browser = raceBrowser({
    electionId: id,
    local,
    onChange: ({ shown, matching, total }) => {
      countNote.textContent = matching === total ? `${fmtInt(total)} races` : `${fmtInt(matching)} of ${fmtInt(total)} races`;
      void shown;
    },
  });
  const listCard = h(
    "section",
    { class: "card lc-card" },
    h(
      "div",
      { class: "card__head" },
      h("h2", { class: "card__title" }, local ? "Races on the ballot" : "All races"),
      h("div", { class: "page-head__meta" }, countNote, h("a", { class: "btn btn--sm btn--ghost", href: `#/calendar` }, icon("calendar", { size: 13 }), "Local calendar")),
    ),
    h("div", { class: "card__body lc-card__body" }, browser.el),
  );
  const notes = h(
    "div",
    { class: "notice lc-provnote" },
    h(
      "span",
      null,
      provBadge("REAL"),
      " Provinces, municipalities and water authorities are real (CBS / PDOK). ",
      provBadge("FICTIONAL"),
      " The local election calendar, every contest, candidate and ballot question — and all results — are fictional simulations.",
    ),
  );
  const regularNote = local
    ? null
    : h(
        "div",
        { class: "notice lc-regnote" },
        icon("ballot", { size: 16 }),
        h(
          "span",
          null,
          h("strong", null, `${election?.name || "This election"} is a regular election. `),
          "Every race is listed below. Local elections (school boards, measures, specials and recalls) can be chosen in the election picker at the top, or from the ",
          h("a", { href: "#/calendar", class: "res-link" }, "local calendar"),
          ".",
        ),
      );

  let data = null;

  function paintHead() {
    const src = data.results_source;
    const t = tally(data.races || [], src);
    const where = local ? `in ${prov}` : "nationwide";
    keyed(sub, JSON.stringify([data.count, src, where]), () => [
      h("b", null, `${fmtInt(data.count ?? t.total)} races`),
      ` ${where} · `,
      h("span", { class: `lc-sub__src lc-sub__src--${src}` }, src === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, SOURCE_TEXT[src] || src),
      local && t.municipalityCount ? h("span", { class: "muted" }, ` · ${fmtInt(t.municipalityCount)} municipalities`) : null,
    ]);
    const hidden = src === "hidden";
    const night = getState().night;
    const rep = night && Number(night.election_id) === Number(id) ? night.snapshot?.reporting : null;
    const items = [
      tile("Races decided", hidden ? "–" : `${fmtInt(t.decided)}/${fmtInt(t.total)}`, hidden ? "results hidden until election night" : src === "final" ? "every race certified" : `${fmtInt(t.counting)} with votes counted`),
      t.measures
        ? tile(src === "final" ? "Measures passed" : "Measures passing", hidden ? "–" : `${fmtInt(t.measuresYes)}/${fmtInt(t.measures)}`, hidden ? `${fmtInt(t.measures)} ballot questions` : `${fmtInt(t.measuresNo)} ${src === "final" ? "failed" : "failing"}`, { accent: "var(--yes)" })
        : null,
      t.boards ? tile("Board seats", hidden ? `${fmtInt(t.seatsUp)}` : `${fmtInt(t.seatsFilled)}/${fmtInt(t.seatsUp)}`, hidden ? `up in ${fmtInt(t.boards)} board elections` : `filled in ${fmtInt(t.boards)} board elections`) : null,
      t.specials || t.recalls ? tile("Specials & recalls", fmtInt(t.specials + t.recalls), [t.recalls ? `${fmtInt(t.recalls)} recall${t.recalls === 1 ? "" : "s"}` : null, t.specials ? `${fmtInt(t.specials)} special election${t.specials === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ")) : null,
      src === "live" && rep
        ? tile("Reporting", fmtPct(rep.pct_expected_ballots), `${fmtInt(rep.municipalities_reporting)} of ${fmtInt(rep.municipalities_total)} municipalities`)
        : local || t.municipalityCount
          ? tile("Municipalities", fmtInt(t.municipalityCount), local ? `with a contest in ${prov}` : "with a municipal race")
          : null,
    ].filter(Boolean);
    keyed(tiles, JSON.stringify([src, t.decided, t.total, t.measuresYes, t.measuresNo, t.seatsFilled, rep?.pct_expected_ballots]), () => items);
  }

  function paint() {
    frame.setSource(data);
    paintHead();
    browser.update(data);
  }

  const fetchData = () => api.get(`/api/elections/${id}/races`, { signal: ctrl.signal });
  try {
    data = await fetchData();
    mount(frame.body, regularNote, tiles, listCard, notes);
    paint();
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, async () => {
    data = await fetchData();
    paint();
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
  };
}
