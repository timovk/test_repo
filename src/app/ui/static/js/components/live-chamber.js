/**
 * House / Senate mini panel for the live pages: hemicycle (called solid · leading light ·
 * holdover outlined · uncalled grey) with the control line, a control status line and a compact
 * party table.  All counts come from the API (night snapshot or /house, /senate); `update(data)`
 * recolours seats and rewrites the table in place.
 *
 * data: {parties:[{party,color,holdover?,called,leading,net?}], called, leading, uncalled, up?,
 *        control, controlLabel, final}
 */
import { h, mount } from "../dom.js";
import { fmtInt, fmtSigned } from "../format.js";
import { hemicycle } from "./hemicycle.js";
import { icon } from "./icons.js";
import { setText } from "./live-util.js";

export function chamberPanel({ kind = "house", total = 150, majority = 76, href }) {
  const isSenate = kind === "senate";
  const hemi = hemicycle({
    total,
    majority,
    groups: [],
    centerLabel: "0",
    centerSub: `${majority} FOR CONTROL`,
    tooltip: (g) =>
      h(
        "div",
        null,
        h("div", { class: "legend__item" }, h("span", { class: "chip__swatch", style: { "--party": g.color } }), h("b", null, g.label)),
        h("div", { class: "muted" }, { called: isSenate ? "won tonight / decided" : "called", leading: "leading, not called", notup: "holdover (not up)", uncalled: "uncalled" }[g.style] || ""),
        g.party ? h("div", { class: "num" }, `${fmtInt(g.seats)} seats`) : null,
      ),
  });
  const status = h("div", { class: "lv-chamber__status" });
  const counts = h("div", { class: "lv-chamber__counts num" });
  const tbody = h("tbody");
  const table = h(
    "table",
    { class: "data lv-chamber__table" },
    h(
      "thead",
      null,
      h(
        "tr",
        null,
        h("th", null, "Party"),
        isSenate ? h("th", { class: "r", title: "Seats not up this election" }, "Hold") : null,
        h("th", { class: "r" }, isSenate ? "Won" : "Called"),
        h("th", { class: "r" }, "Lead"),
        h("th", { class: "r" }, "Total"),
        isSenate ? null : h("th", { class: "r", title: "Net change of decided seats vs the seats held going in" }, "Net"),
      ),
    ),
    tbody,
  );
  const el = h(
    "div",
    { class: `lv-chamber lv-chamber--${kind}` },
    h("div", { class: "lv-chamber__hemi" }, hemi),
    status,
    counts,
    h("div", { class: "table-wrap" }, table),
    h(
      "div",
      { class: "legend lv-chamber__legend" },
      h("span", { class: "legend__item" }, h("span", { class: "lv-dot lv-dot--solid" }), isSenate ? "Won / decided" : "Called"),
      h("span", { class: "legend__item" }, h("span", { class: "lv-dot lv-dot--light" }), "Leading"),
      isSenate ? h("span", { class: "legend__item" }, h("span", { class: "lv-dot lv-dot--ring" }), "Not up (holdover)") : null,
      h("span", { class: "legend__item" }, h("span", { class: "lv-dot lv-dot--grey" }), "Uncalled"),
    ),
    href ? h("a", { class: "lv-more", href }, `Full ${isSenate ? "Senate" : "House"} results →`) : null,
  );

  let tableKey = "";
  function update(d) {
    if (!d) return;
    const parties = (d.parties || [])
      .map((p) => ({ ...p, decided: (p.holdover || 0) + (p.called || 0) }))
      .filter((p) => p.decided + (p.leading || 0) > 0)
      .sort((a, b) => b.decided - a.decided || (b.leading || 0) - (a.leading || 0));
    const groups = [];
    for (const p of parties) {
      if (p.holdover) groups.push({ key: `${p.party}:h`, party: p.party, label: p.party, color: p.color, seats: p.holdover, style: "notup" });
      if (p.called) groups.push({ key: `${p.party}:c`, party: p.party, label: p.party, color: p.color, seats: p.called, style: "called" });
    }
    // leading seats after all decided seats keep the decided blocks contiguous from the left
    for (const p of parties) if (p.leading) groups.push({ key: `${p.party}:l`, party: p.party, label: p.party, color: p.color, seats: p.leading, style: "leading" });
    const top = parties[0];
    hemi.update({
      groups,
      centerLabel: top ? `${top.party} ${top.decided}` : "0",
      centerSub: `${majority} FOR CONTROL`,
      ariaLabel: `${isSenate ? "Senate" : "House"}: ${total} seats, ${majority} for control. ` + parties.map((p) => `${p.party} ${p.decided} decided${p.leading ? `, ${p.leading} leading` : ""}`).join("; "),
    });
    const sKey = `${d.control}|${d.controlLabel}|${d.final}`;
    if (status.dataset.key !== sKey) {
      status.dataset.key = sKey;
      mount(
        status,
        d.control
          ? h("span", { class: "lv-control lv-control--won", style: { "--party": parties.find((p) => p.party === d.control)?.color } }, icon("check", { size: 12 }), `${d.control} ${d.final ? "controls" : "wins control of"} the ${isSenate ? "Senate" : "House"}`)
          : h("span", { class: "lv-control" }, d.controlLabel || `No party has ${majority} ${isSenate ? "decided" : "called"} seats yet`),
      );
    }
    setText(
      counts,
      d.final
        ? `${fmtInt(total)} seats${isSenate && d.up !== undefined ? ` · ${fmtInt(d.up)} were up` : ""}`
        : `${fmtInt(d.called)} ${isSenate ? "won" : "called"} · ${fmtInt(d.leading)} leading · ${fmtInt(d.uncalled)} uncalled${isSenate && d.up !== undefined ? ` · ${fmtInt(d.up)} up` : ""}`,
    );
    const tk = JSON.stringify(parties.map((p) => [p.party, p.holdover, p.called, p.leading, p.net, p.color]));
    if (tk !== tableKey) {
      tableKey = tk;
      mount(
        tbody,
        parties.length
          ? parties.map((p) =>
              h(
                "tr",
                null,
                h("td", null, h("span", { class: "chip", style: { "--party": p.color } }, h("span", { class: "chip__swatch" }), p.party)),
                isSenate ? h("td", { class: "r muted" }, fmtInt(p.holdover || 0)) : null,
                h("td", { class: "r" }, fmtInt(p.called || 0)),
                h("td", { class: "r muted" }, p.leading ? fmtInt(p.leading) : "–"),
                h("td", { class: "r" }, h("b", null, fmtInt(p.decided + (p.leading || 0)))),
                isSenate ? null : h("td", { class: "r" }, p.net === null || p.net === undefined ? "–" : fmtSigned(p.net)),
              ),
            )
          : h("tr", null, h("td", { colspan: isSenate ? 5 : 5, class: "muted" }, "No seats decided yet")),
      );
    }
  }

  return { el, update };
}
