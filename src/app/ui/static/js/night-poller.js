/**
 * Follows the election-night state of the selected election and publishes it to the store
 * (`night`).  The header strip and all live views subscribe to `night`.
 *
 * * Running nights: Server-Sent Events (`/api/night/{id}/stream`, one frame per change), falling
 *   back to polling `/state` every second when EventSource is unavailable or the stream fails.
 * * Ready / paused nights: polling `/state` every 5 s (picks up a start from another tab), no
 *   open stream.
 * * FINAL elections: the stored final state is fetched once (it never changes).
 * * When the election's status changes (simulated → live → final, or back on reset), `/api/meta`
 *   is refreshed so every view sees the new `results_source`.
 */
import { api } from "./api.js";
import { getState, setState } from "./store.js";

let timer = null;
let es = null;
let currentId = null;
let sseRetryAt = 0;
let lastStatus = null;

function electionStatus(id) {
  return (getState().meta?.elections || []).find((e) => e.id === id)?.status || null;
}

async function refreshMeta() {
  try {
    const meta = await api.get("/api/meta");
    setState({ meta });
  } catch {
    /* keep the old meta */
  }
}

/** Publish a state payload (ignoring stale payloads of a previously selected election). */
function publish(st, { authoritative = false } = {}) {
  if (!st || Number(st.election_id) !== Number(currentId)) return;
  const prev = getState().night;
  // Out-of-order frames (a slow poll answering after a newer stream frame) must not rewind.
  if (!authoritative && prev && Number(prev.election_id) === Number(st.election_id) && st.clock && prev.clock) {
    const older = (st.clock.seq ?? 0) < (prev.clock.seq ?? 0);
    const reset = st.clock.status === "ready" && prev.clock.status !== "ready";
    if (older && !reset) return;
  }
  setState({ night: st });
  const status = st.election_status || null;
  if (status && status !== lastStatus && status !== electionStatus(currentId)) refreshMeta();
  lastStatus = status;
  follow(st);
}

/** Choose the transport for the published state: stream while running, else slow polling. */
function follow(st) {
  const status = st?.clock?.status;
  clearTimeout(timer);
  if (status === "finished") {
    closeStream();
    return;
  }
  if (status === "running") {
    if (!es && Date.now() >= sseRetryAt && openStream(currentId)) return;
    if (!es) timer = setTimeout(fetchOnce, 1000);
    return;
  }
  closeStream();
  timer = setTimeout(fetchOnce, 5000);
}

async function fetchOnce() {
  const id = currentId;
  if (!id) return;
  try {
    const st = await api.get(`/api/night/${id}/state?detail=summary`);
    if (id !== currentId) return;
    publish(st);
  } catch (err) {
    if (id !== currentId) return;
    // Not a night-capable election (no night stored) or server down.
    if (getState().night) setState({ night: null });
    closeStream();
    if (err?.status === 404 || err?.status === 409 || err?.status === 503) return; // nothing to follow
    clearTimeout(timer);
    timer = setTimeout(fetchOnce, 10000);
  }
}

function openStream(id) {
  if (!window.EventSource || !id) return false;
  try {
    const src = new EventSource(`/api/night/${id}/stream`);
    es = src;
    src.onmessage = (e) => {
      if (es !== src) return;
      try {
        publish(JSON.parse(e.data));
      } catch {
        /* ignore malformed frame */
      }
    };
    src.onerror = () => {
      if (es !== src) return;
      closeStream();
      sseRetryAt = Date.now() + 15000; // poll for a while, then try the stream again
      if (id === currentId) fetchOnce();
    };
    return true;
  } catch {
    es = null;
    return false;
  }
}

function closeStream() {
  if (es) es.close();
  es = null;
}

export function watchNight(electionId) {
  if (electionId === currentId) return;
  stopNight();
  currentId = electionId;
  lastStatus = electionStatus(electionId);
  if (!electionId) return;
  fetchOnce();
}

export function stopNight() {
  clearTimeout(timer);
  closeStream();
  currentId = null;
}

/** Election id the poller is following. */
export function watchedElection() {
  return currentId;
}

/** Send a playback control action and publish the resulting state. */
export async function nightControl(action, speed) {
  const id = currentId || getState().electionId;
  const st = await api.post(`/api/night/${id}/control`, speed ? { action, speed } : { action });
  if (Number(id) === Number(currentId)) publish(st, { authoritative: true });
  return st;
}
