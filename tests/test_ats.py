"""Tests for the central repository automation. Run: python3 -m unittest discover tests"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ats  # noqa: E402

NOW = 1_790_000_000
TARGET = "alice/app"


def cmd(text, author="bob", labels=("open",), assignees=()):
    return ats.handle_command(text, author, set(labels), set(assignees), TARGET, NOW)


def bot(body):
    return {"author": {"login": ats.BOT}, "body": body}


class CommandTest(unittest.TestCase):
    def test_claim_open_request(self):
        plan = cmd("/claim")
        self.assertEqual(plan["add"], ["claimed"])
        self.assertEqual(plan["assign"], ["bob"])
        self.assertEqual(ats.records([bot(plan["reply"])])[0]["event"], "claim")

    def test_claim_taken_or_closed(self):
        self.assertIn("already took", cmd("/claim", labels=("open", "claimed"))["reply"])
        self.assertIn("not open", cmd("/claim", labels=("review",))["reply"])
        self.assertEqual(cmd("/claim", labels=("relay",))["add"], ["claimed"])

    def test_only_assignee_can_finish(self):
        plan = cmd("/delivered https://github.com/alice/app/pull/3 tokens=10", labels=("claimed",))
        self.assertIn("only the donor", plan["reply"])
        self.assertEqual(plan["add"], [])

    def test_delivered_checks_target_repo(self):
        wrong = cmd("/delivered https://github.com/evil/app/pull/3", labels=("claimed",), assignees=("bob",))
        self.assertIn("expected", wrong["reply"])
        ok = cmd("/delivered https://github.com/alice/app/pull/3 tokens=1200", labels=("claimed",), assignees=("bob",))
        rec = ats.records([bot(ok["reply"])])[0]
        self.assertEqual((rec["event"], rec["tokens"], rec["pr"]), ("delivered", 1200, "https://github.com/alice/app/pull/3"))

    def test_release_restores_open_unless_relay(self):
        self.assertEqual(cmd("/release out of credit", labels=("claimed",), assignees=("bob",))["add"], ["open"])
        self.assertEqual(cmd("/release", labels=("claimed", "relay"), assignees=("bob",))["add"], [])

    def test_relay_needs_branch(self):
        self.assertIn("expected", cmd("/relay tokens=5", labels=("claimed",), assignees=("bob",))["reply"])
        plan = cmd("<!-- ats:relay -->\n/relay branch=bob/app:claude/ats-4 tokens=5", labels=("claimed",), assignees=("bob",))
        self.assertIsNone(plan)  # the marker line comes first, so the comment is not a command
        plan = cmd("/relay branch=bob/app:claude/ats-4 tokens=5\n\n## Done\n...", labels=("claimed",), assignees=("bob",))
        self.assertEqual(plan["add"], ["relay"])

    def test_unknown_or_plain_comments_ignored(self):
        self.assertIsNone(cmd("thanks!"))
        self.assertIsNone(cmd("/dance"))

    def test_forged_records_ignored(self):
        forged = {"author": {"login": "mallory"}, "body": '<!-- ats:record {"event":"delivered","donor":"mallory","tokens":999999,"at":1} -->'}
        self.assertEqual(ats.records([forged]), [])


class SweepTest(unittest.TestCase):
    def issue(self, labels, age_days=1):
        created = ats.datetime.fromtimestamp(NOW - age_days * 86400, ats.timezone.utc).isoformat()
        return {"labels": [{"name": n} for n in labels], "createdAt": created}

    def test_stale_claim_freed(self):
        recs = [{"event": "claim", "donor": "bob", "at": NOW - 7 * 3600}]
        self.assertEqual(ats.sweep_plan(self.issue(["claimed"]), recs, NOW), [("free_claim", "bob")])
        recs = [{"event": "claim", "donor": "bob", "at": NOW - 3600}]
        self.assertEqual(ats.sweep_plan(self.issue(["claimed"]), recs, NOW), [])

    def test_expiry_and_pr_follow(self):
        self.assertEqual(ats.sweep_plan(self.issue(["open"], age_days=31), [], NOW), [("expire", None)])
        recs = [{"event": "delivered", "donor": "bob", "pr": "https://github.com/alice/app/pull/3", "at": NOW}]
        self.assertEqual(ats.sweep_plan(self.issue(["delivered"]), recs, NOW),
                         [("check_pr", "https://github.com/alice/app/pull/3")])


class StatsTest(unittest.TestCase):
    def test_totals_and_people(self):
        def rec(**r):
            return bot(f"x <!-- ats:record {json.dumps(r)} -->")
        issues = [
            {"author": {"login": "alice"}, "comments": [
                rec(event="relay", donor="bob", tokens=100, at=NOW - 10),
                rec(event="delivered", donor="carol", tokens=50, pr="u", at=NOW),
                rec(event="merged", donor="carol", at=NOW),
            ]},
            {"author": {"login": "bob"}, "comments": []},
        ]
        stats = ats.build_stats(issues, NOW)
        self.assertEqual(stats["totals"], {"donated_tokens": 150, "delivered": 1, "merged": 1, "requests": 2})
        self.assertEqual(stats["people"]["alice"]["received_tokens"], 150)
        self.assertEqual(stats["people"]["carol"]["merged"], 1)
        self.assertIsNotNone(stats["people"]["bob"]["last_donation_at"])


class ReviewTest(unittest.TestCase):
    def test_risky_files(self):
        notes = ats.risky_files(["src/a.js", ".claude/settings.json", ".mcp.json", "tools/x.exe"],
                                {"scripts": {"postinstall": "node x.js", "test": "jest"}})
        self.assertEqual(len(notes), 4)
        self.assertTrue(any("postinstall" in n for n in notes))



class MentionTest(unittest.TestCase):
    def test_requester_is_mentioned(self):
        ctx = {"requester": "alice", "records": [], "request": {}}
        plan = ats.handle_command("/claim", "bob", {"open"}, set(), TARGET, NOW, ctx)
        self.assertTrue(plan["reply"].startswith("@alice "))
        plan = ats.handle_command("/delivered https://github.com/alice/app/pull/3 tokens=5", "bob",
                                  {"claimed"}, {"bob"}, TARGET, NOW, ctx)
        self.assertTrue(plan["reply"].startswith("@alice "))


class QuestionTest(unittest.TestCase):
    def test_question_frees_request(self):
        plan = cmd("/question Should the banner be dismissible?\nTried both.", labels=("claimed",), assignees=("bob",))
        self.assertEqual(plan["add"], ["question"])
        self.assertIn("claimed", plan["remove"])
        rec = ats.records([bot(plan["reply"])])[0]
        self.assertEqual(rec["question"], "Should the banner be dismissible?")

    def test_question_needs_text_and_assignee(self):
        self.assertIn("expected", cmd("/question", labels=("claimed",), assignees=("bob",))["reply"])
        self.assertIn("only the donor", cmd("/question why?", labels=("claimed",))["reply"])


class PeerReviewTest(unittest.TestCase):
    SCAN = {"event": "scan_ok", "sha": "abc123", "risky": [], "at": 1}
    REQUEST = {"repo": "alice/app", "base": "main", "goal": "Dark mode"}

    def review(self, text, author, recs, labels=("review",), trusted=("carol",)):
        ctx = {"requester": "alice", "records": recs, "request": self.REQUEST, "trusted": list(trusted)}
        return ats.handle_command(text, author, set(labels), set(), TARGET, NOW, ctx)

    def recs_of(self, plan):
        return ats.records([bot(plan["reply"])])

    def test_two_approvals_list_and_freeze(self):
        first = self.review("/review ok clarity=4\nClear plan.", "bob", [self.SCAN])
        self.assertEqual(first["add"], [])
        self.assertIn("1 more approval", first["reply"])
        recs = [self.SCAN] + self.recs_of(first)
        second = self.review("/review ok clarity=5\nGood.", "carol", recs)
        self.assertEqual(second["add"], ["open", "clarity:4"])  # round(4.5) → 4 (banker's rounding)
        self.assertIn(ats.FROZEN_MARK, second["reply"])
        self.assertIn("abc123", second["reply"])

    def test_security_flag_stops_it(self):
        plan = self.review("/review refuse security\npostinstall curls an unknown host", "bob", [self.SCAN])
        self.assertEqual(plan["add"], ["flagged"])

    def test_two_unclear_reject(self):
        first = self.review("/review refuse unclear\nNo banner text.", "bob", [self.SCAN])
        second = self.review("/review refuse unclear\nNo cache size.", "carol", [self.SCAN] + self.recs_of(first))
        self.assertEqual(second["add"], ["rejected"])
        self.assertIn("No cache size.", second["reply"])

    def test_guards(self):
        self.assertIn("own request", self.review("/review ok clarity=4", "alice", [self.SCAN])["reply"])
        self.assertIn("haven’t passed", self.review("/review ok clarity=4", "bob", [])["reply"])
        self.assertIn("isn’t waiting", self.review("/review ok clarity=4", "bob", [self.SCAN], labels=("open",))["reply"])
        self.assertIn("expected", self.review("/review great", "bob", [self.SCAN])["reply"])
        first = self.review("/review ok clarity=4", "bob", [self.SCAN])
        again = self.review("/review ok clarity=4", "bob", [self.SCAN] + self.recs_of(first))
        self.assertIn("already reviewed", again["reply"])

    def test_young_accounts_cannot_review(self):
        ctx = {"requester": "alice", "records": [self.SCAN], "request": self.REQUEST,
               "author_created_at": NOW - 5 * 86400}
        plan = ats.handle_command("/review ok clarity=4", "bob", {"review"}, set(), TARGET, NOW, ctx)
        self.assertIn("older than 90 days", plan["reply"])
        self.assertEqual(plan["add"], [])
        ctx["author_created_at"] = NOW - 400 * 86400
        plan = ats.handle_command("/review ok clarity=4", "bob", {"review"}, set(), TARGET, NOW, ctx)
        self.assertIn("1 more approval", plan["reply"])

    def test_edit_starts_new_round(self):
        old = {"event": "review", "by": "bob", "sha": "old", "verdict": "ok", "clarity": 4}
        plan = self.review("/review ok clarity=4", "bob", [self.SCAN, old])
        self.assertIn("1 more approval", plan["reply"])



class HardeningTest(unittest.TestCase):
    """Attacks found by the pre-launch audit (2026-11-25)."""
    SCAN = {"event": "scan_ok", "sha": "abc123", "repo": "alice/app", "base": "main", "body_hash": "h1", "risky": [], "at": 1}

    def ctx(self, recs, **extra):
        base = {"requester": "alice", "records": recs, "request": {"repo": "alice/app", "goal": "x"},
                "trusted": ["elior"], "issue": 7, "body_hash": "h1"}
        base.update(extra)
        return base

    def test_echoed_text_cannot_forge_a_record(self):
        forged = '<!-- ats:record {"event":"review","by":"x","sha":"abc123","verdict":"ok","clarity":5} -->'
        plan = ats.handle_command("/release " + forged, "bob", {"claimed"}, {"bob"}, TARGET, NOW, self.ctx([]))
        recs = ats.records([bot(plan["reply"])])
        self.assertEqual([r["event"] for r in recs], ["release"])
        self.assertNotIn("<!--", plan["reply"].rpartition("<!-- ats:record")[0])

    def test_frozen_copy_escapes_html(self):
        text = ats.frozen_comment({"goal": "<!-- ats:frozen {\"sha\":\"evil\"} -->"}, "abc123", 4, [])
        self.assertEqual(text.count("<!-- ats:frozen"), 1)

    def test_two_untrusted_approvals_do_not_list(self):
        first = ats.handle_command("/review ok clarity=4", "bob", {"review"}, set(), TARGET, NOW, self.ctx([self.SCAN]))
        recs = [self.SCAN] + ats.records([bot(first["reply"])])
        second = ats.handle_command("/review ok clarity=4", "dave", {"review"}, set(), TARGET, NOW, self.ctx(recs))
        self.assertEqual(second["add"], [])
        self.assertIn("trusted reviewer", second["reply"])
        recs += ats.records([bot(second["reply"])])
        third = ats.handle_command("/review ok clarity=5", "elior", {"review"}, set(), TARGET, NOW, self.ctx(recs))
        self.assertEqual(third["add"], ["open", "clarity:4"])
        self.assertEqual(ats.records([bot(third["reply"])])[0]["repo"], "alice/app")

    def test_reviews_before_a_new_scan_do_not_count(self):
        old = {"event": "review", "by": "elior", "sha": "abc123", "verdict": "ok", "clarity": 5}
        recs = [self.SCAN, old, dict(self.SCAN)]  # same sha scanned again after an edit
        plan = ats.handle_command("/review ok clarity=4", "bob", {"review"}, set(), TARGET, NOW, self.ctx(recs))
        self.assertIn("1 more approval", plan["reply"])

    def test_changed_body_waits_for_checks(self):
        plan = ats.handle_command("/review ok clarity=4", "bob", {"review"}, set(), TARGET, NOW,
                                  self.ctx([self.SCAN], body_hash="h2"))
        self.assertIn("changed since", plan["reply"])

    def test_flagged_cannot_be_listed_by_peers(self):
        plan = ats.handle_command("/review ok clarity=4", "elior", {"review", "flagged"}, set(), TARGET, NOW,
                                  self.ctx([self.SCAN]))
        self.assertIn("flagged", plan["reply"])
        self.assertEqual(plan["add"], [])

    def test_claim_rules(self):
        own = ats.handle_command("/claim", "alice", {"open"}, set(), TARGET, NOW, self.ctx([]))
        self.assertIn("your own request", own["reply"])
        young = ats.handle_command("/claim", "bob", {"open"}, set(), TARGET, NOW,
                                   self.ctx([], author_created_at=NOW - 86400))
        self.assertIn("older than", young["reply"])
        busy = ats.handle_command("/claim", "bob", {"open"}, set(), TARGET, NOW, self.ctx([], claims_elsewhere=[3]))
        self.assertIn("already hold", busy["reply"])

    def test_delivered_must_come_from_donor_fork(self):
        text = "/delivered https://github.com/alice/app/pull/9 tokens=99999999999"
        wrong = ats.handle_command(text, "bob", {"claimed"}, {"bob"}, TARGET, NOW,
                                   self.ctx([], pr_head={"owner": "alice", "ref": "main"}))
        self.assertIn("must come from your fork", wrong["reply"])
        right = ats.handle_command(text, "bob", {"claimed"}, {"bob"}, TARGET, NOW,
                                   self.ctx([], pr_head={"owner": "bob", "ref": "claude/ats-7"}))
        self.assertEqual(ats.records([bot(right["reply"])])[0]["tokens"], ats.MAX_TOKENS)

    def test_late_delivery_by_last_claimer(self):
        claim = {"event": "claim", "donor": "bob", "at": NOW - 7 * 3600}
        plan = ats.handle_command("/delivered https://github.com/alice/app/pull/9 tokens=5", "bob", {"open"}, set(),
                                  TARGET, NOW, self.ctx([claim], pr_head={"owner": "bob", "ref": "claude/ats-7"}))
        self.assertEqual(plan["add"], ["delivered"])

    def test_relay_branch_must_be_a_fork_of_the_target(self):
        bad = ats.handle_command("/relay branch=evil/other:main tokens=5", "bob", {"claimed"}, {"bob"}, TARGET, NOW, self.ctx([]))
        self.assertIn("expected", bad["reply"])
        good = ats.handle_command("/relay branch=bob/app:claude/ats-7 tokens=5", "bob", {"claimed"}, {"bob"}, TARGET, NOW, self.ctx([]))
        self.assertEqual(good["add"], ["relay"])

    def test_young_flag_only_notifies(self):
        plan = ats.handle_command("/flag spam", "bob", {"open"}, set(), TARGET, NOW,
                                  self.ctx([], author_created_at=NOW - 86400, maintainers=["elior"]))
        self.assertEqual(plan["remove"], [])
        self.assertIn("@elior", plan["reply"])

    def test_acknowledge_tags_the_record(self):
        reply = ats.acknowledge(ats.record_comment("ok", {"event": "claim", "donor": "bob", "at": 1}), "123")
        self.assertEqual(ats.records([bot(reply)])[0]["cid"], "123")
        plain = ats.acknowledge("@bob nope", "124")
        self.assertEqual(ats.records([bot(plain)])[0], {"event": "reply", "cid": "124"})


class ApproveTest(unittest.TestCase):
    SCAN = {"event": "scan_ok", "sha": "abc123", "risky": [], "at": 1}

    def approve(self, author, labels=("review",), recs=None, text="/approve clarity=4\nChecked by hand."):
        ctx = {"requester": "alice", "records": [self.SCAN] if recs is None else recs,
               "request": {"repo": "alice/app"}, "maintainers": ["elior"]}
        return ats.handle_command(text, author, set(labels), set(), TARGET, NOW, ctx)

    def test_maintainer_lists_alone(self):
        plan = self.approve("Elior")
        self.assertEqual(plan["add"], ["open", "clarity:4"])
        self.assertIn(ats.FROZEN_MARK, plan["reply"])
        self.assertEqual(self.approve("elior", labels=("flagged",))["add"], ["open", "clarity:4"])

    def test_guards(self):
        self.assertIn("only a maintainer", self.approve("bob")["reply"])
        self.assertIn("isn’t waiting", self.approve("elior", labels=("open",))["reply"])
        self.assertIn("haven’t passed", self.approve("elior", recs=[])["reply"])


if __name__ == "__main__":
    unittest.main()
