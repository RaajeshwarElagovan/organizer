"""Adversarial tests for the full learning lifecycle (layer 1 + layer 2).

Every test drives the real pipeline (engine.run_scan / engine.consolidate /
memory.MemoryStore) against a throw-away directory with Claude mocked out, and
checks three things: the *observed outcome* recorded in learned.pending is the
documented one (MEMORY-GUIDE.md "How learning changes this file"), the rule
counters move exactly as documented (+0.05 confirm, -0.10 contradiction,
-0.05 ignored, floors 0.5/0.2, cap 0.99), and the scanned directory is never
touched. Run: PYTHONPATH=. python3 -m unittest tests.test_learning -v
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="organizer-learn-")
os.environ.setdefault("ORGANIZER_CONFIG_DIR", os.path.join(_TMP, "config"))
os.environ.setdefault("ORGANIZER_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("ORGANIZER_SOCKET", os.path.join(_TMP, "sock"))

from organizer import brain, classifier, engine, paths  # noqa: E402
from organizer import memory as memmod  # noqa: E402

DAY = 86400
YEAR = time.strftime("%Y")


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def make_mem(nas_dir):
    """Small memory whose rules cover every action that can be confirmed/contradicted."""
    return {
        "version": 1,
        "settings": {"ai_enabled": False, "ai_threshold": 0.7, "use_magic": False},
        "categories": {
            "documents": {"dir": "Documents", "ext": ["pdf", "txt"], "confidence": 0.9},
            "documents/finance": {"dir": "Documents/Finance", "confidence": 0.8},
            "archives": {"dir": "Archives", "ext": ["zip"], "confidence": 0.9},
        },
        "targets": {"nas": nas_dir},
        "rules": [
            {"id": "r-bill", "match": {"glob": "Bill*.pdf"}, "category": "documents/finance", "action": "move",
             "scope": "global", "confidence": 0.85, "source": "claude", "note": "Bills"},
            {"id": "r-backup", "match": {"glob": "*backup*.zip"}, "action": "move-to", "target": "nas",
             "scope": "global", "confidence": 0.9, "source": "user"},
            {"id": "r-oldlog", "match": {"glob": "old-*.log"}, "action": "delete",
             "scope": "global", "confidence": 0.9, "source": "seed"},
            {"id": "r-report", "match": {"glob": "report-*.txt"}, "action": "archive",
             "scope": "global", "confidence": 0.9, "source": "user"},
        ],
        "dir_overrides": {},
        "learned": {"pending": [], "confirmed": []},
        "claude_notes": "",
    }


def good_rewrite(mem, **over):
    rw = {"rules": copy.deepcopy(mem["rules"]), "categories": copy.deepcopy(mem["categories"]),
          "targets": dict(mem["targets"]), "claude_notes": "notes", "rationale": "test"}
    rw.update(over)
    return rw


def tree_snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


class Fixture(unittest.TestCase):
    """Isolated config/data dirs, a victim directory, a fake NAS target, a frozen clock."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=_TMP)
        self.cwd = os.path.join(self.root, "victim")
        self.nas = os.path.join(self.root, "nas")
        os.makedirs(self.cwd)
        os.makedirs(self.nas)
        cfg, data = os.path.join(self.root, "config"), os.path.join(self.root, "data")
        p = mock.patch.multiple(paths, CONFIG_DIR=cfg, DATA_DIR=data,
                                MEMORY_PATH=os.path.join(cfg, "memory.json"),
                                STATE_PATH=os.path.join(data, "state.json"),
                                REPORTS_DIR=os.path.join(data, "reports"))
        p.start()
        self.addCleanup(p.stop)
        paths.ensure_dirs()
        memmod.save(make_mem(self.nas), paths.MEMORY_PATH, backup=False)
        self.store = memmod.MemoryStore(paths.MEMORY_PATH)
        self.state = engine.load_state()
        self.now = time.time()
        self.logs = []

    # -- the "user" ----------------------------------------------------------------
    def touch(self, name, size=1):
        with open(os.path.join(self.cwd, name), "w") as f:
            f.write("x" * size)

    def user_move(self, name, dest_dir):
        """dest_dir relative to cwd, or absolute."""
        d = dest_dir if os.path.isabs(dest_dir) else os.path.join(self.cwd, dest_dir)
        os.makedirs(d, exist_ok=True)
        os.rename(os.path.join(self.cwd, name), os.path.join(d, name))

    def user_delete(self, name):
        os.remove(os.path.join(self.cwd, name))

    def user_rename(self, old, new):
        os.rename(os.path.join(self.cwd, old), os.path.join(self.cwd, new))

    def advance(self, days):
        self.now += days * DAY

    # -- organizer -----------------------------------------------------------------
    def scan(self, **opts):
        opts.setdefault("no_ai", True)
        before = tree_snapshot(self.cwd)
        with mock.patch("time.time", return_value=self.now):
            report = engine.run_scan(self.cwd, opts, self.store, self.state, log=self.logs.append)
        self.assertEqual(tree_snapshot(self.cwd), before, "a scan modified the scanned directory")
        return report

    def rule(self, rid):
        r = memmod.find_rule(self.store.get(), rid)
        self.assertIsNotNone(r, rid)
        return r

    def pending(self):
        return self.store.get()["learned"]["pending"]

    def observed(self):
        return [(o["name"], o["observed"]) for o in self.pending()]

    def proposal(self, report, name):
        return next(p for p in report["proposals"] if p["name"] == name)

    def tracked(self, name):
        return self.state["dirs"][self.cwd]["proposals"][name]

    def assertCounters(self, rid, confidence, hits=0, contradictions=0):
        r = self.rule(rid)
        self.assertAlmostEqual(r["confidence"], confidence, places=3, msg=rid)
        self.assertEqual(r.get("hits", 0), hits, rid)
        self.assertEqual(r.get("contradictions", 0), contradictions, rid)


# ================================================================ layer 1: outcomes

class Confirmations(Fixture):
    """1. A file ends up at the proposed destination -> confirmed, +0.05, hits+1."""

    def test_move_to_category_dir(self):
        self.touch("Bill-jan.pdf")
        r1 = self.scan()
        self.assertEqual(self.proposal(r1, "Bill-jan.pdf")["target"], "Documents/Finance")
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "confirmed")])
        o = self.pending()[0]
        self.assertEqual(o["proposed"], {"action": "move", "target": "Documents/Finance", "rule_id": "r-bill",
                                         "decided_by": "rule", "category": "documents/finance"})
        self.assertCounters("r-bill", 0.9, hits=1)
        # a confirmed file is gone from the listing: it is not tracked any more, so it
        # cannot be confirmed twice
        self.scan()
        self.assertEqual(len(self.pending()), 1)
        self.assertCounters("r-bill", 0.9, hits=1)

    def test_move_to_configured_target(self):
        self.touch("phone-backup-1.zip")
        r1 = self.scan()
        self.assertEqual(self.proposal(r1, "phone-backup-1.zip")["target"], self.nas)
        self.user_move("phone-backup-1.zip", self.nas)
        self.scan()
        self.assertEqual(self.observed(), [("phone-backup-1.zip", "confirmed")])
        self.assertCounters("r-backup", 0.95, hits=1)

    def test_archive(self):
        self.touch("report-q1.txt")
        r1 = self.scan()
        self.assertEqual(self.proposal(r1, "report-q1.txt")["target"], "_archive/" + YEAR)
        self.user_move("report-q1.txt", "_archive/" + YEAR)
        self.scan()
        self.assertEqual(self.observed(), [("report-q1.txt", "confirmed")])
        self.assertCounters("r-report", 0.95, hits=1)

    def test_delete(self):
        self.touch("old-1.log")
        self.scan()
        self.user_delete("old-1.log")
        self.scan()
        self.assertEqual(self.observed(), [("old-1.log", "confirmed")])
        self.assertCounters("r-oldlog", 0.95, hits=1)

    def test_confirmation_is_the_rule_that_proposed_not_any_matching_rule(self):
        # category-table decisions carry rule_id None: no rule counter may move
        self.touch("notes.pdf")
        r1 = self.scan()
        p = self.proposal(r1, "notes.pdf")
        self.assertEqual((p["action"], p["target"], p["rule_id"]), ("move", "Documents", None))
        self.user_move("notes.pdf", "Documents")
        self.scan()
        self.assertEqual(self.observed(), [("notes.pdf", "confirmed")])
        self.assertIsNone(self.pending()[0]["proposed"]["rule_id"])
        for r in self.store.get()["rules"]:
            self.assertEqual((r["hits"], r["contradictions"]), (0, 0), r["id"])
        self.assertCounters("r-bill", 0.85)

    def test_confidence_cap(self):
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["confidence"] = 0.97
        self.store.commit()
        self.touch("Bill-a.pdf")
        self.scan()
        self.user_move("Bill-a.pdf", "Documents/Finance")
        self.scan()
        self.assertCounters("r-bill", 0.99, hits=1)


class Contradictions(Fixture):
    """2. moved elsewhere / 5. deleted -> contradiction, -0.10, contradictions+1."""

    def test_moved_elsewhere_records_where(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_move("Bill-jan.pdf", "Documents")          # parent of the proposed target
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "moved_elsewhere:Documents")])
        self.assertCounters("r-bill", 0.75, contradictions=1)

    def test_moved_into_nested_folder(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_move("Bill-jan.pdf", "Bills/Paid")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "moved_elsewhere:Bills/Paid")])

    def test_moved_to_a_configured_target_instead(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_move("Bill-jan.pdf", self.nas)
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "moved_elsewhere:" + self.nas)])
        self.assertCounters("r-bill", 0.75, contradictions=1)

    def test_moved_deeper_than_the_search_looks_counts_as_deleted(self):
        # Documented limitation of _find_elsewhere: two levels under cwd + targets.
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_move("Bill-jan.pdf", "a/b/c")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "deleted")])
        self.assertCounters("r-bill", 0.75, contradictions=1)

    def test_deleted_instead_of_moved(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_delete("Bill-jan.pdf")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "deleted")])
        self.assertCounters("r-bill", 0.75, contradictions=1)

    def test_deleted_instead_of_archived_or_moved_to(self):
        self.touch("report-q1.txt")
        self.touch("db-backup.zip")
        self.scan()
        self.user_delete("report-q1.txt")
        self.user_delete("db-backup.zip")
        self.scan()
        self.assertEqual(sorted(self.observed()), [("db-backup.zip", "deleted"), ("report-q1.txt", "deleted")])
        self.assertCounters("r-report", 0.8, contradictions=1)
        self.assertCounters("r-backup", 0.8, contradictions=1)

    def test_delete_proposal_but_user_kept_the_file_elsewhere(self):
        # A `delete` proposal is only confirmed if the file is really gone. If the user
        # instead filed it away, that is a contradiction, not a confirmation.
        self.touch("old-1.log")
        self.scan()
        self.user_move("old-1.log", "Keep")
        self.scan()
        self.assertEqual(self.observed(), [("old-1.log", "moved_elsewhere:Keep")])
        self.assertCounters("r-oldlog", 0.8, contradictions=1)

    def test_heuristic_contradiction_touches_no_rule(self):
        self.touch("notes.pdf")
        self.scan()
        self.user_move("notes.pdf", "Elsewhere")
        self.scan()
        self.assertEqual(self.observed(), [("notes.pdf", "moved_elsewhere:Elsewhere")])
        for r in self.store.get()["rules"]:
            self.assertEqual(r["contradictions"], 0, r["id"])

    def test_unreadable_subdir_does_not_break_detection(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.touch("Bill-jan.pdf")
        self.scan()
        locked = os.path.join(self.cwd, "locked")
        os.mkdir(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        self.user_delete("Bill-jan.pdf")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "deleted")])


class Untouched(Fixture):
    """3./4. Leaving a file in place is not a rejection until BOTH ignored_after_scans
    and ignored_after_days have passed; then `ignored` is recorded exactly once."""

    def test_one_scan_later_nothing_is_learned(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.scan()
        self.assertEqual(self.pending(), [])
        self.assertCounters("r-bill", 0.85)
        self.assertEqual(self.tracked("Bill-jan.pdf")["seen_scans"], 1)

    def test_many_scans_on_the_same_day_are_not_a_rejection(self):
        self.touch("Bill-jan.pdf")
        for _ in range(8):
            self.scan()
        self.assertEqual(self.pending(), [])
        self.assertCounters("r-bill", 0.85)
        self.assertEqual(self.tracked("Bill-jan.pdf")["seen_scans"], 7)
        self.assertFalse(self.tracked("Bill-jan.pdf")["ignored_recorded"])

    def test_days_alone_are_not_a_rejection(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.advance(10)
        self.scan()                                    # seen_scans == 1 < 3
        self.assertEqual(self.pending(), [])
        self.assertCounters("r-bill", 0.85)

    def test_scans_and_days_record_ignored_once(self):
        self.touch("Bill-jan.pdf")
        self.scan()                                    # proposal made, first_ts = t0
        for day in (1, 2):
            self.advance(1)
            self.scan()
            self.assertEqual(self.pending(), [], "day %d" % day)
        self.advance(1)                                # t0 + 3d, third scan since proposal
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "ignored")])
        self.assertCounters("r-bill", 0.8)              # -0.05, no contradiction counted
        first_ts = self.tracked("Bill-jan.pdf")["first_ts"]
        for _ in range(4):
            self.advance(1)
            self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "ignored")], "ignored must be recorded once")
        self.assertCounters("r-bill", 0.8)
        self.assertEqual(self.tracked("Bill-jan.pdf")["first_ts"], first_ts)

    def test_ignored_then_acted_on(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        for _ in range(3):
            self.advance(1)
            self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "ignored")])
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "ignored"), ("Bill-jan.pdf", "confirmed")])
        self.assertCounters("r-bill", 0.85, hits=1)

    def test_changed_proposal_restarts_the_clock(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.advance(1)
        self.scan()
        self.assertEqual(self.tracked("Bill-jan.pdf")["seen_scans"], 1)
        # the user retargets the rule by editing memory.json: the proposal changes
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["category"] = "documents"
        self.store.commit()
        self.advance(1)
        r = self.scan()
        self.assertEqual(self.proposal(r, "Bill-jan.pdf")["target"], "Documents")
        t = self.tracked("Bill-jan.pdf")
        self.assertEqual(t["seen_scans"], 0)
        self.assertEqual(t["first_ts"], int(self.now))
        for _ in range(3):
            self.advance(1)
            self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "ignored")])

    def test_ignored_uses_the_rule_floor(self):
        mem = self.store.get()
        mem["settings"]["ai_threshold"] = 0.5                 # keep the rule decided without Claude
        memmod.find_rule(mem, "r-bill")["confidence"] = 0.52
        self.store.commit()
        self.touch("Bill-jan.pdf")
        self.scan()
        for _ in range(3):
            self.advance(1)
            self.scan()
        self.assertCounters("r-bill", 0.5)              # floor for source: claude


class Unknown(Fixture):
    """Proposals that are `keep`/`review`, or files never proposed, teach nothing."""

    def test_review_and_keep_vanishing_is_not_an_outcome(self):
        self.touch("mystery.xyz")                      # no rule, no category -> review
        self.touch("pinned.pdf")
        mem = self.store.get()
        mem["dir_overrides"][self.cwd] = {"ignore": ["pinned.pdf"]}
        self.store.commit()
        r = self.scan()
        self.assertEqual(self.proposal(r, "mystery.xyz")["action"], "review")
        self.assertEqual(self.proposal(r, "pinned.pdf")["action"], "keep")
        self.user_delete("mystery.xyz")
        self.user_move("pinned.pdf", "Documents")
        self.scan()
        self.assertEqual(self.pending(), [])

    def test_review_never_becomes_ignored(self):
        self.touch("mystery.xyz")
        self.scan()
        for _ in range(5):
            self.advance(1)
            self.scan()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.tracked("mystery.xyz")["seen_scans"], 0, "review proposals are not counted")
        self.assertCounters("r-bill", 0.85)

    def test_undecided_without_claude_is_review_and_not_tracked(self):
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["confidence"] = 0.6        # below ai_threshold
        self.store.commit()
        self.touch("Bill-jan.pdf")
        r = self.scan()
        p = self.proposal(r, "Bill-jan.pdf")
        self.assertEqual(p["action"], "review")
        self.assertEqual(p["tentative"]["target"], "Documents/Finance")
        self.user_move("Bill-jan.pdf", "Documents/Finance")     # user did what the tentative said
        self.scan()
        self.assertEqual(self.pending(), [], "a tentative decision is not evidence")
        self.assertCounters("r-bill", 0.6)

    def test_new_file_and_lost_state_produce_nothing(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.state["dirs"] = {}                                 # state.json lost
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        self.scan()
        self.assertEqual(self.pending(), [])


class RenameAndRedundancy(Fixture):
    """6./7. Renames, duplicates and version series must not invent or teach rules."""

    def test_rename_is_seen_as_deleted_and_teaches_no_rule(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_rename("Bill-jan.pdf", "statement.pdf")
        r = self.scan()
        # names-only scanning cannot tell a rename from a delete: documented as `deleted`
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "deleted")])
        self.assertEqual([x["id"] for x in self.store.get()["rules"]], ["r-bill", "r-backup", "r-oldlog", "r-report"])
        p = self.proposal(r, "statement.pdf")
        self.assertEqual((p["rule_id"], p["target"]), (None, "Documents"))
        self.assertEqual(self.tracked("statement.pdf")["seen_scans"], 0)
        # the new name is a fresh proposal: not confirmed, not ignored
        self.scan()
        self.assertEqual(len(self.pending()), 1)

    def test_rename_within_the_same_rule_restarts_tracking(self):
        self.touch("Bill-jan.pdf")
        self.scan()
        self.advance(1)
        self.scan()
        self.user_rename("Bill-jan.pdf", "Bill-feb.pdf")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "deleted")])
        self.assertEqual(self.tracked("Bill-feb.pdf")["seen_scans"], 0)

    def test_identical_duplicate_deleted_as_proposed(self):
        self.touch("scan.pdf", 40)
        self.touch("scan (1).pdf", 40)
        r = self.scan()
        dup = self.proposal(r, "scan (1).pdf")
        self.assertEqual((dup["action"], dup["rule_id"], dup["decided_by"]), ("delete", None, "heuristic"))
        self.user_delete("scan (1).pdf")
        self.scan()
        self.assertEqual(self.observed(), [("scan (1).pdf", "confirmed")])
        self.assertIsNone(self.pending()[0]["proposed"]["rule_id"])
        self.assertEqual(len(self.store.get()["rules"]), 4)
        self.assertCounters("r-bill", 0.85)

    def test_user_keeps_the_copy_and_deletes_the_original(self):
        self.touch("scan.pdf", 40)
        self.touch("scan (1).pdf", 40)
        self.scan()
        self.user_delete("scan.pdf")
        r = self.scan()
        self.assertEqual(self.observed(), [("scan.pdf", "deleted")])
        # the survivor is no longer a duplicate: proposal changed, tracking restarted
        p = self.proposal(r, "scan (1).pdf")
        self.assertEqual((p["action"], p["target"]), ("move", "Documents"))
        self.assertEqual(self.tracked("scan (1).pdf")["seen_scans"], 0)
        self.assertEqual(len(self.store.get()["rules"]), 4)

    def test_revision_duplicate_moved_with_original_is_two_confirmations_no_rule(self):
        self.touch("scan.pdf", 40)
        self.touch("scan (1).pdf", 41)
        r = self.scan()
        self.assertEqual(self.proposal(r, "scan (1).pdf")["action"], "move")
        self.user_move("scan.pdf", "Documents")
        self.user_move("scan (1).pdf", "Documents")
        self.scan()
        self.assertEqual(sorted(self.observed()), [("scan (1).pdf", "confirmed"), ("scan.pdf", "confirmed")])
        self.assertTrue(all(o["proposed"]["rule_id"] is None for o in self.pending()))

    def test_version_series(self):
        self.touch("app-1.0.0.zip")
        self.touch("app-1.1.0.zip")
        r = self.scan()
        old, new = self.proposal(r, "app-1.0.0.zip"), self.proposal(r, "app-1.1.0.zip")
        self.assertEqual((old["action"], old["rule_id"]), ("archive", None))
        self.assertEqual((new["action"], new["target"]), ("move", "Archives"))
        self.user_delete("app-1.0.0.zip")                       # harsher than proposed
        r = self.scan()
        self.assertEqual(self.observed(), [("app-1.0.0.zip", "deleted")])
        self.assertEqual(self.proposal(r, "app-1.1.0.zip")["target"], "Archives")
        self.assertEqual(self.tracked("app-1.1.0.zip")["seen_scans"], 1)
        self.assertEqual(len(self.store.get()["rules"]), 4)
        for x in self.store.get()["rules"]:
            self.assertEqual((x["hits"], x["contradictions"]), (0, 0))


class MixedSequences(Fixture):
    """8. Several scans with mixed evidence: arithmetic and consolidation flag."""

    def test_confirm_confirm_contradict_ignore_contradict(self):
        self.touch("Bill-1.pdf")
        self.scan()
        self.user_move("Bill-1.pdf", "Documents/Finance")
        self.touch("Bill-2.pdf")
        self.scan()
        self.assertCounters("r-bill", 0.9, hits=1)
        self.user_move("Bill-2.pdf", "Documents/Finance")
        self.touch("Bill-3.pdf")
        self.scan()
        self.assertCounters("r-bill", 0.95, hits=2)
        self.user_move("Bill-3.pdf", "Invoices")
        self.touch("Bill-4.pdf")
        r = self.scan()
        self.assertCounters("r-bill", 0.85, hits=2, contradictions=1)
        self.assertFalse(r["consolidation_due"])
        for _ in range(3):
            self.advance(1)
            r = self.scan()
        self.assertCounters("r-bill", 0.8, hits=2, contradictions=1)
        self.assertFalse(r["consolidation_due"])                # 4 pending < 5, 1 contradiction < 2
        self.user_delete("Bill-4.pdf")
        r = self.scan()
        self.assertCounters("r-bill", 0.7, hits=2, contradictions=2)
        self.assertEqual([o for _, o in self.observed()],
                         ["confirmed", "confirmed", "moved_elsewhere:Invoices", "ignored", "deleted"])
        self.assertTrue(r["consolidation_due"])                 # both triggers now hold
        self.assertEqual(memmod.validate(self.store.get()), [])

    def test_several_outcomes_in_one_scan(self):
        for n in ("Bill-a.pdf", "Bill-b.pdf", "Bill-c.pdf"):
            self.touch(n)
        self.scan()
        self.user_move("Bill-a.pdf", "Documents/Finance")
        self.user_move("Bill-b.pdf", "Other")
        self.user_delete("Bill-c.pdf")
        r = self.scan()
        self.assertCounters("r-bill", 0.7, hits=1, contradictions=2)
        self.assertEqual(r["outcomes_recorded"], 3)
        self.assertTrue(r["consolidation_due"])

    def test_floor_is_never_crossed_downwards(self):
        for n in ("Bill-a.pdf", "Bill-b.pdf", "Bill-c.pdf", "Bill-d.pdf"):
            self.touch(n)
        self.scan()
        for n in ("Bill-a.pdf", "Bill-b.pdf", "Bill-c.pdf", "Bill-d.pdf"):
            self.user_delete(n)
        self.scan()
        self.assertCounters("r-bill", 0.5, contradictions=4)      # 0.85 - 0.4 = 0.45 -> floor 0.5


class AdjustRuleUnit(unittest.TestCase):
    def mem(self, source="claude", conf=0.85):
        m = memmod.normalize(make_mem("/nas"))
        r = memmod.find_rule(m, "r-bill")
        r["source"], r["confidence"] = source, conf
        return m, r

    def test_documented_deltas(self):
        for source, floor in (("claude", 0.5), ("user", 0.2), ("seed", 0.2), ("observed", 0.2)):
            m, r = self.mem(source)
            memmod.adjust_rule(m, "r-bill", "confirmed")
            self.assertEqual((r["confidence"], r["hits"], r["contradictions"]), (0.9, 1, 0))
            memmod.adjust_rule(m, "r-bill", "moved_elsewhere")
            self.assertEqual((r["confidence"], r["hits"], r["contradictions"]), (0.8, 1, 1))
            memmod.adjust_rule(m, "r-bill", "deleted")
            self.assertEqual((r["confidence"], r["hits"], r["contradictions"]), (0.7, 1, 2))
            memmod.adjust_rule(m, "r-bill", "ignored")
            self.assertEqual((r["confidence"], r["hits"], r["contradictions"]), (0.65, 1, 2))
            for _ in range(20):
                memmod.adjust_rule(m, "r-bill", "deleted")
            self.assertEqual(r["confidence"], floor, source)
            for _ in range(20):
                memmod.adjust_rule(m, "r-bill", "confirmed")
            self.assertEqual(r["confidence"], 0.99)

    def test_penalty_never_raises_confidence(self):
        # A rule already below its floor (consolidation may write any 0..1 value) must
        # not be *rewarded* by a contradiction.
        m, r = self.mem("claude", 0.3)
        memmod.adjust_rule(m, "r-bill", "deleted")
        self.assertLessEqual(r["confidence"], 0.3)
        m, r = self.mem("user", 0.1)
        memmod.adjust_rule(m, "r-bill", "ignored")
        self.assertLessEqual(r["confidence"], 0.1)

    def test_unknown_kinds_and_ids_are_noops(self):
        m, r = self.mem()
        before = copy.deepcopy(r)
        for kind in ("", "moved_elsewhere:Docs", "CONFIRMED", None, "unknown"):
            memmod.adjust_rule(m, "r-bill", kind)
        memmod.adjust_rule(m, "nope", "confirmed")
        memmod.adjust_rule(m, None, "confirmed")
        self.assertEqual(r, before)

    def test_dir_override_rules_are_adjusted_too(self):
        m, _ = self.mem()
        m["dir_overrides"]["/x"] = {"rules": [{"id": "r-local", "match": {"glob": "a*"}, "action": "keep",
                                               "confidence": 0.8, "source": "user"}]}
        memmod.adjust_rule(m, "r-local", "confirmed")
        self.assertEqual(m["dir_overrides"]["/x"]["rules"][0]["hits"], 1)


# ================================================================ layer 2: consolidation

class Trigger(Fixture):
    """9. needs_consolidation is exactly pending >= learn_batch OR any rule at learn_contradictions."""

    def test_batch_boundary(self):
        mem = self.store.get()
        for i in range(4):
            memmod.add_pending(mem, {"name": "f%d" % i, "observed": "confirmed", "proposed": {}})
        self.assertFalse(memmod.needs_consolidation(mem))
        memmod.add_pending(mem, {"name": "f4", "observed": "confirmed", "proposed": {}})
        self.assertTrue(memmod.needs_consolidation(mem))
        mem["settings"]["learn_batch"] = 6
        self.assertFalse(memmod.needs_consolidation(mem))

    def test_contradiction_boundary(self):
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["contradictions"] = 1
        self.assertFalse(memmod.needs_consolidation(mem))
        memmod.find_rule(mem, "r-bill")["contradictions"] = 2
        self.assertTrue(memmod.needs_consolidation(mem))
        mem["settings"]["learn_contradictions"] = 3
        self.assertFalse(memmod.needs_consolidation(mem))

    def test_scan_reports_consolidation_due(self):
        for i in range(5):
            self.touch("Bill-%d.pdf" % i)
        r = self.scan()
        self.assertFalse(r["consolidation_due"])
        for i in range(5):
            self.user_move("Bill-%d.pdf" % i, "Documents/Finance")
        r = self.scan()
        self.assertEqual(r["memory"]["pending_outcomes"], 5)
        self.assertTrue(r["consolidation_due"])

    def test_daemon_starts_consolidation_only_when_due_and_ai_enabled(self):
        from organizer import daemon
        calls = []

        def fake_consolidate(store, lock, dry_run=False, log=None):
            calls.append(dry_run)
            return {"applied": False}

        def wait():
            for _ in range(200):
                if not daemon.CONSOLIDATING.is_set():
                    return
                time.sleep(0.01)
            self.fail("consolidation flag never cleared")

        with mock.patch.object(daemon, "STORE", self.store), mock.patch.object(engine, "consolidate", fake_consolidate):
            daemon._maybe_consolidate_async()                    # nothing pending
            wait()
            self.assertEqual(calls, [])
            mem = self.store.get()
            for i in range(5):
                memmod.add_pending(mem, {"name": "f%d" % i, "observed": "confirmed", "proposed": {}})
            daemon._maybe_consolidate_async()                    # due, but ai_enabled is False
            wait()
            self.assertEqual(calls, [])
            mem["settings"]["ai_enabled"] = True
            daemon.CONSOLIDATING.set()                           # one already in flight
            daemon._maybe_consolidate_async()
            daemon.CONSOLIDATING.clear()
            self.assertEqual(calls, [])
            daemon._maybe_consolidate_async()
            wait()
            self.assertEqual(calls, [False])


class ConsolidationApply(Fixture):
    """9./10. A rewrite is applied atomically or not at all; the previous copy survives."""

    def fill_pending(self, n=5):
        for i in range(n):
            self.touch("Bill-%d.pdf" % i)
        self.scan()
        for i in range(n):
            self.user_move("Bill-%d.pdf" % i, "Documents/Finance")
        self.scan()
        self.assertEqual(len(self.pending()), n)

    def disk(self):
        with open(paths.MEMORY_PATH, "rb") as f:
            return f.read()

    def bak(self):
        p = paths.MEMORY_PATH + ".bak"
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def consolidate(self, rewrite, dry_run=False):
        if isinstance(rewrite, Exception):
            m = mock.patch.object(brain, "consolidate", side_effect=rewrite)
        else:
            m = mock.patch.object(brain, "consolidate", return_value=rewrite)
        with m:
            return engine.consolidate(self.store, threading.RLock(), dry_run=dry_run, log=self.logs.append)

    def test_applied(self):
        self.fill_pending()
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["contradictions"] = 1
        self.store.commit()
        before_disk = self.disk()
        rw = good_rewrite(mem)
        rw["rules"][0]["confidence"] = 0.95
        rw["rules"][0]["hits"] = 7
        rw["rules"].append({"id": "r-new", "match": {"glob": "Invoice-*.pdf"}, "action": "move",
                            "category": "documents/finance", "confidence": 0.8, "source": "claude"})
        rw["categories"]["documents/invoices"] = {"dir": "Documents/Invoices", "confidence": 0.8}
        rw["targets"]["arch"] = "~/Archive"
        res = self.consolidate(rw)
        self.assertTrue(res["applied"], res)
        mem = self.store.get()
        self.assertEqual(memmod.validate(mem), [])
        self.assertEqual(mem["learned"]["pending"], [])
        self.assertEqual(len(mem["learned"]["confirmed"]), 5)
        r = memmod.find_rule(mem, "r-bill")
        self.assertEqual((r["confidence"], r["hits"], r["contradictions"]), (0.95, 7, 0))
        self.assertEqual(memmod.find_rule(mem, "r-new")["scope"], "global")
        self.assertEqual(mem["claude_notes"], "notes")
        self.assertEqual(mem["last_consolidation"]["rationale"], "test")
        self.assertEqual(mem["targets"]["arch"], "~/Archive")
        self.assertEqual(res["diff"]["rules_added"], ["r-new"])
        self.assertEqual(res["diff"]["categories_added"], ["documents/invoices"])
        self.assertEqual(self.bak(), before_disk, "previous memory kept in .bak")
        self.assertEqual(json.loads(self.disk().decode())["last_consolidation"]["rationale"], "test")
        # reloading the file yields the same memory
        self.assertEqual(memmod.load(paths.MEMORY_PATH), mem)

    def test_settings_learned_and_unknown_keys_in_rewrite_are_ignored(self):
        self.fill_pending()
        mem = self.store.get()
        rw = good_rewrite(mem, settings={"ai_threshold": 0.0, "learn_batch": 1}, version=99,
                          learned={"pending": [{"fake": 1}], "confirmed": []},
                          dir_overrides={"/": {"rules": [{"id": "x", "match": {"glob": "*"}, "action": "delete",
                                                          "confidence": 1, "source": "claude"}]}})
        res = self.consolidate(rw)
        self.assertTrue(res["applied"], res)
        mem = self.store.get()
        self.assertEqual(mem["settings"]["ai_threshold"], 0.7)
        self.assertEqual(mem["settings"]["learn_batch"], 5)
        self.assertEqual(mem["version"], 1)
        self.assertEqual(mem["dir_overrides"], {})
        self.assertEqual(mem["learned"]["pending"], [])

    def test_history_is_capped(self):
        self.fill_pending()
        mem = self.store.get()
        mem["settings"]["max_confirmed_history"] = 3
        mem["learned"]["confirmed"] = [{"name": "older"}]
        res = self.consolidate(good_rewrite(mem))
        self.assertTrue(res["applied"])
        confirmed = self.store.get()["learned"]["confirmed"]
        self.assertEqual(len(confirmed), 3)
        self.assertNotIn({"name": "older"}, confirmed)

    def test_outcomes_arriving_during_the_call_stay_pending_and_are_not_double_counted(self):
        self.fill_pending()
        late = {"name": "late.pdf", "observed": "confirmed", "proposed": {}, "ts": 1}

        def slow_claude(snapshot, log=None):
            memmod.add_pending(self.store.get(), dict(late))    # a scan lands mid-call
            return good_rewrite(snapshot)

        with mock.patch.object(brain, "consolidate", slow_claude):
            res = engine.consolidate(self.store, threading.RLock(), log=self.logs.append)
        self.assertTrue(res["applied"], res)
        mem = self.store.get()
        self.assertEqual(mem["learned"]["pending"], [late])
        self.assertEqual(len(mem["learned"]["confirmed"]), 5)
        self.assertNotIn(late, mem["learned"]["confirmed"], "late outcome must not be consolidated yet")

    def test_dry_run_changes_nothing(self):
        self.fill_pending()
        mem_before = copy.deepcopy(self.store.get())
        disk = self.disk()
        rw = good_rewrite(self.store.get())
        rw["rules"] = rw["rules"][:2]
        res = self.consolidate(rw, dry_run=True)
        self.assertFalse(res["applied"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["diff"]["rules_removed"], ["r-oldlog", "r-report"])
        self.assertEqual(self.store.get(), mem_before)
        self.assertEqual(self.disk(), disk)

    def test_nothing_pending_is_a_noop(self):
        with mock.patch.object(brain, "consolidate") as c:
            res = engine.consolidate(self.store, None, log=self.logs.append)
        self.assertFalse(res["applied"])
        c.assert_not_called()

    def assertRejected(self, rewrite, frag=None):
        mem_before = copy.deepcopy(self.store.get())
        disk, bak = self.disk(), self.bak()
        res = self.consolidate(rewrite)
        self.assertFalse(res["applied"], res)
        if frag:
            self.assertIn(frag, res["reason"])
        self.assertEqual(self.store.get(), mem_before, "in-memory copy must be untouched")
        self.assertEqual(memmod.validate(self.store.get()), [])
        self.assertEqual(self.disk(), disk, "memory.json must be untouched")
        self.assertEqual(self.bak(), bak, "no write happened, so no new .bak")
        self.assertEqual(len(self.store.get()["learned"]["pending"]), 5, "pending evidence must survive")

    def test_hostile_rewrites_are_rejected_and_memory_survives(self):
        self.fill_pending()
        mem = self.store.get()
        self.assertRejected(brain.BrainError("claude timed out"), "timed out")
        self.assertRejected(good_rewrite(mem, rules=[]), "drops more than 50%")
        self.assertRejected(good_rewrite(mem, rules=mem["rules"][:1]), "drops more than 50%")
        self.assertRejected({"rules": [], "categories": {}}, "missing")
        self.assertRejected(good_rewrite(mem, rules="rm -rf /"), "must be a list")
        self.assertRejected(good_rewrite(mem, rules=[1, 2, 3, 4]), "must be a list")
        self.assertRejected(good_rewrite(mem, rules=[[], {}, "x", None]), "must be a list")
        self.assertRejected(good_rewrite(mem, categories=[]), "must be objects")
        self.assertRejected(good_rewrite(mem, targets="~/x"), "must be objects")
        self.assertRejected(good_rewrite(mem, targets={"nas": "/etc/cron.d"}), "under ~")
        self.assertRejected(good_rewrite(mem, targets={"nas": self.nas, "root": "/root"}), "under ~")
        self.assertRejected(good_rewrite(mem, targets={"nas": self.nas, "up": "~/../../etc"}))
        self.assertRejected(good_rewrite(mem, targets={"nas": self.nas, "x": 5}))
        self.assertRejected(good_rewrite(mem, targets={"a/b": "~/x", "nas": self.nas}))
        self.assertRejected(good_rewrite(mem, categories={"../etc": {"dir": "x"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "/etc"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "~/.ssh"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "..\\..\\x"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "a\x00b"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "x", "confidence": 7}}))
        self.assertRejected(good_rewrite(mem, categories={"c": {"dir": "x", "ext": "pdf"}}))
        self.assertRejected(good_rewrite(mem, categories={"x" * 65: {"dir": "x"}}))
        self.assertRejected(good_rewrite(mem, categories={"c": "Documents"}))
        # a rewrite that keeps the rules but moves a category's dir through a rule is still
        # subject to per-rule target validation
        rules = copy.deepcopy(mem["rules"])
        rules[0]["target"] = "../../etc"
        self.assertRejected(good_rewrite(mem, rules=rules), "rules[0]")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["category"] = "does/not/exist"
        self.assertRejected(good_rewrite(mem, rules=rules), "not defined")
        rules = copy.deepcopy(mem["rules"])
        rules[1]["target"] = "/etc"
        self.assertRejected(good_rewrite(mem, rules=rules), "move-to")
        rules = copy.deepcopy(mem["rules"])
        rules[1]["target"] = "~/../../root"
        self.assertRejected(good_rewrite(mem, rules=rules), "move-to")
        rules = copy.deepcopy(mem["rules"])
        rules[3]["target"] = "Documents"
        self.assertRejected(good_rewrite(mem, rules=rules), "archive target")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {"glob": "*", "exec": "rm -rf /"}
        self.assertRejected(good_rewrite(mem, rules=rules), "unknown key")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {"regex": "("}
        self.assertRejected(good_rewrite(mem, rules=rules), "regex invalid")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {"regex": "a" * 201}
        self.assertRejected(good_rewrite(mem, rules=rules), "regex")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {"glob": "x" * 129}
        self.assertRejected(good_rewrite(mem, rules=rules), "glob")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {}
        self.assertRejected(good_rewrite(mem, rules=rules), "non-empty")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["action"] = "shell"
        self.assertRejected(good_rewrite(mem, rules=rules), "action")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["source"] = "attacker"
        self.assertRejected(good_rewrite(mem, rules=rules), "source")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["scope"] = "Downloads"
        self.assertRejected(good_rewrite(mem, rules=rules), "scope")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["hook"] = "curl evil"
        self.assertRejected(good_rewrite(mem, rules=rules), "unknown key")
        rules = copy.deepcopy(mem["rules"])
        rules[1]["id"] = "r-bill"
        self.assertRejected(good_rewrite(mem, rules=rules), "duplicate id")
        for bad in ("0.9", True, 2, -0.1, float("nan"), float("inf")):
            rules = copy.deepcopy(mem["rules"])
            rules[0]["confidence"] = bad
            self.assertRejected(good_rewrite(mem, rules=rules), "confidence")
        for bad in ("abc", None, [1], 1.5, True, -1):
            rules = copy.deepcopy(mem["rules"])
            rules[0]["hits"] = bad
            self.assertRejected(good_rewrite(mem, rules=rules), "hits")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["note"] = ["not", "a", "string"]
        self.assertRejected(good_rewrite(mem, rules=rules), "note")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["category"] = ["documents"]
        self.assertRejected(good_rewrite(mem, rules=rules), "category")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["id"] = ["r-bill"]
        self.assertRejected(good_rewrite(mem, rules=rules), "id")
        rules = copy.deepcopy(mem["rules"])
        rules[0]["match"] = {"ext": [1], "signals": None, "is_dir": "yes", "min_size_mb": True}
        self.assertRejected(good_rewrite(mem, rules=rules), "match")

    def test_claude_cli_garbage_never_reaches_memory(self):
        """Through the real run_claude parser: bad process output is a BrainError."""
        self.fill_pending()
        cases = [
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"is_error": True, "result": "nope"}), stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"structured_output": [1, 2]}), stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"result": "```json\n[1]\n```"}), stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"result": "hello"}), stderr=""),
            subprocess.TimeoutExpired("claude", 1),
            OSError("exec format error"),
        ]
        mem_before = copy.deepcopy(self.store.get())
        disk = self.disk()
        for case in cases:
            kw = {"side_effect": case} if isinstance(case, Exception) else {"return_value": case}
            with mock.patch.object(brain, "_exec", **kw), mock.patch.object(brain, "claude_path", return_value="/bin/claude"):
                res = engine.consolidate(self.store, None, log=self.logs.append)
            self.assertFalse(res["applied"], case)
            self.assertEqual(self.store.get(), mem_before)
            self.assertEqual(self.disk(), disk)
        # a structurally valid but hostile structured_output goes through apply_consolidation
        hostile = {"structured_output": {"rules": [], "categories": {}, "targets": {"x": "/etc"},
                                         "claude_notes": "", "rationale": "wipe"}}
        with mock.patch.object(brain, "_exec", return_value=subprocess.CompletedProcess([], 0, json.dumps(hostile), "")), \
                mock.patch.object(brain, "claude_path", return_value="/bin/claude"):
            res = engine.consolidate(self.store, None, log=self.logs.append)
        self.assertFalse(res["applied"])
        self.assertEqual(self.store.get(), mem_before)

    def test_malformed_rewrite_shapes_do_not_crash_the_driver(self):
        self.fill_pending()
        mem = self.store.get()
        for rw in (good_rewrite(mem, rules=[1, 2, 3, 4]), good_rewrite(mem, rules=[{"id": ["x"]}] * 4),
                   good_rewrite(mem, categories="x"), good_rewrite(mem, rules=None)):
            self.assertRejected(rw)

    def test_consolidated_memory_is_used_by_the_next_scan(self):
        self.fill_pending()
        mem = self.store.get()
        rw = good_rewrite(mem)
        rw["rules"][0]["category"] = "documents"                 # Claude retargets r-bill
        self.assertTrue(self.consolidate(rw)["applied"])
        self.touch("Bill-new.pdf")
        r = self.scan()
        self.assertEqual(self.proposal(r, "Bill-new.pdf")["target"], "Documents")


# ================================================================ memory file robustness

class CorruptAndHotReload(Fixture):
    """12./13. Invalid file -> last good copy + error; valid edit -> hot-reloaded."""

    def write_raw(self, data):
        with open(paths.MEMORY_PATH, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        st = os.stat(paths.MEMORY_PATH)
        os.utime(paths.MEMORY_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))

    def test_syntax_error_keeps_last_good_copy_and_never_overwrites_it(self):
        good = copy.deepcopy(self.store.get())
        broken = '{"version": 1, "rules": ['
        self.write_raw(broken)
        self.assertFalse(self.store.reload())
        self.assertIn("Expecting", self.store.last_error)
        self.assertEqual(self.store.get(), good)
        self.touch("Bill-jan.pdf")
        r = self.scan()
        self.assertTrue(any("last good copy" in w for w in r["warnings"]), r["warnings"])
        self.assertTrue(any("not overwritten" in w for w in r["warnings"]), r["warnings"])
        self.assertEqual(self.proposal(r, "Bill-jan.pdf")["target"], "Documents/Finance")
        # the broken edit is still there to be fixed; nothing was written, no .bak
        with open(paths.MEMORY_PATH) as f:
            self.assertEqual(f.read(), broken)
        self.assertFalse(os.path.exists(paths.MEMORY_PATH + ".bak"))
        # learning continues in memory meanwhile ...
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        for _ in range(2):
            self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "confirmed")])
        with open(paths.MEMORY_PATH) as f:
            self.assertEqual(f.read(), broken)
        # ... and consolidation is refused rather than clobbering the file
        with mock.patch.object(brain, "consolidate", return_value=good_rewrite(self.store.get())):
            res = engine.consolidate(self.store, None, log=self.logs.append)
        self.assertFalse(res["applied"])
        self.assertIn("invalid", res["reason"])
        # the user fixes the file: hot-reloaded, in-memory learning is dropped in favour
        # of the file (documented last-writer-wins), the next scan saves normally
        self.write_raw(good)
        self.assertTrue(self.store.reload())
        self.assertIsNone(self.store.last_error)
        r = self.scan()
        self.assertEqual(r["warnings"], [])
        self.assertTrue(os.path.exists(paths.MEMORY_PATH + ".bak"))
        self.assertEqual(memmod.validate(memmod.load(paths.MEMORY_PATH)), [])

    def test_corrupt_at_startup_is_not_overwritten_by_scans(self):
        with open(paths.MEMORY_PATH) as f:
            original = f.read()
        broken = original.replace('"version": 1', '"version": 1,')   # a typo, not garbage
        self.write_raw(broken)
        store = memmod.MemoryStore(paths.MEMORY_PATH)
        self.assertTrue(store.last_error)
        self.assertEqual(store.get()["rules"], [])              # empty fallback in memory only
        self.touch("Bill-jan.pdf")
        state = engine.load_state()
        for _ in range(3):
            with mock.patch("time.time", return_value=self.now):
                r = engine.run_scan(self.cwd, {"no_ai": True}, store, state, log=self.logs.append)
            self.assertTrue(any("not overwritten" in w for w in r["warnings"]), r["warnings"])
        with open(paths.MEMORY_PATH) as f:
            self.assertEqual(f.read(), broken, "the user's rules must survive to be repaired")
        self.assertFalse(os.path.exists(paths.MEMORY_PATH + ".bak"))
        self.write_raw(original)                                # repaired by hand
        with mock.patch("time.time", return_value=self.now):
            r = engine.run_scan(self.cwd, {"no_ai": True}, store, state, log=self.logs.append)
        self.assertEqual(r["warnings"], [])
        self.assertEqual(self.proposal(r, "Bill-jan.pdf")["rule_id"], "r-bill")

    def test_deleted_file_is_recreated(self):
        os.remove(paths.MEMORY_PATH)
        self.assertFalse(self.store.reload())
        self.assertTrue(self.store.last_error)
        self.touch("Bill-jan.pdf")
        r = self.scan()
        self.assertTrue(os.path.exists(paths.MEMORY_PATH))
        self.assertEqual(self.scan()["warnings"], [])

    def test_schema_invalid_edit_is_refused(self):
        good = copy.deepcopy(self.store.get())
        bad = copy.deepcopy(good)
        bad["rules"][0]["target"] = "../../etc"
        bad["categories"]["evil"] = {"dir": "/etc"}
        bad["settings"]["exec"] = "true"
        self.write_raw(bad)
        self.assertFalse(self.store.reload())
        for frag in ("rules[0]", "categories['evil']", "unknown key 'exec'"):
            self.assertIn(frag, self.store.last_error)
        self.assertEqual(self.store.get(), good)

    def test_unhashable_values_are_an_error_not_a_crash(self):
        good = copy.deepcopy(self.store.get())
        for key, val in (("id", ["r-bill"]), ("category", {"a": 1}), ("category", ["documents"])):
            bad = copy.deepcopy(good)
            bad["rules"][0][key] = val
            self.assertTrue(memmod.validate(bad), key)
            self.write_raw(bad)
            self.assertFalse(self.store.reload())
            self.assertEqual(self.store.get(), good)

    def test_wrong_root_types(self):
        good = copy.deepcopy(self.store.get())
        for raw in ("[]", "null", '"x"', "1", '{"version": 2}', '{"version": 1, "rules": {}}',
                    '{"version": 1, "learned": []}', '{"version": 1, "claude_notes": 5}'):
            self.write_raw(raw)
            self.assertFalse(self.store.reload(), raw)
            self.assertEqual(self.store.get(), good, raw)

    def test_corrupt_at_startup_is_empty_memory_with_error(self):
        self.write_raw("{ not json")
        store = memmod.MemoryStore(paths.MEMORY_PATH)
        self.assertTrue(store.last_error)
        self.assertEqual(store.get()["rules"], [])
        self.assertEqual(store.get()["settings"]["learn_batch"], 5)
        self.assertEqual(memmod.validate(store.get()), [])

    def test_missing_file_is_seeded(self):
        os.remove(paths.MEMORY_PATH)
        store = memmod.MemoryStore(paths.MEMORY_PATH)
        self.assertIsNone(store.last_error)
        self.assertEqual(memmod.validate(store.get()), [])
        self.assertTrue(os.path.exists(paths.MEMORY_PATH))

    def test_valid_manual_edit_is_hot_reloaded(self):
        self.touch("Bill-jan.pdf")
        self.touch("Invoice-1.pdf")
        r = self.scan()
        self.assertEqual(self.proposal(r, "Invoice-1.pdf")["target"], "Documents")
        edited = json.loads(json.dumps(self.store.get()))
        edited["rules"].append({"id": "r-invoice", "match": {"glob": "Invoice-*.pdf"}, "category": "documents/finance",
                                "action": "move", "confidence": 0.9, "source": "user"})
        edited["rules"][0]["confidence"] = 0.6                  # r-bill now below ai_threshold
        edited["claude_notes"] = "hand edited"
        self.write_raw(edited)
        self.assertTrue(self.store.reload())
        self.assertIsNone(self.store.last_error)
        r = self.scan()
        self.assertEqual(self.proposal(r, "Invoice-1.pdf")["rule_id"], "r-invoice")
        self.assertEqual(self.proposal(r, "Bill-jan.pdf")["action"], "review")
        self.assertEqual(self.store.get()["claude_notes"], "hand edited")
        # normalize() filled the defaults the edit left out
        self.assertEqual(memmod.find_rule(self.store.get(), "r-invoice")["hits"], 0)
        # and the daemon's own save keeps the edit
        self.assertEqual(memmod.load(paths.MEMORY_PATH)["claude_notes"], "hand edited")

    def test_hot_reload_then_outcome_uses_the_edited_rule(self):
        self.touch("Invoice-1.pdf")
        self.scan()
        edited = json.loads(json.dumps(self.store.get()))
        edited["rules"].append({"id": "r-invoice", "match": {"glob": "Invoice-*.pdf"}, "category": "documents/finance",
                                "action": "move", "confidence": 0.9, "source": "user"})
        self.write_raw(edited)
        self.scan()                                             # proposal changes: Documents -> Documents/Finance
        self.user_move("Invoice-1.pdf", "Documents/Finance")
        self.scan()
        self.assertEqual(self.observed(), [("Invoice-1.pdf", "confirmed")])
        self.assertCounters("r-invoice", 0.95, hits=1)

    def test_same_mtime_edit_is_still_detected(self):
        """The change stamp is (mtime_ns, size, inode), so an edit landing in the same
        timestamp tick as the daemon's own write is still seen — unless it also keeps
        the byte size and inode, the documented residual edge."""
        with open(paths.MEMORY_PATH) as f:
            raw = f.read()
        st = os.stat(paths.MEMORY_PATH)
        with open(paths.MEMORY_PATH, "w") as f:
            f.write(raw.replace('"claude_notes": ""', '"claude_notes": "sneaky"'))
        os.utime(paths.MEMORY_PATH, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(self.store.reload())
        self.assertEqual(self.store.get()["claude_notes"], "sneaky")
        # same size, same mtime, same inode: not seen until `organizer reload` (force)
        st = os.stat(paths.MEMORY_PATH)
        with open(paths.MEMORY_PATH, "r+") as f:
            data = f.read().replace('"sneaky"', '"SNEAKY"')
            f.seek(0)
            f.write(data)
            f.truncate()
        os.utime(paths.MEMORY_PATH, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertFalse(self.store.reload())
        self.assertTrue(self.store.reload(force=True))
        self.assertEqual(self.store.get()["claude_notes"], "SNEAKY")


class SettingsValidation(unittest.TestCase):
    """Malformed settings are refused at load so needs_consolidation() & co. cannot raise."""

    def mem(self, **settings):
        m = make_mem("/nas")
        m["settings"].update(settings)
        return m

    def test_defaults_and_seed_are_valid(self):
        self.assertEqual(memmod.validate(memmod.empty_memory()), [])
        seed = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "memory.json")
        with open(seed) as f:
            self.assertEqual(memmod.validate(json.load(f)), [])
        self.assertEqual(memmod.validate(self.mem(learn_batch=5.0, ai_max_budget_usd=0.25, ai_threshold=1)), [])

    def test_bad_values_are_named(self):
        cases = {"learn_batch": ["five", None, True, -1, float("nan"), float("inf"), [5]],
                 "learn_contradictions": ["2", -2], "ignored_after_scans": ["3"], "ignored_after_days": [None],
                 "max_confirmed_history": [True], "ai_threshold": [1.5, "0.7"], "archive_after_days": ["180"],
                 "ai_enabled": ["yes", 1, None], "use_magic": ["true"], "delete_policy": [1, None],
                 "ai_model": [5], "ai_timeout_s": ["120"]}
        for key, values in cases.items():
            for v in values:
                errs = memmod.validate(self.mem(**{key: v}))
                self.assertTrue(any("settings.%s" % key in e for e in errs), (key, v, errs))

    def test_learning_logic_never_raises_on_a_loaded_memory(self):
        # only what validate() lets through reaches int()/float(): exercise every consumer
        for settings in ({}, {"learn_batch": 5.0, "learn_contradictions": 2.0, "ignored_after_scans": 3.0,
                              "ignored_after_days": 0.5, "max_confirmed_history": 10.0, "ai_threshold": 0.5}):
            m = memmod.normalize(self.mem(**settings))
            self.assertEqual(memmod.validate(m), [])
            memmod.find_rule(m, "r-bill")["contradictions"] = 2
            self.assertTrue(memmod.needs_consolidation(m))
            for i in range(6):
                memmod.add_pending(m, {"name": "f%d" % i, "observed": "confirmed", "proposed": {}})
            self.assertEqual(memmod.apply_consolidation(m, good_rewrite(m)), [])
            dstate = {"proposals": {"a.pdf": {"action": "move", "target": "Documents", "size": 1, "ts": 0}}}
            engine.detect_outcomes(m, dstate, "/nonexistent-dir", {"a.pdf"}, lambda s: None)

    def test_string_numbers_are_refused_not_coerced(self):
        m = self.mem(learn_batch="5")
        self.assertTrue(memmod.validate(m))
        with self.assertRaises(ValueError):
            memmod.save(m, os.path.join(_TMP, "never.json"), backup=False)


class RegexHeuristic(unittest.TestCase):
    """The textbook nested-quantifier form is refused; everything else is a documented limitation."""

    def test_nested_quantifiers_rejected(self):
        for rx in [r"(a+)+$", r"(a*)*", r"(\w+\s?)*$", r"^(x+)+y", r"([a-z]+)*\.pdf",
                   r"(?:ab+)+", r"(.+){2,}", r"(\d{1,3})+"]:
            errs = memmod.match_problems({"regex": rx})
            self.assertTrue(any("backtracking" in e for e in errs), rx)

    def test_ordinary_rules_accepted(self):
        for rx in [r"^statement-.*\.pdf$", r"backup-\d{8}\.zip", r"(?i)^screenshot", r"\(1\)\.pdf$",
                   r"^(invoice|receipt)-\d+", r"^[a-z]+\.(png|jpg)$", r"\+\)+", r"[)+]+", r"(?i)^x",
                   r"v\d+(\.\d+)?(\.\d+)?"]:
            self.assertEqual(memmod.match_problems({"regex": rx}), [], rx)

    def test_documented_false_positive(self):
        # `(\.\d+)+` is safe (each repetition is anchored by the literal dot) but has the
        # refused shape; MEMORY-GUIDE.md gives the rewrite.
        self.assertTrue(memmod.match_problems({"regex": r"v\d+(\.\d+)+"}))

    def test_applies_on_every_entry_path(self):
        mem = memmod.normalize(make_mem("/nas"))
        bad = {"id": "r-x", "match": {"regex": "(a+)+$"}, "action": "keep", "confidence": 0.9, "source": "user"}
        self.assertTrue(memmod.validate(dict(mem, rules=mem["rules"] + [bad])))
        rw = good_rewrite(mem, rules=mem["rules"] + [bad])
        self.assertTrue(memmod.apply_consolidation(mem, rw))
        added = brain.merge_new_rules({"new_rules": [{"regex": "(a+)+$", "action": "keep", "confidence": 0.9,
                                                      "note": "n"}]}, mem)
        self.assertEqual(added, [])

    def test_known_limitation_other_forms_are_not_caught(self):
        # Documented in THREAT-MODEL.md: this heuristic is not a ReDoS detector.
        for rx in (r"^(a|aa)+$", r"(.*a){20}b", r".*a.*a.*a.*a.*a.*b"):
            self.assertEqual(memmod.match_problems({"regex": rx}), [], rx)


class SameNameCollisions(Fixture):
    """2. Name-only detection cannot tell files apart; the recorded size is used as a
    tie-breaker and a same-name/different-size sighting yields NO signal at all."""

    def test_same_name_pre_exists_at_destination_source_untouched(self):
        os.makedirs(os.path.join(self.cwd, "Documents/Finance"))
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "w") as f:
            f.write("older copy")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.scan()
        self.assertEqual(self.pending(), [], "the source is still there: nothing happened")
        self.assertCounters("r-bill", 0.85)

    def test_source_deleted_unrelated_same_name_at_destination(self):
        os.makedirs(os.path.join(self.cwd, "Documents/Finance"))
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "w") as f:
            f.write("unrelated, different size")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_delete("Bill-jan.pdf")
        self.scan()
        self.assertEqual(self.pending(), [], "must not be a false confirmation")
        self.assertCounters("r-bill", 0.85)
        self.assertTrue(any("ambiguous" in m for m in self.logs), self.logs)

    def test_source_deleted_unrelated_same_name_elsewhere(self):
        os.makedirs(os.path.join(self.cwd, "Other/Deep"))
        with open(os.path.join(self.cwd, "Other/Deep/Bill-jan.pdf"), "w") as f:
            f.write("unrelated")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_delete("Bill-jan.pdf")
        self.scan()
        self.assertEqual(self.pending(), [], "must not be a false contradiction")
        self.assertCounters("r-bill", 0.85)

    def test_delete_proposal_unrelated_same_name_elsewhere(self):
        with open(os.path.join(self.nas, "old-1.log"), "w") as f:
            f.write("unrelated")
        self.touch("old-1.log", 3)
        self.scan()
        self.user_delete("old-1.log")
        self.scan()
        self.assertEqual(self.pending(), [])
        self.assertCounters("r-oldlog", 0.9)

    def test_moved_to_destination_over_a_same_name_file_elsewhere(self):
        os.makedirs(os.path.join(self.cwd, "Other"))
        with open(os.path.join(self.cwd, "Other/Bill-jan.pdf"), "w") as f:
            f.write("unrelated")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "confirmed")])
        self.assertCounters("r-bill", 0.9, hits=1)

    def test_moved_elsewhere_while_unrelated_same_name_sits_at_destination(self):
        os.makedirs(os.path.join(self.cwd, "Documents/Finance"))
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "w") as f:
            f.write("unrelated")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_move("Bill-jan.pdf", "Invoices")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "moved_elsewhere:Invoices")])
        self.assertCounters("r-bill", 0.75, contradictions=1)

    def test_moved_then_edited_is_silent_not_a_contradiction(self):
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "a") as f:
            f.write("annotated")
        self.scan()
        self.assertEqual(self.pending(), [])
        self.assertCounters("r-bill", 0.85)

    def test_renamed_with_a_same_name_file_elsewhere(self):
        os.makedirs(os.path.join(self.cwd, "Other"))
        with open(os.path.join(self.cwd, "Other/Bill-jan.pdf"), "w") as f:
            f.write("unrelated")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_rename("Bill-jan.pdf", "statement.pdf")
        self.scan()
        self.assertEqual(self.pending(), [])                     # ambiguous, not `deleted`
        self.assertCounters("r-bill", 0.85)

    def test_same_name_same_size_is_the_unavoidable_case(self):
        # Two 3-byte files: indistinguishable by name+size. Documented limitation; the
        # wrong signal is a single +0.05 / hits+1, never a rule and never a delete.
        os.makedirs(os.path.join(self.cwd, "Documents/Finance"))
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "w") as f:
            f.write("abc")
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        self.user_delete("Bill-jan.pdf")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "confirmed")])
        self.assertCounters("r-bill", 0.9, hits=1)
        self.assertEqual(len(self.store.get()["rules"]), 4)

    def test_legacy_state_without_size_falls_back_to_name_only(self):
        self.touch("Bill-jan.pdf", 3)
        self.scan()
        del self.tracked("Bill-jan.pdf")["size"]
        self.user_move("Bill-jan.pdf", "Documents/Finance")
        with open(os.path.join(self.cwd, "Documents/Finance/Bill-jan.pdf"), "a") as f:
            f.write("grown")
        self.scan()
        self.assertEqual(self.observed(), [("Bill-jan.pdf", "confirmed")])

    def test_directory_proposals_ignore_size(self):
        mem = self.store.get()
        mem["rules"].append({"id": "r-proj", "match": {"glob": "proj-*", "is_dir": True}, "action": "move",
                             "target": "Projects", "confidence": 0.9, "source": "user"})
        self.store.commit()
        os.mkdir(os.path.join(self.cwd, "proj-a"))
        self.touch("proj-a/file.txt", 5)
        self.scan()
        self.user_move("proj-a", "Projects")
        self.touch("Projects/proj-a/more.txt", 50)             # contents changed after the move
        self.scan()
        self.assertEqual(self.observed(), [("proj-a", "confirmed")])


# ================================================================ boundary bypasses

class NoBypass(Fixture):
    """14. Cached decisions and learned rules go through the same validators as fresh output."""

    def enable_ai(self):
        mem = self.store.get()
        mem["settings"]["ai_enabled"] = True
        self.store.commit()

    def test_stage1_rule_target_through_symlink_is_confined(self):
        outside = os.path.join(self.root, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.cwd, "link"))
        mem = self.store.get()
        mem["rules"].append({"id": "r-esc", "match": {"glob": "esc-*.pdf"}, "action": "move", "target": "link/out",
                             "confidence": 0.95, "source": "claude"})
        mem["categories"]["escape"] = {"dir": "link/deeper", "ext": ["esc"], "confidence": 0.95}
        self.assertEqual(memmod.validate(mem), [])              # shape is fine; only cwd knows about the link
        self.store.commit()
        self.touch("esc-1.pdf")
        self.touch("x.esc")
        self.touch("Bill-jan.pdf")
        r = self.scan()
        for n in ("esc-1.pdf", "x.esc"):
            p = self.proposal(r, n)
            self.assertEqual(p["action"], "review", n)
            self.assertIsNone(p["target"])
            self.assertTrue(any("outside" in x for x in p["reasons"]), p["reasons"])
        self.assertEqual(self.proposal(r, "Bill-jan.pdf")["target"], "Documents/Finance")
        for tgt in list(r["structure"]) + r["summary"]["new_dirs"]:
            self.assertFalse(tgt.startswith("link"), tgt)
        # archive folder itself replaced by a symlink: the same guard applies
        os.symlink(outside, os.path.join(self.cwd, "_archive"))
        self.touch("report-q1.txt")
        r = self.scan()
        self.assertEqual(self.proposal(r, "report-q1.txt")["action"], "review")

    def test_learned_rule_from_claude_is_applied_only_after_validation(self):
        self.enable_ai()
        self.touch("Invoice-1.pdf")
        mem = self.store.get()
        memmod.find_rule(mem, "r-bill")["confidence"] = 0.6      # send something to Claude
        self.store.commit()
        self.touch("Bill-jan.pdf")
        reply = {"proposals": [{"name": "Bill-jan.pdf", "action": "move", "target": "Documents/Finance",
                                "confidence": 0.9, "reason": "bill"}],
                 "new_rules": [
                     {"glob": "Invoice-*.pdf", "action": "move", "target": "../../etc", "confidence": 0.9, "note": "n"},
                     {"glob": "Invoice-*.pdf", "action": "move-to", "target": "/etc", "confidence": 0.9, "note": "n"},
                     {"glob": "*.pdf", "action": "delete", "confidence": 0.9, "note": "broad"},
                     {"regex": "(", "action": "delete", "confidence": 0.9, "note": "bad"},
                     {"glob": "Invoice-*.pdf", "action": "move", "category": "documents/invoices",
                      "category_dir": "Documents/Invoices", "confidence": 0.9, "note": "ok"},
                 ], "memory_notes": "x"}
        with mock.patch.object(brain, "propose", return_value=reply):
            r = self.scan(no_ai=False)
        self.assertEqual(r["ai"]["new_rules"], ["r-invoice-pdf"])
        mem = self.store.get()
        self.assertEqual(memmod.validate(mem), [])
        self.assertEqual(len(mem["rules"]), 5)
        self.assertEqual(memmod.load(paths.MEMORY_PATH)["rules"][-1]["match"], {"glob": "Invoice-*.pdf"})
        # the learned rule decides next scan without Claude, and its outcome adjusts it
        r = self.scan()
        self.assertEqual(self.proposal(r, "Invoice-1.pdf")["rule_id"], "r-invoice-pdf")
        self.user_move("Invoice-1.pdf", "Documents/Invoices")
        self.scan()
        self.assertIn(("Invoice-1.pdf", "confirmed"), self.observed())
        self.assertCounters("r-invoice-pdf", 0.95, hits=1)

    def test_cached_decision_is_revalidated_against_current_memory(self):
        self.enable_ai()
        mem = self.store.get()
        memmod.find_rule(mem, "r-backup")["confidence"] = 0.6
        self.store.commit()
        self.touch("db-backup.zip")
        reply = {"proposals": [{"name": "db-backup.zip", "action": "move-to", "target": "nas",
                                "confidence": 0.9, "reason": "nas"}]}
        with mock.patch.object(brain, "propose", return_value=reply):
            r = self.scan(no_ai=False)
        self.assertEqual(self.proposal(r, "db-backup.zip")["target"], self.nas)
        self.assertIn("db-backup.zip", self.state["ai_decisions"][self.cwd])
        # the user removes the target from memory: the cached decision is now invalid
        mem = self.store.get()
        mem["targets"] = {}
        memmod.find_rule(mem, "r-backup")["action"] = "review"
        del memmod.find_rule(mem, "r-backup")["target"]
        self.store.commit()
        with mock.patch.object(brain, "propose", return_value={"proposals": []}):
            r = self.scan(no_ai=False)
        p = self.proposal(r, "db-backup.zip")
        self.assertEqual(p["action"], "review")
        self.assertNotIn("db-backup.zip", self.state["ai_decisions"].get(self.cwd, {}))
        # cached entries are keyed on size+mtime: a changed file is re-asked, not replayed
        self.touch("db-backup.zip", 5)
        with mock.patch.object(brain, "propose", return_value={"proposals": []}) as pr:
            self.scan(no_ai=False)
        self.assertTrue(pr.called)

    def test_claude_decisions_produce_outcomes_but_touch_no_rule(self):
        self.enable_ai()
        self.touch("mystery.xyz")
        reply = {"proposals": [{"name": "mystery.xyz", "action": "move", "target": "Misc",
                                "confidence": 0.9, "reason": "misc"}]}
        with mock.patch.object(brain, "propose", return_value=reply):
            self.scan(no_ai=False)
        self.user_move("mystery.xyz", "Misc")
        with mock.patch.object(brain, "propose", return_value={"proposals": []}):
            self.scan(no_ai=False)
        self.assertEqual(self.observed(), [("mystery.xyz", "confirmed")])
        self.assertEqual(self.pending()[0]["proposed"]["decided_by"], "claude")
        self.assertIsNone(self.pending()[0]["proposed"]["rule_id"])
        for r in self.store.get()["rules"]:
            self.assertEqual((r["hits"], r["contradictions"]), (0, 0))


# ================================================================ never touch the directory

class NeverWrites(Fixture):
    """15. The whole lifecycle — scans, outcomes, consolidation — leaves cwd byte-identical."""

    def test_full_lifecycle(self):
        names = ["Bill-1.pdf", "Bill-2.pdf", "db-backup.zip", "old-1.log", "report-q1.txt", "scan.pdf",
                 "scan (1).pdf", "app-1.0.0.zip", "app-1.1.0.zip", "mystery.xyz", "notes.pdf"]
        for n in names:
            self.touch(n, 40)
        os.mkdir(os.path.join(self.cwd, "Documents"))
        self.scan()                                             # Fixture.scan snapshots before/after
        self.user_move("Bill-1.pdf", "Documents/Finance")
        self.user_delete("old-1.log")
        self.user_move("db-backup.zip", "Keep")
        self.user_delete("scan (1).pdf")
        self.user_move("report-q1.txt", "_archive/" + YEAR)
        for _ in range(4):
            self.advance(1)
            self.scan()
        self.assertTrue(len(self.pending()) >= 5)
        expected = tree_snapshot(self.cwd)
        rw = good_rewrite(self.store.get())
        with mock.patch.object(brain, "consolidate", return_value=rw):
            self.assertTrue(engine.consolidate(self.store, threading.RLock(), log=self.logs.append)["applied"])
        self.scan()
        self.assertEqual(tree_snapshot(self.cwd), expected)
        self.assertEqual(sorted(os.listdir(self.cwd)),
                         sorted(["Bill-2.pdf", "Documents", "Keep", "_archive", "scan.pdf", "app-1.0.0.zip",
                                 "app-1.1.0.zip", "mystery.xyz", "notes.pdf"]))
        self.assertEqual(sorted(os.listdir(self.nas)), [])
        # organizer wrote only under its own dirs
        self.assertTrue(os.path.exists(paths.STATE_PATH))
        self.assertTrue(os.path.exists(paths.MEMORY_PATH + ".bak"))
        self.assertEqual(memmod.validate(memmod.load(paths.MEMORY_PATH)), [])

    def test_outcome_detection_is_read_only(self):
        """_find_elsewhere / _resolve_target never create the folders they look for."""
        self.touch("Bill-jan.pdf")
        self.scan()
        self.user_delete("Bill-jan.pdf")
        before = (tree_snapshot(self.cwd), tree_snapshot(self.nas))
        with mock.patch("time.time", return_value=self.now):
            engine.detect_outcomes(self.store.get(), self.state["dirs"][self.cwd], self.cwd, set(), lambda m: None)
        self.assertEqual((tree_snapshot(self.cwd), tree_snapshot(self.nas)), before)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "Documents")))


if __name__ == "__main__":
    unittest.main()
