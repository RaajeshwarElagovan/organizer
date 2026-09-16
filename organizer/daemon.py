"""organizerd — Unix-socket daemon that owns memory/state and serves scan requests."""
import errno
import json
import os
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback

from . import brain, engine, paths, protocol, sandbox
from . import memory as memmod
from .memory import MemoryStore

LOCK = threading.RLock()
STORE = None
STATE = None
STARTED = time.time()
CONSOLIDATING = threading.Event()


def log(msg):
    sys.stderr.write("[organizerd] %s\n" % msg)
    sys.stderr.flush()


def _maybe_consolidate_async():
    mem = STORE.get()
    if not memmod.needs_consolidation(mem) or CONSOLIDATING.is_set():
        return
    if not mem["settings"].get("ai_enabled", True):
        return
    CONSOLIDATING.set()

    def worker():
        try:
            engine.consolidate(STORE, LOCK, dry_run=False, log=log)
        except Exception:
            log("consolidation crashed:\n" + traceback.format_exc())
        finally:
            CONSOLIDATING.clear()
    threading.Thread(target=worker, name="consolidate", daemon=True).start()


def handle(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "status":
        mem = STORE.get()
        return {"ok": True, "pid": os.getpid(), "uptime_s": int(time.time() - STARTED),
                "socket": paths.socket_path(), "memory_path": STORE.path,
                "memory_error": STORE.last_error, "rules": len(mem["rules"]),
                "categories": len(mem["categories"]), "pending_outcomes": len(mem["learned"]["pending"]),
                "consolidation_due": memmod.needs_consolidation(mem), "consolidating": CONSOLIDATING.is_set(),
                "dirs_tracked": len(STATE["dirs"]), "ai_enabled": mem["settings"].get("ai_enabled"),
                "ai_model": mem["settings"].get("ai_model"), "claude_cli": brain.claude_path(),
                "last_consolidation": mem.get("last_consolidation"), "sandbox": sandbox.status()}
    if cmd == "reload":
        changed = STORE.reload(force=True)
        return {"ok": True, "reloaded": changed, "error": STORE.last_error, "rules": len(STORE.get()["rules"])}
    if cmd == "scan":
        with LOCK:
            report = engine.run_scan(req["cwd"], req.get("opts", {}), STORE, STATE, log)
        _maybe_consolidate_async()
        return {"ok": True, "report": report}
    if cmd == "explain":
        with LOCK:
            return {"ok": True, "explain": engine.explain(req["cwd"], req["name"], STORE, STATE)}
    if cmd == "history":
        return {"ok": True, "history": engine.history(req["cwd"])}
    if cmd == "learn":
        if CONSOLIDATING.is_set():
            return {"ok": False, "error": "a consolidation is already running"}
        CONSOLIDATING.set()
        try:
            res = engine.consolidate(STORE, LOCK, dry_run=bool(req.get("dry_run")), log=log)
        finally:
            CONSOLIDATING.clear()
        return {"ok": True, "result": res}
    return {"ok": False, "error": "unknown command %r" % cmd}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = protocol.read_message(self.connection)
            if req is None:
                return
            try:
                resp = handle(req)
            except Exception as e:  # keep serving
                log("error handling %s: %s\n%s" % (req.get("cmd"), e, traceback.format_exc()))
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            protocol.write_message(self.connection, resp)
        except (OSError, ValueError) as e:
            log("connection error: %s" % e)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def claim_socket(sock: str) -> None:
    """Remove a stale socket file left by a crashed daemon; refuse to start if another
    daemon still answers on it (otherwise two daemons would fight over state.json and
    the first one's shutdown would unlink the second one's socket)."""
    try:
        st = os.lstat(sock)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise SystemExit("[organizerd] %s exists and is not a socket; remove it or set ORGANIZER_SOCKET" % sock)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(sock)
    except OSError as e:
        if e.errno != errno.ECONNREFUSED:
            raise SystemExit("[organizerd] cannot probe %s: %s" % (sock, e))
    else:
        raise SystemExit("[organizerd] another organizerd is already listening on %s" % sock)
    finally:
        probe.close()
    log("removing stale socket %s" % sock)
    os.remove(sock)


def main():
    global STORE, STATE
    try:
        paths.ensure_dirs()
    except (OSError, RuntimeError) as e:
        raise SystemExit("[organizerd] cannot set up directories: %s" % e)
    brain.start_runner()          # unrestricted helper thread for the claude CLI
    if sandbox.restrict():        # this thread + everything it spawns: read-only outside own dirs
        st = sandbox.status()
        log("landlock ABI %s: writes allowed only under %s" % (st["abi"], ", ".join(st["allowed"])))
    else:
        log("WARNING: no kernel sandbox (%s); relying on code-level read-only behaviour" % sandbox.status()["error"])
    STORE = MemoryStore()
    if STORE.last_error:
        log("WARNING: %s" % STORE.last_error)
    STATE = engine.load_state()
    sock = paths.socket_path()
    claim_socket(sock)
    old_umask = os.umask(0o077)   # 0600 from the moment the socket file exists
    try:
        server = Server(sock, Handler)
    finally:
        os.umask(old_umask)
    os.chmod(sock, 0o600)
    own_ino = os.stat(sock).st_ino
    log("listening on %s (memory %s, %d rules)" % (sock, STORE.path, len(STORE.get()["rules"])))

    def stop(signum, frame):
        log("signal %d, shutting down" % signum)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if os.lstat(sock).st_ino == own_ino:   # not a socket a newer daemon bound meanwhile
                os.remove(sock)
        except OSError:
            pass


if __name__ == "__main__":
    main()
