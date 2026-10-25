# agents-runner

`fns`: start a Claude Code or Codex session after picking, on one page, which account runs it and which account runs each worker the skills spawn.

## Install

Needs `python3`, `claude` and `codex`; on macOS, `swiftc` for the native window (without it the page opens in the browser).

```sh
git clone https://github.com/gianmadd/agents-runner ~/.agents-runner
echo 'source ~/.agents-runner/workers.sh' >> ~/.zshrc   # or ~/.bashrc
```

Open a new terminal, then run `fns` in any repo. To update: `git -C ~/.agents-runner pull`.

[agents-repo-setup](https://github.com/gianmadd/agents-repo-setup) offers this install when a repo uses both Claude Code and Codex.

## Workers across subscriptions

`fns` opens a small window (built on first use with `swiftc` on macOS, else a browser page) and then starts a session in the current folder. There you pick which CLI and account to start (`claude`, `claude-work`, `codex`…) with its model and effort, see each subscription's remaining quota, read without spending tokens, and set the worker table: for each role the skills spawn (`implementer`, `reviewer`, `prover`, `explorer`, or one you add), whether the session's own subagents run it (native) or a one-shot headless run on the other CLI (external), with its model (Sonnet, Opus, Haiku; Luna, Sol, Terra) and effort. `implementer` is always native: the orchestrator works with it, and roles not listed fall back to it. Or click one of the folder's recent sessions, from every account, to resume it, keeping its workers or updating them. Press Start, and the session starts with that table. The page opens on `claude`, with the last model and effort you picked for it in that repo. To skip the page, type an account name as soon as it opens (`work` for `claude-work`) and press Enter: a new session starts there with its saved table. Started the usual way, every worker is native, as before. Why it works this way: [ADR 0001](docs/adr/0001-workers-chosen-per-session.md).

External workers run with permissions bypassed and return only a report, in `.scratch/workers/`: meant for the roles that only read or write in a throwaway copy (reviewer, prover, explorer).

The skills that read the table are in [gianmadd/agentic-dev-kit](https://github.com/gianmadd/agentic-dev-kit) (`implement-loop`); the table injected into the session explains itself, so they need nothing else from here.

## Files

- [`workers.sh`](workers.sh): the `fns` shell function, sourced from the rc file
- [`workers.py`](workers.py): the one-shot local server: quotas, recent sessions, the saved tables, and the launch
- [`page.html`](page.html): the page
- [`window.swift`](window.swift): the native window around the page
- [`test_workers.py`](test_workers.py): `python3 -m unittest`
- [`GLOSSARY.md`](GLOSSARY.md): project vocabulary
