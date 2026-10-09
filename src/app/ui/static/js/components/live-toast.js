/**
 * Transient notifications (e.g. a refused playback action).  Text only, built with h().
 * An optional `action` ({label, onClick}) adds one button (the toast then stays a little longer);
 * `busy` shows a spinner.  The returned element has `.dismiss()`.
 */
import { h } from "../dom.js";

let host = null;

export function toast(message, { kind = "info", timeout = 4200, action, busy = false } = {}) {
  if (!host) {
    host = h("div", { class: "lv-toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(host);
  }
  const el = h(
    "div",
    { class: ["lv-toast", `lv-toast--${kind}`, (action || busy) && "lv-toast--rich"] },
    busy ? h("span", { class: "lc-spinner", "aria-hidden": "true" }) : null,
    h("span", { class: "lv-toast__text" }, String(message)),
    action
      ? h(
          "button",
          {
            class: "btn btn--sm btn--primary lv-toast__action",
            type: "button",
            onclick: () => {
              dismiss();
              action.onClick();
            },
          },
          action.label,
        )
      : null,
  );
  let gone = false;
  function dismiss() {
    if (gone) return;
    gone = true;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 300);
  }
  el.dismiss = dismiss;
  host.appendChild(el);
  if (timeout > 0) setTimeout(dismiss, action && timeout === 4200 ? 12000 : timeout);
  return el;
}
