"""python3 -m unittest discover -s skills/agents-setup/workers"""
import datetime
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


DEFAULT_ROLES = workers.load_table({}, "codex")["roles"]
ACCOUNTS = [{"folder": None, "name": "claude"}, {"folder": "~/.claude-work", "name": "claude-work"}]
ROME = zoneinfo.ZoneInfo("Europe/Rome")
IMPL = {"role": "implementer", "runs": "native", "model": "sonnet", "effort": "medium"}
IMPL_CODEX = {"role": "implementer", "runs": "native", "model": "gpt-6-luna", "effort": "medium"}

if __name__ == "__main__":
    unittest.main()
