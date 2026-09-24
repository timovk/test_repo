/**
 * #/candidates/:id — profile and career of a FICTIONAL person: placeholder portrait, party,
 * offices held, attributes, and every candidacy with its (SIMULATED) result — results of
 * elections that are not reported yet stay hidden.  Includes a quick candidate search.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { fmtInt, fmtPct, fmt2 } from "../format.js";
import { card, links, pageHeader } from "./_shared.js";
import { dataTable } from "../components/table.js";
import { portrait } from "../components/ana-avatar.js";
import { icon } from "../components/icons.js";
import { callout, errorBox, fmtDate, humanize, kpi, kv, partyInfo, pchip, provinceName, refresh, sourcePill, statusBadge } from "../components/ana-ui.js";

export async function render(el, params) {
  const id = Number(params.id);
  const searchHost = searchBox();
  const bodyHost = h("div");
  mount(el, pageHeader({ eyebrow: "Candidate profile", title: "Candidate", categories: ["FICTIONAL"], meta: [searchHost] }), bodyHost);
  if (!Number.isFinite(id)) {
    mount(bodyHost, errorBox(new Error("Unknown candidate id")));
    return;
  }
  await refresh(bodyHost, Promise.all([api.get(`/api/candidates/${id}`), api.get("/api/municipalities", { cache: true }).catch(() => null)]), ([c, munis]) => {
    const t = el.querySelector(".page-head__title");
    if (t) t.textContent = c.name;
    document.title = `${c.name} · Candidate · NL Federal Election Simulator`;
    return profile(c, munis);
  });
}

function searchBox() {
  const list = h("div", { class: "ana-csearch__list", role: "listbox", hidden: true });
  let timer = null;
  let ctrl = null;
  const input = h("input", {
    class: "input",
    type: "search",
    placeholder: "Find a candidate…",
    "aria-label": "Search candidates",
    autocomplete: "off",
    oninput: () => {
      clearTimeout(timer);
      timer = setTimeout(run, 220);
    },
    onkeydown: (e) => {
      if (e.key === "Escape") list.hidden = true;
      if (e.key === "ArrowDown") list.querySelector("a")?.focus();
    },
  });
  async function run() {
    const q = input.value.trim();
    if (q.length < 2) {
      list.hidden = true;
      return;
    }
    ctrl?.abort();
    ctrl = new AbortController();
    try {
      const d = await api.get(`/api/candidates?search=${encodeURIComponent(q)}&limit=8`, { signal: ctrl.signal });
      mount(
        list,
        d.candidates.length
          ? d.candidates.map((c) =>
              h(
                "a",
                { class: "ana-csearch__item", role: "option", href: links.candidate(c.id), onclick: () => (list.hidden = true) },
                h("span", { class: "ana-csearch__av" }, portrait(c.name, { color: partyInfo(c.party, c.color).color, key: c.portrait_key || c.key, size: 26 })),
                h("span", { class: "ana-csearch__name" }, c.name),
                pchip(c.party, c.color),
                h("span", { class: "muted" }, (c.offices || []).join(", ")),
              ),
            )
          : h("div", { class: "ana-csearch__empty" }, "No candidates found"),
      );
      list.hidden = false;
    } catch (e) {
      if (e.name !== "AbortError") list.hidden = true;
    }
  }
  return h("div", { class: "ana-csearch" }, input, list);
}

function meter(label, value, { min = 0, max = 1, fmt = (v) => fmt2(v), note } = {}) {
  if (value === null || value === undefined) return h("div", { class: "ana-meter" }, h("div", { class: "ana-meter__top" }, h("span", null, label), h("span", { class: "muted" }, "–")));
  const t = Math.max(0, Math.min(1, (value - min) / (max - min || 1)));
  return h(
    "div",
    { class: "ana-meter", title: note || "" },
    h("div", { class: "ana-meter__top" }, h("span", null, label), h("b", { class: "num" }, fmt(value))),
    h("div", { class: "ana-meter__track" }, h("div", { class: "ana-meter__fill", style: { width: `${t * 100}%` } })),
  );
}

function axis(label, value, left, right) {
  if (value === null || value === undefined) return null;
  const t = (Math.max(-1, Math.min(1, value)) + 1) / 2;
  return h(
    "div",
    { class: "ana-axis", role: "img", "aria-label": `${label}: ${value} on a scale from −1 (${left}) to +1 (${right})` },
    h("div", { class: "ana-axis__label" }, label),
    h("div", { class: "ana-axis__track" }, h("span", { class: "ana-axis__mid" }), h("span", { class: "ana-axis__dot", style: { left: `${t * 100}%` } })),
    h("div", { class: "ana-axis__ends" }, h("span", null, left), h("b", { class: "num" }, value > 0 ? `+${value}` : String(value)), h("span", null, right)),
  );
}

function profile(c, munis) {
  const party = partyInfo(c.party);
  const muni = munis?.municipalities?.find((m) => m.code === c.home_municipality);
  const serving = (c.offices || []).filter((o) => o.serving);

  const hero = h(
    "section",
    { class: "card ana-cand", style: { "--party": party.color } },
    h("div", { class: "ana-cand__portrait" }, portrait(c.name, { color: party.color, key: c.portrait_key || c.key, size: 132 }), h("div", { class: "ana-cand__ph" }, "Generated placeholder")),
    h(
      "div",
      { class: "ana-cand__main" },
      h("div", { class: "ana-row" }, pchip(c.party), h("span", { class: "secondary" }, party.name), ...serving.map((o) => h("span", { class: "pill pill--final", style: { "--party": party.color } }, icon("check", { size: 11, className: "pill__icon" }), o.office_name))),
      h("h2", { class: "ana-cand__name" }, c.name),
      c.bio ? h("p", { class: "ana-cand__bio" }, c.bio) : null,
      kv(
        [
          ["Born", fmtDate(c.birth_date)],
          ["Gender", c.gender === "F" ? "Female" : c.gender === "M" ? "Male" : c.gender || "–"],
          ["Home municipality", c.home_municipality ? h("a", { class: "ana-link", href: links.municipality(c.home_municipality) }, muni?.name || c.home_municipality) : "–"],
          ["Home province", c.home_province ? h("a", { class: "ana-link", href: links.province(c.home_province) }, provinceName(c.home_province)) : "–"],
          ["Affiliation", (c.affiliations || []).map((a) => `${a.party} ${a.from_year}–${a.to_year ?? "present"}`).join(", ") || "–"],
          ["Record key", h("span", { class: "mono" }, c.key)],
        ],
        { cols: 2 },
      ),
    ),
  );

  const s = c.summary || {};
  const stats = h(
    "div",
    { class: "ana-kpis" },
    kpi("Candidacies", fmtInt(s.candidacies), "ballot lines, all elections"),
    kpi("Won", fmtInt(s.wins), "decided races"),
    kpi("Lost", fmtInt(s.losses), "decided races"),
    kpi("Offices held", fmtInt(s.offices_held), serving.length ? `serving: ${serving.map((o) => o.office).join(", ")}` : "not serving"),
  );

  const attrs = card(
    "Model attributes",
    h(
      "div",
      { class: "ana-stack-v" },
      h(
        "div",
        { class: "ana-meters" },
        meter("Candidate quality", c.quality, { min: -1, max: 1.5, note: "Scenario input: electoral appeal beyond the party" }),
        meter("Campaign strength", c.campaign_strength, { min: 0, max: 1.5 }),
        meter("Fundraising", c.fundraising, { min: 0, max: 2 }),
        meter("Favourability", c.favorability, { min: -10, max: 10, fmt: (v) => `${v > 0 ? "+" : ""}${v}` }),
        meter("Approval", c.approval, { min: 0, max: 100, fmt: (v) => `${v}%` }),
      ),
      c.ideology
        ? h(
            "div",
            { class: "ana-axes" },
            axis("Economic", c.ideology.economic, "left", "right"),
            axis("Social", c.ideology.social, "progressive", "conservative"),
            axis("Europe", c.ideology.europe, "sceptic", "pro-EU"),
          )
        : null,
    ),
    { categories: ["FICTIONAL"], foot: "Scenario parameters of this fictional person; they feed the simulation model." },
  );

  const offices = card(
    `Offices (${(c.offices || []).length})`,
    (c.offices || []).length
      ? dataTable(
          [
            { key: "office_name", label: "Office", format: (v, r) => h("span", null, h("b", null, v), h("span", { class: "muted mono" }, `  ${r.office}`)) },
            { key: "party", label: "Party", format: (v) => pchip(v) },
            { key: "term_start", label: "Term", format: (v, r) => `${fmtDate(v)} – ${fmtDate(r.ended_on || r.term_end)}` },
            { key: "serving", label: "Status", format: (v, r) => (v ? statusBadge("ok", "Serving") : h("span", { class: "muted" }, humanize(r.end_reason) || "Ended")) },
            { key: "start_reason", label: "Took office", format: (v, r) => `${humanize(v)}${r.elected_in ? ` (${r.elected_in})` : ""}` },
          ],
          c.offices,
          { sortKey: "term_start" },
        )
      : h("div", { class: "state" }, "Has not held office"),
    { flush: true, categories: ["SIMULATED"] },
  );

  // Career: group candidacies by election; province EV contests nest under the national PRES race.
  const byElection = new Map();
  for (const cd of c.candidacies || []) {
    const k = cd.election.id;
    if (!byElection.has(k)) byElection.set(k, { election: cd.election, main: [], sub: [] });
    (/^PRES-/.test(cd.race_code) ? byElection.get(k).sub : byElection.get(k).main).push(cd);
  }
  const career = [...byElection.values()].sort((a, b) => b.election.year - a.election.year);
  const raceHref = (cd) => `${links.race(cd.race_code)}?e=${cd.election.id}`;
  const resultCell = (cd) => {
    const r = cd.result;
    if (!r) return h("span", { class: "ana-hidden", title: "Result hidden until the election is reported" }, icon("lock", { size: 11 }), cd.election.reported ? "–" : "hidden");
    return h(
      "span",
      { class: "ana-inl" },
      r.won ? statusBadge("ok", "Won") : h("span", { class: "ana-lost" }, "Lost"),
      h("b", { class: "num" }, fmtPct(r.pct)),
      h("span", { class: "muted num" }, `${fmtInt(r.votes)} votes · margin ${fmt2(r.margin_pp)} pp`),
    );
  };
  const personLink = (p) => (p ? (p.id ? h("a", { class: "ana-link", href: links.candidate(p.id) }, p.name) : p.name) : null);
  const subToggle = (g) => {
    const collapsed = g.main.length > 0;
    const btn = h(
      "button",
      {
        type: "button",
        class: "ana-linkbtn",
        "aria-expanded": collapsed ? "false" : "true",
        onclick: (e) => {
          const tr = e.currentTarget.closest("tr");
          const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
          e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
          e.currentTarget.textContent = `${open ? "Hide" : "Show"} province Electoral College contests (${g.sub.length})`;
          let n = tr.nextElementSibling;
          while (n && n.classList.contains("ana-career__sub")) {
            n.hidden = !open;
            n = n.nextElementSibling;
          }
        },
      },
      `${collapsed ? "Show" : "Hide"} province Electoral College contests (${g.sub.length})`,
    );
    return h("tr", null, h("td", { colspan: 6, class: "ana-career__subhead" }, btn));
  };
  const row = (cd, sub = false, hidden = false) =>
    h(
      "tr",
      { class: sub ? "ana-career__sub" : "", hidden: sub && hidden },
      h("td", null, h("a", { class: "ana-link mono", href: raceHref(cd) }, cd.race_code)),
      h("td", { class: "ana-wrap" }, cd.race_name, cd.incumbent ? h("span", { class: "ana-tag" }, "incumbent") : null),
      h("td", null, humanize(cd.role)),
      h("td", null, pchip(cd.party, cd.color)),
      h("td", { class: "ana-wrap" }, cd.running_mate ? h("span", null, "with ", personLink(cd.running_mate)) : cd.ticket_leader ? h("span", null, "running mate of ", personLink(cd.ticket_leader)) : h("span", { class: "muted" }, "–")),
      h("td", null, resultCell(cd)),
    );
  const careerBody = career.length
    ? h(
        "div",
        { class: "ana-career" },
        career.map((g) =>
          h(
            "section",
            { class: "ana-career__el" },
            h("div", { class: "ana-career__head" }, h("span", { class: "ana-career__year" }, String(g.election.year)), h("span", { class: "ana-career__name" }, g.election.name), sourcePill(g.election.results_source)),
            h(
              "div",
              { class: "table-wrap" },
              h(
                "table",
                { class: "data" },
                h("thead", null, h("tr", null, ["Race", "Contest", "Role", "Party", "Ticket", "Result"].map((x) => h("th", null, x)))),
                h("tbody", null, g.main.map((cd) => row(cd)), g.sub.length ? subToggle(g) : null, g.sub.map((cd) => row(cd, true, g.main.length > 0))),
              ),
            ),
          ),
        ),
      )
    : h("div", { class: "state" }, "No candidacies recorded");

  return h(
    "div",
    { class: "ana-stack-v" },
    callout("sim", h("strong", null, "Fictional person. "), "This candidate is invented by the simulator's scenario; the portrait is a generated placeholder and every vote is SIMULATED. Any resemblance to a real person is coincidental."),
    hero,
    stats,
    h("div", { class: "grid grid--2" }, attrs, offices),
    card("Career — elections contested", careerBody, { flush: true, categories: ["FICTIONAL", "SIMULATED"], foot: "Results of elections that are not reported yet are hidden (they appear during the election night or once the election is final)." }),
  );
}
