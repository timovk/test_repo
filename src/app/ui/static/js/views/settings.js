/**
 * #/settings — UI preferences persisted on the server (`/api/settings`, stored in app_meta):
 * theme, default playback speed for starting an election night, default election, default
 * municipality-map metric and party colour overrides (applied app-wide through
 * store.partyColor).  Party colours are FICTIONAL display data; overriding them never changes
 * any result.
 */
import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { applyTheme } from "../app.js";
import { card, pageHeader } from "./_shared.js";
import { callout, humanize, statusBadge } from "../components/ana-ui.js";
import { getState, setState } from "../store.js";

const HEX = /^#[0-9a-fA-F]{6}$/;

/** Mirror the server settings into the global store so every view picks them up. */
export function publishSettings(s) {
  setState({
    settings: {
      ...getState().settings,
      partyColors: s.party_colors || {},
      playbackSpeed: s.playback_speed ?? null,
      defaultElectionId: s.default_election_id ?? null,
      mapMetric: s.map_metric || null,
    },
  });
}

export async function render(el) {
  const ctrl = new AbortController();
  const status = h("span", { class: "muted", role: "status", "aria-live": "polite" });
  const body = h("div", { class: "grid" }, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "40%", height: "14px" } })));
  mount(el, pageHeader({ eyebrow: "System", title: "Settings", categories: ["FICTIONAL"], meta: [status] }), body);

  let settings;
  try {
    settings = await api.get("/api/settings", { signal: ctrl.signal });
  } catch (err) {
    if (err?.name !== "AbortError") mount(body, callout("warn", `Could not load the settings: ${err.message}`));
    return () => ctrl.abort();
  }

  async function save(patch, label) {
    status.textContent = "Saving…";
    try {
      settings = await api.put("/api/settings", patch);
      publishSettings(settings);
      status.textContent = `${label} saved`;
      paint();
    } catch (err) {
      status.textContent = "";
      mount(status, statusBadge("fail", `Not saved: ${err.message}`));
    }
  }

  function choice(options, value, onPick, label) {
    return h(
      "div",
      { class: "segmented", role: "group", "aria-label": label },
      options.map((o) =>
        h(
          "button",
          { type: "button", class: String(o.value) === String(value) ? "is-active" : "", "aria-pressed": String(String(o.value) === String(value)), onclick: () => onPick(o.value) },
          o.label,
        ),
      ),
    );
  }

  function row(label, hint, control) {
    return h("div", { class: "settings-row" }, h("div", null, h("div", { class: "settings-row__label" }, label), hint ? h("div", { class: "muted settings-row__hint" }, hint) : null), h("div", { class: "settings-row__control" }, control));
  }

  function paint() {
    const opts = settings.options || {};
    const elections = getState().meta?.elections || [];
    const theme = document.documentElement.dataset.theme || settings.theme;
    const colors = { ...(settings.party_colors || {}) };
    const partyRows = (opts.parties || []).map((p) => {
      const current = colors[p.code] || p.color;
      const input = h("input", {
        type: "color",
        value: HEX.test(current) ? current : "#888888",
        "aria-label": `Colour of ${p.name}`,
        oninput: (e) => {
          swatch.style.setProperty("--party", e.target.value);
        },
        onchange: (e) => save({ party_colors: { ...(settings.party_colors || {}), [p.code]: e.target.value } }, `${p.code} colour`),
      });
      const swatch = h("span", { class: "chip__swatch", style: { "--party": current, width: "14px", height: "14px" } });
      const overridden = Boolean(colors[p.code]);
      return h(
        "tr",
        null,
        h("td", null, h("span", { class: "legend__item" }, swatch, h("b", null, p.code))),
        h("td", null, p.name),
        h("td", { class: "mono" }, p.color),
        h("td", null, input),
        h(
          "td",
          null,
          overridden
            ? h("button", {
                type: "button",
                class: "btn btn--sm btn--ghost",
                onclick: () => {
                  const next = { ...(settings.party_colors || {}) };
                  delete next[p.code];
                  save({ party_colors: next }, `${p.code} colour reset`);
                },
              }, "Use default")
            : h("span", { class: "muted" }, "default"),
        ),
      );
    });

    mount(
      body,
      card(
        "Display",
        h(
          "div",
          { class: "settings-list" },
          row("Theme", "Dark is the broadcast default; light suits print and bright rooms.", choice((opts.themes || ["dark", "light"]).map((t) => ({ value: t, label: humanize(t) })), theme, (t) => {
            applyTheme(t);
            save({ theme: t }, "Theme");
          }, "Theme")),
          row(
            "Default municipality map",
            "Metric shown first on the Municipalities map.",
            choice((opts.map_metrics || []).map((m) => ({ value: m, label: humanize(m) })), settings.map_metric, (m) => save({ map_metric: m }, "Map metric"), "Default map metric"),
          ),
        ),
      ),
      card(
        "Election night",
        h(
          "div",
          { class: "settings-list" },
          row(
            "Playback speed when starting a night",
            "1× ≈ 8–9 minutes for a whole night, 25× ≈ 20 seconds. You can always change speed from the top bar.",
            choice((opts.speeds || [1, 2, 5, 10, 25]).map((s) => ({ value: s, label: `${s}×` })), settings.playback_speed, (s) => save({ playback_speed: Number(s) }, "Playback speed"), "Default playback speed"),
          ),
          row(
            "Default election",
            "Election opened when no election is chosen in the address bar.",
            h(
              "select",
              {
                class: "select",
                "aria-label": "Default election",
                onchange: (e) => save({ default_election_id: e.target.value ? Number(e.target.value) : null }, "Default election"),
              },
              h("option", { value: "", selected: settings.default_election_id === null }, "Automatic (the demo election)"),
              elections.map((e) => h("option", { value: e.id, selected: e.id === settings.default_election_id }, `${e.year} · ${e.name}`)),
            ),
          ),
        ),
      ),
      card(
        "Party colours",
        h(
          "div",
          null,
          callout("info", "Party colours are editable display data (FICTIONAL parties). The defaults were checked for colour-vision separation and contrast in both themes; custom colours may be harder to tell apart. Changes apply everywhere immediately."),
          h(
            "table",
            { class: "data", style: { marginTop: "12px" } },
            h("thead", null, h("tr", null, h("th", null, "Party"), h("th", null, "Name"), h("th", null, "Default"), h("th", null, "Colour"), h("th", null, ""))),
            h("tbody", null, partyRows),
          ),
        ),
        { categories: ["FICTIONAL"] },
      ),
      card(
        "Reset",
        h(
          "div",
          { class: "settings-list" },
          row(
            "Restore all defaults",
            "Theme, speed, default election, map metric and party colours.",
            h("button", {
              type: "button",
              class: "btn",
              onclick: () => {
                const d = settings.defaults || {};
                applyTheme(d.theme || "dark");
                save({ ...d }, "Defaults");
              },
            }, "Reset settings"),
          ),
        ),
      ),
    );
  }

  paint();
  return () => ctrl.abort();
}
