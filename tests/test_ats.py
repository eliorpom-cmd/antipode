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


class UnflagTest(unittest.TestCase):
    """Request #9: the requester fixed a flagged setup by editing, and the request stayed stuck."""

    def unflag(self, author="elior", labels=("review", "flagged")):
        return ats.handle_command("/unflag settings.json removed", author, set(labels), set(), TARGET, NOW,
                                  {"requester": "alice", "records": [], "maintainers": ["elior"]})

    def test_maintainer_sends_it_back_to_review(self):
        plan = self.unflag()
        self.assertEqual(plan["remove"], ["flagged"])
        self.assertEqual(plan["add"], ["review"])
        self.assertIn("settings.json removed", plan["reply"])
        self.assertEqual(ats.records([bot(plan["reply"])])[0]["event"], "unflag")

    def test_only_the_maintainer(self):
        plan = self.unflag(author="bob")
        self.assertIn("only a maintainer", plan["reply"])
        self.assertEqual(plan["remove"], [])

    def test_not_flagged(self):
        self.assertIn("isn’t flagged", self.unflag(labels=("review",))["reply"])

    def test_edit_message_names_the_flag(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "elior"}):
            text = ats.edited_while_flagged()
        self.assertIn("still flagged", text)
        self.assertIn("@elior", text)
        self.assertIn("/unflag", text)


class RenameTest(unittest.TestCase):
    """A donor renamed overnight on the first real donations (d3xter1z → twistedwhitebranch)."""

    def comments(self):
        def rec(**r):
            return bot(f"x <!-- ats:record {json.dumps(r)} -->")
        return [
            {"author": {"login": "dex-new"}, "url": "https://github.com/o/r/issues/3#issuecomment-11", "body": "/claim"},
            rec(event="claim", donor="dex-old", at=NOW - 600, cid="11"),
            {"author": {"login": "dex-new"}, "url": "https://github.com/o/r/issues/3#issuecomment-12",
             "body": "/delivered https://github.com/alice/app/pull/1 tokens=100"},
            rec(event="delivered", donor="dex-old", tokens=100, pr="u", at=NOW, cid="12"),
            rec(event="merged", donor="dex-old", at=NOW),
        ]

    def test_records_use_the_current_login(self):
        self.assertEqual({r["donor"] for r in ats.records(self.comments())}, {"dex-new"})

    def test_stats_count_one_person(self):
        stats = ats.build_stats([{"author": {"login": "alice"}, "comments": self.comments()}], NOW)
        self.assertNotIn("dex-old", stats["people"])
        self.assertEqual(stats["people"]["dex-new"]["delivered"], 1)
        self.assertEqual(stats["people"]["dex-new"]["merged"], 1)

    def test_deleted_account_keeps_its_login(self):
        comments = self.comments()
        comments[0]["author"]["login"] = "ghost"
        comments[2]["author"]["login"] = "ghost"
        self.assertEqual({r["donor"] for r in ats.records(comments)}, {"dex-old"})


class StaleLockTest(unittest.TestCase):
    """GitHub skips scheduled sweeps for hours: a lock past its time must be free anyway."""

    def claim(self, age_hours, author="carol"):
        recs = [{"event": "claim", "donor": "bob", "at": NOW - age_hours * 3600}]
        return ats.handle_command("/claim", author, {"claimed"}, {"bob"}, TARGET, NOW,
                                  {"requester": "alice", "records": recs})

    def test_expired_lock_can_be_taken(self):
        plan = self.claim(7)
        self.assertEqual(plan["assign"], ["carol"])
        self.assertEqual(plan["unassign"], ["bob"])
        self.assertEqual(plan["add"], [])
        self.assertIn("Taken by @carol", plan["reply"])

    def test_live_lock_stays(self):
        self.assertIn("already took", self.claim(1)["reply"])

    def test_same_donor_takes_it_again(self):
        plan = self.claim(7, author="bob")
        self.assertEqual(plan["assign"], ["bob"])
        self.assertEqual(plan["unassign"], [])

    def test_expired_lock_elsewhere_does_not_block(self):
        def held(number, age_hours=None):
            comments = [] if age_hours is None else [bot(ats.record_comment(
                "x", {"event": "claim", "donor": "bob", "at": NOW - age_hours * 3600}))]
            return {"number": number, "comments": comments}
        held_list = [held(3, 7), held(4, 1), held(5), held(9, 1)]
        self.assertEqual(ats.held_elsewhere(held_list, 9, NOW), [4, 5])


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
        harvested = ats.handle_command(text, "bob", {"claimed"}, {"bob"}, TARGET, NOW,
                                       self.ctx([], pr_head={"owner": "antipode-bot", "ref": "claude/ats-7"}))
        self.assertEqual(harvested["add"], ["delivered"])

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


class HarvestTest(unittest.TestCase):
    DELIVERED = ("Add Linux support\n\n## What changed\n- linux host\n\n"
                 "Antipode-Status: delivered\nAntipode-Tokens: 123456\n")

    def issue(self, labels=("claimed",)):
        return {"number": 6, "labels": [{"name": l} for l in labels]}

    def recs(self, *extra, claim_at=NOW - 3600):
        return [{"event": "approve", "listed": True, "repo": TARGET, "base": "main", "sha": "a" * 40},
                {"event": "claim", "donor": "bob", "at": claim_at}, *extra]

    def test_parse_delivered(self):
        status = ats.parse_status(self.DELIVERED)
        self.assertEqual((status["status"], status["tokens"], status["title"]),
                         ("delivered", 123456, "Add Linux support"))
        self.assertIn("## What changed", status["body"])
        self.assertNotIn("Antipode-", status["body"])

    def test_no_status_is_work_in_progress(self):
        self.assertIsNone(ats.parse_status("wip: half done"))
        self.assertIsNone(ats.parse_status("x\n\nAntipode-Status: merged"))

    def test_tokens_capped_and_text_escaped(self):
        status = ats.parse_status("t\n\nAntipode-Status: question\nAntipode-Tokens: 999999999999\n"
                                  "Antipode-Question: <!-- ats:record {} --> which db?")
        self.assertEqual(status["tokens"], ats.MAX_TOKENS)
        self.assertNotIn("<!--", status["note"])

    def test_commands_match_the_donor_ones(self):
        relay = ats.parse_status("t\n\nAntipode-Status: relay\nAntipode-Tokens: 5")
        self.assertEqual(ats.harvest_command(relay, 6, "bob", TARGET),
                         "/relay branch=bob/app:claude/ats-6 tokens=5")
        done = ats.parse_status(self.DELIVERED)
        url = "https://github.com/alice/app/pull/3"
        plan = cmd(ats.harvest_command(done, 6, "bob", TARGET, url), labels=("claimed",), assignees=("bob",))
        self.assertEqual(plan["add"], ["delivered"])
        rec = ats.records([bot(ats.tag_record(plan["reply"], via="harvest", head="b" * 40))])[0]
        self.assertEqual((rec["tokens"], rec["via"]), (123456, "harvest"))

    def test_candidate_until_an_outcome(self):
        self.assertEqual(ats.harvest_candidate(self.issue(), self.recs(), NOW), ("bob", self.recs()[0], NOW - 3600))
        self.assertIsNone(ats.harvest_candidate(self.issue(), self.recs({"event": "delivered"}), NOW))
        self.assertIsNone(ats.harvest_candidate(self.issue(), self.recs({"event": "release", "via": "harvest"}), NOW))
        # The sweep freed the lock: a late delivery is still read, until nobody could expect it.
        swept = self.recs({"event": "release", "donor": "bob"})
        self.assertEqual(ats.harvest_candidate(self.issue(("open",)), swept, NOW)[0], "bob")
        self.assertIsNone(ats.harvest_candidate(self.issue(), self.recs(claim_at=NOW - 13 * 3600), NOW))
        self.assertIsNone(ats.harvest_candidate(self.issue(("delivered",)), self.recs(), NOW))

    def test_pr_body_keeps_the_donor_credited(self):
        body = ats.pr_body(ats.parse_status(self.DELIVERED), "https://github.com/x/y/issues/6", "bob", 6)
        self.assertIn("## What changed", body)
        self.assertTrue(body.startswith("> [bob](https://github.com/bob)’s agent did this for your "
                                        "[Antipode request #6](https://github.com/x/y/issues/6)."))
        self.assertIn("Made with [Antipode](https://useantipode.com/", body)
        self.assertNotRegex(body, r"(?<![\w`/])@bob")
        self.assertIn("nobody reads comments here", body.split("\n")[0])

    def test_pr_body_pings_nobody(self):
        status = ats.parse_status("t\n\nThanks @alice and @bob-2, see a@b.com and `@carol`.\n\n"
                                  "Antipode-Status: delivered")
        body = ats.pr_body(status, "https://github.com/x/y/issues/6", "bob", 6)
        self.assertIn("`@alice`", body)
        self.assertIn("`@bob-2`", body)
        self.assertIn("a@b.com", body)
        self.assertIn("`@carol`", body)
        self.assertNotIn("``", body)

    def test_note_goes_before_the_record(self):
        reply = ats.record_comment("Partly done.", {"event": "relay"})
        out = ats.with_note(reply, "## Left\n- tests <b>")
        self.assertEqual(ats.records([bot(out)])[0]["event"], "relay")
        self.assertIn("&lt;b&gt;", out)

    def test_both_bot_accounts_are_trusted(self):
        old = {"author": {"login": "github-actions[bot]"}, "body": ats.record_comment("x", {"event": "claim"})}
        new = {"author": {"login": "antipode-bot"}, "body": ats.record_comment("y", {"event": "release"})}
        fake = {"author": {"login": "antipode-b0t"}, "body": ats.record_comment("z", {"event": "merged"})}
        self.assertEqual([r["event"] for r in ats.records([old, new, fake])], ["claim", "release"])


class BotGhTest(unittest.TestCase):
    """Issue #13: `gh issue comment` prints a URL, not JSON; the comment must not crash the run."""

    def run_with(self, stdout, **kwargs):
        from unittest import mock
        done = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.dict(os.environ, {"ANTIPODE_BOT_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}), \
                mock.patch.object(ats.subprocess, "run", return_value=done) as run:
            return ats.comment_as_bot(10, "hi") if kwargs.get("comment") else ats.bot_gh("api", "x"), run

    def test_comment_prints_url(self):
        _, run = self.run_with("https://github.com/o/r/issues/10#issuecomment-1\n", comment=True)
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "t")

    def test_api_still_parses_json(self):
        result, _ = self.run_with('{"html_url": "u"}')
        self.assertEqual(result, {"html_url": "u"})

    def test_api_error_says_why(self):
        """#16: the run only said "Validation Failed (HTTP 422)"; the API's reason must reach the log."""
        from unittest import mock
        body = '{"message": "Validation Failed", "errors": [{"resource": "PullRequest", "code": "custom", ' \
               '"message": "Fork collab can\'t be granted by someone without permission"}]}'
        failed = mock.Mock(returncode=1, stdout=body, stderr="gh: Validation Failed (HTTP 422)")
        with mock.patch.dict(os.environ, {"ANTIPODE_BOT_TOKEN": "t"}), \
                mock.patch.object(ats.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as caught:
                ats.bot_gh("api", "-X", "POST", "repos/o/r/pulls")
        self.assertIn("Fork collab can't be granted", str(caught.exception))

    def test_pull_request_comes_from_the_bot_fork(self):
        """#16: GitHub refuses a pull request from the donor's fork ("must be a collaborator"). The bot
        copies the donor's commit into its own fork and opens the pull request from there."""
        from unittest import mock
        calls = []

        def fake_bot(*args, **kwargs):
            calls.append(args)
            if args[-1] == "default_branch_only=true":
                return {"full_name": "antipode-bot/t"}
            return {"html_url": "u"} if args[3] == "repos/o/t/pulls" else {}

        def fake_read(*args, **kwargs):
            if args[1] == "repos/antipode-bot/t":
                return {"full_name": "antipode-bot/t"}
            return {"message": "Not Found", "status": "404"}  # what gh prints on a 404
        with mock.patch.object(ats, "gh_json", side_effect=fake_read), \
                mock.patch.object(ats, "bot_gh", side_effect=fake_bot):
            self.assertEqual(ats.open_pull_request("o/t", "main", "d", 16, "T", "B", "abc"), "u")
        self.assertIn(("api", "-X", "POST", "repos/antipode-bot/t/git/refs", "-f", "ref=refs/heads/claude/ats-16",
                       "-f", "sha=abc"), calls)
        self.assertIn("head=antipode-bot:claude/ats-16", calls[-1])

    def test_existing_pull_request_is_reused(self):
        from unittest import mock
        def fake_read(*args, **kwargs):
            return [{"html_url": "by-hand"}] if "head=d:claude/ats-16" in args[1] else []
        with mock.patch.object(ats, "gh_json", side_effect=fake_read), \
                mock.patch.object(ats, "bot_gh") as bot:
            self.assertEqual(ats.open_pull_request("o/t", "main", "d", 16, "T", "B", "abc"), "by-hand")
        bot.assert_not_called()


class PluginVersionTest(unittest.TestCase):
    """28/11: the bot tells donors on old plugins (GitHub tools in the session before 0.4.0) to update."""

    MARK = "\n<!-- antipode-plugin 0.5.0 -->"

    def setUp(self):
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "owner"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_signature_is_read(self):
        self.assertEqual(ats.plugin_version("/claim" + self.MARK), (0, 5, 0))
        self.assertIsNone(ats.plugin_version("/claim"))

    def test_who_gets_the_notice(self):
        self.assertTrue(ats.outdated_plugin("/claim", "bob"))
        self.assertTrue(ats.outdated_plugin("/review ok clarity=4\nfine\n<!-- antipode-plugin 0.4.2 -->", "bob"))
        self.assertFalse(ats.outdated_plugin("/claim" + self.MARK, "bob"))
        self.assertFalse(ats.outdated_plugin("/approve", "bob"))  # not a plugin command
        self.assertFalse(ats.outdated_plugin("/review ok clarity=4\nx", "Owner"))  # the owner types by hand

    def test_notice_sits_above_the_record(self):
        reply = ats.record_comment("Taken.", {"event": "claim", "donor": "bob"})
        out = ats.with_notice(reply, ats.update_notice("bob"))
        self.assertIn("out of date", out)
        self.assertIn(ats.SECURITY_URL, out)
        self.assertEqual(ats.records([bot(out)])[0]["event"], "claim")
        self.assertTrue(out.rstrip().endswith("-->"))

    def test_signature_never_reaches_a_reason(self):
        body = "/release push denied" + self.MARK
        plan = ats.handle_command(ats.PLUGIN_MARK_RE.sub("", body).strip(), "bob", {"claimed"}, {"bob"},
                                  TARGET, NOW, {"records": []})
        self.assertNotIn("antipode-plugin", plan["reply"])

    def test_enforced_notice_says_nothing_ran(self):
        from unittest import mock
        with mock.patch.object(ats, "ENFORCE_PLUGIN", True):
            self.assertIn("was not run", ats.update_notice("bob"))
        self.assertIn("was run as usual", ats.update_notice("bob"))

    def test_adoption_counts_each_donor_once_at_their_newest_version(self):
        def c(author, body, at="2026-11-29T10:00:00Z"):
            return {"author": {"login": author}, "body": body, "createdAt": at}
        issues = [{"comments": [c("bob", "/claim"), c("bob", "/release x" + self.MARK),
                                c("amy", "/review ok clarity=3\nok"), c("old", "/claim", "2026-11-27T00:00:00Z"),
                                c("antipode-bot", "/claim"), c("eve", "thanks")]}]
        got = ats.plugin_adoption(issues, "2026-11-28T17:00:00Z")
        self.assertEqual((got["current_logins"], got["outdated_logins"]), (["bob"], ["amy"]))

    def test_skills_sign_with_the_plugin_version(self):
        root = os.path.join(os.path.dirname(__file__), "..", "plugins", "antipode")
        with open(os.path.join(root, ".claude-plugin", "plugin.json")) as f:
            version = json.load(f)["version"]
        for path in ("references/setup.md", "skills/give/SKILL.md"):
            with open(os.path.join(root, path)) as f:
                self.assertIn(f"<!-- antipode-plugin {version} -->", f.read(), path)
        self.assertLessEqual(ats.MIN_PLUGIN, tuple(int(x) for x in version.split(".")))


class LabelRepairTest(unittest.TestCase):
    """Outage of 27/11 (#13): the bot commented, then crashed before the labels. #9 stayed in review
    although its record said it was listed. The next pass must put the labels back."""

    POSTED = "2026-11-27T19:37:40Z"

    def setUp(self):
        from unittest import mock
        self.mock = mock
        patcher = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def crash_after_comment(self, plan):
        """apply_plan as on 27/11: the comment goes out, the label edit never happens."""
        posted = []

        def fake_gh(*args, **kwargs):
            raise RuntimeError("crashed before the labels")
        with self.mock.patch.object(ats, "comment_as_bot", side_effect=lambda n, body: posted.append(body)), \
                self.mock.patch.object(ats, "gh", side_effect=fake_gh):
            with self.assertRaises(RuntimeError):
                ats.apply_plan(9, plan)
        return {"author": {"login": ats.BOT}, "body": posted[0], "createdAt": self.POSTED}

    def repair(self, labels, comments, events=""):
        issue = {"number": 9, "labels": [{"name": n} for n in labels], "comments": comments}
        calls = []

        def fake_gh(*args, **kwargs):
            calls.append(args)
            return events if args[0] == "api" else ""
        with self.mock.patch.object(ats, "gh", side_effect=fake_gh):
            ats.repair_labels(issue)
        edits = [a for a in calls if a[:2] == ("issue", "edit")]
        return calls, edits, {l["name"] for l in issue["labels"]}

    def listed_plan(self):
        return ats.empty_plan(add=["open", "clarity:4"], remove=["review"], reply=ats.record_comment(
            "Reviewed and listed.", {"event": "review", "by": "lucaci32u4", "listed": True, "at": NOW}))

    def test_record_carries_label_changes(self):
        reply = ats.with_label_changes(ats.record_comment("x", {"event": "claim"}), ["claimed"], [])
        rec = ats.records([bot(reply)])[0]
        self.assertEqual((rec["event"], rec["labels"]), ("claim", {"add": ["claimed"], "remove": []}))
        self.assertEqual(ats.with_label_changes("plain text", ["open"], []), "plain text")
        self.assertEqual(ats.with_label_changes(ats.record_comment("x", {"event": "reply"}), [], []),
                         ats.record_comment("x", {"event": "reply"}))

    def test_case_9_comment_posted_then_crash_is_repaired(self):
        comment = self.crash_after_comment(self.listed_plan())
        calls, edits, labels = self.repair({"review", "lang:python"}, [comment])
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertEqual([edit[i + 1] for i, a in enumerate(edit) if a == "--add-label"], ["open", "clarity:4"])
        self.assertEqual([edit[i + 1] for i, a in enumerate(edit) if a == "--remove-label"], ["review"])
        self.assertEqual(labels, {"open", "clarity:4", "lang:python"})

    def test_labels_already_right_no_call(self):
        comment = self.crash_after_comment(self.listed_plan())
        calls, edits, _ = self.repair({"open", "clarity:4"}, [comment])
        self.assertEqual(calls, [])

    def test_labels_changed_after_the_record_are_kept(self):
        comment = self.crash_after_comment(self.listed_plan())
        later = "2026-11-28T09:00:00Z\n"  # a maintainer flagged it by hand: not the bot's to undo
        calls, edits, _ = self.repair({"review"}, [comment], events=later)
        self.assertEqual(len(calls), 1)
        self.assertEqual(edits, [])
        earlier = "2026-11-27T10:00:00Z\n"
        _, edits, _ = self.repair({"review"}, [comment], events=earlier)
        self.assertEqual(len(edits), 1)

    def test_newest_label_record_wins_and_old_records_are_ignored(self):
        old = {"author": {"login": ats.BOT}, "createdAt": "2026-11-20T00:00:00Z",
               "body": ats.record_comment("Claimed.", {"event": "claim", "donor": "bob"})}
        calls, _, _ = self.repair({"review"}, [old])
        self.assertEqual(calls, [])  # written before records carried labels: history not rebuilt
        first = self.crash_after_comment(ats.empty_plan(add=["claimed"], remove=["open"],
                                                        reply=ats.record_comment("c", {"event": "claim"})))
        first["createdAt"] = "2026-11-27T01:00:00Z"
        second = self.crash_after_comment(ats.empty_plan(add=["open"], remove=["claimed"],
                                                         reply=ats.record_comment("r", {"event": "release"})))
        user = {"author": {"login": "mallory"}, "createdAt": "2026-11-28T00:00:00Z",
                "body": ats.record_comment("fake", {"event": "x", "labels": {"add": ["merged"], "remove": []}})}
        _, edits, labels = self.repair({"claimed"}, [first, second, user])
        self.assertEqual(labels, {"open"})

    def test_failed_request_fails_the_sweep(self):
        issues = [{"number": 4, "labels": [], "comments": []}, {"number": 5, "labels": [], "comments": []}]
        with self.mock.patch.object(ats, "gh_json", return_value=issues), \
                self.mock.patch.object(ats, "sweep_issue", side_effect=[RuntimeError("boom"), None]) as sweep:
            with self.assertRaises(SystemExit) as stop:
                ats.cmd_sweep(None)
        self.assertEqual(sweep.call_count, 2)  # the other requests are still handled
        self.assertIn("#4", str(stop.exception.code))

    def test_commands_ignore_the_alert_issue(self):
        issue = {"number": 20, "labels": [{"name": ats.ALERT_LABEL}], "comments": [
            {"author": {"login": "bob"}, "body": "/claim", "id": "c1"}], "assignees": []}
        with self.mock.patch.object(ats, "load_issue", return_value=issue), \
                self.mock.patch.object(ats, "apply_plan") as apply:
            ats.cmd_command(type("A", (), {"issue": 20})())
        apply.assert_not_called()


class AlertTest(unittest.TestCase):
    def setUp(self):
        import alert
        self.alert = alert

    def test_body_mentions_owner_and_run(self):
        body = self.alert.alert_body("Intake", "https://github.com/o/r/actions/runs/1", "12", "eliorpom-cmd")
        self.assertTrue(body.startswith("@eliorpom-cmd **Intake** failed while handling request #12."))
        self.assertIn("https://github.com/o/r/actions/runs/1", body)
        self.assertNotIn("request #", self.alert.alert_body("Sweep", "u", "", "o"))
        self.assertEqual(ats.records([{"author": {"login": "github-actions[bot]"}, "body": body}]), [])

    def test_one_alert_an_hour_per_workflow(self):
        now = 1_790_000_000
        recent = datetime_iso(now - 600)
        comments = [{"body": self.alert.alert_body("Harvest", "u", "", "o"), "createdAt": recent}]
        self.assertTrue(self.alert.recently_alerted(comments, "Harvest", now))
        self.assertFalse(self.alert.recently_alerted(comments, "Sweep", now))
        self.assertFalse(self.alert.recently_alerted(comments, "Harvest", now + 3600))

    def test_label_matches_ats(self):
        self.assertEqual(self.alert.LABEL, ats.ALERT_LABEL)
        with open(os.path.join(ROOT, "labels.json")) as f:
            self.assertIn(ats.ALERT_LABEL, [l["name"] for l in json.load(f)])


def datetime_iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    unittest.main()
