/**
 * Deterministic placeholder portraits (FICTIONAL people have no photos): initials on a party
 * gradient with a subtle geometric pattern seeded by the portrait key.
 */
import { svg } from "../dom.js";

function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function initials(name = "") {
  const parts = name
    .replace(/\b(van|de|der|den|het|ten|ter|te|in|'t)\b/gi, " ")
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

export function avatar(name, { color = "#3a475f", key = name, size = 56 } = {}) {
  const seed = hash(key || name || "x");
  const id = `av${seed.toString(36)}`;
  const angle = seed % 360;
  const shapes = [];
  for (let i = 0; i < 4; i++) {
    const r = 8 + ((seed >> (i * 5)) % 22);
    const cx = (seed >> (i * 3)) % 64;
    const cy = (seed >> (i * 4 + 2)) % 64;
    shapes.push(svg("circle", { cx, cy, r, fill: "#fff", opacity: 0.06 + (i % 2) * 0.04 }));
  }
  return svg(
    "svg",
    { viewBox: "0 0 64 64", width: size, height: size, role: "img", "aria-label": `Placeholder portrait of ${name}` },
    svg(
      "defs",
      null,
      svg(
        "linearGradient",
        { id, x1: "0", y1: "0", x2: "1", y2: "1", gradientTransform: `rotate(${angle % 90})` },
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
