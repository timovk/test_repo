/**
 * #/scenarios — Scenario editor for the FICTIONAL political assumptions behind elections.
 * Lists built-in (read-only) and user scenarios; shows a scenario as a structured form and as
 * YAML; edits become editor operations (PUT edits / YAML), with server-side validation,
 * duplicate, import (paste / upload), export (download), delete, and creating (and simulating)
 * an election from a scenario — with explicit warnings, since that writes to the database.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { setQuery } from "../router.js";
import { getState, setState } from "../store.js";
import { fmtInt } from "../format.js";
import { card, links, pageHeader } from "./_shared.js";
import { provBadge } from "../components/badges.js";
import { icon } from "../components/icons.js";
import { callout, copyButton, downloadLink, errorBox, field, fmtDate, fmtDateTime, humanize, kv, partyInfo, pchip, provinceName, refresh, selectBox, statusBadge, tabBar, toast } from "../components/ana-ui.js";

const EV_METHODS = [
  { value: "winner_take_all", label: "Winner-take-all (default)" },
  { value: "district", label: "District method (2 at-large + 1 per House district)" },
  { value: "proportional", label: "Proportional (largest remainder)" },
];
const STRATEGIES = ["balanced", "battleground", "base", "expansion"];
const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;

export async function render(el, _params, ctx) {
  const st = { slug: ctx.query.s || null, tab: ctx.query.tab || "form", list: null, sc: null, pending: new Map(), seq: 0, mode: ctx.query.import ? "import" : "view" };
  const listHost = h("div");
  const mainHost = h("div");
  mount(
    el,
    pageHeader({ eyebrow: "System · Scenarios", title: "Scenario Editor", categories: ["FICTIONAL"], meta: [h("button", { class: "btn", type: "button", onclick: () => openImport() }, icon("download", { size: 14 }), "Import YAML")] }),
    callout("sim", h("strong", null, "Scenarios are fictional political assumptions. "), "They define invented parties, candidates, national and provincial environments, shocks and campaign settings. Built-in scenarios are read-only (duplicate to edit); user scenarios are saved to config/scenarios/user and the database."),
    h("div", { class: "ana-scn" }, listHost, mainHost),
  );

  async function loadList() {
    st.list = await api.get("/api/scenarios");
    renderList();
    return st.list;
  }

  function renderList() {
    const items = st.list?.scenarios || [];
    mount(
      listHost,
      card(
        `Scenarios (${items.length})`,
        h(
          "ul",
          { class: "ana-scnlist", role: "list" },
          items.map((s) =>
            h(
              "li",
              null,
              h(
                "button",
                {
                  type: "button",
                  class: ["ana-scnitem", s.slug === st.slug && st.mode === "view" && "is-active"],
                  "aria-current": s.slug === st.slug ? "true" : undefined,
                  onclick: () => select(s.slug),
                },
                h("div", { class: "ana-scnitem__top" }, h("span", { class: "ana-scnitem__name" }, s.name), s.read_only ? h("span", { class: "ana-scnitem__lock", title: "Built-in, read-only" }, icon("lock", { size: 12 })) : null),
                h("div", { class: "ana-scnitem__slug mono" }, s.slug),
                h(
                  "div",
                  { class: "ana-scnitem__meta" },
                  h("span", null, `${s.year} · ${humanize(s.election_type)}`),
                  h("span", { class: `ana-src-tag ana-src-tag--${s.source}` }, s.source === "builtin" ? "Built-in" : s.source === "user" ? "User" : "Database"),
                  s.valid ? null : statusBadge("fail", "Invalid"),
                  s.elections?.length ? h("span", { class: "muted" }, `used by ${s.elections.map((e) => `#${e}`).join(", ")}`) : null,
                ),
              ),
            ),
          ),
        ),
        { flush: true, categories: ["FICTIONAL"], actions: h("button", { class: "btn btn--sm", type: "button", onclick: () => openImport() }, "New / import") },
      ),
    );
  }

  async function select(slug, { tab } = {}) {
    st.slug = slug;
    st.mode = "view";
    st.pending.clear();
    if (tab) st.tab = tab;
    setQuery({ s: slug, import: null });
    renderList();
    await refresh(mainHost, api.get(`/api/scenarios/${encodeURIComponent(slug)}`), (sc) => {
      st.sc = sc;
      return editor();
    });
  }

  /* -------------------------------------------------------------- editor */
  function editor() {
    const sc = st.sc;
    const ro = sc.read_only;
    const content = h("div");
    const pendingHost = h("div");
    const tabs = [
      { key: "form", label: "Structured form" },
      { key: "yaml", label: "YAML" },
      { key: "election", label: "Create election" },
    ];
    const show = (k) => {
      st.tab = k;
      setQuery({ tab: k === "form" ? null : k });
      mount(content, k === "form" ? formView(pendingHost) : k === "yaml" ? yamlView() : electionView());
    };
    const v = sc.validation || {};
    const actionsHost = h("div", { class: "ana-row" });
    const dupHost = h("div");
    mount(
      actionsHost,
      h("button", { class: "btn btn--sm", type: "button", onclick: () => mount(dupHost, duplicateForm()) }, "Duplicate…"),
      downloadLink(`/api/scenarios/${encodeURIComponent(sc.slug)}/export`, "Export YAML"),
      ro ? null : deleteButton(sc),
    );
    const head = h(
      "section",
      { class: "card ana-scnhead" },
      h(
        "div",
        { class: "ana-row ana-row--between" },
        h("div", null, h("div", { class: "ana-scnhead__name" }, sc.name), h("div", { class: "ana-note", style: { margin: 0 } }, h("span", { class: "mono" }, sc.slug), ` · ${sc.year} · ${humanize(sc.election_type)} · seed ${sc.seed}`)),
        h("div", { class: "ana-row" }, provBadge("FICTIONAL"), ro ? h("span", { class: "pill pill--closed" }, icon("lock", { size: 11, className: "pill__icon" }), "Built-in · read-only") : h("span", { class: "pill" }, "User scenario"), v.ok ? statusBadge("ok", "Valid") : statusBadge("fail", `${(v.problems || []).length} problem(s)`)),
      ),
      h("p", { class: "ana-sub", style: { marginTop: "10px" } }, sc.document?.scenario?.description || ""),
      kv(
        [
          ["File", sc.file ? h("span", { class: "mono" }, sc.file) : "–"],
          ["Document hash", h("span", { class: "mono" }, sc.hash || "–")],
          ["Database row", sc.database ? `#${sc.database.id} · ${fmtDateTime(sc.database.updated_at)}` : "not stored"],
          ["Elections", sc.elections?.length ? h("span", { class: "ana-row", style: { gap: "8px" } }, sc.elections.map((id) => h("a", { class: "ana-link", href: `#/night?e=${id}` }, `#${id}`))) : "none yet"],
          ["Geography checked", v.geography_checked ? "yes (loaded CBS geography)" : "no"],
        ],
        { cols: 2 },
      ),
      (v.problems || []).length ? callout("warn", h("strong", null, "Geography problems: "), v.problems.join("; ")) : null,
      h("div", { class: "ana-row ana-row--between", style: { marginTop: "12px" } }, actionsHost, ro ? h("span", { class: "ana-note", style: { margin: 0 } }, "Built-in scenarios cannot be changed: duplicate it (or “Save as copy”) to edit.") : null),
      dupHost,
    );
    const bar = tabBar(tabs, tabs.some((t) => t.key === st.tab) ? st.tab : "form", show, { label: "Scenario views" });
    show(tabs.some((t) => t.key === st.tab) ? st.tab : "form");
    return h("div", { class: "ana-stack-v" }, head, pendingHost, h("div", null, bar, content));
  }

  function deleteButton(sc) {
    const b = h("button", { class: "btn btn--sm btn--ghost ana-danger", type: "button" }, "Delete");
    let armed = false;
    b.onclick = async () => {
      if (!armed) {
        armed = true;
        b.textContent = "Confirm delete";
        setTimeout(() => {
          armed = false;
          b.textContent = "Delete";
        }, 4000);
        return;
      }
      try {
        await api.del(`/api/scenarios/${encodeURIComponent(sc.slug)}`);
        toast(`Scenario ${sc.slug} deleted`, "success");
        st.slug = null;
        await loadList();
        select(st.list.scenarios[0]?.slug);
      } catch (e) {
        toast(e.message, "error");
      }
    };
    return b;
  }

  function duplicateForm(prefill = {}) {
    const sc = st.sc;
    const slug = h("input", { class: "input", required: true, value: prefill.slug || `${sc.slug}-copy`, pattern: "[a-z0-9][a-z0-9-]+", "aria-describedby": "dup-hint" });
    const name = h("input", { class: "input", value: prefill.name || `${sc.name} (copy)` });
    const seed = h("input", { class: "input", type: "number", min: 0, step: 1, placeholder: String(sc.seed) });
    const msg = h("div", { "aria-live": "polite" });
    const go = h("button", { class: "btn btn--primary btn--sm", type: "submit" }, prefill.withEdits ? "Save as copy" : "Duplicate");
    return h(
      "form",
      {
        class: "ana-inlineform",
        onsubmit: async (e) => {
          e.preventDefault();
          if (!SLUG_RE.test(slug.value)) {
            slug.setAttribute("aria-invalid", "true");
            mount(msg, callout("warn", "Slug: lowercase letters, digits and dashes (2–63 characters)."));
            return;
          }
          go.disabled = true;
          try {
            const body = { new_slug: slug.value };
            if (name.value.trim()) body.name = name.value.trim();
            if (seed.value) body.seed = Number(seed.value);
            const copy = await api.post(`/api/scenarios/${encodeURIComponent(sc.slug)}/duplicate`, body);
            if (prefill.withEdits && st.pending.size) await api.put(`/api/scenarios/${encodeURIComponent(copy.slug)}`, { edits: [...st.pending.values()] });
            toast(`Created user scenario ${copy.slug}`, "success");
            await loadList();
            select(copy.slug);
          } catch (err) {
            go.disabled = false;
            mount(msg, callout("warn", err.message));
          }
        },
      },
      h("div", { class: "ana-row", style: { alignItems: "flex-end" } }, field("New slug", slug, h("span", { id: "dup-hint" }, "lowercase-with-dashes")), field("Name", name), field("Seed (optional)", seed), go, h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: (e) => e.currentTarget.closest("form").remove() }, "Cancel")),
      msg,
    );
  }

  /* -------------------------------------------------------------- pending edits */
  function setEdit(key, op, label, input) {
    st.pending.set(key, { ...op, __label: label });
    input?.classList.add("is-dirty");
    renderPending();
  }
  let pendingHostRef = null;
  function renderPending() {
    const host = pendingHostRef;
    if (!host) return;
    const n = st.pending.size;
    if (!n) {
      mount(host);
      return;
    }
    const ro = st.sc.read_only;
    const resultHost = h("div", { "aria-live": "polite" });
    const edits = () => [...st.pending.values()].map(({ __label, ...op }) => op);
    const validateBtn = h("button", { class: "btn btn--sm", type: "button", onclick: async () => {
      await refresh(resultHost, api.post("/api/scenarios/validate", { base: st.sc.slug, edits: edits() }), (r) => validationResult(r));
    } }, "Validate");
    const saveBtn = ro
      ? h("button", { class: "btn btn--sm btn--primary", type: "button", onclick: () => mount(resultHost, duplicateForm({ withEdits: true, slug: `${st.sc.slug}-edit`, name: `${st.sc.name} (edited)` })) }, "Save as copy…")
      : h("button", { class: "btn btn--sm btn--primary", type: "button", onclick: async () => {
          saveBtn.disabled = true;
          try {
            const r = await api.put(`/api/scenarios/${encodeURIComponent(st.sc.slug)}`, { edits: edits() });
            toast(`Saved ${r.changes?.length ?? n} change(s) to ${st.sc.slug}`, "success");
            await loadList();
            select(r.slug || st.sc.slug);
          } catch (e) {
            saveBtn.disabled = false;
            mount(resultHost, callout("warn", h("strong", null, "Not saved: "), e.message));
          }
        } }, "Save changes");
    mount(
      host,
      h(
        "section",
        { class: "card ana-pending" },
        h(
          "div",
          { class: "ana-row ana-row--between" },
          h("div", null, h("strong", null, `${n} pending edit${n === 1 ? "" : "s"}`), h("span", { class: "muted" }, ro ? " — built-in scenario: validate, or save as a new user scenario" : " — not saved yet")),
          h("div", { class: "ana-row" }, validateBtn, saveBtn, h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: () => select(st.sc.slug) }, "Discard")),
        ),
        h("ul", { class: "ana-pending__list" }, [...st.pending.values()].map((e) => h("li", null, h("span", { class: "mono ana-pending__op" }, e.op), e.__label))),
        resultHost,
      ),
    );
  }

  function validationResult(r) {
    return h(
      "div",
      { class: "ana-stack-v", style: { marginTop: "10px" } },
      r.ok ? callout("ok", h("strong", null, "Valid. "), r.geography_checked ? "Schema and geography checks passed." : "Schema checks passed.") : callout("warn", h("strong", null, "Invalid. "), [...(r.errors || []), ...(r.problems || [])].join(" · ") || "See details."),
      (r.errors || []).length ? h("pre", { class: "ana-pre" }, r.errors.join("\n\n")) : null,
      (r.problems || []).length ? h("ul", { class: "ana-notes" }, r.problems.map((p) => h("li", null, p))) : null,
      (r.changes || []).length ? h("div", null, h("div", { class: "ana-field__label" }, "Changes"), h("ul", { class: "ana-notes" }, r.changes.map((c) => h("li", { class: "mono" }, c)))) : null,
    );
  }

  /* -------------------------------------------------------------- form view */
  function num(value, onChange, { step = 0.01, min, max, width, label } = {}) {
    const i = h("input", { class: "input input--sm", type: "number", step, min, max, value: value ?? "", "aria-label": label, style: width ? { width } : undefined });
    i.addEventListener("change", () => {
      if (i.value === "" || Number.isNaN(Number(i.value))) return;
      onChange(Number(i.value), i);
    });
    return i;
  }
  function txt(value, onChange, { label, width } = {}) {
    const i = h("input", { class: "input input--sm", value: value ?? "", "aria-label": label, style: { width: width || "100%" } });
    i.addEventListener("change", () => onChange(i.value, i));
    return i;
  }

  function section(title, body, note) {
    return h("fieldset", { class: "ana-fs" }, h("legend", null, title), note ? h("p", { class: "ana-note", style: { marginTop: 0 } }, note) : null, body);
  }

  function formView(pendingHost) {
    pendingHostRef = pendingHost;
    renderPending();
    const doc = st.sc.document || {};
    const s = doc.scenario || {};
    const parties = doc.parties || [];
    const cands = doc.candidates || [];
    const env = doc.environment || {};
    const codes = parties.map((p) => p.code);
    const candName = (k) => {
      const c = cands.find((x) => x.key === k);
      return c ? `${c.first_name} ${c.last_name}` : k;
    };

    const scenarioSec = section(
      "Scenario",
      h(
        "div",
        { class: "ana-form-grid" },
        field("Name", txt(s.name, (v, i) => setEdit("s.name", { op: "set", path: "scenario.name", value: v }, `name = “${v}”`, i), { label: "Name" })),
        field("Election year", num(s.year, (v, i) => setEdit("s.year", { op: "set", path: "scenario.year", value: v }, `year = ${v}`, i), { step: 1, label: "Year" })),
        field("Election date", (() => {
          const i = h("input", { class: "input input--sm", type: "date", value: s.election_date || "", style: { width: "100%" } });
          i.addEventListener("change", () => setEdit("s.date", { op: "set", path: "scenario.election_date", value: i.value }, `election date = ${i.value}`, i));
          return i;
        })()),
        field("Seed", num(s.seed, (v, i) => setEdit("s.seed", { op: "set_seed", value: v }, `seed = ${v}`, i), { step: 1, label: "Seed", width: "100%" })),
        field("Political geography seed", num(s.political_geography_seed, (v, i) => setEdit("s.gseed", { op: "set_political_geography_seed", value: v }, `political geography seed = ${v}`, i), { step: 1, label: "Political geography seed", width: "100%" }), "optional"),
      ),
    );

    const natEnv = section(
      "National environment (logit shift per party)",
      h(
        "div",
        { class: "ana-envgrid" },
        parties.map((p) =>
          h(
            "label",
            { class: "ana-result" },
            pchip(p.code, p.color),
            num(env.national?.[p.code] ?? 0, (v, i) => setEdit(`env.${p.code}`, { op: "set_national_environment", party: p.code, value: v }, `national environment ${p.code} = ${v}`, i), { step: 0.01, label: `${p.code} national environment` }),
          ),
        ),
      ),
      "Positive values improve a party's national utility (≈ +0.05 ≈ one or two points of vote share).",
    );
    const provEnv = env.provinces && Object.keys(env.provinces).length
      ? h("div", { class: "ana-chips" }, Object.entries(env.provinces).flatMap(([prov, m]) => Object.entries(m).map(([party, v]) => h("span", { class: "ana-chipbox" }, h("b", null, provinceName(prov)), pchip(party), h("span", { class: "num" }, `${v > 0 ? "+" : ""}${v}`)))))
      : h("span", { class: "muted" }, "none");

    const partyTable = section(
      "Parties",
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "data ana-edit-table" },
          h("thead", null, h("tr", null, ["Code", "Colour", "Name", "Abbrev.", "Base share", "Turnout propensity", "Family"].map((x) => h("th", null, x)))),
          h(
            "tbody",
            null,
            parties.map((p) => {
              const col = h("input", { type: "color", class: "ana-color", value: /^#[0-9a-f]{6}$/i.test(p.color) ? p.color : "#888888", "aria-label": `${p.code} colour` });
              col.addEventListener("change", () => setEdit(`p.${p.code}.color`, { op: "set_party_field", party: p.code, field: "color", value: col.value.toUpperCase() }, `${p.code} colour = ${col.value.toUpperCase()}`, col));
              return h(
                "tr",
                null,
                h("td", null, pchip(p.code, p.color)),
                h("td", null, col),
                h("td", null, txt(p.name, (v, i) => setEdit(`p.${p.code}.name`, { op: "set_party_field", party: p.code, field: "name", value: v }, `${p.code} name = “${v}”`, i), { label: `${p.code} name`, width: "220px" })),
                h("td", null, txt(p.abbreviation, (v, i) => setEdit(`p.${p.code}.abbr`, { op: "set_party_field", party: p.code, field: "abbreviation", value: v }, `${p.code} abbreviation = ${v}`, i), { label: `${p.code} abbreviation`, width: "70px" })),
                h("td", null, num(p.base_share, (v, i) => setEdit(`p.${p.code}.base`, { op: "set_party_base_share", party: p.code, value: v }, `${p.code} base share = ${v}`, i), { step: 0.005, min: 0, max: 1, label: `${p.code} base share` })),
                h("td", null, num(p.turnout_propensity ?? 0, (v, i) => setEdit(`p.${p.code}.tp`, { op: "set_party_field", party: p.code, field: "turnout_propensity", value: v }, `${p.code} turnout propensity = ${v}`, i), { step: 0.01, label: `${p.code} turnout propensity` })),
                h("td", { class: "muted" }, p.family || "–"),
              );
            }),
          ),
        ),
      ),
    );

    const candTable = section(
      `Candidates (${cands.length}) & quality`,
      h(
        "div",
        { class: "table-wrap", style: { "--table-max-h": "380px" } },
        h(
          "table",
          { class: "data ana-edit-table" },
          h("thead", null, h("tr", null, ["Candidate", "Party", "Quality", "Campaign strength", "Fundraising", "Favourability", "Incumbent office"].map((x) => h("th", null, x)))),
          h(
            "tbody",
            null,
            cands.map((c) =>
              h(
                "tr",
                null,
                h("td", null, h("b", null, `${c.first_name} ${c.last_name}`), h("div", { class: "muted mono", style: { fontSize: "11px" } }, c.key)),
                h("td", null, pchip(c.party)),
                h("td", null, num(c.quality, (v, i) => setEdit(`c.${c.key}.q`, { op: "set_candidate_quality", candidate: c.key, value: v }, `${c.key} quality = ${v}`, i), { step: 0.05, label: `${c.key} quality` })),
                h("td", null, num(c.campaign_strength, (v, i) => setEdit(`c.${c.key}.cs`, { op: "set", path: `candidates.${c.key}.campaign_strength`, value: v }, `${c.key} campaign strength = ${v}`, i), { step: 0.05, label: `${c.key} campaign strength` })),
                h("td", null, num(c.fundraising, (v, i) => setEdit(`c.${c.key}.fr`, { op: "set", path: `candidates.${c.key}.fundraising`, value: v }, `${c.key} fundraising = ${v}`, i), { step: 0.05, label: `${c.key} fundraising` })),
                h("td", null, num(c.favorability, (v, i) => setEdit(`c.${c.key}.fav`, { op: "set", path: `candidates.${c.key}.favorability`, value: v }, `${c.key} favourability = ${v}`, i), { step: 0.5, label: `${c.key} favourability` })),
                h("td", { class: "muted" }, c.incumbent_office || "–"),
              ),
            ),
          ),
        ),
      ),
      "Quality ≈ −1 … 1.5: electoral appeal beyond the party label.",
    );

    const tickets = doc.president?.tickets || [];
    const ticketRows = tickets.map((t, idx) => {
      const inc = h("input", { type: "checkbox", checked: !!t.incumbent, "aria-label": `${t.party} incumbent` });
      inc.addEventListener("change", () => setEdit(`t.${idx}.inc`, { op: "set", path: `president.tickets.${idx}.incumbent`, value: inc.checked }, `${t.party} ticket incumbent = ${inc.checked}`, inc));
      const wd = h("input", { type: "checkbox", checked: !!t.withdrawn, "aria-label": `${t.party} withdrawn` });
      wd.addEventListener("change", () => setEdit(`t.${t.party}.wd`, { op: "withdraw_ticket", party: t.party, withdrawn: wd.checked }, `${t.party} ticket withdrawn = ${wd.checked}`, wd));
      const rm = h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: (e) => { setEdit(`t.${t.party}.rm.${++st.seq}`, { op: "remove_ticket", party: t.party }, `remove ${t.party} ticket`); e.currentTarget.closest("tr").classList.add("is-removed"); e.currentTarget.disabled = true; } }, "Remove");
      return h("tr", null, h("td", null, pchip(t.party)), h("td", null, candName(t.president)), h("td", null, candName(t.vice_president)), h("td", null, h("label", { class: "ana-check" }, inc, "incumbent")), h("td", null, h("label", { class: "ana-check" }, wd, "withdrawn")), h("td", null, rm));
    });
    const tp = h("select", { class: "select", "aria-label": "New ticket party" }, codes.map((c) => h("option", { value: c }, c)));
    const tpres = h("select", { class: "select", "aria-label": "President" }, cands.map((c) => h("option", { value: c.key }, `${c.first_name} ${c.last_name} (${c.party})`)));
    const tvp = h("select", { class: "select", "aria-label": "Vice president" }, cands.map((c) => h("option", { value: c.key }, `${c.first_name} ${c.last_name} (${c.party})`)));
    const addTicket = h("div", { class: "ana-row", style: { marginTop: "10px", alignItems: "flex-end" } }, field("Party", tp), field("President", tpres), field("Vice president", tvp), h("button", { class: "btn btn--sm", type: "button", onclick: () => {
      if (tpres.value === tvp.value) {
        toast("President and vice president must differ", "error");
        return;
      }
      const replace = tickets.some((t) => t.party === tp.value);
      setEdit(`t.add.${++st.seq}`, { op: "add_ticket", party: tp.value, president: tpres.value, vice_president: tvp.value, replace }, `${replace ? "replace" : "add"} ${tp.value} ticket: ${candName(tpres.value)} / ${candName(tvp.value)}`);
    } }, "Add ticket"));
    const ticketSec = section("Presidential tickets", h("div", null, h("div", { class: "table-wrap" }, h("table", { class: "data ana-edit-table" }, h("thead", null, h("tr", null, ["Party", "President", "Vice president", "", "", ""].map((x) => h("th", null, x)))), h("tbody", null, ticketRows))), addTicket), "Adding a ticket for a party that already has one replaces it.");

    const shocks = env.shocks || {};
    const turnoutSec = section(
      "Turnout, elasticity & shocks",
      h(
        "div",
        { class: "ana-stack-v" },
        h(
          "div",
          { class: "ana-form-grid" },
          field("Turnout base", num(env.turnout_base, (v, i) => setEdit("env.turnout", { op: "set", path: "environment.turnout_base", value: v }, `turnout base = ${v}`, i), { step: 0.005, min: 0.3, max: 0.99, label: "Turnout base", width: "100%" }), "share of eligible voters"),
          field("Elasticity strength", num(env.elasticity_strength, (v, i) => setEdit("env.elast", { op: "set", path: "environment.elasticity_strength", value: v }, `elasticity strength = ${v}`, i), { step: 0.05, label: "Elasticity strength", width: "100%" })),
          field("National shock SD", num(shocks.national_sd, (v, i) => setEdit("env.nsd", { op: "set", path: "environment.shocks.national_sd", value: v }, `national shock SD = ${v}`, i), { step: 0.005, min: 0, label: "National shock SD", width: "100%" }), "also the forecast's prior SD"),
          field("Province shock SD", num(shocks.province_sd, (v, i) => setEdit("env.psd", { op: "set", path: "environment.shocks.province_sd", value: v }, `province shock SD = ${v}`, i), { step: 0.005, min: 0, label: "Province shock SD", width: "100%" })),
          field("Spatial shock SD", num(shocks.spatial_sd, (v, i) => setEdit("env.ssd", { op: "set", path: "environment.shocks.spatial_sd", value: v }, `spatial shock SD = ${v}`, i), { step: 0.005, min: 0, label: "Spatial shock SD", width: "100%" })),
        ),
        h("div", null, h("div", { class: "ana-field__label" }, "Province environments"), provEnv),
        (env.events || []).length
          ? h("div", null, h("div", { class: "ana-field__label" }, `Events (${env.events.length})`), h("ul", { class: "ana-events" }, env.events.map((e) => h("li", null, h("b", null, e.name), e.probability != null ? h("span", { class: "ana-tag" }, `p = ${e.probability}`) : null, h("div", { class: "muted" }, e.description || ""), h("div", { class: "ana-chips" }, Object.entries(e.national || {}).map(([p, v]) => h("span", { class: "ana-chipbox" }, pchip(p), h("span", { class: "num" }, `${v > 0 ? "+" : ""}${v}`))), Object.entries(e.provinces || {}).flatMap(([prov, m]) => Object.entries(m).map(([p, v]) => h("span", { class: "ana-chipbox" }, h("b", null, prov), pchip(p), h("span", { class: "num" }, `${v > 0 ? "+" : ""}${v}`)))))))))
          : null,
      ),
    );

    const evSel = selectBox(EV_METHODS, doc.electoral_college?.allocation || "winner_take_all", (v) => setEdit("ev", { op: "set_ev_allocation", value: v }, `EV allocation = ${v}`, evSel), { "aria-label": "EV allocation method" });
    const evSec = section("Electoral College", field("EV allocation method", evSel, "Winner-take-all is the constitution's default; the others are what-if rules for this scenario."));

    const pol = doc.polling || {};
    const polSec = section(
      "Polling",
      h(
        "div",
        { class: "ana-stack-v" },
        h(
          "div",
          { class: "ana-form-grid" },
          field("Start date", (() => {
            const i = h("input", { class: "input input--sm", type: "date", value: pol.start_date || "", style: { width: "100%" } });
            i.addEventListener("change", () => setEdit("pol.start", { op: "set", path: "polling.start_date", value: i.value }, `polling start = ${i.value}`, i));
            return i;
          })()),
          ...["national_polls", "province_polls", "senate_polls", "generic_ballot_polls"].map((k) => field(humanize(k), num(pol[k], (v, i) => setEdit(`pol.${k}`, { op: "set", path: `polling.${k}`, value: Math.round(v) }, `${humanize(k)} = ${Math.round(v)}`, i), { step: 1, min: 0, label: humanize(k), width: "100%" }))),
        ),
        (pol.pollsters || []).length ? h("div", { class: "ana-chips" }, pol.pollsters.map((p) => h("span", { class: "ana-chipbox" }, h("b", null, p.name), h("span", { class: "ana-rating" }, p.rating_label || "–"), h("span", { class: "muted" }, p.method || "")))) : null,
      ),
      "Pollsters are fictional; house effects and ratings are edited in YAML.",
    );

    const camp = doc.campaigns || {};
    const campSec = section(
      "Campaigns",
      h(
        "div",
        { class: "ana-stack-v" },
        field("Weeks", num(camp.weeks, (v, i) => setEdit("camp.weeks", { op: "set", path: "campaigns.weeks", value: Math.round(v) }, `campaign weeks = ${Math.round(v)}`, i), { step: 1, min: 1, max: 30, label: "Weeks" })),
        h(
          "div",
          { class: "ana-replan" },
          codes.map((c) => {
            const strat = selectBox(STRATEGIES.map((x) => ({ value: x, label: humanize(x) })), camp.strategies?.[c] || "balanced", (v) => setEdit(`camp.s.${c}`, { op: "set", path: `campaigns.strategies.${c}`, value: v }, `${c} strategy = ${v}`, strat), { "aria-label": `${c} strategy` });
            return h("div", { class: "ana-replan__row" }, pchip(c), num(camp.budgets?.[c], (v, i) => setEdit(`camp.b.${c}`, { op: "set", path: `campaigns.budgets.${c}`, value: v }, `${c} budget = ${v}`, i), { step: 1, min: 0, label: `${c} budget` }), strat);
          }),
        ),
      ),
    );

    return h(
      "div",
      { class: "ana-stack-v" },
      st.sc.read_only ? callout("info", h("strong", null, "Read-only built-in scenario. "), "You can still try edits here: “Validate” checks them on the server and “Save as copy…” stores them as a new user scenario.") : null,
      h("div", { class: "ana-scnform" }, scenarioSec, natEnv, partyTable, candTable, ticketSec, turnoutSec, evSec, polSec, campSec),
    );
  }

  /* -------------------------------------------------------------- YAML view */
  function yamlView() {
    const sc = st.sc;
    const ro = sc.read_only;
    const ta = h("textarea", { class: "textarea ana-yaml", spellcheck: "false", "aria-label": `YAML of scenario ${sc.slug}` });
    ta.value = sc.yaml || "";
    const out = h("div", { "aria-live": "polite" });
    const file = h("input", { type: "file", accept: ".yaml,.yml,text/yaml,application/x-yaml", class: "sr-only", id: "ana-yaml-file" });
    file.addEventListener("change", async () => {
      const f = file.files?.[0];
      if (f) ta.value = await f.text();
    });
    const newSlug = h("input", { class: "input", placeholder: `${sc.slug}-custom`, "aria-label": "Slug for the new scenario" });
    return card(
      "Scenario YAML",
      h(
        "div",
        { class: "ana-stack-v" },
        h(
          "div",
          { class: "ana-row ana-row--between" },
          h(
            "div",
            { class: "ana-row" },
            h("button", { class: "btn btn--sm", type: "button", onclick: () => refresh(out, api.post("/api/scenarios/validate", { yaml: ta.value }), (r) => validationResult(r)) }, "Validate YAML"),
            ro
              ? h("span", { class: "ana-row" }, newSlug, h("button", { class: "btn btn--sm btn--primary", type: "button", onclick: async () => {
                  const slug = newSlug.value.trim();
                  if (slug && !SLUG_RE.test(slug)) return toast("Invalid slug", "error");
                  try {
                    const created = await api.post("/api/scenarios", slug ? { yaml: ta.value, slug } : { yaml: ta.value });
                    toast(`Created ${created.slug}`, "success");
                    await loadList();
                    select(created.slug);
                  } catch (e) {
                    mount(out, callout("warn", h("strong", null, "Not created: "), e.message));
                  }
                } }, "Save as new scenario"))
              : h("button", { class: "btn btn--sm btn--primary", type: "button", onclick: async () => {
                  try {
                    const r = await api.put(`/api/scenarios/${encodeURIComponent(sc.slug)}`, { yaml: ta.value });
                    toast(`Saved ${sc.slug}${r.changes?.length ? ` (${r.changes.length} changes)` : ""}`, "success");
                    await loadList();
                    select(r.slug || sc.slug, { tab: "yaml" });
                  } catch (e) {
                    mount(out, callout("warn", h("strong", null, "Not saved: "), e.message));
                  }
                } }, "Save YAML"),
          ),
          h("div", { class: "ana-row" }, h("label", { class: "btn btn--sm btn--ghost", for: "ana-yaml-file" }, "Load file…"), file, copyButton(ta.value, "Copy"), downloadLink(`/api/scenarios/${encodeURIComponent(sc.slug)}/export`, "Download")),
        ),
        ta,
        out,
      ),
      { categories: ["FICTIONAL"], foot: ro ? "Built-in: edits here can be validated or saved as a new user scenario." : "Saving replaces the scenario document (a variant row is kept for elections already created from it)." },
    );
  }

  /* -------------------------------------------------------------- create election */
  function electionView() {
    const sc = st.sc;
    const seed = h("input", { class: "input", type: "number", min: 0, step: 1, placeholder: String(sc.seed) });
    const year = h("input", { class: "input", type: "number", min: 2000, max: 2200, step: 1, placeholder: String(sc.year) });
    const strict = h("input", { type: "checkbox", checked: true });
    const simulate = h("input", { type: "checkbox", checked: true });
    const ack = h("input", { type: "checkbox" });
    const out = h("div", { "aria-live": "polite" });
    const go = h("button", { class: "btn btn--primary", type: "submit", disabled: true }, "Create election");
    ack.addEventListener("change", () => (go.disabled = !ack.checked));
    const existing = (getState().meta?.elections || []).map((e) => `${e.year} (${e.status})`).join(", ");
    return card(
      "Create an election from this scenario",
      h(
        "form",
        {
          class: "ana-stack-v",
          onsubmit: async (e) => {
            e.preventDefault();
            const body = { scenario: sc.slug, strict: strict.checked, simulate: simulate.checked };
            if (seed.value) body.seed = Number(seed.value);
            if (year.value) body.year = Number(year.value);
            go.disabled = true;
            mount(out, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "40%", height: "14px" } }), h("span", { class: "muted" }, simulate.checked ? "Creating and simulating — this can take a little while on the real country…" : "Creating…")));
            try {
              const el = await api.post("/api/elections", body);
              const e2 = el.election || el;
              const meta = await api.get("/api/meta");
              setState({ meta });
              api.invalidate("/api/scenarios");
              mount(out, createdPanel(e2));
              toast(`Election #${e2.id} created`, "success");
              loadList();
            } catch (err) {
              mount(out, callout("warn", h("strong", null, err.status === 409 ? "Calendar conflict: " : err.status === 422 ? "Invalid: " : "Failed: "), err.message));
            } finally {
              go.disabled = !ack.checked;
            }
          },
        },
        callout(
          "warn",
          h("strong", null, "This writes to the database. "),
          "A new SCHEDULED election is created from this FICTIONAL scenario (and SIMULATED if selected — its results stay hidden until its election night or finalisation). Elections are certified in chronological order: the API refuses a year that already holds an election or a date on or before an election that is already FINAL. ",
          existing ? `Existing elections: ${existing}.` : "",
        ),
        h("div", { class: "ana-form-grid" }, field("Seed", seed, "blank = scenario seed"), field("Year", year, "blank = scenario year")),
        h("div", { class: "ana-row" }, h("label", { class: "ana-check" }, strict, "Strict geography check"), h("label", { class: "ana-check" }, simulate, "Simulate immediately")),
        h("label", { class: "ana-check ana-ack" }, ack, "I understand this creates a new simulated election in the database."),
        h("div", null, go),
        out,
      ),
      { categories: ["FICTIONAL", "SIMULATED"] },
    );
  }

  function createdPanel(e) {
    const simBtn = h("button", { class: "btn btn--sm", type: "button" }, "Simulate now");
    const simOut = h("div", { "aria-live": "polite" });
    simBtn.onclick = async () => {
      simBtn.disabled = true;
      try {
        const r = await api.post(`/api/elections/${e.id}/simulate`, {});
        const meta = await api.get("/api/meta");
        setState({ meta });
        mount(simOut, callout("ok", `Election #${e.id} simulated (status ${r.election?.status || r.status || "simulated"}). Results stay hidden until its night.`));
      } catch (err) {
        simBtn.disabled = false;
        mount(simOut, callout("warn", err.message));
      }
    };
    return h(
      "div",
      { class: "ana-stack-v" },
      callout("ok", h("strong", null, `Election #${e.id} created: `), `${e.name} · ${fmtDate(e.election_date)} · status ${e.status}.`),
      h(
        "div",
        { class: "ana-row" },
        h("a", { class: "btn btn--sm btn--primary", href: `#/night?e=${e.id}` }, "Open its election night"),
        h("a", { class: "btn btn--sm", href: `#/forecast?e=${e.id}` }, "Forecast it"),
        e.status === "scheduled" ? simBtn : null,
      ),
      simOut,
    );
  }

  /* -------------------------------------------------------------- import */
  function openImport() {
    st.mode = "import";
    setQuery({ import: "1", s: null, tab: null });
    renderList();
    const ta = h("textarea", { class: "textarea ana-yaml", spellcheck: "false", placeholder: "scenario:\n  slug: my-scenario\n  name: …\n  year: 2030\n  seed: 1\nparties: …", "aria-label": "Scenario YAML to import" });
    const slug = h("input", { class: "input", placeholder: "optional — taken from the document" });
    const file = h("input", { type: "file", accept: ".yaml,.yml,text/yaml,application/x-yaml", class: "sr-only", id: "ana-import-file" });
    file.addEventListener("change", async () => {
      const f = file.files?.[0];
      if (f) ta.value = await f.text();
    });
    const out = h("div", { "aria-live": "polite" });
    const tpl = st.list?.scenarios?.[0]?.slug;
    mount(
      mainHost,
      card(
        "Import a scenario (YAML)",
        h(
          "div",
          { class: "ana-stack-v" },
          h("p", { class: "ana-sub" }, "Paste a scenario document or load a .yaml file, validate it, then save it as a user scenario. Tip: export a built-in scenario as a starting point."),
          h("div", { class: "ana-row" }, h("label", { class: "btn btn--sm", for: "ana-import-file" }, "Choose file…"), file, tpl ? h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: async () => { const d = await api.get(`/api/scenarios/${tpl}`); ta.value = d.yaml; } }, `Start from ${tpl}`) : null, h("div", { class: "ana-grow" }), field("Slug", slug)),
          ta,
          h(
            "div",
            { class: "ana-row" },
            h("button", { class: "btn btn--sm", type: "button", onclick: () => refresh(out, api.post("/api/scenarios/validate", { yaml: ta.value }), (r) => validationResult(r), { onError: (e) => callout("warn", h("strong", null, "Could not validate: "), e.message) }) }, "Validate"),
            h("button", { class: "btn btn--sm btn--primary", type: "button", onclick: async () => {
              const s = slug.value.trim();
              if (s && !SLUG_RE.test(s)) return toast("Invalid slug", "error");
              try {
                const created = await api.post("/api/scenarios", s ? { yaml: ta.value, slug: s } : { yaml: ta.value });
                toast(`Imported ${created.slug}`, "success");
                await loadList();
                select(created.slug);
              } catch (e) {
                mount(out, callout("warn", h("strong", null, e.status === 409 ? "Slug already exists: " : "Not imported: "), e.message));
              }
            } }, "Save as user scenario"),
            h("button", { class: "btn btn--sm btn--ghost", type: "button", onclick: () => select(st.slug || st.list.scenarios[0]?.slug) }, "Cancel"),
          ),
          out,
        ),
        { categories: ["FICTIONAL"] },
      ),
    );
  }

  /* -------------------------------------------------------------- start */
  try {
    await loadList();
  } catch (e) {
    mount(listHost, errorBox(e));
    return;
  }
  if (st.mode === "import") openImport();
  else await select(st.slug && st.list.scenarios.some((s) => s.slug === st.slug) ? st.slug : st.list.scenarios[st.list.scenarios.length - 1]?.slug);
  void fmtInt;
  void links;
}
