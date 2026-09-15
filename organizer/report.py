"""Terminal rendering of a scan report."""
import os
import shutil
import time

ACTION_ORDER = ("move", "move-to", "archive", "delete", "review", "keep")
ACTION_LABEL = {"move": "MOVE", "move-to": "MOVE-TO", "archive": "ARCHIVE", "delete": "DELETE",
                "review": "REVIEW", "keep": "KEEP"}


def _use_color():
    return os.isatty(1) and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") not in (None, "dumb")


def _c(code, s, on):
    return "\033[%sm%s\033[0m" % (code, s) if on else s


def _human(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return ("%d%s" % (n, u)) if u == "B" else ("%.1f%s" % (n, u))
        n /= 1024.0


def render_tree(report: dict, color: bool) -> list:
    cwd = report["cwd"]
    props = {p["name"]: p for p in report["proposals"]}
    lines = [_c("1", os.path.basename(cwd) + "/", color)]
    # nested dict of relative folders -> files
    root = {}
    for target, files in report["structure"].items():
        if os.path.isabs(os.path.expanduser(target)):
            continue
        node = root
        for part in target.split("/"):
            node = node.setdefault(part + "/", {})
        for f in files:
            node[f] = None
    for p in report["proposals"]:
        if p["action"] in ("keep", "review"):
            key = p["name"] + ("/" if p["is_dir"] else "")
            root.setdefault(key, None if not p["is_dir"] else {})
    existing = set(report["existing_dirs"])

    def walk(node, prefix, rel):
        items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            tag = ""
            if name.endswith("/"):
                path = (rel + "/" if rel else "") + name[:-1]
                top = path.split("/")[0]
                if top not in existing and not os.path.isdir(os.path.join(cwd, path)):
                    tag = _c("32", "  (+ new)", color)
                elif path in props and props[path]["action"] == "keep":
                    tag = _c("90", "  (keep)", color)
                lines.append(prefix + branch + _c("1;34", name, color) + tag)
                if child:
                    walk(child, prefix + ("    " if last else "│   "), path)
            else:
                p = props.get(name)
                if p and p["action"] == "review":
                    tag = _c("33", "  ? review", color)
                elif p and p["action"] == "keep" and not rel:
                    tag = _c("90", "  (keep)", color)
                lines.append(prefix + branch + name + tag)
    walk(root, "", "")
    for target, files in report["structure"].items():
        if os.path.isabs(os.path.expanduser(target)):
            lines.append(_c("1;35", "→ %s/" % target, color) + _c("90", "  (move-to)", color))
            for f in files:
                lines.append("    " + f)
    dels = [p for p in report["proposals"] if p["action"] == "delete"]
    if dels:
        lines.append(_c("1;31", "✗ delete", color) + _c("90", "  (%s)" % _human(report["summary"]["bytes_to_delete"]), color))
        for p in dels:
            lines.append("    " + p["name"] + _c("90", "  — " + p["reasons"][-1], color))
    return lines


def render_table(report: dict, color: bool, show_keep: bool) -> list:
    width = shutil.get_terminal_size((120, 40)).columns
    lines = []
    for action in ACTION_ORDER:
        rows = [p for p in report["proposals"] if p["action"] == action]
        if not rows or (action == "keep" and not show_keep):
            continue
        code = {"delete": "31", "review": "33", "move": "32", "archive": "36", "move-to": "35", "keep": "90"}[action]
        lines.append("")
        lines.append(_c("1;" + code, "%s (%d)" % (ACTION_LABEL[action], len(rows)), color))
        for p in rows:
            who = {"rule": "R", "heuristic": "H", "claude": "C"}.get(p["decided_by"], "?")
            tgt = p["target"] or ""
            if action == "review" and p.get("tentative", {}).get("target"):
                tgt = "(tentative: %s)" % p["tentative"]["target"]
            head = "  %-38s %8s %5dd  %-26s %s%.2f  " % (
                p["name"][:38], p["size_h"], p["age_days"], tgt[:26], who, p["confidence"])
            why = "; ".join(p["reasons"])
            room = max(20, width - len(head) - 1)
            lines.append(head + _c("90", why[:room], color))
    return lines


def render_text(report: dict, show_keep: bool = False) -> str:
    color = _use_color()
    s = report["summary"]
    acts = ", ".join("%s %d" % (k, v) for k, v in sorted(s["actions"].items()))
    out = []
    out.append(_c("1", "organizer report — %s" % report["cwd"], color))
    out.append(_c("90", "%s · %d entries · %s · %s" % (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(report["scanned_at"])), s["entries"], acts,
        report["note"]), color))
    ai = report["ai"]
    if ai["enabled"]:
        if ai["used"]:
            bits = []
            if ai.get("asked"):
                bits.append("%d asked now%s" % (ai["asked"], (", $%.4f" % ai["cost_usd"]) if ai.get("cost_usd") is not None else ""))
            if ai.get("cached"):
                bits.append("%d from cache" % ai["cached"])
            out.append(_c("90", "Claude decided %d entr%s (%s; %s)%s" % (
                ai["undecided"], "y" if ai["undecided"] == 1 else "ies", ai["model"], ", ".join(bits),
                ("; learned rules: " + ", ".join(ai["new_rules"])) if ai["new_rules"] else ""), color))
        elif ai["undecided"]:
            out.append(_c("33", "Claude stage did not run for %d entries" % ai["undecided"], color))
    for w in report["warnings"]:
        out.append(_c("33", "! " + w, color))
    if report.get("outcomes_recorded"):
        out.append(_c("90", "Learned from %d observed outcome(s) since last scan%s" % (
            report["outcomes_recorded"], " — consolidation due" if report.get("consolidation_due") else ""), color))
    out.append("")
    out += render_tree(report, color)
    out += render_table(report, color, show_keep)
    out.append("")
    out.append(_c("90", "JSON: %s" % report.get("report_path", ""), color))
    return "\n".join(out)


def render_explain(data: dict) -> str:
    f = data["facts"]
    p = data["stage1"]
    lines = ["%s" % f["name"],
             "  type: %s  mime: %s  size: %s  age: %dd (untouched %dd)" % (
                 "dir" if f["is_dir"] else ("." + f["ext"] if f["ext"] else "no-ext"), f.get("mime"),
                 f["size_h"], f["age_days"], f["untouched_days"]),
             "  signals: %s" % (", ".join(f["signals"]) or "-"),
             "  group: %s" % (f.get("group") or "-"),
             "  matching rules: %s" % (", ".join(data["matching_rules"]) or "-"),
             "  stage-1 decision: %s %s (%.2f, %s) — %s" % (
                 p["action"], p["target"] or "", p["confidence"], p["decided_by"], "; ".join(p["reasons"])),
             "  goes to Claude: %s (threshold %.2f)" % ("yes" if p["confidence"] < data["threshold"] else "no", data["threshold"])]
    if data.get("last_reported"):
        lr = data["last_reported"]
        lines.append("  last report: %s %s by %s" % (lr["action"], lr.get("target") or "", lr.get("decided_by")))
    for o in data.get("outcomes", []):
        lines.append("  outcome: proposed %s %s -> observed %s" % (
            o["proposed"]["action"], o["proposed"].get("target") or "", o["observed"]))
    return "\n".join(lines)
