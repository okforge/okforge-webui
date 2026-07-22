'use strict';

/* okforge web UI — one page, five stages (PLAN.md). Vanilla JS, no build. */

const $ = s => document.querySelector(s);

const state = {
  pdf: null,        // selected PDF path
  probe: null,      // last probe result
  project: null,    // selected project name (md workspace + optional KB)
  kb: null,         // the project's KB info object, when the KB exists
  endpoints: [],
  pilotJobId: null,
  watchingJobId: null,  // job the run-live panel follows
};

// ------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.method = opts.method || 'POST';
    opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers);
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

let toastTimer = null;
function toast(msg, kind = 'error') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = kind === 'error' ? '' : 'info';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 6000);
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'onclick') e.onclick = v;
    else if (k === 'text') e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

// ------------------------------------------------- header: LLM busy light

async function pollSlots() {
  const light = $('#busy-light'), text = $('#busy-text');
  try {
    const s = await api(`/api/server/slots?endpoint=${$('#endpoint').value}`);
    if (s.error) throw new Error(s.error);
    if (s.hosted) {
      light.className = 'light idle';
      text.textContent = 'hosted';
      return;
    }
    light.className = 'light ' + (s.busy > 0 ? 'busy' : 'idle');
    text.textContent = s.busy > 0 ? `${s.busy}/${s.total} slots busy` : 'idle';
  } catch (e) {
    light.className = 'light error';
    text.textContent = 'unreachable';
  }
}

// --------------------------------------------------------- stage 1: probe

async function loadInbox(selectPath) {
  const d = await api('/api/inbox');
  const sel = $('#inbox-select');
  sel.replaceChildren(el('option', {value: '', text: '— pick a PDF —'}));
  for (const p of d.pdfs) {
    const sizeMb = (p.size / 1048576).toFixed(1);
    sel.append(el('option', {value: p.path, text: `${p.name}  (${sizeMb} MB)`}));
  }
  if (selectPath) sel.value = selectPath;
  updateUploadControls();
}

function uploadWithProgress(url, formData) {
  // fetch has no upload-progress events; XHR does.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const bar = $('#upload-progress');
    xhr.open('POST', url);
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) {
        bar.classList.remove('hidden');
        bar.value = Math.round(100 * e.loaded / e.total);
      }
    };
    xhr.onload = () => {
      bar.classList.add('hidden');
      bar.value = 0;
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        let msg = `${xhr.status} ${xhr.statusText}`;
        try {
          const j = JSON.parse(xhr.responseText);
          if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
        } catch (e) { /* not JSON */ }
        reject(new Error(msg));
      }
    };
    xhr.onerror = () => {
      bar.classList.add('hidden');
      reject(new Error('upload failed (network)'));
    };
    xhr.send(formData);
  });
}

// One PDF at a time: while a file is selected ("active"), choosing and
// uploading another is ghosted — clear the selection (pick "— pick a
// PDF —" or delete the file) to upload the next one. Upload itself
// also needs a chosen file.
function updateUploadControls() {
  const active = !!$('#inbox-select').value;
  $('#upload-file').disabled = active;
  $('#upload-btn').disabled = active || !$('#upload-file').files.length;
}

// A different PDF in the dropdown invalidates the probe and everything
// staged on it — otherwise a run/pilot would silently use the OLD file.
// Called from the dropdown's onchange AND after an upload auto-selects
// the new file (programmatic .value changes fire no change event).
function pdfSelectionChanged(newValue) {
  updateUploadControls();
  const changed = !!newValue && newValue !== state.pdf;
  if (state.pdf && newValue !== state.pdf) {
    state.pdf = null;
    state.probe = null;
    $('#probe-result').classList.add('hidden');
    $('#pilot-review').classList.add('hidden');
    $('#pilot-logbox').classList.add('hidden');
    $('#pilot-status').textContent = '';
    updateStageGates();
    saveSession();
  }
  // A new document while stage 3 still holds a project is an easy trap:
  // the next run would quietly land in the old project. One blocking
  // choice — keep (adding this document to it) or clear the selection.
  // Session restore never comes through here, so reloads stay quiet.
  if (changed && state.project) warnStaleProject();
}

function warnStaleProject() {
  const keep = confirm(
    `Project "${state.project}" is still selected in stage 3.\n\n` +
    `OK — keep it: this document's run will add to "${state.project}".\n` +
    `Cancel — clear it, then pick or create a project in stage 3.`);
  if (!keep) {
    $('#kb-select').value = '';
    selectProject('');
  }
}

// ---- multi-file upload → one combined PDF ------------------------------
// One rule: pages follow natural file-name order (page2 before page10).
// The panel shows the exact plan before upload and the page mapping after.

function sortedUploadFiles() {
  return [...$('#upload-file').files].sort((a, b) =>
    a.name.localeCompare(b.name, undefined,
                         {numeric: true, sensitivity: 'base'}));
}

function defaultCombineName(files) {
  const stems = files.map(f => f.name.replace(/\.[^.]+$/, ''));
  let prefix = stems[0];
  for (const s of stems.slice(1))
    while (prefix && !s.startsWith(prefix)) prefix = prefix.slice(0, -1);
  prefix = prefix.replace(/[\s_\-0-9]+$/, '');
  return prefix || stems[0];
}

function updateCombinePanel() {
  const panel = $('#combine-panel');
  const btn = $('#upload-btn');
  const files = sortedUploadFiles();
  if (files.length < 2) {
    panel.classList.add('hidden');
    btn.textContent = 'Upload';
    return;
  }
  btn.textContent = `Upload ${files.length} files as one PDF`;
  panel.replaceChildren(
    el('div', {},
      el('b', {text: `These ${files.length} files will be combined into one PDF, in this order:`})),
    el('ol', {}, ...files.map(f => el('li', {text: f.name}))),
    el('div', {class: 'row'},
      el('label', {text: 'Combined name '},
        el('input', {type: 'text', id: 'combine-name', size: '24',
                     value: defaultCombineName(files)})),
      el('span', {class: 'muted', text: '.pdf'})),
    el('div', {class: 'muted',
      text: 'The order comes from the file names (page2 sorts before ' +
            'page10). Wrong order? Rename the files and pick them again.'}));
  panel.classList.remove('hidden');
}

async function combineUpload() {
  const files = sortedUploadFiles();
  const name = ($('#combine-name')?.value || '').trim();
  if (!name) { toast('name the combined PDF first'); return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('name', name);
  const d = await uploadWithProgress('/api/inbox/combine', fd);
  $('#upload-file').value = '';
  $('#upload-btn').textContent = 'Upload';
  // Result view: what landed where. Stays up (the probe box appears
  // below it) until the selection changes or a rescan resets stage 1.
  $('#combine-panel').replaceChildren(
    el('div', {},
      el('b', {text: `combined into ${d.name} (${d.page_count} pages)`})),
    ...d.parts.map(p => el('div', {class: 'muted',
      text: `${p.name} → page${p.pages.includes('-') ? 's' : ''} ${p.pages}`})));
  toast(`combined ${d.parts.length} files into ${d.name} (${d.page_count} pages)`, 'info');
  await loadInbox(d.path);
  pdfSelectionChanged(d.path);
  await runProbe();
}

async function uploadPdf() {
  const f = $('#upload-file').files[0];
  if (!f) { toast('choose a .pdf file first'); return; }
  const fd = new FormData();
  fd.append('file', f);
  const d = await uploadWithProgress('/api/inbox', fd);
  $('#upload-file').value = '';  // consumed — the dropdown owns it now
  await loadInbox(d.path);
  pdfSelectionChanged(d.path);
  toast(`uploaded ${d.name}`, 'info');
  await runProbe();  // auto-probe the fresh upload
}

function verdictText(p) {
  if (p.verdict === 'text')
    return `Text layer present (${p.text_pages}/${p.page_count} pages) — but old ` +
      'scans often carry poor embedded OCR and the text layer has no images. ' +
      'Default run renders pages and does our own OCR + image extraction; ' +
      'tick <i>use embedded text layer</i> in stage 4 to trust it instead.';
  if (p.verdict === 'scan')
    return 'Pure scan, no text layer — needs the OCR pipeline (stages 2–4).';
  return `Mixed: ${p.text_pages}/${p.page_count} pages have text — OCR pipeline recommended.`;
}

async function runProbe() {
  state.pdf = $('#inbox-select').value;
  if (!state.pdf) { toast('pick a PDF first'); return; }
  const p = await api('/api/probe', {json: {pdf: state.pdf}});
  state.probe = p;
  const box = $('#probe-result');
  box.classList.remove('hidden');
  const img = p.largest_image
    ? `largest embedded image ${p.largest_image.width_px}×${p.largest_image.height_px}` +
      (p.largest_image.approx_dpi ? ` (~${p.largest_image.approx_dpi} dpi)` : '')
    : 'no embedded images found';
  const lang = p.language_guess
    ? `language guess: <b>${esc(p.language_guess)}</b>`
    : 'language: unknown (no extractable text)';
  box.innerHTML = `
    <div class="row">
      <span class="badge ${p.verdict}">${p.verdict}</span>
      <b>${esc(p.name)}</b>
      <span class="muted">${p.page_count} pages, ${p.text_pages} with text · ${img}</span>
    </div>
    <p>${verdictText(p)}</p>
    <p class="muted">${lang}</p>
    ${p.sample_text ? `<pre class="sample">p.${p.sample_page}: ${esc(p.sample_text)}</pre>` : ''}
  `;
  // translate toggle default: guessed non-English text
  $('#run-translate').checked = !!(p.language_guess && p.language_guess !== 'en');
  // Embedded-text opt-IN: default is always our own OCR + image pipeline.
  // Only meaningful when a text layer exists at all.
  $('#run-textlayer').checked = false;
  $('#run-textlayer').disabled = p.verdict === 'scan';
  $('#run-from').value = 1;
  $('#run-to').value = p.page_count;
  $('#run-from').max = $('#run-to').max = p.page_count;
  updateChunkPlan();
  updateStageGates();
  saveSession();
}

// --------------------------------------------------------- stage 2: pilot

function attachEvents(jobId, {onLog, onProgress, onImage, onStatus}) {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  if (onLog) es.addEventListener('log', e => onLog(e.data));
  if (onProgress) es.addEventListener('progress', e => onProgress(JSON.parse(e.data)));
  if (onImage) es.addEventListener('image', e => onImage(JSON.parse(e.data)));
  es.addEventListener('status', e => {
    const s = JSON.parse(e.data);
    if (onStatus) onStatus(s);
    if (['done', 'failed', 'cancelled'].includes(s.status)) es.close();
  });
  es.onerror = () => es.close();
  return es;
}

// Validate a pages spec before it hits the queue — two live pilots
// failed on '1,2' (comma form) and '1-4' on a 2-page PDF (job log
// 572-573, 2026-07-18); catch both here with a usable message.
function checkPagesSpec(pages) {
  const m = /^(\d+)(?:-(\d+))?$/.exec(pages);
  if (!m) {
    toast(`pages must be one page or a range — e.g. 16 or 5-7 (got "${pages}")`);
    return false;
  }
  const a = +m[1], b = +(m[2] || m[1]);
  if (b < a) { toast(`backwards range: ${pages}`); return false; }
  const max = state.probe && state.probe.page_count;
  if (a < 1 || (max && b > max)) {
    toast(`page out of range — this document has ${max || '?'} page(s)`);
    return false;
  }
  return true;
}

async function runPilot(pagesOverride) {
  if (!state.pdf) { toast('probe a PDF first'); return; }
  const pages = pagesOverride || $('#pilot-pages').value.trim();
  if (!pages) { toast('give a pages spec, e.g. 16 or 5-7'); return; }
  if (!checkPagesSpec(pages)) return;
  const job = await api('/api/jobs', {json: {
    type: 'pilot',
    pdf: state.pdf,
    pages: pages,
    figures: $('#pilot-figures').checked,
    tables: $('#pilot-tables').checked,
    prompt_extra: $('#pilot-prompt-extra').value.trim() || null,
    endpoint: $('#endpoint').value,
  }});
  state.pilotJobId = job.id;
  $('#pilot-status').textContent = `job #${job.id} queued…`;
  $('#pilot-logbox').classList.remove('hidden');
  $('#pilot-log').textContent = '';
  $('#pilot-review').classList.remove('hidden');
  $('#pilot-review').replaceChildren(el('p', {class: 'muted', text: 'waiting for OCR…'}));
  attachEvents(job.id, {
    onLog: line => {
      const log = $('#pilot-log');
      log.textContent += line + '\n';
      log.scrollTop = log.scrollHeight;
    },
    onProgress: p => { $('#pilot-status').textContent = `job #${job.id}: OCR page ${p.page}…`; },
    onStatus: s => {
      $('#pilot-status').textContent = `job #${job.id}: ${s.status}` +
        (s.error ? ` — ${s.error}` : '');
      if (s.status === 'done') loadPilotReview(job.id, state.pdf);
      if (s.status === 'failed') toast(`pilot failed: ${s.error}`);
    },
  });
  refreshJobs();
}

async function loadPilotReview(jobId, pdf) {
  const box = $('#pilot-review');
  box.classList.remove('hidden');
  try {
    let pages = await api(`/api/jobs/${jobId}/files/pilot.pages.json`);
    if (typeof pages === 'string') pages = JSON.parse(pages);
    box.replaceChildren(...pages.map(pg => renderPilotPage(jobId, pdf, pg)));
  } catch (e) {
    box.replaceChildren(el('p', {class: 'muted', text: `no pilot output: ${e.message}`}));
  }
}

function renderPilotPage(jobId, pdf, pg) {
  const crops = el('div', {class: 'cropstrip'});
  for (const im of pg.images || []) {
    const url = `/api/jobs/${jobId}/files/${im.path}`;
    crops.append(el('figure', {},
      el('img', {src: url, onclick: () => window.open(url)}),
      el('figcaption', {text: im.path.split('/').pop()})));
  }
  const cols = el('div', {class: 'pilot-cols'},
    el('div', {class: 'pageimg'},
      el('img', {src: `/api/page-image?pdf=${encodeURIComponent(pdf)}&page=${pg.page}`,
                 alt: `page ${pg.page}`})),
    el('div', {class: 'transcript', text: pg.content || '(empty)'}));
  return el('div', {class: 'pilot-page'},
    el('div', {class: 'row'},
      el('b', {text: `Page ${pg.page}`}),
      el('button', {class: 'small', text: 're-run this page',
        onclick: () => runPilot(String(pg.page))}),
    ),
    cols, crops);
}

// -------------------------------------------------- stage 3: project setup

// A project is one name spanning both phases: its OCR'd markdown lives
// in md-out/<name>/ and its knowledge base (created at first ingest) is
// kbs/<name>/. The dropdown shows one entry per name — the union of KB
// names and md-out dirs.

async function loadKbs(selectName) {
  const [d, m] = await Promise.all([api('/api/kbs'), api('/api/md-out')]);
  state.endpoints = d.endpoints;
  const epSel = $('#endpoint');
  if (!epSel.options.length) {
    epSel.replaceChildren(...d.endpoints.map(e => el('option', {value: e, text: e})));
  }
  const projects = new Map();  // name -> {kb: info|null, md: run|null}
  for (const kb of d.kbs) projects.set(kb.name, {kb, md: null});
  for (const r of m.runs) {
    if (projects.has(r.name)) projects.get(r.name).md = r;
    else projects.set(r.name, {kb: null, md: r});
  }
  state.projects = projects;
  const sel = $('#kb-select');
  const prev = selectName || sel.value;
  sel.replaceChildren(el('option', {value: '', text: '— pick a project —'}));
  for (const name of [...projects.keys()].sort((a, b) =>
      a.toLowerCase().localeCompare(b.toLowerCase()))) {
    const p = projects.get(name);
    const hint = p.kb ? (p.md ? '  · md + KB' : '') : '  · md only';
    sel.append(el('option', {value: name, text: name + hint}));
  }
  if (prev) sel.value = prev;
  selectProject(sel.value);
}

function selectProject(name) {
  state.project = name || null;
  const p = (name && state.projects.get(name)) || {kb: null, md: null};
  state.kb = p.kb;
  saveSession();
  // With a project selected, the new-project controls are ghosted —
  // Clear first to make another one (prevents half-typed strays).
  $('#kb-name').disabled = !!name;
  $('#kb-lang').disabled = !!name;
  $('#kb-create-btn').disabled = !!name;
  $('#kb-clear-btn').classList.toggle('hidden', !name);
  const box = $('#kb-info');
  if (!name) {
    box.classList.add('hidden');
  } else if (state.kb) {
    const k = state.kb;
    box.classList.remove('hidden');
    box.innerHTML = `
      <b>${esc(k.name)}</b> <span class="muted">${esc(k.path)}</span><br>
      <span class="muted">model ${esc(k.model || '?')} · lang ${esc(k.language || '?')} ·
      endpoint ${esc(k.endpoint || k.endpoint_url || '?')} ·
      ${k.docs} docs · ${k.concepts} concepts · ${k.entities} entities ·
      ${k.images} images · ${k.raw_files} raw files</span>` +
      (k.description ? `<br><span class="muted"
        title="The project description — what MCP clients see when picking a knowledge base. Auto-written after each ingest; curate it with: okforge describe">“${esc(k.description)}”</span>` : '');
  } else {
    box.classList.remove('hidden');
    box.innerHTML = `
      <b>${esc(name)}</b> <span class="muted">markdown so far — no knowledge
      base yet; ingest from stage 5 (or the stage-4 auto-ingest toggle)
      creates it</span>` +
      (p.md ? `<br><span class="muted">${p.md.chunks} chunk(s),
        ${p.md.files} file(s) in md-out/${esc(name)}/</span>` : '');
  }
  if (name) {
    box.append(el('div', {class: 'row'},
      el('button', {class: 'small danger', text: 'Delete project…',
        title: 'Remove the whole project — markdown to trash/, knowledge base ' +
          'to kbs-retired/, published site to trash/. Everything is moved ' +
          'aside, never erased.',
        onclick: () => deleteProject().catch(e => toast(e.message))})));
  }
  updateStageGates();
  renderVerify();
  refreshJobs().catch(() => {});  // re-apply the per-project job filter
}

async function retireKb() {
  if (!state.kb) return;
  const name = state.kb.name;
  const typed = prompt(
    `Retire KB "${name}"?\n\nIt moves to kbs-retired/ — nothing is deleted, ` +
    'and restoring is just moving the directory back.\n\n' +
    'Type the KB name to confirm:');
  if (typed === null) return;
  if (typed.trim() !== name) { toast('name did not match — KB not retired'); return; }
  const r = await api(`/api/kbs/${encodeURIComponent(name)}`, {method: 'DELETE'});
  toast(`KB ${name} retired to ${r.retired_to}`, 'info');
  $('#kb-select').value = '';
  // The project may live on as markdown-only (md-out dir stays put).
  await loadKbs(state.project);
  refreshJobs().catch(() => {});
}

async function createProject() {
  const name = $('#kb-name').value.trim();
  if (!name) { toast('project name required'); return; }
  const d = await api('/api/md-out', {json: {name}});
  toast(`created project ${d.name}`, 'info');
  await loadKbs(d.name);
}

// ------------------------------------------- deletes (all trash-style)

async function deletePdf() {
  const path = $('#inbox-select').value;
  if (!path) { toast('pick a PDF first'); return; }
  const name = path.split(/[\\/]/).pop();
  if (!confirm(`Delete ${name} from the inbox?\n\n` +
               'It moves to trash/inbox/ — recoverable by moving it back.\n\n' +
               'Finished runs and the knowledge base keep their results, but ' +
               "this PDF's runs can no longer be resumed or re-OCR'd " +
               'unless you restore it.')) return;
  const d = await api(`/api/inbox/${encodeURIComponent(name)}`, {method: 'DELETE'});
  toast(`${d.name} moved to trash` +
        (d.purged_jobs ? `; ${d.purged_jobs} old job row(s) cleared` : ''), 'info');
  $('#inbox-select').value = '';
  pdfSelectionChanged(null);
  await loadInbox();
  refreshJobs().catch(() => {});
}

async function deleteMd() {
  const name = state.project;
  if (!name) return;
  if (!confirm(`Delete ALL markdown of project "${name}"?\n\n` +
               `md-out/${name}/ moves to trash/md-out/ — recoverable. ` +
               'The knowledge base (if any) is untouched.')) return;
  await api(`/api/md-out/${encodeURIComponent(name)}`, {method: 'DELETE'});
  toast(`markdown of ${name} moved to trash`, 'info');
  await loadKbs(name);  // md-only projects vanish; selection then clears
}

async function deleteSite() {
  const name = state.project;
  if (!name || !state.kb) return;
  if (!confirm(`Remove the published site of "${name}"?\n\n` +
               'It moves to trash/sites/ — the knowledge base is untouched.')) return;
  await api(`/api/kb/${encodeURIComponent(name)}/site`, {method: 'DELETE'});
  toast(`site of ${name} moved to trash`, 'info');
  await loadKbs(name);
}

async function deleteProject() {
  const name = state.project;
  if (!name) return;
  const p = state.projects.get(name) || {};
  const parts = [];
  if (p.md) parts.push('markdown → trash/md-out/');
  if (p.kb) parts.push('knowledge base → kbs-retired/');
  if (p.kb && p.kb.published) parts.push('published site → trash/sites/');
  const typed = prompt(
    `Delete project "${name}"?\n\nEverything is moved aside, not erased:\n  ` +
    (parts.join('\n  ') || '(nothing found on disk)') +
    '\n\nType the project name to confirm:');
  if (typed === null) return;
  if (typed.trim() !== name) { toast('name did not match — nothing deleted'); return; }
  const activeJobs = (await api('/api/jobs?active=1&limit=1000')).jobs;
  if (activeJobs.some(j => j.kb === name || j.params.out_name === name)) {
    toast('project has queued/running jobs — wait for them or cancel them first');
    return;
  }
  const gone = [];
  if (p.kb && p.kb.published) {
    await api(`/api/kb/${encodeURIComponent(name)}/site`, {method: 'DELETE'});
    gone.push('site');
  }
  if (p.kb) {
    await api(`/api/kbs/${encodeURIComponent(name)}`, {method: 'DELETE'});
    gone.push('knowledge base');
  }
  if (p.md) {
    await api(`/api/md-out/${encodeURIComponent(name)}`, {method: 'DELETE'});
    gone.push('markdown');
  }
  toast(`project ${name} deleted (${gone.join(', ') || 'nothing on disk'}) — ` +
        'recoverable from trash/ and kbs-retired/', 'info');
  $('#kb-select').value = '';
  await loadKbs();
  refreshJobs().catch(() => {});
}

// ----------------------------------------------------------- stage 4: run

function runRange() {
  const max = state.probe ? state.probe.page_count : 1;
  let from = parseInt($('#run-from').value, 10) || 1;
  let to = parseInt($('#run-to').value, 10) || max;
  from = Math.max(1, Math.min(from, max));
  to = Math.max(from, Math.min(to, max));
  return [from, to];
}

function chunkList(from, to, chunkPages) {
  const chunks = [];
  for (let s = from; s <= to; s += chunkPages)
    chunks.push([s, Math.min(s + chunkPages - 1, to)]);
  return chunks;
}

function updateChunkPlan() {
  const box = $('#chunk-plan');
  if (!state.probe) { box.textContent = ''; return; }
  const n = parseInt($('#chunk-pages').value, 10) || 20;
  const [from, to] = runRange();
  const chunks = chunkList(from, to, n);
  const preview = chunks.slice(0, 6).map(([s, e]) => s === e ? `${s}` : `${s}–${e}`).join(', ');
  box.textContent = `${chunks.length} chunk(s): ${preview}${chunks.length > 6 ? ', …' : ''}`;
}

async function startRun() {
  if (!state.pdf || !state.probe) { toast('probe a PDF first (stage 1)'); return; }
  if (!state.project) { toast('pick or create a project first (stage 3)'); return; }
  // Duplicate-run guard: a second Start run used to silently enqueue a
  // whole second job tree (bit a real run on 2026-07-05, and again as
  // job #570 on 2026-07-15). Two lessons baked in: (a) fetch active
  // jobs only — a big run's own children flood any newest-N window and
  // bury the row the guard needs; (b) match children too, not just the
  // 'full' parent — the parent finishes seconds after expanding, so a
  // live run is visible only through its queued/running children.
  const existing = (await api('/api/jobs?active=1&limit=1000')).jobs.find(j =>
    ['queued', 'running'].includes(j.status) &&
    j.params.pdf === state.pdf && !j.kb &&
    j.params.out_name === state.project);
  if (existing) {
    const runId = existing.parent ?? existing.id;
    if (!confirm(`Run #${runId} already covers this PDF and project ` +
                 `(#${existing.id} ${existing.type} is ${existing.status}). ` +
                 'Usually the "resume" button on that run in the job queue is what ' +
                 'you want. Start a second run anyway? (Finished chunks would be skipped.)')) {
      return;
    }
  }
  // Every run produces markdown into md-out/<project>/ — ingest into the
  // KB is a separate step (stage 5), or auto-queued via the toggle.
  const [from, to] = runRange();
  const body = {
    type: 'full',
    dest: 'md-out',
    out_name: state.project,
    pdf: state.pdf,
    pages: `${from}-${to}`,
    chunk_pages: parseInt($('#chunk-pages').value, 10) || 20,
    endpoint: $('#endpoint').value,
  };
  if ($('#run-textlayer').checked) {
    // Explicitly trusted text layer: chunked pymupdf extraction (no OCR,
    // no images). Opt-in only: old scans often carry poor embedded OCR.
    body.text_layer = true;
  } else {
    Object.assign(body, {
      figures: $('#run-figures').checked,
      translate: $('#run-translate').checked,
      src_lang: state.probe.language_guess || null,
      prompt_extra: $('#run-prompt-extra').value.trim() || null,
    });
  }
  if ($('#run-auto-ingest').checked) {
    body.auto_ingest = true;
    body.lang = $('#kb-lang').value.trim() || 'en';
  }
  const job = await api('/api/jobs', {json: body});
  toast(`queued job #${job.id} (${job.type})`, 'info');
  refreshJobs();
}

// ------------------------------- markdown panel (stage 5, every project)

function mdOutUrl(rel) {
  const base = `/api/md-out/${encodeURIComponent(state.project)}`;
  return rel ? base + '/' + rel.split('/').map(encodeURIComponent).join('/') : base;
}

async function renderVerifyMd() {
  $('#verify-md').classList.remove('hidden');
  $('#md-browse-link').href = mdOutUrl('');
  const btn = $('#md-ingest-btn');
  btn.textContent = state.kb
    ? 'Ingest new md → knowledge base'
    : 'Create knowledge base & ingest md';
  // The create step is the next big action for a fresh project — give
  // it the primary (blue) look; the incremental ingest stays low-key.
  btn.classList.toggle('primary', !state.kb);
  const list = $('#md-files');
  const empty = () => {
    $('#md-summary').textContent = '';
    btn.classList.add('hidden');
    $('#md-browse-link').classList.add('hidden');
    $('#md-delete-btn').classList.add('hidden');
    list.replaceChildren(el('div', {class: 'muted',
      text: 'no markdown yet — start a run in stage 4, or use "Add ' +
            'markdown…" to bring your own files'}));
  };
  let d;
  try {
    d = await api(mdOutUrl(''));
  } catch (e) {
    empty();  // legacy KB-only project: no md-out dir yet — runs create it
    return;
  }
  const mds = d.files.filter(f =>
    f.path.endsWith('.md') && !f.path.endsWith('_src.md') && !f.path.includes('/'));
  if (!mds.length) { empty(); return; }
  $('#md-browse-link').classList.remove('hidden');
  $('#md-delete-btn').classList.remove('hidden');
  // Which chunks the KB already has — the ingest button only shows
  // while there is something left to ingest.
  let indexed = new Set();
  if (state.kb) {
    try {
      indexed = new Set((await api(
        `/api/kb/${encodeURIComponent(state.kb.name)}/docs`)).docs);
    } catch (e) { /* engine hiccup: fall through, button stays visible */ }
  }
  const uningested = mds.filter(f => !indexed.has(f.path.replace(/\.md$/, '')));
  const images = d.files.filter(f => IMG_EXT_RE.test(f.path)).length;
  $('#md-summary').textContent =
    `${mds.length} markdown chunk(s)` +
    (state.kb ? `, ${uningested.length} not ingested yet` : '') +
    (images ? `, ${images} image(s)` : '');
  btn.classList.toggle('hidden', state.kb ? !uningested.length : false);
  list.replaceChildren();
  for (const f of mds) {
    const row = el('div', {class: 'row'},
      el('a', {href: mdOutUrl(f.path), target: '_blank', rel: 'noopener',
               text: f.path}),
      el('span', {class: 'muted', text: `${(f.size / 1024).toFixed(1)} KB`}));
    if (state.kb && !indexed.has(f.path.replace(/\.md$/, '')))
      row.append(el('span', {class: 'muted', text: '· not ingested yet'}));
    list.append(row);
  }
}

// Hand-made markdown/text straight into the project — the no-OCR input
// path. Files land in md-out/<project>/ and the normal ingest button
// picks them up like any OCR'd chunk.
async function addMdFiles() {
  const input = $('#md-add-file');
  const files = [...input.files];
  input.value = '';  // else re-picking the same files fires no change event
  if (!state.project || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const d = await api(`/api/md-out/${encodeURIComponent(state.project)}/files`,
                      {method: 'POST', body: fd});
  toast(`added ${d.added.length} file(s) to ${state.project} — ` +
        'ingest them from this panel when ready', 'info');
  renderVerifyMd().catch(() => {});
  loadKbs(state.project).catch(() => {});  // md-only project counts changed
}

async function ingestMdRun() {
  const name = state.project;
  if (!name) return;
  // Double-queue guard: two ingest clicks 8s apart both ran on
  // 2026-07-18 (jobs 601/602) because nothing stopped the second.
  const activeJobs = (await api('/api/jobs?active=1&limit=1000')).jobs;
  if (activeJobs.some(j => j.type === 'ingest_md' && j.kb === name)) {
    toast('an ingest for this project is already queued or running');
    return;
  }
  const lang = $('#kb-lang').value.trim() || 'en';
  const ep = $('#endpoint').value;
  if (!state.kb &&
      !confirm(`Create knowledge base "${name}" (lang ${lang}, endpoint ${ep}) ` +
               'and ingest the markdown into it?')) return;
  const job = await api('/api/jobs', {json: {
    type: 'ingest_md', kb: name, src: 'md-out', out_name: name,
    create_kb: true, lang, endpoint: ep}});
  toast(`queued job #${job.id} (ingest md → KB ${name})`, 'info');
  refreshJobs();
}

async function startReocr() {
  if (!state.pdf || !state.probe) { toast('probe a PDF first (stage 1)'); return; }
  if (!state.kb) { toast('pick a KB first (stage 3)'); return; }
  const page = parseInt($('#reocr-page').value, 10);
  if (!page || page < 1) { toast('enter a page number'); return; }
  if (!checkPagesSpec(String(page))) return;
  const job = await api('/api/jobs', {json: {
    type: 'reocr',
    kb: state.kb.name,
    pdf: state.pdf,
    pages: String(page),
    figures: $('#run-figures').checked,
    tables: $('#reocr-tables').checked,
    prompt_extra: $('#run-prompt-extra').value.trim() || null,
    translate: $('#run-translate').checked,
    src_lang: state.probe.language_guess || null,
  }});
  toast(`queued job #${job.id} (re-OCR p.${page}) — re-ingest the chunk after it finishes`, 'info');
  refreshJobs();
}

async function startRecompile() {
  if (!state.pdf || !state.probe) { toast('probe a PDF first (stage 1)'); return; }
  if (!state.kb) { toast('pick a KB first (stage 3)'); return; }
  const page = parseInt($('#reocr-page').value, 10);
  if (!page || page < 1) { toast('enter a page number (the chunk containing it is re-ingested)'); return; }
  if (!checkPagesSpec(String(page))) return;
  const job = await api('/api/jobs', {json: {
    type: 'recompile',
    kb: state.kb.name,
    pdf: state.pdf,
    pages: String(page),
  }});
  toast(`queued job #${job.id} (re-ingest chunk containing p.${page})`, 'info');
  refreshJobs();
}

function fmtEta(secs) {
  if (secs < 90) return `${secs}s`;
  const m = Math.round(secs / 60);
  if (m < 90) return `${m} min`;
  return `${(m / 60).toFixed(1)} h`;
}

async function retryJob(id) {
  const j = await api(`/api/jobs/${id}/retry`, {method: 'POST'});
  toast(`queued job #${j.id} (${j.type})`, 'info');
  refreshJobs();
}

async function cancelJob(id) {
  await api(`/api/jobs/${id}/cancel`, {method: 'POST'});
  refreshJobs();
}

// Human title for the live panel: "OCR pages p.1-2 · book.pdf (#593)".
function watchTitle(job) {
  const pdfName = (job.params.pdf || '').split(/[\\/]/).pop();
  return jobTypeLabel(job) +
    (job.params.pages ? ` p.${job.params.pages}` : '') +
    (pdfName ? ` · ${pdfName}` : '') +
    ` (#${job.id})`;
}

function watchJob(job) {
  if (state.watchingJobId === job.id) return;
  if (state.watchEs) state.watchEs.close();  // never two streams at once
  state.watchingJobId = job.id;
  const seenCrops = new Set();
  const live = $('#run-live');
  live.classList.remove('hidden');
  $('#run-live-title').textContent = watchTitle(job);
  $('#run-log').textContent = '';
  $('#run-crops').replaceChildren();
  const bar = $('#run-progress'), barText = $('#run-progress-text');
  let range = null;  // [start, end] of the chunk, for the progress bar
  if (job.params.pages) {
    const [s, e] = String(job.params.pages).split('-').map(Number);
    range = [s, e || s];
    bar.max = range[1] - range[0] + 1;
  } else if (state.probe && job.params.pdf === state.probe.pdf) {
    range = [1, state.probe.page_count];
    bar.max = state.probe.page_count;
  }
  bar.value = 0;
  barText.textContent = '';
  state.watchEs = attachEvents(job.id, {
    onLog: line => {
      const log = $('#run-log');
      log.textContent += line + '\n';
      log.scrollTop = log.scrollHeight;
    },
    onProgress: p => {
      if (range) {
        bar.value = Math.max(bar.value, p.page - range[0]);
        barText.textContent = `page ${p.page} of ${range[0]}–${range[1]}`;
      } else {
        barText.textContent = `page ${p.page}`;
      }
    },
    onImage: im => {
      if (seenCrops.has(im.url)) return;
      seenCrops.add(im.url);
      $('#run-crops').append(el('figure', {},
        el('img', {src: im.url, onclick: () => window.open(im.url)}),
        el('figcaption', {text: `p.${im.page}`})));
    },
    onStatus: s => {
      // a stale stream's terminal event must not clear another job's watch
      if (state.watchingJobId !== job.id) return;
      $('#run-live-title').textContent = `${watchTitle(job)} · ${s.status}`;
      if (['done', 'failed', 'cancelled'].includes(s.status)) {
        if (range) bar.value = bar.max;
        state.watchingJobId = null;
        refreshJobs();
        if (state.project) loadKbs(state.project).catch(() => {});  // stats moved
      }
    },
  });
}

// Plain-language labels for job types (raw type stays in the tooltip).
const JOB_TYPE_LABELS = {
  full: 'OCR + ingest', add: 'ingest chunk', ocr: 'OCR pages',
  translate: 'translate', reocr: 're-OCR page', recompile: 're-ingest chunk',
  pilot: 'pilot', extract: 'extract text', publish: 'Publish site',
  ingest_md: 'Ingest into knowledge base',
  describe: 'write project description',
};

function jobTypeLabel(j) {
  if (j.type === 'full' && j.params.dest === 'md-out') return 'OCR → markdown';
  return JOB_TYPE_LABELS[j.type] || j.type;
}

// id → status from the previous refresh, to detect running→done transitions
let prevJobStatuses = new Map();
// Runs whose technical child steps the user expanded (▸/▾ toggle);
// survives the 5s refresh cycle.
const expandedJobs = new Set();
// Whether Publish is currently held for an in-flight ingest, so
// #site-status is only rewritten when the gate opens/closes.
let publishGated = false;

function flashVerifyStage() {
  const h = document.querySelector('#stage-verify h2');
  h.classList.remove('flash');
  void h.offsetWidth;  // restart the animation if already flashing
  h.classList.add('flash');
  setTimeout(() => h.classList.remove('flash'), 2600);
}

async function refreshJobs() {
  // Limit must comfortably cover a full book run so children aren't
  // fetched without their parent row. Size for the worst case actually
  // seen: a 191-page book at 1 page per chunk is ~400 rows (2 children
  // per chunk + parent), which blew past the old 150.
  const d = await api('/api/jobs?limit=1000');
  // Completion signal: a top-level run finishing should point at stage 5
  // instead of ending silently. Empty map on first refresh after a page
  // load means already-done jobs never re-toast.
  let kbStatsStale = false;
  for (const j of d.jobs) {
    const prev = prevJobStatuses.get(j.id);
    if (['queued', 'running'].includes(prev) && j.status === 'done' &&
        !j.parent && ['full', 'add', 'publish', 'ingest_md'].includes(j.type)) {
      if (j.type === 'full' && j.params.dest === 'md-out') {
        // The md-out parent is just the expander — chunks are still
        // queued; the roll-up on its row tracks real progress.
        toast(`run #${j.id} started — markdown appears in stage 5 as chunks finish`, 'info');
      } else if (j.type === 'ingest_md' &&
                 !d.jobs.some(k => k.parent === j.id)) {
        // Ingest that queued no adds: say so, or the click looks ignored.
        toast(`ingest #${j.id}: nothing new to ingest — the KB already has every chunk`, 'info');
      } else if (j.type === 'ingest_md') {
        // This parent is just the expander — chunks are still queued.
        // The real "finished" signal fires when the publish gate lifts.
        toast(`ingest #${j.id} started — the KB fills in as chunks land`, 'info');
        // First ingest creates the KB — refresh so its panel appears.
        if (j.kb === state.project)
          loadKbs(state.project).catch(() => {});
      } else {
        toast(`Run #${j.id} finished — ask your KB in stage 5`, 'info');
        flashVerifyStage();
      }
    }
    // A finished chunk of the selected project → refresh its md panel.
    if (['queued', 'running'].includes(prev) &&
        ['done', 'failed'].includes(j.status) && !j.kb &&
        j.params.dest === 'md-out' && j.params.out_name === state.project) {
      renderVerifyMd().catch(() => {});
    }
    // An ingested chunk (add child) of the selected project → the KB
    // stats tiles are stale. The parent-only checks above never see
    // these, and the auto-watch may not be following them (state.kb is
    // still null while the ingest's create_kb KB is brand new).
    if (['queued', 'running'].includes(prev) && j.status === 'done' &&
        j.type === 'add' && j.kb === state.project) {
      kbStatsStale = true;
    }
  }
  prevJobStatuses = new Map(d.jobs.map(j => [j.id, j.status]));
  // One refresh per cycle no matter how many chunks landed; loadKbs →
  // selectProject → renderVerify redraws the tiles (and reveals the KB
  // panel on a new KB's first chunk).
  if (kbStatsStale) loadKbs(state.project).catch(() => {});
  // Default view: the selected KB's jobs only — a couple of book runs
  // would otherwise grow the table forever. Anything still queued or
  // running stays visible regardless of KB (the queue is serial
  // machine-wide, so another KB's active job explains any waiting).
  // Finished KB-less jobs (pilots) show only for the currently probed
  // PDF: your pilot never vanishes mid-flow, but pilots from other
  // books/days don't pile up. "show all KBs" reveals everything.
  const active = j => ['queued', 'running'].includes(j.status);
  const kblessRelevant = j => !j.kb && (
    (state.pdf && j.params.pdf === state.pdf) ||
    (state.project && j.params.out_name === state.project));
  // The stage-1 rescan reset "clears" the queue view: finished jobs from
  // before the reset stay hidden (still in the DB; "show all" reveals).
  const cleared = j => !active(j) && j.id <= (state.clearedBelowId || 0);
  let jobsToShow;
  if ($('#jobs-all').checked) jobsToShow = d.jobs;
  else if (state.kb) jobsToShow = d.jobs.filter(j =>
    !cleared(j) && (j.kb === state.kb.name || active(j) || kblessRelevant(j)));
  else jobsToShow = d.jobs.filter(j =>
    !cleared(j) && (active(j) || kblessRelevant(j)));
  const tbody = $('#jobs-table tbody');
  tbody.replaceChildren();
  // group children under their parent, newest parents first
  const byParent = new Map();
  const tops = [];
  for (const j of jobsToShow) {
    if (j.parent) {
      if (!byParent.has(j.parent)) byParent.set(j.parent, []);
      byParent.get(j.parent).push(j);
    } else tops.push(j);
  }
  // children whose parent fell outside the fetch window must still render
  // (otherwise a long run's active jobs silently vanish from the table)
  const topIds = new Set(tops.map(j => j.id));
  for (const [pid, kids] of byParent) {
    if (!topIds.has(pid)) tops.push(...kids);
  }
  // While an ingest for the selected project is in flight, its button
  // is a no-op — grey it out (double-queue guard's visible half).
  $('#md-ingest-btn').disabled = !!(state.project && d.jobs.some(j =>
    ['queued', 'running'].includes(j.status) &&
    j.type === 'ingest_md' && j.kb === state.project));
  // Publishing mid-ingest would build a site missing the chunks still
  // in flight — hold the button until every add lands. The gate flag
  // keeps #site-status writes to transitions only, so renderVerify's
  // own text isn't clobbered every 5 s.
  const ingestActive = !!(state.project && d.jobs.some(j =>
    ['queued', 'running'].includes(j.status) &&
    ['ingest_md', 'add'].includes(j.type) && j.kb === state.project));
  $('#publish-btn').disabled = ingestActive;
  if (ingestActive !== publishGated) {
    publishGated = ingestActive;
    $('#site-status').textContent = ingestActive
      ? 'ingest in progress — publish when it finishes'
      : (state.kb && state.kb.published ? '' : 'no site built yet');
    if (!ingestActive) {
      toast('Ingest finished — ask your KB in stage 5', 'info');
      flashVerifyStage();
      // The KB may have just been created by this ingest — without a
      // reload, stage 3/5 keep claiming "md only / no knowledge base
      // yet" until the user refreshes (seen live 2026-07-22).
      loadKbs(state.project).catch(() => {});
    }
  }
  const addRow = (j, isChild) => {
    const tr = el('tr', isChild ? {class: 'child'} : {});
    const composite = ['full', 'ingest_md'].includes(j.type);
    const kids = byParent.get(j.id) || [];
    // Composite-job rollup, computed up front: it feeds both the status
    // line and the resume-button decision below.
    let rollup = null;
    if (composite) {
      const mdOut = j.params.dest === 'md-out';
      const finalType = j.type === 'ingest_md' ? 'add'
        : mdOut
          ? (j.params.text_layer ? 'extract'
             : j.params.translate ? 'translate' : 'ocr')
          : 'add';
      const chunks = new Map();
      for (const c of kids) {
        const key = c.params.pages || c.params.path;
        if (c.type !== finalType || !key) continue;
        if (!chunks.has(key)) chunks.set(key, false);
        if (c.status === 'done') chunks.set(key, true);
      }
      if (chunks.size)
        rollup = {mdOut,
                  done: [...chunks.values()].filter(Boolean).length,
                  total: chunks.size};
    }
    const actions = el('td');
    if (['queued', 'running'].includes(j.status))
      actions.append(el('button', {class: 'small', text: 'cancel',
        onclick: () => cancelJob(j.id)}));
    // A fully-completed composite offers no resume — there is nothing to
    // resume, and the button read as "something is unfinished". It shows
    // only when interrupted (failed/cancelled) or done with chunks
    // missing — and never while any child step is still queued/running
    // (the parent row IS the running run then; resuming would double it).
    const kidsActive = kids.some(c => ['queued', 'running'].includes(c.status));
    const resumable = composite
      ? (!kidsActive &&
         (['failed', 'cancelled'].includes(j.status) ||
          (j.status === 'done' && rollup && rollup.done < rollup.total)))
      : ['done', 'failed', 'cancelled'].includes(j.status);
    if (resumable && j.type !== 'pilot')
      actions.append(' ', el('button', {class: 'small',
        text: composite ? 'resume' : 'retry',
        title: composite
          ? 'Re-expand this run with identical params; already-done chunks are skipped'
          : 'Re-queue this step with identical params',
        onclick: () => retryJob(j.id)}));
    if (j.status === 'running' || (j.status !== 'queued' && j.type !== 'full'))
      actions.append(' ', el('button', {class: 'small', text: 'watch',
        onclick: () => watchJob(j)}));
    if (j.type === 'pilot' && j.status === 'done')
      actions.append(' ', el('button', {class: 'small', text: 'review',
        onclick: () => {
          $('#stage-pilot').classList.remove('disabled');
          $('#pilot-gate-hint').classList.add('hidden');
          loadPilotReview(j.id, j.params.pdf).catch(e => toast(e.message));
          $('#stage-pilot').scrollIntoView({behavior: 'smooth'});
        }}));
    let statusText = j.status + (j.error ? ' — ' + j.error : '');
    let statusClass = j.status;
    if (j.params.retried_as) statusText += ` → retried as #${j.params.retried_as}`;
    // One line answering "is the run complete?" — a chunk counts done
    // when ANY final step covering it succeeded (add for ingests, the
    // last OCR-side step for md runs).
    if (rollup) {
      // The expander parent finishes seconds in; while its child steps
      // are still going the collapsed row should read as running work.
      if (j.status === 'done' && kidsActive) {
        statusText = 'working';
        statusClass = 'running';
      }
      statusText += ` — ${rollup.done}/${rollup.total} chunks ` +
        (rollup.mdOut ? 'OCR’d' : 'ingested');
      if (rollup.done === rollup.total && j.status === 'done' && !kidsActive)
        statusClass = 'done';
    } else if (j.type === 'ingest_md' && j.status === 'done' && !kids.length) {
      statusText += ' — nothing new to ingest';
    }
    if (j.status === 'running') {
      if (j.stalled) {
        statusClass += ' stalled';
        statusText += ` — stalled? log silent ${Math.round(j.log_idle_seconds / 60)} min`;
      } else if (j.eta_seconds != null) {
        statusText += ` — ~${fmtEta(j.eta_seconds)} left`;
      }
    }
    // Runs collapse to one row; ▸ reveals the technical child steps.
    const idCell = el('td');
    if (!isChild && kids.length) {
      const open = expandedJobs.has(j.id);
      idCell.append(el('button', {class: 'small expander', text: open ? '▾' : '▸',
        title: open ? 'hide the steps of this run'
                    : `show the ${kids.length} step(s) of this run`,
        onclick: () => {
          if (expandedJobs.has(j.id)) expandedJobs.delete(j.id);
          else expandedJobs.add(j.id);
          refreshJobs().catch(() => {});
        }}), ' ');
    }
    idCell.append('#' + j.id);
    tr.append(
      idCell,
      el('td', {text: jobTypeLabel(j), title: j.type}),
      el('td', {text: j.kb || j.params.out_name || ''}),
      el('td', {text: j.params.pages || ''}),
      el('td', {}, el('span', {class: 'status ' + statusClass, text: statusText})),
      actions);
    tbody.append(tr);
    // Auto-follow a running job — but never hijack an existing watch, and
    // only for the selected KB's jobs (or KB-less ones like pilots). The
    // manual "watch" button stays the explicit override; watchJob nulls
    // watchingJobId on terminal status, so a run's next chunk still follows.
    if (j.status === 'running' && state.watchingJobId === null &&
        (!j.kb || (state.kb && j.kb === state.kb.name)))
      watchJob(j);
  };
  for (const j of tops) {
    addRow(j, false);
    // Child steps render only when the run is expanded — in run order
    // (ascending id): done chunks, then the active one, then queued.
    if (expandedJobs.has(j.id))
      for (const c of (byParent.get(j.id) || []).sort((a, b) => a.id - b.id))
        addRow(c, true);
  }
  if (!tbody.children.length) {
    tbody.append(el('tr', {}, el('td', {colspan: '6', class: 'muted',
      text: state.project ? 'no jobs for this project yet'
                          : 'no jobs yet — run a probe or pilot above'})));
  }
}

// -------------------------------------------------- stage 5: verify & use

function wikiUrl(rel) {
  const base = `/api/kb/${encodeURIComponent(state.kb.name)}/wiki`;
  return rel ? base + '/' + rel.split('/').map(encodeURIComponent).join('/') : base;
}

function renderVerify() {
  if (!state.project) {
    $('#verify-body').classList.add('hidden');
    $('#verify-md').classList.add('hidden');
    $('#verify-empty').classList.remove('hidden');
    return;
  }
  $('#verify-empty').classList.add('hidden');
  // Markdown panel shows for every project; the KB panel joins it once
  // the knowledge base exists (first ingest creates it).
  renderVerifyMd().catch(e => toast(e.message));
  if (!state.kb) {
    $('#verify-body').classList.add('hidden');
    return;
  }
  $('#verify-body').classList.remove('hidden');
  const k = state.kb;
  $('#kb-stats').replaceChildren(...[
    ['docs', k.docs], ['concepts', k.concepts], ['entities', k.entities],
    ['images', k.images], ['citations', k.citations ?? 0],
    ['raw files', k.raw_files],
  ].map(([label, v]) => el('div', {class: 'stat'},
    el('b', {text: String(v)}), el('span', {text: label}))));
  const link = $('#site-link');
  link.classList.toggle('hidden', !k.published);
  link.href = `/api/kb/${encodeURIComponent(k.name)}/site/`;
  $('#raw-link').href = `/api/kb/${encodeURIComponent(k.name)}/raw`;
  $('#sources-md-link').href = `/api/kb/${encodeURIComponent(k.name)}/sources.md`;
  $('#copy-publish-cmd').classList.toggle('hidden', !k.published);
  $('#site-delete-btn').classList.toggle('hidden', !k.published);
  $('#site-status').textContent = k.published
    ? '' : 'no site built yet';
  loadWikiNav().catch(e => toast(e.message));
}

function copyPublishCmd() {
  if (!state.kb || !state.kb.publish_cmd) return;
  const cmd = state.kb.publish_cmd;
  // navigator.clipboard only exists in a secure context (https or localhost);
  // this UI is served over plain http on the LAN vhost, so it's undefined
  // here and .writeText would throw before returning a promise at all.
  if (navigator.clipboard) {
    navigator.clipboard.writeText(cmd)
      .then(() => toast('go-public rsync command copied', 'info'))
      .catch(() => copyViaTextarea(cmd));
  } else {
    copyViaTextarea(cmd);
  }
}

function copyViaTextarea(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  document.body.removeChild(ta);
  toast(ok ? 'go-public rsync command copied' : text, 'info');
}

async function publishSite() {
  if (!state.kb) { toast('pick a KB first'); return; }
  const job = await api('/api/jobs', {json: {type: 'publish', kb: state.kb.name}});
  toast(`queued job #${job.id} (publish) — link appears when the build finishes`, 'info');
  refreshJobs();
}

function wikiFileLink(rel) {
  const name = rel.split('/').pop();
  return el('div', {},
    el('a', {text: name, onclick: () => loadWikiFile(rel).catch(e => toast(e.message))}));
}

function wikiDirNode(rel, label) {
  const ul = el('ul');
  const det = el('details', {}, el('summary', {text: label}), ul);
  det.addEventListener('toggle', async () => {
    if (!det.open || ul.childElementCount) return;
    try {
      const ls = await api(wikiUrl(rel));
      for (const d of ls.dirs)
        ul.append(el('li', {}, wikiDirNode(d, d.split('/').pop() + '/')));
      for (const f of ls.files) ul.append(el('li', {}, wikiFileLink(f)));
      if (!ls.dirs.length && !ls.files.length)
        ul.append(el('li', {class: 'muted', text: '(empty)'}));
    } catch (e) { toast(e.message); }
  });
  return det;
}

async function loadWikiNav() {
  const nav = $('#wiki-nav');
  nav.replaceChildren();
  const root = await api(wikiUrl(''));
  for (const f of root.files) nav.append(wikiFileLink(f));
  for (const d of root.dirs) nav.append(wikiDirNode(d, d + '/'));
}

const IMG_EXT_RE = /\.(jpe?g|png|gif|webp)$/i;

async function loadWikiFile(rel) {
  const art = $('#wiki-article');
  if (IMG_EXT_RE.test(rel)) {
    art.replaceChildren(el('p', {class: 'muted', text: rel}),
      el('img', {src: wikiUrl(rel)}));
    return;
  }
  const text = await api(wikiUrl(rel));
  const curDir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
  art.innerHTML = mdToHtml(stripFrontmatter(text), curDir);
  for (const a of art.querySelectorAll('a[data-wiki]')) {
    a.onclick = e => { e.preventDefault(); openWikiName(a.dataset.wiki); };
  }
}

async function wikiSearch() {
  const q = $('#wiki-search').value.trim();
  if (!q) { toast('type a search term first'); return; }
  if (!state.kb) { toast('pick a KB first'); return; }
  const d = await api(`/api/kb/${encodeURIComponent(state.kb.name)}/search?q=${encodeURIComponent(q)}&limit=30`);
  const art = $('#wiki-article');
  art.replaceChildren(el('h3', {text: `Search: “${q}” — ${d.results.length} hit(s)`}));
  if (!d.results.length) {
    art.append(el('p', {class: 'muted', text: 'No matches. Lexical search: try a different word form.'}));
    return;
  }
  const ul = el('ul');
  for (const r of d.results) {
    const li = el('li');
    const label = r.path + (r.page != null ? ` (p. ${r.page})` : '');
    li.append(el('a', {href: '#', text: label, onclick: e => {
      e.preventDefault();
      loadWikiFile(r.path).catch(err => toast(err.message));
    }}), el('div', {class: 'muted', text: r.snippet}));
    ul.append(li);
  }
  art.append(ul);
}

async function openWikiName(name) {
  // wikilink target: try the usual sections until one resolves
  const slug = name.trim().toLowerCase().replace(/\s+/g, '-');
  for (const cand of [`concepts/${slug}.md`, `entities/${slug}.md`,
                      `summaries/${slug}.md`, `${slug}.md`, `summaries/${name}.md`]) {
    try { await loadWikiFile(cand); return; } catch (e) { /* try next */ }
  }
  toast(`no wiki page found for [[${name}]]`);
}

function wikiResolve(src, curDir) {
  if (/^(https?:)?\/\//.test(src) || src.startsWith('data:')) return src;
  const stack = [];
  for (const part of ((curDir ? curDir + '/' : '') + src).split('/')) {
    if (part === '..') stack.pop();
    else if (part && part !== '.') stack.push(part);
  }
  return wikiUrl(stack.join('/'));
}

function stripFrontmatter(md) {
  // OKF pages open with a ---\n...\n---\n YAML block — bundle metadata,
  // not reading material; the browser shows the body only.
  if (!md.startsWith('---\n')) return md;
  const end = md.indexOf('\n---', 3);
  if (end === -1) return md;
  const nl = md.indexOf('\n', end + 1);
  return nl === -1 ? '' : md.slice(nl + 1).replace(/^\n+/, '');
}

function mdToHtml(md, curDir) {
  const inline = s => {
    s = esc(s);
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g,
      (m, alt, src) => `<img src="${wikiResolve(src, curDir)}" alt="${alt}">`);
    s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,
      (m, n, label) => `<a href="#" data-wiki="${n}">${label || n}</a>`);
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      (m, t, u) => /^https?:/.test(u)
        ? `<a href="${u}" target="_blank" rel="noopener">${t}</a>`
        : `<a href="#" data-wiki="${u.replace(/\.md$/, '')}">${t}</a>`);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/(^|\W)\*([^*]+)\*(?=\W|$)/g, '$1<i>$2</i>');
    return s;
  };
  let html = '', inCode = false, inList = false, para = [], tableRows = [];
  const flushPara = () => {
    if (para.length) { html += `<p>${inline(para.join(' '))}</p>`; para = []; }
  };
  const flushTable = () => {
    if (!tableRows.length) return;
    const cells = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
    const isSep = r => /^[\s|:-]+$/.test(r) && r.includes('-');
    let rows = tableRows, headHtml = '';
    if (rows.length >= 2 && isSep(rows[1])) {
      headHtml = '<thead><tr>' +
        cells(rows[0]).map(c => `<th>${inline(c.trim())}</th>`).join('') +
        '</tr></thead>';
      rows = rows.slice(2);
    }
    html += '<table>' + headHtml + '<tbody>' +
      rows.map(r => '<tr>' +
        cells(r).map(c => `<td>${inline(c.trim())}</td>`).join('') + '</tr>').join('') +
      '</tbody></table>';
    tableRows = [];
  };
  for (const line of md.split('\n')) {
    if (line.startsWith('```')) {
      flushPara(); flushTable();
      html += inCode ? '</pre>' : '<pre>';
      inCode = !inCode;
      continue;
    }
    if (inCode) { html += esc(line) + '\n'; continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      flushPara();
      if (inList) { html += '</ul>'; inList = false; }
      tableRows.push(line);
      continue;
    }
    flushTable();
    const h = line.match(/^(#{1,5})\s+(.*)/);
    if (h) {
      flushPara();
      if (inList) { html += '</ul>'; inList = false; }
      const lvl = Math.min(h[1].length + 1, 6);  // h1 -> h2: page has its own h2s
      html += `<h${lvl}>${inline(h[2])}</h${lvl}>`;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushPara();
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ''))}</li>`;
      continue;
    }
    if (inList && line.trim()) { html += `<li>${inline(line.trim())}</li>`; continue; }
    if (inList) { html += '</ul>'; inList = false; }
    if (!line.trim()) { flushPara(); continue; }
    para.push(line.trim());
  }
  flushPara();
  flushTable();
  if (inList) html += '</ul>';
  if (inCode) html += '</pre>';
  return html;
}

async function runQuery() {
  const q = $('#query-input').value.trim();
  if (!q) { toast('type a question first'); return; }
  if (!state.kb) { toast('pick a KB first'); return; }
  const box = $('#query-answer');
  box.classList.remove('hidden');
  box.textContent = '';
  const btn = $('#query-btn');
  btn.disabled = true;
  try {
    const r = await fetch(`/api/kb/${encodeURIComponent(state.kb.name)}/query`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q}),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      box.textContent += dec.decode(value, {stream: true});
      box.scrollTop = box.scrollHeight;
    }
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------- session persistence

// Survive a reload: the probed PDF and selected KB come back, everything
// else (form fields) is rebuilt by re-running the cheap local probe.
const SESSION_KEY = 'okforge.session';

function saveSession() {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      pdf: state.pdf,
      project: state.project,
      clearedBelowId: state.clearedBelowId || 0,
    }));
  } catch (e) { /* private mode / storage full — persistence is best-effort */ }
}

function loadSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); }
  catch (e) { return null; }
}

async function restoreSession(saved) {
  // The project restore rides through loadKbs(savedProject) → selectProject;
  // this brings back the probed PDF. If it's gone from the inbox, drop it.
  if (!saved || !saved.pdf) return;
  const sel = $('#inbox-select');
  if (![...sel.options].some(o => o.value === saved.pdf)) { saveSession(); return; }
  sel.value = saved.pdf;
  await runProbe();
}

// ------------------------------------------------------------ stage gates

function setGateHint(sel, text) {
  const h = $(sel);
  h.textContent = text;
  h.classList.toggle('hidden', !text);
}

function updateStageGates() {
  // The pilot is never ghosted once a PDF is probed: even "text" PDFs are
  // usually old scans whose embedded OCR we don't trust — the default
  // pipeline renders pages and does our own OCR + image extraction.
  const needProbe = !state.probe;
  const needProject = !state.project;
  $('#stage-pilot').classList.toggle('disabled', needProbe);
  setGateHint('#pilot-gate-hint', needProbe ? 'Probe a PDF in stage 1 first.' : '');
  $('#stage-run').classList.toggle('disabled', needProbe || needProject);
  let runHint = '';
  if (needProbe && needProject)
    runHint = 'Probe a PDF in stage 1 and pick or create a project in stage 3 first.';
  else if (needProbe)
    runHint = 'Probe a PDF in stage 1 first.';
  else if (needProject)
    runHint = 'Pick or create a project in stage 3 first.';
  setGateHint('#run-gate-hint', runHint);
  $('#stage-verify').classList.toggle('disabled', needProject);
}

// ------------------------------------------------------------------ init

function init() {
  // Rescan is a reset: selection returns to the "— pick a PDF —" state,
  // probe/pilot info clears, and finished jobs leave the queue view
  // (they stay in the database — "show all KBs" reveals them).
  $('#inbox-refresh').onclick = async () => {
    try {
      await loadInbox();
      $('#inbox-select').value = '';
      $('#combine-panel').classList.add('hidden');
      pdfSelectionChanged(null);
      const d = await api('/api/jobs?limit=1');
      state.clearedBelowId = d.jobs.length ? d.jobs[0].id : 0;
      saveSession();
      refreshJobs().catch(() => {});
    } catch (e) { toast(e.message); }
  };
  // Auto-probe: picking a PDF probes it right away (fast local scan).
  $('#inbox-select').onchange = e => {
    $('#combine-panel').classList.add('hidden');
    pdfSelectionChanged(e.target.value);
    if (e.target.value) runProbe().catch(err => toast(err.message));
  };
  $('#upload-file').onchange = () => {
    updateUploadControls();
    updateCombinePanel();
  };
  // Discovering in the pilot that you need --figures should carry into the
  // run; unticking the run box must NOT reach back into the pilot.
  $('#pilot-figures').onchange = e => {
    if (e.target.checked) $('#run-figures').checked = true;
  };
  // Same one-way carry for OCR prompt hints refined during the pilot.
  $('#pilot-prompt-extra').onchange = e => {
    if (e.target.value.trim()) $('#run-prompt-extra').value = e.target.value.trim();
  };
  $('#upload-btn').onclick = () =>
    ($('#upload-file').files.length > 1 ? combineUpload() : uploadPdf())
      .catch(e => toast(e.message));
  $('#pilot-btn').onclick = () => runPilot().catch(e => toast(e.message));
  $('#kb-select').onchange = e => selectProject(e.target.value);
  $('#kb-create-btn').onclick = () => createProject().catch(e => toast(e.message));
  $('#kb-clear-btn').onclick = () => {
    $('#kb-select').value = '';
    selectProject('');
  };
  $('#chunk-pages').oninput = updateChunkPlan;
  $('#run-from').oninput = updateChunkPlan;
  $('#run-to').oninput = updateChunkPlan;
  $('#run-btn').onclick = () => startRun().catch(e => toast(e.message));
  $('#md-ingest-btn').onclick = () => ingestMdRun().catch(e => toast(e.message));
  $('#md-add-btn').onclick = () => $('#md-add-file').click();
  $('#md-add-file').onchange = () => addMdFiles().catch(e => toast(e.message));
  $('#pdf-delete-btn').onclick = () => deletePdf().catch(e => toast(e.message));
  $('#md-delete-btn').onclick = () => deleteMd().catch(e => toast(e.message));
  $('#site-delete-btn').onclick = () => deleteSite().catch(e => toast(e.message));
  $('#retire-kb-btn').onclick = () => retireKb().catch(e => toast(e.message));
  $('#reocr-btn').onclick = () => startReocr().catch(e => toast(e.message));
  $('#recompile-btn').onclick = () => startRecompile().catch(e => toast(e.message));
  $('#jobs-all').onchange = () => refreshJobs().catch(() => {});
  $('#query-btn').onclick = () => runQuery().catch(e => toast(e.message));
  $('#query-input').addEventListener('keydown',
    e => { if (e.key === 'Enter') runQuery().catch(err => toast(err.message)); });
  $('#publish-btn').onclick = () => publishSite().catch(e => toast(e.message));
  $('#copy-publish-cmd').onclick = copyPublishCmd;
  $('#wiki-search-btn').onclick = () => wikiSearch().catch(e => toast(e.message));
  $('#wiki-search').addEventListener('keydown',
    e => { if (e.key === 'Enter') wikiSearch().catch(err => toast(err.message)); });
  // Image lightbox: click any wiki image to zoom; click anywhere to close.
  $('#wiki-article').addEventListener('click', e => {
    if (e.target.tagName === 'IMG') {
      $('#lightbox img').src = e.target.src;
      $('#lightbox').classList.remove('hidden');
    }
  });
  $('#lightbox').onclick = () => $('#lightbox').classList.add('hidden');
  document.addEventListener('keydown',
    e => { if (e.key === 'Escape') $('#lightbox').classList.add('hidden'); });
  $('#endpoint').onchange = pollSlots;

  const saved = loadSession();
  state.clearedBelowId = (saved && saved.clearedBelowId) || 0;
  // Older sessions stored kb/md instead of project — same name either way.
  const savedProject = saved && (saved.project || saved.kb || saved.md || undefined);
  Promise.all([
    loadKbs(savedProject).then(pollSlots),
    loadInbox(),
  ]).then(() => restoreSession(saved))
    .catch(e => toast(e.message));
  refreshJobs().catch(e => toast(e.message));
  setInterval(pollSlots, 6000);
  setInterval(() => refreshJobs().catch(() => {}), 5000);
  updateStageGates();
}

document.addEventListener('DOMContentLoaded', init);
