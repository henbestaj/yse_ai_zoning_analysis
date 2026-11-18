"use strict";
/* Zoning Results Viewer: plain JavaScript, no build step and no libraries (see README.md).

   It reads the JSON that `zoning_db.py export --format json` writes: published runs from data/ (listed in
   data/index.json, which build_index.py makes), or a file the reader opens from their own computer, which
   never leaves their browser. Nothing is saved anywhere; the page is a view of those files.

   The address bar holds what's on screen (#run=...&view=...&zone=...&field=...), so any view can be
   bookmarked or sent to a teammate. Everything else (search, filters, sort) only lasts until a reload. */

// ------------------------------------------------------------------ building HTML (always escaped)

function esc(value) {
  return String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// html`...` escapes every value put into it, except HTML made by html`...` itself (or lists of it), so text
// from the documents can never turn into markup.
class Safe {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => { out += piece(value) + strings[i + 1]; });
  return new Safe(out);
}

function piece(value) {
  if (value === null || value === undefined || value === false) return "";
  if (Array.isArray(value)) return value.map(piece).join("");
  if (value instanceof Safe) return value.text;
  return esc(value);
}

const $ = (selector, root = document) => root.querySelector(selector);

// ------------------------------------------------------------------ wording and formatting

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const UNIT_LABELS = {
  sq_ft: "sq ft", acres: "acres", sq_m: "sq m", ft: "ft", m: "m", stories: "stories",
  units_per_acre: "units per acre", sq_ft_per_unit: "sq ft per unit", acres_per_unit: "acres per unit", count: "",
};

function quantity(value, unit) {
  if (unit === "percent") return `${number.format(value)}%`;
  const label = unit in UNIT_LABELS ? UNIT_LABELS[unit] : String(unit).replace(/_/g, " ");
  return label ? `${number.format(value)} ${label}` : number.format(value);
}

const capitalize = text => text ? text[0].toUpperCase() + text.slice(1) : text;
const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

const WORDS = { min: "Minimum", max: "Maximum" };
function fieldLabel(name) {
  const words = name.split("_");
  words[0] = WORDS[words[0]] || capitalize(words[0]);
  return words.join(" ");
}
const shortLabel = name => fieldLabel(name).replace(/^Minimum /, "Min. ").replace(/^Maximum /, "Max. ");

function placeName(run) {
  return run.place_kind === "town" ? run.place.split("_").map(capitalize).join(" ") : run.place;
}

const day = iso => new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const moment = iso => iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
const pagesText = pages => pages.length ? `${pages.length === 1 ? "p." : "pp."} ${pages.join(", ")}` : "";

const ZONES_FROM = {
  scraped: "taken from the town's parcel data",
  given: "a list given to the model",
  model: "found in the document by the model",
};

// What a field's result looks like at a glance. Each state has its own mark shape, not just a color.
const STATES = {
  ok: { label: "Has a value", count: "with a value", help: "A value that applies generally, with no flags" },
  check: { label: "Take a closer look", count: "to look at closely", help: "Only conditional values, several values, or a flag to read" },
  na: { label: "Doesn't apply", count: "not applicable", help: "The document says this requirement doesn't apply" },
  missing: { label: "Not found", count: "not found", help: "The model found nothing about this in the document" },
};
const STATE_ORDER = ["ok", "check", "na", "missing"];

// Flags come from zoning_db.py's summarize_field; the common ones get plainer wording
const FLAG_TEXT = {
  "only conditional values": "No general value: every value applies only in certain cases.",
  "stated, but not as a number": "Stated in words, not as a number: read the text.",
  "a value couldn't be converted to a standard unit": "A value couldn't be converted to a standard unit.",
};
const flagText = flag => FLAG_TEXT[flag] || capitalize(flag) + ".";

// ------------------------------------------------------------------ preparing a run

function classify(field, result) {
  if (result.status === "not_applicable") return "na";
  if (result.status !== "stated") return "missing";
  const general = field.kind === "text" ? Boolean(result.general)
    : Object.values(result.general || {}).some(v => v !== null);
  return general && !result.flags.length ? "ok" : "check";
}

function prepare(data, source) {
  if (!data || data.format !== "zoning-run") {
    throw new Error(`${source} isn't a zoning results file. Make one with: zoning_db.py export --format json`);
  }
  if (data.version !== 1) {
    throw new Error(`${source} uses format version ${data.version}, which this viewer doesn't know yet.`);
  }
  const fields = data.fields;
  const empty = field => ({ status: "not_found", general: field.kind === "text" ? "" : {}, flags: [], values: [] });
  const zones = data.zones.map(z => {
    const info = {};
    for (const field of fields) {
      const result = z.fields[field.name] || empty(field);
      // General values first, then conditional ones, each in the model's order
      const values = [...result.values.filter(v => !v.condition), ...result.values.filter(v => v.condition)];
      info[field.name] = { ...result, values, state: classify(field, result) };
    }
    const name = (info.district_name && info.district_name.general) || "";
    return {
      zone: z.zone, found: z.found_in_document, notes: z.notes || "", info, name,
      // The name to show next to the code; some towns' codes are already the full name
      subName: name.toLowerCase() === z.zone.toLowerCase() ? "" : name,
      type: (info.district_type && info.district_type.general) || "",
    };
  });
  zones.sort((a, b) => a.zone.localeCompare(b.zone, undefined, { numeric: true, sensitivity: "base" }));
  return {
    run: data.run, document: data.document, fields, zones, place: placeName(data.run),
    // Measurements first, then the descriptive text fields (whose lists can be long)
    order: [...fields.filter(f => f.kind !== "text"), ...fields.filter(f => f.kind === "text")],
    docUrl: null, docNote: null,
  };
}

// A short version of a field's result, for the table and the comparison: { text, more }, where `more`
// counts the conditional values that the text leaves out
function summarize(field, result) {
  if (result.state === "missing") return { text: "Not found", more: 0 };
  if (result.state === "na") return { text: "Doesn't apply", more: 0 };
  const values = result.values;
  const conditional = values.filter(v => v.condition).length;
  if (field.kind === "text") {
    if (values.length > 3) return { text: `${values.length} listed`, more: 0 };
    if (result.general) return { text: result.general, more: conditional };
    return { text: `${values[0].text} (conditional)`, more: values.length - 1 };
  }
  const general = Object.entries(result.general || {}).filter(([, v]) => v !== null).map(([u, v]) => quantity(v, u));
  if (general.length) return { text: general.join(" · "), more: conditional };
  // No single general number: several of them, one that couldn't be converted, or a rule in words
  const generalValues = values.filter(v => !v.condition);
  const numbers = generalValues.filter(v => !("text" in v));
  if (numbers.length > 1) return { text: "Several general values", more: conditional };
  if (numbers.length === 1) return { text: valueText(numbers[0]).main, more: conditional };
  const words = generalValues.find(v => "text" in v);
  if (words) return { text: words.text, more: conditional };
  return { text: `Only ${plural(conditional, "conditional value")}`, more: 0 };
}

// ------------------------------------------------------------------ state and loading

const VIEWS = ["districts", "compare", "table"];
const state = {
  index: { runs: [] },
  indexProblem: null,
  cache: new Map(),    // published file name -> prepared run
  local: null,         // a run opened from the reader's computer
  current: null,       // the run on screen
  route: null,
  lastHash: null,
  lastRoute: null,
  query: "",
  onlyCheck: false,
  quotes: false,
  sort: "zone",
  units: {},           // field name -> unit chosen in the comparison
};

function readRoute() {
  const params = new URLSearchParams(location.hash.slice(1));
  const view = params.get("view");
  return { run: params.get("run"), view: VIEWS.includes(view) ? view : "districts",
           zone: params.get("zone"), field: params.get("field") };
}

function link(route) {
  const params = new URLSearchParams();
  for (const key of ["run", "view", "zone", "field"]) if (route[key]) params.set(key, route[key]);
  return "#" + params.toString();
}

async function loadIndex() {
  try {
    const response = await fetch("data/index.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(response.status);
    state.index = await response.json();
  } catch {
    state.index = { runs: [] };
    state.indexProblem = location.protocol === "file:" ? "file" : "missing";
  }
}

async function getRun(key) {
  if (key === "local") return state.local;
  if (state.cache.has(key)) return state.cache.get(key);
  const entry = state.index.runs.find(r => r.file === key);
  if (!entry) throw new Error("That run isn't published (any more). Pick a place from the list.");
  const response = await fetch("data/" + encodeURIComponent(key));
  if (!response.ok) throw new Error(`Couldn't load ${key} (HTTP ${response.status}).`);
  const run = prepare(await response.json(), key);
  run.key = key;
  if (run.document.file && entry.document) run.docUrl = "data/" + encodeURIComponent(run.document.file);
  else if (run.document.file) run.docNote = "The zoning document wasn't published with this run, so page numbers aren't links.";
  state.cache.set(key, run);
  return run;
}

// A run opened from the reader's computer: the .json export, plus its PDF if they have it. The PDF is
// checked against the fingerprint stored with the run, so page links can't point into the wrong document.
async function openFiles(fileList) {
  const files = [...fileList];
  const json = files.find(f => /\.json$/i.test(f.name));
  const pdfs = files.filter(f => /\.pdf$/i.test(f.name));
  try {
    if (!json) {
      if (pdfs.length && state.local) {
        await attachDocument(state.local, pdfs);
        render();
        return;
      }
      throw new Error("Choose the results file (.json), plus its PDF if you have it.");
    }
    let data;
    try { data = JSON.parse(await json.text()); } catch { throw new Error(`${json.name} isn't valid JSON.`); }
    const run = prepare(data, json.name);
    run.key = "local";
    run.fileName = json.name;
    if (state.local && state.local.docUrl) URL.revokeObjectURL(state.local.docUrl);
    state.local = run;
    if (pdfs.length) await attachDocument(run, pdfs);
    else if (run.document.file) run.docNote = `To link page numbers, open ${run.document.file} (or the original ${run.document.name}) too.`;
    showProblem(null);
    if (location.hash === link({ run: "local" })) render();
    else location.hash = link({ run: "local" });
  } catch (err) {
    showProblem(err.message);
  }
}

async function attachDocument(run, pdfs) {
  // The copy the export made, or the original, by name first
  pdfs.sort((a, b) => (b.name === run.document.file || b.name === run.document.name)
                      - (a.name === run.document.file || a.name === run.document.name));
  for (const pdf of pdfs) {
    const hash = await sha256(pdf);
    if (hash === null || hash === run.document.sha256) {
      if (run.docUrl) URL.revokeObjectURL(run.docUrl);
      run.docUrl = URL.createObjectURL(pdf);
      run.docNote = hash === null ? `Couldn't check that ${pdf.name} is the document this run used.` : null;
      return;
    }
  }
  run.docNote = `${plural(pdfs.length, "PDF")} opened, but none is the document this run used (${run.document.name}), ` +
                "so page numbers aren't links.";
}

async function sha256(file) {
  if (!window.crypto || !crypto.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// ------------------------------------------------------------------ shared pieces

function mark(stateName, title) {
  return html`<i class="mark s-${stateName}"${title ? html` title="${title}"` : ""} aria-hidden="true"></i>`;
}

function legend() {
  return html`<div class="legend" aria-label="Legend">${STATE_ORDER.map(s =>
    html`<span title="${STATES[s].help}">${mark(s)}${STATES[s].label}</span>`)}</div>`;
}

function strip(run, zone) {
  const label = run.order.map(f => `${fieldLabel(f.name)}: ${STATES[zone.info[f.name].state].label}`).join("\n");
  return html`<span class="strip" title="${label}" aria-hidden="true">${run.order.map(f => mark(zone.info[f.name].state))}</span>`;
}

function stack(counts, total) {
  return html`<span class="stack" aria-hidden="true">${STATE_ORDER.map(s => counts[s]
    ? html`<i class="s-${s}" style="flex: ${counts[s]}" title="${counts[s]} of ${total}: ${STATES[s].label}"></i>` : "")}</span>`;
}

function countStates(run, field) {
  const counts = { ok: 0, check: 0, na: 0, missing: 0 };
  for (const zone of run.zones) counts[zone.info[field.name].state]++;
  return counts;
}

function pageLinks(run, pages) {
  if (!pages.length) return html`<span class="muted">No page given</span>`;
  if (!run.docUrl) return html`<span class="muted">${pagesText(pages)}</span>`;
  return pages.map(p => html`<a class="page-link" href="${run.docUrl}#page=${p}" target="_blank" rel="noopener">Open page ${p}</a>`);
}

function valueText(v) {
  if ("text" in v) return { main: v.text, aside: "" };
  if (v.value !== null && v.value !== undefined) {
    const same = v.reported_unit === v.unit;
    return { main: quantity(v.value, v.unit), aside: same ? "" : `the document says ${quantity(v.reported_value, v.reported_unit)}` };
  }
  return { main: quantity(v.reported_value, v.reported_unit), aside: "couldn't be converted to a standard unit" };
}

function renderValue(run, v, extra) {
  const { main, aside } = valueText(v);
  return html`
    <li class="value ${v.condition ? "conditional" : "general"}${extra ? " extra" : ""}">
      <button type="button" class="value-line" data-action="toggle-value" aria-expanded="false"
              title="Show the quote and page">
        <span class="value-text">
          <span class="value-main">${main}</span>
          ${aside ? html`<span class="value-aside">(${aside})</span>` : ""}
          ${v.condition ? html`<span class="value-cond">${v.condition}</span>` : ""}
        </span>
        <span class="value-pages">${pagesText(v.pages)}</span>
      </button>
      <div class="source">
        <blockquote>${v.quote}</blockquote>
        <div class="source-actions">
          ${pageLinks(run, v.pages)}
          <button type="button" class="link" data-action="copy" data-text="${v.quote}"
                  title="Copy the quote, to find it in the document with Ctrl+F">Copy quote</button>
        </div>
      </div>
    </li>`;
}

const LIST_LIMIT = 8;
function renderValues(run, result) {
  const values = result.values;
  const long = values.length > LIST_LIMIT + 2;
  return html`
    <ul class="values${long ? " collapsed" : ""}">${values.map((v, i) => renderValue(run, v, long && i >= LIST_LIMIT))}</ul>
    ${long ? html`<button type="button" class="link more" data-action="toggle-list" data-more="Show all ${values.length}"
                          data-less="Show fewer">Show all ${values.length}</button>` : ""}`;
}

function renderFlags(flags) {
  if (!flags.length) return "";
  return html`<ul class="flags">${flags.map(f => html`<li><span class="flag-icon" aria-hidden="true">!</span>${flagText(f)}</li>`)}</ul>`;
}

function renderResult(run, field, result) {
  if (result.state === "missing") return html`<p class="pill s-missing">${mark("missing")}Not found in the document</p>`;
  if (result.state === "na") return html`<p class="pill s-na">${mark("na")}Doesn't apply to this district</p>`;
  return html`${renderValues(run, result)}${renderFlags(result.flags)}`;
}

function searchBox(placeholder) {
  return html`<input class="search" type="search" data-role="search" placeholder="${placeholder}"
                     value="${state.query}" aria-label="${placeholder}" title="Press / to search">`;
}

const matchesQuery = zone => {
  const q = state.query.trim().toLowerCase();
  return !q || zone.zone.toLowerCase().includes(q) || zone.name.toLowerCase().includes(q);
};

const banner = (kind, content) => html`<div class="banner ${kind}">${content}</div>`;

// ------------------------------------------------------------------ home page

function dropZone() {
  return html`
    <div class="dropzone">
      <p><strong>Drop a results file here</strong>: the <code>.json</code> export, plus its PDF if you have it.</p>
      <button type="button" class="button" data-action="choose-files">Choose files…</button>
      <p class="hint">Files you open stay on your computer. To make one, export a run with
        <code>zoning_db.py export --run N --format json</code> or the app's <em>Download JSON</em> button.</p>
    </div>`;
}

function renderHome() {
  const groups = new Map();
  for (const entry of state.index.runs) {
    const name = placeName(entry);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(entry);
  }
  const places = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  for (const [, runs] of places) runs.sort((a, b) => b.started_at.localeCompare(a.started_at));

  let listing;
  if (places.length) {
    listing = html`<div class="places">${places.map(([name, runs]) => {
      const newest = runs[0];
      return html`
        <a class="place-card" href="${link({ run: newest.file })}">
          <span class="place-name">${name}</span>
          <span class="place-meta">${plural(newest.zones, "district")} · read ${day(newest.started_at)}</span>
          <span class="place-meta">${newest.model}, ${newest.effort} effort</span>
          <span class="place-tags">
            ${newest.status !== "succeeded" ? html`<span class="tag warn">${capitalize(newest.status)}: ${newest.zones} of ${newest.zones_requested}</span>` : ""}
            ${newest.document ? "" : html`<span class="tag">No document</span>`}
            ${runs.length > 1 ? html`<span class="tag">${runs.length} runs</span>` : ""}
          </span>
        </a>`;
    })}</div>`;
  } else if (state.indexProblem === "file") {
    listing = banner("info", "This page was opened as a file, so it can't list published results. Open a results file below.");
  } else {
    listing = html`<p class="muted">Nothing has been published yet. You can still open a results file below.</p>`;
  }

  return html`
    <section class="home">
      <div class="hero">
        <h1>Zoning results</h1>
        <p>Each place's zoning districts and their dimensional rules, as a language model read them from the
          zoning regulations. Every value comes with the document's exact wording and page, so you can check it.</p>
      </div>
      <h2>Places</h2>
      ${listing}
      <h2>Open a file from your computer</h2>
      ${dropZone()}
      <details class="howto">
        <summary>How to read the results</summary>
        <ul>
          <li><strong>Districts</strong> shows one district at a time. Click any value to see the quote it came from,
            with links to that page of the document.</li>
          <li><strong>Compare a field</strong> lines up one requirement, like minimum lot area, across every district,
            which makes odd values stand out.</li>
          <li><strong>Table</strong> is the overview: every district and field at once. Click a cell to see its details.</li>
          <li>The marks show what was found: ${legend()}</li>
          <li>The address bar always points at what you're looking at, so you can copy it to show a teammate.</li>
        </ul>
      </details>
    </section>`;
}

// ------------------------------------------------------------------ run header

function siblings(run) {
  if (run.key === "local") return [];
  return state.index.runs.filter(e => placeName(e) === run.place).sort((a, b) => b.started_at.localeCompare(a.started_at));
}

function runOption(entry) {
  const status = entry.status === "succeeded" ? "" : ` · ${entry.status} (${entry.zones} of ${entry.zones_requested})`;
  return `${day(entry.started_at)} · ${entry.model}, ${entry.effort} · ${plural(entry.zones, "district")}${status}`;
}

function renderRunHead(run, route) {
  const r = run.run;
  const others = siblings(run);
  const missing = r.zones_requested - run.zones.length;
  const tabs = [["districts", "Districts"], ["compare", "Compare a field"], ["table", "Table"]];
  return html`
    <section class="run-head">
      <nav class="crumbs"><a href="#">All places</a> <span aria-hidden="true">›</span></nav>
      <div class="run-title">
        <h1>${run.place}</h1>
        ${others.length > 1 ? html`
          <label class="run-pick"><span class="sr-only">Run</span>
            <select data-action="pick-run">${others.map(e =>
              html`<option value="${e.file}"${e.file === run.key ? " selected" : ""}>${runOption(e)}</option>`)}</select>
          </label>` : ""}
        <span class="spacer"></span>
        ${run.docUrl
          ? html`<a class="button" href="${run.docUrl}" target="_blank" rel="noopener">Open the zoning document</a>`
          : html`<span class="muted small">No document attached</span>`}
      </div>
      <p class="run-meta">
        ${plural(run.zones.length, "district")} · read by ${r.model} (${r.effort} effort) on ${day(r.started_at)} ·
        district list ${ZONES_FROM[r.zones_from] || r.zones_from}
      </p>
      ${run.key === "local" ? banner("info", html`Opened from your computer: <strong>${run.fileName}</strong>. It isn't
        published, so links to it only work for you, until you reload the page.`) : ""}
      ${r.status !== "succeeded" ? banner("warn", html`<strong>This run is ${r.status}.</strong>
        ${missing > 0 ? `${plural(missing, "district")} of ${r.zones_requested} ${missing === 1 ? "is" : "are"} missing.` : ""}
        ${r.error ? html`<span class="muted">(${r.error})</span>` : ""}`) : ""}
      ${run.docNote ? banner("info", run.docNote) : ""}
      ${renderAbout(run)}
      <div class="tabs-row">
        <nav class="tabs">${tabs.map(([view, label]) => html`
          <a href="${link({ run: route.run, view, zone: route.zone, field: route.field })}"
             ${route.view === view ? html`aria-current="page"` : ""}>${label}</a>`)}</nav>
        <label class="switch" title="Show every value's quote without clicking">
          <input type="checkbox" data-action="toggle-quotes"${state.quotes ? " checked" : ""}> Show all quotes
        </label>
      </div>
    </section>`;
}

function renderAbout(run) {
  const r = run.run;
  const rows = [
    ["Status", `${capitalize(r.status)}: ${r.zones_requested - (r.zones_failed || 0)} of ${r.zones_requested} districts`],
    ["District list", capitalize(ZONES_FROM[r.zones_from] || r.zones_from)],
    ["Model", `${r.model} on ${r.provider}, ${r.effort} effort`],
    ["Started", moment(r.started_at)],
    ["Finished", moment(r.finished_at)],
    ["Document", run.document.name],
    ["Fingerprint", run.document.sha256.slice(0, 16) + "…"],
    ["Run", `#${r.id}`],
  ];
  return html`
    <details class="about">
      <summary>About this run and what the model was asked</summary>
      <div class="about-body">
        <dl>${rows.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>
        <div>
          <h3>Fields the model was asked for</h3>
          <dl class="asked">${run.fields.map(f => html`<dt>${fieldLabel(f.name)}</dt><dd>${f.description}</dd>`)}</dl>
        </div>
      </div>
    </details>`;
}

// ------------------------------------------------------------------ districts view

function renderZoneItems(run, current) {
  const zones = run.zones.filter(matchesQuery);
  if (!zones.length) return html`<li class="empty">No districts match “${state.query}”.</li>`;
  return zones.map(z => html`
    <li><a class="zone-item${z === current ? " active" : ""}" href="${link({ run: run.key, zone: z.zone })}"
           ${z === current ? html`aria-current="true"` : ""}>
      <span class="zone-item-top"><b>${z.zone}</b>${z.found ? "" : html`<span class="tag warn">Not in document</span>`}</span>
      ${z.subName ? html`<span class="zone-item-name">${z.subName}</span>` : ""}
      ${strip(run, z)}
    </a></li>`);
}

function renderDistricts(run, route) {
  const zone = run.zones.find(z => z.zone === route.zone) || run.zones[0];
  if (!zone) return banner("warn", "This run has no districts.");
  return html`
    <div class="layout">
      <aside class="sidebar">
        ${searchBox("Search districts")}
        <ul class="list" id="zone-list">${renderZoneItems(run, zone)}</ul>
        ${legend()}
      </aside>
      <section class="detail">${renderZone(run, zone)}</section>
    </div>`;
}

function renderZone(run, zone) {
  const i = run.zones.indexOf(zone);
  const prev = run.zones[i - 1], next = run.zones[i + 1];
  const toCheck = run.order.filter(f => zone.info[f.name].state !== "ok");
  const shown = state.onlyCheck ? toCheck : run.order;
  const measures = shown.filter(f => f.kind !== "text"), texts = shown.filter(f => f.kind === "text");
  const longNotes = zone.notes.length > 280;
  return html`
    <article class="panel zone">
      <header class="zone-head">
        <div>
          ${zone.subName ? html`<p class="zone-code">${zone.zone}</p>` : ""}
          <h2>${zone.name || zone.zone}</h2>
          ${zone.type ? html`<span class="tag">${zone.type}</span>` : ""}
        </div>
        <div class="zone-tools">
          ${navButton(run, prev, "prev", "← Previous", "Previous district (← key)")}
          <span class="muted small">${i + 1} of ${run.zones.length}</span>
          ${navButton(run, next, "next", "Next →", "Next district (→ key)")}
          ${copyLinkButton({ run: run.key, zone: zone.zone }, "Copy link")}
        </div>
      </header>
      ${zone.found ? "" : html`<div class="pad">${banner("warn",
        "The model says the document doesn't define this district, so treat anything below with suspicion.")}</div>`}
      ${zone.notes ? html`
        <section class="notes${longNotes ? " clamped" : ""}">
          <h3>The model's notes</h3>
          <p>${zone.notes}</p>
          ${longNotes ? html`<button type="button" class="link" data-action="toggle-notes">Read all</button>` : ""}
        </section>` : ""}
      <div class="zone-toolbar">
        <div class="segmented" role="group" aria-label="Which fields to show">
          <button type="button" data-action="only-check" data-value="0" aria-pressed="${!state.onlyCheck}">All fields (${run.order.length})</button>
          <button type="button" data-action="only-check" data-value="1" aria-pressed="${state.onlyCheck}">To check (${toCheck.length})</button>
        </div>
        <span class="muted small">Click a value to see its quote and page.</span>
      </div>
      ${measures.length ? html`<h3 class="group-title">Dimensional requirements</h3>${measures.map(f => renderField(run, zone, f))}` : ""}
      ${texts.length ? html`<h3 class="group-title">Description and uses</h3>${texts.map(f => renderField(run, zone, f))}` : ""}
      ${shown.length ? "" : html`<p class="empty pad">Nothing to check: every field has a general value and no flags.</p>`}
    </article>`;
}

function navButton(run, zone, direction, label, title) {
  if (!zone) return html`<span class="button ghost disabled" aria-disabled="true">${label}</span>`;
  return html`<a class="button ghost" data-nav="${direction}" href="${link({ run: run.key, zone: zone.zone })}" title="${title}">${label}</a>`;
}

function copyLinkButton(route, label) {
  if (route.run === "local") return "";
  return html`<button type="button" class="link" data-action="copy-link" data-link="${link(route)}"
                      title="Copy a link to this, to send to a teammate">${label}</button>`;
}

function renderField(run, zone, field) {
  const result = zone.info[field.name];
  const label = fieldLabel(field.name);
  const describe = field.description.trim().toLowerCase() !== label.toLowerCase();
  return html`
    <div class="field s-${result.state}" id="field-${field.name}">
      <div class="field-label">
        <h4>${mark(result.state, STATES[result.state].label)}${label}</h4>
        ${describe ? html`<p class="field-desc" title="${field.description}">${field.description}</p>` : ""}
        <p class="field-links">
          <a href="${link({ run: run.key, view: "compare", field: field.name, zone: zone.zone })}">Compare across districts</a>
          ${copyLinkButton({ run: run.key, zone: zone.zone, field: field.name }, "Copy link")}
        </p>
      </div>
      <div class="field-body">${renderResult(run, field, result)}</div>
    </div>`;
}

// ------------------------------------------------------------------ compare view

function renderCompare(run, route) {
  const field = run.fields.find(f => f.name === route.field) || run.order[0];
  const i = run.order.indexOf(field);
  const counts = countStates(run, field);
  const numeric = field.kind !== "text";
  const unit = field.units.includes(state.units[field.name]) ? state.units[field.name] : field.units[0];
  const zones = compareOrder(run, field, unit, numeric);
  const amount = z => (z.info[field.name].general || {})[unit];
  const max = numeric ? Math.max(0, ...zones.map(z => amount(z) ?? 0)) : 0;
  const fieldLink = f => link({ run: run.key, view: "compare", field: f.name, zone: route.zone });

  return html`
    <div class="layout">
      <aside class="sidebar">
        <p class="sidebar-title">Fields</p>
        <ul class="list">${run.order.map(f => html`
          <li><a class="field-item${f === field ? " active" : ""}" href="${fieldLink(f)}">
            <span>${fieldLabel(f.name)}</span>${stack(countStates(run, f), run.zones.length)}
          </a></li>`)}</ul>
        ${legend()}
      </aside>
      <section class="detail">
        <article class="panel">
          <header class="zone-head">
            <div>
              <p class="zone-code">${field.kind === "text" ? "text" : field.units.map(u => UNIT_LABELS[u] ?? u).filter(Boolean).join(" / ") || field.kind}</p>
              <h2>${fieldLabel(field.name)}</h2>
              <p class="field-desc">What the model was asked for: “${field.description}”</p>
            </div>
            <div class="zone-tools">
              ${run.order[i - 1] ? html`<a class="button ghost" data-nav="prev" href="${fieldLink(run.order[i - 1])}" title="Previous field (← key)">←</a>` : ""}
              ${run.order[i + 1] ? html`<a class="button ghost" data-nav="next" href="${fieldLink(run.order[i + 1])}" title="Next field (→ key)">→</a>` : ""}
              ${copyLinkButton({ run: run.key, view: "compare", field: field.name }, "Copy link")}
            </div>
          </header>
          <div class="zone-toolbar">
            <span class="counts">${STATE_ORDER.filter(s => counts[s]).map(s =>
              html`<span class="count">${mark(s)}${counts[s]} ${STATES[s].count}</span>`)}</span>
            ${numeric ? html`
              <span class="controls">
                ${field.units.length > 1 ? html`<span class="segmented" role="group" aria-label="Unit">${field.units.map(u =>
                  html`<button type="button" data-action="unit" data-field="${field.name}" data-unit="${u}"
                               aria-pressed="${u === unit}">${UNIT_LABELS[u] || u}</button>`)}</span>` : ""}
                <label class="small">Sort
                  <select data-action="sort">
                    ${[["zone", "by district"], ["high", "highest first"], ["low", "lowest first"]].map(([v, l]) =>
                      html`<option value="${v}"${state.sort === v ? " selected" : ""}>${l}</option>`)}
                  </select>
                </label>
              </span>` : ""}
          </div>
          <div class="cmp-rows">${zones.map(z => renderCompareRow(run, field, z, numeric, max ? (amount(z) ?? 0) / max : 0, route))}</div>
        </article>
      </section>
    </div>`;
}

function compareOrder(run, field, unit, numeric) {
  const zones = [...run.zones];
  if (!numeric || state.sort === "zone") return zones;
  const amount = z => { const v = (z.info[field.name].general || {})[unit]; return v === null || v === undefined ? null : v; };
  return zones.sort((a, b) => {
    const x = amount(a), y = amount(b);
    if (x === null || y === null) return (x === null) - (y === null);
    return state.sort === "low" ? x - y : y - x;
  });
}

function renderCompareRow(run, field, zone, numeric, share, route) {
  const result = zone.info[field.name];
  const summary = summarize(field, result);
  const open = zone.zone === route.zone;
  const extra = [
    summary.more ? `+${summary.more} conditional` : "",
    result.flags.length ? plural(result.flags.length, "flag") : "",
  ].filter(Boolean).join(" · ");
  return html`
    <div class="cmp-row s-${result.state}${open ? " open" : ""}" id="row-${zone.zone}">
      <button type="button" class="cmp-line${numeric ? "" : " no-bar"}" data-action="toggle-row" aria-expanded="${open}">
        <span class="cmp-zone">${mark(result.state, STATES[result.state].label)}<b>${zone.zone}</b><small>${zone.subName}</small></span>
        <span class="cmp-value">${summary.text}</span>
        ${numeric ? html`<span class="bar" aria-hidden="true"><i style="width: ${(share * 100).toFixed(1)}%"></i></span>` : ""}
        <span class="cmp-extra">${extra}</span>
      </button>
      <div class="cmp-detail">
        ${renderResult(run, field, result)}
        <p class="field-links"><a href="${link({ run: run.key, zone: zone.zone, field: field.name })}">Open ${zone.zone} →</a></p>
      </div>
    </div>`;
}

// ------------------------------------------------------------------ table view

function tableColumns(run) {
  return run.order.filter(f => f.name !== "district_name");  // already shown next to each code
}

function renderTable(run) {
  const columns = tableColumns(run);
  return html`
    <div class="table-tools">
      ${searchBox("Search districts")}
      <span class="muted small">Click a cell for its details, or a column name to compare it.</span>
      ${legend()}
    </div>
    <div class="matrix-wrap">
      <table class="matrix">
        <thead><tr>
          <th class="corner" scope="col">District</th>
          ${columns.map(f => html`<th scope="col"><a href="${link({ run: run.key, view: "compare", field: f.name })}"
                                                     title="${fieldLabel(f.name)}: ${f.description}">${shortLabel(f.name)}</a></th>`)}
        </tr></thead>
        <tbody id="matrix-body">${renderTableRows(run)}</tbody>
      </table>
    </div>`;
}

function renderTableRows(run) {
  const columns = tableColumns(run);
  const zones = run.zones.filter(matchesQuery);
  if (!zones.length) return html`<tr><td class="empty" colspan="${columns.length + 1}">No districts match “${state.query}”.</td></tr>`;
  return zones.map(z => html`
    <tr>
      <th scope="row"><a href="${link({ run: run.key, zone: z.zone })}"><b>${z.zone}</b><small>${z.subName}</small></a></th>
      ${columns.map(f => {
        const result = z.info[f.name];
        const { text, more } = summarize(f, result);
        const title = STATES[result.state].label + (more ? `; ${plural(more, "more value")} for specific cases` : "");
        return html`<td class="s-${result.state}"><a href="${link({ run: run.key, zone: z.zone, field: f.name })}"
                       title="${title}">${mark(result.state)}<span class="cell-text">${text}</span>${more
                       ? html`<span class="more-count" aria-label="${plural(more, "more value")}">+${more}</span>` : ""}</a></td>`;
      })}
    </tr>`);
}

// ------------------------------------------------------------------ drawing the page

let renderCount = 0;

async function render() {
  const mine = ++renderCount;
  const route = readRoute();
  const app = $("#app");
  state.route = route;
  if (!route.run) {
    state.current = null;
    app.innerHTML = renderHome();
    document.title = "Zoning Results Viewer";
    afterRender(route);
    return;
  }
  let run;
  try {
    if (route.run !== "local" && !state.cache.has(route.run)) app.innerHTML = html`<p class="loading">Loading…</p>`;
    run = await getRun(route.run);
  } catch (err) {
    if (mine === renderCount) app.innerHTML = problemPage(err.message);
    return;
  }
  if (mine !== renderCount) return;  // a newer navigation took over while this one was loading
  if (!run) {
    app.innerHTML = problemPage("That run was opened from a file on your computer, and reloading the page closes it. Open the file again.");
    return;
  }
  state.current = run;
  const body = route.view === "compare" ? renderCompare(run, route)
    : route.view === "table" ? renderTable(run) : renderDistricts(run, route);
  app.innerHTML = html`${renderRunHead(run, route)}${body}`;
  const subject = route.view === "compare" ? fieldLabel((run.fields.find(f => f.name === route.field) || run.order[0]).name)
    : route.view === "table" ? "Table" : (route.zone || (run.zones[0] && run.zones[0].zone) || "");
  document.title = `${subject} · ${run.place} · Zoning Results Viewer`;
  afterRender(route);
}

function problemPage(message) {
  return html`<section class="home">${banner("error", message)}<p><a href="#">← All places</a></p>${dropZone()}</section>`;
}

// Scroll only when the address changed (not when a filter or toggle redrew the page)
function afterRender(route) {
  if (location.hash === state.lastHash) return;
  const previous = state.lastRoute;
  state.lastHash = location.hash;
  state.lastRoute = route;
  const target = route.view === "districts" && route.field ? document.getElementById("field-" + route.field)
    : route.view === "compare" && route.zone ? document.getElementById("row-" + route.zone) : null;
  if (target) {
    target.scrollIntoView({ block: "center" });
    target.classList.add("flash");
    return;
  }
  if (!previous || previous.run !== route.run || previous.view !== route.view || previous.zone !== route.zone) {
    window.scrollTo(0, 0);
  }
}

function showProblem(message) {
  const box = $("#problem");
  box.hidden = !message;
  box.textContent = message || "";
}

async function copyText(text, button, done) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  const before = button.textContent;
  button.textContent = done;
  setTimeout(() => { button.textContent = before; }, 1500);
}

// ------------------------------------------------------------------ events

document.addEventListener("click", event => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  if (action === "toggle-value") {
    const open = el.closest(".value").classList.toggle("open");
    el.setAttribute("aria-expanded", open);
  } else if (action === "toggle-row") {
    const open = el.closest(".cmp-row").classList.toggle("open");
    el.setAttribute("aria-expanded", open);
  } else if (action === "toggle-list") {
    const collapsed = el.previousElementSibling.classList.toggle("collapsed");
    el.textContent = collapsed ? el.dataset.more : el.dataset.less;
  } else if (action === "toggle-notes") {
    const open = el.closest(".notes").classList.toggle("open");
    el.textContent = open ? "Show less" : "Read all";
  } else if (action === "copy") {
    copyText(el.dataset.text, el, "Copied");
  } else if (action === "copy-link") {
    copyText(location.href.split("#")[0] + el.dataset.link, el, "Link copied");
  } else if (action === "choose-files") {
    $("#file-input").click();
  } else if (action === "only-check") {
    state.onlyCheck = el.dataset.value === "1";
    render();
  } else if (action === "unit") {
    state.units[el.dataset.field] = el.dataset.unit;
    render();
  }
});

document.addEventListener("change", event => {
  const el = event.target;
  if (el.id === "file-input") {
    if (el.files.length) openFiles(el.files);
    el.value = "";
  } else if (el.dataset.action === "pick-run") {
    location.hash = link({ ...state.route, run: el.value });
  } else if (el.dataset.action === "sort") {
    state.sort = el.value;
    render();
  } else if (el.dataset.action === "toggle-quotes") {
    state.quotes = el.checked;
    document.body.classList.toggle("show-quotes", state.quotes);
  }
});

// Search redraws only the list, so the search box keeps its focus
document.addEventListener("input", event => {
  if (event.target.dataset.role !== "search" || !state.current) return;
  state.query = event.target.value;
  const list = $("#zone-list"), rows = $("#matrix-body");
  if (list) {
    const shown = state.current.zones.find(z => z.zone === state.route.zone) || state.current.zones[0];
    list.innerHTML = html`${renderZoneItems(state.current, shown)}`;
  }
  if (rows) rows.innerHTML = html`${renderTableRows(state.current)}`;
});

document.addEventListener("keydown", event => {
  if (event.target.closest("input, select, textarea") || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === "/") {
    const search = $("[data-role=search]");
    if (search) { event.preventDefault(); search.focus(); }
  } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    const target = $(event.key === "ArrowLeft" ? "[data-nav=prev]" : "[data-nav=next]");
    if (target) { event.preventDefault(); target.click(); }
  }
});

// Dropping files anywhere on the page opens them
let dragDepth = 0;
const carriesFiles = event => [...(event.dataTransfer ? event.dataTransfer.types : [])].includes("Files");
window.addEventListener("dragenter", event => {
  if (!carriesFiles(event)) return;
  dragDepth++;
  document.body.classList.add("dragging");
});
window.addEventListener("dragleave", event => {
  if (carriesFiles(event) && --dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); }
});
window.addEventListener("dragover", event => { if (carriesFiles(event)) event.preventDefault(); });
window.addEventListener("drop", event => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  openFiles(event.dataTransfer.files);
});

window.addEventListener("error", event => showProblem(`Something went wrong: ${event.message}`));
window.addEventListener("unhandledrejection", event => showProblem(`Something went wrong: ${event.reason && event.reason.message || event.reason}`));
window.addEventListener("hashchange", () => { showProblem(null); render(); });

loadIndex().then(render);
