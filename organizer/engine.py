"""Shared scan pipeline used by both the daemon and the in-process CLI fallback.

scan -> outcome detection (learning layer 1) -> classifier -> Claude stage ->
state + report persistence -> optional consolidation trigger (learning layer 2).
Nothing in here modifies files outside organizer's own config/data dirs.
"""
import copy
import glob
import json
import os
import time

from . import brain, classifier, memory as memmod, paths, scanner
from .memory import MemoryStore

STATE_VERSION = 1
AI_CACHE_TTL = 7 * 86400
MAX_REPORTS_PER_DIR = 20


def _log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        with open(paths.STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if st.get("version") != STATE_VERSION:
            raise ValueError
        st.setdefault("dirs", {})
        st.setdefault("ai_decisions", {})
        return st
    except (OSError, ValueError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "dirs": {}, "ai_decisions": {}}


def save_state(state: dict) -> None:
    paths.ensure_dirs()
    tmp = paths.STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, paths.STATE_PATH)


# ---------------------------------------------------------------- learning layer 1

def _resolve_target(cwd: str, target):
    if not target:
        return None
    t = os.path.expanduser(target)
    return t if os.path.isabs(t) else os.path.join(cwd, t)


def _find_elsewhere(cwd: str, name: str, mem: dict, exclude: str):
    """Shallow search (depth 2 under cwd, plus configured targets) for a moved file."""
    candidates = []
    try:
        with os.scandir(cwd) as it:
            level1 = [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        level1 = []
    for d in level1:
        candidates.append(d)
        try:
            with os.scandir(d) as it:
                candidates += [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
    for t in mem.get("targets", {}).values():
        candidates.append(os.path.expanduser(t))
    for d in candidates:
        if exclude and os.path.abspath(d) == os.path.abspath(exclude):
            continue
        if os.path.exists(os.path.join(d, name)):
            rel = os.path.relpath(d, cwd)
            return d if rel.startswith("..") else rel
    return None


def detect_outcomes(mem: dict, dstate: dict, cwd: str, current: set, log=_log) -> list:
    outcomes = []
    prev = dstate.get("proposals", {})
    limit = int(mem["settings"]["ignored_after_scans"])
    min_age = float(mem["settings"]["ignored_after_days"]) * 86400
    now = time.time()
    for name, p in prev.items():
        action = p.get("action")
        if action in ("keep", "review"):
            continue
        if name in current:
            p["seen_scans"] = int(p.get("seen_scans", 0)) + 1
            old_enough = now - float(p.get("first_ts", p.get("ts", now))) >= min_age
            if p["seen_scans"] >= limit and old_enough and not p.get("ignored_recorded"):
                p["ignored_recorded"] = True
                outcomes.append(_outcome(name, p, "ignored"))
            continue
        target_path = _resolve_target(cwd, p.get("target"))
        if target_path and os.path.exists(os.path.join(target_path, name)):
            outcomes.append(_outcome(name, p, "confirmed"))
        elif action == "delete":
            outcomes.append(_outcome(name, p, "confirmed"))
        else:
            where = _find_elsewhere(cwd, name, mem, target_path)
            if where:
                outcomes.append(_outcome(name, p, "moved_elsewhere:" + where))
            else:
                outcomes.append(_outcome(name, p, "deleted"))
    for o in outcomes:
        kind = o["observed"].split(":", 1)[0]
        memmod.adjust_rule(mem, o["proposed"].get("rule_id"), kind)
        memmod.add_pending(mem, o)
        log("outcome: %s -> %s (proposed %s %s)" % (o["name"], o["observed"], o["proposed"]["action"],
                                                    o["proposed"].get("target") or ""))
    return outcomes


def _outcome(name, p, observed):
    return {"name": name, "cwd_proposal_ts": p.get("ts"),
            "facts": {"ext": p.get("ext"), "size": p.get("size"), "signals": p.get("signals", [])[:6]},
            "proposed": {"action": p.get("action"), "target": p.get("target"), "rule_id": p.get("rule_id"),
                         "decided_by": p.get("decided_by"), "category": p.get("category")},
            "observed": observed}


# ---------------------------------------------------------------- pipeline

def run_scan(cwd: str, opts: dict, store: MemoryStore, state: dict, log=_log) -> dict:
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise FileNotFoundError("not a directory: %s" % cwd)
    mem = store.get()
    settings = mem["settings"]
    warnings = []
    if store.last_error:
        warnings.append("memory.json could not be reloaded (%s); using last good copy" % store.last_error)

    scan = scanner.scan_dir(cwd, settings)
    names = {e["name"] for e in scan["entries"]}
    dstate = state["dirs"].setdefault(cwd, {"scans": 0, "proposals": {}})
    outcomes = detect_outcomes(mem, dstate, cwd, names, log)

    proposals = classifier.classify(scan, mem)
    threshold = float(settings["ai_threshold"])
    decided, undecided = classifier.split_undecided(proposals, threshold)

    ai = {"enabled": bool(settings.get("ai_enabled", True)) and not opts.get("no_ai"),
          "undecided": len(undecided), "used": False, "cached": 0, "asked": 0, "error": None,
          "new_rules": [], "cost_usd": None, "model": settings.get("ai_model")}
    if undecided and ai["enabled"]:
        # per-entry cache: a Claude decision is reused while the entry's facts are unchanged
        cache = state.setdefault("ai_decisions", {}).setdefault(cwd, {})
        facts_by_name = {e["name"]: e for e in scan["entries"]}
        to_ask, cached_data = [], {"proposals": []}
        for p in undecided:
            key = _entry_key(facts_by_name[p["name"]])
            c = cache.get(p["name"])
            if c and c.get("key") == key and not opts.get("fresh") and time.time() - c.get("ts", 0) < AI_CACHE_TTL:
                cached_data["proposals"].append(c["decision"])
            else:
                to_ask.append(p)
        if cached_data["proposals"]:
            hit = [p for p in undecided if p not in to_ask]
            brain.merge_proposals(hit, cached_data, mem)
            for p in hit:
                p["reasons"].append("(cached)")
            ai["cached"] = len(hit)
            ai["used"] = True
        if to_ask:
            ai["asked"] = len(to_ask)
            try:
                data = brain.propose(to_ask, decided + [p for p in undecided if p not in to_ask],
                                     scan["existing_dirs"], mem, cwd, log)
                ai["cost_usd"] = data.get("_meta", {}).get("cost_usd")
                ai["new_rules"] = brain.merge_new_rules(data, mem)
                unanswered = brain.merge_proposals(to_ask, data, mem)
                now_ts = int(time.time())
                for cp in data.get("proposals", []):
                    if cp.get("name") in facts_by_name and cp.get("name") not in unanswered:
                        cache[cp["name"]] = {"key": _entry_key(facts_by_name[cp["name"]]), "ts": now_ts, "decision": cp}
                for p in to_ask:
                    if p["name"] in unanswered:
                        _to_review(p, "Claude did not answer for this entry")
                ai["used"] = True
            except brain.BrainError as e:
                ai["error"] = str(e)
                warnings.append("Claude stage unavailable: %s — undecided entries left as review" % e)
                log("brain error: %s" % e)
                for p in to_ask:
                    _to_review(p, "below confidence threshold; Claude stage failed")
        _prune_cache(state, cwd, names)
    else:
        for p in undecided:
            _to_review(p, "below confidence threshold; AI stage %s" % (
                "disabled" if not ai["enabled"] else "skipped"))

    # persist proposals for outcome detection next time
    now = int(time.time())
    new_props = {}
    for p in proposals:
        old = dstate["proposals"].get(p["name"])
        same = old and old.get("action") == p["action"] and old.get("target") == p["target"]
        new_props[p["name"]] = {
            "action": p["action"], "target": p["target"], "rule_id": p["rule_id"], "decided_by": p["decided_by"],
            "category": p["category"], "ext": p["ext"], "size": p["size"], "signals": p["signals"], "ts": now,
            "first_ts": old.get("first_ts", old.get("ts", now)) if same else now,
            "seen_scans": old.get("seen_scans", 0) if same else 0,
            "ignored_recorded": old.get("ignored_recorded", False) if same else False,
        }
    dstate["proposals"] = new_props
    dstate["scans"] = int(dstate.get("scans", 0)) + 1
    dstate["last_scan"] = now
    save_state(state)
    try:
        store.commit()
    except ValueError as e:
        warnings.append("memory not saved: %s" % e)

    report = build_report(cwd, scan, proposals, warnings, ai, outcomes, mem)
    report["report_path"] = save_report(cwd, report)
    report["consolidation_due"] = memmod.needs_consolidation(mem)
    return report


def _to_review(p: dict, why: str) -> None:
    if p["action"] != "review":
        p["tentative"] = {"action": p["action"], "target": p["target"], "category": p["category"]}
    p["action"] = "review"
    p["target"] = None
    p["reasons"].append(why)


def _entry_key(facts: dict) -> str:
    return "%s|%s" % (facts.get("size"), facts.get("mtime"))


def _prune_cache(state: dict, cwd: str, names: set) -> None:
    cache = state.get("ai_decisions", {}).get(cwd, {})
    now = time.time()
    for k in [k for k, v in cache.items() if k not in names or now - v.get("ts", 0) > AI_CACHE_TTL]:
        del cache[k]


# ---------------------------------------------------------------- report structure

def build_report(cwd, scan, proposals, warnings, ai, outcomes, mem) -> dict:
    counts = {}
    for p in proposals:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    new_dirs, tree = set(), {}
    for p in proposals:
        if p["action"] in ("move", "archive") and p["target"]:
            tree.setdefault(p["target"], []).append(p["name"])
            parts = p["target"].split("/")
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if not os.path.isdir(os.path.join(cwd, d)):
                    new_dirs.add(d)
        elif p["action"] == "move-to" and p["target"]:
            tree.setdefault(p["target"], []).append(p["name"])
    return {
        "tool": "organizer", "version": __import__("organizer").__version__,
        "cwd": cwd, "scanned_at": scan["scanned_at"], "report_only": True,
        "note": "Report only — no files were modified.",
        "summary": {"entries": len(proposals), "actions": counts, "new_dirs": sorted(new_dirs),
                    "bytes_to_delete": sum(p["size"] for p in proposals if p["action"] == "delete")},
        "warnings": warnings,
        "ai": ai,
        "outcomes_recorded": len(outcomes),
        "existing_dirs": scan["existing_dirs"],
        "structure": {d: sorted(v) for d, v in sorted(tree.items())},
        "proposals": sorted(proposals, key=lambda p: (p["action"], p["target"] or "", p["name"].lower())),
        "memory": {"rules": len(mem["rules"]), "pending_outcomes": len(mem["learned"]["pending"]),
                   "path": paths.MEMORY_PATH},
    }


def save_report(cwd: str, report: dict) -> str:
    paths.ensure_dirs()
    d = os.path.join(paths.REPORTS_DIR, paths.dir_slug(cwd))
    os.makedirs(d, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(report["scanned_at"]))
    path = os.path.join(d, ts + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    latest = os.path.join(d, "latest.json")
    tmp = latest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    os.replace(tmp, latest)
    old = sorted(p for p in glob.glob(os.path.join(d, "*.json")) if not p.endswith("latest.json"))
    for p in old[:-MAX_REPORTS_PER_DIR]:
        try:
            os.remove(p)
        except OSError:
            pass
    return path


def history(cwd: str) -> list:
    d = os.path.join(paths.REPORTS_DIR, paths.dir_slug(os.path.abspath(cwd)))
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        if p.endswith("latest.json"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                r = json.load(f)
            out.append({"path": p, "scanned_at": r.get("scanned_at"), "summary": r.get("summary", {}),
                        "ai_used": r.get("ai", {}).get("used")})
        except (OSError, ValueError):
            pass
    return out


# ---------------------------------------------------------------- explain

def explain(cwd: str, name: str, store: MemoryStore, state: dict) -> dict:
    cwd = os.path.abspath(cwd)
    mem = store.get()
    scan = scanner.scan_dir(cwd, mem["settings"])
    entry = next((e for e in scan["entries"] if e["name"] == name), None)
    if not entry:
        raise FileNotFoundError("%s not found in %s" % (name, cwd))
    rules = memmod.rules_for_dir(mem, cwd)
    matching = [r["id"] for r in rules if memmod.rule_matches(r, entry)]
    proposal = classifier.classify_entry(entry, mem, cwd, rules)
    last = state["dirs"].get(cwd, {}).get("proposals", {}).get(name)
    hist = [o for o in mem["learned"]["pending"] + mem["learned"]["confirmed"] if o.get("name") == name]
    return {"facts": entry, "matching_rules": matching, "stage1": proposal, "last_reported": last,
            "outcomes": hist[-5:], "threshold": mem["settings"]["ai_threshold"]}


# ---------------------------------------------------------------- learning layer 2

def consolidate(store: MemoryStore, lock=None, dry_run: bool = False, log=_log) -> dict:
    mem = store.get()
    if not mem["learned"]["pending"]:
        return {"applied": False, "reason": "no pending outcomes"}
    snapshot = copy.deepcopy(mem)
    n_pending = len(snapshot["learned"]["pending"])
    log("consolidation: asking Claude to rewrite memory from %d outcomes" % n_pending)
    try:
        rewrite = brain.consolidate(snapshot, log)
    except brain.BrainError as e:
        log("consolidation failed: %s" % e)
        return {"applied": False, "reason": str(e)}
    diff = _diff_summary(snapshot, rewrite)
    if dry_run:
        return {"applied": False, "dry_run": True, "rewrite": {k: v for k, v in rewrite.items() if k != "_meta"},
                "diff": diff}
    if lock:
        lock.acquire()
    try:
        cur = store.get()
        tail = cur["learned"]["pending"][n_pending:]
        problems = memmod.apply_consolidation(cur, rewrite)
        if problems:
            log("consolidation rejected: %s" % "; ".join(problems))
            return {"applied": False, "reason": "; ".join(problems), "diff": diff}
        cur["learned"]["pending"] = tail
        store.commit()
    finally:
        if lock:
            lock.release()
    log("consolidation applied: %s" % rewrite.get("rationale", ""))
    return {"applied": True, "rationale": rewrite.get("rationale", ""), "diff": diff,
            "cost_usd": rewrite.get("_meta", {}).get("cost_usd")}


def _diff_summary(old: dict, new: dict) -> dict:
    old_ids = {r["id"] for r in old["rules"]}
    new_ids = {r.get("id") for r in new.get("rules", [])}
    return {
        "rules_added": sorted(i for i in new_ids - old_ids if i),
        "rules_removed": sorted(old_ids - new_ids),
        "rules_changed": sorted(r.get("id") for r in new.get("rules", []) if r.get("id") in old_ids and
                                r != next(o for o in old["rules"] if o["id"] == r.get("id"))),
        "categories_added": sorted(set(new.get("categories", {})) - set(old["categories"])),
        "categories_removed": sorted(set(old["categories"]) - set(new.get("categories", {}))),
        "targets": new.get("targets", {}),
        "claude_notes_len": len(str(new.get("claude_notes", ""))),
    }
