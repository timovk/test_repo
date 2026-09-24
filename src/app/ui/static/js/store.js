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

/** ElectionBrief (meta.elections row) of an election id (default: the selected election). */
export function getElection(id = state.electionId) {
  return (state.meta?.elections || []).find((e) => e.id === Number(id)) || null;
}

/**
 * The night payload of an election if the store currently holds it (the poller only follows the
 * selected election), else null.
 */
export function nightFor(id = state.electionId) {
  const n = state.night;
  return n && Number(n.election_id) === Number(id) ? n : null;
}

/** Statuses the API treats as decided (meta.decided_statuses), with the documented default. */
export function decidedStatuses() {
  return state.meta?.decided_statuses || ["CALLED", "FINAL", "PROJECTED_WINNER"];
}
