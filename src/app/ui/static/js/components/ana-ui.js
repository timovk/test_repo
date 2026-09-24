/**
 * UI helpers for the analysis & system pages (forecast, polling, campaign, history, candidate,
 * scenarios, data, settings).  Accessible tabs / segmented controls, form fields, status
 * badges (icon + label, never colour alone), probability cells, toasts and small utilities.
 * Everything is built with h()/svg(): API text is never parsed as HTML.
 */
import { h, mount } from "../dom.js";
import { getState, partyColor } from "../store.js";
import { fmtProb } from "../format.js";
import { icon } from "./icons.js";
import { provBadge } from "./badges.js";

/* ------------------------------------------------------------------ parties */

/** Party record from meta (name, abbreviation, colour) — colour honours user overrides. */
export function partyInfo(code, fallbackColor) {
  const p = (getState().meta?.parties || []).find((x) => x.code === code);
  return {
    code,
    name: p?.name || (code === "independent" ? "Independent" : code),
    abbr: p?.abbreviation || (code === "independent" ? "IND" : code),
    color: partyColor(code, fallbackColor || p?.color || (code === "independent" ? "#8A8A8A" : undefined)),
  };
}

/** Stable party order (meta order) — colour and position follow the entity, never its rank. */
export function partyOrder() {
  return (getState().meta?.parties || []).map((p) => p.code);
}

export function sortByPartyOrder(codes) {
  const order = partyOrder();
  const idx = (c) => {
    const i = order.indexOf(c);
    return i < 0 ? 999 : i;
  };
  return [...codes].sort((a, b) => idx(a) - idx(b));
}

/** Party chip that always shows the code next to its swatch (secondary encoding). */
export function pchip(code, fallbackColor, { name } = {}) {
  const p = partyInfo(code, fallbackColor);
  return h(
    "span",
    { class: "chip", style: { "--party": p.color }, title: name || p.name },
    h("span", { class: "chip__swatch" }),
    code === "independent" ? "IND" : code || "–",
  );
}

/* ------------------------------------------------------------------ controls */

/**
 * Accessible tab list (role=tablist, arrow-key navigation).  `items`: [{key, label}].
 * Returns the element; `onSelect(key)` is called on activation.
 */
export function tabBar(items, active, onSelect, { label = "Sections" } = {}) {
  const buttons = [];
  const bar = h("div", { class: "tabs ana-tabs", role: "tablist", "aria-label": label });
  const activate = (key, focus) => {
    buttons.forEach((b) => {
      const on = b.dataset.key === key;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    onSelect(key);
  };
  for (const it of items) {
    const b = h(
      "button",
      {
        type: "button",
        role: "tab",
        "data-key": it.key,
        class: it.key === active ? "is-active" : "",
        "aria-selected": it.key === active ? "true" : "false",
        tabindex: it.key === active ? "0" : "-1",
        onclick: () => activate(it.key),
        onkeydown: (e) => {
          const i = buttons.indexOf(e.currentTarget);
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const n = buttons[(i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
            activate(n.dataset.key, true);
          }
        },
      },
      it.label,
      it.badge != null ? h("span", { class: "ana-tabs__badge" }, String(it.badge)) : null,
    );
    buttons.push(b);
    bar.appendChild(b);
  }
  bar.select = (key) => activate(key);
  return bar;
}

/** Segmented control. options: [{value, label}] | values. */
export function segmented(options, value, onChange, { label = "Options", disabled = false } = {}) {
  const opts = options.map((o) => (typeof o === "object" ? o : { value: o, label: String(o) }));
  const group = h("div", { class: "segmented", role: "radiogroup", "aria-label": label });
  const btns = opts.map((o) =>
    h(
      "button",
      {
        type: "button",
        role: "radio",
        "aria-checked": String(o.value) === String(value) ? "true" : "false",
        class: String(o.value) === String(value) ? "is-active" : "",
        disabled,
        title: o.title,
        onclick: () => {
          btns.forEach((b, i) => {
            const on = String(opts[i].value) === String(o.value);
            b.classList.toggle("is-active", on);
            b.setAttribute("aria-checked", on ? "true" : "false");
          });
          onChange(o.value);
        },
      },
      o.label,
    ),
  );
  btns.forEach((b) => group.appendChild(b));
  return group;
}

/** <select> with options [{value,label}] | values. */
export function selectBox(options, value, onChange, attrs = {}) {
  return h(
    "select",
    { class: "select", onchange: (e) => onChange(e.target.value), ...attrs },
    options.map((o) => {
      const opt = typeof o === "object" ? o : { value: o, label: String(o) };
      return h("option", { value: opt.value, selected: String(opt.value) === String(value), disabled: opt.disabled }, opt.label);
    }),
  );
}

let fieldSeq = 0;
/** Labelled form field; `control` gets an id so the label is associated. */
export function field(label, control, hint) {
  const id = control.id || `ana-f${++fieldSeq}`;
  control.id = id;
  return h("div", { class: "ana-field" }, h("label", { class: "ana-field__label", for: id }, label), control, hint ? h("div", { class: "ana-field__hint" }, hint) : null);
}

/** Filter row: one left-aligned row above the content it scopes. */
export function filterRow(...children) {
  return h("div", { class: "ana-filters", role: "group", "aria-label": "Filters" }, ...children);
}

export function labelled(label, control) {
  return h("label", { class: "ana-inline" }, h("span", { class: "ana-inline__label" }, label), control);
}

/* ------------------------------------------------------------------ display */

/** Key/value grid. rows: [[label, value], …] */
export function kv(rows, { cols = 2 } = {}) {
  return h(
    "dl",
    { class: "ana-kv", style: { "--kv-cols": cols } },
    rows.filter(Boolean).map(([k, v]) => h("div", { class: "ana-kv__row" }, h("dt", null, k), h("dd", null, v ?? "–"))),
  );
}

/** Status badge with icon + label (status colours are reserved and never used alone). */
export function statusBadge(kind, label) {
  const map = { ok: ["check", "good"], warn: ["alert", "warning"], fail: ["alert", "critical"], info: ["alert", "info"] };
  const [ico, cls] = map[kind] || map.info;
  return h("span", { class: `ana-status ana-status--${cls}` }, icon(ico, { size: 12 }), label);
}

/** Probability meter cell (bar in the entity colour + value text in ink). */
export function probCell(p, color, { width = 72 } = {}) {
  if (p === null || p === undefined) return h("span", { class: "muted" }, "–");
  return h(
    "div",
    { class: "ana-prob", title: `${(p * 100).toFixed(1)}%` },
    h("div", { class: "ana-prob__track", style: { width: `${width}px` } }, h("div", { class: "ana-prob__fill", style: { width: `${Math.max(0, Math.min(1, p)) * 100}%`, "--party": color || "var(--seq-4)" } })),
    h("span", { class: "ana-prob__val num" }, fmtProb(p)),
  );
}

/** Hero-style KPI tile (proportional figures; label sentence case). */
export function kpi(label, value, sub, { accent } = {}) {
  return h(
    "div",
    { class: "ana-kpi", style: accent ? { "--party": accent } : undefined },
    h("div", { class: "ana-kpi__label" }, label),
    h("div", { class: "ana-kpi__value" }, value),
    sub ? h("div", { class: "ana-kpi__sub" }, sub) : null,
  );
}

/** Provenance legend row explaining the badges on the page. */
export function provLegend(cats) {
  const text = {
    REAL: "official Dutch open data (CBS / PDOK)",
    DERIVED: "computed from real data",
    FICTIONAL: "invented construct of this simulator",
    SIMULATED: "model output — not a real result or prediction",
  };
  return h("div", { class: "ana-provlegend" }, cats.map((c) => h("span", { class: "ana-provlegend__item" }, provBadge(c), h("span", null, text[c] || ""))));
}

/** Callout (info / warning) with an icon and text nodes. */
export function callout(kind, ...children) {
  return h("div", { class: `notice ana-callout ana-callout--${kind}`, role: kind === "warn" ? "alert" : "note" }, icon(kind === "warn" ? "alert" : kind === "ok" ? "check" : "alert", { size: 16, className: "ana-callout__icon" }), h("div", null, ...children));
}

/** Download link (native <a download>). */
export function downloadLink(href, label, { primary = false, small = true } = {}) {
  return h("a", { class: ["btn", small && "btn--sm", primary && "btn--primary"], href, download: "", rel: "noopener" }, icon("download", { size: 13 }), label);
}

/** Small copy-to-clipboard button. */
export function copyButton(text, label = "Copy") {
  const b = h("button", {
    type: "button",
    class: "btn btn--sm btn--ghost",
    "aria-label": `${label} to clipboard`,
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        b.textContent = "Copied";
      } catch {
        b.textContent = "Copy failed";
      }
      setTimeout(() => (b.textContent = label), 1400);
    },
  }, label);
  return b;
}

/* ------------------------------------------------------------------ toasts */
let toastHost = null;
export function toast(message, kind = "info") {
  if (!toastHost) {
    toastHost = h("div", { class: "ana-toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(toastHost);
  }
  const t = h("div", { class: `ana-toast ana-toast--${kind}` }, icon(kind === "error" ? "alert" : "check", { size: 14 }), h("span", null, message));
  toastHost.appendChild(t);
  setTimeout(() => t.classList.add("is-leaving"), 3600);
  setTimeout(() => t.remove(), 4200);
}

/* ------------------------------------------------------------------ async */

/** Render into `el` keeping the previous content dimmed while refetching (no skeleton flash). */
export async function refresh(el, promise, render, { onError } = {}) {
  const hadContent = el.childNodes.length > 0 && !el.querySelector(":scope > .state");
  if (hadContent) el.classList.add("ana-busy");
  else mount(el, h("div", { class: "state" }, h("div", { class: "skeleton", style: { width: "50%", height: "14px" } })));
  try {
    const data = await promise;
    el.classList.remove("ana-busy");
    mount(el, render(data));
    return data;
  } catch (err) {
    el.classList.remove("ana-busy");
    if (err?.name === "AbortError") return null;
    console.warn(err);
    mount(el, onError ? onError(err) : errorBox(err));
    return null;
  }
}

export function errorBox(err, title) {
  const status = err?.status;
  return h(
    "div",
    { class: "state ana-error" },
    h("strong", null, title || (status === 404 ? "Not available" : status === 409 ? "Not possible for this election" : "Something went wrong")),
    h("span", { class: "muted" }, err?.message || String(err)),
  );
}

/** Run async tasks with a concurrency limit. */
export async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const k = i++;
      try {
        out[k] = await fn(items[k], k);
      } catch (e) {
        out[k] = { error: e };
      }
    }
  });
  await Promise.all(workers);
  return out;
}

/** Interval runner with cleanup. */
export function every(ms, fn) {
  let stopped = false;
  let t = null;
  const tick = async () => {
    if (stopped) return;
    try {
      await fn();
    } finally {
      if (!stopped) t = setTimeout(tick, ms);
    }
  };
  t = setTimeout(tick, ms);
  return () => {
    stopped = true;
    clearTimeout(t);
  };
}

/* ------------------------------------------------------------------ misc */

export const provinceName = (code) => {
  const names = { GR: "Groningen", FR: "Fryslân", DR: "Drenthe", OV: "Overijssel", FL: "Flevoland", GE: "Gelderland", UT: "Utrecht", NH: "Noord-Holland", ZH: "Zuid-Holland", ZE: "Zeeland", NB: "Noord-Brabant", LI: "Limburg" };
  return names[code] || code;
};
export const PROVINCES = ["GR", "FR", "DR", "OV", "FL", "GE", "UT", "NH", "ZH", "ZE", "NB", "LI"];

export const humanize = (s) => (s ? String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "");

export const fmtDate = (s) => {
  if (!s) return "–";
  const d = new Date(`${String(s).slice(0, 10)}T12:00:00`);
  if (Number.isNaN(d.getTime())) return String(s);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
};
export const fmtDateShort = (s) => {
  const d = s instanceof Date ? s : new Date(`${String(s).slice(0, 10)}T12:00:00`);
  return Number.isNaN(d.getTime()) ? String(s) : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
};
export const fmtDateTime = (s) => {
  if (!s) return "–";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? String(s) : d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
};
export const fmtBytes = (n) => {
  if (n === null || n === undefined) return "–";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kB`;
  return `${n} B`;
};

/** Election brief by id from meta. */
export function electionById(id) {
  return (getState().meta?.elections || []).find((e) => e.id === Number(id)) || null;
}

/** Results-source pill: FINAL / LIVE / HIDDEN (label + icon). */
export function sourcePill(src) {
  if (src === "live") return h("span", { class: "pill pill--close ana-src" }, h("span", { class: "live-dot" }), "Live count");
  if (src === "final") return h("span", { class: "pill ana-src" }, icon("lock", { size: 11, className: "pill__icon" }), "Final results");
  return h("span", { class: "pill pill--closed ana-src" }, icon("lock", { size: 11, className: "pill__icon" }), "Results hidden");
}
