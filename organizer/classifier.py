"""Stage 1: deterministic classification from facts + memory.

Produces one proposal per entry. Proposals below settings["ai_threshold"] are
"undecided" and are handed to the Claude stage (brain.py) by the engine.
"""
import os
import time

from . import memory as memmod
from .memory import ARCHIVE_DIR


def _target_for(mem: dict, category, action, explicit_target, facts):
    if action == "delete":
        return None
    if action == "keep" or action == "review":
        return None
    if action == "archive":
        year = time.strftime("%Y", time.localtime(facts.get("mtime", time.time())))
        return "%s/%s" % (ARCHIVE_DIR, year)
    if explicit_target:
        t = explicit_target
        if action == "move-to":
            t = mem.get("targets", {}).get(t, t)
        return t
    cat = mem.get("categories", {}).get(category)
    if cat:
        return cat["dir"]
    return None


def make_proposal(facts, action, category=None, target=None, confidence=0.5, reasons=None,
                  rule_id=None, decided_by="heuristic", mem=None):
    return {
        "name": facts["name"],
        "is_dir": bool(facts.get("is_dir")),
        "size": facts.get("size", 0),
        "size_h": facts.get("size_h", ""),
        "age_days": facts.get("age_days", 0),
        "untouched_days": facts.get("untouched_days", 0),
        "ext": facts.get("ext", ""),
        "mime": facts.get("mime"),
        "signals": list(facts.get("signals", [])),
        "category": category,
        "action": action,
        "target": target if target is not None else _target_for(mem or {}, category, action, None, facts),
        "confidence": round(float(confidence), 3),
        "reasons": list(reasons or []),
        "rule_id": rule_id,
        "decided_by": decided_by,
    }


def _category_table(mem: dict, facts: dict):
    """Return (category, confidence, reason) from the categories table, or None."""
    best = None
    for name, cat in mem.get("categories", {}).items():
        conf = float(cat.get("confidence", 0.6))
        sigs = cat.get("signals") or []
        exts = [e.lower() for e in (cat.get("ext") or [])]
        mimes = cat.get("mime_prefix") or []
        hit = None
        if sigs and all(s in facts.get("signals", []) for s in sigs):
            hit = "signals %s" % "+".join(sigs)
        elif exts and facts.get("ext") in exts:
            hit = "extension .%s" % facts["ext"]
        elif mimes and any((facts.get("mime") or "").startswith(mp) for mp in mimes):
            hit = "mime %s" % facts["mime"]
        if hit and (best is None or conf > best[1]):
            best = (name, conf, "category table: %s -> %s" % (hit, name))
    return best


def classify_entry(facts: dict, mem: dict, cwd: str, rules: list) -> dict:
    s = mem["settings"]
    sig = facts.get("signals", [])
    grp = facts.get("group", {})
    ov = mem.get("dir_overrides", {}).get(cwd, {})

    if facts["name"] in ov.get("ignore", []):
        return make_proposal(facts, "keep", confidence=0.99, reasons=["ignored by dir override"],
                             decided_by="rule", mem=mem)

    # --- cross-file heuristics that decide redundancy
    if "duplicate_identical" in sig:
        return make_proposal(facts, "delete", confidence=0.9,
                             reasons=["identical duplicate (same size) of %s" % grp["duplicate_of"]], mem=mem)
    if "extracted_dir_present" in sig:
        return make_proposal(facts, "delete", confidence=0.85,
                             reasons=["already extracted to ./%s/" % grp["extracted_to"]], mem=mem)
    if "series_older" in sig:
        action = "delete" if s.get("delete_policy") == "aggressive" else "archive"
        return make_proposal(facts, action, confidence=0.85,
                             reasons=["older build in series; newest is %s" % grp["series_newest"]], mem=mem)

    # --- memory rules (dir-scoped first, then global)
    for r in rules:
        if memmod.rule_matches(r, facts):
            reasons = ["rule %s (%s)" % (r["id"], r.get("source", "?"))]
            if r.get("note"):
                reasons.append(r["note"])
            if "duplicate_revision" in sig:
                reasons.append("revision of %s (different size)" % grp["duplicate_of"])
            target = _target_for(mem, r.get("category"), r["action"], r.get("target"), facts)
            p = make_proposal(facts, r["action"], r.get("category"), target, r.get("confidence", 0.5),
                              reasons, r["id"], "rule", mem)
            _apply_age_policy(p, facts, s, mem)
            return p

    # --- directories: extracted content is kept; others are for Claude
    if facts.get("is_dir"):
        if "archive_present" in sig:
            return make_proposal(facts, "keep", confidence=0.8,
                                 reasons=["extracted contents of %s" % grp["extracted_from"]], mem=mem)
        organized = {c["dir"].split("/")[0] for c in mem.get("categories", {}).values()} | {ARCHIVE_DIR}
        if facts["name"] in organized:
            return make_proposal(facts, "keep", confidence=0.9, reasons=["organizer folder"], mem=mem)
        return make_proposal(facts, "review", confidence=0.4,
                             reasons=["directory; no rule matched"], mem=mem)

    # --- category table
    hit = _category_table(mem, facts)
    if hit:
        cat, conf, why = hit
        reasons = [why]
        if "duplicate_revision" in sig:
            reasons.append("revision of %s (different size)" % grp["duplicate_of"])
        if "series_newest" in sig:
            reasons.append("newest build in series")
            conf = max(conf, 0.85)
        p = make_proposal(facts, "move", cat, None, conf, reasons, None, "heuristic", mem)
        _apply_age_policy(p, facts, s, mem)
        return p

    return make_proposal(facts, "review", confidence=0.3,
                         reasons=["no rule or category matched"], mem=mem)


def _apply_age_policy(p: dict, facts: dict, s: dict, mem: dict) -> None:
    if p["action"] in ("delete", "keep", "review", "archive"):
        return
    # mtime is the reliable "when did this arrive" signal; atime is only used as a
    # veto (something opened it recently) because indexers/backups also bump it.
    age = facts.get("age_days", 0)
    untouched = facts.get("untouched_days", 0)
    if untouched < int(s["recent_access_veto_days"]) and age > int(s["archive_after_days"]):
        p["reasons"].append("%dd old but accessed %dd ago; not archived" % (age, untouched))
        return
    if age > int(s["stale_after_days"]) and (
            "drive_download" in facts.get("signals", []) or "duplicate_revision" in facts.get("signals", [])):
        p["action"] = "delete"
        p["target"] = None
        p["reasons"].append("stale (%dd old) and disposable" % age)
        p["confidence"] = max(p["confidence"], 0.8)
    elif age > int(s["archive_after_days"]):
        p["action"] = "archive"
        p["target"] = _target_for(mem, None, "archive", None, facts)
        p["reasons"].append("%d days old" % age)
        p["confidence"] = max(p["confidence"], 0.75)


def classify(scan: dict, mem: dict) -> list:
    cwd = scan["cwd"]
    rules = memmod.rules_for_dir(mem, cwd)
    return [classify_entry(e, mem, cwd, rules) for e in scan["entries"]]


def split_undecided(proposals: list, threshold: float):
    decided, undecided = [], []
    for p in proposals:
        (decided if p["confidence"] >= threshold else undecided).append(p)
    return decided, undecided
