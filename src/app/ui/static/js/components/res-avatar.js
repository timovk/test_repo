/**
 * Placeholder portrait for FICTIONAL people on the results pages.  Same look as
 * components/avatar.js (initials on a party gradient with a seeded pattern), but the seed is
 * shifted unsigned (`>>>`) so circle radii / positions are never negative — avatar.js uses `>>`
 * on a uint32 seed, which logs "<circle> attribute r: A negative value" console errors — and each
 * gradient id is unique even when the same person appears twice on a page.
 */
import { svg } from "../dom.js";
import { initials } from "./avatar.js";

function hash(str) {
  let x = 2166136261;
  for (let i = 0; i < str.length; i++) {
    x ^= str.charCodeAt(i);
    x = Math.imul(x, 16777619);
  }
  return x >>> 0;
}

let uid = 0;
export function resAvatar(name, { color = "#3a475f", key = name, size = 56 } = {}) {
  const seed = hash(key || name || "x");
  const id = `resav${seed.toString(36)}_${++uid}`;
  const shapes = [];
  for (let i = 0; i < 4; i++) {
    const r = 8 + ((seed >>> (i * 5)) % 22);
    const cx = (seed >>> (i * 3)) % 64;
    const cy = (seed >>> (i * 4 + 2)) % 64;
    shapes.push(svg("circle", { cx, cy, r, fill: "#fff", opacity: 0.06 + (i % 2) * 0.04 }));
  }
  return svg(
    "svg",
    { viewBox: "0 0 64 64", width: size, height: size, role: "img", "aria-label": `Placeholder portrait of ${name} (fictional person)` },
    svg(
      "defs",
      null,
      svg(
        "linearGradient",
        { id, x1: "0", y1: "0", x2: "1", y2: "1", gradientTransform: `rotate(${(seed % 360) % 90})` },
        svg("stop", { offset: "0", "stop-color": color }),
        svg("stop", { offset: "1", "stop-color": "#0b1220" }),
      ),
    ),
    svg("rect", { width: 64, height: 64, fill: `url(#${id})` }),
    ...shapes,
    svg("circle", { cx: 32, cy: 26, r: 11, fill: "#fff", opacity: 0.18 }),
    svg("path", { d: "M12 60c2-12 10-18 20-18s18 6 20 18z", fill: "#fff", opacity: 0.18 }),
    svg("text", { x: 32, y: 38, "text-anchor": "middle", "font-size": 18, "font-weight": 800, fill: "#fff", "font-family": "Inter, sans-serif" }, initials(name)),
  );
}
