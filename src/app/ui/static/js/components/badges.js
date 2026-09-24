/** Provenance badges (REAL / DERIVED / FICTIONAL / SIMULATED), race-status pills, party chips. */
import { h } from "../dom.js";
import { STATUS_LABELS } from "../format.js";
import { icon } from "./icons.js";

const PROV_TITLES = {
  REAL: "Real Dutch source data (CBS / PDOK)",
  DERIVED: "Derived deterministically from real data",
  FICTIONAL: "Fictional construct of this simulator",
  SIMULATED: "Model-generated simulation output — not a real result or prediction",
};

export function provBadge(category = "SIMULATED", label) {
  const cat = String(category).toUpperCase();
  return h("span", { class: `prov prov--${cat.toLowerCase()}`, title: PROV_TITLES[cat] || cat }, label || cat);
}

const STATUS_CLASS = {
  SCHEDULED: "closed",
  POLLS_CLOSED: "closed",
  TOO_EARLY_TO_CALL: "early",
  TOO_CLOSE_TO_CALL: "close",
  LEAN: "lean",
  PROJECTED_WINNER: "projected",
  CALLED: "called",
  RECOUNT: "recount",
  FINAL: "final",
};

/** Race status pill.  `color` = party colour of the leader / called candidate. */
export function statusPill(status, { color, label } = {}) {
  const s = String(status || "SCHEDULED").toUpperCase();
  const cls = STATUS_CLASS[s] || "early";
  const ico =
    s === "CALLED" ? icon("check", { size: 11, className: "pill__icon" }) :
    s === "FINAL" ? icon("lock", { size: 11, className: "pill__icon" }) :
    s === "RECOUNT" || s === "TOO_CLOSE_TO_CALL" ? icon("alert", { size: 11, className: "pill__icon" }) : null;
  return h(
    "span",
    { class: `pill pill--${cls}`, style: color ? { "--party": color } : undefined, title: STATUS_LABELS[s] || s },
    ico,
    label || STATUS_LABELS[s] || s,
  );
}

export function partyChip(code, { color, name } = {}) {
  return h(
    "span",
    { class: "chip", style: { "--party": color }, title: name || code || "Independent" },
    h("span", { class: "chip__swatch" }),
    code || "IND",
  );
}

export function toWin(majority = 88) {
  return h("span", { class: "to-win" }, `${majority} TO WIN`);
}

const FLIP_TITLES = {
  flip: "Changed party compared with the previous holder",
  hold: "Same party as the previous holder",
  new: "No previous holder (new seat / first election)",
};

/** Flip / hold / new badge from an API `flip_status` (null → nothing). */
export function flipBadge(flipStatus, { party } = {}) {
  if (!flipStatus) return null;
  const s = String(flipStatus).toLowerCase();
  return h(
    "span",
    { class: `flipb flipb--${s}`, title: FLIP_TITLES[s] || s, style: party ? { "--party": party } : undefined },
    s === "flip" ? icon("flip", { size: 10, className: "pill__icon" }) : null,
    s.toUpperCase(),
  );
}

/** Pulsing LIVE marker (or a static label for other phases). */
export function liveBadge(label = "LIVE") {
  return h("span", { class: "livebadge" }, h("span", { class: "live-dot", "aria-hidden": "true" }), label);
}
