"""Application-side validation of Claude output (brain.merge_proposals,
brain.merge_new_rules, memory.apply_consolidation, engine.run_scan).

Every test here feeds malformed or hostile model output and asserts that it
cannot produce a destination outside the scanned directory / configured
targets, cannot crash the scan, and never modifies anything in the scanned
directory. Run: python3 -m unittest discover -s tests -v
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="organizer-test-")
os.environ["ORGANIZER_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["ORGANIZER_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["ORGANIZER_SOCKET"] = os.path.join(_TMP, "sock")

from organizer import brain, engine, paths  # noqa: E402
from organizer import memory as memmod  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "memory.json")


def load_seed():
    with open(SEED) as f:
        return memmod.normalize(json.load(f))


def entry(name, category=None, action="move", ext="pdf", age=10):
    return {"name": name, "is_dir": False, "size": 100, "size_h": "100B", "age_days": age,
            "untouched_days": age, "ext": ext, "mime": None, "signals": [], "category": category,
            "action": action, "target": None, "confidence": 0.4, "reasons": ["stage1"],
            "rule_id": None, "decided_by": "heuristic"}


def decide(cp, p=None, mem=None, cwd=None):
    p = p or entry(cp.get("name", "a.pdf"))
    mem = mem or load_seed()
    rej = brain.merge_proposals([p], {"proposals": [cp]}, mem, cwd)
    return p, rej


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class TearDown(unittest.TestCase):
    pass


class PathHelpers(unittest.TestCase):
    def test_relative_ok(self):
        for raw, want in [("Docs/Sub", "Docs/Sub"), ("Docs\\Sub", "Docs/Sub"), ("./a/./b/", "a/b"),
                          (" Images ", "Images"), ("a//b", "a/b")]:
            self.assertEqual(memmod.clean_relative_target(raw), want, raw)

    def test_relative_rejects(self):
        bad = ["", "   ", ".", "./", "..", "../x", "a/../b", "a/..", "/etc", "//etc", "\\etc", "~", "~/x",
               "~root", "C:\\x", "a\x00b", "a\nb", "\x1b[31m", None, 5, ["a"], "x" * 201, "..\\..\\x"]
        for raw in bad:
            self.assertIsNone(memmod.clean_relative_target(raw), repr(raw))

    def test_archive(self):
        self.assertEqual(memmod.clean_archive_target("_archive/2024"), "_archive/2024")
        self.assertEqual(memmod.clean_archive_target("_archive\\2024"), "_archive/2024")
        for raw in ["Documents", "_archive/../x", "/_archive/2024", "_archives/x", "", None]:
            self.assertIsNone(memmod.clean_archive_target(raw), repr(raw))

    def test_target_value(self):
        self.assertEqual(memmod.clean_target_value("~/Documents/Backups"), "~/Documents/Backups")
        self.assertEqual(memmod.clean_target_value("/mnt/backup/"), "/mnt/backup")
        for raw in ["~", "~/", "/", "~/../etc", "/etc/../root", "Documents", "~user/x", "~/a\\b", "", None]:
            self.assertIsNone(memmod.clean_target_value(raw), repr(raw))

    def test_move_to(self):
        mem = load_seed()
        mem["targets"] = {"backups": "~/Documents/Backups", "nas": "/mnt/nas/drop"}
        ok = memmod.clean_move_to_target
        self.assertEqual(ok("backups", mem), "~/Documents/Backups")
        self.assertEqual(ok("nas", mem), "/mnt/nas/drop")          # user-configured, honoured as-is
        self.assertEqual(ok("~/Documents/Backups", mem), "~/Documents/Backups")
        for raw in ["/etc", "/etc/cron.d", paths.HOME + "/x", "~/../../etc", "~/.", "~", "unknown-name",
                    "Documents/Backups", "../x", "", None, "~/x\x00"]:
            self.assertIsNone(ok(raw, mem), repr(raw))


class MergeProposals(TearDown):
    def assert_rejected(self, cp, msg=None, **kw):
        p, rej = decide(cp, **kw)
        self.assertIn(p["name"], rej, "expected rejection for %r" % (cp,))
        self.assertIn("rejected", rej[p["name"]])
        # the stage-1 proposal is untouched
        self.assertEqual(p["decided_by"], "heuristic")
        self.assertIsNone(p["target"])
        if msg:
            self.assertIn(msg, rej[p["name"]])
        return p, rej

    def test_move_traversal_and_absolute(self):
        for t in ["../../etc", "Docs/../../x", "/etc/cron.d", "\\etc", "..\\..\\x", "~/.ssh", "~", "C:\\Users"]:
            self.assert_rejected({"name": "a.pdf", "action": "move", "target": t, "confidence": 0.9, "reason": "x"})

    def test_move_empty_or_missing_target_without_category(self):
        for t in ["", " ", ".", "./", None]:
            self.assert_rejected({"name": "a.pdf", "action": "move", "target": t, "confidence": 0.9, "reason": "x"})

    def test_move_missing_target_falls_back_to_category_dir(self):
        p, rej = decide({"name": "a.pdf", "action": "move", "category": "documents", "confidence": 0.9, "reason": "r"})
        self.assertEqual(rej, {})
        self.assertEqual(p["target"], "Documents")
        self.assertEqual(p["decided_by"], "claude")

    def test_move_backslash_normalised(self):
        p, rej = decide({"name": "a.pdf", "action": "move", "target": "Documents\\Finance", "confidence": 0.9, "reason": "r"})
        self.assertEqual(rej, {})
        self.assertEqual(p["target"], "Documents/Finance")

    def test_move_control_chars(self):
        self.assert_rejected({"name": "a.pdf", "action": "move", "target": "Docs\x00/../x", "confidence": 0.9, "reason": "x"})
        self.assert_rejected({"name": "a.pdf", "action": "move", "target": "Docs\n../x", "confidence": 0.9, "reason": "x"})

    def test_move_through_symlink_escaping_cwd(self):
        cwd = tempfile.mkdtemp(dir=_TMP)
        outside = tempfile.mkdtemp(dir=_TMP)
        os.symlink(outside, os.path.join(cwd, "link"))
        self.assert_rejected({"name": "a.pdf", "action": "move", "target": "link/Docs", "confidence": 0.9, "reason": "x"},
                             "outside", cwd=cwd)
        p, rej = decide({"name": "a.pdf", "action": "move", "target": "Docs/New", "confidence": 0.9, "reason": "x"}, cwd=cwd)
        self.assertEqual(rej, {})

    def test_archive_targets(self):
        for t in ["../x", "/etc", "Documents", "_archive/../x", "~/_archive"]:
            self.assert_rejected({"name": "a.pdf", "action": "archive", "target": t, "confidence": 0.9, "reason": "x"})
        p, rej = decide({"name": "a.pdf", "action": "archive", "target": "_archive\\2024", "confidence": 0.9, "reason": "x"})
        self.assertEqual(p["target"], "_archive/2024")
        p, rej = decide({"name": "a.pdf", "action": "archive", "confidence": 0.9, "reason": "x"})
        self.assertRegex(p["target"], r"^_archive/\d{4}$")

    def test_move_to_targets(self):
        mem = load_seed()
        mem["targets"] = {"backups": "~/Documents/Backups"}
        for t in ["/etc", "/root/.ssh", paths.HOME + "/Documents", "~/../../etc", "~", "~/", "Documents",
                  "nope", "", None, "backups/../../etc"]:
            self.assert_rejected({"name": "a.pdf", "action": "move-to", "target": t, "confidence": 0.9, "reason": "x"}, mem=mem)
        p, _ = decide({"name": "a.pdf", "action": "move-to", "target": "backups", "confidence": 0.9, "reason": "x"}, mem=mem)
        self.assertEqual(p["target"], "~/Documents/Backups")
        p, _ = decide({"name": "a.pdf", "action": "move-to", "target": "~/Archive/Old", "confidence": 0.9, "reason": "x"}, mem=mem)
        self.assertEqual(p["target"], "~/Archive/Old")

    def test_non_move_actions_drop_target(self):
        for a in ("delete", "keep", "review"):
            p, rej = decide({"name": "a.pdf", "action": a, "target": "../../etc", "confidence": 0.9, "reason": "x"})
            self.assertEqual(rej, {})
            self.assertIsNone(p["target"])
            self.assertEqual(p["action"], a)

    def test_unknown_action(self):
        for a in ["execute", "rm", "", None, 3, ["move"]]:
            self.assert_rejected({"name": "a.pdf", "action": a, "target": "Docs", "confidence": 0.9, "reason": "x"})

    def test_bad_confidence(self):
        for c in ["high", None, True, float("nan"), float("inf"), [1]]:
            self.assert_rejected({"name": "a.pdf", "action": "move", "target": "Docs", "confidence": c, "reason": "x"})
        p, _ = decide({"name": "a.pdf", "action": "move", "target": "Docs", "confidence": 7, "reason": "x"})
        self.assertEqual(p["confidence"], 1.0)
        p, _ = decide({"name": "a.pdf", "action": "move", "target": "Docs", "confidence": -3, "reason": "x"})
        self.assertEqual(p["confidence"], 0.0)

    def test_bad_category(self):
        for c in ["../../x", "/abs", "a\x00b", "x" * 65, 5]:
            self.assert_rejected({"name": "a.pdf", "action": "move", "target": "Docs", "category": c, "confidence": 0.9, "reason": "x"})

    def test_reason_sanitised(self):
        p, _ = decide({"name": "a.pdf", "action": "move", "target": "Docs", "confidence": 0.9,
                       "reason": "line1\nline2\x1b[31m" + "z" * 1000})
        self.assertEqual(len(p["reasons"]), 1)
        self.assertLessEqual(len(p["reasons"][0]), len("claude: ") + brain.MAX_REASON_LEN)
        self.assertNotIn("\n", p["reasons"][0])
        self.assertNotIn("\x1b", p["reasons"][0])
        p, _ = decide({"name": "a.pdf", "action": "move", "target": "Docs", "confidence": 0.9, "reason": {"k": 1}})
        self.assertTrue(p["reasons"][0].startswith("claude: "))

    def test_unlisted_and_duplicate_names_ignored(self):
        a, b = entry("a.pdf"), entry("b.pdf")
        rej = brain.merge_proposals([a, b], {"proposals": [
            {"name": "c.pdf", "action": "delete", "confidence": 0.9, "reason": "x"},
            {"name": 5, "action": "delete", "confidence": 0.9, "reason": "x"},
            {"name": "a.pdf", "action": "move", "target": "Docs", "confidence": 0.9, "reason": "first"},
            {"name": "a.pdf", "action": "delete", "confidence": 0.9, "reason": "second"},
        ]}, load_seed())
        self.assertEqual(a["action"], "move")
        self.assertEqual(a["reasons"], ["claude: first"])
        self.assertEqual(set(rej), {"b.pdf"})
        self.assertNotIn("rejected", rej["b.pdf"])

    def test_malformed_container(self):
        mem = load_seed()
        for data in [{}, {"proposals": None}, {"proposals": "x"}, {"proposals": [None, 1, "s", []]}, [], None, "str"]:
            a = entry("a.pdf")
            rej = brain.merge_proposals([a], data, mem)
            self.assertEqual(set(rej), {"a.pdf"})
            self.assertEqual(a["decided_by"], "heuristic")

    def test_rejected_after_accepted_does_not_undo(self):
        a = entry("a.pdf")
        rej = brain.merge_proposals([a], {"proposals": [
            {"name": "a.pdf", "action": "move", "target": "Docs", "confidence": 0.9, "reason": "ok"},
            {"name": "a.pdf", "action": "move", "target": "../x", "confidence": 0.9, "reason": "evil"}]}, load_seed())
        self.assertEqual(rej, {})
        self.assertEqual(a["target"], "Docs")


class MergeNewRules(TearDown):
    def rules_after(self, new_rules, mem=None):
        mem = mem or load_seed()
        before = {r["id"] for r in mem["rules"]}
        added = brain.merge_new_rules({"new_rules": new_rules}, mem)
        self.assertEqual(memmod.validate(mem), [], "memory must stay valid")
        return added, mem, before

    def test_traversal_in_category_and_dir(self):
        added, mem, _ = self.rules_after([
            {"glob": "Foo-*.pdf", "action": "move", "category": "../../evil", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move", "category": "docs/x", "category_dir": "/etc", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move", "category": "docs/x", "category_dir": "../up", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move", "category": "docs/x", "category_dir": "~/x", "confidence": 0.8, "note": "n"},
        ])
        self.assertEqual(added, [])
        self.assertNotIn("../../evil", mem["categories"])
        self.assertNotIn("docs/x", mem["categories"])

    def test_bad_targets(self):
        added, mem, _ = self.rules_after([
            {"glob": "Foo-*.pdf", "action": "move", "target": "../x", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move", "target": "/etc", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move-to", "target": "/etc", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move-to", "target": "~/../../etc", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move-to", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "archive", "target": "Docs", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "move", "confidence": 0.8, "note": "no target, no category"},
        ])
        self.assertEqual(added, [])

    def test_bad_match(self):
        added, mem, _ = self.rules_after([
            {"glob": "*.pdf", "action": "delete", "confidence": 0.8, "note": "broad"},
            {"glob": "*", "action": "delete", "confidence": 0.8, "note": "broad"},
            {"glob": ["a*"], "action": "delete", "confidence": 0.8, "note": "type"},
            {"regex": "(", "action": "delete", "confidence": 0.8, "note": "bad regex"},
            {"regex": 5, "action": "delete", "confidence": 0.8, "note": "type"},
            {"action": "delete", "confidence": 0.8, "note": "no match"},
            {"glob": "x" * 200, "action": "delete", "confidence": 0.8, "note": "long"},
            "not a dict", None,
        ])
        self.assertEqual(added, [])

    def test_bad_action_and_confidence(self):
        added, mem, _ = self.rules_after([
            {"glob": "Foo-*.pdf", "action": "execute", "confidence": 0.8, "note": "n"},
            {"glob": "Foo-*.pdf", "action": "delete", "confidence": "high", "note": "n"},
            {"glob": "Foo-*.pdf", "action": "delete", "confidence": float("nan"), "note": "n"},
        ])
        self.assertEqual(added, [])

    def test_good_rules_accepted_and_normalised(self):
        mem = load_seed()
        mem["targets"] = {"backups": "~/Documents/Backups"}
        added, mem, before = self.rules_after([
            {"glob": "statement-*.pdf", "action": "move", "category": "documents/finance",
             "category_dir": "Documents\\Finance", "confidence": 2, "note": "n\nn"},
            {"glob": "device-backup*.zip", "action": "move-to", "target": "backups", "confidence": 0.8, "note": "n"},
            {"glob": "old-*.log", "action": "archive", "target": "_archive\\2020", "confidence": 0.3, "note": "n"},
        ], mem)
        self.assertEqual(len(added), 3)
        self.assertEqual(mem["categories"]["documents/finance"]["dir"], "Documents/Finance")
        by = {r["id"]: r for r in mem["rules"]}
        self.assertEqual(by[added[0]]["confidence"], 0.95)
        self.assertEqual(by[added[0]]["note"], "n n")
        self.assertEqual(by[added[1]]["target"], "~/Documents/Backups")
        self.assertEqual(by[added[2]]["target"], "_archive/2020")
        self.assertEqual(by[added[2]]["confidence"], 0.5)

    def test_regex_rules_rejected_even_when_valid(self):
        # Policy: model-generated rules are glob-only. A regex is refused whether it is
        # safe, hostile, or accompanied by a perfectly good glob; the user-only regex
        # key never enters memory through new_rules.
        added, mem, before = self.rules_after([
            {"regex": r"^statement-.*\.pdf$", "action": "keep", "confidence": 0.8, "note": "safe"},
            {"regex": r"(a+)+$", "action": "delete", "confidence": 0.8, "note": "backtracking"},
            {"regex": r"^(a|aa)+$", "action": "delete", "confidence": 0.8, "note": "not caught by the heuristic"},
            {"regex": "", "action": "keep", "confidence": 0.8, "note": "empty"},
            {"regex": None, "action": "keep", "confidence": 0.8, "note": "null"},
            {"glob": "statement-*.pdf", "regex": r"^statement", "action": "keep", "confidence": 0.8, "note": "both"},
        ])
        self.assertEqual(added, [])
        self.assertEqual({r["id"] for r in mem["rules"]}, before)
        self.assertFalse(any("regex" in r["match"] for r in mem["rules"] if r["source"] == "claude"))
        log = []
        brain.merge_new_rules({"new_rules": [{"regex": "^x", "action": "keep", "confidence": 0.8, "note": "n"}]},
                              load_seed(), log=log.append)
        self.assertTrue(any("regex rules are user-only" in m for m in log), log)

    def test_glob_rules_still_accepted(self):
        added, mem, _ = self.rules_after([
            {"glob": "statement-*.pdf", "action": "move", "category": "documents", "confidence": 0.8, "note": "n"},
            {"glob": "device-backup-*.zip", "action": "archive", "confidence": 0.8, "note": "n"},
        ])
        self.assertEqual(len(added), 2)
        by = {r["id"]: r for r in mem["rules"]}
        self.assertEqual(by[added[0]]["match"], {"glob": "statement-*.pdf"})
        self.assertEqual(by[added[1]]["match"], {"glob": "device-backup-*.zip"})
        self.assertEqual(by[added[0]]["source"], "claude")
        # the learned glob rule actually classifies (glob is case-insensitive)
        self.assertTrue(memmod.rule_matches(by[added[0]], {"name": "statement-2026.pdf", "ext": "pdf"}))
        self.assertFalse(memmod.rule_matches(by[added[0]], {"name": "notes.pdf", "ext": "pdf"}))

    def test_schema_no_longer_offers_regex(self):
        props = brain.PROPOSAL_SCHEMA["properties"]["new_rules"]["items"]["properties"]
        self.assertIn("glob", props)
        self.assertNotIn("regex", props)

    def test_limits(self):
        many = [{"glob": "Rule%d-*.pdf" % i, "action": "keep", "confidence": 0.8, "note": "n"} for i in range(9)]
        added, mem, _ = self.rules_after(many)
        self.assertEqual(len(added), 5)
        mem = load_seed()
        brain.merge_new_rules({"new_rules": "x", "memory_notes": "a\x00b\n" + "n" * 5000}, mem)
        self.assertLessEqual(len(mem["claude_notes"]), 2000)
        self.assertNotIn("\x00", mem["claude_notes"])


class Consolidation(TearDown):
    def rewrite(self, mem):
        return {"rules": copy.deepcopy(mem["rules"]), "categories": copy.deepcopy(mem["categories"]),
                "targets": dict(mem["targets"]), "claude_notes": "", "rationale": "r"}

    def test_new_target_outside_home_rejected(self):
        mem = load_seed()
        rw = self.rewrite(mem)
        rw["targets"]["evil"] = "/etc/cron.d"
        self.assertTrue(memmod.apply_consolidation(mem, rw))
        self.assertNotIn("evil", mem["targets"])

    def test_existing_target_kept_new_home_target_ok(self):
        mem = load_seed()
        mem["targets"] = {"nas": "/mnt/nas/drop"}
        rw = self.rewrite(mem)
        rw["targets"]["arch"] = "~/Archive"
        self.assertEqual(memmod.apply_consolidation(mem, rw), [])
        self.assertEqual(mem["targets"], {"nas": "/mnt/nas/drop", "arch": "~/Archive"})
        # but a *changed* value for an existing name must be under ~
        rw = self.rewrite(mem)
        rw["targets"]["nas"] = "/etc"
        self.assertTrue(memmod.apply_consolidation(mem, rw))

    def test_category_dir_traversal_rejected(self):
        mem = load_seed()
        rw = self.rewrite(mem)
        rw["categories"]["x"] = {"dir": "../../x", "confidence": 0.8}
        self.assertTrue(memmod.apply_consolidation(mem, rw))
        rw = self.rewrite(mem)
        rw["categories"]["../y"] = {"dir": "Y", "confidence": 0.8}
        self.assertTrue(memmod.apply_consolidation(mem, rw))

    def test_rule_target_traversal_rejected(self):
        mem = load_seed()
        rw = self.rewrite(mem)
        rw["rules"][0]["target"] = "../../etc"
        self.assertTrue(memmod.apply_consolidation(mem, rw))
        rw = self.rewrite(mem)
        rw["rules"][0]["action"] = "move-to"
        rw["rules"][0]["target"] = "/etc"
        self.assertTrue(memmod.apply_consolidation(mem, rw))

    def user_regex_rule(self, rid="r-user-rx", rx=r"^invoice-\d{4}\.pdf$"):
        return {"id": rid, "match": {"regex": rx}, "action": "keep", "scope": "global",
                "confidence": 0.9, "hits": 0, "contradictions": 0, "source": "user", "note": "hand-written"}

    def test_new_regex_rule_rejected(self):
        # A rewrite may not add a regex rule, safe or hostile. Memory is untouched.
        mem = load_seed()
        for rx in [r"^statement-.*\.pdf$", r"(a+)+$", r"^(a|aa)+$", r"(.*a){20}b", "(", "a" * 201]:
            rw = self.rewrite(mem)
            rw["rules"].append({"id": "r-new-rx", "match": {"regex": rx}, "action": "delete", "confidence": 0.9,
                                "source": "claude"})
            snapshot = copy.deepcopy(mem)
            probs = memmod.apply_consolidation(mem, rw)
            self.assertTrue(probs, rx)
            self.assertTrue(any("regex" in p for p in probs), probs)
            self.assertEqual(mem, snapshot)
            self.assertFalse(any("regex" in r["match"] for r in mem["rules"]))

    def test_regex_added_to_existing_glob_rule_rejected(self):
        mem = load_seed()
        rw = self.rewrite(mem)
        rw["rules"][0]["match"] = dict(rw["rules"][0]["match"], regex=r"^(a|aa)+$")
        snapshot = copy.deepcopy(mem)
        probs = memmod.apply_consolidation(mem, rw)
        self.assertTrue(any("regex rules are user-only" in p for p in probs), probs)
        self.assertEqual(mem, snapshot)

    def test_user_regex_rule_kept_verbatim_ok_but_not_edited(self):
        mem = load_seed()
        mem["rules"].append(self.user_regex_rule())
        self.assertEqual(memmod.validate(mem), [])
        # kept verbatim (even with a new id / bumped confidence): fine
        rw = self.rewrite(mem)
        rw["rules"][-1]["confidence"] = 0.95
        self.assertEqual(memmod.apply_consolidation(mem, rw), [])
        self.assertEqual(mem["rules"][-1]["match"], {"regex": r"^invoice-\d{4}\.pdf$"})
        self.assertEqual(mem["rules"][-1]["confidence"], 0.95)
        # the pattern itself edited, even to something harmless: rejected
        rw = self.rewrite(mem)
        rw["rules"][-1]["match"]["regex"] = r"^invoice-\d{4}\.(pdf|txt)$"
        self.assertTrue(any("regex" in p for p in memmod.apply_consolidation(mem, rw)))
        self.assertEqual(mem["rules"][-1]["match"], {"regex": r"^invoice-\d{4}\.pdf$"})
        # dropped: fine (the 50 % rule permits losing one)
        rw = self.rewrite(mem)
        rw["rules"] = [r for r in rw["rules"] if r["id"] != "r-user-rx"]
        self.assertEqual(memmod.apply_consolidation(mem, rw), [])
        self.assertFalse(any("regex" in r["match"] for r in mem["rules"]))
        # once dropped it cannot come back through a later rewrite
        rw = self.rewrite(mem)
        rw["rules"].append(self.user_regex_rule())
        self.assertTrue(memmod.apply_consolidation(mem, rw))

    def test_malformed_shapes_do_not_crash(self):
        mem = load_seed()
        for bad in [{"rules": "x"}, {"rules": [1, 2]}, {"categories": []}, {"targets": "s"}]:
            rw = self.rewrite(mem)
            rw.update(bad)
            snapshot = copy.deepcopy(mem)
            self.assertTrue(memmod.apply_consolidation(mem, rw))
            self.assertEqual(mem, snapshot)


class ValidateMemory(TearDown):
    def test_seed_is_valid(self):
        self.assertEqual(memmod.validate(load_seed()), [])

    def test_bad_paths_flagged(self):
        mem = load_seed()
        mem["categories"]["bad"] = {"dir": "/etc"}
        mem["targets"]["t"] = "~/../x"
        mem["rules"].append({"id": "r-x", "match": {"glob": "Foo-*"}, "action": "move", "target": "../up",
                             "confidence": 0.5, "source": "user"})
        mem["rules"].append({"id": "r-y", "match": {"glob": "Foo-*"}, "action": "archive", "target": "Docs",
                             "confidence": 0.5, "source": "user"})
        mem["rules"].append({"id": "r-z", "match": {"glob": 5}, "action": "keep", "confidence": True, "source": "user"})
        errs = "\n".join(memmod.validate(mem))
        for frag in ["categories['bad'].dir", "targets['t']", "rules[", "'../up'", "archive target", "glob must",
                     "confidence must"]:
            self.assertIn(frag, errs)

    def test_user_regex_rules_remain_supported(self):
        # Hand-written regex rules are part of the memory format: they validate,
        # survive save/load and classify. Only the model may not write them.
        mem = load_seed()
        mem["rules"].append({"id": "r-rx", "match": {"regex": r"^screenshot[ _-]\d+\.png$"}, "action": "move",
                             "target": "Images/Screenshots", "confidence": 0.9, "source": "user"})
        self.assertEqual(memmod.validate(mem), [])
        path = os.path.join(_TMP, "user-regex.json")
        memmod.save(mem, path, backup=False)
        loaded = memmod.load(path)
        rule = memmod.find_rule(loaded, "r-rx")
        self.assertEqual(rule["match"], {"regex": r"^screenshot[ _-]\d+\.png$"})
        self.assertTrue(memmod.rule_matches(rule, {"name": "Screenshot_2026.png", "ext": "png"}))
        self.assertFalse(memmod.rule_matches(rule, {"name": "photo.png", "ext": "png"}))
        # and the pre-existing safety checks still apply to hand-written ones
        mem["rules"][-1]["match"] = {"regex": "(a+)+$"}
        self.assertTrue(any("backtracking" in e for e in memmod.validate(mem)))

    def test_bad_types_flagged_not_crashing(self):
        # counters feed int() arithmetic, note/category feed string ops and dict lookups:
        # every one of these used to pass validate() and crash a later scan (or validate itself)
        mem = load_seed()
        mem["rules"].append({"id": ["r-list"], "match": {"glob": "Foo-*"}, "action": "keep", "confidence": 0.5,
                             "source": "user", "hits": "many", "contradictions": True, "note": ["x"],
                             "category": ["documents"]})
        errs = "\n".join(memmod.validate(mem))
        for frag in ["string id", "hits must", "contradictions must", "note must", "category must"]:
            self.assertIn(frag, errs)


class EngineIntegration(TearDown):
    """A hostile model reply through the real pipeline: nothing escapes, nothing is touched."""

    def setUp(self):
        self.cwd = tempfile.mkdtemp(dir=_TMP)
        outside = tempfile.mkdtemp(dir=_TMP)
        os.symlink(outside, os.path.join(self.cwd, "link"))
        for n in ("a.pdf", "b.xyz", "c.pdf", "d.xyz", "e.xyz"):
            with open(os.path.join(self.cwd, n), "w") as f:
                f.write("x")
        paths.ensure_dirs()
        with open(SEED) as f:
            mem = json.load(f)
        mem["settings"]["ai_threshold"] = 0.99   # force every entry to the Claude stage
        memmod.save(mem, paths.MEMORY_PATH, backup=False)
        self.store = memmod.MemoryStore()
        self.state = engine.load_state()
        self.state["dirs"] = {}
        self.state["ai_decisions"] = {}

    def snapshot(self):
        out = {}
        for root, dirs, files in os.walk(self.cwd):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
        return out

    def test_hostile_reply(self):
        reply = {"proposals": [
            {"name": "a.pdf", "action": "move", "target": "../../etc/cron.d", "confidence": 0.95, "reason": "x"},
            {"name": "b.xyz", "action": "move-to", "target": "/root", "confidence": 0.95, "reason": "x"},
            {"name": "c.pdf", "action": "move", "target": "link/out", "confidence": 0.95, "reason": "x"},
            {"name": "d.xyz", "action": "archive", "target": "~/.ssh", "confidence": 0.95, "reason": "x"},
            {"name": "e.xyz", "action": "move", "target": "Misc\\Stuff", "confidence": 0.95, "reason": "fine"},
        ], "new_rules": [
            {"glob": "b*.xyz", "action": "move", "category": "../../pwn", "confidence": 0.9, "note": "n"},
            {"regex": "^(a|aa)+$", "action": "delete", "confidence": 0.9, "note": "regex: user-only key"},
        ]}
        before = self.snapshot()
        with mock.patch.object(brain, "propose", return_value=reply):
            report = engine.run_scan(self.cwd, {}, self.store, self.state, log=lambda m: None)
        self.assertEqual(self.snapshot(), before, "scanned directory must be untouched")
        by = {p["name"]: p for p in report["proposals"]}
        for n in ("a.pdf", "b.xyz", "c.pdf", "d.xyz"):
            self.assertEqual(by[n]["action"], "review", n)
            self.assertIsNone(by[n]["target"])
            self.assertTrue(any("rejected" in r for r in by[n]["reasons"]), by[n]["reasons"])
        self.assertEqual(by["e.xyz"]["action"], "move")
        self.assertEqual(by["e.xyz"]["target"], "Misc/Stuff")
        self.assertEqual(report["ai"]["new_rules"], [])
        self.assertFalse(any("regex" in r["match"] for r in memmod.load(paths.MEMORY_PATH)["rules"]))
        for tgt in list(report["structure"]) + report["summary"]["new_dirs"]:
            self.assertFalse(tgt.startswith(("/", "~")) or ".." in tgt.split("/"), tgt)
        # only the validated decision was cached; the rest are not
        cache = self.state["ai_decisions"][self.cwd]
        self.assertEqual(set(cache), {"e.xyz"})
        self.assertEqual(memmod.validate(self.store.get()), [])

    def test_poisoned_cache_is_purged(self):
        self.state["ai_decisions"][self.cwd] = {
            "a.pdf": {"key": None, "ts": 0, "decision": {"name": "a.pdf", "action": "move", "target": "../x",
                                                       "confidence": 0.9, "reason": "old"}}}
        # give it a matching key + fresh ts so it would be replayed
        from organizer.scanner import scan_dir
        facts = {e["name"]: e for e in scan_dir(self.cwd, self.store.get()["settings"])["entries"]}
        import time
        self.state["ai_decisions"][self.cwd]["a.pdf"].update(key=engine._entry_key(facts["a.pdf"]), ts=int(time.time()))
        with mock.patch.object(brain, "propose", return_value={"proposals": []}):
            report = engine.run_scan(self.cwd, {}, self.store, self.state, log=lambda m: None)
        by = {p["name"]: p for p in report["proposals"]}
        self.assertEqual(by["a.pdf"]["action"], "review")
        self.assertNotIn("a.pdf", self.state["ai_decisions"].get(self.cwd, {}))

    def test_consolidation_cannot_smuggle_regex_into_memory_file(self):
        mem = self.store.get()
        mem["learned"]["pending"].append({"name": "x.pdf", "proposed": {"action": "move", "target": "Docs"},
                                          "observed": "moved_elsewhere:Other", "ts": 1})
        self.store.commit()
        on_disk = memmod.load(paths.MEMORY_PATH)
        rewrite = {"rules": copy.deepcopy(mem["rules"]) + [
                       {"id": "r-evil", "match": {"regex": "^(a|aa)+$"}, "action": "delete", "confidence": 0.9,
                        "source": "claude"}],
                   "categories": copy.deepcopy(mem["categories"]), "targets": dict(mem["targets"]),
                   "claude_notes": "n", "rationale": "r"}
        with mock.patch.object(brain, "consolidate", return_value=rewrite):
            res = engine.consolidate(self.store, None, dry_run=False, log=lambda m: None)
        self.assertFalse(res["applied"], res)
        self.assertIn("regex", res["reason"])
        self.assertEqual(memmod.load(paths.MEMORY_PATH), on_disk, "memory.json must be untouched")
        self.assertEqual(len(self.store.get()["learned"]["pending"]), 1, "outcomes stay pending")

    def test_brain_error_and_garbage_reply(self):
        with mock.patch.object(brain, "propose", return_value="garbage"):
            report = engine.run_scan(self.cwd, {}, self.store, self.state, log=lambda m: None)
        self.assertTrue(all(p["action"] == "review" for p in report["proposals"]))


if __name__ == "__main__":
    unittest.main()
