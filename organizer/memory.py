"""Memory file: load/validate/save, hot-reload, rule matching, outcome bookkeeping."""
import copy
import fnmatch
import json
import os
import re
import shutil
import time

from . import paths

MEMORY_VERSION = 1
ACTIONS = ("move", "move-to", "archive", "delete", "keep", "review")
SOURCES = ("seed", "claude", "observed", "user")

DEFAULT_SETTINGS = {
    "archive_after_days": 180,
    "stale_after_days": 365,
    "delete_policy": "conservative",   # conservative: older versions -> archive; aggressive: delete
    "use_magic": True,
    "ai_enabled": True,
    "ai_model": "sonnet",
    "ai_threshold": 0.7,
    "ai_max_budget_usd": 0.10,
    "ai_timeout_s": 120,
    "learn_batch": 5,
    "learn_contradictions": 2,
    "ignored_after_scans": 3,
    "ignored_after_days": 3,
    "recent_access_veto_days": 7,
    "max_confirmed_history": 200,
}

MATCH_KEYS = {"glob", "regex", "ext", "mime_prefix", "is_dir", "min_size_mb", "max_size_mb",
              "older_than_days", "signals"}
RULE_KEYS = {"id", "match", "category", "action", "target", "scope", "confidence", "hits",
             "contradictions", "source", "note", "created"}


def empty_memory() -> dict:
    return {
        "version": MEMORY_VERSION,
        "settings": dict(DEFAULT_SETTINGS),
        "categories": {},
        "targets": {},
        "rules": [],
        "dir_overrides": {},
        "learned": {"pending": [], "confirmed": []},
        "claude_notes": "",
    }


def validate(mem) -> list:
    """Return a list of human-readable problems; empty list means valid."""
    errs = []
    if not isinstance(mem, dict):
        return ["memory root must be an object"]
    if mem.get("version") != MEMORY_VERSION:
        errs.append("version must be %d" % MEMORY_VERSION)
    for key, typ in (("settings", dict), ("categories", dict), ("targets", dict), ("rules", list),
                     ("dir_overrides", dict), ("learned", dict), ("claude_notes", str)):
        if key in mem and not isinstance(mem[key], typ):
            errs.append("%s must be %s" % (key, typ.__name__))
    for k in mem.get("settings", {}):
        if k not in DEFAULT_SETTINGS:
            errs.append("settings: unknown key %r" % k)
    for name, cat in mem.get("categories", {}).items():
        if not isinstance(cat, dict) or not isinstance(cat.get("dir"), str):
            errs.append("categories[%r] needs a string 'dir'" % name)
    for name, t in mem.get("targets", {}).items():
        if not isinstance(t, str):
            errs.append("targets[%r] must be a path string" % name)
    ids = set()
    for i, r in enumerate(mem.get("rules", [])):
        where = "rules[%d]" % i
        if not isinstance(r, dict):
            errs.append(where + " must be an object"); continue
        rid = r.get("id")
        if not isinstance(rid, str) or not rid:
            errs.append(where + " needs a string id")
        elif rid in ids:
            errs.append(where + " duplicate id %r" % rid)
        ids.add(rid)
        for k in r:
            if k not in RULE_KEYS:
                errs.append("%s: unknown key %r" % (where, k))
        m = r.get("match")
        if not isinstance(m, dict) or not m:
            errs.append(where + " needs a non-empty 'match' object")
        else:
            for k in m:
                if k not in MATCH_KEYS:
                    errs.append("%s.match: unknown key %r" % (where, k))
            if "regex" in m:
                try:
                    re.compile(m["regex"])
                except re.error as e:
                    errs.append("%s.match.regex invalid: %s" % (where, e))
        if r.get("action") not in ACTIONS:
            errs.append("%s action must be one of %s" % (where, ",".join(ACTIONS)))
        if r.get("action") == "move-to" and not r.get("target"):
            errs.append(where + " move-to requires a target")
        c = r.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            errs.append(where + " confidence must be 0..1")
        if r.get("source") not in SOURCES:
            errs.append("%s source must be one of %s" % (where, ",".join(SOURCES)))
        cat = r.get("category")
        if cat is not None and cat not in mem.get("categories", {}) and r.get("action") in ("move",):
            errs.append("%s category %r is not defined in categories" % (where, cat))
    for d, ov in mem.get("dir_overrides", {}).items():
        if not isinstance(ov, dict):
            errs.append("dir_overrides[%r] must be an object" % d); continue
        for k in ov:
            if k not in ("rules", "ignore", "note"):
                errs.append("dir_overrides[%r]: unknown key %r" % (d, k))
    learned = mem.get("learned", {})
    for k in ("pending", "confirmed"):
        if k in learned and not isinstance(learned[k], list):
            errs.append("learned.%s must be a list" % k)
    return errs


def normalize(mem: dict) -> dict:
    base = empty_memory()
    for k, v in base.items():
        mem.setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULT_SETTINGS.items():
        mem["settings"].setdefault(k, v)
    mem["learned"].setdefault("pending", [])
    mem["learned"].setdefault("confirmed", [])
    for r in mem["rules"]:
        r.setdefault("scope", "global")
        r.setdefault("hits", 0)
        r.setdefault("contradictions", 0)
        r.setdefault("note", "")
    return mem


def load(path: str = paths.MEMORY_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        mem = json.load(f)
    errs = validate(mem)
    if errs:
        raise ValueError("memory.json invalid: " + "; ".join(errs[:8]))
    return normalize(mem)


def save(mem: dict, path: str = paths.MEMORY_PATH, backup: bool = True) -> None:
    errs = validate(mem)
    if errs:
        raise ValueError("refusing to save invalid memory: " + "; ".join(errs[:8]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def seed_if_missing(path: str = paths.MEMORY_PATH) -> bool:
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(paths.SEED_MEMORY):
        shutil.copy(paths.SEED_MEMORY, path)
    else:
        save(empty_memory(), path, backup=False)
    return True


class MemoryStore:
    """Owns the in-memory copy and hot-reloads when the file changes on disk."""

    def __init__(self, path: str = paths.MEMORY_PATH):
        self.path = path
        self.mem = None
        self.mtime = None
        self.last_error = None
        seed_if_missing(path)
        self.reload(force=True)

    def _stat_mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def reload(self, force: bool = False) -> bool:
        m = self._stat_mtime()
        if not force and m == self.mtime:
            return False
        try:
            self.mem = load(self.path)
            self.mtime = m
            self.last_error = None
            return True
        except (OSError, ValueError, json.JSONDecodeError) as e:
            self.last_error = str(e)
            if self.mem is None:
                self.mem = normalize(empty_memory())
            return False

    def get(self) -> dict:
        self.reload()
        return self.mem

    def commit(self) -> None:
        save(self.mem, self.path)
        self.mtime = self._stat_mtime()


# ---------------------------------------------------------------- rule matching

def rule_matches(rule: dict, facts: dict) -> bool:
    m = rule.get("match", {})
    name = facts["name"]
    if "glob" in m and not fnmatch.fnmatch(name.lower(), m["glob"].lower()):
        return False
    if "regex" in m and not re.search(m["regex"], name, re.I):
        return False
    if "ext" in m:
        exts = m["ext"] if isinstance(m["ext"], list) else [m["ext"]]
        if facts.get("ext", "") not in [e.lower().lstrip(".") for e in exts]:
            return False
    if "mime_prefix" in m:
        mime = facts.get("mime") or ""
        if not mime.startswith(m["mime_prefix"]):
            return False
    if "is_dir" in m and bool(facts.get("is_dir")) != bool(m["is_dir"]):
        return False
    size_mb = facts.get("size", 0) / (1024 * 1024)
    if "min_size_mb" in m and size_mb < m["min_size_mb"]:
        return False
    if "max_size_mb" in m and size_mb > m["max_size_mb"]:
        return False
    if "older_than_days" in m and facts.get("untouched_days", 0) <= m["older_than_days"]:
        return False
    if "signals" in m:
        want = m["signals"] if isinstance(m["signals"], list) else [m["signals"]]
        if not all(s in facts.get("signals", []) for s in want):
            return False
    return True


def rules_for_dir(mem: dict, cwd: str):
    """Dir-scoped rules first (override + scope), then global, each by confidence desc."""
    ov = mem.get("dir_overrides", {}).get(cwd, {})
    scoped = [r for r in ov.get("rules", []) if isinstance(r, dict)]
    scoped += [r for r in mem["rules"] if r.get("scope") not in (None, "global") and r["scope"] == cwd]
    glob = [r for r in mem["rules"] if r.get("scope") in (None, "global")]
    key = lambda r: -float(r.get("confidence", 0))
    return sorted(scoped, key=key) + sorted(glob, key=key)


def find_rule(mem: dict, rule_id: str):
    for r in mem["rules"]:
        if r.get("id") == rule_id:
            return r
    for ov in mem.get("dir_overrides", {}).values():
        for r in ov.get("rules", []):
            if r.get("id") == rule_id:
                return r
    return None


def new_rule_id(mem: dict, hint: str) -> str:
    base = "r-" + re.sub(r"[^a-z0-9]+", "-", hint.lower()).strip("-")[:32] or "r-rule"
    rid, n = base, 1
    while find_rule(mem, rid):
        n += 1
        rid = "%s-%d" % (base, n)
    return rid


# ---------------------------------------------------------------- learning bookkeeping

def adjust_rule(mem: dict, rule_id: str, outcome: str) -> None:
    """Immediate, deterministic confidence bookkeeping. Never invents rules."""
    r = find_rule(mem, rule_id) if rule_id else None
    if not r:
        return
    floor = 0.5 if r.get("source") == "claude" else 0.2
    c = float(r.get("confidence", 0.5))
    if outcome == "confirmed":
        r["hits"] = int(r.get("hits", 0)) + 1
        r["confidence"] = round(min(0.99, c + 0.05), 3)
    elif outcome in ("moved_elsewhere", "deleted"):
        r["contradictions"] = int(r.get("contradictions", 0)) + 1
        r["confidence"] = round(max(floor, c - 0.1), 3)
    elif outcome == "ignored":
        r["confidence"] = round(max(floor, c - 0.05), 3)


def add_pending(mem: dict, outcome: dict) -> None:
    outcome.setdefault("ts", int(time.time()))
    mem["learned"]["pending"].append(outcome)


def needs_consolidation(mem: dict) -> bool:
    s = mem["settings"]
    if len(mem["learned"]["pending"]) >= int(s["learn_batch"]):
        return True
    limit = int(s["learn_contradictions"])
    return any(int(r.get("contradictions", 0)) >= limit for r in mem["rules"])


def apply_consolidation(mem: dict, rewrite: dict) -> list:
    """Merge a Claude rewrite into mem. Returns list of problems (empty = applied)."""
    problems = []
    for k in ("rules", "categories", "targets", "claude_notes"):
        if k not in rewrite:
            problems.append("rewrite missing %r" % k)
    if problems:
        return problems
    if mem["rules"] and len(rewrite["rules"]) < len(mem["rules"]) * 0.5:
        return ["rewrite drops more than 50%% of rules (%d -> %d)" % (len(mem["rules"]), len(rewrite["rules"]))]
    candidate = copy.deepcopy(mem)
    candidate["rules"] = rewrite["rules"]
    candidate["categories"] = rewrite["categories"]
    candidate["targets"] = rewrite["targets"]
    candidate["claude_notes"] = str(rewrite["claude_notes"])[:8000]
    for r in candidate["rules"]:
        r.setdefault("source", "claude")
        r.setdefault("scope", "global")
        r.setdefault("hits", 0)
        r["contradictions"] = 0
        r.setdefault("note", "")
        if not r.get("id"):
            r["id"] = new_rule_id(candidate, r.get("match", {}).get("glob", "rule"))
    errs = validate(candidate)
    if errs:
        return errs
    confirmed = candidate["learned"]["confirmed"]
    confirmed.extend(candidate["learned"]["pending"])
    keep = int(candidate["settings"]["max_confirmed_history"])
    candidate["learned"]["confirmed"] = confirmed[-keep:]
    candidate["learned"]["pending"] = []
    candidate["last_consolidation"] = {"ts": int(time.time()), "rationale": str(rewrite.get("rationale", ""))[:2000]}
    mem.clear()
    mem.update(candidate)
    return []
