"""Memory file: load/validate/save, hot-reload, rule matching, outcome bookkeeping."""
import copy
import fnmatch
import json
import math
import os
import re
import shutil
import time

from . import paths

MEMORY_VERSION = 1
ACTIONS = ("move", "move-to", "archive", "delete", "keep", "review")
SOURCES = ("seed", "claude", "observed", "user")
ARCHIVE_DIR = "_archive"

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


# ---------------------------------------------------------------- target paths
#
# Path semantics (see MEMORY-GUIDE.md / THREAT-MODEL.md):
#   move     -> folder relative to the scanned directory
#   archive  -> relative, always beneath ARCHIVE_DIR
#   move-to  -> a name from `targets`, or a `~/...` path (absolute destination)
# These helpers are the single place that decides whether a destination is
# acceptable. Claude output, memory edits and consolidation rewrites all pass
# through them; the prompt and --json-schema are not relied upon.

_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# `(a+)+`, `(\w+\s?)*`, `(.*x){20}`: a quantified group whose last item is itself
# quantified. This is a heuristic for the textbook form only — see THREAT-MODEL.md.
_NESTED_QUANTIFIER = re.compile(r"(?<!\\)[*+?}]\)[*+{]")
_DRIVE = re.compile(r"^[A-Za-z]:")
MAX_TARGET_LEN = 200


def clean_relative_target(t):
    """Normalise a folder path relative to the scanned dir -> 'A/B', or None.

    Rejects non-strings, empty values, control characters, absolute paths
    (/, \\, C:), home-relative paths (~) and any '..' segment. Backslashes are
    treated as separators; empty and '.' segments are dropped.
    """
    if not isinstance(t, str):
        return None
    t = t.strip()
    if not t or len(t) > MAX_TARGET_LEN or _CTRL_CHARS.search(t):
        return None
    t = t.replace("\\", "/")
    if t.startswith(("/", "~")) or _DRIVE.match(t):
        return None
    parts = [seg.strip() for seg in t.split("/")]
    parts = [seg for seg in parts if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    out = "/".join(parts)
    return None if out.startswith("~") else out


def clean_archive_target(t):
    """Like clean_relative_target but the first segment must be ARCHIVE_DIR."""
    c = clean_relative_target(t)
    if c is None or c.split("/")[0] != ARCHIVE_DIR:
        return None
    return c


def clean_target_value(v):
    """Validate a `targets` value (an absolute destination): '~/x' or '/x'.

    Returns the normalised, still-unexpanded string, or None. Rejects '..',
    backslashes, control characters, bare '~' and '/'.
    """
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or len(v) > MAX_TARGET_LEN or _CTRL_CHARS.search(v) or "\\" in v:
        return None
    if v.startswith("~/"):
        rest = clean_relative_target(v[2:])
        return None if rest is None else "~/" + rest
    if v.startswith("/"):
        rest = clean_relative_target(v.lstrip("/"))
        return None if rest is None else "/" + rest
    return None


def under_home(v) -> bool:
    """True if a (validated) target value resolves beneath the user's home."""
    c = clean_target_value(v)
    if c is None:
        return False
    home = os.path.normpath(paths.HOME)
    full = os.path.normpath(os.path.expanduser(c))
    return full.startswith(home + os.sep)


def clean_move_to_target(t, mem):
    """Resolve a move-to destination -> unexpanded absolute-ish path, or None.

    Accepted: a key of mem['targets'] (resolved to its configured value), or a
    '~/...' path beneath the home directory. Anything else — bare absolute
    paths, relative paths, traversal — is rejected.
    """
    if not isinstance(t, str):
        return None
    t = t.strip()
    if not t or len(t) > MAX_TARGET_LEN or _CTRL_CHARS.search(t):
        return None
    configured = mem.get("targets", {}).get(t)
    if configured is not None:
        return clean_target_value(configured)
    if not t.replace("\\", "/").startswith("~/"):
        return None
    c = clean_target_value(t.replace("\\", "/"))
    return c if c is not None and under_home(c) else None


def clean_category_key(k):
    """Category keys look like 'documents/finance'; same shape rules as a relative path."""
    c = clean_relative_target(k)
    if c is None or len(c) > 64:
        return None
    return c


def rule_target_problem(rule: dict, mem: dict):
    """Return a problem string if a rule's target is invalid for its action, else None."""
    action, target = rule.get("action"), rule.get("target")
    if action in ("delete", "keep", "review"):
        return None
    if action == "move-to":
        if not target:
            return "move-to requires a target"
        if clean_move_to_target(target, mem) is None:
            return "move-to target %r must be a name in targets or a ~/ path" % (target,)
        return None
    if target is None:
        return None
    if action == "archive":
        return None if clean_archive_target(target) is not None else \
            "archive target %r must be a relative path under %s/" % (target, ARCHIVE_DIR)
    if clean_relative_target(target) is None:
        return "target %r must be a relative path without '..'" % (target,)
    return None


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


def match_problems(m: dict) -> list:
    """Type-check a rule's match object so a bad rule cannot crash classification."""
    errs = []
    if "glob" in m and not (isinstance(m["glob"], str) and m["glob"].strip() and len(m["glob"]) <= 128):
        errs.append("glob must be a non-empty string")
    if "regex" in m:
        if not (isinstance(m["regex"], str) and m["regex"] and len(m["regex"]) <= 200):
            errs.append("regex must be a non-empty string (max 200 chars)")
        else:
            try:
                re.compile(m["regex"])
            except re.error as e:
                errs.append("regex invalid: %s" % e)
            else:
                if _NESTED_QUANTIFIER.search(m["regex"]):
                    errs.append("regex quantifies a group that ends in a quantifier (catastrophic backtracking)")
    for k in ("ext", "signals"):
        if k in m:
            v = m[k] if isinstance(m[k], list) else [m[k]]
            if not v or not all(isinstance(x, str) for x in v):
                errs.append("%s must be a string or list of strings" % k)
    if "mime_prefix" in m and not isinstance(m["mime_prefix"], str):
        errs.append("mime_prefix must be a string")
    if "is_dir" in m and not isinstance(m["is_dir"], bool):
        errs.append("is_dir must be a boolean")
    for k in ("min_size_mb", "max_size_mb", "older_than_days"):
        if k in m and (isinstance(m[k], bool) or not isinstance(m[k], (int, float))):
            errs.append("%s must be a number" % k)
    return errs


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
    for k, v in mem.get("settings", {}).items():
        if k not in DEFAULT_SETTINGS:
            errs.append("settings: unknown key %r" % k); continue
        want = DEFAULT_SETTINGS[k]
        if isinstance(want, bool):
            if not isinstance(v, bool):
                errs.append("settings.%s must be true/false" % k)
        elif isinstance(want, str):
            if not isinstance(v, str):
                errs.append("settings.%s must be a string" % k)
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            errs.append("settings.%s must be a non-negative number" % k)
        elif k == "ai_threshold" and v > 1:
            errs.append("settings.ai_threshold must be 0..1")
    for name, cat in mem.get("categories", {}).items():
        if clean_category_key(name) is None:
            errs.append("categories: invalid key %r" % (name,)); continue
        if not isinstance(cat, dict) or not isinstance(cat.get("dir"), str):
            errs.append("categories[%r] needs a string 'dir'" % name); continue
        if clean_relative_target(cat["dir"]) is None:
            errs.append("categories[%r].dir %r must be a relative path without '..'" % (name, cat["dir"]))
        for lk in ("ext", "signals", "mime_prefix"):
            v = cat.get(lk)
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                errs.append("categories[%r].%s must be a list of strings" % (name, lk))
        c = cat.get("confidence", 0.6)
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
            errs.append("categories[%r].confidence must be 0..1" % name)
    for name, t in mem.get("targets", {}).items():
        if not isinstance(name, str) or not name or _CTRL_CHARS.search(name) or "/" in name:
            errs.append("targets: invalid name %r" % (name,))
        if clean_target_value(t) is None:
            errs.append("targets[%r] must be an absolute or ~/ path without '..'" % name)
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
        else:
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
            errs += ["%s.match.%s" % (where, e) for e in match_problems(m)]
        if r.get("action") not in ACTIONS:
            errs.append("%s action must be one of %s" % (where, ",".join(ACTIONS)))
        tp = rule_target_problem(r, mem)
        if tp:
            errs.append("%s %s" % (where, tp))
        c = r.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
            errs.append(where + " confidence must be 0..1")
        scope = r.get("scope", "global")
        if scope != "global" and not (isinstance(scope, str) and os.path.isabs(scope)):
            errs.append(where + " scope must be 'global' or an absolute directory path")
        if r.get("source") not in SOURCES:
            errs.append("%s source must be one of %s" % (where, ",".join(SOURCES)))
        for k in ("hits", "contradictions"):
            v = r.get(k, 0)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                errs.append("%s %s must be a non-negative integer" % (where, k))
        if not isinstance(r.get("note", ""), str):
            errs.append(where + " note must be a string")
        cat = r.get("category")
        if cat is not None and not isinstance(cat, str):
            errs.append(where + " category must be a string")
        elif cat is not None and cat not in mem.get("categories", {}) and r.get("action") in ("move",):
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
        """Change stamp: mtime alone can collide with the daemon's own write on
        coarse-timestamp filesystems, so size and inode are part of it."""
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
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
        """Write the in-memory copy back. Refused while the file on disk is one we
        could not load: overwriting it would destroy a human/agent edit that only
        needs fixing (and .bak would lose it on the following write)."""
        if self.last_error and os.path.exists(self.path):
            raise ValueError("memory.json on disk is invalid and was not overwritten (%s); "
                             "fix it or restore memory.json.bak, then `organizer reload`" % self.last_error)
        save(self.mem, self.path)
        self.mtime = self._stat_mtime()
        self.last_error = None


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
        r["confidence"] = round(max(min(floor, c), c - 0.1), 3)
    elif outcome == "ignored":
        r["confidence"] = round(max(min(floor, c), c - 0.05), 3)


def add_pending(mem: dict, outcome: dict) -> None:
    outcome.setdefault("ts", int(time.time()))
    mem["learned"]["pending"].append(outcome)


def needs_consolidation(mem: dict) -> bool:
    s = mem["settings"]
    if len(mem["learned"]["pending"]) >= int(s["learn_batch"]):
        return True
    limit = int(s["learn_contradictions"])
    return any(int(r.get("contradictions", 0)) >= limit for r in mem["rules"])


def _regex_rules_introduced(mem: dict, rules: list) -> list:
    """`regex` is a user-only match key. A consolidation rewrite may carry an existing
    regex rule through unchanged (pattern-for-pattern), but may not add one or edit
    a pattern: the model's regexes would otherwise run against every file name on
    every scan, and match_problems() is only a heuristic (THREAT-MODEL.md)."""
    existing = set()
    for r in mem.get("rules", []):
        m = r.get("match") if isinstance(r, dict) else None
        if isinstance(m, dict) and isinstance(m.get("regex"), str):
            existing.add(m["regex"])
    errs = []
    for i, r in enumerate(rules):
        rx = r.get("match", {}).get("regex")
        if rx is not None and rx not in existing:
            errs.append("rules[%d] (%s): regex rules are user-only; a rewrite may keep an existing regex "
                        "verbatim but must use glob for new or changed patterns" % (i, r.get("id")))
    return errs


def apply_consolidation(mem: dict, rewrite: dict) -> list:
    """Merge a Claude rewrite into mem. Returns list of problems (empty = applied)."""
    problems = []
    for k in ("rules", "categories", "targets", "claude_notes"):
        if k not in rewrite:
            problems.append("rewrite missing %r" % k)
    if problems:
        return problems
    if not isinstance(rewrite["rules"], list) or not all(isinstance(r, dict) for r in rewrite["rules"]):
        return ["rewrite.rules must be a list of objects"]
    if not isinstance(rewrite["categories"], dict) or not isinstance(rewrite["targets"], dict):
        return ["rewrite.categories and rewrite.targets must be objects"]
    if mem["rules"] and len(rewrite["rules"]) < len(mem["rules"]) * 0.5:
        return ["rewrite drops more than 50%% of rules (%d -> %d)" % (len(mem["rules"]), len(rewrite["rules"]))]
    # Claude may keep existing targets verbatim but may only introduce destinations under ~.
    for name, v in rewrite["targets"].items():
        if mem.get("targets", {}).get(name) == v:
            continue
        if not under_home(v):
            problems.append("rewrite.targets[%r] = %r: new targets must be under ~" % (name, v))
    if problems:
        return problems
    candidate = copy.deepcopy(mem)
    candidate["rules"] = copy.deepcopy(rewrite["rules"])
    candidate["categories"] = copy.deepcopy(rewrite["categories"])
    candidate["targets"] = dict(rewrite["targets"])
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
    errs = _regex_rules_introduced(mem, candidate["rules"])
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
