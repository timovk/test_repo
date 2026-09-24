/**
 * Results map (prefix `res-`): wraps components/map.js with theme-aware styling — a thin
 * surface-coloured stroke separates polygons (the "surface gap"), hover draws an ink ring, and
 * race states map to fill/stroke conventions shared with the tile map:
 *   called/final → solid party colour · projected → strong · lean/leading → light party tint ·
 *   too close → amber dashed edge · recount → orange dashed edge · none → "not up" neutral.
 * Restyles itself when the theme changes.
 */
import { subscribe } from "../store.js";
import { electionMap } from "./map.js";
import { cssVar, mix } from "./res-kit.js";

const STATE_WEIGHT = { called: 1, final: 1, projected: 0.86, lean: 0.66, leading: 0.52, close: 0.52 };

/** Resolve a {fill, state} spec into Leaflet path options. */
export function paint(spec) {
  const surface = cssVar("--surface-1");
  const base = { weight: 0.7, color: surface, opacity: 1, fillOpacity: 1, dashArray: null };
  if (!spec || !spec.fill) return { ...base, fillColor: spec?.none ? cssVar("--not-up") : cssVar("--uncalled"), fillOpacity: spec?.none ? 0.9 : 1 };
  const st = spec.state || "called";
  let fill = spec.fill;
  if (st in STATE_WEIGHT && STATE_WEIGHT[st] < 1) fill = mix(spec.fill, cssVar("--surface-2"), STATE_WEIGHT[st]);
  const out = { ...base, fillColor: fill };
  if (st === "close") Object.assign(out, { color: cssVar("--status-warning"), weight: 1.6, dashArray: "4 3" });
  if (st === "recount") Object.assign(out, { color: cssVar("--status-serious"), weight: 1.8, dashArray: "3 3" });
  if (spec.emphasis) Object.assign(out, { color: cssVar("--text-primary"), weight: 2.4, dashArray: null });
  if (spec.dim) out.fillOpacity = 0.35;
  return out;
}

/**
 * @param {HTMLElement} container  (must already be attached to the document)
 * @param {{layer:string, spec:(f)=>{fill?:string,state?:string,none?:boolean}, tooltip?:(f)=>Node,
 *          onClick?:(f)=>void, height?:number, filter?:(f)=>boolean, outline?:string|null,
 *          outlineFilter?:(f)=>boolean, label:string, outlineWeight?:number}} opts
 */
export function resMap(container, { layer, spec, tooltip, onClick, height, filter, outline = "provinces", outlineFilter, label, outlineWeight = 1.3 }) {
  let specFn = spec;
  const m = electionMap(container, {
    layer,
    height,
    filter,
    outline,
    outlineFilter,
    label,
    style: (f) => paint(specFn(f)),
    tooltip,
    onClick,
    hoverStyle: () => ({ weight: 2.4, color: cssVar("--text-primary"), dashArray: null }),
    outlineStyle: () => ({ weight: outlineWeight, color: mix(cssVar("--text-primary"), cssVar("--surface-2"), 0.55), opacity: 0.9, fill: false }),
  });
  m.el.classList.add("res-map");
  const unsub = subscribe("settings", () => m.restyle());
  const destroy = m.destroy;
  return {
    ...m,
    /** Re-apply styles with an optional new spec function. */
    update(newSpec) {
      if (newSpec) specFn = newSpec;
      m.restyle((f) => paint(specFn(f)));
    },
    destroy() {
      unsub();
      try {
        destroy();
      } catch {
        /* map already gone */
      }
    },
  };
}
