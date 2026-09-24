/**
 * Tiny DOM helpers (no framework).  `h(tag, attrs, ...children)` builds elements; strings are
 * text nodes (never parsed as HTML, so API data can't inject markup).  `svg()` builds SVG nodes.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class" || k === "className") el.setAttribute("class", Array.isArray(v) ? v.filter(Boolean).join(" ") : v);
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv === undefined || sv === null) continue;
        if (sk.startsWith("--")) el.style.setProperty(sk, sv);
        else el.style[sk] = sv;
      }
    } else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

export function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "style" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) if (sv != null) el.style.setProperty(sk, sv);
      } else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

/** Replace `el` content only when the key changed (cheap diffing for live updates). */
export function keyed(el, key, render) {
  if (el.dataset.key === key) return el;
  el.dataset.key = key;
  mount(el, render());
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
