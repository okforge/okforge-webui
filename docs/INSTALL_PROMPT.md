# Prompt for an agent-assisted install

Paste the block below into a coding agent (Claude Code, or any agent
that can read a repo and run shell commands) to have it walk you
through installing and configuring the suite on a fresh machine.

It deliberately does **not** restate the install steps — it points the
agent at `README.md` and `docs/OPERATIONS.md` so this file cannot drift
out of sync with them. The only things inlined are failure modes seen
in real installs that the referenced docs don't make obvious: the
LiteLLM provider prefix on the model name, verifying an ingest by its
output rather than its job status, environment-variable persistence,
and the Quartz publish traps.

---

```text
You are helping me install and configure okforge — a local-first
pipeline that turns scanned PDFs into a citation-backed Markdown wiki
I can browse, publish as a website, and query from an AI client.

Start here: https://github.com/okforge/okforge-webui

Read that repo's README.md (Prerequisites, Directory layout, Install,
Run it, First run, Configuration) and docs/OPERATIONS.md before
proposing any commands. Those are authoritative — prefer them over
anything you assume about the project.

Work with me interactively, one step at a time. Before each step, tell
me what it will do and wait for me to confirm. Run commands yourself
where you can; when something needs my machine or my credentials, give
me the exact command to paste and ask me for the output.

First ask me these, and don't guess:
- OS and shell (Linux, macOS, Windows/PowerShell, or WSL)
- the base directory to install into (see README "Directory layout")
- my OpenAI-compatible LLM endpoint URL and model name, and whether
  that model is vision-capable — required for the scanned-PDF OCR path
- whether this machine has outbound internet or is LAN-only / offline

On the model name, get this right or ingestion fails silently: the
value I configure must carry a **LiteLLM provider prefix even for a
local endpoint** — `openai/Qwen3.6-27B-MTP`, not `Qwen3.6-27B-MTP`.
One configured value feeds two consumers. The OCR path strips the
prefix before calling the endpoint, so OCR works either way; the
engine (`add`, `query`, `describe`) passes the string to LiteLLM,
which cannot route it without a provider and errors out. Configure it
without the prefix and you get working OCR and a knowledge base that
never gains a single concept. If my endpoint is llama.cpp, vLLM, or
anything else OpenAI-compatible, the prefix is still `openai/`.

Then work through these, checking with me at each boundary:
1. Prerequisites, clone, virtualenv, dependencies.
2. Configuration — set only the OKFORGE_WEBUI_* variables I actually
   need, and show me the current default before changing anything.
   Tell me whether each variable is set for this shell session only or
   persisted, and give me the persistence form for my OS: a shell
   profile on Linux/macOS/WSL, `setx` on Windows/PowerShell, or a
   systemd unit / drop-in if I am running the backend as a service.
   A session-only export that vanishes on reboot is a common cause of
   "it worked yesterday".
3. Start the backend and confirm the web UI loads.
4. Walk me through the README's "First run — start with a small test"
   on one short PDF, end to end. Don't skip this — a failure on a
   3-page test is far cheaper to diagnose than one on a 400-page scan.
   **A job queue showing "done" is not proof the ingest worked.** When
   the run finishes, check the KB on disk before we move on: confirm
   `wiki/concepts/` or `wiki/entities/` actually gained files and that
   `.okforge/hashes.json` lists the document. If those are empty while
   the queue says done, read the job log — an LLM call that fails
   inside LiteLLM can leave the pipeline reporting success with an
   empty knowledge base, and the model-prefix mistake above is the
   usual cause.
5. Quartz static-site publishing. Do not silently skip this step:
   ask me now whether I want it (it is optional, but it is how a
   finished knowledge base becomes a browsable website). If I say
   yes, follow the "Publishing a KB as a website (Quartz)" section of
   docs/OPERATIONS.md. Traps that cause most first-publish failures:
   - `npx quartz plugin install` is a required step and is easy to
     miss (it is `plugin install` — there is no `plugin create`).
   - That command installs from a lockfile. On a fresh checkout with
     no `quartz.lock.json` it fails; copy `quartz.config.default.yaml`
     to `quartz.config.yaml` first and run
     `npx quartz plugin install --from-config`. If the flags differ
     from what is written here, trust `npx quartz plugin install
     --help` over this document.
   - On an offline or LAN-only box the og-image emitter fetches a font
     over the network at build time and must be disabled.
   Quartz is pinned here to a branch, not a tag, so its config layout
   and plugin system move: configuration now lives in
   `quartz.config.yaml` / `quartz.config.default.yaml` rather than a
   `quartz.config.ts`, and plugins are npm specifiers rather than
   git clones. Read the checkout you actually have before following
   any Quartz instruction in this file. Finish by publishing the
   small-test KB from step 4 and opening the built site.

If a step fails, check the Troubleshooting section of
docs/OPERATIONS.md before improvising a fix.
```
