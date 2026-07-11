'use strict';

/* okforge web UI — one page, five stages (PLAN.md). Vanilla JS, no build. */

const $ = s => document.querySelector(s);

const state = {
  pdf: null,        // selected PDF path
  probe: null,      // last probe result
  kb: null,         // selected KB info object
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

// A different PDF in the dropdown invalidates the probe and everything
// staged on it — otherwise a run/pilot would silently use the OLD file.
// Called from the dropdown's onchange AND after an upload auto-selects
// the new file (programmatic .value changes fire no change event).
function pdfSelectionChanged(newValue) {
  if (!state.pdf || newValue === state.pdf) return;
  state.pdf = null;
  state.probe = null;
  $('#probe-result').classList.add('hidden');
  $('#pilot-review').classList.add('hidden');
  $('#pilot-logbox').classList.add('hidden');
  $('#pilot-status').textContent = '';
  updateStageGates();
  saveSession();
}

async function uploadPdf() {
  const f = $('#upload-file').files[0];
  if (!f) { toast('choose a .pdf file first'); return; }
  const fd = new FormData();
  fd.append('file', f);
  const d = await uploadWithProgress('/api/inbox', fd);
  await loadInbox(d.path);
  pdfSelectionChanged(d.path);
  toast(`uploaded ${d.name}`, 'info');
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

async function runPilot(pagesOverride) {
  if (!state.pdf) { toast('probe a PDF first'); return; }
  const pages = pagesOverride || $('#pilot-pages').value.trim();
  if (!pages) { toast('give a pages spec, e.g. 16 or 5-7'); return; }
  const job = await api('/api/jobs', {json: {
    type: 'pilot',
    pdf: state.pdf,
    pages: pages,
    figures: $('#pilot-figures').checked,
    tables: $('#pilot-tables').checked,
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

// ------------------------------------------------------- stage 3: KB setup

async function loadKbs(selectName) {
  const d = await api('/api/kbs');
  state.endpoints = d.endpoints;
  const epSel = $('#endpoint');
  if (!epSel.options.length) {
    epSel.replaceChildren(...d.endpoints.map(e => el('option', {value: e, text: e})));
  }
  const sel = $('#kb-select');
  const prev = selectName || sel.value;
  sel.replaceChildren(el('option', {value: '', text: '— pick a KB —'}));
  for (const kb of d.kbs) sel.append(el('option', {value: kb.name, text: kb.name}));
  if (prev) sel.value = prev;
  state.kbs = d.kbs;
  if (sel.value) selectKb(sel.value);
}

function selectKb(name) {
  state.kb = (state.kbs || []).find(k => k.name === name) || null;
  saveSession();
  const box = $('#kb-info');
  if (!state.kb) { box.classList.add('hidden'); updateStageGates(); return; }
  const k = state.kb;
  box.classList.remove('hidden');
  box.innerHTML = `
    <b>${esc(k.name)}</b> <span class="muted">${esc(k.path)}</span><br>
    <span class="muted">model ${esc(k.model || '?')} · lang ${esc(k.language || '?')} ·
    endpoint ${esc(k.endpoint || k.endpoint_url || '?')} ·
    ${k.docs} docs · ${k.concepts} concepts · ${k.entities} entities ·
    ${k.images} images · ${k.raw_files} raw files</span>`;
  updateStageGates();
  renderVerify();
  refreshJobs().catch(() => {});  // re-apply the per-KB job filter
}

async function createKb() {
  const name = $('#kb-name').value.trim();
  if (!name) { toast('KB name required'); return; }
  const kb = await api('/api/kbs', {json: {
    name: name,
    lang: $('#kb-lang').value.trim() || 'en',
    endpoint: $('#endpoint').value,
  }});
  toast(`created KB ${kb.name}`, 'info');
  await loadKbs(kb.name);
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
  if (!state.kb) { toast('pick or create a KB first (stage 3)'); return; }
  // Duplicate-run guard: a second Start run used to silently enqueue a
  // whole second job tree (bit a real run on 2026-07-05).
  const existing = (await api('/api/jobs?limit=150')).jobs.find(j =>
    j.type === 'full' && ['queued', 'running'].includes(j.status) &&
    j.kb === state.kb.name && j.params.pdf === state.pdf);
  if (existing &&
      !confirm(`Run #${existing.id} already covers this PDF and KB (${existing.status}). ` +
               'Usually the "resume" button on that run in the job queue is what ' +
               'you want. Start a second run anyway? (Finished chunks would be skipped.)')) {
    return;
  }
  let job;
  const useTextLayer = $('#run-textlayer').checked;
  if (useTextLayer && state.probe.page_count < 20) {
    // Short text-layer PDF: the engine's own pymupdf short-doc path is fine.
    job = await api('/api/jobs', {json: {type: 'add', kb: state.kb.name, path: state.pdf}});
  } else if (useTextLayer) {
    // Explicitly trusted text layer: chunked pymupdf extraction (no OCR,
    // no images) instead of the engine's PageIndex TOC path — page-cited
    // short docs. Opt-in only: old scans often carry poor embedded OCR.
    const [from, to] = runRange();
    job = await api('/api/jobs', {json: {
      type: 'full',
      kb: state.kb.name,
      pdf: state.pdf,
      pages: `${from}-${to}`,
      text_layer: true,
      chunk_pages: parseInt($('#chunk-pages').value, 10) || 20,
    }});
  } else {
    const [from, to] = runRange();
    job = await api('/api/jobs', {json: {
      type: 'full',
      kb: state.kb.name,
      pdf: state.pdf,
      pages: `${from}-${to}`,
      figures: $('#run-figures').checked,
      translate: $('#run-translate').checked,
      src_lang: state.probe.language_guess || null,
      chunk_pages: parseInt($('#chunk-pages').value, 10) || 20,
    }});
  }
  toast(`queued job #${job.id} (${job.type})`, 'info');
  refreshJobs();
}

async function startReocr() {
  if (!state.pdf || !state.probe) { toast('probe a PDF first (stage 1)'); return; }
  if (!state.kb) { toast('pick a KB first (stage 3)'); return; }
  const page = parseInt($('#reocr-page').value, 10);
  if (!page || page < 1) { toast('enter a page number'); return; }
  const job = await api('/api/jobs', {json: {
    type: 'reocr',
    kb: state.kb.name,
    pdf: state.pdf,
    pages: String(page),
    figures: $('#run-figures').checked,
    tables: $('#reocr-tables').checked,
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

function watchJob(job) {
  if (state.watchingJobId === job.id) return;
  if (state.watchEs) state.watchEs.close();  // never two streams at once
  state.watchingJobId = job.id;
  const seenCrops = new Set();
  const live = $('#run-live');
  live.classList.remove('hidden');
  $('#run-live-title').textContent = `job #${job.id} — ${job.type}` +
    (job.params.pages ? ` p.${job.params.pages}` : '');
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
      $('#run-live-title').textContent =
        `job #${job.id} — ${job.type}${job.params.pages ? ` p.${job.params.pages}` : ''} · ${s.status}`;
      if (['done', 'failed', 'cancelled'].includes(s.status)) {
        if (range) bar.value = bar.max;
        state.watchingJobId = null;
        refreshJobs();
        if (state.kb) loadKbs(state.kb.name);  // stats moved; refresh
      }
    },
  });
}

// Human labels for job types in the table (raw type stays in the tooltip).
const JOB_TYPE_LABELS = {
  full: 'book run', add: 'ingest', ocr: 'OCR', translate: 'translate',
  reocr: 're-OCR page', recompile: 're-ingest chunk', pilot: 'pilot',
  extract: 'extract', publish: 'publish site',
};

// id → status from the previous refresh, to detect running→done transitions
let prevJobStatuses = new Map();

function flashVerifyStage() {
  const h = document.querySelector('#stage-verify h2');
  h.classList.remove('flash');
  void h.offsetWidth;  // restart the animation if already flashing
  h.classList.add('flash');
  setTimeout(() => h.classList.remove('flash'), 2600);
}

async function refreshJobs() {
  // Limit must comfortably cover a full book run (28 chunks × 2 children
  // + parent) so children aren't fetched without their parent row.
  const d = await api('/api/jobs?limit=150');
  // Completion signal: a top-level run finishing should point at stage 5
  // instead of ending silently. Empty map on first refresh after a page
  // load means already-done jobs never re-toast.
  for (const j of d.jobs) {
    const prev = prevJobStatuses.get(j.id);
    if (['queued', 'running'].includes(prev) && j.status === 'done' &&
        !j.parent && ['full', 'add', 'publish'].includes(j.type)) {
      toast(`Run #${j.id} finished — ask your KB in stage 5`, 'info');
      flashVerifyStage();
    }
  }
  prevJobStatuses = new Map(d.jobs.map(j => [j.id, j.status]));
  // Default view: the selected KB's jobs only — a couple of book runs
  // would otherwise grow the table forever. Anything still queued or
  // running stays visible regardless of KB (the queue is serial
  // machine-wide, so another KB's active job explains any waiting), as
  // do KB-less jobs (pilots) — including finished ones, so a pilot's
  // history never vanishes just because no KB is selected yet.
  const active = j => ['queued', 'running'].includes(j.status);
  let jobsToShow;
  if ($('#jobs-all').checked) jobsToShow = d.jobs;
  else if (state.kb) jobsToShow = d.jobs.filter(j =>
    j.kb === state.kb.name || !j.kb || active(j));
  else jobsToShow = d.jobs.filter(j => active(j) || !j.kb);
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
  const addRow = (j, isChild) => {
    const tr = el('tr', isChild ? {class: 'child'} : {});
    const actions = el('td');
    if (['queued', 'running'].includes(j.status))
      actions.append(el('button', {class: 'small', text: 'cancel',
        onclick: () => cancelJob(j.id)}));
    if (['done', 'failed', 'cancelled'].includes(j.status) && j.type !== 'pilot')
      actions.append(' ', el('button', {class: 'small',
        text: j.type === 'full' ? 'resume' : 'retry',
        title: j.type === 'full'
          ? 'Re-expand this run with identical range + chunk size; already-done chunks are skipped'
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
    // Full-job rollup: one line answering "is the book complete?" —
    // a chunk counts done when ANY add covering its pages succeeded.
    if (j.type === 'full') {
      const kids = byParent.get(j.id) || [];
      const chunks = new Map();
      for (const c of kids) {
        if (c.type !== 'add' || !c.params.pages) continue;
        if (!chunks.has(c.params.pages)) chunks.set(c.params.pages, false);
        if (c.status === 'done') chunks.set(c.params.pages, true);
      }
      if (chunks.size) {
        const done = [...chunks.values()].filter(Boolean).length;
        statusText += ` — ${done}/${chunks.size} chunks ingested`;
        if (done === chunks.size && j.status === 'done') statusClass = 'done';
      }
    }
    if (j.status === 'running') {
      if (j.stalled) {
        statusClass += ' stalled';
        statusText += ` — stalled? log silent ${Math.round(j.log_idle_seconds / 60)} min`;
      } else if (j.eta_seconds != null) {
        statusText += ` — ~${fmtEta(j.eta_seconds)} left`;
      }
    }
    tr.append(
      el('td', {text: '#' + j.id}),
      el('td', {text: JOB_TYPE_LABELS[j.type] || j.type, title: j.type}),
      el('td', {text: j.kb || ''}),
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
    // children in run order (ascending id): done chunks first, then the
    // active one, then what's still queued — reads like a progress list
    for (const c of (byParent.get(j.id) || []).sort((a, b) => a.id - b.id))
      addRow(c, true);
  }
  if (!tbody.children.length) {
    tbody.append(el('tr', {}, el('td', {colspan: '6', class: 'muted',
      text: state.kb ? 'no jobs for this KB yet'
                     : 'no jobs yet — run a probe or pilot above'})));
  }
}

// -------------------------------------------------- stage 5: verify & use

function wikiUrl(rel) {
  const base = `/api/kb/${encodeURIComponent(state.kb.name)}/wiki`;
  return rel ? base + '/' + rel.split('/').map(encodeURIComponent).join('/') : base;
}

function renderVerify() {
  if (!state.kb) return;
  $('#verify-empty').classList.add('hidden');
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
  $('#copy-publish-cmd').classList.toggle('hidden', !k.published);
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
      kb: state.kb ? state.kb.name : null,
    }));
  } catch (e) { /* private mode / storage full — persistence is best-effort */ }
}

function loadSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); }
  catch (e) { return null; }
}

async function restoreSession(saved) {
  // The KB restore rides through loadKbs(saved.kb) → selectKb; this brings
  // back the probed PDF. If it's gone from the inbox, drop it silently.
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
  const needKb = !state.kb;
  $('#stage-pilot').classList.toggle('disabled', needProbe);
  setGateHint('#pilot-gate-hint', needProbe ? 'Probe a PDF in stage 1 first.' : '');
  $('#stage-run').classList.toggle('disabled', needProbe || needKb);
  let runHint = '';
  if (needProbe && needKb)
    runHint = 'Probe a PDF in stage 1 and pick or create a KB in stage 3 first.';
  else if (needProbe)
    runHint = 'Probe a PDF in stage 1 first.';
  else if (needKb)
    runHint = 'Pick or create a KB in stage 3 first.';
  setGateHint('#run-gate-hint', runHint);
  $('#stage-verify').classList.toggle('disabled', needKb);
}

// ------------------------------------------------------------------ init

function init() {
  $('#inbox-refresh').onclick = () => loadInbox().catch(e => toast(e.message));
  $('#inbox-select').onchange = e => pdfSelectionChanged(e.target.value);
  // Discovering in the pilot that you need --figures should carry into the
  // run; unticking the run box must NOT reach back into the pilot.
  $('#pilot-figures').onchange = e => {
    if (e.target.checked) $('#run-figures').checked = true;
  };
  $('#upload-btn').onclick = () => uploadPdf().catch(e => toast(e.message));
  $('#probe-btn').onclick = () => runProbe().catch(e => toast(e.message));
  $('#pilot-btn').onclick = () => runPilot().catch(e => toast(e.message));
  $('#kb-select').onchange = e => selectKb(e.target.value);
  $('#kb-create-btn').onclick = () => createKb().catch(e => toast(e.message));
  $('#chunk-pages').oninput = updateChunkPlan;
  $('#run-from').oninput = updateChunkPlan;
  $('#run-to').oninput = updateChunkPlan;
  $('#run-btn').onclick = () => startRun().catch(e => toast(e.message));
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
  Promise.all([
    loadKbs(saved ? saved.kb : undefined).then(pollSlots),
    loadInbox(),
  ]).then(() => restoreSession(saved))
    .catch(e => toast(e.message));
  refreshJobs().catch(e => toast(e.message));
  setInterval(pollSlots, 6000);
  setInterval(() => refreshJobs().catch(() => {}), 5000);
  updateStageGates();
}

document.addEventListener('DOMContentLoaded', init);
