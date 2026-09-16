"""Adversarial probe for organizer's Landlock boundary. Run by tests/test_sandbox.py.

The probe executes *inside* a process that went through organizer's real
startup (cli._fallback_context() or daemon.main()) and tries every kind of
filesystem modification against a victim directory that lies outside the
allowed roots. Each attempt records whether the kernel blocked it. The test
process then also compares a before/after snapshot of the victim tree, so a
"blocked" verdict is never taken on faith.

Modes (argv[1]):
  inprocess   -- cli._fallback_context(), then attacks from: the restricted
                 thread, a thread started afterwards, a subprocess, and the
                 pre-Landlock brain._runner thread (documented escape).
  daemon      -- runs organizer.daemon.main() with a test-only `_probe`
                 command that executes the battery in a request-handler thread.
  nolandlock  -- like inprocess but with landlock made unavailable, to pin the
                 current fail-open behaviour.
Results are JSON on stdout (inprocess/nolandlock) or via the socket (daemon).
"""
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

BLOCK_ERRNOS = {errno.EACCES, errno.EPERM, errno.EXDEV, errno.EROFS}


def snapshot(root):
    """lstat + content hash of every entry under root (symlinks not followed)."""
    out = {}
    for dp, dns, fns in os.walk(root, followlinks=False):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            h = None
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as f:
                    h = hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(p, root)] = [st.st_mode, st.st_size, st.st_mtime_ns, st.st_nlink, h,
                                             os.readlink(p) if os.path.islink(p) else None]
    st = os.lstat(root)
    out["."] = [st.st_mode, None, st.st_mtime_ns, None, None, None]
    return out


def _attempt(results, name, fn, expect_blocked=True):
    try:
        fn()
    except OSError as e:
        blocked = e.errno in BLOCK_ERRNOS
        results.append({"name": name, "blocked": blocked, "errno": errno.errorcode.get(e.errno, str(e.errno)),
                        "expect_blocked": expect_blocked, "ok": blocked == expect_blocked})
        return
    results.append({"name": name, "blocked": False, "errno": None, "expect_blocked": expect_blocked,
                    "ok": not expect_blocked})


def _touch_rm(path):
    def f():
        open(path, "x").close()
        os.remove(path)
    return f


def _symlink_once(src, dst):
    def f():
        if not os.path.lexists(dst):
            os.symlink(src, dst)
    return f


def victim_only_attacks(victim):
    """Small battery that touches nothing but the victim dir. Used when the
    sandbox is known to be inactive (fail-open test) so no real file elsewhere
    is ever created."""
    R = []
    V = lambda *p: os.path.join(victim, *p)
    _attempt(R, "write: open existing 'w'", lambda: open(V("existing.txt"), "w").close())
    _attempt(R, "create: open 'x'", lambda: open(V("new.txt"), "x").close())
    _attempt(R, "create: mkdir", lambda: os.mkdir(V("newdir")))
    _attempt(R, "rename: within victim", lambda: os.rename(V("dup (1).txt"), V("renamed.txt")))
    _attempt(R, "delete: os.remove file", lambda: os.remove(V("renamed.txt")))
    _attempt(R, "subprocess: sh touch victim/new", lambda: subprocess.run(
        ["sh", "-c", "touch '%s'" % V("sub.txt")], check=True, capture_output=True))
    return R


def attacks(victim, allowed, tmp_victim=None):
    """victim: dir outside the allowed roots; allowed: a dir organizer may write to
    (its data dir). Returns list of {name, blocked, errno, expect_blocked, ok}.

    Only call this when the sandbox is active: several probes aim at $HOME and
    /etc and rely on the kernel to refuse them."""
    R = []
    V = lambda *p: os.path.join(victim, *p)
    A = lambda *p: os.path.join(allowed, *p)
    home = os.path.expanduser("~")

    def wr(path, mode="w"):
        return lambda: open(path, mode).close()

    def creat(path, flags):
        return lambda: os.close(os.open(path, flags, 0o644))

    # ---- sanity: reads must still work, and the allowed dir must be writable
    _attempt(R, "control: read victim file", wr(V("existing.txt"), "r"), expect_blocked=False)
    _attempt(R, "control: list victim dir", lambda: os.listdir(victim), expect_blocked=False)
    _attempt(R, "control: write inside allowed dir", wr(A("mine.txt"), "a"), expect_blocked=False)

    # ---- normal writes
    _attempt(R, "write: open existing 'w'", wr(V("existing.txt")))
    _attempt(R, "write: open existing 'a'", wr(V("existing.txt"), "a"))
    _attempt(R, "write: open existing 'r+'", wr(V("existing.txt"), "r+"))
    _attempt(R, "write: os.open O_WRONLY", creat(V("existing.txt"), os.O_WRONLY))
    _attempt(R, "write: os.open O_RDWR", creat(V("existing.txt"), os.O_RDWR))
    _attempt(R, "write: os.truncate", lambda: os.truncate(V("existing.txt"), 0))
    _attempt(R, "write: O_TRUNC", creat(V("existing.txt"), os.O_WRONLY | os.O_TRUNC))

    # ---- creation
    _attempt(R, "create: open 'x'", wr(V("new.txt"), "x"))
    _attempt(R, "create: O_CREAT|O_EXCL", creat(V("new2.txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL))
    _attempt(R, "create: mkdir", lambda: os.mkdir(V("newdir")))
    _attempt(R, "create: makedirs nested", lambda: os.makedirs(V("a", "b", "c")))
    _attempt(R, "create: mkdir _archive (organizer's own folder name)", lambda: os.mkdir(V("_archive")))
    _attempt(R, "create: symlink", lambda: os.symlink("existing.txt", V("newlink")))
    _attempt(R, "create: hardlink", lambda: os.link(V("existing.txt"), V("hard.txt")))
    _attempt(R, "create: mkfifo", lambda: os.mkfifo(V("fifo")))
    _attempt(R, "create: hardlink of victim file into allowed dir",
             lambda: os.link(V("existing.txt"), A("stolen-hardlink")))

    # ---- deletion
    _attempt(R, "delete: os.remove file", lambda: os.remove(V("existing.txt")))
    _attempt(R, "delete: os.unlink symlink", lambda: os.unlink(V("slink")))
    _attempt(R, "delete: os.rmdir empty dir", lambda: os.rmdir(V("emptydir")))
    _attempt(R, "delete: shutil.rmtree", lambda: shutil.rmtree(V("dir")))
    _attempt(R, "delete: rmtree victim root", lambda: shutil.rmtree(victim))

    # ---- rename
    _attempt(R, "rename: within victim", lambda: os.rename(V("existing.txt"), V("renamed.txt")))
    _attempt(R, "rename: os.replace within victim", lambda: os.replace(V("existing.txt"), V("renamed.txt")))
    _attempt(R, "rename: dir within victim", lambda: os.rename(V("dir"), V("dir2")))
    _attempt(R, "rename: victim file -> allowed dir (exfiltrate)", lambda: os.rename(V("existing.txt"), A("pulled.txt")))
    _attempt(R, "rename: allowed file -> victim (inject)", lambda: os.rename(A("mine.txt"), V("pushed.txt")))
    _attempt(R, "rename: into proposed sub-folder", lambda: os.rename(V("existing.txt"), V("dir", "existing.txt")))

    # ---- symlink escapes (symlinks live in the ALLOWED dir, point at the victim)
    _attempt(R, "symlink escape: create link in allowed dir -> victim",
             _symlink_once(victim, A("to_victim")), expect_blocked=False)   # creating it is harmless
    _attempt(R, "symlink escape: write through allowed/to_victim/existing.txt", wr(A("to_victim", "existing.txt")))
    _attempt(R, "symlink escape: create through allowed/to_victim/new.txt", wr(A("to_victim", "new3.txt"), "x"))
    _attempt(R, "symlink escape: delete through allowed/to_victim", lambda: os.remove(A("to_victim", "existing.txt")))
    _attempt(R, "symlink escape: mkdir through allowed/to_victim", lambda: os.mkdir(A("to_victim", "d")))
    _attempt(R, "symlink escape: rename through allowed/to_victim",
             lambda: os.rename(A("to_victim", "existing.txt"), A("to_victim", "x.txt")))
    # nested: allowed/l1 -> allowed/l2 -> victim
    _attempt(R, "nested symlink: create chain",
             lambda: (_symlink_once(A("l2"), A("l1"))(), _symlink_once(victim, A("l2"))()), expect_blocked=False)
    _attempt(R, "nested symlink: write through allowed/l1/existing.txt", wr(A("l1", "existing.txt")))
    _attempt(R, "nested symlink: create through allowed/l1/new.txt", wr(A("l1", "new4.txt"), "x"))
    _attempt(R, "nested symlink: unlink through allowed/l1", lambda: os.unlink(A("l1", "slink")))
    # existing symlinks inside the victim
    _attempt(R, "existing symlink: write via victim/slink -> victim/existing.txt", wr(V("slink")))
    _attempt(R, "existing symlink: replace victim/slink itself", lambda: (os.unlink(V("slink")), os.symlink("x", V("slink"))))
    _attempt(R, "existing symlink: write via victim/abslink (absolute) ", wr(V("abslink")))
    # victim/to_allowed -> allowed/mine.txt: the write lands INSIDE the allowed dir
    _attempt(R, "existing symlink: write via victim/to_allowed -> allowed file (lands in allowed dir)",
             wr(V("to_allowed"), "a"), expect_blocked=False)
    # symlink swap: create dir under allowed, then swap it for a link to victim
    def swap():
        if not os.path.lexists(A("swap")):
            os.mkdir(A("swap"))
            os.rmdir(A("swap"))
            os.symlink(victim, A("swap"))
        open(A("swap", "swapped.txt"), "w").close()
    _attempt(R, "symlink swap: dir replaced by link to victim, then write", swap)

    # ---- .. traversal and absolute paths
    _attempt(R, "traversal: allowed/../victim/existing.txt", wr(os.path.join(allowed, "..", os.path.basename(victim), "existing.txt")))
    _attempt(R, "traversal: allowed/../victim/new.txt create",
             wr(os.path.join(allowed, "..", os.path.basename(victim), "new5.txt"), "x"))
    _attempt(R, "traversal: openat(dirfd=allowed, '../victim/existing.txt')",
             lambda: os.close(os.open(os.path.join("..", os.path.basename(victim), "existing.txt"), os.O_WRONLY,
                                      dir_fd=os.open(allowed, os.O_RDONLY))))
    _attempt(R, "traversal: os.chdir(victim) then relative write", lambda: (os.chdir(victim), open("rel.txt", "w").close()))
    _attempt(R, "absolute: create file in $HOME", wr(os.path.join(home, ".organizer-sandbox-probe"), "x"))
    _attempt(R, "absolute: create dir in $HOME", lambda: os.mkdir(os.path.join(home, ".organizer-sandbox-probe-dir")))
    _attempt(R, "absolute: append to ~/.bashrc", wr(os.path.join(home, ".bashrc"), "a"))
    _attempt(R, "absolute: create in ~/.ssh (if exists)", wr(os.path.join(home, ".ssh", "organizer-probe"), "x")) \
        if os.path.isdir(os.path.join(home, ".ssh")) else None
    _attempt(R, "absolute: /etc/organizer-probe", wr("/etc/organizer-probe", "x"))
    _attempt(R, "absolute: /var/tmp/organizer-probe", wr("/var/tmp/organizer-probe", "x"))
    uniq = "organizer-probe-%d-%d" % (os.getpid(), threading.get_ident())
    _attempt(R, "absolute: /dev/shm create", _touch_rm(os.path.join("/dev/shm", uniq)))
    _attempt(R, "absolute: /dev/null open for write", wr("/dev/null", "w"))
    _attempt(R, "absolute: /dev/null O_WRONLY (subprocess DEVNULL pattern)", creat("/dev/null", os.O_WRONLY))
    _attempt(R, "absolute: /dev/tty open for write", wr("/dev/tty", "w")) if os.path.exists("/dev/tty") else None
    _attempt(R, "absolute: /tmp create", _touch_rm(os.path.join("/tmp", uniq)))
    _attempt(R, "absolute: /tmp mkdir", lambda: os.mkdir(os.path.join("/tmp", uniq + "-d")))
    _attempt(R, "absolute: /tmp/organizer-<uid>.sock legacy socket path create",
             _touch_rm("/tmp/organizer-%d.sock" % os.getuid()))
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt):
        _attempt(R, "runtime dir: create file in $XDG_RUNTIME_DIR root", _touch_rm(os.path.join(rt, uniq)))
        _attempt(R, "runtime dir: mkdir in $XDG_RUNTIME_DIR root", lambda: os.mkdir(os.path.join(rt, uniq + "-d")))
        legacy = os.path.join(rt, "organizer.sock")        # may exist if an older daemon is running
        _attempt(R, "runtime dir: legacy $XDG_RUNTIME_DIR/organizer.sock write/create",
                 wr(legacy, "a") if os.path.exists(legacy) else _touch_rm(legacy))
        _attempt(R, "runtime dir: unlink $XDG_RUNTIME_DIR/bus (dbus socket)", lambda: os.unlink(os.path.join(rt, "bus"))) \
            if os.path.exists(os.path.join(rt, "bus")) else None
        for sub in ("doc", "gvfs", "keyring", "gnupg", "pulse", "systemd"):
            d = os.path.join(rt, sub)
            if os.path.isdir(d):
                _attempt(R, "runtime dir: create under $XDG_RUNTIME_DIR/%s" % sub, _touch_rm(os.path.join(d, uniq)))

    # ---- temporary files
    _attempt(R, "tempfile: NamedTemporaryFile(dir=victim)", lambda: tempfile.NamedTemporaryFile(dir=victim).close())
    _attempt(R, "tempfile: mkstemp(dir=victim)", lambda: os.close(tempfile.mkstemp(dir=victim)[0]))
    _attempt(R, "tempfile: mkdtemp(dir=victim)", lambda: tempfile.mkdtemp(dir=victim))
    _attempt(R, "tempfile: O_TMPFILE in victim",
             creat(victim, getattr(os, "O_TMPFILE", 0o20000000) | os.O_WRONLY))
    _attempt(R, "tempfile: engine-style victim/x.tmp + os.replace",
             lambda: (open(V("x.tmp"), "w").close(), os.replace(V("x.tmp"), V("x.json"))))
    def no_tempdir(fn):
        # tempfile probes /tmp, /var/tmp, /usr/tmp and cwd; when Landlock denies all of
        # them it raises FileNotFoundError("No usable temporary directory found").
        def f():
            tempfile.tempdir = None
            try:
                fn()
            except FileNotFoundError as e:
                if "No usable temporary directory" in str(e):
                    raise OSError(errno.EACCES, str(e))
                raise
        return f
    _attempt(R, "tempfile: default tempdir (/tmp)", no_tempdir(lambda: tempfile.NamedTemporaryFile().close()))
    _attempt(R, "tempfile: mkdtemp() default", no_tempdir(lambda: tempfile.mkdtemp()))

    # ---- the scanned directory itself: organizer's own write patterns aimed at it
    _attempt(R, "scanned dir: write report into it", wr(V("latest.json")))
    _attempt(R, "scanned dir: create proposed folder Documents/Finance", lambda: os.makedirs(V("Documents", "Finance")))
    _attempt(R, "scanned dir: move file into proposed folder", lambda: os.rename(V("existing.txt"), V("dir", "existing.txt")))
    _attempt(R, "scanned dir: delete 'duplicate'", lambda: os.remove(V("dup (1).txt")))
    if tmp_victim:
        _attempt(R, "scanned dir under /tmp: write existing", wr(os.path.join(tmp_victim, "existing.txt")))
        _attempt(R, "scanned dir under /tmp: create", wr(os.path.join(tmp_victim, "new.txt"), "x"))
        _attempt(R, "scanned dir under /tmp: delete", lambda: os.remove(os.path.join(tmp_victim, "existing.txt")))

    # ---- metadata (outside Landlock's filesystem access scope; documented gap)
    _attempt(R, "metadata: chmod victim file (Landlock does not cover; documented)",
             lambda: os.chmod(V("existing.txt"), os.lstat(V("existing.txt")).st_mode & 0o7777), expect_blocked=False)
    _attempt(R, "metadata: utime victim file (Landlock does not cover; documented)",
             lambda: os.utime(V("existing.txt"), ns=(os.lstat(V("existing.txt")).st_atime_ns,
                                                     os.lstat(V("existing.txt")).st_mtime_ns)), expect_blocked=False)

    # ---- processes spawned from the restricted thread inherit the domain
    def sh(cmd):
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError(errno.EACCES, (r.stderr or "").strip()[:120])
    _attempt(R, "subprocess: sh touch victim/new", lambda: sh("touch '%s'" % V("sub.txt")))
    _attempt(R, "subprocess: sh rm victim file", lambda: sh("rm '%s'" % V("existing.txt")))
    _attempt(R, "subprocess: sh mv victim file", lambda: sh("mv '%s' '%s'" % (V("existing.txt"), V("mv.txt"))))
    _attempt(R, "subprocess: sh mkdir -p victim/a/b", lambda: sh("mkdir -p '%s'" % V("a", "b")))
    _attempt(R, "subprocess: `file --mime-type` (read) still works",
             lambda: sh("command -v file >/dev/null && file -b --mime-type '%s' >/dev/null || true" % V("existing.txt")),
             expect_blocked=False)
    return R


def legitimate_writes(config, data, sock_dir):
    """Everything organizer must still be able to do after restrict(). All expected to succeed."""
    import socket
    R = []
    C = lambda *p: os.path.join(config, *p)
    D = lambda *p: os.path.join(data, *p)

    def save_pattern(path):
        def f():
            with open(path + ".tmp", "w") as fh:
                fh.write("{}")
            os.replace(path + ".tmp", path)
        return f

    def bak_pattern():
        shutil.copy2(C("memory.json"), C("memory.json.bak"))
    _attempt(R, "legit: memory.json tmp+replace (config)", save_pattern(C("memory.json")), expect_blocked=False)
    _attempt(R, "legit: memory.json.bak copy2 (config)", bak_pattern, expect_blocked=False)
    _attempt(R, "legit: state.json tmp+replace (data)", save_pattern(D("state.json")), expect_blocked=False)
    _attempt(R, "legit: reports/<slug>/ mkdir + report + latest.json (data)",
             lambda: (os.makedirs(D("reports", "slug"), exist_ok=True), save_pattern(D("reports", "slug", "latest.json"))(),
                      open(D("reports", "slug", "20260101-000000.json"), "w").close()), expect_blocked=False)
    _attempt(R, "legit: prune old report (data)", lambda: os.remove(D("reports", "slug", "20260101-000000.json")),
             expect_blocked=False)

    def sock_cycle():
        p = os.path.join(sock_dir, "probe.sock")
        if os.path.exists(p):
            os.remove(p)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(p); os.chmod(p, 0o600); s.close(); os.remove(p)
    _attempt(R, "legit: bind + chmod + unlink Unix socket (socket dir)", sock_cycle, expect_blocked=False)
    return R


def from_thread(fn):
    """Run fn in a thread created now (after restrict) and return its result."""
    box = {}
    t = threading.Thread(target=lambda: box.update(r=fn()))
    t.start(); t.join()
    return box["r"]


def no_new_privs():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("NoNewPrivs:"):
                return line.split()[1] == "1"
    return None


def prepare_victim(victim, allowed):
    os.makedirs(os.path.join(victim, "dir"), exist_ok=True)
    os.makedirs(os.path.join(victim, "emptydir"), exist_ok=True)
    for n in ("existing.txt", "dup (1).txt", os.path.join("dir", "inner.txt")):
        with open(os.path.join(victim, n), "w") as f:
            f.write("victim " + n)
    os.symlink("existing.txt", os.path.join(victim, "slink"))
    os.symlink(os.path.join(victim, "existing.txt"), os.path.join(victim, "abslink"))
    with open(os.path.join(allowed, "mine.txt"), "w") as f:
        f.write("mine")
    os.symlink(os.path.join(allowed, "mine.txt"), os.path.join(victim, "to_allowed"))


def _inprocess(victim, allowed, tmp_victim, break_landlock):
    from organizer import brain, cli, sandbox
    if break_landlock:
        def boom():
            raise OSError(errno.ENOSYS, "simulated: landlock unavailable")
        sandbox.abi_version = boom
    # control: before restrict, this user can write next to the victim (so later
    # denials come from Landlock, not from DAC). Uses a sibling dir so the victim
    # tree stays byte-identical for the test's snapshot comparison.
    escape = os.path.join(os.path.dirname(victim), "escape")
    p = os.path.join(escape, "canary")
    open(p, "w").close(); os.remove(p)
    err = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        cli._fallback_context()          # the real --no-daemon startup: start_runner() + restrict()
    finally:
        sys.stderr.close(); sys.stderr = err
    out = {"mode": "nolandlock" if break_landlock else "inprocess", "status": sandbox.status(),
           "no_new_privs": no_new_privs()}
    if not sandbox.status()["applied"]:
        # never aim at $HOME without the kernel guard: victim-only battery, then stop
        out["same_thread"] = victim_only_attacks(victim)
        json.dump(out, sys.stdout)
        return
    from organizer import paths
    out["legit"] = legitimate_writes(paths.CONFIG_DIR, paths.DATA_DIR, os.path.dirname(paths.socket_path()))
    out["same_thread"] = attacks(victim, allowed, tmp_victim)
    out["new_thread"] = from_thread(lambda: attacks(victim, allowed))
    # documented escape: the helper thread created before restrict()
    runner_probe = os.path.join(escape, "runner-wrote-this")
    try:
        brain._runner.call(lambda: open(runner_probe, "w").close())
        out["runner_thread_can_write"] = True
        os.path.exists(runner_probe) and brain._runner.call(lambda: os.remove(runner_probe))
    except OSError as e:
        out["runner_thread_can_write"] = False
        out["runner_errno"] = errno.errorcode.get(e.errno)
    out["runner_subprocess_can_write"] = brain._runner.call(
        lambda: subprocess.run(["sh", "-c", "touch '%s' && rm '%s'" % (runner_probe, runner_probe)],
                               capture_output=True).returncode == 0)
    # the real in-process pipeline against the victim (no AI, no network)
    from organizer import engine, memory as memmod
    store = memmod.MemoryStore(); state = engine.load_state()
    rep = engine.run_scan(victim, {"no_ai": True}, store, state, log=lambda m: None)
    out["scan_ok"] = rep.get("report_only") is True and len(rep["proposals"]) > 0
    json.dump(out, sys.stdout)


def _daemon(victim, allowed, tmp_victim):
    from organizer import brain, daemon
    orig = daemon.handle

    def handle(req):
        if req.get("cmd") != "_probe":
            return orig(req)
        via = req.get("via", "handler")
        if not daemon.sandbox.status()["applied"]:
            return {"ok": True, "result": victim_only_attacks(victim), "no_new_privs": no_new_privs(),
                    "sandbox": daemon.sandbox.status()}
        if via == "handler":
            res = attacks(victim, allowed, tmp_victim)
        elif via == "legit":
            from organizer import paths
            res = legitimate_writes(paths.CONFIG_DIR, paths.DATA_DIR, os.path.dirname(paths.socket_path()))
        elif via == "thread":
            res = from_thread(lambda: attacks(victim, allowed))
        elif via == "runner":
            p = os.path.join(os.path.dirname(victim), "escape", "runner-wrote-this")
            try:
                brain._runner.call(lambda: open(p, "w").close())
                brain._runner.call(lambda: os.remove(p))
                res = True
            except OSError:
                res = False
        else:
            return {"ok": False, "error": "bad via"}
        return {"ok": True, "result": res, "no_new_privs": no_new_privs(), "sandbox": daemon.sandbox.status()}
    daemon.handle = handle
    daemon.main()


if __name__ == "__main__":
    mode, victim, allowed = sys.argv[1], sys.argv[2], sys.argv[3]
    tmp_victim = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    if mode == "daemon":
        _daemon(victim, allowed, tmp_victim)
    else:
        _inprocess(victim, allowed, tmp_victim, break_landlock=(mode == "nolandlock"))
