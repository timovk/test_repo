/**
 * Presidential ticket card: placeholder portrait (portrait_key), president + running mate, party,
 * electoral votes (decided + leading), national popular vote (votes, %), a per-ticket EV meter on
 * the 0–174 scale with the 88 marker, and winner / PV-leader markers.  `update(n)` changes the
 * numbers in place (no re-render), flashing values that changed.
 */
import { h } from "../dom.js";
import { fmtInt, fmtPct } from "../format.js";
import { portrait as avatar } from "./live-portrait.js";
import { icon } from "./icons.js";
import { flash, setText } from "./live-util.js";

/**
 * @param {{key,name,mate,party,partyName,color,portrait,incumbent}} t
 * @param {{total?:number, needed?:number, size?:'lg'|'md'}} opts
 */
export function ticketCard(t, { total = 174, needed = 88, size = "md" } = {}) {
  const evNum = h("span", { class: "lv-ticket__evnum" }, "0");
  const evLead = h("span", { class: "lv-ticket__evlead num" });
  const pvPct = h("span", { class: "lv-ticket__pvpct" }, "–");
  const pvVotes = h("span", { class: "lv-ticket__pvvotes num" }, "");
  const pvProj = h("span", { class: "lv-ticket__pvproj num" }, "");
  const segD = h("span", { class: "lv-meter__seg" });
  const segL = h("span", { class: "lv-meter__seg lv-meter__seg--lead" });
  const flags = h("span", { class: "lv-ticket__flags" });
  const meter = h(
    "div",
    { class: "lv-meter", "aria-hidden": "true" },
    segD,
    segL,
    h("span", { class: "lv-meter__tick", style: { left: `${(needed / total) * 100}%` } }),
  );
  const el = h(
    "article",
    { class: ["lv-ticket", size === "lg" && "lv-ticket--lg"], style: { "--party": t.color }, "aria-label": `${t.name}${t.mate ? ` and ${t.mate}` : ""}, ${t.party}` },
    h(
      "div",
      { class: "lv-ticket__top" },
      h("div", { class: "lv-ticket__avatar" }, avatar(t.name, { color: t.color, key: t.portrait || t.key, size: size === "lg" ? 64 : 52 })),
      h(
        "div",
        { class: "lv-ticket__who" },
        h("div", { class: "lv-ticket__name" }, t.name, flags),
        t.mate ? h("div", { class: "lv-ticket__mate" }, `with ${t.mate}`) : null,
        h(
          "div",
          { class: "lv-ticket__party" },
          h("span", { class: "chip__swatch" }),
          h("b", null, t.party || "IND"),
          t.partyName ? h("span", { class: "muted" }, ` · ${t.partyName}`) : null,
          t.incumbent ? h("span", { class: "lv-inc", title: "Incumbent" }, "INC") : null,
        ),
      ),
    ),
    h(
      "div",
      { class: "lv-ticket__nums" },
      h("div", { class: "lv-ticket__ev" }, evNum, h("span", { class: "lv-ticket__evlabel" }, "EV"), evLead),
      h("div", { class: "lv-ticket__pv" }, pvPct, pvVotes, pvProj),
    ),
    meter,
  );
  let last = {};
  el.update = (n = {}) => {
    const decided = n.decided ?? null;
    const leading = n.leading ?? 0;
    const txt = decided === null ? "–" : fmtInt(decided);
    if (evNum.textContent !== txt) {
      setText(evNum, txt);
      if (last.decided !== undefined) flash(evNum);
    }
    setText(evLead, leading > 0 ? `+${fmtInt(leading)} leading` : n.maxPossible !== undefined && n.maxPossible !== null && n.showMax ? `max ${fmtInt(n.maxPossible)}` : "");
    setText(pvPct, n.pct === null || n.pct === undefined ? "–" : fmtPct(n.pct));
    setText(pvVotes, n.votes === null || n.votes === undefined ? "No votes counted" : `${fmtInt(n.votes)} votes`);
    setText(pvProj, n.projectedPct !== null && n.projectedPct !== undefined ? `proj. ${fmtPct(n.projectedPct)}` : "");
    segD.style.width = `${((decided || 0) / total) * 100}%`;
    segL.style.width = `${(leading / total) * 100}%`;
    el.classList.toggle("is-winner", !!n.winner);
    el.classList.toggle("is-out", !!n.out);
    const fk = `${n.winner ? 1 : 0}${n.pvLeader ? 1 : 0}${n.finalist ? 1 : 0}`;
    if (flags.dataset.key !== fk) {
      flags.dataset.key = fk;
      flags.replaceChildren(
        ...[
          n.winner ? h("span", { class: "lv-flag lv-flag--win", title: n.winnerTitle || "Winner" }, icon("check", { size: 11 }), n.winnerLabel || "WINNER") : null,
          n.pvLeader ? h("span", { class: "lv-flag", title: "Most votes counted nationally" }, "PV LEAD") : null,
          n.finalist ? h("span", { class: "lv-flag", title: "Finalist in the contingent election" }, "FINALIST") : null,
        ].filter(Boolean),
      );
      if (n.pvLeader && last.pvLeader === false) flash(el, "lv-flash-card");
    }
    last = { decided, pvLeader: !!n.pvLeader };
    return el;
  };
  return el;
}
