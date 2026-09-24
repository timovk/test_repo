/**
 * Polls the live election-night state while the selected election's night is running (or has
 * been viewed), publishing it to the store (`night`).  Uses Server-Sent Events when available,
 * falling back to polling.  The header strip and all live views subscribe to `night`.
 */
import { api } from "./api.js";
import { getState, setState } from "./store.js";

let timer = null;
let es = null;
let currentId = null;

async function fetchOnce() {
  if (!currentId) return;
  try {
    const st = await api.get(`/api/night/${currentId}/state?detail=summary`);
    setState({ night: st });
    schedule(st);
  } catch (err) {
    // Not a night-capable election (e.g. history) or server down: back off.
    setState({ night: null });
    schedule(null, 10000);
  }
}

function schedule(st, delay) {
  clearTimeout(timer);
  const running = st?.clock?.status === "running";
  timer = setTimeout(fetchOnce, delay ?? (running ? 1000 : 5000));
}

export function watchNight(electionId) {
  if (electionId === currentId) return;
  stopNight();
  currentId = electionId;
  if (!electionId) return;
  if (window.EventSource) {
    try {
      es = new EventSource(`/api/night/${electionId}/stream`);
      es.onmessage = (e) => {
        try {
          setState({ night: JSON.parse(e.data) });
        } catch {
          /* ignore malformed frame */
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        fetchOnce();
      };
    } catch {
      es = null;
    }
  }
  fetchOnce();
}

export function stopNight() {
  clearTimeout(timer);
  es?.close();
  es = null;
  currentId = null;
}

/** Send a playback control action and publish the resulting state. */
export async function nightControl(action, speed) {
  const id = currentId || getState().electionId;
  const st = await api.post(`/api/night/${id}/control`, speed ? { action, speed } : { action });
  setState({ night: st });
  schedule(st);
  return st;
}
