/**
 * Minimal global state with subscriptions: meta (constitution, elections), the selected
 * election, UI settings, and the live election-night state (polled while a night runs).
 */

const listeners = new Map();
const state = {
  meta: null,
  electionId: null,
  settings: { theme: "dark", partyColors: {} },
  night: null, // latest NightManager.state() payload for state.electionId
};

export function getState() {
  return state;
}

export function setState(patch) {
  const changed = [];
  for (const [k, v] of Object.entries(patch)) {
    if (state[k] !== v) {
      state[k] = v;
      changed.push(k);
    }
  }
  for (const k of changed) (listeners.get(k) || []).forEach((fn) => fn(state[k], state));
  if (changed.length) (listeners.get("*") || []).forEach((fn) => fn(state, changed));
}

/** Subscribe to one key (or "*"); returns an unsubscribe function. */
export function subscribe(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  return () => listeners.get(key).delete(fn);
}

/** Party colour with user overrides (settings) applied. */
export function partyColor(code, fallback) {
  return state.settings.partyColors?.[code] || fallback || "var(--uncalled)";
}
