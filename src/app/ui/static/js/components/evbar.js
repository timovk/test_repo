/**
 * Electoral-vote bar: decided EV (solid, party colour), leading EV (hatched), uncalled (track),
 * with the majority marker ("88 TO WIN").  Tickets are ordered as given (keep a stable order —
 * colour follows the entity, never its rank).
 */
import { h } from "../dom.js";
import { fmtInt } from "../format.js";

/**
 * @param {{total:number, majority:number, tickets:Array<{key,label,color,decided:number,leading:number}>,
 *          large?:boolean, showLegend?:boolean}} props
 */
export function evBar({ total = 174, majority = 88, tickets = [], large = false, showLegend = true }) {
  const segs = [];
  const decidedSegs = tickets.filter((t) => t.decided > 0);
  const leadingSegs = tickets.filter((t) => t.leading > 0);
  const used = tickets.reduce((a, t) => a + (t.decided || 0) + (t.leading || 0), 0);
  for (const t of decidedSegs)
    segs.push(h("div", { class: "evbar__seg", style: { "--party": t.color, flex: `0 0 ${(t.decided / total) * 100}%` }, title: `${t.label}: ${t.decided} EV decided` }));
  const uncalled = Math.max(0, total - used);
  if (uncalled > 0) segs.push(h("div", { class: "evbar__seg evbar__seg--uncalled", style: { flex: `1 1 ${(uncalled / total) * 100}%` }, title: `${uncalled} EV uncalled` }));
  for (const t of [...leadingSegs].reverse())
    segs.push(h("div", { class: "evbar__seg evbar__seg--leading", style: { "--party": t.color, flex: `0 0 ${(t.leading / total) * 100}%` }, title: `${t.label}: leading in ${t.leading} EV` }));
  const pct = (majority / total) * 100;
  const left = tickets[0];
  const right = tickets.length > 1 ? tickets[1] : null;
  return h(
    "div",
    { class: ["evbar", large && "evbar--lg"], role: "img", "aria-label": `Electoral votes: ${tickets.map((t) => `${t.label} ${t.decided}`).join(", ")}; ${majority} needed of ${total}` },
    h(
      "div",
      { style: { position: "relative" } },
      h("div", { class: "evbar__track" }, segs),
      h("div", { class: "evbar__marker", style: { left: `${pct}%` } }, h("span", { class: "evbar__marker-label" }, `${majority} TO WIN`)),
    ),
    showLegend
      ? h(
          "div",
          { class: "evbar__legend" },
          h("span", { style: { color: "var(--text-primary)" } }, left ? `${left.label} ${fmtInt(left.decided)}` : ""),
          h("span", { class: "muted" }, `${fmtInt(uncalled)} uncalled · ${total} total`),
          h("span", null, right ? `${fmtInt(right.decided)} ${right.label}` : ""),
        )
      : null,
  );
}
