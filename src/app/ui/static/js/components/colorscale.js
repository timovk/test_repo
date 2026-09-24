/**
 * Colour scales for maps.  Categorical = party identity; sequential = one hue light→dark;
 * diverging = two hues with a neutral midpoint; margin shading = party hue mixed toward the
 * surface by margin (a sequential ramp per party).
 */

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function sequential(value, [lo, hi]) {
  const ramp = [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--seq-${i}`));
  if (value === null || value === undefined || Number.isNaN(value)) return cssVar("--uncalled-soft");
  const t = Math.max(0, Math.min(0.9999, (value - lo) / Math.max(1e-9, hi - lo)));
  return ramp[Math.floor(t * ramp.length)];
}

export function diverging(value, maxAbs) {
  const steps = ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"].map(cssVar);
  if (value === null || value === undefined || Number.isNaN(value)) return cssVar("--uncalled-soft");
  const t = Math.max(-1, Math.min(1, value / Math.max(1e-9, maxAbs)));
  const idx = Math.round((t + 1) * 3);
  return steps[idx];
}

/** Party colour faded toward the surface for small margins (0 pp → faint, ≥ cap pp → full). */
export function marginShade(color, marginPP, cap = 30) {
  if (!color) return cssVar("--uncalled");
  const t = Math.max(0.18, Math.min(1, Math.abs(marginPP ?? 0) / cap));
  return `color-mix(in srgb, ${color} ${Math.round(t * 100)}%, ${cssVar("--surface-2")})`;
}

export function rampLegend(kind, labels) {
  const n = kind === "diverging" ? 7 : 6;
  const cols = kind === "diverging"
    ? ["--div-neg-3", "--div-neg-2", "--div-neg-1", "--div-mid", "--div-pos-1", "--div-pos-2", "--div-pos-3"]
    : [1, 2, 3, 4, 5, 6].map((i) => `--seq-${i}`);
  return { stops: cols.slice(0, n).map((c) => `var(${c})`), labels };
}
