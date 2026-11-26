const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

const TAPE = ["SPY", "QQQ", "IWM", "VIX", "DXY", "BTC", "US2Y", "US10Y"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return `${String(d).padStart(2, "0")} ${MONTHS[m - 1]} ${y}`;
}

function fmtStamp(iso) {
  if (!iso) return "";
  const date = iso.slice(0, 10);
  const time = iso.slice(11, 16);
  return `${fmtDate(date)} ${time} UTC`;
}

function fmtLast(value, decimals) {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtMove(value, kind) {
  if (value == null || Number.isNaN(value)) return { text: "—", cls: "flat" };
  const eps = kind === "yield" ? 0.05 : 0.005;
  if (Math.abs(value) < eps) {
    return { text: kind === "yield" ? "0bp" : "0.00%", cls: "flat" };
  }
  const sign = value > 0 ? "+" : "";
  if (kind === "yield") {
    const digits = Math.abs(value - Math.round(value)) < 0.05 ? 0 : 1;
    return { text: `${sign}${value.toFixed(digits)}bp`, cls: value > 0 ? "up" : "down" };
  }
  return { text: `${sign}${value.toFixed(2)}%`, cls: value > 0 ? "up" : "down" };
}

function fmtPp(value) {
  if (value == null || Number.isNaN(value)) return null;
  if (Math.abs(value) < 0.005) return { text: "0.00pp", cls: "flat" };
  const sign = value > 0 ? "+" : "";
  return { text: `${sign}${value.toFixed(2)}pp`, cls: value > 0 ? "up" : "down" };
}

function indexRows(data) {
  const map = new Map();
  const prices = [];
  for (const group of data.groups || []) {
    for (const row of group.rows) {
      map.set(row.id, row);
      if (row.kind !== "yield") prices.push(row);
    }
  }
  return { map, prices };
}

function vixTag(level) {
  if (level == null) return "";
  if (level >= 30) return "STRESS";
  if (level >= 20) return "ELEVATED";
  if (level < 14) return "LOW";
  return "";
}

function addItem(line, label, lastText, move, tag) {
  const item = el("span", "item");
  item.append(el("span", "dot", "·"));
  item.append(el("span", "sym", label));
  if (lastText) item.append(el("span", "px", lastText));
  if (move) item.append(el("span", `chg ${move.cls}`, move.text));
  if (tag) item.append(el("span", "tag", tag));
  line.append(item);
}

function renderHeader(data) {
  const header = document.getElementById("top");
  header.replaceChildren();
  const brand = el("h1", "brand", "MARKETS");
  brand.append(el("span", null, "EOD DESK"));
  const asof = el("div", "asof");
  asof.append(el("div", "date", fmtDate(data.asOf)));
  asof.append(el("div", "sub", `US CASH CLOSE · UPDATED ${fmtStamp(data.generatedAt)}`));
  header.append(brand, asof);
  document.title = `MARKETS · ${fmtDate(data.asOf)}`;
}

function renderStrip(data) {
  const strip = document.getElementById("strip");
  const { map, prices } = indexRows(data);
  strip.hidden = false;
  strip.replaceChildren();

  const tape = el("div", "line");
  tape.append(el("span", "k", "TAPE"));
  for (const id of TAPE) {
    const row = map.get(id);
    if (!row) continue;
    const last = row.kind === "yield"
      ? `${fmtLast(row.last, row.decimals)}%`
      : fmtLast(row.last, row.decimals);
    const move = fmtMove(row.chg && row.chg.d1, row.kind);
    const tag = id === "VIX" ? vixTag(row.last) : "";
    addItem(tape, row.id, last, move, tag);
  }
  const y2 = map.get("US2Y");
  const y10 = map.get("US10Y");
  if (y2 && y10 && y2.last != null && y10.last != null) {
    addItem(tape, "2s10s", null, fmtMove((y10.last - y2.last) * 100, "yield"), "");
  }
  const spy = map.get("SPY");
  const rsp = map.get("RSP");
  if (spy && rsp && spy.chg && rsp.chg && spy.chg.d1 != null && rsp.chg.d1 != null) {
    addItem(tape, "RSP-SPY", null, fmtPp(rsp.chg.d1 - spy.chg.d1), "");
  }
  strip.append(tape);

  const ranked = prices
    .filter((row) => row.chg && row.chg.d1 != null)
    .sort((a, b) => b.chg.d1 - a.chg.d1);
  if (ranked.length) {
    const moves = el("div", "line");
    moves.append(el("span", "k", "1D"));
    const upLabel = el("span", "k up", "UP");
    moves.append(upLabel);
    for (const row of ranked.slice(0, 3)) {
      addItem(moves, row.id, null, fmtMove(row.chg.d1, "price"), "");
    }
    moves.append(el("span", "k down", "DN"));
    for (const row of ranked.slice(-3).reverse()) {
      addItem(moves, row.id, null, fmtMove(row.chg.d1, "price"), "");
    }
    strip.append(moves);
  }
}

function rangeCell(row) {
  const td = el("td");
  const wrap = el("span", "rng");
  const track = el("span", "track");
  const mark = document.createElement("i");
  let pos = 50;
  if (row.low52 != null && row.high52 != null && row.high52 > row.low52 && row.last != null) {
    pos = ((row.last - row.low52) / (row.high52 - row.low52)) * 100;
    pos = Math.max(0, Math.min(100, pos));
  }
  mark.style.left = `${pos}%`;
  track.append(mark);
  wrap.append(track, el("span", "pos", String(Math.round(pos))));
  const low = fmtLast(row.low52, row.decimals);
  const high = fmtLast(row.high52, row.decimals);
  wrap.title = `52w close range ${low} – ${high} · ${Math.round(pos)}%`;
  td.append(wrap);
  return td;
}

function sparkCell(values) {
  const td = el("td");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 84 18");
  svg.setAttribute("class", "spark");
  svg.setAttribute("aria-hidden", "true");
  if (values && values.length >= 2) {
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const up = values[values.length - 1] >= values[0];
    svg.classList.add(up ? "up" : "down");
    const points = values.map((value, i) => {
      const x = (i / (values.length - 1)) * 84;
      const y = 16.5 - ((value - min) / span) * 15;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", points);
    svg.append(line);
  }
  td.append(svg);
  return td;
}

function renderTable(group, asOf) {
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["NAME", "LAST", "1D", "1W", "1M", "YTD", "52W", "3M"]) {
    hr.append(el("th", null, label));
  }
  head.append(hr);
  const body = document.createElement("tbody");
  for (const row of group.rows) {
    const tr = document.createElement("tr");
    const name = el("td", "name");
    name.append(el("span", "sym", row.id));
    name.append(el("span", "lbl", row.name));
    if (row.lastDate && row.lastDate !== asOf) {
      const stale = el("span", "stale", fmtDate(row.lastDate).slice(0, 6));
      stale.title = `Last print ${fmtDate(row.lastDate)}`;
      name.append(stale);
    }
    name.title = [row.name, row.symbol, row.source, row.lastDate].filter(Boolean).join(" · ");
    tr.append(name);

    const last = el("td", null, row.kind === "yield"
      ? `${fmtLast(row.last, row.decimals)}%`
      : fmtLast(row.last, row.decimals));
    tr.append(last);

    for (const key of ["d1", "w1", "m1", "ytd"]) {
      const move = fmtMove(row.chg ? row.chg[key] : null, row.kind);
      tr.append(el("td", move.cls, move.text));
    }
    tr.append(rangeCell(row), sparkCell(row.spark));
    body.append(tr);
  }
  table.append(head, body);
  return table;
}

function renderGrid(data) {
  const grid = document.getElementById("grid");
  grid.replaceChildren();
  const columns = [el("div", "col"), el("div", "col")];
  for (const group of data.groups || []) {
    const panel = el("section", "panel");
    const title = el("h2", null, group.label);
    if (group.note) title.append(el("span", "note", group.note));
    const scroller = el("div", "scroller");
    scroller.append(renderTable(group, data.asOf));
    panel.append(title, scroller);
    const index = Math.max(0, Math.min(1, (group.column || 1) - 1));
    columns[index].append(panel);
  }
  grid.append(...columns.filter((col) => col.childElementCount));
}

function renderFooter(data) {
  const foot = document.getElementById("foot");
  foot.replaceChildren();
  if (data.failures && data.failures.length) {
    const miss = el("div", "miss");
    const bits = data.failures.map((item) => `${item.id} (${item.error})`);
    miss.textContent = `MISSING · ${bits.join(" · ")}`;
    foot.append(miss);
  }
  foot.append(el(
    "div",
    null,
    "EOD aligned to the US cash session · Yahoo Finance chart · US Treasury par curve · currency-api USD/CNH · FRED / CoinGecko fallback · ETF total return · front-month futures carry roll gaps · not a live feed",
  ));
}

function renderStatus(message) {
  const grid = document.getElementById("grid");
  grid.replaceChildren(el("div", null, message));
  document.getElementById("status") || grid.firstChild.setAttribute("id", "status");
}

async function main() {
  renderStatus("LOADING");
  try {
    const response = await fetch("data/markets.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data.groups || !data.groups.length) throw new Error("snapshot has no rows");
    renderHeader(data);
    renderStrip(data);
    renderGrid(data);
    renderFooter(data);
  } catch (err) {
    document.getElementById("top").replaceChildren(el("h1", "brand", "MARKETS"));
    renderStatus(`NO SNAPSHOT · ${err.message}`);
  }
}

main();
