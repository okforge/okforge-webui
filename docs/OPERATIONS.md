# Operating knowledge bases

Day-to-day KB management, learned the practical way. Commands assume the
layout from the README (`/opt/okforge/{tooling,kbs,inbox,md-out,trash}`);
adjust paths if yours differs. `<tooling>` means this repo's checkout,
`<tooling>/.venv/bin/okforge` the engine CLI.

## Anatomy of a KB

A KB is one **self-contained directory** under the KB root:

```
<Subject>/
  raw/          # ingested inputs (chunk .md + .pages.json, PDFs, images)
  wiki/         # the product: summaries/, concepts/, entities/, sources/, index.md, log.md
  .okforge/     # engine state: config.yaml, hashes.json, files/  (legacy name: .openkb/)
  .env          # LLM endpoint for THIS KB (OPENAI_API_BASE, LLM_API_KEY)
  .git/         # snapshot history (pre-ingest commits)
```

Self-contained means: copy the directory, you've copied the KB — state,
config, wiki, and git history included. The webui discovers KBs by
scanning the KB root for dirs containing an engine state dir on every
request; nothing is "installed" beyond being in that directory.

## Create

- **Web UI (stage 3)**: creates a *project* — an `md-out/<name>/` folder
  that collects the OCR'd markdown. The KB itself is initialized at the
  project's **first ingest** (the stage-5 button or the stage-4
  auto-ingest toggle): config + `.env` for the chosen endpoint, with the
  `llm_extra_body` thinking off-switch written automatically. A project
  whose markdown is never ingested never grows a KB — that's the
  markdown-only mode.
- **CLI**: `mkdir <Subject> && cd <Subject> && okforge init -m <model> -l en`
  (or `init --json` for scripts — fully non-interactive).

## Retire

"Retire KB…" in the stage-5 Knowledge base row (typed-name
confirmation), or `DELETE /api/kbs/<name>`. Archive-first: the KB directory **moves** to
`<base>/kbs-retired/<name>-<date>/` — nothing is deleted, git history
and snapshots travel with it — and its entries leave the engine's
global registry (`known_kbs`, and `default_kb` if it pointed there).
Refused while the KB has queued/running jobs. Restore = move the
directory back under `kbs/`. True deletion stays a deliberate manual
`rm -rf` of the retired copy.

## Deleting from the web UI (all archive-first)

Every delete button in the UI **moves** — nothing is erased. Restore =
move the file/directory back; emptying `trash/` is a deliberate manual
act outside the UI.

- **Delete PDF…** (stage 1): inbox PDF → `trash/inbox/`. Also purges the
  file's *finished* job history from the queue, so a later re-upload of
  the same file starts with a clean slate. Refused (409) while any
  queued/running job still references the PDF. Finished runs and the KB
  keep their results, but a later resume/re-OCR of that PDF fails with a
  clear "source PDF no longer exists — restore it from trash/inbox/"
  message until the file is put back.
- **Delete markdown…** (stage 5, Markdown row): the project's
  `md-out/<name>/` folder → `trash/md-out/` — for redoing the OCR from
  scratch. Hidden while the project has no markdown.
- **Remove site…** (stage 5): published site → `trash/sites/`; the KB
  itself is untouched, re-publish rebuilds it.
- **Retire KB…** (stage 5): KB → `kbs-retired/` (see Retire above).
- **Delete project…** (stage-3 project info box, typed-name
  confirmation): all of the above for one project in a single click —
  site, then KB, then markdown.

## Pull raw sources (external RAG / pipelines)

`raw/` holds the pristine ingested artifacts — the `<stem>_pN_M.md`
chunks exactly as OCR'd, their `.pages.json` page arrays, the
`_images/` crops, and any archived source PDFs. A read-only API serves
them so an external pipeline (e.g. a separate RAG system) can sync a
KB's sources without filesystem access:

```bash
# One call enumerates everything: flat recursive listing, size + mtime
curl http://<host>/api/kb/<name>/raw

# Fetch any listed file (md/json as text, images/PDF as binary)
curl -O http://<host>/api/kb/<name>/raw/<stem>_p1_20.md
curl http://<host>/api/kb/<name>/raw/<stem>_p1_20.pages.json

# All INGESTED chunks concatenated in page order, one markdown file —
# for RAG systems that re-chunk anyway. Source-language variants,
# pilots, and never-added leftovers are excluded automatically.
curl -O http://<host>/api/kb/<name>/sources.md
```

The same `/raw` URL opened in a **browser** renders a download page
instead of JSON (content negotiation on `Accept: text/html`) — per-file
download links plus the one-click `sources.md`. Stage 5 links to both
("raw sources" / "all sources (.md)") for the selected KB.

Prefer these over `wiki/sources/` when fidelity matters: the wiki
copies have image references rewritten at ingest. Hidden scratch
entries (`.reocr_job*`) never appear in listings.

## Copy between machines

From the source machine (trailing slashes matter):

```bash
rsync -a --exclude __pycache__ /opt/okforge/kbs/<Subject>/ user@otherhost:/opt/okforge/kbs/<Subject>/
```

- Appears in the destination's UI on next page load. No restart, no registration.
- `.env`, `llm_extra_body` config, and git history travel with it.
- **Don't copy while something is ingesting into that KB** on the source.
- Keep KBs **pure** — no tool installs inside KB dirs — so copies stay
  cheap and nothing extra rides an rsync.
- The copies are then **independent** — adds on one do not appear on the
  other. Pick a canonical home per KB, or re-rsync to overwrite the stale one.

## Remove / archive

1. Check nothing queued/running for it:
   `sqlite3 <tooling>/webui/jobs.sqlite "SELECT id,type,status FROM jobs WHERE kb='<Subject>' AND status IN ('queued','running');"`
2. `rm -rf` the directory — or `mv` it outside the KB root, which removes it
   just as completely and is reversible.
3. Registry hygiene (usually a no-op — only KBs *created* on that machine are
   registered): check the global config (`~/.config/okforge/`, legacy
   `~/.config/openkb/`) for a stale `known_kbs:` line; repoint
   `default_kb:` if it referenced the removed KB. The webui never uses
   the registry (always passes `--kb-dir`); it only affects bare CLI use.

Removing one *document* from a KB is different: `okforge remove <doc-name>`
inside the KB unwinds its summary, sources, and concept/entity contributions
(the prelude to re-adding a re-OCR'd chunk).

## Per-KB LLM configuration

The webui header's endpoint choice is written into the KB at **first
ingest**; after that the KB's own files below decide which server every
add, query, MCP `ask`, and auto-describe call hits — the header no
longer matters for that KB.

- `.env` — endpoint: `OPENAI_API_BASE=http://<llm-host>:8080/v1`, `LLM_API_KEY=no-key`.
- **Repointing a KB** (LLM moved to a new box / new model): edit `.env`
  (URL + key) and `config.yaml` (`model:`); the next call uses them —
  no restart, nothing to re-register. Keep `llm_extra_body` right for
  the new host (llama.cpp/Qwen: `chat_template_kwargs` thinking-off;
  OpenRouter: `reasoning.enabled: false`). The stage-3 project box
  shows the endpoint a KB currently points at. Live example:
  FloridaHB913's `.env` pins its queries to a different GPU box than
  the one that OCR'd it.
- `.okforge/config.yaml` — `model`, `language`, and:

```yaml
# MANDATORY on llama.cpp hosts serving Qwen-family models (thinking is ON
# by default there): without this every add pays a hidden reasoning block
# (measured: 27 min -> 2.3 min per 20-page chunk add).
llm_extra_body:
  chat_template_kwargs:
    enable_thinking: false
```

- Project description (the MCP `list_projects` 'about' line): the webui
  **auto-writes one after every ingest** — a `describe` job runs one LLM
  call over the index.md document list and refreshes the line as the KB
  grows. Curate it any time with
  `okforge describe "One line about the whole project."` — a curated
  line is detected (it no longer matches the `description.auto` sidecar
  in the state dir) and never overwritten.

## Ingest lifecycle (webui)

- A book run = one `full` job that expands into `ocr` → (`translate`)
  children per chunk, writing markdown into `md-out/<project>/`. The
  queue is strictly serial by design.
- **Ingestion is always a separate job**: `ingest_md` (the stage-5
  button, or queued automatically when the run's auto-ingest toggle is
  on) creates the KB if needed, copies new chunks into its `raw/`, and
  expands into one `add` child per not-yet-indexed chunk, in page
  order. Already-ingested chunks are skipped, so re-running it after
  more OCR ingests only what's new.
- **Bring-your-own markdown**: "Add markdown…" (stage 5, Markdown row)
  uploads `.md`/`.txt` files straight into `md-out/<project>/` — no OCR
  — where the same ingest button treats them like any OCR'd chunk
  (`.txt` is saved as `.md`; duplicate names are refused). Page-scan
  images (jpg/png/tif/bmp) go the other way: the stage-1 upload wraps
  them into a PDF so the normal OCR pipeline applies.
- **Combining page-per-file scans**: selecting several PDFs/images in
  the stage-1 upload merges them into one inbox PDF in natural
  file-name order (`page2` before `page10`); the panel shows the exact
  order and name before uploading, and the per-file page mapping
  after. When file names don't reflect the true order, reorder before
  uploading instead — [PDF Arranger](https://github.com/pdfarranger/pdfarranger)
  (GUI drag-to-reorder) or `img2pdf` + `pdfunite` (CLI) — and upload
  the result as one PDF.
- While an ingest runs, the stage-5 **KB stats update as each chunk
  lands** and **Publish is held** ("ingest in progress") until the last
  chunk is in.
- Every non-skipped `add` first **git-commits the KB** ("pre-ingest
  snapshot...") — a botched ingest is one `git reset --hard` away.
- Every `add` ends by logging an `okf-lint` verdict.
- **Empty-plan guard**: if an add finishes with the KB still at zero
  concepts AND zero entities (the engine's concepts-plan LLM call can
  return a valid-but-empty plan and report success), the add job
  automatically re-rolls the doc once (remove `--keep-raw` + re-add)
  and logs `[WARN]` lines either way. Bounded at one retry — a blank
  document is legitimately concept-free, and a near-deterministic
  model can return the same empty plan every roll; the manual roll is
  "Re-ingest chunk" (stage 4).
- **Resume**: the resume button on a full job re-plans the same range/chunk
  size and skips chunks already on disk (ingests skip chunks already
  indexed). Free no-op audit when everything's done.
- **Retry** on a child clones it to the *back* of the queue under the same
  parent; the old row keeps its terminal status forever.
- **Stalled?** flag = running job with a log silent ≥ 20 min. Advisory only.
  A quiet `add` can be normal; a quiet first-page OCR usually is not.
- **Re-OCR one page** (stage 4, behind the "Fix a badly-OCR'd page
  (advanced)" fold): redoes a single page and splices it into
  its chunk's `.md`/`.pages.json` (and the English pair on translated
  runs). Tick **table mode** for pages with complex tables — model
  reasoning on + an information-first prompt (convey the table's
  meaning, not its grid). Much slower per page; preview with the pilot's
  table-mode checkbox first.
- **Re-ingest chunk** (same fold): after re-OCRing page(s), one job does
  engine `remove --keep-raw --yes` + re-add of the chunk containing the
  given page — the wiki refresh step, no CLI needed.
- **OCR hints** (stages 2 and 4): free-text instructions appended to the
  OCR prompt (`--prompt-extra`) for documents the standard prompt
  mishandles — "ignore marginalia", "columns read right-to-left".
  Refine the wording against a pilot page first; the pilot's hint
  carries into the run field one-way. Applies to pilot, run, and
  re-OCR jobs; ignored in text-layer mode (no OCR happens).
- **Duplicate runs**: Start run warns if a full job for the same
  PDF + project is already active; a second ingest click while one is
  in flight is a guarded no-op. Collapsed queue rows roll up progress
  ("working — N/M chunks OCR'd" / "N/M chunks ingested"); ▸ expands
  the technical child steps.

## What happens when you add the same info twice, or add new info later

- **Exact re-add** (identical file content, from any path): `add` hash-checks
  the content first — a hash it's already seen is skipped outright
  ("Skipping already-known file"), no wiki changes, no LLM calls. Safe to
  re-run `add` blindly; this is what makes resuming a crashed `full` job
  safe.
- **Re-ingesting an edited version of the same source file** (same path,
  content changed — e.g. you fixed an OCR error and re-add): identity is
  keyed by *path*, not content hash, so the doc keeps its original wiki name
  and its summary/source files are overwritten in place, not duplicated.
  Any concepts/entities that doc touches go through the "update" path below.
- **A different file that happens to share a filename stem** with an
  existing doc (different path, unrelated content): gets a deterministic
  `-{hash}` suffix so it can't collide with or overwrite the other document.
- **A new document that mentions a concept/entity an earlier document
  already created**: that concept/entity page is *not* appended to — the
  compiler feeds the LLM the current on-disk page plus the new document's
  info and asks for a full rewrite of the body (only the `sources:`
  frontmatter list is accreted, so citations back to every contributing
  document survive). This full-rewrite behavior is why hand-edited
  concept/entity pages don't reliably survive verbatim once a second
  document touches the same topic — see "Opening the wiki in Obsidian"
  below for what's safe to hand-edit.
- **A new document that only introduces brand-new concepts/entities**:
  those get fresh pages; nothing else on disk changes.
- **Per-document summary pages** (`wiki/summaries/<doc>.md`) are never
  touched by *other* documents' additions — only that doc's own re-ingest,
  or an explicit `okforge recompile <doc>`, ever rewrites one.

## Opening the wiki in Obsidian

Each subject's `wiki/` folder is a self-contained Obsidian vault: **Open
folder as vault** → select `/opt/okforge/kbs/<Subject>/wiki`.

Link style is a per-KB config choice (`link_style:` in
`.okforge/config.yaml`, default `markdown`): relative markdown links work
fine as an Obsidian vault, but `link_style: wikilinks` gets the more
native `[[...]]` experience — Obsidian's own rename-safe linking and a
livelier graph view. `okforge reindex` after changing it on an existing KB.

**Editing pages directly in Obsidian — what's safe and what isn't:**

- Safe to edit freely: anything you wrote yourself (explorations, your own
  notes added alongside the vault) and any page that will never be
  touched by a future ingest — a one-off document's summary in a KB you're
  done adding to.
- **Not safe to assume edits persist**: `wiki/concepts/*.md` and
  `wiki/entities/*.md` pages get rewritten, not appended to, the next time
  *any* document (new or re-ingested) mentions that same concept/entity —
  your edits influence the result but aren't guaranteed to survive
  verbatim. If you're actively still adding sources to a KB, treat
  concept/entity pages as generated output, not a place to keep
  hand-written additions.
- **Don't break the YAML frontmatter block** (the `---`-delimited header
  at the top of every generated page — `type:`, `sources:`, etc.). A
  future update to that same page can still recover from a malformed
  block, but it rebuilds minimal frontmatter from scratch rather than
  preserving whatever else was in it.
- `wiki/index.md`'s "## Documents" list and `wiki/log.md` are managed by
  the engine — hand edits there are the most likely to get clobbered or
  confuse the next `add`/`remove`.
- Run `okforge okf-lint` after manual edits if unsure — it flags missing
  frontmatter/type fields and structural problems (advisory, doesn't block
  anything).

## Publishing a KB as a website (Quartz)

Turns a KB's `wiki/` into a static site with full-text search, a graph
view, backlinks, and hover previews — the shareable counterpart to
Obsidian: no app to install, just a URL.

- **Quartz install (once per machine)** into `<base>/quartz` (needs
  Node.js 18+ — see the README prerequisites).

  Linux/macOS:
  ```bash
  git clone https://github.com/jackyzha0/quartz.git /opt/okforge/quartz
  cd /opt/okforge/quartz
  git checkout v5
  npm ci
  npx quartz plugin install
  ```

  Windows (PowerShell):
  ```powershell
  git clone https://github.com/jackyzha0/quartz.git C:\okforge\quartz
  cd C:\okforge\quartz
  git checkout v5
  npm ci
  npx quartz plugin install
  ```

  **Do not skip step 5, `npx quartz plugin install`.** Quartz v5's
  community plugins (footer, explorer, …) live in a generated
  `.quartz/plugins/` dir, and without it builds fail with the cryptic
  `Could not resolve "../../.quartz/plugins"` — nothing in that error
  says a plugin step was missed.

  The publish job invokes `node` directly (npx isn't reliably on a
  service's PATH): it uses `node` from PATH, falling back to
  `/usr/bin/node`, or set `OKFORGE_WEBUI_NODE` explicitly.

- **Publish site** button (verify stage) builds the wiki into
  `<sites-dir>/<Subject>/` via a `publish` job; the
  **view published site** link appears once built
  (`/api/kb/<Subject>/site/`).
- Optional per-KB branding in `.okforge/config.yaml`:
  `site_title:` and `site_title_suffix:` (defaults: KB name, empty).
- **Going public** = rsync the sites dir to your internet host (the
  exact command is printed at the end of every publish job log). The
  host needs MultiViews or equivalent for Quartz's extensionless links —
  on Apache that's `Options +MultiViews` on the docroot directory (in a
  conf file; `.htaccess` won't work under `AllowOverride None`).
- Re-publish any time; it's a full rebuild (seconds) and safe while
  ingests run elsewhere in the queue (serial worker).
- **Building without the webui** — the same command the `publish` job
  runs:
  ```bash
  cd /opt/okforge/quartz
  node quartz/bootstrap-cli.mjs build \
    --directory /opt/okforge/kbs/<Subject>/wiki \
    --output /opt/okforge/sites/<Subject>
  ```
  The webui writes `quartz.config.yaml`'s `pageTitle`/`pageTitleSuffix`/
  `baseUrl` automatically from the KB's config before this runs — copy
  `quartz.config.default.yaml` over it and edit those three keys by hand
  if building entirely outside the webui.
- **Local preview before publishing anything**: add `--serve` (and
  `--watch` to rebuild on save) to the command above — serves at
  `http://localhost:8080` with hot reload, no Apache or rsync involved.

## Topic tree (hierarchical concepts, per-KB opt-in)

Flat `concepts/` doesn't scale past a few books. With
`topic_tree: true` in a KB's `.okforge/config.yaml`,
`okforge --kb-dir <kb> reindex` clusters existing concepts into named
topic dirs (`concepts/<topic>/`, each with a `_topic.md` summary node);
later ingests place new concepts by tree descent, and overflowing nodes
split into subtopics. Queries gain a `read_topic` navigation tool.
Reindex ends by retargeting markdown links to moved pages. Off by
default; a bad reindex is one `git reset --hard` away (pre-ingest
snapshots). Moving a concept file to another topic dir by hand is fine
— everything follows the files.

## Ingest cost at collection scale (measured)

One calibration datapoint to set expectations before committing to a
multi-day run: 364 hour-long, news-dense transcript documents (a full
year of a daily show) into one KB with `topic_tree: true`, on a single
llama.cpp host (2 parallel slots, 262k context each) serving a 27B
Q5 model with thinking off. Final wiki: 364 summaries, 812 concepts in
14 topics, 2,123 entities, 40 MB. Total: **~46 h wall time**, ~26 LLM
calls per document, zero failed adds.

- **Per-add cost grows with the wiki, not the document.** 2.8 min/doc
  for the first 30 docs → ~6 min/doc by doc 150 → a **~9 min/doc
  plateau** past doc 200. Budget a long run at the late rate, not the
  early one.
- **The dominant cost is entity-page rewrites, not the planning
  prompt.** A recurring entity's page is rewritten in full on every
  mention, and hot pages grow with the corpus (the top page ended at
  140 KB), so rewrite outputs grow too: median entity update 1.2k
  tokens out, p95 17k. Output tokens are the slow direction on local
  hosts — this is where the 9 min/doc goes. Entity-dense material
  averaged ~6 new entities per document.
- **Prompt growth is real but survivable**: the per-add planning prompt
  embeds every concept and entity brief; it grew ~200 tokens/doc and
  ended at ~91k tokens. Fine inside a 262k slot — but size the serving
  context before the run, and note `wiki/index.md` (the query agent's
  first read) ended at ~425 KB.
- **Prefix caching is what makes this affordable**: 84% of ~312M input
  tokens were cache hits (steady-state adds run 97–99% cached). On a
  hosted API, check the provider's prompt-caching support first —
  uncached, the same run bills over 1M input tokens per document.
- **Reindex cadence**: first `reindex` once ~30 docs are in, again at
  the end. The final one here took 9 minutes: 648 concepts clustered,
  ~9k markdown links retargeted.
- **Interruptions are cheap**: adds hash-skip known files, so a killed
  run resumes by re-running the same loop (the replay over 300 done
  docs takes minutes), and an interrupted add rolls back its journal
  on the next start.

## Troubleshooting

- **Job hangs mid-LLM-call, GPU busy but nothing returns** (or hangs right
  after you cancelled another job): a runaway generation is holding a slot.
  Confirm with `curl -s http://<llm-host>:8080/slots` (`is_processing: true`
  for a dead request). **Fix: force-unload the model** — llama.cpp reloads
  on demand; the running job's retry logic usually recovers by itself.
  Corollary: avoid cancelling LLM-bound jobs unless truly wedged; the cancel
  itself can orphan a generation.
- **Adds suddenly much slower**: check thinking got re-enabled (missing
  `llm_extra_body` block in a new KB) or the model was reloaded with a
  different preset (`curl -s http://<llm-host>:8080/props`).
- **A doc lands with 0 concepts / 0 entities and the add log has no
  "Generating N concept(s)" lines**: the opposite concepts-plan failure
  — a valid-but-empty plan. First-doc adds auto-re-roll once (see
  Ingest lifecycle); if the `[WARN] still 0` line shows and the doc has
  real content, "Re-ingest chunk" (stage 4) rolls again manually.
- **One add takes 5× normal and the log shows hundreds of entities
  planned**: a runaway concepts-plan — the model degenerated into a
  10k+-token plan requesting hundreds of entities (normal: ~10). The
  add still "succeeds", but it writes a pile of vague junk entity pages
  ("progressive-stuff") that bloat every later planning prompt. Repair:
  `okforge remove <doc> --keep-raw` then re-add the same file — remove
  deletes every page whose *only* source was that doc, merely drops the
  doc from co-sourced pages, and lint-fixes dangling wikilinks; the
  re-add compiles a fresh (normally sane) plan. Preview with
  `--dry-run` first; watching per-doc wall time in the ingest log
  catches runaways early.
- **A hung LLM call outlives its timeout**: the client timeout only
  bounds a *responsive* server. A wedged llama.cpp slot that stops
  responding mid-request holds the call until TCP gives up (~30 min
  observed, vs a 600 s configured timeout). Same fix as the first
  bullet: force-unload the model. Entity generations lost this way are
  logged as a WARN and the add completes without them.
- **Backend up, UI stale**: hard-refresh. On Apache deployments also
  remember static files are served from the docroot, not the repo — an
  un-rsynced frontend change never reaches the browser (standalone mode
  serves straight from the repo).

## Updating an instance

- **Frontend-only** (files under `webui/static/`): safe mid-job. Apache
  deployments copy to the docroot
  (`sudo rsync -a --delete webui/static/ /var/www/okforge-webui/`);
  standalone deployments serve straight from the repo — a `git pull` is
  the whole update.
- **Backend / engine**: wait for an idle queue, then `git pull`,
  `pip install -r requirements.txt` into the instance's venv, and
  `sudo systemctl restart okforge-webui` — **never restart while an
  ingest job is running**.
- **Engine and OCR-tool upgrades** are pinned versions: bump the pin in
  `requirements.txt`, then `pip install -r requirements.txt` on each
  instance.
- Full redeploy (vhost/unit changes): rerun `webui/deploy.sh` with your
  overrides (see script header) — it restarts the backend, same
  idle-queue rule applies.
