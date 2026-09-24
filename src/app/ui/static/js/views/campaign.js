/**
 * #/campaign — FICTIONAL campaign plans and their SIMULATED effects: budget, strategy and
 * allocations per party (by action type, by week, by target), a map + table of where the
 * resources went, realised effects (revealed only once the election is reported — hidden before
 * and during the night), and a what-if re-plan preview (POST /plan, never stored).
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { subscribe } from "../store.js";
import { fmtInt, fmt1, fmt2 } from "../format.js";
import { card, currentElectionId, electionPicker, links, pageHeader } from "./_shared.js";
import { dataTable, barCell } from "../components/table.js";
import { electionMap } from "../components/map.js";
import { sequential } from "../components/colorscale.js";
import { icon } from "../components/icons.js";
import { callout, errorBox, humanize, kpi, partyInfo, pchip, refresh, segmented, selectBox, sortByPartyOrder, toast, field } from "../components/ana-ui.js";
import { columns, hbars, scatterChart, seqCell, tipBox, tipRow } from "../components/ana-charts.js";

const STRATEGIES = ["balanced", "battleground", "base", "expansion"];
const fx = (v) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(3)}`);

export async function render(el, _params, ctx) {
  const id = ctx.electionId || currentElectionId();
  const st = { party: ctx.query.party || null, level: ctx.query.level || "province", data: null, map: null };
  const cleanups = [];
  const bodyHost = h("div");
  mount(
    el,
    pageHeader({ eyebrow: "Analysis · Campaigns", title: "Campaign", categories: ["FICTIONAL", "SIMULATED"], meta: [electionPicker()] }),
    bodyHost,
  );
  if (!id) return;

  const destroyMap = () => {
    if (st.map) {
      try {
        st.map.destroy();
      } catch {
        /* ignore */
      }
      st.map = null;
    }
  };
  cleanups.push(destroyMap);

  async function load() {
    destroyMap();
    await refresh(bodyHost, api.get(`/api/campaigns/${id}?all_targets=true`), (d) => {
      st.data = d;
      return build(d);
    });
  }

  function build(d) {
    const camps = [...d.campaigns].sort((a, b) => b.budget - a.budget);
    if (!camps.length) return h("div", { class: "state ana-empty" }, h("strong", null, "No campaigns for this election"), h("span", { class: "muted" }, "Campaign plans are generated when an election is set up from a scenario."));
    if (!st.party || !camps.some((c) => c.party === st.party)) st.party = camps[0].party;
    const color = (c) => partyInfo(c.party, c.color).color;
    const revealed = d.effects_revealed;
    const units = d.units || {};

    const notice = revealed
      ? callout("ok", h("strong", null, "Realised effects revealed. "), `This election is reported, so the simulated effect of every allocation on the result is shown next to the plan's expected effect (units: ${units.effects || "logit points"}).`)
      : callout("warn", h("strong", null, "Realised effects are hidden until the election is reported. "), `They are inputs of the hidden result (${(d.hidden_fields || []).join(", ")}). Plans and expected effects are always visible. ${d.notice || ""}`);

    const cards = h(
      "div",
      { class: "ana-camp__cards", role: "list" },
      camps.map((c) =>
        h(
          "button",
          { type: "button", role: "listitem", class: ["ana-camp", c.party === st.party && "is-active"], style: { "--party": color(c) }, "aria-pressed": c.party === st.party ? "true" : "false", onclick: () => { st.party = c.party; setQuery({ party: c.party }); mount(detailHost, detail(c)); cards.querySelectorAll(".ana-camp").forEach((b) => { const on = b.dataset.p === c.party; b.classList.toggle("is-active", on); b.setAttribute("aria-pressed", on ? "true" : "false"); }); }, "data-p": c.party },
          h("div", { class: "ana-camp__top" }, pchip(c.party, c.color), h("span", { class: "ana-camp__strategy" }, humanize(c.strategy))),
          h("div", { class: "ana-camp__name" }, c.party_name),
          h("div", { class: "ana-camp__ticket" }, c.ticket || "No presidential ticket"),
          h("div", { class: "ana-camp__budget" }, h("span", { class: "ana-camp__big" }, fmt1(c.budget)), h("span", { class: "muted" }, " budget units")),
          h("div", { class: "ana-camp__meta" }, `${fmt1(c.spend)} spent · ${fmtInt(c.allocations)} allocations · ${c.targets_total} targets`),
        ),
      ),
    );

    // Party × action heat table (sequential, one hue)
    const actions = [...new Set(camps.flatMap((c) => c.by_action.map((a) => a.action)))];
    const maxAct = Math.max(...camps.flatMap((c) => c.by_action.map((a) => a.amount)), 1e-9);
    const mixTable = dataTable(
      [
        { key: "party", label: "Party", format: (v, r) => pchip(v, r.color) },
        { key: "strategy", label: "Strategy", format: (v) => humanize(v) },
        { key: "budget", label: "Budget", align: "r", format: (v) => fmt1(v) },
        ...actions.map((a) => ({ key: `a_${a}`, label: humanize(a), align: "r", value: (r) => r.by_action.find((x) => x.action === a)?.amount ?? null, format: (v) => (v == null ? h("span", { class: "muted" }, "–") : seqCell(v, maxAct, (x) => fmt1(x))) })),
      ],
      camps,
      { sortKey: "budget" },
    );

    const budgetBars = hbars({
      rows: sortByPartyOrder(camps.map((c) => c.party)).map((p) => camps.find((c) => c.party === p)).map((c) => ({ key: c.party, label: c.party, labelNode: pchip(c.party, c.color), value: c.budget, color: color(c), tip: tipBox(c.party_name, [tipRow(color(c), "Budget", fmt1(c.budget)), tipRow(null, "Spend", fmt1(c.spend)), tipRow(null, "Fundraising proceeds", fmt1(c.fundraising_proceeds)), tipRow(null, "Strategy", humanize(c.strategy))]) })),
      format: (v) => fmt1(v),
      labelWidth: 64,
      ariaLabel: "Campaign budget per party",
    });

    const detailHost = h("div");
    mount(detailHost, detail(camps.find((c) => c.party === st.party)));

    return h(
      "div",
      { class: "ana-stack-v" },
      notice,
      cards,
      h(
        "div",
        { class: "ana-split" },
        card("Spend by action type (resource units)", mixTable, { flush: true, categories: ["FICTIONAL"], foot: `Amounts in ${units.amount || "abstract resource units"}; darker = more.` }),
        card("Budget by party", budgetBars, { categories: ["FICTIONAL"] }),
      ),
      detailHost,
      replanCard(camps),
    );
  }

  function detail(c) {
    destroyMap();
    const d = st.data;
    const revealed = d.effects_revealed;
    const col = partyInfo(c.party, c.color).color;
    const byAction = hbars({ rows: [...c.by_action].sort((a, b) => b.amount - a.amount).map((a) => ({ key: a.action, label: humanize(a.action), value: a.amount, color: col, tip: tipBox(humanize(a.action), [tipRow(col, "Amount", fmt1(a.amount)), tipRow(null, "Allocations", fmtInt(a.allocations)), a.units ? tipRow(null, "Events", fmtInt(a.units)) : null].filter(Boolean)) })), format: (v) => fmt1(v), labelWidth: 110, ariaLabel: `${c.party} spend by action type` });
    const byWeek = columns({ bins: c.by_week.map((w) => ({ label: `W${w.week}`, y: w.amount, tip: tipBox(`Week ${w.week}`, [tipRow(col, "Amount", fmt1(w.amount))]) })), color: col, yFormat: (v) => fmt1(v), height: 190, ariaLabel: `${c.party} spend per campaign week` });

    const targets = c.targets || [];
    const national = targets.filter((t) => t.level === "national");
    const levels = ["province", "district"].filter((l) => targets.some((t) => t.level === l));
    if (!levels.includes(st.level)) st.level = levels[0] || "province";
    const mapHost = h("div", { class: "ana-camp__map" });
    const maxAmt = Math.max(...targets.map((t) => t.amount), 1e-9);
    const tableRows = targets.map((t) => ({ ...t, topActions: Object.entries(t.actions || {}).sort((a, b) => b[1] - a[1]) }));
    const table = dataTable(
      [
        { key: "level", label: "Level", format: (v) => humanize(v) },
        { key: "name", label: "Target", format: (v, r) => (r.level === "province" ? h("a", { class: "ana-link", href: links.province(r.code) }, v) : r.level === "district" ? h("a", { class: "ana-link", href: links.district(r.code) }, `${r.code} · ${v}`) : v) },
        { key: "amount", label: "Amount", format: (v) => barCell(v / maxAmt, col, fmt1(v)) },
        { key: "topActions", label: "Main actions", sort: false, format: (v) => h("span", { class: "muted" }, v.slice(0, 3).map(([a, x]) => `${humanize(a)} ${fmt1(x)}`).join(" · ")) },
        { key: "expected_effect", label: "Expected effect", align: "r", format: fx },
        { key: "realized_effect", label: "Realised effect", align: "r", format: (v) => (revealed ? fx(v) : hiddenCell()) },
        { key: "turnout_effect", label: "Turnout effect", align: "r", format: (v) => (revealed ? fx(v) : hiddenCell()) },
      ],
      tableRows,
      { sortKey: "amount", maxHeight: 460 },
    );

    const mapCard = card(
      "Where the resources went",
      h(
        "div",
        { class: "ana-stack-v" },
        h(
          "div",
          { class: "ana-row ana-row--between" },
          levels.length > 1 ? segmented(levels.map((l) => ({ value: l, label: humanize(l) + "s" })), st.level, (v) => { st.level = v; setQuery({ level: v }); drawMap(); }, { label: "Map level" }) : h("span"),
          h("div", { class: "ana-scale" }, h("span", null, "Amount"), h("span", { class: "ana-scale__ramp" }, [1, 2, 3, 4, 5, 6].map((i) => h("span", { style: { background: `var(--seq-${i})` } }))), h("span", null, `0 → ${fmt1(maxAmt)}`)),
        ),
        mapHost,
        national.length ? h("div", { class: "ana-note" }, `Plus national (untargeted) spend: ${national.map((t) => fmt1(t.amount)).join(", ")} units.`) : null,
      ),
      { categories: ["REAL", "FICTIONAL"], foot: "Boundaries: CBS/PDOK (REAL); districts FICTIONAL; allocations FICTIONAL. Grey = not targeted. The table below is the accessible equivalent." },
    );

    function drawMap() {
      destroyMap();
      mount(mapHost);
      const lvlTargets = Object.fromEntries(targets.filter((t) => t.level === st.level).map((t) => [t.code, t]));
      const lvlMax = Math.max(...Object.values(lvlTargets).map((t) => t.amount), 1e-9);
      try {
        st.map = electionMap(mapHost, {
          layer: st.level === "district" ? "districts" : "provinces",
          height: 460,
          outline: st.level === "district" ? "provinces" : null,
          style: (f) => {
            const t = lvlTargets[f.properties.code];
            return { fillColor: t ? sequential(t.amount, [0, lvlMax]) : getComputedStyle(document.documentElement).getPropertyValue("--uncalled-soft").trim(), fillOpacity: 0.95 };
          },
          tooltip: (f) => {
            const t = lvlTargets[f.properties.code];
            return h(
              "div",
              { class: "ana-tip" },
              h("div", { class: "ana-tip__title" }, `${f.properties.code} · ${f.properties.name}`),
              t
                ? [tipRow(col, "Amount", fmt1(t.amount)), tipRow(null, "Expected effect", fx(t.expected_effect)), tipRow(null, "Realised effect", revealed ? fx(t.realized_effect) : "hidden")]
                : h("div", { class: "muted" }, `Not targeted by ${c.party}`),
            );
          },
          onClick: (f) => (location.hash = st.level === "district" ? links.district(f.properties.code) : links.province(f.properties.code)),
        });
        st.map.el.setAttribute("role", "img");
        st.map.el.setAttribute("aria-label", `Map of ${c.party} campaign spending by ${st.level}`);
      } catch (e) {
        mount(mapHost, errorBox(e));
      }
    }
    requestAnimationFrame(drawMap);

    const scatter =
      revealed && targets.some((t) => t.realized_effect != null)
        ? card(
            "Expected vs. realised effect",
            scatterChart({
              points: targets.filter((t) => t.realized_effect != null).map((t) => ({ x: t.expected_effect, y: t.realized_effect, label: t.name, short: t.code, color: col, amount: t.amount })),
              xLabel: "Expected effect (logit)",
              yLabel: "Realised effect (logit)",
              xFormat: (v) => v.toFixed(2),
              yFormat: (v) => v.toFixed(2),
              diagonal: true,
              diagonalLabel: "as planned",
              height: 300,
              ariaLabel: `${c.party}: expected versus realised campaign effect per target`,
              tip: (p) => tipBox(p.label, [tipRow(col, "Expected", fx(p.x)), tipRow(null, "Realised", fx(p.y)), tipRow(null, "Amount", fmt1(p.amount))]),
            }),
            { categories: ["SIMULATED"], foot: "Points above the line did better than planned." },
          )
        : null;

    return h(
      "section",
      { class: "ana-stack-v", "aria-label": `${c.party} campaign` },
      h(
        "div",
        { class: "ana-section-title" },
        h("h2", { class: "ana-row" }, pchip(c.party, c.color), c.party_name),
        h("p", null, `${humanize(c.strategy)} strategy · seed ${c.seed}`),
      ),
      h(
        "div",
        { class: "ana-kpis" },
        kpi("Budget", fmt1(c.budget), "resource units", { accent: col }),
        kpi("Spend", fmt1(c.spend), `incl. ${fmt2(c.fundraising_proceeds)} fundraising proceeds`),
        kpi("Allocations", fmtInt(c.allocations), `${c.targets_total} targets`),
        kpi("Ticket", h("span", { class: "ana-kpi__text" }, c.ticket || "–"), "presidential ticket"),
      ),
      h("div", { class: "grid grid--2" }, card("Allocations by action type", byAction, { categories: ["FICTIONAL"] }), card("Spend by campaign week", byWeek, { categories: ["FICTIONAL"] })),
      h("div", { class: scatter ? "ana-split ana-split--wide" : "" }, mapCard, scatter),
      card(`Targets (${targets.length})`, table, { flush: true, categories: revealed ? ["FICTIONAL", "SIMULATED"] : ["FICTIONAL"], foot: `Effects in ${d.units?.effects || "logit points of party utility"}.${revealed ? "" : " Realised effects: hidden until reported."}` }),
    );
  }

  function hiddenCell() {
    return h("span", { class: "ana-hidden", title: "Hidden until the election is reported" }, icon("lock", { size: 11 }), "hidden");
  }

  /* -------------------------------------------------------------- what-if */
  function replanCard(camps) {
    const inputs = {};
    const rows = sortByPartyOrder(camps.map((c) => c.party)).map((p) => {
      const c = camps.find((x) => x.party === p);
      const budget = h("input", { class: "input input--sm", type: "number", min: 0, step: 1, value: c.budget, "aria-label": `${p} budget` });
      const strat = selectBox(STRATEGIES.map((s) => ({ value: s, label: humanize(s) })), c.strategy, () => {}, { "aria-label": `${p} strategy` });
      inputs[p] = { budget, strat, c };
      return h("div", { class: "ana-replan__row" }, pchip(p, c.color), budget, strat);
    });
    const seed = h("input", { class: "input", type: "number", min: 0, step: 1, placeholder: "stored seed" });
    const out = h("div", { "aria-live": "polite" });
    const btn = h("button", { class: "btn btn--primary", type: "button" }, "Preview re-plan");
    btn.onclick = async () => {
      const budgets = {};
      const strategies = {};
      for (const [p, i] of Object.entries(inputs)) {
        if (Number(i.budget.value) !== i.c.budget) budgets[p] = Number(i.budget.value);
        if (i.strat.value !== i.c.strategy) strategies[p] = i.strat.value;
      }
      const body = {};
      if (Object.keys(budgets).length) body.budgets = budgets;
      if (Object.keys(strategies).length) body.strategies = strategies;
      if (seed.value) body.seed = Number(seed.value);
      btn.disabled = true;
      await refresh(out, api.post(`/api/campaigns/${id}/plan`, body), (res) => previewTable(res, camps));
      btn.disabled = false;
    };
    return card(
      "What-if re-plan (preview, not stored)",
      h(
        "div",
        { class: "ana-stack-v" },
        h("p", { class: "ana-sub" }, "Change budgets or strategies and preview how the campaign planner would allocate resources. Nothing is saved and the election's result is not affected; only expected effects are shown."),
        h("div", { class: "ana-replan" }, rows),
        h("div", { class: "ana-row" }, field("Seed", seed), h("div", { class: "ana-grow" }), btn),
        out,
      ),
      { categories: ["FICTIONAL"] },
    );
  }

  function previewTable(res, camps) {
    const base = Object.fromEntries(camps.map((c) => [c.party, c]));
    return h(
      "div",
      { class: "ana-stack-v" },
      callout("sim", h("strong", null, "Preview only — not stored. "), res.note || ""),
      dataTable(
        [
          { key: "party", label: "Party", format: (v) => pchip(v) },
          { key: "strategy", label: "Strategy", format: (v, r) => h("span", null, humanize(v), base[r.party] && base[r.party].strategy !== v ? h("span", { class: "ana-changed" }, " changed") : null) },
          { key: "budget", label: "Budget", align: "r", format: (v) => fmt1(v) },
          { key: "spend", label: "Spend", align: "r", format: (v) => fmt1(v) },
          { key: "allocations", label: "Allocations", align: "r", format: (v) => fmtInt(v) },
          { key: "targets_total", label: "Targets", align: "r" },
          { key: "by_action", label: "Top actions", sort: false, format: (v) => h("span", { class: "muted" }, [...(v || [])].sort((a, b) => b.amount - a.amount).slice(0, 3).map((a) => `${humanize(a.action)} ${fmt1(a.amount)}`).join(" · ")) },
          { key: "targets", label: "Top targets", sort: false, format: (v) => h("span", { class: "muted" }, (v || []).slice(0, 3).map((t) => `${t.code} ${fmt1(t.amount)}`).join(" · ")) },
        ],
        res.campaigns || [],
        { sortKey: "budget" },
      ),
    );
  }

  /* -------------------------------------------------------------- live → final */
  let lastStatus = null;
  cleanups.push(
    subscribe("night", (night) => {
      const status = night?.election_status || null;
      if (lastStatus && status && status !== lastStatus && st.data && !st.data.effects_revealed && (status === "final" || status === "certified")) {
        toast("The election is now final — realised campaign effects revealed", "success");
        load();
      }
      lastStatus = status;
    }),
  );

  await load();
  return () => cleanups.forEach((f) => f());
}
