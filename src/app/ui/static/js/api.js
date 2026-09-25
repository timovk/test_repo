/**
 * JSON API client.  All data comes from the FastAPI backend under /api; the UI performs no
 * election mathematics.  GET responses can be cached (immutable resources such as GeoJSON).
 */

const cache = new Map();

export class ApiError extends Error {
  constructor(status, detail, code) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.code = code;
  }
}

/** Requests that take longer than this are aborted and reported (never an endless spinner). */
export const DEFAULT_TIMEOUT_MS = 60000;

/** Link an optional caller signal with a timeout; returns {signal, done, timedOut()}. */
function withTimeout(callerSignal, timeoutMs) {
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeoutMs);
  const onAbort = () => ctrl.abort();
  if (callerSignal) {
    if (callerSignal.aborted) ctrl.abort();
    else callerSignal.addEventListener("abort", onAbort, { once: true });
  }
  return {
    signal: ctrl.signal,
    timedOut: () => timedOut,
    done: () => {
      clearTimeout(timer);
      if (callerSignal) callerSignal.removeEventListener("abort", onAbort);
    },
  };
}

async function request(method, path, body, { signal, timeout = DEFAULT_TIMEOUT_MS } = {}) {
  const url = path.startsWith("/") ? path : `/api/${path}`;
  const t = withTimeout(signal, timeout);
  try {
    const res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: t.signal,
    });
    const ct = res.headers.get("content-type") || "";
    const payload = ct.includes("json") ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail), payload?.code);
    }
    return payload;
  } catch (err) {
    if (t.timedOut()) {
      throw new ApiError(0, `The server did not answer ${method} ${url} within ${Math.round(timeout / 1000)} s. Check the terminal running \`python -m app run\`.`, "timeout");
    }
    if (err instanceof ApiError || err?.name === "AbortError") throw err;
    throw new ApiError(0, `Cannot reach the server (${method} ${url}): ${err?.message || err}`, "network");
  } finally {
    t.done();
  }
}

export const api = {
  get(path, { cache: useCache = false, signal, timeout } = {}) {
    const url = path.startsWith("/") ? path : `/api/${path}`;
    if (useCache && cache.has(url)) return cache.get(url);
    const p = request("GET", url, undefined, { signal, timeout });
    if (useCache) {
      cache.set(url, p);
      p.catch(() => cache.delete(url));
    }
    return p;
  },
  post: (path, body = {}, opts = {}) => request("POST", path, body, opts),
  put: (path, body = {}, opts = {}) => request("PUT", path, body, opts),
  del: (path) => request("DELETE", path),
  invalidate(prefix = "") {
    for (const k of [...cache.keys()]) if (k.startsWith(prefix) || k.startsWith(`/api/${prefix}`)) cache.delete(k);
  },
};
