/** Transient notifications (e.g. a refused playback action).  Text only, built with h(). */
import { h } from "../dom.js";

let host = null;

export function toast(message, { kind = "info", timeout = 4200 } = {}) {
  if (!host) {
    host = h("div", { class: "lv-toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(host);
  }
  const el = h("div", { class: `lv-toast lv-toast--${kind}` }, String(message));
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 300);
  }, timeout);
  return el;
}
