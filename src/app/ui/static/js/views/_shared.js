/**
 * Helpers shared by all views: page header with provenance badges, election picker, async
 * loading/error states, party colour lookup and the "fictional system" notice.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { provBadge } from "../components/badges.js";
import { getState, partyColor } from "../store.js";

/** Page header: eyebrow, title, optional meta nodes (badges, pickers, buttons). */
export function pageHeader({ eyebrow, title, meta = [], categories = [] }) {
  return h(
    "header",
    { class: "page-head" },
    h("div", null, eyebrow ? h("div", { class: "page-head__eyebrow" }, eyebrow) : null, h("h1", { class: "page-head__title" }, title)),
    h("div", { class: "page-head__meta" }, ...categories.map((c) => provBadge(c)), ...meta),
  );
}

export function card(title, body, { actions, foot, flush = false, className, categories = [] } = {}) {
  return h(
    "section",
    { class: ["card", className] },
    title || actions || categories.length
      ? h(
          "div",
          { class: "card__head" },
          h("h2", { class: "card__title" }, title || ""),
          h("div", { class: "page-head__meta" }, ...categories.map((c) => provBadge(c)), actions || null),
        )
      : null,
    h("div", { class: flush ? "card__body card__body--flush" : "card__body" }, body),
    foot ? h("div", { class: "card__foot" }, foot) : null,
  );
}

export function stat(label, value, delta) {
  return h("div", { class: "stat" }, h("span", { class: "stat__label" }, label), h("span", { class: "stat__value" }, value), delta ? h("span", { class: "stat__delta" }, delta) : null);
}

/** Current election id (store) with a fallback to meta.demo_election_id. */
export function currentElectionId() {
  const { electionId, meta } = getState();
  return electionId || meta?.demo_election_id || null;
}

/** Election picker bound to the global selection (navigates with ?e=<id>). */
export function electionPicker() {
  const { meta } = getState();
  const cur = currentElectionId();
  const sel = h(
    "select",
    { class: "select", "aria-label": "Election", onchange: (e) => {
      const [path] = location.hash.replace(/^#/, "").split("?");
      location.hash = `#${path || "/night"}?e=${e.target.value}`;
    } },
    (meta?.elections || []).map((e) => h("option", { value: e.id, selected: e.id === cur }, `${e.year} · ${e.name} (${e.status})`)),
  );
  return sel;
}

/** Render a promise into `el` with loading and error states. */
export async function load(el, promise, render) {
  mount(el, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "60%", height: "14px" } })));
  try {
    const data = await promise;
    mount(el, render(data));
    return data;
  } catch (err) {
    console.error(err);
    mount(el, errorState(err));
    return null;
  }
}

export function errorState(err) {
  const status = err?.status;
  const msg = status === 404 ? "Not available for this election." : err?.message || String(err);
  return h("div", { class: "state" }, h("strong", null, status === 404 ? "No data" : "Something went wrong"), h("span", { class: "muted" }, msg));
}

export function emptyState(text) {
  return h("div", { class: "state" }, text);
}

/** Party colour for a code, honouring user overrides; falls back to the payload colour. */
export function colorOf(code, fallback) {
  return partyColor(code, fallback);
}

export function fictionalNotice(text) {
  return h(
    "div",
    { class: "notice notice--fictional" },
    provBadge("FICTIONAL"),
    h(
      "span",
      null,
      text ||
        "This is a fictional constitutional system. Geography and population are real (CBS/PDOK); parties, candidates, districts and all results are simulated and are not predictions of real Dutch politics.",
    ),
  );
}

/** Link helpers keep URLs consistent across views. */
export const links = {
  province: (code) => `#/provinces/${code}`,
  municipality: (code) => `#/municipalities/${code}`,
  district: (code) => `#/house/${code}`,
  race: (code) => `#/races/${code}`,
  candidate: (id) => `#/candidates/${id}`,
};

export { api };
