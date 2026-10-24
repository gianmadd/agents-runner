# `claude --workers` / `codex --workers`: pick the session's worker table on a local page first.
# Source it from the shell's rc file. Aliases like `claude-work` keep working: they call these functions.
if [ -n "$ZSH_VERSION" ]; then
  _workers_dir=${${(%):-%x}:A:h}
else
  _workers_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi

_workers_wanted() { for _a in "$@"; do [ "$_a" = --workers ] && return 0; done; return 1; }

claude() { if _workers_wanted "$@"; then python3 "$_workers_dir/workers.py" claude "$@"; else command claude "$@"; fi; }
codex() { if _workers_wanted "$@"; then python3 "$_workers_dir/workers.py" codex "$@"; else command codex "$@"; fi; }
