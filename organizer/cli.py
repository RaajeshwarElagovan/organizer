"""organizer — CLI client. Talks to organizerd; falls back to in-process mode."""
import argparse
import json
import os
import sys
import time

from . import __version__, brain, engine, paths, protocol, report as rep, sandbox
from . import memory as memmod


def _fallback_context():
    paths.ensure_dirs()
    brain.start_runner()
    if not sandbox.restrict():
        sys.stderr.write("organizer: no kernel sandbox (%s)\n" % sandbox.status()["error"])
    store = memmod.MemoryStore()
    state = engine.load_state()
    return store, state


def _request(req, args, fallback):
    """Send to daemon unless --no-daemon; on DaemonUnavailable run fallback() in-process."""
    if not getattr(args, "no_daemon", False):
        try:
            resp = protocol.send_request(req)
            if not resp.get("ok"):
                sys.exit("error: %s" % resp.get("error"))
            return resp, True
        except protocol.DaemonUnavailable as e:
            if fallback is None:
                sys.exit("organizer daemon not running (%s). Start it: systemctl --user start organizer" % e)
            sys.stderr.write("organizer: daemon not reachable (%s); running in-process\n" % e)
    if fallback is None:
        sys.exit("this command needs the daemon")
    return fallback(), False


def cmd_scan(args):
    cwd = os.path.abspath(args.path or os.getcwd())
    opts = {"no_ai": args.no_ai, "fresh": args.fresh}

    def fb():
        store, state = _fallback_context()
        return {"ok": True, "report": engine.run_scan(cwd, opts, store, state,
                                                       log=lambda m: sys.stderr.write("organizer: %s\n" % m))}
    resp, _ = _request({"cmd": "scan", "cwd": cwd, "opts": opts}, args, fb)
    report = resp["report"]
    if args.json:
        json.dump(report, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        print(rep.render_text(report, show_keep=args.all))


def cmd_explain(args):
    cwd = os.getcwd()
    name = os.path.basename(args.file.rstrip("/"))
    d = os.path.dirname(os.path.abspath(args.file))
    if d:
        cwd = d

    def fb():
        store, state = _fallback_context()
        return {"ok": True, "explain": engine.explain(cwd, name, store, state)}
    resp, _ = _request({"cmd": "explain", "cwd": cwd, "name": name}, args, fb)
    if args.json:
        print(json.dumps(resp["explain"], indent=1, ensure_ascii=False))
    else:
        print(rep.render_explain(resp["explain"]))


def cmd_history(args):
    cwd = os.path.abspath(args.path or os.getcwd())
    resp, _ = _request({"cmd": "history", "cwd": cwd}, args, lambda: {"ok": True, "history": engine.history(cwd)})
    hist = resp["history"]
    if not hist:
        print("no reports for %s" % cwd)
        return
    for h in hist:
        acts = ", ".join("%s %d" % (k, v) for k, v in sorted(h["summary"].get("actions", {}).items()))
        print("%s  %3d entries  %s%s\n    %s" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(h["scanned_at"] or 0)), h["summary"].get("entries", 0),
            acts, "  [claude]" if h.get("ai_used") else "", h["path"]))


def cmd_status(args):
    try:
        resp = protocol.send_request({"cmd": "status"}, timeout=10)
    except protocol.DaemonUnavailable as e:
        print("daemon: not running (%s)" % e)
        print("memory: %s" % paths.MEMORY_PATH)
        print("start with: systemctl --user start organizer   (or use --no-daemon)")
        sys.exit(1)
    if args.json:
        print(json.dumps(resp, indent=1)); return
    print("daemon: running  pid %s  uptime %ss  socket %s" % (resp["pid"], resp["uptime_s"], resp["socket"]))
    print("memory: %s  (%d rules, %d categories)%s" % (
        resp["memory_path"], resp["rules"], resp["categories"],
        ("  ! " + resp["memory_error"]) if resp.get("memory_error") else ""))
    print("learning: %d pending outcome(s)%s%s" % (
        resp["pending_outcomes"], "  — consolidation due" if resp["consolidation_due"] else "",
        "  (running)" if resp["consolidating"] else ""))
    lc = resp.get("last_consolidation")
    if lc:
        print("last consolidation: %s — %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(lc["ts"])), lc.get("rationale", "")[:200]))
    print("ai: %s  model %s  claude=%s" % ("enabled" if resp["ai_enabled"] else "disabled", resp["ai_model"], resp["claude_cli"] or "NOT FOUND"))
    sb = resp.get("sandbox", {})
    if sb.get("applied"):
        print("sandbox: landlock ABI %s — daemon can only write under: %s" % (sb["abi"], ", ".join(sb["allowed"])))
    else:
        print("sandbox: NONE (%s)" % sb.get("error"))
    print("tracked directories: %d" % resp["dirs_tracked"])


def cmd_reload(args):
    resp, _ = _request({"cmd": "reload"}, args, None)
    if resp.get("error"):
        sys.exit("memory not reloaded: %s" % resp["error"])
    print("memory reloaded (%d rules)" % resp["rules"])


def cmd_learn(args):
    def fb():
        store, _ = _fallback_context()
        return {"ok": True, "result": engine.consolidate(store, None, dry_run=args.dry_run,
                                                          log=lambda m: sys.stderr.write("organizer: %s\n" % m))}
    resp, _ = _request({"cmd": "learn", "dry_run": args.dry_run}, args, fb)
    res = resp["result"]
    if args.json:
        print(json.dumps(res, indent=1, ensure_ascii=False)); return
    if res.get("dry_run"):
        print("DRY RUN — proposed memory rewrite (not applied):")
        print(json.dumps(res["diff"], indent=1))
        print("rationale: %s" % res["rewrite"].get("rationale", ""))
        print("claude_notes:\n%s" % res["rewrite"].get("claude_notes", ""))
    elif res.get("applied"):
        print("memory rewritten. %s" % res.get("rationale", ""))
        print(json.dumps(res["diff"], indent=1))
    else:
        print("not applied: %s" % res.get("reason"))


def cmd_memory(args):
    sub = args.what or "show"
    if sub == "path":
        print(paths.MEMORY_PATH); return
    if sub == "validate":
        try:
            mem = memmod.load(paths.MEMORY_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            sys.exit("INVALID: %s" % e)
        print("valid: %d rules, %d categories, %d pending outcomes" % (
            len(mem["rules"]), len(mem["categories"]), len(mem["learned"]["pending"])))
        return
    if sub == "show":
        try:
            with open(paths.MEMORY_PATH, "r", encoding="utf-8") as f:
                mem = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit("cannot read memory: %s" % e)
        if args.json:
            print(json.dumps(mem, indent=1, ensure_ascii=False)); return
        print("memory: %s" % paths.MEMORY_PATH)
        print("settings: %s" % json.dumps(mem.get("settings", {})))
        print("\ncategories:")
        for k, v in mem.get("categories", {}).items():
            print("  %-24s -> %-24s %s" % (k, v.get("dir"), v.get("note", "")))
        if mem.get("targets"):
            print("\ntargets: %s" % json.dumps(mem["targets"]))
        print("\nrules (%d):" % len(mem.get("rules", [])))
        for r in mem.get("rules", []):
            print("  %-26s %-8s %.2f hits=%-3d contra=%-2d %-8s %s  %s" % (
                r["id"], r["action"], r.get("confidence", 0), r.get("hits", 0), r.get("contradictions", 0),
                r.get("source"), json.dumps(r["match"]), r.get("note", "")[:50]))
        learned = mem.get("learned", {})
        print("\nlearning: %d pending, %d consolidated" % (len(learned.get("pending", [])), len(learned.get("confirmed", []))))
        for o in learned.get("pending", [])[-10:]:
            print("  %s: proposed %s %s -> %s" % (o["name"], o["proposed"]["action"], o["proposed"].get("target") or "", o["observed"]))
        if mem.get("claude_notes"):
            print("\nclaude_notes:\n  " + mem["claude_notes"].replace("\n", "\n  "))
        return
    sys.exit("unknown memory subcommand %r" % sub)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-daemon", action="store_true", default=argparse.SUPPRESS,
                        help="run in-process instead of via organizerd")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    ap = argparse.ArgumentParser(prog="organizer", parents=[common],
                                 description="Report-only file organizer: classifies the current directory by name, type "
                                             "and metadata and proposes a structure. Never modifies files.")
    ap.set_defaults(no_daemon=False, json=False)
    ap.add_argument("--version", action="version", version="organizer " + __version__)
    sp = ap.add_subparsers(dest="cmd", parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    def add_scan(p):
        p.add_argument("path", nargs="?", help="directory to scan (default: cwd)")
        p.add_argument("--all", action="store_true", help="also list entries proposed to keep")
        p.add_argument("--no-ai", action="store_true", help="skip the Claude stage")
        p.add_argument("--fresh", action="store_true", help="ignore cached Claude results")
        p.set_defaults(func=cmd_scan)
    add_scan(sp.add_parser("scan", help="classify a directory and print the proposal (default)"))
    p = sp.add_parser("explain", help="show how one entry is classified and why"); p.add_argument("file"); p.set_defaults(func=cmd_explain)
    p = sp.add_parser("history", help="list saved reports for a directory"); p.add_argument("path", nargs="?"); p.set_defaults(func=cmd_history)
    p = sp.add_parser("status", help="daemon and memory status"); p.set_defaults(func=cmd_status)
    p = sp.add_parser("reload", help="force the daemon to reload memory.json"); p.set_defaults(func=cmd_reload)
    p = sp.add_parser("learn", help="consolidate observed outcomes into memory via Claude now")
    p.add_argument("--dry-run", action="store_true", help="show the proposed rewrite without applying"); p.set_defaults(func=cmd_learn)
    p = sp.add_parser("memory", help="show | validate | path"); p.add_argument("what", nargs="?", choices=["show", "validate", "path"]); p.set_defaults(func=cmd_memory)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    known = {"scan", "explain", "history", "status", "reload", "learn", "memory"}
    # default subcommand: scan  (organizer [path] [flags])
    if not any(a in known for a in argv if not a.startswith("-")):
        argv = ["scan"] + argv if not argv or argv[0] not in ("-h", "--help", "--version") else argv
    # allow global flags after the subcommand too
    args = ap.parse_args(argv)
    if not hasattr(args, "func"):
        ap.print_help(); return
    args.func(args)


if __name__ == "__main__":
    main()
