/** Inline SVG icon set (stroke icons, 16×16 viewBox 24). */
import { svg } from "../dom.js";

const PATHS = {
  night: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  president: "M12 2l2.9 6.9 7.1.6-5.4 4.7 1.7 7-6.3-3.9-6.3 3.9 1.7-7L2 9.5l7.1-.6z",
  college: "M3 21h18M5 21V10m4 11V10m6 11V10m4 11V10M2 10l10-7 10 7z",
  provinces: "M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3z M9 3v15 M15 6v15",
  municipalities: "M3 3h8v8H3z M13 3h8v8h-8z M3 13h8v8H3z M13 13h8v8h-8z",
  house: "M3 11l9-7 9 7v10H3z M9 21v-6h6v6",
  senate: "M4 20h16 M6 20V9 M10 20V9 M14 20V9 M18 20V9 M3 9l9-5 9 5z",
  governors: "M12 2l3 6 6 1-4.5 4 1 6-5.5-3-5.5 3 1-6L3 9l6-1z",
  forecast: "M3 20l6-8 4 5 8-12",
  polling: "M4 20V10 M10 20V4 M16 20v-7 M22 20H2",
  campaign: "M3 11v2a2 2 0 0 0 2 2h2l5 4V5L7 9H5a2 2 0 0 0-2 2z M16 8a5 5 0 0 1 0 8",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5 M12 7v5l3 3",
  scenario: "M4 4h16v16H4z M8 9h8 M8 13h8 M8 17h5",
  data: "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3z M3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6 M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  play: "M6 4l14 8-14 8z",
  pause: "M6 4h4v16H6z M14 4h4v16h-4z",
  step: "M5 4l10 8-10 8z M17 4h2v16h-2z",
  finish: "M4 4l8 8-8 8z M12 4l8 8-8 8z",
  reset: "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5",
  check: "M4 12l5 5L20 6",
  lock: "M6 11h12v10H6z M8 11V7a4 4 0 0 1 8 0v4",
  flip: "M7 7h13l-4-4 M17 17H4l4 4",
  alert: "M12 3l10 18H2z M12 10v5 M12 18h.01",
  download: "M12 3v12 M7 10l5 5 5-5 M4 21h16",
  sun: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
};

export function icon(name, { size = 16, className = "" } = {}) {
  const d = PATHS[name] || PATHS.alert;
  return svg(
    "svg",
    {
      viewBox: "0 0 24 24",
      width: size,
      height: size,
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      class: className,
      "aria-hidden": "true",
    },
    ...d.split(" M").map((seg, i) => svg("path", { d: i === 0 ? seg : `M${seg}` })),
  );
}
