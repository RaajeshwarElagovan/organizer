"""Stage 2: Claude via the `claude -p` CLI (Claude Code login; no API key, no tools)."""
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from . import paths
from . import memory as memmod
from .memory import ARCHIVE_DIR

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "action": {"type": "string", "enum": list(memmod.ACTIONS)},
                    "target": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "action", "confidence", "reason"],
            },
        },
        "new_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "glob": {"type": "string"},
                    "category": {"type": "string"},
                    "category_dir": {"type": "string"},
                    "action": {"type": "string", "enum": list(memmod.ACTIONS)},
                    "target": {"type": "string"},
                    "confidence": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["action", "confidence", "note"],
            },
        },
        "memory_notes": {"type": "string"},
    },
    "required": ["proposals"],
}

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "match": {"type": "object"},
        "category": {"type": "string"},
        "action": {"type": "string", "enum": list(memmod.ACTIONS)},
        "target": {"type": "string"},
        "scope": {"type": "string"},
        "confidence": {"type": "number"},
        "hits": {"type": "integer"},
        "source": {"type": "string", "enum": list(memmod.SOURCES)},
        "note": {"type": "string"},
    },
    "required": ["id", "match", "action", "confidence", "source"],
}

LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": RULE_SCHEMA},
        "categories": {"type": "object", "additionalProperties": {"type": "object"}},
        "targets": {"type": "object", "additionalProperties": {"type": "string"}},
        "claude_notes": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["rules", "categories", "targets", "claude_notes", "rationale"],
}


class BrainError(Exception):
    pass


class _Runner:
    """Runs claude subprocesses on a thread created before the Landlock sandbox is
    applied, so the CLI can write its own state under ~/.claude. Calls are serialized."""

    def __init__(self):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="claude-runner", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            fn, args, done = self.q.get()
            try:
                done.put((fn(*args), None))
            except BaseException as e:  # propagate to caller
                done.put((None, e))

    def call(self, fn, *args):
        done = queue.Queue()
        self.q.put((fn, args, done))
        result, err = done.get()
        if err:
            raise err
        return result


_runner = None


def start_runner():
    """Call once, before sandbox.restrict(), from the thread that will be restricted."""
    global _runner
    if _runner is None:
        _runner = _Runner()


def _exec(cmd, user_prompt, timeout, env):
    return subprocess.run(cmd, input=user_prompt, capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=paths.HOME)


def claude_path():
    return shutil.which("claude") or (
        os.path.join(paths.HOME, ".local", "bin", "claude")
        if os.path.exists(os.path.join(paths.HOME, ".local", "bin", "claude")) else None)


def _read_prompt(name: str) -> str:
    with open(os.path.join(paths.PROMPTS_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def run_claude(system_prompt: str, user_prompt: str, schema: dict, settings: dict, log=None) -> dict:
    exe = claude_path()
    if not exe:
        raise BrainError("claude CLI not found on PATH")
    # --tools "" + --setting-sources "" + --strict-mcp-config: no built-in tools, no MCP
    # servers, no hooks/CLAUDE.md from settings. (--bare is not used: it skips credential
    # reads and breaks the Claude Code login.)
    cmd = [exe, "-p", "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
           "--output-format", "json", "--model", str(settings.get("ai_model", "sonnet")),
           "--max-turns", "1", "--max-budget-usd", str(settings.get("ai_max_budget_usd", 0.1)),
           "--json-schema", json.dumps(schema), "--system-prompt", system_prompt]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.setdefault("HOME", paths.HOME)
    if log:
        log("claude: %s" % " ".join(cmd[:9] + ["..."]))
    t0 = time.time()
    timeout = float(settings.get("ai_timeout_s", 120))
    try:
        if _runner is not None:
            proc = _runner.call(_exec, cmd, user_prompt, timeout, env)
        else:
            proc = _exec(cmd, user_prompt, timeout, env)
    except subprocess.TimeoutExpired:
        raise BrainError("claude timed out after %ss" % settings.get("ai_timeout_s"))
    except OSError as e:
        raise BrainError("could not run claude: %s" % e)
    if proc.returncode != 0:
        raise BrainError("claude exited %d: %s" % (proc.returncode, (proc.stderr or proc.stdout).strip()[:400]))
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise BrainError("claude returned non-JSON output: %s" % proc.stdout[:200])
    if isinstance(outer, list):  # stream-ish output: take the result record
        outer = next((o for o in outer if isinstance(o, dict) and o.get("type") == "result"), {})
    if outer.get("is_error"):
        raise BrainError("claude error: %s" % str(outer.get("result", ""))[:400])
    data = outer.get("structured_output")
    if data is None:
        raw = outer.get("result", "")
        if isinstance(raw, dict):
            data = raw
        else:
            raw = str(raw).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw[raw.find("{"):]
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise BrainError("claude result is not JSON: %s" % raw[:200])
    if not isinstance(data, dict):
        raise BrainError("claude result is not a JSON object (%s)" % type(data).__name__)
    if log:
        log("claude: ok in %.1fs, cost $%.4f" % (time.time() - t0, float(outer.get("total_cost_usd", 0) or 0)))
    data["_meta"] = {"cost_usd": outer.get("total_cost_usd"), "duration_s": round(time.time() - t0, 1),
                     "model": settings.get("ai_model")}
    return data


# ---------------------------------------------------------------- proposals

def _compact_entry(p: dict) -> str:
    bits = ["%s" % p["name"], "dir" if p["is_dir"] else ("." + p["ext"] if p["ext"] else "no-ext"),
            p["size_h"], "%dd old" % p["age_days"]]
    if p.get("mime"):
        bits.append(p["mime"])
    if p.get("signals"):
        bits.append("signals=" + ",".join(p["signals"]))
    bits.append("tentative=%s%s" % (p["action"], (" " + p["target"]) if p.get("target") else ""))
    return " | ".join(bits)


def build_propose_prompt(undecided, decided, existing_dirs, mem, cwd) -> str:
    lines = ["Scanned directory: %s" % cwd, "",
             "EXISTING SUB-FOLDERS: %s" % (", ".join(existing_dirs) or "(none)"), "",
             "ALREADY DECIDED (for structural consistency; do not re-decide):"]
    for p in decided:
        lines.append("- %s -> %s%s" % (p["name"], p["action"], (" " + p["target"]) if p.get("target") else ""))
    lines += ["", "UNDECIDED (decide each of these):"]
    for p in undecided:
        lines.append("- " + _compact_entry(p))
    cats = {k: {kk: vv for kk, vv in v.items() if kk in ("dir", "note")} for k, v in mem.get("categories", {}).items()}
    rules = [{"id": r["id"], "match": r["match"], "action": r["action"], "category": r.get("category"),
              "target": r.get("target"), "confidence": r.get("confidence"), "note": r.get("note", "")}
             for r in mem.get("rules", [])]
    confirmed = mem["learned"]["confirmed"][-20:]
    lines += ["", "MEMORY.categories: " + json.dumps(cats, ensure_ascii=False),
              "MEMORY.targets: " + json.dumps(mem.get("targets", {})),
              "MEMORY.rules: " + json.dumps(rules, ensure_ascii=False),
              "MEMORY.claude_notes: " + (mem.get("claude_notes") or "(empty)")]
    if confirmed:
        lines.append("RECENT OBSERVED OUTCOMES: " + json.dumps(
            [{"name": c.get("name"), "proposed": c.get("proposed", {}).get("action"),
              "target": c.get("proposed", {}).get("target"), "observed": c.get("observed")} for c in confirmed]))
    return "\n".join(lines)


def propose(undecided, decided, existing_dirs, mem, cwd, log=None) -> dict:
    settings = mem["settings"]
    prompt = build_propose_prompt(undecided, decided, existing_dirs, mem, cwd)
    data = run_claude(_read_prompt("system_propose.md"), prompt, PROPOSAL_SCHEMA, settings, log)
    return data


MAX_REASON_LEN = 300
_WS = re.compile(r"\s+")


class _Reject(Exception):
    """A Claude-generated item failed application-side validation."""


def _num(v, lo=0.0, hi=1.0):
    """Finite number clamped to [lo, hi]; bool/str/NaN/inf are rejected."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _Reject("confidence must be a finite number")
    return round(max(lo, min(hi, float(v))), 3)


def _text(v, limit):
    """Model-supplied free text: collapse whitespace/control chars, cap length."""
    return _WS.sub(" ", memmod._CTRL_CHARS.sub(" ", str(v if v is not None else ""))).strip()[:limit]


def _archive_default(p: dict) -> str:
    mtime = time.time() - float(p.get("age_days", 0) or 0) * 86400
    return "%s/%s" % (ARCHIVE_DIR, time.strftime("%Y", time.localtime(mtime)))


def _inside(cwd: str, rel: str) -> bool:
    """The realpath of cwd/rel must stay under realpath(cwd) — catches symlinked sub-dirs."""
    root = os.path.realpath(cwd)
    dest = os.path.realpath(os.path.join(root, rel))
    return dest.startswith(root.rstrip(os.sep) + os.sep)


def validate_target(action: str, target, category, mem: dict, cwd=None, p=None):
    """Return the destination for a Claude-decided action, or raise _Reject.

    move     -> relative path (Claude's target, else the category's dir); stays under cwd
    archive  -> relative path under _archive/ (default _archive/<year of the entry>)
    move-to  -> configured target name or ~/... path
    others   -> None
    """
    if action in ("delete", "keep", "review"):
        return None
    if action == "move-to":
        t = memmod.clean_move_to_target(target, mem)
        if t is None:
            raise _Reject("move-to target %r is not a configured target or a ~/ path" % (target,))
        return t
    if action == "archive":
        if target in (None, ""):
            return _archive_default(p or {})
        t = memmod.clean_archive_target(target)
        if t is None:
            raise _Reject("archive target %r must be a relative path under %s/" % (target, ARCHIVE_DIR))
    else:  # move
        if target in (None, ""):
            cat = mem.get("categories", {}).get(category or "")
            target = cat.get("dir") if isinstance(cat, dict) else None
            if target is None:
                raise _Reject("move without a target or known category")
        t = memmod.clean_relative_target(target)
        if t is None:
            raise _Reject("move target %r must be a relative path without '..'" % (target,))
    if cwd and not _inside(cwd, t):
        raise _Reject("target %r resolves outside the scanned directory" % (t,))
    return t


def _check_proposal(cp, p: dict, mem: dict, cwd):
    """Validate one Claude proposal against the entry it claims to decide."""
    if not isinstance(cp, dict):
        raise _Reject("proposal is not an object")
    action = cp.get("action")
    if action not in memmod.ACTIONS:
        raise _Reject("unknown action %r" % (action,))
    conf = _num(cp.get("confidence"))
    category = cp.get("category")
    if category in (None, ""):
        category = p.get("category")
    elif memmod.clean_category_key(category) is None:
        raise _Reject("invalid category %r" % (category,))
    target = validate_target(action, cp.get("target"), category, mem, cwd, p)
    return {"action": action, "category": category, "target": target, "confidence": conf, "reason": _text(cp.get("reason"), MAX_REASON_LEN)}


def merge_proposals(undecided: list, data: dict, mem: dict, cwd=None) -> dict:
    """Overlay validated Claude decisions on undecided proposals.

    Returns {name: reason} for every entry that was not decided — either Claude
    did not answer for it or its answer failed validation. Nothing from `data`
    reaches a proposal without passing _check_proposal.
    """
    by_name = {p["name"]: p for p in undecided}
    answered, rejected = set(), {}
    items = data.get("proposals") if isinstance(data, dict) else None
    for cp in items if isinstance(items, list) else []:
        name = cp.get("name") if isinstance(cp, dict) else None
        p = by_name.get(name) if isinstance(name, str) else None
        if not p or name in answered:
            continue
        try:
            ok = _check_proposal(cp, p, mem, cwd)
        except _Reject as e:
            rejected[name] = "Claude proposal rejected: %s" % e
            continue
        p["action"] = ok["action"]
        p["category"] = ok["category"]
        p["target"] = ok["target"]
        p["confidence"] = ok["confidence"]
        p["reasons"] = ["claude: " + ok["reason"]]
        p["decided_by"] = "claude"
        p["rule_id"] = None
        answered.add(name)
        rejected.pop(name, None)
    out = {}
    for n in by_name:
        if n not in answered:
            out[n] = rejected.get(n, "Claude did not answer for this entry")
    return out


def _broad_glob(g: str) -> bool:
    g = g.strip().lower()
    return g in ("*", "*.*", "?", "**") or (g.startswith("*.") and g.count("*") == 1 and len(g) <= 6)


def _check_new_rule(nr, mem: dict) -> dict:
    """Validate a Claude-suggested rule; returns a memory rule dict or raises _Reject.

    Model-generated rules match by `glob` only. `regex` is a user-only match key
    (MEMORY-GUIDE.md): a regex the model wrote would run against every file name
    on every scan, and match_problems() is only a heuristic against backtracking
    (THREAT-MODEL.md), so it is refused here regardless of its content.
    """
    if not isinstance(nr, dict):
        raise _Reject("rule is not an object")
    if "regex" in nr:
        raise _Reject("regex rules are user-only; model rules must use glob")
    match = {}
    if nr.get("glob") not in (None, ""):
        match["glob"] = nr["glob"]
    if not match:
        raise _Reject("rule needs a glob")
    probs = memmod.match_problems(match)
    if probs:
        raise _Reject("; ".join(probs))
    if "glob" in match and _broad_glob(match["glob"]):
        raise _Reject("over-broad glob %r" % match["glob"])
    action = nr.get("action")
    if action not in memmod.ACTIONS:
        raise _Reject("unknown action %r" % (action,))
    conf = _num(nr.get("confidence", 0.7), 0.5, 0.95)
    cat = nr.get("category")
    if cat in (None, ""):
        cat = None
    elif memmod.clean_category_key(cat) is None:
        raise _Reject("invalid category %r" % (cat,))
    cat_dir = None
    if cat and cat not in mem.get("categories", {}):
        raw = nr.get("category_dir")
        if raw in (None, ""):
            raw = "/".join(part.replace("-", " ").title().replace(" ", "-") for part in cat.split("/"))
        cat_dir = memmod.clean_relative_target(raw)
        if cat_dir is None:
            raise _Reject("invalid category_dir %r" % (raw,))
    rule = {"match": match, "category": cat, "action": action, "scope": "global", "confidence": conf,
            "hits": 0, "contradictions": 0, "source": "claude", "note": _text(nr.get("note"), 200),
            "created": int(time.time())}
    target = nr.get("target")
    if target not in (None, ""):
        if action == "move-to":
            rule["target"] = memmod.clean_move_to_target(target, mem)
        elif action == "archive":
            rule["target"] = memmod.clean_archive_target(target)
        elif action == "move":
            rule["target"] = memmod.clean_relative_target(target)
        else:
            target = None
        if target is not None and rule.get("target") is None:
            raise _Reject("invalid target %r for action %s" % (target, action))
    if action == "move-to" and not rule.get("target"):
        raise _Reject("move-to rule needs a target")
    if action == "move" and not rule.get("target") and not cat:
        raise _Reject("move rule needs a target or category")
    return rule, cat_dir


def merge_new_rules(data: dict, mem: dict, log=None) -> list:
    added = []
    items = data.get("new_rules") if isinstance(data, dict) else None
    for nr in (items if isinstance(items, list) else [])[:5]:
        try:
            rule, cat_dir = _check_new_rule(nr, mem)
        except _Reject as e:
            if log:
                log("claude: new rule rejected: %s" % e)
            continue
        if any(r.get("match") == rule["match"] and r.get("action") == rule["action"] for r in mem["rules"]):
            continue
        if cat_dir is not None:
            mem["categories"][rule["category"]] = {"dir": cat_dir, "confidence": 0.8, "note": rule["note"]}
        rule["id"] = memmod.new_rule_id(mem, rule["match"]["glob"])
        mem["rules"].append(rule)
        added.append(rule["id"])
    notes = _text(data.get("memory_notes") if isinstance(data, dict) else "", 2000)
    if notes:
        combined = (mem.get("claude_notes", "") + "\n" + notes).strip()
        mem["claude_notes"] = combined[-2000:]
    return added


# ---------------------------------------------------------------- consolidation

def build_learn_prompt(mem: dict) -> str:
    rules = [{k: v for k, v in r.items() if k != "created"} for r in mem["rules"]]
    return "\n".join([
        "CURRENT MEMORY",
        "categories: " + json.dumps(mem["categories"], ensure_ascii=False),
        "targets: " + json.dumps(mem["targets"]),
        "rules: " + json.dumps(rules, ensure_ascii=False),
        "claude_notes: " + (mem.get("claude_notes") or "(empty)"),
        "",
        "OBSERVED OUTCOMES (pending, newest last):",
        json.dumps(mem["learned"]["pending"], ensure_ascii=False),
        "",
        "EARLIER OUTCOMES (already consolidated, for context):",
        json.dumps([{"name": c.get("name"), "proposed": c.get("proposed"), "observed": c.get("observed")}
                    for c in mem["learned"]["confirmed"][-50:]], ensure_ascii=False),
    ])


def consolidate(mem: dict, log=None) -> dict:
    """Ask Claude for a rewritten memory. Returns the rewrite dict (not applied)."""
    return run_claude(_read_prompt("system_learn.md"), build_learn_prompt(mem), LEARN_SCHEMA,
                      mem["settings"], log)
