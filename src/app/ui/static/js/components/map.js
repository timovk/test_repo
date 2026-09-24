/**
 * Leaflet wrapper for vector election maps (no tile basemap → fully offline).  GeoJSON layers
 * come from /api/geo/*.geojson (cached).  Styling and tooltips are supplied by the view, so the
 * same component renders winner / margin / swing / turnout / reporting / party-share maps.
 */
import { api } from "../api.js";
import { h } from "../dom.js";

const NL_BOUNDS = [
  [50.72, 3.3],
  [53.58, 7.25],
];

export function electionMap(container, { layer, style, tooltip, onClick, height, outline = "provinces" }) {
  const el = h("div", { class: "map", style: height ? { "--map-h": `${height}px` } : undefined });
  container.appendChild(el);
  const map = L.map(el, {
    zoomControl: true,
    attributionControl: true,
    preferCanvas: true,
    zoomSnap: 0.25,
    minZoom: 6,
    maxZoom: 13,
  });
  map.attributionControl.setPrefix("");
  map.attributionControl.addAttribution("Boundaries: CBS/PDOK (CC BY 4.0) · Results: SIMULATED");
  map.fitBounds(NL_BOUNDS);
  let geo = null;
  let outlineLayer = null;
  let styleFn = style;
  let tooltipFn = tooltip;

  const ready = (async () => {
    const data = await api.get(`/api/geo/${layer}.geojson`, { cache: true });
    geo = L.geoJSON(data, {
      style: (f) => ({ weight: 0.6, color: "rgba(8,13,25,0.55)", fillOpacity: 0.92, ...(styleFn ? styleFn(f) : {}) }),
      onEachFeature: (f, lyr) => {
        if (tooltipFn) lyr.bindTooltip(() => tooltipFn(f), { sticky: true, className: "nl-tip", direction: "top", opacity: 1 });
        lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#ffffff" }));
        lyr.on("mouseout", () => geo.resetStyle(lyr));
        if (onClick) lyr.on("click", () => onClick(f));
      },
    }).addTo(map);
    if (outline && outline !== layer) {
      const o = await api.get(`/api/geo/${outline}.geojson`, { cache: true });
      outlineLayer = L.geoJSON(o, { interactive: false, style: { weight: 1.6, color: "rgba(255,255,255,0.55)", fill: false } }).addTo(map);
    }
    const b = geo.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding: [8, 8] });
    return geo;
  })();

  return {
    map,
    el,
    ready,
    /** Re-style all features (e.g. after live updates or metric change). */
    restyle(newStyle, newTooltip) {
      if (newStyle) styleFn = newStyle;
      if (newTooltip) tooltipFn = newTooltip;
      if (geo) geo.setStyle((f) => ({ weight: 0.6, color: "rgba(8,13,25,0.55)", fillOpacity: 0.92, ...(styleFn ? styleFn(f) : {}) }));
    },
    fitTo(filterFn) {
      if (!geo) return;
      const layers = geo.getLayers().filter((l) => filterFn(l.feature));
      if (layers.length) map.fitBounds(L.featureGroup(layers).getBounds(), { padding: [12, 12] });
    },
    destroy() {
      map.remove();
      el.remove();
      void outlineLayer;
    },
  };
}
