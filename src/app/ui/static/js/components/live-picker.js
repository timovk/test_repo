/**
 * Election picker for page headers (the strip has the global one): navigates to the same route
 * with ?e=<id>.  `presidentialOnly` marks elections without a presidential race (midterms) as
 * unavailable, for the President / Electoral College pages.
 */
import { h } from "../dom.js";
import { getState } from "../store.js";

export function electionSelect({ presidentialOnly = false, label = "Election" } = {}) {
  const { meta, electionId } = getState();
  const els = (meta?.elections || []).slice().reverse();
  return h(
    "label",
    { class: "lv-select" },
    h("span", { class: "sr-only" }, label),
    h(
      "select",
      {
        class: "select",
        "aria-label": label,
        onchange: (e) => {
          const [path] = location.hash.replace(/^#/, "").split("?");
          location.hash = `#${path || "/night"}?e=${e.target.value}`;
        },
      },
      els.map((e) => {
        const noPres = presidentialOnly && e.election_type !== "general";
        const status = e.status === "live" ? "LIVE" : e.status === "final" || e.status === "certified" ? "final" : "not reported";
        return h("option", { value: e.id, selected: e.id === electionId, disabled: noPres && e.id !== electionId }, `${e.year} · ${e.name} (${noPres ? "no presidential race" : status})`);
      }),
    ),
  );
}
