/** Shared floating tooltip for charts (follows the pointer; content is built with DOM nodes). */
import { h, mount } from "../dom.js";

let tip = null;
function ensure() {
  if (!tip) {
    tip = h("div", { class: "chart-tip", role: "tooltip" });
    document.body.appendChild(tip);
  }
  return tip;
}

export function showTip(evt, content) {
  const t = ensure();
  mount(t, content);
  const pad = 14;
  const w = t.offsetWidth || 180;
  const hgt = t.offsetHeight || 60;
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + hgt > window.innerHeight - 8) y = evt.clientY - hgt - pad;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
  t.classList.add("is-visible");
}

export function hideTip() {
  if (tip) tip.classList.remove("is-visible");
}
