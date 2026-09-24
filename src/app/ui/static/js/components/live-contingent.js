/**
 * Contingent election panel with the full audit trail (API `contingent` object of
 * /api/elections/{id}/president): rules in force, finalists (EV, popular vote), every House
 * ballot by province delegation (tile map of delegation votes + the member-vote breakdown
 * table), eliminations, and the Senate's Vice-President vote.  Display only — every tally,
 * requirement and outcome is an API value.
 */
import { h } from "../dom.js";
import { fmtInt } from "../format.js";
import { icon } from "./icons.js";
import { portrait } from "./live-portrait.js";
import { tileMap } from "./tilemap.js";
import { colorFor, decidedByLabel } from "./live-util.js";

const PROVINCE_NAMES = {
  GR: "Groningen", FR: "Fryslân", DR: "Drenthe", OV: "Overijssel", FL: "Flevoland", GE: "Gelderland",
  UT: "Utrecht", NH: "Noord-Holland", ZH: "Zuid-Holland", ZE: "Zeeland", NB: "Noord-Brabant", LI: "Limburg",
};

/**
 * @param {object} contingent API object
 * @param {{tickets:Array, provinces?:Array}} ctx  /president tickets (names, colours, running mates)
 */
export function contingentPanel(contingent, { tickets = [], provinces = [] } = {}) {
  const b = contingent.ballots || {};
  const cfg = b.config || {};
  const byKey = new Map(tickets.map((t) => [t.key, t]));
  const mateToTicket = new Map(tickets.filter((t) => t.running_mate).map((t) => [t.running_mate.key, t]));
  const provName = new Map(provinces.map((p) => [p.code, p.name]));
  const pname = (code) => provName.get(code) || PROVINCE_NAMES[code] || code;
  const nameOf = (k) => byKey.get(k)?.president?.name || mateToTicket.get(k)?.running_mate?.name || k;
  const colorOf = (k) => {
    const t = byKey.get(k) || mateToTicket.get(k);
    return t ? colorFor(t.party, t.color) : "var(--uncalled)";
  };
  const partyOf = (k) => (byKey.get(k) || mateToTicket.get(k))?.party || "";
  const chip = (k, extra) =>
    h("span", { class: "chip lv-cont__chip", style: { "--party": colorOf(k) } }, h("span", { class: "chip__swatch" }), nameOf(k), partyOf(k) ? h("span", { class: "muted" }, ` (${partyOf(k)})`) : null, extra || null);

  const finalists = b.finalists || contingent.finalists || [];
  const rules = [
    ["Body", decidedByLabel(b.rounds?.[0]?.body || "house_delegations")],
    ["Finalists", `Top ${cfg.finalists ?? finalists.length} by electoral votes`],
    ["Majority", `${b.rounds?.[0]?.required ?? "–"} of 12 delegations`],
    ["Max. ballots", cfg.max_ballots ?? "–"],
    ["Deadlock fallback", decidedByLabel(cfg.deadlock_fallback)],
    ["Vice President", cfg.vice_president_by_senate ? `Senate, top ${cfg.vice_president_finalists ?? 2}` : "–"],
  ];

  const finalistCards = h(
    "div",
    { class: "lv-cont__finalists" },
    finalists.map((k) => {
      const t = byKey.get(k);
      const won = k === (b.winner || contingent.winner);
      return h(
        "div",
        { class: ["lv-cont__finalist", won && "is-winner"], style: { "--party": colorOf(k) } },
        h("div", { class: "lv-cont__avatar" }, portrait(nameOf(k), { color: colorOf(k), key: t?.president?.portrait_key || k, size: 44 })),
        h(
          "div",
          null,
          h("div", { class: "lv-cont__fname" }, nameOf(k), won ? h("span", { class: "lv-flag lv-flag--win" }, icon("check", { size: 10 }), "ELECTED") : null),
          h("div", { class: "muted lv-cont__fmeta" }, `${partyOf(k)} · with ${t?.running_mate?.name || "–"}`),
          h(
            "div",
            { class: "lv-cont__fnums num" },
            h("span", null, h("b", null, fmtInt(b.finalist_ev?.[k])), " EV"),
            h("span", null, h("b", null, fmtInt(b.finalist_popular_votes?.[k])), " votes"),
          ),
        ),
      );
    }),
  );

  const rounds = (b.rounds || []).map((r) => roundBlock(r, { nameOf, colorOf, chip, pname, house: true }));
  const vp = b.vice_president;
  const vpBlock = vp
    ? h(
        "div",
        { class: "lv-cont__vp" },
        h("h3", { class: "lv-cont__h" }, "Vice President — ", decidedByLabel(vp.decided_by || vp.method)),
        h("p", { class: "muted lv-cont__p" }, `Finalists: the running mates of ${(vp.finalists || []).map(nameOf).join(" and ")}. Required: ${vp.required ?? "–"} senators.`),
        (vp.rounds || []).map((r) => roundBlock(r, { nameOf: (k) => nameOf(vp.candidates?.[k] || k), colorOf, chip: (k, extra) => chip(vp.candidates?.[k] || k, extra), pname, house: false, candidateMap: vp.candidates })),
        h("p", { class: "lv-cont__outcome" }, icon("check", { size: 14 }), " ", h("b", null, nameOf(vp.vice_president)), ` elected Vice President (running mate of ${nameOf(vp.winner_line)}).`),
      )
    : null;

  const winner = b.winner || contingent.winner;
  return h(
    "div",
    { class: "lv-cont" },
    h(
      "div",
      { class: "lv-cont__outcome lv-cont__outcome--lead", style: { "--party": colorOf(winner) } },
      icon("president", { size: 18 }),
      h(
        "span",
        null,
        h("b", null, nameOf(winner)),
        ` (${partyOf(winner)}) was elected President by the House on ballot ${b.ballots ?? contingent.rounds ?? "–"}` +
          `${contingent.vice_president?.name ? `; ${contingent.vice_president.name} became Vice President.` : "."}` +
          ` Outcome: ${String(contingent.outcome || b.outcome || "").replace(/_/g, " ")}.`,
      ),
    ),
    h(
      "p",
      { class: "lv-cont__p" },
      "No ticket reached the Electoral College majority, so the constitution's contingent procedure applied: the House votes by province delegation — each province casts one vote, decided by a majority of its House members; a delegation without a majority is ",
      h("b", null, "divided"),
      " and casts no vote. The last-placed finalist is eliminated after an inconclusive ballot.",
    ),
    h("dl", { class: "lv-cont__rules" }, rules.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, String(v))))),
    h("h3", { class: "lv-cont__h" }, "Finalists"),
    finalistCards,
    rounds,
    vpBlock,
  );
}

function roundBlock(r, { nameOf, colorOf, chip, pname, house, candidateMap }) {
  const cands = r.candidates || [];
  const delegations = r.delegations || {};
  const breakdown = r.delegation_breakdown || {};
  const mapKey = (k) => (candidateMap ? candidateMap[k] || k : k);
  const tallies = h(
    "div",
    { class: "lv-round__tallies" },
    cands.map((k) =>
      h(
        "div",
        { class: ["lv-round__tally", r.winner === k && "is-winner", r.eliminated === k && "is-out"], style: { "--party": colorOf(mapKey(k)) } },
        chip(k),
        h(
          "span",
          { class: "num lv-round__tallyn" },
          house ? h("b", null, `${fmtInt(r.tallies?.[k])} deleg.`) : h("b", null, `${fmtInt(r.tallies?.[k])} votes`),
          house ? h("span", { class: "muted" }, ` · ${fmtInt(r.member_votes?.[k])} members`) : null,
        ),
        r.winner === k ? h("span", { class: "lv-flag lv-flag--win" }, icon("check", { size: 10 }), "WINS") : null,
        r.eliminated === k ? h("span", { class: "lv-flag" }, "ELIMINATED") : null,
      ),
    ),
    house && (r.divided || []).length ? h("div", { class: "lv-round__tally lv-round__tally--div" }, h("span", { class: "lv-swatch lv-swatch--uncalled" }), h("span", null, h("b", null, `${r.divided.length} divided`), h("span", { class: "muted" }, ` · ${r.divided.map(pname).join(", ")}`))) : null,
  );
  let body = null;
  if (house && Object.keys(delegations).length) {
    const tiles = Object.keys(delegations).map((code) => {
      const v = delegations[code];
      return {
        code,
        name: pname(code),
        ev: null,
        state: v ? "called" : "uncalled",
        color: v ? colorOf(v) : undefined,
        sub: v ? String(nameOf(v)).split(" ").slice(-1)[0] : "DIVIDED",
        title: `${pname(code)} delegation: ${v ? `votes for ${nameOf(v)}` : "divided (no majority), casts no vote"}`,
      };
    });
    const tipFor = (p) => {
      const bd = breakdown[p.code] || {};
      return h(
        "div",
        { class: "lv-tip" },
        h("strong", null, `${p.name} delegation`),
        h("div", null, delegations[p.code] ? chip(delegations[p.code]) : h("span", { class: "muted" }, "Divided — no majority")),
        h(
          "div",
          { class: "lv-tip__grid num" },
          Object.entries(bd)
            .sort((a, b2) => b2[1] - a[1])
            .map(([k, n]) => [h("span", { class: "muted" }, nameOf(k)), h("b", null, `${n}`)]),
        ),
      );
    };
    const tm = tileMap(tiles, (code) => (location.hash = `#/provinces/${code}`), { tooltip: tipFor, compact: true, label: `Ballot ${r.number}: delegation votes by province` });
    const rows = Object.keys(delegations).sort((a, c) => pname(a).localeCompare(pname(c), "nl"));
    const table = h(
      "div",
      { class: "table-wrap lv-round__table" },
      h(
        "table",
        { class: "data" },
        h("thead", null, h("tr", null, h("th", null, "Province"), h("th", null, "Delegation vote"), cands.map((k) => h("th", { class: "r", title: nameOf(k) }, String(nameOf(k)).split(" ").slice(-1)[0])))),
        h(
          "tbody",
          null,
          rows.map((code) =>
            h(
              "tr",
              null,
              h("td", null, h("a", { href: `#/provinces/${code}` }, pname(code))),
              h("td", null, delegations[code] ? chip(delegations[code]) : h("span", { class: "muted" }, "Divided")),
              cands.map((k) => h("td", { class: ["r", delegations[code] === k && "lv-strong"] }, breakdown[code]?.[k] ? fmtInt(breakdown[code][k]) : "–")),
            ),
          ),
        ),
      ),
    );
    body = h("div", { class: "lv-round__body" }, h("div", { class: "lv-round__map" }, tm), table);
  }
  return h(
    "section",
    { class: "lv-round" },
    h(
      "div",
      { class: "lv-round__head" },
      h("span", { class: "lv-round__num" }, `Ballot ${r.number}`),
      h("span", { class: "muted" }, `${decidedByLabel(r.body)} · ${fmtInt(r.required)} required`),
      h("span", { class: "lv-round__note" }, String(r.note || "").replace(/\b([a-z]+(?:-[a-z0-9]+)+)\b/g, (k) => nameOf(mapKey(k)) || k)),
    ),
    tallies,
    body,
  );
}
