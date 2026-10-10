/**
 * The world clock client (docs/CLOCK.md): one "today" for the whole FICTIONAL world.  The world
 * moves from election day to election day; on each election day the user WATCHES its night or
 * COUNTS it instantly.
 *
 *   * `refreshClock()` — GET /api/clock → store.clock (today, today's elections, next election
 *     day, running job).  Refreshed at boot, after every clock action and when a job ends.
 *   * `goToNextElectionDay()` — POST /api/clock/next (creates the day's election: seconds) →
 *     the payload with `arrived` (the arrival dialog shows it).
 *   * `watchToday()` — POST /api/clock/watch (simulates today's election, 10–15 s for a regular
 *     one), then selects it, opens #/night?e=ID and starts playback (the strip's Start ▶).
 *   * `countToday()` / `skipTo(date)` — 202 background jobs, polled every second through
 *     GET /api/clock/job → store.clockJob; `onClockJobDone(fn)` fires once per finished job.
 *   * store.clockBusy — the action in flight ("refresh" while the clock is re-read after a job);
 *     while a job runs the server answers 409 "the clock is busy", so every clock button is
 *     disabled (`clockIsBusy()`).
 *
 * The client never decides anything: today, the next day, what is pending and every result are
 * API values.
 */
import { api } from "./api.js";
import { toast } from "./components/live-toast.js";
import { fmtDate } from "./components/local-kit.js";
import { nightControl, stopNight } from "./night-poller.js";
import { getState, setState } from "./store.js";

const JOB_POLL_MS = 1000;
/** Clock actions create or simulate elections (a regular November election takes seconds). */
const ACTION_TIMEOUT_MS = 600000;

/* ------------------------------------------------------------------ state */
let clockPromise = null;

/** GET /api/clock → store.clock (concurrent calls share one request). */
export function refreshClock() {
  if (clockPromise) return clockPromise;
  clockPromise = api
    .get("/api/clock", { timeout: 120000 })
    .then((c) => {
      publishClock(c);
      return c;
    })
    .finally(() => {
      clockPromise = null;
    });
  return clockPromise;
}

let clockAt = 0; // when store.clock was last published

/** Whether store.clock was published (or is being re-read) within the last `ms` milliseconds. */
export function clockIsFresh(ms = 2500) {
  return !!clockPromise || Date.now() - clockAt < ms;
}

function publishClock(c) {
  if (!c || !c.today) return;
  clockAt = Date.now();
  setState({ clock: c });
  if (c.job) handleJob(c.job);
}

/** The clock action in flight, or the kind of the running job ("count" / "skip"), else null. */
export function clockIsBusy() {
  const { clockBusy, clockJob } = getState();
  return clockBusy || (clockJob?.running ? clockJob.kind : null);
}

function setBusy(kind) {
  setState({ clockBusy: kind });
}

let refreshing = null; // the clock re-read after a finished job (store.clockBusy "refresh")

/** Wait for a pending post-job refresh; throw when another clock action or job is running. */
async function ensureIdle() {
  if (refreshing) await refreshing;
  const busy = clockIsBusy();
  if (busy) throw new Error(busy === "count" || busy === "skip" ? "The clock is busy — wait for the running count or skip to finish." : "The clock is busy — another clock action is in progress.");
}

/* ------------------------------------------------------------------ background job */
let jobTimer = null;
const seenRunning = new Set(); // job ids seen running in this page session
const handled = new Set(); // job ids whose end was handled
const jobListeners = new Set();

/** Subscribe to finished jobs (fires once per job that ran while this page was open). */
export function onClockJobDone(fn) {
  jobListeners.add(fn);
  return () => jobListeners.delete(fn);
}

function handleJob(job) {
  setState({ clockJob: job });
  if (job.running) {
    seenRunning.add(job.id);
    ensureJobPoll();
    return;
  }
  if (seenRunning.has(job.id) && !handled.has(job.id)) {
    handled.add(job.id);
    jobFinished(job);
  }
}

function ensureJobPoll() {
  if (jobTimer) return;
  const tick = async () => {
    jobTimer = null;
    let next = JOB_POLL_MS;
    try {
      const { job } = await api.get("/api/clock/job", { timeout: 20000 });
      if (job) handleJob(job);
      if (!job?.running) return;
    } catch {
      next = 3000; // server busy or restarting: keep trying, slower
    }
    if (!jobTimer) jobTimer = setTimeout(tick, next);
  };
  jobTimer = setTimeout(tick, JOB_POLL_MS);
}

async function jobFinished(job) {
  const counted = job.result?.counted || [];
  const selected = getState().electionId;
  // The night state the poller holds for a counted election is stale.  Drop it, but don't
  // re-follow now: the server rebuilds a counted election's night on its next request (seconds
  // to a minute on the REAL geography), so that happens when its night page is opened.
  if (selected && counted.map(Number).includes(Number(selected))) {
    stopNight();
    setState({ night: null });
  }
  // /api/meta is fast, /api/clock takes seconds on the REAL geography: tell the listeners (which
  // reload their own panels) as soon as meta is fresh, while the clock refreshes in parallel.
  api.invalidate("/api/");
  // Until the fresh clock arrives the page shows the previous day: hold every clock action.
  setBusy("refresh");
  const clockDone = (refreshing = refreshClock()
    .catch(() => null)
    .finally(() => {
      refreshing = null;
      if (getState().clockBusy === "refresh") setBusy(null);
    }));
  const meta = await api.get("/api/meta").catch(() => null);
  if (meta) setState({ meta });
  jobListeners.forEach((fn) => {
    try {
      fn(job);
    } catch (err) {
      console.error(err);
    }
  });
  await clockDone;
  if (job.status === "error") {
    toast(`The clock could not finish: ${job.error || "unknown error"}`, { kind: "error", timeout: 12000 });
  } else if (!location.hash.startsWith("#/today")) {
    toast(jobSummary(job), { timeout: 9000, action: { label: "Open Today", onClick: () => (location.hash = "#/today") } });
  }
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** One-line summary of a finished job. */
export function jobSummary(job) {
  if (!job) return "";
  if (job.status === "error") return `Stopped: ${job.error || "unknown error"}`;
  const n = job.result?.counted?.length ?? job.done ?? 0;
  const secs = job.seconds !== null && job.seconds !== undefined ? ` in ${Math.round(job.seconds)} s` : "";
  if (job.kind === "skip") return `Skipped ahead to ${job.result?.today ? fmtDate(job.result.today, "long") : "the new date"} · ${plural(n, "election")} counted${secs}.`;
  return `${n === 1 && job.current ? job.current : plural(n, "election")} counted instantly${secs}.`;
}

/* ------------------------------------------------------------------ actions */
/**
 * POST /api/clock/next: go to the next election day (409 while today's election is unfinished).
 * Returns the clock payload with `arrived: {previous, today, election_id, election, news}`.
 */
export async function goToNextElectionDay() {
  await ensureIdle();
  setBusy("next");
  try {
    const res = await api.post("/api/clock/next", {}, { timeout: ACTION_TIMEOUT_MS });
    api.invalidate("/api/");
    publishClock(res);
    const meta = await api.get("/api/meta").catch(() => null);
    if (meta) setState({ meta });
    return res;
  } finally {
    setBusy(null);
  }
}

/** POST /api/clock/watch, then open today's election night and start the count. */
export async function watchToday() {
  await ensureIdle();
  setBusy("watch");
  try {
    const res = await api.post("/api/clock/watch", {}, { timeout: ACTION_TIMEOUT_MS });
    api.invalidate("/api/");
    publishClock(res);
    const meta = await api.get("/api/meta").catch(() => null);
    if (meta) setState({ meta });
    await openAndStartNight(Number(res.election_id));
    return res;
  } finally {
    setBusy(null);
  }
}

/**
 * Select an election, open its night page and start (or resume) playback — what the strip's
 * Start ▶ button does.  A night that is already running is just opened.
 */
export async function openAndStartNight(id) {
  const { selectElection } = await import("./app.js");
  stopNight();
  setState({ night: null });
  selectElection(id);
  const target = `#/night?e=${id}`;
  if (location.hash !== target) location.hash = target;
  let status = null;
  try {
    // The first request of a night builds it on the server (seconds on the REAL geography).
    const st = await api.get(`/api/night/${id}/state?detail=summary`, { timeout: 180000 });
    status = st?.clock?.status || null;
  } catch {
    status = "ready";
  }
  if (status !== "ready" && status !== "paused") return;
  try {
    await nightControl(status === "ready" ? "start" : "resume");
  } catch (err) {
    toast(err?.message || "Could not start the election night — press Start in the top bar.", { kind: "error", timeout: 9000 });
  }
}

/** POST /api/clock/count: count today's election (and anything unfinished before it) instantly. */
export async function countToday() {
  await ensureIdle();
  setBusy("count");
  try {
    const res = await api.post("/api/clock/count", {}, { timeout: 120000 });
    seenRunning.add(res?.job?.id);
    publishClock(res);
    if (res?.job) handleJob(res.job);
    ensureJobPoll();
    return res;
  } finally {
    setBusy(null);
  }
}

/** POST /api/clock/skip {date}: count every election day before `date` and move the clock there. */
export async function skipTo(date) {
  await ensureIdle();
  setBusy("skip");
  try {
    const res = await api.post("/api/clock/skip", { date }, { timeout: 120000 });
    seenRunning.add(res?.job?.id);
    publishClock(res);
    if (res?.job) handleJob(res.job);
    ensureJobPoll();
    return res;
  } finally {
    setBusy(null);
  }
}
