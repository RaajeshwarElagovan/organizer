"""Stage 2: Claude via the `claude -p` CLI (Claude Code login; no API key, no tools)."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from . import paths
from . import memory as memmod

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
                    "regex": {"type": "string"},
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


def merge_proposals(undecided: list, data: dict, mem: dict) -> list:
    """Overlay Claude decisions on undecided proposals; returns names not answered."""
    by_name = {p["name"]: p for p in undecided}
    answered = set()
    for cp in data.get("proposals", []):
        p = by_name.get(cp.get("name"))
        if not p:
            continue
        action = cp.get("action")
        if action not in memmod.ACTIONS:
            continue
        p["action"] = action
        p["category"] = cp.get("category") or p.get("category")
        target = cp.get("target") or None
        if action in ("move", "archive", "move-to") and not target:
            cat = mem.get("categories", {}).get(p["category"] or "")
            target = cat["dir"] if cat else None
        if action in ("delete", "keep", "review"):
            target = None
        if action == "move" and target:
            target = target.strip("/").replace("\\", "/")
        p["target"] = target
        p["confidence"] = round(max(0.0, min(1.0, float(cp.get("confidence", 0.6)))), 3)
        p["reasons"] = ["claude: " + str(cp.get("reason", "")).strip()]
        p["decided_by"] = "claude"
        p["rule_id"] = None
        answered.add(p["name"])
    return [n for n in by_name if n not in answered]


def merge_new_rules(data: dict, mem: dict) -> list:
    added = []
    for nr in data.get("new_rules", [])[:5]:
        match = {}
        if nr.get("glob"):
            match["glob"] = nr["glob"]
        if nr.get("regex"):
            match["regex"] = nr["regex"]
        if not match or nr.get("action") not in memmod.ACTIONS:
            continue
        if match.get("glob") in ("*", "*.*") or (match.get("glob", "").startswith("*.") and match["glob"].count("*") == 1 and len(match["glob"]) <= 6):
            continue  # reject over-broad globs like "*.pdf"
        dup = any(r.get("match") == match and r.get("action") == nr["action"] for r in mem["rules"])
        if dup:
            continue
        cat = nr.get("category")
        if cat and cat not in mem["categories"]:
            d = nr.get("category_dir") or "/".join(part.replace("-", " ").title().replace(" ", "-") for part in cat.split("/"))
            mem["categories"][cat] = {"dir": d, "confidence": 0.8, "note": nr.get("note", "")}
        rule = {"id": memmod.new_rule_id(mem, match.get("glob") or match.get("regex")), "match": match,
                "category": cat, "action": nr["action"], "scope": "global",
                "confidence": round(max(0.5, min(0.95, float(nr.get("confidence", 0.7)))), 3),
                "hits": 0, "contradictions": 0, "source": "claude", "note": str(nr.get("note", ""))[:200],
                "created": int(time.time())}
        if nr.get("target"):
            rule["target"] = nr["target"]
        if rule["action"] == "move-to" and not rule.get("target"):
            continue
        mem["rules"].append(rule)
        added.append(rule["id"])
    notes = str(data.get("memory_notes") or "").strip()
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
