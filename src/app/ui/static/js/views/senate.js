/**
 * #/senate — the 24-seat Senate (2 per province, 3 staggered classes of 8; 13 FOR CONTROL):
 * composition before vs after the election (two hemicycles), party table (holdovers, won /
 * called, leading, decided, projected), all 24 seats by province (not up vs contested, holder,
 * current leader, challenger, projected winner, flip / hold), a map of the contested seats and the
 * class I / II / III table.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { partyChip, provBadge } from "../components/badges.js";
import { hemicycle } from "../components/hemicycle.js";
import { cssVar, flipTag, liveRefresh, marginLabel, parties, partyShade, partyTag, pc, raceColor, racePill, raceState, swatchLegend } from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const ROMAN = { 1: "I", 2: "II", 3: "III" };

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const ctrl = new AbortController();
  const frame = pageFrame(el, {
    eyebrow: "Results · Senate",
    title: "Senate",
    categories: ["SIMULATED", "FICTIONAL"],
    electionId: id,
    noticeText: "Holdover seats and the seats up this election are shown; winners appear once reported.",
  });

  let data = null;
  let seatsInfo = null; // /api/senate/seats (next election year per seat)
  let order = [];
  let partyNames = {};
  let map = null;
  let lastSource = null;
  let hemiBefore = null;
  let hemiAfter = null;

  const compCard = liveCard("Composition", { id: "res-s-comp" });
  const controlEl = h("div", { class: "res-control" });
  const beforeHost = h("div", { class: "res-hemi res-hemi--sm" });
  const afterHost = h("div", { class: "res-hemi res-hemi--sm" });
  const beforeCap = h("div", { class: "res-hemi-cap" });
  const afterCap = h("div", { class: "res-hemi-cap" });
  const partyTable = h("div");
  const hemiLegend = h("div", { class: "res-hemi-legend" });
  compCard.body.append(
    controlEl,
    h("div", { class: "res-senate-comp" }, h("div", null, beforeCap, beforeHost), h("div", { class: "res-senate-arrow", "aria-hidden": "true" }, "→"), h("div", null, afterCap, afterHost)),
    hemiLegend,
    h("div", { class: "res-divider" }),
    partyTable,
  );
  const seatsCard = liveCard("All 24 seats", { id: "res-s-seats", categories: [provBadge("FICTIONAL", "Seats FICTIONAL")] });
  const mapCard = liveCard("Seats up this election", { id: "res-s-map", categories: [provBadge("REAL", "Real boundaries")] });
  const mapHost = h("div", { class: "res-map-host" });
  const mapLegend = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapHost, mapLegend);
  const classCard = liveCard("Classes", { id: "res-s-classes", flush: true });

  const seats = () => data?.seats || [];
  const upByProvince = () => {
    const m = {};
    for (const s of seats()) if (s.up) (m[s.province_code] ||= []).push(s);
    return m;
  };

  // ------------------------------------------------------------ composition
  function ordered(obj) {
    const keys = Object.keys(obj || {});
    return [...order.filter((c) => keys.includes(c)), ...keys.filter((c) => !order.includes(c))];
  }
  function paintComposition() {
    const src = data.results_source;
    const cur = data.composition?.current || {};
    const bp = Object.fromEntries((data.by_party || []).map((p) => [p.party, p]));
    const beforeGroups = ordered(cur).map((p) => ({ key: p, label: `${p} · ${cur[p]} before the election`, color: pc(p, bp[p]?.color), seats: cur[p], style: "called" }));
    const afterGroups = [];
    for (const p of ordered(bp)) {
      const x = bp[p];
      if (x.holdover) afterGroups.push({ key: `${p}-h`, label: `${p} · holdover (not up)`, color: pc(p, x.color), seats: x.holdover, style: "notup" });
      const won = src === "final" ? x.won ?? 0 : x.called ?? 0;
      if (won) afterGroups.push({ key: `${p}-w`, label: `${p} · ${src === "final" ? "won" : "called"}`, color: pc(p, x.color), seats: won, style: "called" });
      if (src === "live" && x.leading) afterGroups.push({ key: `${p}-l`, label: `${p} · leading`, color: pc(p, x.color), seats: x.leading, style: "leading" });
    }
    const c = data.control || {};
    const afterCenter = src === "final" ? `${c.controlling_party || c.largest_party || ""} ${c.controlling_party ? "" : c.largest_seats ?? ""}`.trim() : `${data.up} UP`;
    const lbl = data.label || "13 FOR CONTROL";
    if (!hemiBefore) {
      hemiBefore = hemicycle({ total: data.seats_total, majority: data.majority, groups: beforeGroups, centerLabel: "24", centerSub: lbl, tooltip: (g) => g.label });
      mount(beforeHost, hemiBefore);
    } else hemiBefore.update({ groups: beforeGroups, centerLabel: "24", centerSub: lbl });
    if (!hemiAfter) {
      hemiAfter = hemicycle({ total: data.seats_total, majority: data.majority, groups: afterGroups, centerLabel: afterCenter, centerSub: lbl, tooltip: (g) => g.label });
      mount(afterHost, hemiAfter);
    } else hemiAfter.update({ groups: afterGroups, centerLabel: afterCenter, centerSub: lbl });
    mount(beforeCap, h("span", { class: "res-hemi-cap__t" }, "Before the election"), h("span", { class: "muted" }, `${data.not_up} holdovers + ${data.up} seats up (class ${(data.classes_up || []).map((x) => ROMAN[x] || x).join(", ")})`));
    mount(afterCap, h("span", { class: "res-hemi-cap__t" }, src === "final" ? "After the election" : src === "live" ? "Projected after the election" : "After the election"), h("span", { class: "muted" }, src === "hidden" ? "results hidden until reported" : src === "live" ? "holdovers + called + leading" : "holdovers + winners"));
    keyed(hemiLegend, src, () =>
      swatchLegend([
        { kind: "outline", color: cssVar("--text-secondary"), label: "Holdover (not up)" },
        { color: cssVar("--text-secondary"), label: src === "final" ? "Won" : "Called" },
        src === "live" ? { color: partyShade(cssVar("--text-secondary"), 0.3), label: "Leading" } : null,
        { color: cssVar("--uncalled"), label: src === "hidden" ? "Up · not reported" : "Undecided" },
      ].filter(Boolean)),
    );
    keyed(controlEl, JSON.stringify([src, c]), () => {
      if (src === "hidden") return h("div", { class: "res-control__line" }, h("span", { class: "res-control__big" }, lbl), h("span", { class: "muted" }, `${data.up} of ${data.seats_total} seats are up; ${data.not_up} senators are not up this election`));
      if (c.controlling_party) return h("div", { class: "res-control__line" }, h("span", { class: "res-control__big" }, partyChip(c.controlling_party, { color: pc(c.controlling_party) }), src === "live" ? " reaches control" : " controls the Senate"), h("span", { class: "muted" }, c.label || lbl));
      return h("div", { class: "res-control__line" }, h("span", { class: "res-control__big" }, src === "final" ? "No majority" : lbl), h("span", { class: "muted" }, c.label || (src === "live" ? "No party has 13 decided seats yet" : "")));
    });
    const rows = [...(data.by_party || [])].sort((a, b) => (b.total_projected ?? b.total_decided ?? 0) - (a.total_projected ?? a.total_decided ?? 0) || (b.current || 0) - (a.current || 0));
    keyed(partyTable, JSON.stringify([src, rows]), () =>
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "data res-seat-table" },
          h("caption", { class: "sr-only" }, "Senate seats by party"),
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", { scope: "col" }, "Party"),
              h("th", { class: "r", scope: "col" }, "Before"),
              h("th", { class: "r", scope: "col" }, "Holdover"),
              src !== "hidden" ? h("th", { class: "r", scope: "col" }, src === "final" ? "Won" : "Called") : null,
              src === "live" ? h("th", { class: "r", scope: "col" }, "Leading") : null,
              h("th", { class: "r", scope: "col" }, "Decided"),
              src !== "hidden" ? h("th", { class: "r", scope: "col" }, "Projected") : null,
            ),
          ),
          h(
            "tbody",
            null,
            rows.map((p) =>
              h(
                "tr",
                null,
                h("td", null, partyTag(p.party, pc(p.party, p.color), partyNames[p.party])),
                h("td", { class: "r num muted" }, fmtInt(p.current)),
                h("td", { class: "r num" }, fmtInt(p.holdover)),
                src !== "hidden" ? h("td", { class: "r num" }, h("b", null, fmtInt(src === "final" ? p.won : p.called))) : null,
                src === "live" ? h("td", { class: "r num muted" }, fmtInt(p.leading)) : null,
                h("td", { class: "r num" }, h("b", null, fmtInt(p.total_decided))),
                src !== "hidden" ? h("td", { class: "r num" }, fmtInt(p.total_projected)) : null,
              ),
            ),
          ),
        ),
      ),
    );
    compCard.setTitle(`Senate · ${lbl}`);
  }

  // ------------------------------------------------------------ seats by province
  /** Strongest counted candidate other than the shown leader/winner (sorting API votes only). */
  function challengerOf(s, leaderKey) {
    const r = s.race;
    if (!r) return null;
    const lines = [...(r.lines || [])].filter((l) => l.key !== leaderKey);
    if (!lines.some((l) => l.votes)) return null;
    const leaderIsInc = (r.lines || []).find((l) => l.key === leaderKey)?.incumbent;
    // Incumbent leading → the challenger is the best non-incumbent; otherwise show the runner-up.
    const pool = leaderIsInc ? lines.filter((l) => !l.incumbent) : lines;
    pool.sort((a, b) => (b.votes || 0) - (a.votes || 0));
    return pool[0] ? { line: pool[0], label: leaderIsInc ? "Challenger" : "Runner-up" } : null;
  }
  function seatRow(s) {
    const src = data.results_source;
    const nextYear = seatsInfo?.[s.code]?.next_election_year;
    const holder = s.holder;
    if (!s.up)
      return h(
        "div",
        { class: "res-seat res-seat--notup", style: { "--party": pc(s.party) } },
        h("div", { class: "res-seat__head" }, h("span", { class: "res-code" }, s.code), h("span", { class: "res-seat__class" }, `Class ${ROMAN[s.senate_class] || s.senate_class}`), h("span", { class: "res-tag" }, "NOT UP")),
        h("div", { class: "res-seat__holder" }, holder ? partyTag(holder.party, pc(holder.party), holder.name) : h("span", { class: "muted" }, "Vacant")),
        h("div", { class: "res-seat__meta muted" }, holder?.term_end ? `term to ${String(holder.term_end).slice(0, 4)}` : "", nextYear ? ` · next up ${nextYear}` : ""),
      );
    const r = s.race || {};
    const leaderLine = (r.lines || []).find((l) => l.key === (r.winner || r.leader));
    const chx = challengerOf(s, leaderLine?.key);
    const ch = chx?.line;
    return h(
      "div",
      { class: "res-seat res-seat--up", style: { "--party": raceColor(r) || "var(--uncalled)" } },
      h("div", { class: "res-seat__head" }, h("a", { href: links.race(s.code), class: "res-code res-link" }, s.code), h("span", { class: "res-seat__class" }, `Class ${ROMAN[s.senate_class] || s.senate_class}`), s.is_special ? h("span", { class: "res-tag" }, "SPECIAL") : h("span", { class: "res-tag res-tag--up" }, "UP"), h("span", { class: "res-toolbar__spacer" }), racePill(r)),
      h("div", { class: "res-seat__holder" }, h("span", { class: "res-seat__k" }, "Holder"), holder ? partyTag(holder.party, pc(holder.party), holder.name) : h("span", { class: "muted" }, "Vacant"), holder && holder.running === false ? h("span", { class: "res-tag" }, "RETIRING") : null),
      src === "hidden"
        ? h("div", { class: "res-seat__line muted" }, `${(r.lines || []).length} candidates on the ballot`)
        : [
            h("div", { class: "res-seat__line" }, h("span", { class: "res-seat__k" }, r.winner ? "Winner" : "Leader"), leaderLine ? partyTag(leaderLine.party, pc(leaderLine.party, leaderLine.color), leaderLine.name) : h("span", { class: "muted" }, "–"), leaderLine?.pct !== null && leaderLine?.pct !== undefined ? h("b", { class: "num" }, fmtPct(leaderLine.pct)) : null),
            h("div", { class: "res-seat__line" }, h("span", { class: "res-seat__k" }, chx?.label || "Challenger"), ch ? partyTag(ch.party, pc(ch.party, ch.color), ch.name) : h("span", { class: "muted" }, "–"), ch?.pct !== null && ch?.pct !== undefined ? h("span", { class: "num muted" }, fmtPct(ch.pct)) : null),
            h(
              "div",
              { class: "res-seat__line" },
              h("span", { class: "res-seat__k" }, "Projected"),
              s.projected_party ? partyChip(s.projected_party, { color: pc(s.projected_party) }) : h("span", { class: "muted" }, "–"),
              r.margin_pp !== null && r.margin_pp !== undefined ? h("span", { class: "muted" }, "margin ", marginLabel(null, r.margin_pp)) : null,
              s.flip_status ? flipTag(s.flip_status, { prev: s.party }) : null,
            ),
          ],
    );
  }
  function paintSeats() {
    const byProv = {};
    for (const s of seats()) (byProv[s.province_code] ||= { name: s.province_name, seats: [] }).seats.push(s);
    const key = JSON.stringify(seats().map((s) => [s.code, s.race?.status, s.race?.leader, s.race?.winner, s.race?.reporting_pct, s.projected_party, (s.race?.lines || []).map((l) => l.votes)]));
    keyed(seatsCard.body, key, () =>
      h(
        "div",
        { class: "res-seatgrid" },
        Object.entries(byProv).map(([code, p]) =>
          h(
            "div",
            { class: "res-seatgrid__prov" },
            h("div", { class: "res-seatgrid__head" }, h("a", { href: links.province(code), class: "res-link" }, h("span", { class: "res-code" }, code), p.name)),
            p.seats.sort((a, b) => a.seat_number - b.seat_number).map(seatRow),
          ),
        ),
      ),
    );
  }

  // ------------------------------------------------------------ map
  function specFor(f) {
    const up = upByProvince()[f.properties.code];
    if (!up) return { none: true };
    const r = up[0].race || {};
    const col = raceColor(r);
    if (col) return { fill: col, state: raceState(r) };
    return up[0].party ? { fill: pc(up[0].party), state: "leading" } : {};
  }
  function tooltip(f) {
    const code = f.properties.code;
    const all = seats().filter((s) => s.province_code === code);
    return h(
      "div",
      { class: "res-tip" },
      h("div", { class: "res-tip__head" }, h("strong", null, f.properties.name)),
      all.map((s) =>
        h(
          "div",
          { class: "res-tip__row", style: { gridTemplateColumns: "auto 1fr auto" } },
          h("span", { class: "res-code" }, s.code),
          s.up ? h("span", null, s.race ? racePill(s.race) : null, " ", s.race?.leader_name ? `${s.race.winner_name || s.race.leader_name} (${s.race.winner_party || s.race.leader_party})` : `held by ${s.holder?.name || "–"} (${s.party})`) : h("span", { class: "muted" }, `not up · ${s.holder?.name || "vacant"} (${s.party || "–"})`),
          s.flip_status ? flipTag(s.flip_status) : h("span"),
        ),
      ),
    );
  }
  function paintMap() {
    const src = data.results_source;
    const partiesIn = [...new Set(Object.values(upByProvince()).map((u) => u[0].race?.winner_party || u[0].race?.leader_party || (src === "hidden" ? u[0].party : null)).filter(Boolean))];
    mount(
      mapLegend,
      h(
        "div",
        { class: "res-legend-stack" },
        swatchLegend(partiesIn.map((p) => ({ color: src === "hidden" ? partyShade(pc(p), 0.5) : pc(p), label: p })), { title: src === "hidden" ? "Holder of the seat up" : src === "final" ? "Winner" : "Leader" }),
        swatchLegend([{ kind: "none", label: "No seat up" }, src === "live" ? { kind: "close", label: "Too close" } : null].filter(Boolean)),
      ),
    );
    if (map) {
      map.update();
      return;
    }
    map = resMap(mapHost, { layer: "provinces", outline: null, height: 560, label: "Map of the provinces with a Senate seat up this election; the seat list lists the same results", spec: specFor, tooltip, onClick: (f) => (location.hash = links.province(f.properties.code)) });
    map.ready.catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
  }

  // ------------------------------------------------------------ classes
  function paintClasses() {
    const up = new Set(data.classes_up || []);
    const byClass = { 1: [], 2: [], 3: [] };
    for (const s of seats()) (byClass[s.senate_class] ||= []).push(s);
    keyed(classCard.body, JSON.stringify(seats().map((s) => [s.code, s.party, s.projected_party])), () =>
      h(
        "div",
        { class: "res-classes" },
        Object.entries(byClass).map(([k, list]) =>
          h(
            "div",
            { class: ["res-class", up.has(Number(k)) && "is-up"] },
            h("div", { class: "res-class__head" }, h("strong", null, `Class ${ROMAN[k] || k}`), up.has(Number(k)) ? h("span", { class: "res-tag res-tag--up" }, "UP THIS ELECTION") : h("span", { class: "muted" }, list[0] && seatsInfo?.[list[0].code]?.next_election_year ? `next up ${seatsInfo[list[0].code].next_election_year}` : "not up"), h("span", { class: "muted num" }, `${list.length} seats`)),
            h(
              "ul",
              { class: "res-class__list" },
              list.map((s) => h("li", null, h("span", { class: "res-code" }, s.code), s.holder ? partyTag(s.holder.party, pc(s.holder.party), s.holder.name) : h("span", { class: "muted" }, "Vacant"), s.up && s.projected_party && s.projected_party !== s.party ? h("span", { class: "res-class__to" }, "→ ", partyChip(s.projected_party, { color: pc(s.projected_party) })) : null)),
            ),
          ),
        ),
      ),
    );
  }

  function paint() {
    frame.setSource(data);
    paintComposition();
    paintSeats();
    paintMap();
    paintClasses();
  }

  const fetchData = () => api.get(`/api/elections/${id}/senate`, { signal: ctrl.signal });
  try {
    const [d, p, si] = await Promise.all([fetchData(), parties(), api.get("/api/senate/seats", { cache: true }).catch(() => null)]);
    data = d;
    order = p.order;
    partyNames = Object.fromEntries(p.list.map((x) => [x.code, x.name]));
    seatsInfo = Object.fromEntries((si?.seats || []).map((s) => [s.code, s]));
    lastSource = d.results_source;
    frame.setTitle("Senate", `Results · Senate · ${d.election?.name || ""}`);
    mount(frame.body, compCard, h("div", { class: "res-grid res-grid--map" }, mapCard, classCard), seatsCard);
    paint();
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, async () => {
    const d = await fetchData();
    if (d.results_source !== lastSource) {
      lastSource = d.results_source;
      hemiBefore = null;
      hemiAfter = null;
    }
    data = d;
    paint();
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}
