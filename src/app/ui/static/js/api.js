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

async function request(method, path, body, { signal } = {}) {
  const res = await fetch(path.startsWith("/") ? path : `/api/${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });
  const ct = res.headers.get("content-type") || "";
  const payload = ct.includes("json") ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail), payload?.code);
  }
  return payload;
}

export const api = {
  get(path, { cache: useCache = false, signal } = {}) {
    const url = path.startsWith("/") ? path : `/api/${path}`;
    if (useCache && cache.has(url)) return cache.get(url);
    const p = request("GET", url, undefined, { signal });
    if (useCache) {
      cache.set(url, p);
      p.catch(() => cache.delete(url));
    }
    return p;
  },
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
  invalidate(prefix = "") {
    for (const k of [...cache.keys()]) if (k.startsWith(prefix) || k.startsWith(`/api/${prefix}`)) cache.delete(k);
  },
};
