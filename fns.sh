# `fns`: pick on a local page which CLI to start (claude, claude-work, codex…) and its worker table, then start it here.
# Source it from the shell's rc file.
if [ -n "$ZSH_VERSION" ]; then
  _fns_dir=${${(%):-%x}:A:h}
else
  _fns_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi

fns() { python3 "$_fns_dir/src/workers.py"; }
