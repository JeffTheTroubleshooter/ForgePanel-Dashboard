// Pure helpers for the tile grid drag-and-drop (no DOM access, unit-testable in node).
// rects: layout boxes {left, top, width, height} of the OTHER tiles of one grid, in DOM order.
// Returns the index (0..rects.length) where a tile dropped at point (x, y) should be inserted.
function insertionIndex(rects, x, y) {
  if (!rects.length) return 0;
  // group tiles into rows by their top edge (CSS grid rows share a top)
  const rows = [];
  rects.forEach((r, i) => {
    const row = rows.find(w => Math.abs(w.top - r.top) < Math.max(4, r.height / 2));
    if (row) { row.items.push(i); row.bottom = Math.max(row.bottom, r.top + r.height); }
    else rows.push({ top: r.top, bottom: r.top + r.height, items: [i] });
  });
  rows.sort((a, b) => a.top - b.top);
  // pick the row whose vertical band holds y (bands split halfway through the row gap)
  let row = rows[rows.length - 1];
  for (let k = 0; k < rows.length; k++) {
    const next = rows[k + 1];
    const limit = next ? (rows[k].bottom + next.top) / 2 : Infinity;
    if (y < limit) { row = rows[k]; break; }
  }
  // in that row: before the first tile whose horizontal center is right of x
  for (const i of row.items) {
    const r = rects[i];
    if (x < r.left + r.width / 2) return i;
  }
  const last = row.items[row.items.length - 1];
  return last + 1;
}
if (typeof module !== "undefined") module.exports = { insertionIndex };
