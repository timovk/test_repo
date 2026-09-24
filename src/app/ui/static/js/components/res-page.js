/**
 * Page frame for the results pages (prefix `res-`): header (eyebrow, title, provenance badges,
 * results-source badge, election picker), live ticker, hidden-results notice, toolbar and body.
 * The frame is built once; `setSource(payload)` / `setTitle()` update it in place.
 */
import { h, keyed, mount } from "../dom.js";
import { electionPicker, errorState, pageHeader } from "../views/_shared.js";
import { hiddenNotice, nightTicker, sourceBadge } from "./res-kit.js";

export function pageFrame(el, { eyebrow, title, categories = [], electionId, picker = true, noticeText }) {
  const src = h("span", { class: "res-src-slot" });
  const header = pageHeader({ eyebrow, title, categories, meta: [src, picker ? electionPicker() : null] });
  header.classList.add("res-head");
  const ticker = nightTicker(electionId);
  const notice = h("div", { class: "res-notice-slot" });
  const toolbar = h("div", { class: "res-toolbar", role: "toolbar", "aria-label": "Page controls" });
  const body = h("div", { class: "res-body" }, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "50%", height: "14px" } })));
  mount(el, header, ticker, notice, toolbar, body);
  return {
    header,
    toolbar,
    body,
    setTitle(text, eyebrowText) {
      const t = header.querySelector(".page-head__title");
      if (t && text) t.textContent = text;
      const e = header.querySelector(".page-head__eyebrow");
      if (e && eyebrowText) e.textContent = eyebrowText;
      if (text) document.title = `${text} · NL Federal Election Simulator`;
    },
    setSource(payload) {
      const s = payload?.results_source || "hidden";
      keyed(src, s, () => sourceBadge(s));
      keyed(notice, s, () => hiddenNotice(payload, noticeText));
    },
    error(err) {
      console.error(err);
      mount(body, errorState(err));
    },
    stop() {
      ticker.stop?.();
    },
  };
}

/** A card section (same markup as _shared.card) whose body can be re-rendered in place. */
export function liveCard(title, { categories = [], actions, foot, flush = false, className, id } = {}) {
  const bodyEl = h("div", { class: flush ? "card__body card__body--flush" : "card__body" });
  const titleEl = h("h2", { class: "card__title", id }, title || "");
  const metaEl = h("div", { class: "page-head__meta" }, ...categories, actions || null);
  const footEl = foot !== undefined ? h("div", { class: "card__foot" }, foot) : null;
  const el = h("section", { class: ["card", className], "aria-labelledby": id }, h("div", { class: "card__head" }, titleEl, metaEl), bodyEl, footEl);
  el.body = bodyEl;
  el.meta = metaEl;
  el.foot = footEl;
  el.setTitle = (t) => {
    titleEl.textContent = t;
  };
  return el;
}
