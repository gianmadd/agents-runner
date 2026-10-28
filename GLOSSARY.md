# Agents Runner

Starts a coding-agent session after choosing which subscription runs it and which runs each of its workers.

## Language

**Worker**:
A subagent a skill spawns to do part of its work (implement, review, prove, explore), as opposed to the orchestrator, the session the user talks to.
_Avoid_: delegate, helper

**Worker table**:
The per-session map from each worker role to native or external, with its model and effort; chosen on the launcher's page and injected into that session only. `implementer` is native, or external where Codex runs as a subagent, and a role not listed runs as `implementer`.
_Avoid_: role table, routing table

**Native**:
A worker run as the session's own subagent, on the session's account.

**External**:
A worker run on the other provider: a one-shot headless process (prompt in, report out, no resume), or, from a Claude account with the codex plugin, a `codex:<family>` subagent.
_Avoid_: delegate, headless agent

**Launcher**:
The `fns` command: a local page to pick which CLI and account to start, with the quotas and the Worker table, then it starts that session in the current folder.
_Avoid_: wrapper, router
