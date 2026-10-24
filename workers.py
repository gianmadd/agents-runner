"""`claude --workers` / `codex --workers`: pick this session's worker table on a local page, then start the CLI with it.

Usage (from the shell functions in workers.sh): python3 workers.py <claude|codex> [cli args…]
"""
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import datetime
import zoneinfo
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
_E5 = ["low", "medium", "high", "xhigh", "max"]
# Worker models per provider, the first is the default, each with the efforts its CLI accepts (codex: ~/.codex/models_cache.json).
MODELS = {
    "claude": {"sonnet": _E5, "opus": _E5, "haiku": _E5},
    "codex": {"gpt-6-luna": _E5, "gpt-6.1-sol": _E5 + ["ultra"], "gpt-5.6-terra": _E5 + ["ultra"]},
}
OTHER = {"claude": "codex", "codex": "claude"}
ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
DEFAULT_DIR = {"claude": "~/.claude", "codex": "~/.codex"}
DEFAULT_TABLE = {
    "roles": [
        {"role": "implementer", "runs": "native", "effort": "medium"},
        {"role": "reviewer", "runs": "external", "effort": "medium"},
        {"role": "prover", "runs": "external", "effort": "medium"},
        {"role": "explorer", "runs": "external", "effort": "low"},
    ],
    "account": None,
}
RC_FILES = ["~/.zshrc", "~/.zprofile", "~/.bashrc", "~/.bash_profile", "~/.profile"]


# --- Accounts: one config folder per subscription ---

def expand(folder):
    return os.path.expanduser(os.path.expandvars(folder))

def alias_dirs(provider, rc_text):
    """(name, folder) for each folder that shell aliases/exports point the provider's env var at; name is the alias."""
    found = []
    for m in re.finditer(r"(?:alias\s+([\w.-]+)=['\"]?)?" + ENV[provider] + r"=['\"]?([^\s'\";]+)", rc_text):
        found.append((m.group(1) or os.path.basename(m.group(2)).lstrip("."), m.group(2)))
    return found


def logged_in(provider, folder):
    """folder None = the default account (env var unset)."""
    if provider == "codex":
        return os.path.exists(os.path.join(expand(folder or DEFAULT_DIR["codex"]), "auth.json"))
    path = expand(folder) if folder else None
    if path and os.path.exists(os.path.join(path, ".credentials.json")):
        return True
    if sys.platform != "darwin":
        return os.path.exists(os.path.join(os.path.expanduser(path or DEFAULT_DIR["claude"]), ".credentials.json"))
    # Keychain item per folder: "Claude Code-credentials" + "-" + sha256(folder)[:8] when the env var is set.
    import hashlib, unicodedata
    service = "Claude Code-credentials"
    if path:
        service += "-" + hashlib.sha256(unicodedata.normalize("NFC", path).encode()).hexdigest()[:8]
    return subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True).returncode == 0


def accounts(provider):
    """[{"folder": None (default) or folder, "name": alias}, …] with a login. The default folder named explicitly is the default account."""
    rc_text = ""
    for rc in RC_FILES:
        try:
            with open(os.path.expanduser(rc)) as f:
                rc_text += f.read() + "\n"
        except OSError:
            pass
    default = os.path.realpath(expand(DEFAULT_DIR[provider]))
    found = [{"folder": None, "name": provider}]
    for name, folder in alias_dirs(provider, rc_text):
        if os.path.realpath(expand(folder)) != default and folder not in [a["folder"] for a in found]:
            found.append({"folder": folder, "name": name})
    return [a for a in found if logged_in(provider, a["folder"])]


def session_account(provider, accs):
    """The account this launch runs on, from the env var the alias set."""
    current = os.environ.get(ENV[provider])
    for a in accs:
        if a["folder"] and current and os.path.realpath(expand(a["folder"])) == os.path.realpath(expand(current)):
            return a
    return {"folder": current, "name": os.path.basename(current).lstrip(".") if current else provider}


def env_for(provider, account):
    env = dict(os.environ)
    env.pop(ENV[provider], None)
    if account:
        env[ENV[provider]] = expand(account)
    return env


# --- Quotas: zero model tokens ---

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_reset(text, now):
    """'Oct 24 at 7pm (Europe/Rome)' or '7:30pm (…)' → epoch seconds. No year in the text: a date well past is next year's."""
    m = re.search(r"(?:(\w{3}) (\d+) at )?(\d+)(?::(\d+))?(am|pm)(?: \(([^)]+)\))?", text or "")
    if not m or (m.group(1) and m.group(1) not in MONTHS):
        return None
    try:
        tz = zoneinfo.ZoneInfo(m.group(6)) if m.group(6) else None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = None
    local = datetime.datetime.fromtimestamp(now, tz)
    hour = int(m.group(3)) % 12 + (12 if m.group(5) == "pm" else 0)
    month, day = (MONTHS.index(m.group(1)) + 1, int(m.group(2))) if m.group(1) else (local.month, local.day)
    when = local.replace(month=month, day=day, hour=hour, minute=int(m.group(4) or 0), second=0, microsecond=0)
    if when.timestamp() < now - (86400 if m.group(1) else 0):
        when = when.replace(year=when.year + 1) if m.group(1) else when + datetime.timedelta(days=1)
    return when.timestamp()


def window_label(text):
    """'Current session' → 5h, 'Current week (all models)' → 7d, 'Current week (Fable)' → 7d Fable."""
    if text == "Current session":
        return "5h"
    model = re.search(r"\((.+)\)", text)
    return "7d" + (f" {model.group(1)}" if model and model.group(1) != "all models" else "")


def parse_claude_usage(text, now=None):
    """'Current session: 7% used · resets Oct 24 at 7pm (Europe/Rome)' → windows. Undocumented text: may break on CLI updates."""
    now = now or time.time()
    windows = []
    for m in re.finditer(r"^(Current [^:\n]+): (\d+(?:\.\d+)?)% used(?: · resets ([^\n]+))?", text, re.M):
        windows.append({"label": window_label(m.group(1)), "used": float(m.group(2)), "resets": parse_reset(m.group(3), now)})
    return windows


def parse_codex_limits(result):
    limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
    names = {300: "5h", 10080: "7d"}
    windows = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if w:
            windows.append({"label": names.get(w.get("windowDurationMins"), key), "used": w.get("usedPercent"), "resets": w.get("resetsAt")})
    return windows


def read_quota(provider, account):
    env = env_for(provider, account)
    if provider == "claude":
        out = subprocess.run(["claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence"],
                             env=env, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=30)
        return parse_claude_usage(json.loads(out.stdout).get("result", ""))
    handshake = "\n".join(json.dumps(m) for m in [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "workers", "version": "0"}}},
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read"},
    ]) + "\n"
    proc = subprocess.Popen(["codex", "app-server"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(15, proc.kill)
    timer.start()
    try:
        proc.stdin.write(handshake)
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                if "result" not in msg:
                    raise RuntimeError(msg.get("error", "no result"))
                return parse_codex_limits(msg["result"])
        raise RuntimeError("app-server closed without an answer")
    finally:
        timer.cancel()
        proc.kill()


# --- The table: saved per repo, injected per session ---

def table_file():
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    return os.path.join(out.stdout.strip(), "agents-workers.json") if out.returncode == 0 else None


def load_saved(path):
    """{provider: last confirmed table}: models differ per provider, so each keeps its own."""
    try:
        with open(path) as f:
            saved = json.load(f)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_table(saved, provider):
    """The provider's last confirmed table, else the default; rows without a model get the default model."""
    table = saved.get(provider) or json.loads(json.dumps(DEFAULT_TABLE))
    for row in table.get("roles", []):
        row.setdefault("model", next(iter(MODELS[provider if row.get("runs") == "native" else OTHER[provider]])))
    return table


def validate(table, provider, external_accounts):
    """The page is a trust boundary: only known values reach the shell recipe."""
    roles = []
    for row in table.get("roles", []):
        role = str(row.get("role", "")).strip()
        runs = row.get("runs")
        models = MODELS[provider if runs == "native" else OTHER[provider]] if runs in ("native", "external") else {}
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", role) or row.get("model") not in models or row.get("effort") not in models[row["model"]]:
            raise ValueError(f"bad row: {row}")
        roles.append({"role": role, "runs": runs, "model": row["model"], "effort": row["effort"]})
    # The implementer edits code under the orchestrator's eyes and is every unlisted role's fallback: always native, always there.
    if [r["runs"] for r in roles if r["role"] == "implementer"] != ["native"]:
        raise ValueError("implementer must be listed once, as native")
    folders = [a["folder"] for a in external_accounts]
    account = table.get("account")
    if account not in folders:
        account = folders[0] if folders else None
    return {"roles": roles, "account": account}


def instructions(provider, table, names):
    """The text injected into the session: the table and how to run an external worker. names: native/external → account name."""
    other = OTHER[provider]
    folder = ".scratch/workers/<role>-<slug>-<HHMMSS>"
    prefix = f"{ENV[other]}={table['account']} " if table["account"] else ""
    if other == "codex":
        recipe = (f"{prefix}codex exec --dangerously-bypass-approvals-and-sandbox -m <model> "
                  f"-c model_reasoning_effort=<effort> --ephemeral -o {folder}/report.md - "
                  f"< {folder}/prompt.md > {folder}/run.log 2>&1")
        wait = "run it with Bash in the background; you are notified when it exits"
    else:
        recipe = (f"{prefix}claude -p --model <model> --effort <effort> --dangerously-skip-permissions "
                  f"--no-session-persistence < {folder}/prompt.md > {folder}/report.md 2> {folder}/run.log")
        wait = "start it as a background terminal, then poll it with an empty `write_stdin` (long waits, up to 300 s) until it exits"
    rows = "\n".join(
        f"| {r['role']} | {r['runs']} ({names[r['runs']]}) | "
        f"{r['model']} | {r['effort']} |"
        for r in table["roles"])
    return f"""## Workers

This session's worker table, chosen at launch. When a skill spawns a worker (implementer, reviewer, prover, explorer…), look up its role; a role not listed is `implementer`.

| Role | Runs | Model | Effort |
|------|------|-------|--------|
{rows}

- **native**: spawn your own subagent with that model and effort.
- **external**: a one-shot headless run on {other}, which only returns a report. For each one:
  1. Create `{folder}/` (paths relative to the repo root, absolute in the command) and write the whole task to `prompt.md` there, ending with the repo's git rules: never push, never touch the main branch.
  2. From the folder the worker must work in (the ticket's worktree, if any), {wait}:
     `{recipe}`
     with `<model>` and `<effort>` from the role's row.
  3. It succeeded when the exit code is 0 and `report.md` is not empty: that file is the worker's report. Otherwise read `run.log`, tell the user in one line why it failed, and run the role as a native subagent instead.
  Several external workers can run at once: start them together, then wait for each.
"""


# --- The page: a one-shot local server ---

def pick_table(provider, saved):
    other = OTHER[provider]
    token = secrets.token_urlsafe(16)
    all_accounts = {p: accounts(p) for p in ("claude", "codex")}
    session = session_account(provider, all_accounts[provider])
    state = {"provider": provider, "other": other, "repo": os.path.basename(os.getcwd()), 
             "models": MODELS, "accounts": all_accounts, "session": session, "table": saved}
    chosen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, kind="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == f"/{token}/":
                with open(os.path.join(HERE, "page.html")) as f:
                    page = f.read().replace("__STATE__", json.dumps(state).replace("</", "<\\/"))
                self.reply(200, page, "text/html; charset=utf-8")
            elif self.path.startswith(f"/{token}/quota/"):
                p, _, i = self.path[len(f"/{token}/quota/"):].partition("/")
                try:
                    account = all_accounts[p][int(i)]
                    self.reply(200, json.dumps({"windows": read_quota(p, account["folder"])}))
                except Exception as e:  # any failure shows as n/a on the page
                    self.reply(200, json.dumps({"error": str(e)[:200]}))
            else:
                self.reply(404, "{}")

        def do_POST(self):
            if self.path != f"/{token}/confirm":
                return self.reply(404, "{}")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                chosen["table"] = validate(body, provider, all_accounts[other])
            except (ValueError, AttributeError) as e:
                return self.reply(400, json.dumps({"error": str(e)}))
            self.reply(200, "{}")
            threading.Thread(target=server.shutdown).start()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{token}/"
    print(f"Confirm the worker table in the browser: {url}", file=sys.stderr)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(130)
    table = chosen["table"]
    external = next((a["name"] for a in all_accounts[other] if a["folder"] == table["account"]), other)
    return table, {"native": session["name"], "external": external}


def main():
    provider, args = sys.argv[1], [a for a in sys.argv[2:] if a != "--workers"]
    path = table_file()
    saved = load_saved(path) if path else {}
    table, names = pick_table(provider, load_table(saved, provider))
    if path:
        with open(path, "w") as f:
            json.dump({**saved, provider: table}, f, indent=2)
    text = instructions(provider, table, names)
    if provider == "claude":
        argv = ["claude", *args, "--append-system-prompt", text]
    else:
        argv = ["codex", "-c", "developer_instructions=" + json.dumps(text), *args]  # a JSON string is a valid TOML string
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
