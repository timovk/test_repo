/**
 * Sortable data table.
 * columns: [{key, label, align:'r'|'l', sort:true|fn, format:(v,row)=>string|Node, value:(row)=>any, width}]
 */
import { h, mount } from "../dom.js";

export function dataTable(columns, rows, { sortKey, sortDir = "desc", onRowClick, rowHref, maxHeight, empty = "No data", rowClass, caption } = {}) {
  const wrap = h("div", { class: "table-wrap", style: maxHeight ? { "--table-max-h": `${maxHeight}px` } : undefined });
  let key = sortKey;
  let dir = sortDir;
  const valueOf = (col, row) => (col.value ? col.value(row) : row[col.key]);

  function render() {
    let data = rows;
    if (key) {
      const col = columns.find((c) => c.key === key);
      if (col) {
        data = [...rows].sort((a, b) => {
          const va = valueOf(col, a);
          const vb = valueOf(col, b);
          if (va === vb) return 0;
          if (va === null || va === undefined) return 1;
          if (vb === null || vb === undefined) return -1;
          const cmp = typeof va === "string" ? va.localeCompare(vb, "nl") : va - vb;
          return dir === "asc" ? cmp : -cmp;
        });
      }
    }
    const thead = h(
      "thead",
      null,
      h(
        "tr",
        null,
        columns.map((c) =>
          h(
            "th",
            {
              class: [c.align === "r" && "r", key === c.key && "is-sorted", key === c.key && dir === "asc" && "asc"],
              "data-sort": c.sort === false ? undefined : c.key,
              style: c.width ? { width: c.width } : undefined,
              scope: "col",
              tabindex: c.sort === false ? undefined : "0",
              "aria-sort": key === c.key ? (dir === "asc" ? "ascending" : "descending") : undefined,
              onkeydown: c.sort === false ? undefined : (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  e.currentTarget.click();
                  wrap.querySelector(`th[data-sort="${c.key}"]`)?.focus();
                }
              },
              onclick: c.sort === false ? undefined : () => {
                if (key === c.key) dir = dir === "asc" ? "desc" : "asc";
                else {
                  key = c.key;
                  dir = typeof valueOf(c, rows[0] || {}) === "string" ? "asc" : "desc";
                }
                render();
              },
            },
            c.label,
          ),
        ),
      ),
    );
    const tbody = h(
      "tbody",
      null,
      data.length
        ? data.map((row) =>
            h(
              "tr",
              {
                "data-href": rowHref ? rowHref(row) : undefined,
                class: rowClass ? rowClass(row) : undefined,
                onclick: onRowClick ? () => onRowClick(row) : rowHref ? () => (location.hash = rowHref(row)) : undefined,
              },
              columns.map((c) => {
                const v = valueOf(c, row);
                const out = c.format ? c.format(v, row) : v ?? "–";
                return h("td", { class: c.align === "r" ? "r" : undefined }, out);
              }),
            ),
          )
        : h("tr", null, h("td", { colspan: columns.length, class: "muted" }, empty)),
    );
    mount(wrap, h("table", { class: "data" }, caption ? h("caption", { class: "sr-only" }, caption) : null, thead, tbody));
  }
  render();
  wrap.update = (newRows) => {
    rows = newRows;
    render();
  };
  return wrap;
}

/** Inline share bar for table cells. */
export function barCell(share, color, text) {
  return h(
    "div",
    { class: "bar-cell" },
    h("div", { class: "bar-cell__bar", style: { width: `${Math.max(1, Math.min(100, share * 100)) * 0.8}px`, "--party": color } }),
    h("span", null, text),
  );
}
