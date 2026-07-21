# Prompt for an agent-assisted install

Paste the block below into a coding agent (Claude Code, or any agent
that can read a repo and run shell commands) to have it walk you
through installing and configuring the suite on a fresh machine.

It deliberately does **not** restate the install steps — it points the
agent at `README.md` and `docs/OPERATIONS.md` so this file cannot drift
out of sync with them. The only things inlined are the two failure
modes that account for most first-publish support questions.

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
- whether I want the optional Quartz static-site publishing
- whether this machine has outbound internet or is LAN-only / offline

Then work through these, checking with me at each boundary:
1. Prerequisites, clone, virtualenv, dependencies.
2. Configuration — set only the OKFORGE_WEBUI_* variables I actually
   need, and show me the current default before changing anything.
3. Start the backend and confirm the web UI loads.
4. Walk me through the README's "First run — start with a small test"
   on one short PDF, end to end. Don't skip this — a failure on a
   3-page test is far cheaper to diagnose than one on a 400-page scan.
5. Quartz, only if I said yes — follow the "Publishing a KB as a
   website (Quartz)" section of docs/OPERATIONS.md. Two traps cause
   most first-publish failures: `npx quartz plugin install` is a
   required step and is easy to miss (it is `plugin install` — there
   is no `plugin create`), and on an offline or LAN-only box the
   og-image emitter in the default quartz.config.ts fetches a font
   over the network at build time and must be removed.

If a step fails, check the Troubleshooting section of
docs/OPERATIONS.md before improvising a fix.
```
