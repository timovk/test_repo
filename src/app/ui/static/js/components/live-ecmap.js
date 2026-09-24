/**
 * Electoral College map with two synchronised modes: the 12-province tile cartogram and the
 * geographic (Leaflet, offline vector) province map.  Province states come from API statuses:
 * uncalled · leading (hatched / light) · called (solid) · flip badge · too close (amber, icon) ·
 * recount (dashed orange, label).  Includes the state + party legend and a table view (the
 * accessible alternative).  `update(rows)` restyles in place.
 *
 * Rows (normalised by the caller from /president provinces or the night snapshot):
 *   {code, name, ev, status, leader:{key,name,party,color}|null, winner:{…}|null, margin, reporting,
 *    winProb, flip, previousParty, recounted, highlight, lean:{…}|null}
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtPct, fmtProb, STATUS_LABELS } from "../format.js";
import { flipBadge, statusPill } from "./badges.js";
import { icon } from "./icons.js";
import { electionMap } from "./map.js";
import { dataTable } from "./table.js";
import { tileMap } from "./tilemap.js";
import { cssVar, mapState, MAP_STATE_LABELS } from "./live-util.js";
import { subscribe } from "../store.js";

const MODE_KEY = "nlfed.ecmap.mode";

function readMode(fallback) {
  try {
    return localStorage.getItem(MODE_KEY) || fallback;
  } catch {
    return fallback;
  }
}
function saveMode(m) {
  try {
    localStorage.setItem(MODE_KEY, m);
  } catch {
    /* per-viewer convenience only */
  }
}

/** State of a normalised row (API status → map state). */
export function rowState(p) {
  return mapState(p.status, !!p.leader && (p.reporting ?? 0) > 0);
}

/** Tooltip body for a province row. */
export function provinceTip(p) {
  const st = rowState(p);
  const who = p.winner || p.leader;
  return h(
    "div",
    { class: "lv-tip" },
    h("div", { class: "lv-tip__head" }, h("strong", null, p.name || p.code), h("span", { class: "muted num" }, ` · ${p.ev ?? "–"} EV`)),
    h("div", { class: "lv-tip__row" }, statusPill(p.status, { color: (p.winner || p.leader)?.color }), p.flip ? flipBadge(p.flip) : null),
    who
      ? h(
          "div",
          { class: "lv-tip__row" },
          h("span", { class: "chip__swatch", style: { "--party": who.color } }),
          h("span", null, who.name || who.key),
          h("span", { class: "muted" }, who.party ? ` (${who.party})` : ""),
          h("span", { class: "muted" }, p.winner ? " · winner" : st === "leading" || st === "close" || st === "recount" ? " · leads" : ""),
        )
      : h("div", { class: "muted" }, "No votes counted yet"),
    p.winner && p.leader && p.leader.key !== p.winner.key ? h("div", { class: "muted" }, `Counted leader: ${p.leader.name || p.leader.key}`) : null,
    p.lean && !p.winner ? h("div", { class: "muted" }, `Model lean: ${p.lean.name || p.lean.key}`) : null,
    h(
      "div",
      { class: "lv-tip__grid num" },
      h("span", { class: "muted" }, "Margin"),
      h("b", null, p.margin === null || p.margin === undefined ? "–" : `${fmtPct(p.margin, 2).replace("%", "")} pp`),
      h("span", { class: "muted" }, "Reporting"),
      h("b", null, fmtPct(p.reporting)),
      p.winProb !== null && p.winProb !== undefined && !p.winner ? [h("span", { class: "muted" }, "Win prob. (model)"), h("b", null, fmtProb(p.winProb))] : null,
      p.previousParty ? [h("span", { class: "muted" }, "Previously"), h("b", null, p.previousParty)] : null,
    ),
    p.recounted ? h("div", { class: "lv-tip__note" }, icon("alert", { size: 11 }), " Decided after a recount") : null,
    p.highlight ? h("div", { class: "lv-tip__note" }, "Tipping-point province") : null,
  );
}

function stateSwatch(state) {
  return h("span", { class: `lv-swatch lv-swatch--${state}`, "aria-hidden": "true" });
}

/**
 * @param {{height?:number, onSelect?:(code)=>void, mode?:'tiles'|'geo', compactTiles?:boolean, caption?:string}} opts
 */
export function ecMap({ height = 420, onSelect, mode, compactTiles = false, caption } = {}) {
  let rows = [];
  let current = readMode(mode || "tiles");
  const tiles = tileMap([], onSelect, { tooltip: provinceTip, showReporting: true, compact: compactTiles, label: "Electoral College tile map: 12 provinces; select a province for details" });
  const tilesWrap = h("div", { class: "lv-ecmap__tiles" }, tiles);
  const geoWrap = h("div", { class: "lv-ecmap__geo", role: "img", "aria-label": "Geographic province map of the Electoral College (the table view lists the same data)" });
  let geo = null;
  let labels = [];
  const legendEl = h("div", { class: "lv-ecmap__legend" });
  const tableHost = h("div");
  const tableCols = [
    { key: "name", label: "Province", format: (v, r) => h("a", { href: `#/provinces/${r.code}` }, v) },
    { key: "ev", label: "EV", align: "r" },
    { key: "status", label: "Status", value: (r) => STATUS_LABELS[r.status] || r.status, format: (v, r) => statusPill(r.status, { color: (r.winner || r.leader)?.color }) },
    { key: "who", label: "Winner / leader", value: (r) => (r.winner || r.leader)?.name || "", format: (v, r) => ((r.winner || r.leader) ? h("span", { class: "chip" }, h("span", { class: "chip__swatch", style: { "--party": (r.winner || r.leader).color } }), `${v} (${(r.winner || r.leader).party || "–"})`) : "–") },
    { key: "margin", label: "Margin", align: "r", format: (v) => (v === null || v === undefined ? "–" : `${fmtPct(v, 2).replace("%", "")} pp`) },
    { key: "reporting", label: "Reporting", align: "r", format: (v) => fmtPct(v) },
    { key: "flip", label: "Flip", format: (v) => flipBadge(v) || "–" },
  ];
  const table = dataTable(tableCols, [], { rowHref: (r) => `#/provinces/${r.code}` });
  tableHost.appendChild(table);

  const btnTiles = h("button", { type: "button", onclick: () => setMode("tiles"), "aria-pressed": "false" }, "Tiles");
  const btnGeo = h("button", { type: "button", onclick: () => setMode("geo"), "aria-pressed": "false" }, "Map");
  const toolbar = h("div", { class: "segmented", role: "group", "aria-label": "Map type" }, btnTiles, btnGeo);

  const el = h(
    "div",
    { class: "lv-ecmap" },
    h("div", { class: "lv-ecmap__stage", style: { "--ecmap-h": `${height}px` } }, tilesWrap, geoWrap),
    legendEl,
    caption ? h("p", { class: "lv-ecmap__caption muted" }, caption) : null,
    h("details", { class: "lv-tableview" }, h("summary", null, "Table view"), tableHost),
  );

  function geoStyle(f) {
    const p = rows.find((r) => r.code === f.properties.code);
    const unc = cssVar("--uncalled") || "#3a475f";
    if (!p) return { fillColor: unc, fillOpacity: 0.9 };
    const st = rowState(p);
    const color = (p.winner || p.leader)?.color;
    const base = { weight: 0.8, color: cssVar("--bg") || "#080d19", fillColor: color || unc, fillOpacity: 0.92, dashArray: null };
    if (st === "uncalled") return { ...base, fillColor: unc, fillOpacity: 0.85 };
    if (st === "leading") return { ...base, fillOpacity: 0.42 };
    if (st === "close") return { ...base, fillOpacity: 0.3, color: cssVar("--status-warning"), weight: 2.4, dashArray: "5 4" };
    if (st === "recount") return { ...base, fillOpacity: 0.45, color: cssVar("--status-serious"), weight: 2.6, dashArray: "3 4" };
    if (p.highlight) return { ...base, color: cssVar("--text-primary"), weight: 2.6 };
    return base;
  }

  async function ensureGeo() {
    if (geo || typeof L === "undefined") return;
    geo = electionMap(geoWrap, {
      layer: "provinces",
      height,
      outline: null,
      style: geoStyle,
      tooltip: (f) => {
        const p = rows.find((r) => r.code === f.properties.code);
        return p ? provinceTip(p) : h("div", null, f.properties.name);
      },
      onClick: (f) => onSelect && onSelect(f.properties.code),
    });
    try {
      await geo.ready;
      const info = await api.get("/api/provinces", { cache: true });
      for (const pr of info.provinces || []) {
        if (!pr.centroid) continue;
        const node = h("div", { class: "lv-maplabel" }, h("b", null, pr.code), h("span", null, String(pr.electoral_votes ?? "")));
        const m = L.marker([pr.centroid[1], pr.centroid[0]], { icon: L.divIcon({ className: "lv-maplabel-wrap", html: node, iconSize: [36, 30], iconAnchor: [18, 15] }), interactive: false, keyboard: false });
        m.addTo(geo.map);
        labels.push(m);
      }
    } catch (e) {
      if (e?.name !== "AbortError") console.warn(e);
    }
  }

  function setMode(m) {
    current = m === "geo" ? "geo" : "tiles";
    saveMode(current);
    btnTiles.classList.toggle("is-active", current === "tiles");
    btnGeo.classList.toggle("is-active", current === "geo");
    btnTiles.setAttribute("aria-pressed", String(current === "tiles"));
    btnGeo.setAttribute("aria-pressed", String(current === "geo"));
    tilesWrap.hidden = current !== "tiles";
    geoWrap.hidden = current !== "geo";
    if (current === "geo") {
      // create after the container is visible so Leaflet can measure it
      requestAnimationFrame(() => {
        if (!geo) ensureGeo();
        else geo.map.invalidateSize();
      });
    }
  }

  function renderLegend() {
    const parties = new Map();
    for (const r of rows) {
      const who = r.winner || r.leader;
      if (who?.party && !parties.has(who.party)) parties.set(who.party, who);
    }
    const states = ["uncalled", "leading", "close", "called", "recount"];
    mount(
      legendEl,
      h(
        "div",
        { class: "legend lv-legend-states" },
        states.map((s) =>
          h(
            "span",
            { class: "legend__item" },
            stateSwatch(s),
            s === "close" ? icon("alert", { size: 11 }) : null,
            MAP_STATE_LABELS[s],
          ),
        ),
        h("span", { class: "legend__item" }, h("span", { class: "tile__flip lv-flip-key" }, "FLIP"), "Changed party"),
      ),
      parties.size
        ? h(
            "div",
            { class: "legend lv-legend-parties" },
            [...parties.values()].map((w) => h("span", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": w.color } }), `${w.party}${w.short ? ` · ${w.short}` : ""}`)),
          )
        : null,
    );
  }

  function toTiles() {
    return rows.map((p) => ({
      code: p.code,
      name: p.name,
      ev: p.ev,
      state: rowState(p),
      color: (p.winner || p.leader)?.color,
      flip: p.flip === "flip",
      recounted: !!p.recounted,
      reporting: p.reporting,
      highlight: !!p.highlight,
      dim: !!p.dim,
      title: `${p.name}: ${p.ev} electoral votes, ${STATUS_LABELS[p.status] || p.status}${(p.winner || p.leader) ? `, ${p.winner ? "won by" : "led by"} ${(p.winner || p.leader).name}` : ""}`,
    }));
  }

  let legendKey = "";
  function update(next) {
    rows = next || [];
    tiles.update(toTiles());
    if (geo) geo.restyle(geoStyle);
    const lk = JSON.stringify(rows.map((r) => [(r.winner || r.leader)?.party, (r.winner || r.leader)?.color]));
    if (lk !== legendKey) {
      legendKey = lk;
      renderLegend();
    }
    table.update(rows.map((r) => ({ ...r, who: (r.winner || r.leader)?.name || "" })));
  }

  const unsub = subscribe("settings", () => geo && geo.restyle(geoStyle));
  setMode(current);
  return {
    el,
    toolbar,
    update,
    setMode,
    destroy() {
      unsub();
      labels = [];
      geo?.destroy();
      geo = null;
    },
  };
}
