"""`fns`: pick on a local page which CLI to start (claude, claude-work, codex…) and its worker table, then start it here.

Usage (from the shell function in workers.sh): python3 workers.py
"""
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import datetime
import zoneinfo
import webbrowser
try:
    from compression import zstd  # Python 3.14+: Codex compresses older rollouts to .jsonl.zst
except ImportError:
    zstd = None
ZSTD_ERROR = (zstd.ZstdError,) if zstd else ()

HERE = os.path.dirname(os.path.abspath(__file__))
_E5 = ["low", "medium", "high", "xhigh", "max"]
# Worker models per provider, weakest first, each with the efforts its CLI accepts (codex: ~/.codex/models_cache.json).
MODELS = {
    "claude": {"haiku": _E5, "sonnet": _E5, "opus": _E5},
    "codex": {"gpt-6-luna": _E5, "gpt-5.6-terra": _E5 + ["ultra"], "gpt-6.1-sol": _E5 + ["ultra"]},
}
DEFAULT_MODEL = {"claude": "sonnet", "codex": "gpt-6-luna"}
# The account external runs use until a table is saved, by alias name; falls back to the first account.
DEFAULT_EXTERNAL = {"claude": "claude-work"}
# Orchestrator models, weakest first; LEAD_DEFAULT is preselected (opus as the Setup's Subagents entry, sol as Codex's config).
LEADS = {
    "claude": {"haiku": _E5, "sonnet": _E5, "opus": _E5, "fable": _E5},
    "codex": {"gpt-6-luna": _E5, "gpt-5.6-terra": _E5 + ["ultra"], "gpt-6.1-sol": _E5 + ["ultra"]},
}
LEAD_DEFAULT = {"claude": {"model": "opus", "effort": "medium"}, "codex": {"model": "gpt-6.1-sol", "effort": "medium"}}
OTHER = {"claude": "codex", "codex": "claude"}
ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
DEFAULT_DIR = {"claude": "~/.claude", "codex": "~/.codex"}
DEFAULT_TABLE = {
    "roles": [
        {"role": "implementer", "runs": "native", "effort": "medium"},
        {"role": "reviewer", "runs": "native", "effort": "medium"},
        {"role": "prover", "runs": "native", "effort": "medium"},
        {"role": "explorer", "runs": "native", "effort": "low"},
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
    import unicodedata
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


# --- Recent sessions of this folder, to resume (undocumented session files: may change with the CLIs) ---

def first_text(content):
    """A user message's text without the command tags Claude wraps slash commands in."""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return re.sub(r"<[^>]+>", " ", str(content)).split()


def claude_sessions(folder, cwd, limit):
    project = os.path.join(expand(folder or DEFAULT_DIR["claude"]), "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
    try:
        files = sorted((os.path.join(project, f) for f in os.listdir(project) if f.endswith(".jsonl")), key=os.path.getmtime, reverse=True)
    except OSError:
        return []
    found = []
    for path in files:
        title, first, interactive = None, None, False
        with open(path, errors="replace") as f:
            for line in f:
                if '"ai-title"' in line or '"custom-title"' in line:
                    d = json.loads(line)
                    title = d.get("customTitle") or d.get("aiTitle") or title
                elif first is None and '"type":"user"' in line:
                    d = json.loads(line)
                    interactive = d.get("entrypoint") == "cli"
                    if not interactive:
                        break
                    if not d.get("isMeta"):
                        first = " ".join(first_text(d.get("message", {}).get("content", "")))[:100] or None
        if interactive:
            found.append({"id": os.path.basename(path)[:-6], "title": title or first or "(no title)", "time": os.path.getmtime(path)})
            if len(found) == limit:
                break
    return found


def codex_sessions(folder, cwd, limit):
    home = expand(folder or DEFAULT_DIR["codex"])
    names = {}
    try:
        with open(os.path.join(home, "session_index.jsonl")) as f:
            for line in f:
                d = json.loads(line)
                names[d.get("id")] = d.get("thread_name")
    except (OSError, ValueError):
        pass
    found = []
    for root, dirs, files in os.walk(os.path.join(home, "sessions"), topdown=True):
        dirs.sort(reverse=True)  # YYYY/MM/DD: newest first
        for name in sorted(files, reverse=True):
            if not name.startswith("rollout-") or (name.endswith(".zst") and not zstd):
                continue
            path = os.path.join(root, name)
            try:
                with (zstd.open(path, "rt", errors="replace") if name.endswith(".zst") else open(path, errors="replace")) as f:
                    meta = json.loads(f.readline() or "{}").get("payload", {})
                    # Subagents carry a dict as source, headless runs "exec": only sessions you typed in.
                    if meta.get("cwd") != cwd or meta.get("source") not in ("cli", "vscode"):
                        continue
                    first = None
                    for line in f:
                        if '"user_message"' in line:
                            first = " ".join(first_text(json.loads(line)["payload"].get("message", "")))[:100]
                            break
            except (OSError, ValueError, KeyError, *ZSTD_ERROR):
                continue  # one unreadable rollout must not hide the others
            found.append({"id": meta["id"], "title": names.get(meta["id"]) or first or "(no title)", "time": os.path.getmtime(path)})
            if len(found) == limit:
                return found
    return found


def recent_sessions(all_accounts, cwd, limit=10):
    """The last sessions started in cwd, per account, newest first."""
    found = []
    for p, read in (("claude", claude_sessions), ("codex", codex_sessions)):
        for i, a in enumerate(all_accounts[p]):
            try:
                found += [{**x, "provider": p, "account": i} for x in read(a["folder"], cwd, limit)]
            except (OSError, ValueError, KeyError):
                pass  # an unreadable store just lists nothing
    return sorted(found, key=lambda x: -x["time"])


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
    """A lead account's last confirmed table, else the default (every worker native); rows without a model get the default."""
    table = saved or json.loads(json.dumps(DEFAULT_TABLE))
    for row in table.get("roles", []):
        row.setdefault("model", DEFAULT_MODEL[provider if row.get("runs") == "native" else OTHER[provider]])
    return table


def native_only(table, provider):
    """The other CLI has no account: its rows run native, on the default model, keeping the effort when it exists there."""
    model = DEFAULT_MODEL[provider]
    for row in table["roles"]:
        if row["runs"] == "external":
            row.update(runs="native", model=model, effort=row["effort"] if row["effort"] in MODELS[provider][model] else "medium")
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
    if not external_accounts and any(r["runs"] == "external" for r in roles):
        raise ValueError(f"no {OTHER[provider]} account to run external workers")
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


# --- The page: a one-shot local server, shown in a native window ---

def window_app():
    """The native window (window.swift), compiled once per source version; None without swiftc: the browser then shows the page."""
    src = os.path.join(HERE, "window.swift")
    with open(src, "rb") as f:
        out = os.path.join(os.path.expanduser("~/.cache/fns"), hashlib.sha256(f.read()).hexdigest()[:12], "fns")  # named fns in the menu bar
    if os.path.exists(out):
        return out
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return None
    print("Building the fns window (once)…", file=sys.stderr)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    built = subprocess.run(["swiftc", "-O", src, "-o", out + ".tmp"], capture_output=True, text=True)
    if built.returncode:
        print(built.stderr[-500:], file=sys.stderr)
        return None
    os.replace(out + ".tmp", out)
    return out


def validate_lead(body, all_accounts, sessions=()):
    """Which CLI and account to start, with its model and effort; resume only a session the server listed."""
    provider, index = body.get("provider"), body.get("account")
    if provider not in LEADS or not isinstance(index, int) or not 0 <= index < len(all_accounts[provider]):
        raise ValueError("bad session account")
    model, effort = body.get("model"), body.get("effort")
    if model not in LEADS[provider] or effort not in LEADS[provider][model]:
        raise ValueError("bad session model")
    resume = body.get("resume")
    if resume is not None and not any(x["id"] == resume and x["provider"] == provider and x["account"] == index for x in sessions):
        raise ValueError("unknown session")
    return {"provider": provider, "folder": all_accounts[provider][index]["folder"], "model": model, "effort": effort,
            "resume": resume, "update": bool(body.get("update")) if resume else True}


def pick(saved):
    token = secrets.token_urlsafe(16)
    all_accounts = {p: accounts(p) for p in ("claude", "codex")}
    if not any(all_accounts.values()):
        sys.exit("fns: no Claude Code or Codex login found")
    state = {"repo": os.path.basename(os.getcwd()), "models": MODELS, "defaultModel": DEFAULT_MODEL, "leads": LEADS, "leadDefault": LEAD_DEFAULT, "accounts": all_accounts,
             "tables": {}, "lead": saved.get("lead") or {}}
    # One table per lead account (by alias name): claude and claude-work can route differently.
    for p in ("claude", "codex"):
        for a in all_accounts[p]:
            mine = (saved.get("tables") or {}).get(a["name"])
            t = load_table(mine, p)
            if not mine:
                t["account"] = next((x["folder"] for x in all_accounts[OTHER[p]] if x["name"] == DEFAULT_EXTERNAL.get(OTHER[p])), t["account"])
            if not all_accounts[OTHER[p]]:
                native_only(t, p)
            state["tables"][a["name"]] = t
    chosen = {}
    sessions = []
    reader = threading.Thread(target=lambda: sessions.extend(recent_sessions(all_accounts, os.getcwd())))
    reader.start()

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
            elif self.path == f"/{token}/sessions":
                reader.join()
                self.reply(200, json.dumps(sessions))
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
                reader.join()
                lead = validate_lead(body.get("lead", {}), all_accounts, sessions)
                chosen.update(lead=lead, table=validate(body, lead["provider"], all_accounts[OTHER[lead["provider"]]]))
            except (ValueError, AttributeError) as e:
                return self.reply(400, json.dumps({"error": str(e)}))
            self.reply(200, "{}")
            threading.Thread(target=server.shutdown).start()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{token}/"
    print(f"Pick the session in the window ({url})", file=sys.stderr)
    app = window_app()
    viewer = subprocess.Popen([app, url]) if app else None
    if viewer:  # closing the window without Start cancels
        threading.Thread(target=lambda: (viewer.wait(), server.shutdown()), daemon=True).start()
    else:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        chosen.clear()
    if viewer and viewer.poll() is None:
        viewer.terminate()
    if "table" not in chosen:
        sys.exit(130)
    lead, table = chosen["lead"], chosen["table"]
    name = lambda p, folder: next((a["name"] for a in all_accounts[p] if a["folder"] == folder), p)
    names = {"native": name(lead["provider"], lead["folder"]), "external": name(OTHER[lead["provider"]], table["account"])}
    return lead, table, names


def claude_model(model, folder):
    """The account's own variant of the picked model, e.g. "opus" → "opus[1m]" when its settings say so."""
    try:
        with open(os.path.join(expand(folder or DEFAULT_DIR["claude"]), "settings.json")) as f:
            own = json.load(f).get("model") or ""
    except (OSError, ValueError, AttributeError):
        return model
    return own if re.fullmatch(re.escape(model) + r"\[\w+\]", own) else model


def launch_argv(lead, text):
    """New session: the table goes in as instructions. Resume: kept as it was, or, with update, Claude re-renders its
    system prompt for this launch only (the recorded one returns on a later plain resume) and Codex, which keeps its
    first instructions, gets the table as the first message."""
    p, flags, resume = lead["provider"], [], lead.get("resume")
    if p == "claude":
        flags += ["--model", claude_model(lead["model"], lead.get("folder")), "--effort", lead["effort"]]
        if not resume:
            return ["claude", *flags, "--append-system-prompt", text]
        update = ["--system-prompt-snapshot", "off", "--append-system-prompt", text] if lead["update"] else []
        return ["claude", "--resume", resume, *flags, *update]
    flags += ["-m", lead["model"], "-c", f"model_reasoning_effort={lead['effort']}"]
    if not resume:
        return ["codex", *flags, "-c", "developer_instructions=" + json.dumps(text)]  # a JSON string is a valid TOML string
    update = ["The worker table of this session changed. From now on follow this one, not the one in your instructions.\n\n" + text] if lead["update"] else []
    return ["codex", "resume", *flags, resume, *update]


def main():
    path = table_file()
    saved = load_saved(path) if path else {}
    lead, table, names = pick(saved)
    if path:
        last = {k: lead[k] for k in ("provider", "folder", "model", "effort")}
        with open(path, "w") as f:
            tables = {**(saved.get("tables") or {}), **({names["native"]: table} if lead["update"] else {})}
            json.dump({"lead": last, "tables": tables}, f, indent=2)
    os.environ.pop(ENV[lead["provider"]], None)  # the default account needs the env var unset
    if lead["folder"]:
        os.environ[ENV[lead["provider"]]] = expand(lead["folder"])
    argv = launch_argv(lead, instructions(lead["provider"], table, names))
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
