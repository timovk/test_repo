/** Number / text formatting (nl-NL style grouping, tabular numbers). */

const nf0 = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const fmtInt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : nf0.format(v));
export const fmtVotes = fmtInt;
export const fmt1 = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : nf1.format(v));
export const fmt2 = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : nf2.format(v));

/** Percentage from a 0–100 number. */
export const fmtPct = (v, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "–" : `${digits === 2 ? nf2.format(v) : nf1.format(v)}%`;
/** Percentage from a 0–1 share. */
export const fmtShare = (v, digits = 1) => (v === null || v === undefined ? "–" : fmtPct(v * 100, digits));
/** Signed percentage points. */
export const fmtPP = (v, digits = 1) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = digits === 2 ? nf2.format(Math.abs(v)) : nf1.format(Math.abs(v));
  return `${v > 0 ? "+" : v < 0 ? "−" : "±"}${s}`;
};
export const fmtSigned = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${nf0.format(Math.abs(v))}`);
export const fmtCompact = (v) => {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v);
  if (a >= 1e6) return `${nf1.format(v / 1e6)}M`;
  if (a >= 1e3) return `${nf1.format(v / 1e3)}k`;
  return nf0.format(v);
};
export const fmtProb = (p) => {
  if (p === null || p === undefined) return "–";
  if (p > 0.99) return ">99%";
  if (p < 0.01 && p > 0) return "<1%";
  return `${Math.round(p * 100)}%`;
};

export const STATUS_LABELS = {
  SCHEDULED: "Scheduled",
  POLLS_CLOSED: "Polls closed",
  TOO_EARLY_TO_CALL: "Too early to call",
  TOO_CLOSE_TO_CALL: "Too close to call",
  LEAN: "Lean",
  PROJECTED_WINNER: "Projected",
  CALLED: "Called",
  RECOUNT: "Recount",
  FINAL: "Final",
};

export const titleCase = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : "");
