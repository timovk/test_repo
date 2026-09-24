/** Hash router: #/path/:param?query — lazy-loads view modules from ./views/<name>.js. */
import { ROUTES, DEFAULT_ROUTE } from "./routes.js";

function compile(path) {
  const keys = [];
  const re = new RegExp(
    "^" +
      path.replace(/\/:([a-zA-Z_]+)/g, (_, k) => {
        keys.push(k);
        return "/([^/]+)";
      }) +
      "/?$",
  );
  return { re, keys };
}

const compiled = ROUTES.map((r) => ({ ...r, ...compile(r.path) }));

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#/, "") || DEFAULT_ROUTE;
  const [pathPart, queryPart = ""] = raw.split("?");
  const query = Object.fromEntries(new URLSearchParams(queryPart));
  for (const r of compiled) {
    const m = pathPart.match(r.re);
    if (m) {
      const params = Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])]));
      return { route: r, params, query, path: pathPart };
    }
  }
  return { route: null, params: {}, query, path: pathPart };
}

export function navigate(path, query) {
  const q = query ? `?${new URLSearchParams(query)}` : "";
  location.hash = `#${path}${q}`;
}

/** Update query parameters without re-rendering the view (e.g. map metric toggles). */
export function setQuery(patch) {
  const { path, query } = parseHash();
  const next = { ...query, ...patch };
  for (const k of Object.keys(next)) if (next[k] === null || next[k] === undefined || next[k] === "") delete next[k];
  const q = new URLSearchParams(next).toString();
  history.replaceState(null, "", `#${path}${q ? `?${q}` : ""}`);
}

export function startRouter(onRoute) {
  const handler = () => onRoute(parseHash());
  window.addEventListener("hashchange", handler);
  handler();
}
