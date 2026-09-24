/**
 * Electoral-vote bar: per ticket, decided EV (solid, party colour) followed by the EV of provinces
 * it currently leads (hatched), then uncalled EV (track), with the majority marker ("88 TO WIN").
 * Tickets are drawn in the order given (callers keep a stable, meaningful order — colour follows
 * the entity, never its rank).  All numbers come from the API; the bar only lays them out.
 *
 * The returned element has `update(props)` which re-keys the existing segments in place, so live
 * updates animate (flex-grow transitions) instead of re-rendering.
 */
import { h } from "../dom.js";
import { fmtInt } from "../format.js";
import { hideTip, showTip } from "./tooltip.js";

/**
 * @param {{total:number, majority:number, tickets:Array<{key,label,color,decided:number,leading:number,name?:string}>,
 *          large?:boolean, showLegend?:boolean, compact?:boolean, legendMax?:number, markerLabel?:string|false}} props
 */
export function evBar(props) {
  let p = normalize(props);
  const track = h("div", { class: "evbar__track" });
  const markerLabel = h("span", { class: "evbar__marker-label" });
  const marker = h("div", { class: "evbar__marker", "aria-hidden": "true" }, markerLabel);
  const legend = h("div", { class: "evbar__legend" });
  const root = h(
    "div",
    { class: ["evbar", p.large && "evbar--lg", p.compact && "evbar--compact"], role: "img" },
    h("div", { class: "evbar__wrap", style: { position: "relative" } }, track, marker),
    p.showLegend ? legend : null,
  );
  const segs = new Map();

  function seg(id, cls, color) {
    let el = segs.get(id);
    if (!el) {
      el = h("div", { class: ["evbar__seg", cls] });
      el.addEventListener("mousemove", (e) => el._tip && showTip(e, el._tip()));
      el.addEventListener("mouseleave", hideTip);
      segs.set(id, el);
    }
    if (color) el.style.setProperty("--party", color);
    return el;
  }

  function sync() {
    const { total, majority, tickets } = p;
    const order = [];
    let used = 0;
    for (const t of tickets) {
      const d = Math.max(0, t.decided || 0);
      const l = Math.max(0, t.leading || 0);
      used += d + l;
      const sd = seg(`${t.key}:d`, null, t.color);
      sd.style.flexGrow = String(d);
      sd.hidden = d <= 0;
      sd._tip = () => tipNode(t, "decided", total);
      order.push(sd);
      const sl = seg(`${t.key}:l`, "evbar__seg--leading", t.color);
      sl.style.flexGrow = String(l);
      sl.hidden = l <= 0;
      sl._tip = () => tipNode(t, "leading", total);
      order.push(sl);
    }
    const uncalled = Math.max(0, total - used);
    const su = seg("__uncalled", "evbar__seg--uncalled");
    su.style.flexGrow = String(uncalled);
    su.hidden = uncalled <= 0;
    su._tip = () => h("div", null, h("strong", { class: "num" }, `${fmtInt(uncalled)} EV`), h("div", { class: "muted" }, "Uncalled, no votes counted yet"));
    order.push(su);
    // drop segments of tickets that disappeared
    const keep = new Set(order);
    for (const [id, el] of segs) if (!keep.has(el)) (el.remove(), segs.delete(id));
    // reorder only when needed (moving nodes restarts nothing, but avoid churn)
    const cur = [...track.children];
    if (cur.length !== order.length || cur.some((c, i) => c !== order[i])) order.forEach((el) => track.appendChild(el));

    marker.style.left = `${(majority / total) * 100}%`;
    markerLabel.textContent = p.markerLabel === false ? "" : p.markerLabel || `${majority} TO WIN`;
    root.setAttribute(
      "aria-label",
      `Electoral votes, ${majority} of ${total} needed to win: ` +
        (tickets.length ? tickets.map((t) => `${t.label} ${t.decided || 0} decided${t.leading ? `, leading in ${t.leading}` : ""}`).join("; ") : "none allocated") +
        `; ${uncalled} uncalled.`,
    );
    if (p.showLegend) renderLegend(uncalled);
  }

  function renderLegend(uncalled) {
    const { tickets, total, legendMax } = p;
    const shown = tickets.filter((t) => (t.decided || 0) + (t.leading || 0) > 0).slice(0, legendMax);
    const items = shown.map((t) =>
      h(
        "span",
        { class: "evbar__key" },
        h("span", { class: "chip__swatch", style: { "--party": t.color } }),
        h("span", { class: "evbar__key-label" }, t.label),
        h("b", { class: "num" }, fmtInt(t.decided || 0)),
        t.leading ? h("span", { class: "evbar__key-lead num", title: "EV in provinces where this ticket leads, not yet called" }, `+${fmtInt(t.leading)}`) : null,
      ),
    );
    const allocated = tickets.reduce((a, t) => a + (t.decided || 0), 0);
    const rest = h(
      "span",
      { class: "evbar__key evbar__key--rest muted" },
      shown.length ? `${fmtInt(uncalled)} uncalled` : `${fmtInt(allocated)} EV allocated · ${fmtInt(total)} available`,
    );
    legend.replaceChildren(...items, rest);
  }

  sync();
  root.update = (next) => {
    p = normalize({ ...p, ...next });
    sync();
    return root;
  };
  return root;
}

function normalize(props = {}) {
  return {
    total: props.total || 174,
    majority: props.majority || 88,
    tickets: props.tickets || [],
    large: !!props.large,
    compact: !!props.compact,
    showLegend: props.showLegend !== false,
    legendMax: props.legendMax ?? 6,
    markerLabel: props.markerLabel,
  };
}

function tipNode(t, kind, total) {
  const v = kind === "decided" ? t.decided : t.leading;
  return h(
    "div",
    null,
    h("div", { class: "legend__item", style: { marginBottom: "4px" } }, h("span", { class: "chip__swatch", style: { "--party": t.color } }), h("span", null, t.name || t.label)),
    h("strong", { class: "num" }, `${fmtInt(v)} EV `),
    h("span", { class: "muted" }, kind === "decided" ? "called / decided" : "leading, not yet called"),
    h("div", { class: "muted num" }, `${fmtInt(t.decided || 0)} decided · ${fmtInt(t.leading || 0)} leading · of ${total}`),
  );
}
