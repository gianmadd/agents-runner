"""python3 -m unittest discover -s skills/agents-setup/workers"""
import datetime
import json
import os
import tempfile
import unittest
import zoneinfo

import workers


class Parsing(unittest.TestCase):
    def test_claude_usage_text(self):
        now = datetime.datetime(2026, 10, 24, 15, 0, tzinfo=ROME).timestamp()
        text = ("Current session: 7% used · resets Oct 24 at 7pm (Europe/Rome)\n"
                "Current week (all models): 81% used · resets Oct 27 at 9:30am (Europe/Rome)\n"
                "Current week (Fable): 0% used\n")
        self.assertEqual(workers.parse_claude_usage(text, now), [
            {"label": "5h", "used": 7.0, "resets": datetime.datetime(2026, 10, 24, 19, 0, tzinfo=ROME).timestamp()},
            {"label": "7d", "used": 81.0, "resets": datetime.datetime(2026, 10, 27, 9, 30, tzinfo=ROME).timestamp()},
            {"label": "7d Fable", "used": 0.0, "resets": None},
        ])

    def test_reset_without_year_or_date(self):
        now = datetime.datetime(2026, 12, 31, 23, 0, tzinfo=ROME).timestamp()
        self.assertEqual(workers.parse_reset("Jan 2 at 1am (Europe/Rome)", now), datetime.datetime(2027, 1, 2, 1, 0, tzinfo=ROME).timestamp())
        self.assertEqual(workers.parse_reset("1am (Europe/Rome)", now), datetime.datetime(2027, 1, 1, 1, 0, tzinfo=ROME).timestamp())

    def test_codex_limits_prefer_codex_bucket(self):
        result = {"rateLimits": {"primary": {"usedPercent": 1}},
                  "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 5},
                                                    "secondary": {"usedPercent": 21, "windowDurationMins": 10080}}}}
        self.assertEqual([w["used"] for w in workers.parse_codex_limits(result)], [40, 21])
        self.assertEqual(workers.parse_codex_limits(result)[0]["label"], "5h")

    def test_alias_dirs(self):
        rc = "alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'\nexport CODEX_HOME=\"$HOME/.codex-x\"\n"
        self.assertEqual(workers.alias_dirs("claude", rc), [("claude-work", "~/.claude-work")])
        self.assertEqual(workers.alias_dirs("codex", rc), [("codex-x", "$HOME/.codex-x")])


class Table(unittest.TestCase):
    def test_validate_rejects_shell_text(self):
        with self.assertRaises(ValueError):
            workers.validate({"roles": [{"role": "x; rm -rf ~", "runs": "native", "model": "sonnet", "effort": "low"}]}, "claude", ACCOUNTS)
        with self.assertRaises(ValueError):
            workers.validate({"roles": [{"role": "x", "runs": "native", "model": "sonnet", "effort": "$(id)"}]}, "claude", ACCOUNTS)
        with self.assertRaises(ValueError):  # a Claude model on the Codex side, and an effort luna lacks
            workers.validate({"roles": [{"role": "x", "runs": "external", "model": "opus", "effort": "low"}]}, "claude", ACCOUNTS)
        with self.assertRaises(ValueError):
            workers.validate({"roles": [{"role": "x", "runs": "external", "model": "gpt-6-luna", "effort": "ultra"}]}, "claude", ACCOUNTS)

    def test_validate_keeps_model_and_effort(self):
        t = workers.validate({"roles": [IMPL, {"role": "reviewer", "runs": "external", "model": "gpt-6.1-sol", "effort": "ultra"}]}, "claude", ACCOUNTS)
        self.assertEqual(t["roles"][1]["model"], "gpt-6.1-sol")

    def test_implementer_is_always_native(self):
        for roles in ([{"role": "reviewer", "runs": "native", "model": "sonnet", "effort": "low"}],
                      [{"role": "implementer", "runs": "external", "model": "gpt-6-luna", "effort": "low"}],
                      [IMPL, IMPL]):
            with self.assertRaises(ValueError):
                workers.validate({"roles": roles}, "claude", ACCOUNTS)

    def test_validate_unknown_account_falls_back(self):
        t = workers.validate({"roles": [IMPL_CODEX, {"role": "reviewer", "runs": "external", "model": "sonnet", "effort": "low"}], "account": "/etc"},
                             "codex", ACCOUNTS)
        self.assertIsNone(t["account"])

    def test_instructions_from_claude_point_at_codex(self):
        text = workers.instructions("claude", {"roles": [{"role": "reviewer", "runs": "external", "model": "gpt-6-luna", "effort": "medium"},
                                                         {"role": "implementer", "runs": "native", "model": "opus", "effort": "max"}],
                                               "account": None}, {"native": "claude-work", "external": "codex"})
        self.assertIn("| reviewer | external (codex) | gpt-6-luna | medium |", text)
        self.assertIn("| implementer | native (claude-work) | opus | max |", text)
        self.assertIn("codex exec --dangerously-bypass-approvals-and-sandbox", text)
        self.assertNotIn("CODEX_HOME", text)

    def test_old_saved_rows_get_the_default_model(self):
        t = workers.load_table({"codex": {"roles": [{"role": "reviewer", "runs": "external", "effort": "high"}]}}, "codex")
        self.assertEqual(t["roles"][0]["model"], "sonnet")
        self.assertEqual(workers.load_table({}, "claude")["roles"][0]["model"], "sonnet")

    def test_instructions_from_codex_name_the_claude_account(self):
        text = workers.instructions("codex", {"roles": DEFAULT_ROLES, "account": "~/.claude-work"}, {"native": "codex", "external": "claude-work"})
        self.assertIn("CLAUDE_CONFIG_DIR=~/.claude-work claude -p --model <model> --effort <effort>", text)
        self.assertIn("write_stdin", text)

class Lead(unittest.TestCase):
    def test_validate_lead(self):
        lead = workers.validate_lead({"provider": "claude", "account": 1, "model": "opus", "effort": "high"}, {"claude": ACCOUNTS, "codex": [{"folder": None, "name": "codex"}]})
        self.assertEqual(lead["folder"], "~/.claude-work")
        for bad in ({"provider": "codex", "account": 1, "model": "default", "effort": "default"},
                    {"provider": "claude", "account": "0", "model": "default", "effort": "default"},
                    {"provider": "claude", "account": 0, "model": "default", "effort": "high"},
                    {"provider": "claude", "account": 0, "model": "opus", "effort": "ultra"}):
            with self.assertRaises(ValueError):
                workers.validate_lead(bad, {"claude": ACCOUNTS, "codex": [{"folder": None, "name": "codex"}]})

    def test_resume_only_listed_sessions(self):
        all_ = {"claude": ACCOUNTS, "codex": [{"folder": None, "name": "codex"}]}
        listed = [{"provider": "claude", "account": 1, "id": "abc"}]
        lead = workers.validate_lead({"provider": "claude", "account": 1, "model": "opus", "effort": "high", "resume": "abc"}, all_, listed)
        self.assertEqual((lead["resume"], lead["update"]), ("abc", False))
        with self.assertRaises(ValueError):  # same id, other account
            workers.validate_lead({"provider": "claude", "account": 0, "model": "opus", "effort": "high", "resume": "abc"}, all_, listed)

    def test_resume_argv(self):
        lead = {"provider": "claude", "model": "sonnet", "effort": "high", "resume": "abc", "update": False}
        self.assertEqual(workers.launch_argv(lead, "T"), ["claude", "--resume", "abc", "--model", "sonnet", "--effort", "high"])
        self.assertEqual(workers.launch_argv({**lead, "update": True}, "T")[-4:],
                         ["--system-prompt-snapshot", "off", "--append-system-prompt", "T"])
        argv = workers.launch_argv({**lead, "provider": "codex", "model": "gpt-6.1-sol", "update": True}, "T")
        self.assertEqual(argv[:2] + argv[-2:-1], ["codex", "resume", "abc"])
        self.assertTrue(argv[-1].endswith("\n\nT"))

    def test_claude_model_keeps_the_accounts_variant(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump({"model": "opus[1m]"}, f)
            self.assertEqual(workers.claude_model("opus", d), "opus[1m]")
            self.assertEqual(workers.claude_model("sonnet", d), "sonnet")
            self.assertEqual(workers.claude_model("opus", os.path.join(d, "missing")), "opus")

    def test_launch_argv(self):
        self.assertEqual(workers.launch_argv({"provider": "claude", "model": "sonnet", "effort": "high", "update": True}, "T"),
                         ["claude", "--model", "sonnet", "--effort", "high", "--append-system-prompt", "T"])
        self.assertEqual(workers.launch_argv({"provider": "codex", "model": "gpt-6.1-sol", "effort": "high", "update": True}, "T"),
                         ["codex", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "-c", 'developer_instructions="T"'])

DEFAULT_ROLES = workers.load_table({}, "codex")["roles"]
ACCOUNTS = [{"folder": None, "name": "claude"}, {"folder": "~/.claude-work", "name": "claude-work"}]
ROME = zoneinfo.ZoneInfo("Europe/Rome")
IMPL = {"role": "implementer", "runs": "native", "model": "sonnet", "effort": "medium"}
IMPL_CODEX = {"role": "implementer", "runs": "native", "model": "gpt-6-luna", "effort": "medium"}

if __name__ == "__main__":
    unittest.main()
