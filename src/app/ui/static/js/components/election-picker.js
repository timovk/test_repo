/**
 * Searchable election picker for the national strip.  With hundreds of (mostly local) elections a
 * plain <select> is unusable, so this is a button + popover listbox:
 *   * search box (name, province codes and names, date, year, status),
 *   * filter All / Regular / Local,
 *   * groups: "Regular elections" first, then "Local elections · <year>" (newest year first),
 *   * keyboard: ↑/↓/Home/End move, Enter selects, Esc closes.
 * Selecting calls `onSelect(id)`.  `update(elections, currentId)` repaints in place.
 */
import { h, mount } from "../dom.js";
import { fmtInt } from "../format.js";
import { icon } from "./icons.js";
import { electionProvinces, fmtDate, provinceName, provincesText } from "./local-kit.js";

const STATUS = {
  live: ["live", "Live"],
  final: ["final", "Final"],
  certified: ["final", "Final"],
  simulated: ["ready", "Not reported"],
  scheduled: ["scheduled", "Scheduled"],
};

export const statusInfo = (e) => STATUS[e?.status] || ["scheduled", e?.status || "–"];

/**
 * Display label of an election brief: {title, sub, tip}.  A local election (every province voting
 * on its date) reads "7 Feb 2029 · OV, ZE, NB" with the province names in `sub` / `tip`.
 */
export function electionLabel(e) {
  if (!e) return { title: "–", sub: "", tip: "" };
  if (e.local) {
    const codes = electionProvinces(e);
    const names = provincesText(codes, { names: true });
    return {
      title: codes.length ? `${fmtDate(e.election_date)} · ${provincesText(codes)}` : `${fmtDate(e.election_date)} · Local`,
      sub: names ? `Local · ${names}` : "Local elections",
      tip: `${e.name}${names ? ` — ${names}` : ""}`,
    };
  }
  return { title: e.name, sub: `${fmtDate(e.election_date, "long")} · ${e.election_type || "election"}`, tip: e.name };
}

/** Grouped elections: [{key, label, items}] — regular first, then local by year (newest first). */
export function groupElections(elections = []) {
  const byDateDesc = (a, b) => (a.election_date < b.election_date ? 1 : a.election_date > b.election_date ? -1 : b.id - a.id);
  const regular = elections.filter((e) => !e.local).sort(byDateDesc);
  const local = elections.filter((e) => e.local).sort(byDateDesc);
  const groups = [];
  if (regular.length) groups.push({ key: "regular", label: "Regular elections", items: regular });
  const years = [...new Set(local.map((e) => e.year))].sort((a, b) => b - a);
  for (const y of years) groups.push({ key: `local-${y}`, label: `Local elections · ${y}`, items: local.filter((e) => e.year === y) });
  return groups;
}

let uid = 0;

export function electionPickerButton({ onSelect } = {}) {
  const listId = `ep-list-${++uid}`;
  const st = { elections: [], currentId: null, filter: "all", q: "", active: 0, open: false, flat: [] };
  const btnText = h("span", { class: "ep-btn__text" });
  const btnDot = h("span", { class: "ep-dot" });
  const btn = h(
    "button",
    { class: "ep-btn", type: "button", "aria-haspopup": "listbox", "aria-expanded": "false", "aria-controls": listId, onclick: () => (st.open ? close() : open()) },
    btnDot,
    btnText,
    icon("chevronDown", { size: 12, className: "ep-btn__chev" }),
  );
  const input = h("input", {
    class: "input ep-search",
    type: "search",
    placeholder: "Search elections, provinces, dates…",
    "aria-label": "Search elections",
    "aria-controls": listId,
    autocomplete: "off",
    oninput: (e) => {
      st.q = e.target.value.trim().toLowerCase();
      st.active = 0;
      paintList();
    },
  });
  const segBtns = [
    ["all", "All"],
    ["regular", "Regular"],
    ["local", "Local"],
  ].map(([k, l]) => {
    const b = h("button", { type: "button", "data-k": k, onclick: () => setFilter(k) }, l);
    return b;
  });
  const seg = h("div", { class: "segmented ep-seg", role: "group", "aria-label": "Election kind" }, segBtns);
  const list = h("div", { class: "ep-list", role: "listbox", id: listId, "aria-label": "Elections", tabindex: "-1" });
  const foot = h("div", { class: "ep-foot" });
  const pop = h(
    "div",
    { class: "ep-pop", hidden: true, role: "dialog", "aria-label": "Choose the election shown on every page" },
    h("div", { class: "ep-head" }, h("label", { class: "ep-searchbox" }, icon("search", { size: 14 }), input), seg),
    list,
    foot,
  );
  const el = h("div", { class: "ep" }, btn, pop);

  function setFilter(k) {
    st.filter = k;
    st.active = 0;
    paintSeg();
    paintList();
    input.focus();
  }

  function paintSeg() {
    for (const b of segBtns) {
      const on = b.dataset.k === st.filter;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }

  function paintButton() {
    const cur = st.elections.find((e) => e.id === st.currentId);
    const lab = electionLabel(cur);
    btnText.textContent = cur ? lab.title : "Choose an election";
    const [cls, word] = statusInfo(cur);
    btnDot.className = `ep-dot ep-dot--${cls}`;
    btn.title = cur ? `${lab.tip} (${word}) — choose the election shown on every page` : "Choose the election shown on every page";
    btn.setAttribute("aria-label", cur ? `Election: ${cur.name}, ${word}. Change election` : "Choose an election");
  }

  function matches(e) {
    if (st.filter === "regular" && e.local) return false;
    if (st.filter === "local" && !e.local) return false;
    if (!st.q) return true;
    const lab = electionLabel(e);
    const codes = electionProvinces(e);
    const hay = [e.name, lab.title, lab.sub, ...codes, ...codes.map((c) => provinceName(c)), e.election_date, fmtDate(e.election_date, "long"), String(e.year), e.status, statusInfo(e)[1], e.election_type]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return st.q.split(/\s+/).every((w) => hay.includes(w));
  }

  function paintList() {
    const groups = groupElections(st.elections.filter(matches));
    st.flat = groups.flatMap((g) => g.items);
    if (st.active >= st.flat.length) st.active = Math.max(0, st.flat.length - 1);
    let i = 0;
    mount(
      list,
      groups.length
        ? groups.map((g) =>
            h(
              "div",
              { class: "ep-group", role: "group", "aria-label": g.label },
              h("div", { class: "ep-group__head" }, h("span", null, g.label), h("span", { class: "num" }, fmtInt(g.items.length))),
              g.items.map((e) => item(e, i++)),
            ),
          )
        : h("div", { class: "ep-empty" }, `No election matches “${st.q}”.`),
    );
    const nLocal = st.elections.filter((e) => e.local).length;
    foot.textContent = `${fmtInt(st.elections.length - nLocal)} regular · ${fmtInt(nLocal)} local elections${st.q || st.filter !== "all" ? ` · ${fmtInt(st.flat.length)} shown` : ""}`;
    scrollActive();
  }

  function item(e, idx) {
    const lab = electionLabel(e);
    const [cls, word] = statusInfo(e);
    const cur = e.id === st.currentId;
    return h(
      "div",
      {
        class: ["ep-item", cur && "is-current", idx === st.active && "is-active"],
        role: "option",
        id: `${listId}-${e.id}`,
        "aria-selected": String(cur),
        "data-idx": idx,
        title: lab.tip,
        onmousemove: () => setActive(idx, false),
        onclick: () => choose(e.id),
      },
      h("span", { class: `ep-dot ep-dot--${cls}`, "aria-hidden": "true" }),
      h("span", { class: "ep-item__main" }, h("span", { class: "ep-item__title" }, lab.title), h("span", { class: "ep-item__sub" }, lab.sub)),
      h("span", { class: `ep-status ep-status--${cls}` }, cls === "live" ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null, word),
      cur ? h("span", { class: "ep-item__check", title: "Selected" }, icon("check", { size: 12 })) : h("span", { class: "ep-item__check" }),
    );
  }

  function setActive(idx, scroll = true) {
    if (idx === st.active) return;
    list.querySelector(".ep-item.is-active")?.classList.remove("is-active");
    st.active = idx;
    const node = list.querySelector(`.ep-item[data-idx="${idx}"]`);
    node?.classList.add("is-active");
    if (node) input.setAttribute("aria-activedescendant", node.id);
    if (scroll) scrollActive();
  }

  function scrollActive() {
    const node = list.querySelector(".ep-item.is-active");
    if (node) {
      input.setAttribute("aria-activedescendant", node.id);
      const top = node.offsetTop; // .ep-list is the offset parent (position: relative)
      if (top < list.scrollTop + 28) list.scrollTop = Math.max(0, top - 28);
      else if (top + node.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = top + node.offsetHeight - list.clientHeight + 4;
    }
  }

  function choose(id) {
    close(true);
    if (id !== st.currentId) onSelect?.(id);
  }

  function onKey(e) {
    if (!st.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(st.flat.length - 1, st.active + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(0, st.active - 1));
    } else if (e.key === "Home" && document.activeElement !== input) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && document.activeElement !== input) {
      e.preventDefault();
      setActive(st.flat.length - 1);
    } else if (e.key === "Enter") {
      const target = st.flat[st.active];
      if (target) {
        e.preventDefault();
        choose(target.id);
      }
    }
  }

  function onDocDown(e) {
    if (!el.contains(e.target)) close(false);
  }

  function open() {
    st.open = true;
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    el.classList.add("is-open");
    st.q = "";
    input.value = "";
    paintSeg();
    // Start on the selected election.
    const groups = groupElections(st.elections.filter(matches));
    const flat = groups.flatMap((g) => g.items);
    st.active = Math.max(0, flat.findIndex((e) => e.id === st.currentId));
    paintList();
    document.addEventListener("mousedown", onDocDown, true);
    document.addEventListener("keydown", onKey, true);
    requestAnimationFrame(() => {
      input.focus();
      scrollActive();
    });
  }

  function close(focusButton) {
    if (!st.open) return;
    st.open = false;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    el.classList.remove("is-open");
    document.removeEventListener("mousedown", onDocDown, true);
    document.removeEventListener("keydown", onKey, true);
    if (focusButton) btn.focus();
  }

  el.update = (elections, currentId) => {
    st.elections = elections || [];
    st.currentId = currentId;
    paintButton();
    if (st.open) paintList();
  };
  el.close = () => close(false);
  return el;
}
