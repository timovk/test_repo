/**
 * #/house — the 150-seat House: national seat counter by party (called / leading / total /
 * previous / net change, 76 FOR CONTROL) with a hemicycle, a map of the 150 FICTIONAL districts
 * (control, leader, called, flips, open seats, incumbents, margins), flips / closest lists and
 * a sortable district table.  Live elections refresh from the night at a modest cadence.
 */
import { api } from "../api.js";
import { h, keyed, mount } from "../dom.js";
import { fmtInt, fmtPct, fmtProb, fmtSigned } from "../format.js";
import { setQuery } from "../router.js";
import { partyChip, provBadge, statusPill } from "../components/badges.js";
import { hemicycle } from "../components/hemicycle.js";
import { dataTable } from "../components/table.js";
import {
  cssVar,
  flipTag,
  liveRefresh,
  marginLabel,
  parties,
  partyShade,
  partyTag,
  pc,
  raceColor,
  racePill,
  raceState,
  rampLegend,
  reportingMeter,
  searchInput,
  segmented,
  selectControl,
  swatchLegend,
} from "../components/res-kit.js";
import { resMap } from "../components/res-map.js";
import { liveCard, pageFrame } from "../components/res-page.js";
import { currentElectionId, links } from "./_shared.js";

const STATUS_ORDER = ["FINAL", "CALLED", "PROJECTED_WINNER", "LEAN", "TOO_CLOSE_TO_CALL", "RECOUNT", "TOO_EARLY_TO_CALL", "POLLS_CLOSED", "SCHEDULED"];

export async function render(el, params, ctx) {
  const id = currentElectionId();
  const ctrl = new AbortController();
  const ui = { mode: ctx.query.map, prov: ctx.query.prov || "", q: "" };
  const frame = pageFrame(el, {
    eyebrow: "Results · House of Representatives",
    title: "House",
    categories: ["SIMULATED", "FICTIONAL"],
    electionId: id,
    noticeText: "The seat counter shows the seats each party holds going into the election; the map shows incumbents and open seats.",
  });

  let data = null;
  let order = [];
  let partyNames = {};
  let provinces = [];
  let map = null;
  let table = null;
  let hemi = null;
  let lastSource = null;

  const seatCard = liveCard("Seats", { id: "res-h-seats" });
  const hemiHost = h("div", { class: "res-hemi" });
  const seatSide = h("div", { class: "res-seat-side" });
  const controlEl = h("div", { class: "res-control" });
  seatCard.body.append(h("div", { class: "res-seat-grid" }, h("div", null, controlEl, hemiHost, h("div", { class: "res-hemi-legend" })), seatSide));
  const mapCard = liveCard("District map", { id: "res-h-map", categories: [provBadge("FICTIONAL", "Fictional districts")] });
  const mapTools = h("div", { class: "res-card-tools" });
  const mapHost = h("div", { class: "res-map-host" });
  const legendEl = h("div", { class: "res-map-legend" });
  mapCard.body.append(mapTools, mapHost, legendEl);
  const statusCard = liveCard("Race status", { id: "res-h-status" });
  const flipCard = liveCard("Flips", { id: "res-h-flips" });
  const closeCard = liveCard("Closest races", { id: "res-h-close" });
  const tableCard = liveCard("All 150 districts", { id: "res-h-table", flush: true, categories: [provBadge("FICTIONAL", "Districts FICTIONAL"), provBadge("SIMULATED", "Results SIMULATED")] });
  const tableTools = h("div", { class: "res-card-tools res-card-tools--pad" });
  const tableHost = h("div");
  tableCard.body.append(tableTools, tableHost);

  const rows = () => data?.districts || [];
  const byCode = () => {
    if (byCode.data !== data) {
      byCode.cache = Object.fromEntries(rows().map((r) => [r.district_code || r.code, r]));
      byCode.data = data;
    }
    return byCode.cache;
  };

  // ---------------------------------------------------------------- seat counter
  function groupsFor() {
    const bp = Object.fromEntries((data.by_party || []).map((p) => [p.party, p]));
    const src = data.results_source;
    const codes = [...order.filter((c) => bp[c]), ...Object.keys(bp).filter((c) => !order.includes(c))];
    const groups = [];
    if (src === "hidden") {
      for (const c of codes) if (bp[c].previous) groups.push({ key: c, label: `${c} · held going in`, color: pc(c, bp[c].color), seats: bp[c].previous, style: "called" });
      return groups;
    }
    for (const c of codes) {
      const p = bp[c];
      const called = src === "final" ? p.won ?? p.total ?? 0 : p.called || 0;
      if (called) groups.push({ key: `${c}-c`, label: `${c} · ${src === "final" ? "won" : "called"}`, color: pc(c, p.color), seats: called, style: "called" });
      if (src === "live" && p.leading) groups.push({ key: `${c}-l`, label: `${c} · leading`, color: pc(c, p.color), seats: p.leading, style: "leading" });
    }
    return groups;
  }

  function paintSeats() {
    const src = data.results_source;
    const groups = groupsFor();
    const c = data.control || {};
    const center = src === "hidden" ? "150" : src === "final" ? `${c.largest_party || ""} ${c.largest_seats ?? ""}`.trim() : `${fmtInt(data.called)}`;
    const sub = `${data.label || `${data.majority} FOR CONTROL`}`;
    if (!hemi || hemi._src !== src) {
      hemi = hemicycle({ total: data.seats_total, majority: data.majority, groups, centerLabel: center, centerSub: sub, tooltip: (g) => g.label });
      hemi._src = src;
      mount(hemiHost, hemi);
    } else hemi.update({ groups, centerLabel: center, centerSub: sub });
    seatCard.setTitle(src === "hidden" ? "House going into the election" : `House · ${data.label || "76 FOR CONTROL"}`);
    keyed(controlEl, JSON.stringify([src, c, data.called]), () => {
      if (src === "hidden") return h("div", { class: "res-control__line" }, h("span", { class: "res-control__big" }, "76 FOR CONTROL"), h("span", { class: "muted" }, "All 150 seats are up · results hidden until reported"));
      if (src === "live") {
        const cp = c.controlling_party;
        return h(
          "div",
          { class: "res-control__line" },
          cp ? h("span", { class: "res-control__big", style: { "--party": pc(cp) } }, partyChip(cp, { color: pc(cp) }), " wins control") : h("span", { class: "res-control__big" }, "76 FOR CONTROL"),
          h("span", { class: "muted" }, cp ? "76 called seats reached" : `${fmtInt(data.called)} of ${data.seats_total} races decided · no party has 76 decided seats yet`),
        );
      }
      return h(
        "div",
        { class: "res-control__line" },
        c.controlling_party ? h("span", { class: "res-control__big" }, partyChip(c.controlling_party, { color: pc(c.controlling_party) }), " controls the House") : h("span", { class: "res-control__big" }, "No majority"),
        h("span", { class: "muted" }, c.label || ""),
      );
    });
    // party table
    const bp = [...(data.by_party || [])].sort((a, b) => (src === "hidden" ? (b.previous || 0) - (a.previous || 0) : (b.total ?? 0) - (a.total ?? 0) || (b.previous || 0) - (a.previous || 0)));
    keyed(seatSide, JSON.stringify([src, bp]), () =>
      h(
        "table",
        { class: "data res-seat-table" },
        h("caption", { class: "sr-only" }, "House seats by party"),
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", { scope: "col" }, "Party"),
            src === "live" ? [h("th", { class: "r", scope: "col" }, "Called"), h("th", { class: "r", scope: "col" }, "Leading")] : null,
            src === "final" ? h("th", { class: "r", scope: "col" }, "Won") : null,
            src !== "hidden" ? h("th", { class: "r", scope: "col" }, "Total") : null,
            h("th", { class: "r", scope: "col" }, src === "hidden" ? "Held" : "Prev."),
            src !== "hidden" ? h("th", { class: "r", scope: "col" }, "Net") : null,
            src === "final" ? h("th", { class: "r", scope: "col" }, "Vote") : null,
          ),
        ),
        h(
          "tbody",
          null,
          bp.map((p) =>
            h(
              "tr",
              null,
              h("td", null, partyTag(p.party, pc(p.party, p.color), partyNames[p.party])),
              src === "live" ? [h("td", { class: "r num" }, h("b", null, fmtInt(p.called))), h("td", { class: "r num muted" }, fmtInt(p.leading))] : null,
              src === "final" ? h("td", { class: "r num" }, h("b", null, fmtInt(p.won))) : null,
              src !== "hidden" ? h("td", { class: "r num" }, fmtInt(p.total)) : null,
              h("td", { class: "r num muted" }, fmtInt(p.previous)),
              src !== "hidden" ? h("td", { class: ["r", "num", "res-net", p.net_change > 0 && "is-up", p.net_change < 0 && "is-down"] }, p.net_change === null || p.net_change === undefined ? "–" : fmtSigned(p.net_change)) : null,
              src === "final" ? h("td", { class: "r num muted" }, fmtPct(p.vote_pct)) : null,
            ),
          ),
        ),
      ),
    );
    const leg = seatCard.body.querySelector(".res-hemi-legend");
    keyed(leg, src, () =>
      src === "live"
        ? swatchLegend([
            { color: cssVar("--text-secondary"), label: "Called / projected" },
            { color: partyShade(cssVar("--text-secondary"), 0.3), label: "Leading" },
            { color: cssVar("--uncalled"), label: "Uncalled" },
          ])
        : src === "hidden"
          ? h("span", { class: "res-muted-note" }, "Seats held by each party going into the election (incumbent parties).")
          : null,
    );
  }

  // ---------------------------------------------------------------- map
  const MODES = [
    { value: "control", label: "Control", title: "Winner / leader with call status" },
    { value: "leader", label: "Leader", title: "Current leader, called or not" },
    { value: "called", label: "Called", title: "Only decided races" },
    { value: "flips", label: "Flips" },
    { value: "open", label: "Open seats" },
    { value: "incumbents", label: "Incumbents" },
    { value: "margin", label: "Margins" },
  ];
  const modesFor = (src) => (src === "hidden" ? MODES.filter((m) => ["incumbents", "open"].includes(m.value)) : MODES);

  function specFor(f) {
    const r = byCode()[f.properties.code];
    if (!r) return { none: true };
    const col = raceColor(r);
    const decided = ["CALLED", "FINAL", "PROJECTED_WINNER"].includes(r.status);
    let spec;
    switch (ui.mode) {
      case "leader":
        spec = r.leader_party ? { fill: pc(r.leader_party, r.leader_color) } : {};
        break;
      case "called":
        spec = decided && col ? { fill: col } : {};
        break;
      case "flips":
        spec = r.flip_status === "flip" ? { fill: col } : r.flip_status ? { fill: cssVar("--uncalled") } : {};
        break;
      case "open": {
        const c = col || (r.previous_party ? pc(r.previous_party) : null);
        spec = r.open_seat ? (c ? { fill: c, state: col ? raceState(r) : "lean" } : { fill: cssVar("--text-muted") }) : { none: true };
        break;
      }
      case "incumbents":
        spec = r.incumbent?.party ? { fill: pc(r.incumbent.party), state: r.incumbent.running === false ? "leading" : "called" } : {};
        break;
      case "margin":
        spec = col && r.margin_pp !== null ? { fill: partyShade(col, (r.margin_pp || 0) / 25) } : {};
        break;
      default:
        spec = col ? { fill: col, state: raceState(r) } : {};
    }
    if (ui.prov && f.properties.province_code !== ui.prov) spec.dim = true;
    return spec;
  }

  function tooltip(f) {
    const r = byCode()[f.properties.code];
    if (!r) return h("div", { class: "res-tip" }, h("strong", null, f.properties.name));
    const src = data.results_source;
    return h(
      "div",
      { class: "res-tip" },
      h("div", { class: "res-tip__head" }, h("strong", null, r.name), racePill(r)),
      h("div", { class: "res-tip__sub muted" }, `${r.district_code || r.code} · ${provName(r.province_code)} · pop. ${fmtInt(f.properties.population)}`),
      src !== "hidden" && (r.top || []).some((l) => l.votes)
        ? h(
            "div",
            { class: "res-tip__rows" },
            (r.top || []).map((l) =>
              h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(l.party, l.color) } }), h("span", { class: "res-tip__name" }, `${l.name} (${l.party})`, l.incumbent ? " ·inc" : ""), h("b", { class: "num" }, fmtPct(l.pct)), h("span", { class: "res-tip__votes num" }, fmtInt(l.votes))),
            ),
          )
        : h(
            "div",
            { class: "res-tip__rows" },
            (r.top || []).map((l) => h("div", { class: "res-tip__row" }, h("span", { class: "chip__swatch", style: { "--party": pc(l.party, l.color) } }), h("span", { class: "res-tip__name" }, `${l.name} (${l.party})`), h("span", { class: "muted" }, l.incumbent ? "incumbent" : ""), h("span"))),
          ),
      h(
        "div",
        { class: "res-tip__kv" },
        r.margin_pp !== null && r.margin_pp !== undefined ? h("span", null, "Margin ", h("b", null, marginLabel(r.winner_party || r.leader_party, r.margin_pp))) : null,
        src === "live" ? h("span", null, "Reporting ", h("b", null, fmtPct(r.reporting_pct))) : null,
        src === "final" ? h("span", null, "Turnout ", h("b", null, fmtPct(r.turnout_pct))) : null,
        src === "live" && r.win_probability !== null && r.win_probability !== undefined ? h("span", null, "Call model ", h("b", null, fmtProb(r.win_probability))) : null,
        r.incumbent ? h("span", null, "Incumbent ", h("b", null, `${r.incumbent.name} (${r.incumbent.party})`), r.incumbent.running === false ? " · retiring" : "") : h("span", null, "No incumbent"),
        r.open_seat ? h("span", { class: "res-tag res-tag--open" }, "OPEN SEAT") : null,
        r.flip_status ? flipTag(r.flip_status, { prev: r.previous_party }) : null,
      ),
    );
  }
  const provName = (c) => provinces.find((p) => p.code === c)?.name || c;

  function legendFor() {
    const src = data.results_source;
    const present = [...new Set(rows().map((r) => r.winner_party || r.leader_party).filter(Boolean))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    const partyItems = (list) => list.map((p) => ({ color: pc(p), label: p }));
    switch (ui.mode) {
      case "margin":
        return rampLegend([0.1, 0.3, 0.5, 0.7, 0.9].map((t) => partyShade(cssVar("--text-secondary"), t)), ["0", "5", "10", "15", "20", "25+ pp"], { title: "Margin · leader's hue, darker = wider" });
      case "flips": {
        const fl = [...new Set(rows().filter((r) => r.flip_status === "flip").map((r) => r.winner_party || r.leader_party))];
        return swatchLegend([...fl.map((p) => ({ color: pc(p), label: `Flipped to ${p}` })), { color: cssVar("--uncalled"), label: "Held" }]);
      }
      case "open":
        return swatchLegend([{ color: partyShade(cssVar("--text-secondary"), 0.6), label: src === "hidden" ? "Open seat (previous holder's colour)" : "Open seat (leader / winner)" }, { kind: "none", label: "Incumbent running" }], { title: `${rows().filter((r) => r.open_seat).length} open seats` });
      case "incumbents": {
        const inc = [...new Set(rows().map((r) => r.incumbent?.party).filter(Boolean))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
        return h("div", { class: "res-legend-stack" }, swatchLegend(partyItems(inc), { title: "Incumbent party" }), swatchLegend([{ color: partyShade(cssVar("--text-secondary"), 0.35), label: "Incumbent retiring (tint)" }, { color: cssVar("--uncalled"), label: "Vacant" }]));
      }
      case "called":
        return swatchLegend([...partyItems(present), { color: cssVar("--uncalled"), label: "Not yet decided" }]);
      default:
        return h(
          "div",
          { class: "res-legend-stack" },
          swatchLegend(partyItems(present)),
          src === "live" && ui.mode === "control"
            ? swatchLegend([{ color: cssVar("--text-secondary"), label: "Called" }, { color: partyShade(cssVar("--text-secondary"), 0.3), label: "Leading / lean" }, { kind: "close", label: "Too close" }, { kind: "recount", label: "Recount" }, { color: cssVar("--uncalled"), label: "No result" }], { title: "State" })
            : null,
        );
    }
  }

  function paintMap(first) {
    const src = data.results_source;
    const modes = modesFor(src);
    if (!ui.mode || !modes.some((m) => m.value === ui.mode)) ui.mode = modes[0].value;
    keyed(mapTools, `${src}|${ui.mode}|${ui.prov}`, () => [
      segmented(modes, ui.mode, (v) => {
        ui.mode = v;
        setQuery({ map: v });
        paintMap(false);
      }, { label: "Map mode" }),
      h("span", { class: "res-toolbar__spacer" }),
      selectControl([{ value: "", label: "All provinces" }, ...provinces.map((p) => ({ value: p.code, label: p.name }))], ui.prov, (v) => {
        ui.prov = v;
        setQuery({ prov: v || null });
        zoom();
        paintTable(false);
        paintMap(false);
      }, { label: "Zoom to province" }),
    ]);
    mount(legendEl, legendFor());
    mapCard.setTitle(`District map · ${modes.find((m) => m.value === ui.mode)?.label || ""}`);
    if (first || !map) {
      map?.destroy();
      mount(mapHost);
      map = resMap(mapHost, {
        layer: "districts",
        outline: "provinces",
        height: 640,
        label: "Map of the 150 House districts; the district table below lists the same results",
        spec: specFor,
        tooltip,
        onClick: (f) => (location.hash = links.district(f.properties.code)),
      });
      map.ready.then(() => ui.prov && zoom()).catch((err) => mount(mapHost, h("div", { class: "state" }, `Map unavailable: ${err.message}`)));
    } else map.update();
  }
  function zoom() {
    if (!map) return;
    map.fitTo((f) => !ui.prov || f.properties.province_code === ui.prov);
  }

  // ---------------------------------------------------------------- side lists
  function paintSide() {
    const src = data.results_source;
    const sc = data.status_counts || {};
    keyed(statusCard.body, JSON.stringify(sc), () =>
      h("ul", { class: "res-statuslist" }, STATUS_ORDER.filter((s) => sc[s]).map((s) => h("li", null, statusPill(s), h("span", { class: "res-statuslist__n num" }, String(sc[s]))))),
    );
    const flips = rows().filter((r) => r.flip_status === "flip");
    keyed(flipCard.body, JSON.stringify(flips.map((r) => [r.code, r.winner_party || r.leader_party])) + src, () =>
      flips.length
        ? h(
            "div",
            null,
            h("div", { class: "res-flipcount" }, h("span", { class: "res-out__big num" }, String(data.flips ?? flips.length)), h("span", { class: "muted" }, src === "live" ? " seats changing hands so far" : " seats changed hands")),
            h(
              "ul",
              { class: "res-rank res-rank--scroll" },
              flips.map((r) =>
                h(
                  "li",
                  null,
                  h("a", { href: links.district(r.district_code || r.code), class: "res-rank__name" }, h("span", { class: "res-code" }, r.district_code || r.code), " ", r.name),
                  partyChip(r.previous_party, { color: pc(r.previous_party) }),
                  h("span", { class: "muted", "aria-hidden": "true" }, "→"),
                  partyChip(r.winner_party || r.leader_party, { color: raceColor(r) }),
                ),
              ),
            ),
          )
        : h("p", { class: "muted res-empty" }, src === "hidden" ? "Flips are shown once races are decided." : "No seat has changed hands."),
    );
    const closest = rows().filter((r) => r.margin_pp !== null && r.margin_pp !== undefined && (r.winner_party || r.leader_party) && (r.reporting_pct ?? 100) > 0).sort((a, b) => a.margin_pp - b.margin_pp).slice(0, 8);
    keyed(closeCard.body, JSON.stringify(closest.map((r) => [r.code, r.margin_pp, r.status])), () =>
      closest.length
        ? h(
            "ol",
            { class: "res-rank" },
            closest.map((r) => h("li", null, h("a", { href: links.district(r.district_code || r.code), class: "res-rank__name" }, h("span", { class: "res-code" }, r.district_code || r.code), " ", r.name), partyChip(r.winner_party || r.leader_party, { color: raceColor(r) }), h("span", { class: "res-rank__val num" }, marginLabel(null, r.margin_pp)))),
          )
        : h("p", { class: "muted res-empty" }, src === "hidden" ? "Margins appear once votes are counted." : "No votes counted yet."),
    );
  }

  // ---------------------------------------------------------------- table
  function paintTable(rebuild) {
    const src = data.results_source;
    const q = ui.q;
    const vis = rows()
      .filter((r) => !ui.prov || r.province_code === ui.prov)
      .filter((r) => !q || `${r.district_code} ${r.name} ${r.province_code} ${r.incumbent?.name || ""} ${r.leader_name || ""}`.toLowerCase().includes(q))
      .map((r) => ({ ...r, _leader: r.winner_party || r.leader_party || "", _inc: r.incumbent?.name || "", _status: STATUS_ORDER.length - STATUS_ORDER.indexOf(r.status) }));
    if (rebuild || !table) {
      table = dataTable(
        [
          { key: "district_code", label: "District", format: (v, r) => h("a", { href: links.district(v), class: "res-link" }, h("span", { class: "res-code" }, v), r.name) },
          { key: "_status", label: "Status", format: (v, r) => racePill(r) },
          src !== "hidden" ? { key: "_leader", label: src === "final" ? "Winner" : "Leader", format: (v, r) => (v ? partyTag(v, raceColor(r), r.winner_name || r.leader_name) : h("span", { class: "muted" }, "–")) } : null,
          src !== "hidden" ? { key: "margin_pp", label: "Margin", align: "r", format: (v) => marginLabel(null, v) } : null,
          src === "live" ? { key: "reporting_pct", label: "Reporting", format: (v) => reportingMeter(v, { width: 46 }) } : null,
          src === "final" ? { key: "turnout_pct", label: "Turnout", align: "r", format: (v) => fmtPct(v) } : null,
          { key: "_inc", label: "Incumbent", format: (v, r) => (r.incumbent ? h("span", { class: "res-who" }, partyChip(r.incumbent.party, { color: pc(r.incumbent.party) }), h("span", { class: "res-who__name" }, r.incumbent.name), r.incumbent.running === false ? h("span", { class: "res-tag" }, "RETIRING") : null) : h("span", { class: "muted" }, "Vacant")) },
          { key: "open_seat", label: "Open", value: (r) => (r.open_seat ? 1 : 0), format: (v) => (v ? h("span", { class: "res-tag res-tag--open" }, "OPEN") : "") },
          src !== "hidden" ? { key: "flip_status", label: "Flip", format: (v, r) => flipTag(v, { prev: r.previous_party }) } : { key: "previous_party", label: "Held by", format: (v) => (v ? partyChip(v, { color: pc(v) }) : "–") },
        ].filter(Boolean),
        vis,
        { sortKey: "district_code", sortDir: "asc", maxHeight: 640, rowHref: (r) => links.district(r.district_code), rowClass: (r) => (r.flip_status === "flip" ? "res-row--flip" : undefined), caption: "House district results" },
      );
      mount(tableHost, table);
    } else table.update(vis);
    const n = tableTools.querySelector(".res-muted-note");
    if (n) n.textContent = `${vis.length} of ${rows().length} districts`;
  }

  function paint(first) {
    frame.setSource(data);
    paintSeats();
    paintMap(first);
    paintSide();
    paintTable(first);
  }

  const fetchData = () => api.get(`/api/elections/${id}/house`, { signal: ctrl.signal });
  try {
    const [d, p, pv] = await Promise.all([fetchData(), parties(), api.get("/api/provinces", { cache: true })]);
    data = d;
    order = p.order;
    partyNames = Object.fromEntries(p.list.map((x) => [x.code, x.name]));
    provinces = pv.provinces || [];
    lastSource = d.results_source;
    frame.setTitle("House", `Results · House of Representatives · ${d.election?.name || ""}`);
    mount(
      frame.body,
      seatCard,
      h("div", { class: "res-grid res-grid--map" }, mapCard, h("div", { class: "res-stack" }, statusCard, flipCard, closeCard)),
      tableCard,
    );
    mount(tableTools, searchInput("Search district, incumbent or candidate…", (q) => {
      ui.q = q;
      paintTable(false);
    }, { label: "Search districts" }), h("span", { class: "res-muted-note" }));
    paint(true);
  } catch (err) {
    if (err?.name !== "AbortError") frame.error(err);
    return () => ctrl.abort();
  }

  const stopLive = liveRefresh(id, async () => {
    const d = await fetchData();
    const changed = d.results_source !== lastSource;
    lastSource = d.results_source;
    data = d;
    paint(changed);
  });

  return () => {
    ctrl.abort();
    stopLive();
    frame.stop();
    map?.destroy();
  };
}
