"""Installer, uninstaller, daemon and CLI lifecycle tests.

Everything runs in a throw-away layout under ~/.cache/organizer-tests with
its own $HOME, $XDG_RUNTIME_DIR and a shim directory first on $PATH:
`systemctl` / `loginctl` record their arguments instead of touching the real
user manager, `rm` (uninstall tests only) refuses to delete anything outside
the layout, and `claude` is whatever the test needs (absent, failing, hanging).
The real installed service, ~/.config/organizer and ~/.local are never touched.

Run: PYTHONPATH=. python3 -m unittest tests.test_lifecycle -v
"""
import hashlib
import json
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "organizer-tests")
UID = os.getuid()
USERNAME = pwd.getpwuid(UID).pw_name

SYSTEMCTL_SHIM = """#!/bin/sh
echo "systemctl $*" >> "$SHIM_LOG"
[ -n "${SHIM_NO_SYSTEMD:-}" ] && exit 1
[ -n "${SHIM_ENABLE_FAILS:-}" ] && [ "$2" = "enable" ] && { echo "Failed to start organizer.service" >&2; exit 1; }
exit 0
"""
LOGINCTL_SHIM = """#!/bin/sh
echo "loginctl $*" >> "$SHIM_LOG"
case "$1" in show-user) echo "${SHIM_LINGER:-no}";; esac
exit 0
"""
# Forwards to /bin/rm only the operands that lie inside the test layout; everything
# else is logged and skipped, so a bug can be observed without doing damage.
RM_SHIM = """#!/bin/sh
echo "rm $*" >> "$SHIM_LOG"
n=$#; i=0; have=0
while [ $i -lt $n ]; do
  a=$1; shift; i=$((i+1))
  case "$a" in
    -*) set -- "$@" "$a";;
    "$SHIM_ROOT"/*) set -- "$@" "$a"; have=1;;
  esac
done
[ $have = 1 ] && exec /bin/rm "$@"
exit 0
"""


def _write_exe(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def tree_digest(root):
    """{relative path: (mode, sha256)} for every file under root."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in fns:
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            h = hashlib.sha256()
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    h.update(f.read())
            out[rel] = (stat.S_IMODE(st.st_mode), h.hexdigest())
    return out


def rpc(sock_path, req, timeout=30):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode())


class Layout:
    """Fake $HOME + runtime dir + shim PATH. `env` is minimal on purpose (no inherited
    ORGANIZER_*, PYTHONPATH, CLAUDE*), so each test states what it relies on."""

    def __init__(self, rm_shim=False):
        os.makedirs(CACHE, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="lc-", dir=CACHE)
        self.home = os.path.join(self.root, "home")
        self.rt = os.path.join(self.root, "rt")
        self.shims = os.path.join(self.root, "shims")
        self.cwd = os.path.join(self.root, "cwd")
        self.log = os.path.join(self.root, "shim.log")
        for d in (self.home, self.shims, self.cwd):
            os.makedirs(d)
        os.makedirs(self.rt, mode=0o700)
        _write_exe(os.path.join(self.shims, "systemctl"), SYSTEMCTL_SHIM)
        _write_exe(os.path.join(self.shims, "loginctl"), LOGINCTL_SHIM)
        if rm_shim:
            _write_exe(os.path.join(self.shims, "rm"), RM_SHIM)
        self.env = {"HOME": self.home, "PATH": self.shims + ":/usr/bin:/bin", "XDG_RUNTIME_DIR": self.rt,
                    "USER": USERNAME, "SHIM_LOG": self.log, "SHIM_ROOT": self.root, "LANG": "C.UTF-8"}
        # installed layout
        self.lib = os.path.join(self.home, ".local", "lib", "organizer")
        self.launcher = os.path.join(self.home, ".local", "bin", "organizer")
        self.unit = os.path.join(self.home, ".config", "systemd", "user", "organizer.service")
        self.cfg = os.path.join(self.home, ".config", "organizer")
        self.data = os.path.join(self.home, ".local", "share", "organizer")
        self.sock = os.path.join(self.rt, "organizer", "organizer.sock")

    def run(self, argv, extra=None, cwd=None, timeout=180, env=None, **kw):
        e = dict(env if env is not None else self.env)
        e.update(extra or {})
        return subprocess.run(argv, env=e, cwd=cwd or self.cwd, capture_output=True, text=True, timeout=timeout, **kw)

    def install(self, extra=None):
        return self.run([os.path.join(REPO, "install.sh")], extra)

    def uninstall(self, *args, extra=None):
        return self.run([os.path.join(REPO, "uninstall.sh")] + list(args), extra)

    def shim_log(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [l.rstrip("\n") for l in f]

    def checkout_env(self, claude=None):
        """Env to run the *checkout* (not the installed tree) against this layout."""
        e = dict(self.env, PYTHONPATH=REPO, ORGANIZER_CONFIG_DIR=os.path.join(self.root, "cfg"),
                 ORGANIZER_DATA_DIR=os.path.join(self.root, "data"))
        if claude is not None:
            cdir = os.path.join(self.root, "claude-bin")
            os.makedirs(cdir, exist_ok=True)
            _write_exe(os.path.join(cdir, "claude"), claude)
            e["PATH"] = cdir + ":" + e["PATH"]
        return e

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def scan_dir(root):
    d = os.path.join(root, "scanme")
    os.makedirs(d, exist_ok=True)
    for name, body in (("mystery_thing", "zz\n"), ("report-final-v2.xyz", "x"), ("photo.jpg", "j"), ("notes.txt", "n")):
        with open(os.path.join(d, name), "w") as f:
            f.write(body)
    return d


def report_of(stdout):
    return json.loads(stdout[stdout.index("{"):])


class Daemon:
    """A checkout daemon started with `env`; waits for its socket or its exit."""

    def __init__(self, env, sock, cwd=None):
        self.sock = sock
        self.err = None
        self.proc = subprocess.Popen([sys.executable, "-m", "organizer.daemon"], env=env, cwd=cwd or REPO,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            if self.proc.poll() is not None:
                break
            if os.path.exists(sock):
                try:
                    rpc(sock, {"cmd": "status"}, timeout=5)
                    break
                except OSError:
                    pass
            time.sleep(0.05)

    def alive(self):
        return self.proc.poll() is None

    def stop(self, timeout=10):
        if self.err is not None:
            return self.err
        if self.alive():
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.err = self.proc.stderr.read()
        self.proc.stderr.close()
        return self.err


# ------------------------------------------------------------------ install.sh

class Install(unittest.TestCase):

    def setUp(self):
        self.L = Layout()
        self.addCleanup(self.L.cleanup)

    def assertInstalled(self, L):
        for p in (os.path.join(L.lib, "organizer", "cli.py"), os.path.join(L.lib, "organizer", "prompts"),
                  os.path.join(L.lib, "seed", "memory.json"), os.path.join(L.lib, "README.md"),
                  os.path.join(L.lib, "MEMORY-GUIDE.md"), L.launcher, L.unit,
                  os.path.join(L.cfg, "memory.json"), os.path.join(L.cfg, "MEMORY-GUIDE.md"), L.data):
            self.assertTrue(os.path.exists(p), p)
        self.assertTrue(os.access(L.launcher, os.X_OK))
        with open(L.unit) as a, open(os.path.join(REPO, "systemd", "organizer.service")) as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual([n for _, dns, _ in os.walk(L.lib) for n in dns if n == "__pycache__"], [])

    def test_fresh_install_layout_and_systemd_calls(self):
        r = self.L.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInstalled(self.L)
        with open(os.path.join(self.L.cfg, "memory.json")) as a, open(os.path.join(REPO, "seed", "memory.json")) as b:
            self.assertEqual(a.read(), b.read())
        self.assertIn("seeded", r.stdout)
        log = self.L.shim_log()
        self.assertIn("systemctl --user daemon-reload", log)
        self.assertIn("systemctl --user enable organizer.service", log)
        self.assertIn("systemctl --user restart organizer.service", log)
        self.assertEqual(len([l for l in log if "restart" in l or "start " in l]), 1, "the daemon is started once")
        self.assertNotIn("Traceback", r.stderr)

    def test_install_twice_is_idempotent(self):
        self.assertEqual(self.L.install().returncode, 0)
        first = tree_digest(self.L.home)
        r = self.L.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kept existing", r.stdout)
        self.assertEqual(tree_digest(self.L.home), first)

    def test_reinstall_keeps_config_and_data_but_refreshes_code(self):
        self.assertEqual(self.L.install().returncode, 0)
        mem_path = os.path.join(self.L.cfg, "memory.json")
        with open(mem_path) as f:
            mem = json.load(f)
        mem["claude_notes"] = "user edited this"
        with open(mem_path, "w") as f:
            json.dump(mem, f)
        os.makedirs(os.path.join(self.L.data, "reports", "x"))
        with open(os.path.join(self.L.data, "reports", "x", "latest.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.L.data, "state.json"), "w") as f:
            f.write('{"dirs": {}}')
        with open(os.path.join(self.L.lib, "organizer", "stray.py"), "w") as f:      # from an older version
            f.write("x")
        before_user = {k: v for k, v in tree_digest(self.L.home).items()
                       if k.startswith(".config/organizer/memory") or k.startswith(".local/share/organizer")}
        r = self.L.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kept existing", r.stdout)
        after = tree_digest(self.L.home)
        self.assertEqual({k: v for k, v in after.items() if k in before_user}, before_user)
        self.assertFalse(os.path.exists(os.path.join(self.L.lib, "organizer", "stray.py")))
        self.assertInstalled(self.L)

    def test_install_works_when_USER_is_unset(self):
        env = dict(self.L.env)
        del env["USER"]
        r = self.L.run([os.path.join(REPO, "install.sh")], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInstalled(self.L)

    def test_linger_is_opt_in(self):
        r = self.L.install()
        self.assertEqual(r.returncode, 0)
        self.assertFalse([l for l in self.L.shim_log() if "enable-linger" in l], self.L.shim_log())
        self.assertIn("linger", r.stdout.lower())          # tells the user how to opt in
        r = self.L.install({"ORGANIZER_LINGER": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("loginctl enable-linger %s" % USERNAME, self.L.shim_log())
        os.remove(self.L.log)
        r = self.L.install({"ORGANIZER_LINGER": "1", "SHIM_LINGER": "yes"})
        self.assertEqual(r.returncode, 0)
        self.assertFalse([l for l in self.L.shim_log() if "enable-linger" in l])

    def test_install_without_usable_systemd(self):
        r = self.L.install({"SHIM_NO_SYSTEMD": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInstalled(self.L)
        self.assertIn("systemd --user not available", r.stdout)
        self.assertIn("organizer.daemon", r.stdout)
        self.assertFalse([l for l in self.L.shim_log() if "enable" in l])

    def test_failed_install_reports_and_rerun_recovers(self):
        r = self.L.install({"SHIM_ENABLE_FAILS": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("re-run", (r.stdout + r.stderr).lower())
        # simulate an interruption that happened earlier: half-copied package, no launcher
        shutil.rmtree(os.path.join(self.L.lib, "organizer", "prompts"))
        os.remove(self.L.launcher)
        r = self.L.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInstalled(self.L)

    def test_requires_the_interpreter_the_launcher_uses(self):
        with open(os.path.join(REPO, "install.sh")) as f:
            script = f.read()
        with open(os.path.join(REPO, "systemd", "organizer.service")) as f:
            unit = f.read()
        self.assertIn("ExecStart=/usr/bin/python3", unit)
        self.assertIn("exec /usr/bin/python3", script)
        self.assertIn("/usr/bin/python3", script.split("mkdir -p")[0], "the check must name the interpreter it uses")

    def test_installed_launcher_runs_installed_version_even_inside_a_checkout(self):
        self.assertEqual(self.L.install().returncode, 0)
        from organizer import __version__
        r = self.L.run([self.L.launcher, "--version"])
        self.assertEqual(r.stdout.strip(), "organizer " + __version__)
        # a different `organizer` package in the cwd must not shadow the installed one
        fake = os.path.join(self.L.cwd, "organizer")
        os.makedirs(fake)
        with open(os.path.join(fake, "__init__.py"), "w") as f:
            f.write('__version__ = "9.9.9-shadow"\n')
        with open(os.path.join(fake, "cli.py"), "w") as f:
            f.write('print("SHADOWED")\n')
        r = self.L.run([self.L.launcher, "--version"], cwd=self.L.cwd)
        if sys.version_info >= (3, 11):
            self.assertEqual(r.stdout.strip(), "organizer " + __version__, r.stdout + r.stderr)
        else:  # PYTHONSAFEPATH needs 3.11; older interpreters keep python -m semantics
            self.skipTest("PYTHONSAFEPATH needs Python >= 3.11")

    def test_installed_cli_end_to_end_without_daemon(self):
        self.assertEqual(self.L.install().returncode, 0)
        d = scan_dir(self.L.root)
        r = self.L.run([self.L.launcher, "--no-daemon", d, "--no-ai", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = report_of(r.stdout)
        self.assertEqual(rep["summary"]["entries"], 4)
        self.assertTrue(os.path.exists(os.path.join(self.L.data, "state.json")))
        self.assertTrue(os.path.isdir(os.path.join(self.L.data, "reports")))
        self.assertEqual(sorted(os.listdir(d)), ["mystery_thing", "notes.txt", "photo.jpg", "report-final-v2.xyz"])
        # daemon not running: automatic fallback, same result
        r = self.L.run([self.L.launcher, d, "--no-ai", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("daemon not reachable", r.stderr)
        self.assertEqual(report_of(r.stdout)["summary"]["entries"], 4)
        r = self.L.run([self.L.launcher, "status"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running", r.stdout)


# ---------------------------------------------------------------- uninstall.sh

class Uninstall(unittest.TestCase):

    def setUp(self):
        self.L = Layout(rm_shim=True)
        self.addCleanup(self.L.cleanup)

    def rm_operands(self):
        ops = []
        for l in self.L.shim_log():
            if l.startswith("rm "):
                ops += [a for a in l.split()[1:] if not a.startswith("-")]
        return ops

    def test_uninstall_after_install_keeps_user_data(self):
        self.assertEqual(self.L.install().returncode, 0)
        os.makedirs(os.path.join(self.L.rt, "organizer"), mode=0o700)
        r = self.L.uninstall()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for gone in (self.L.lib, self.L.launcher, self.L.unit, os.path.join(self.L.rt, "organizer")):
            self.assertFalse(os.path.exists(gone), gone)
        for kept in (os.path.join(self.L.cfg, "memory.json"), self.L.data):
            self.assertTrue(os.path.exists(kept), kept)
        self.assertIn("memory kept", r.stdout)
        log = self.L.shim_log()
        self.assertIn("systemctl --user disable --now organizer.service", log)
        self.assertIn("systemctl --user daemon-reload", log)

    def test_uninstall_purge_removes_config_and_data(self):
        self.assertEqual(self.L.install().returncode, 0)
        r = self.L.uninstall("--purge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for gone in (self.L.lib, self.L.launcher, self.L.unit, self.L.cfg, self.L.data):
            self.assertFalse(os.path.exists(gone), gone)

    def test_uninstall_partial_install(self):
        os.makedirs(os.path.join(self.L.lib, "organizer"))
        r = self.L.uninstall()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.L.lib))
        r = self.L.uninstall()                                   # nothing left: still clean
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_uninstall_works_when_USER_is_unset(self):
        self.assertEqual(self.L.install().returncode, 0)
        env = dict(self.L.env)
        del env["USER"]
        r = self.L.run([os.path.join(REPO, "uninstall.sh"), "--purge"], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.L.cfg))
        self.assertNotIn("unbound", r.stderr)

    def test_uninstall_never_touches_tmp_organizer(self):
        """Without XDG_RUNTIME_DIR the socket fallback is /tmp/organizer-<uid>; a plain
        /tmp/organizer (e.g. a checkout of this repo) is not ours and must never be removed."""
        self.assertEqual(self.L.install().returncode, 0)
        env = dict(self.L.env)
        del env["XDG_RUNTIME_DIR"]
        r = self.L.run([os.path.join(REPO, "uninstall.sh")], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ops = self.rm_operands()
        self.assertNotIn("/tmp/organizer", ops, ops)
        self.assertNotIn("/organizer", ops, ops)
        self.assertIn("/tmp/organizer-%d" % UID, ops, ops)
        for op in ops:
            self.assertTrue(op.startswith(self.L.root) or op == "/tmp/organizer-%d" % UID, op)


# ------------------------------------------------------------ paths / runtime dir

class RuntimeDir(unittest.TestCase):

    def setUp(self):
        self.L = Layout()
        self.addCleanup(self.L.cleanup)

    def socket_dir(self, **env):
        e = self.L.checkout_env()
        e.pop("XDG_RUNTIME_DIR", None)
        e.update(env)
        r = subprocess.run([sys.executable, "-c", "from organizer import paths; print(paths.socket_dir())"],
                           env=e, capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_runtime_dir_normal(self):
        self.assertEqual(self.socket_dir(XDG_RUNTIME_DIR=self.L.rt), os.path.join(self.L.rt, "organizer"))

    def test_runtime_dir_unset_or_missing_falls_back_to_tmp(self):
        self.assertEqual(self.socket_dir(), "/tmp/organizer-%d" % UID)
        self.assertEqual(self.socket_dir(XDG_RUNTIME_DIR=os.path.join(self.L.root, "nope")), "/tmp/organizer-%d" % UID)
        self.assertEqual(self.socket_dir(XDG_RUNTIME_DIR=""), "/tmp/organizer-%d" % UID)

    @unittest.skipIf(UID == 0, "root owns everything")
    def test_runtime_dir_owned_by_someone_else_falls_back_to_tmp(self):
        """`sudo -u`/`su` keep the caller's XDG_RUNTIME_DIR; a dir we cannot own must not
        crash startup with EACCES — it is simply not our runtime dir."""
        self.assertEqual(self.socket_dir(XDG_RUNTIME_DIR="/"), "/tmp/organizer-%d" % UID)

    def test_squatted_socket_dir_is_a_clean_error_not_a_traceback(self):
        os.symlink(self.L.root, os.path.join(self.L.rt, "organizer"))
        env = self.L.checkout_env()
        r = subprocess.run([sys.executable, "-m", "organizer.daemon"], env=env, capture_output=True, text=True,
                           cwd=REPO, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing socket dir", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        d = scan_dir(self.L.root)
        r = subprocess.run([sys.executable, "-m", "organizer.cli", "--no-daemon", d, "--no-ai"], env=env,
                           capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing socket dir", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


# ------------------------------------------------------------------- daemon

class DaemonSocket(unittest.TestCase):

    def setUp(self):
        self.L = Layout()
        self.addCleanup(self.L.cleanup)
        self.env = self.L.checkout_env()
        self.daemons = []

    def tearDown(self):
        for d in self.daemons:
            d.stop()

    def start(self, env=None):
        d = Daemon(env or self.env, self.L.sock)
        self.daemons.append(d)
        return d

    def test_socket_and_dir_permissions(self):
        d = self.start()
        self.assertTrue(d.alive(), d.stop() if not d.alive() else "")
        st = os.stat(self.L.sock)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(st.st_uid, UID)
        sd = os.lstat(os.path.dirname(self.L.sock))
        self.assertTrue(stat.S_ISDIR(sd.st_mode))
        self.assertEqual(stat.S_IMODE(sd.st_mode), 0o700)
        self.assertEqual(sd.st_uid, UID)
        resp = rpc(self.L.sock, {"cmd": "status"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["socket"], self.L.sock)
        self.assertEqual(resp["pid"], d.proc.pid)

    def test_socket_is_0600_under_permissive_umask(self):
        d = Daemon(self.env, self.L.sock, cwd=REPO)
        self.daemons.append(d)
        # start a second one with umask 000 to be sure the mode does not depend on the caller
        d.stop()
        self.assertFalse(os.path.exists(self.L.sock))
        proc = subprocess.Popen([sys.executable, "-m", "organizer.daemon"], env=self.env, cwd=REPO,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: os.umask(0))
        try:
            for _ in range(100):
                if os.path.exists(self.L.sock):
                    break
                time.sleep(0.05)
            self.assertEqual(stat.S_IMODE(os.stat(self.L.sock).st_mode), 0o600)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            proc.stderr.close()

    def test_stale_socket_file_is_replaced(self):
        os.makedirs(os.path.dirname(self.L.sock), mode=0o700)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.L.sock)
        s.close()                                           # dead listener leaves the file behind
        self.assertTrue(os.path.exists(self.L.sock))
        d = self.start()
        self.assertTrue(d.alive())
        self.assertTrue(rpc(self.L.sock, {"cmd": "status"})["ok"])

    def test_second_daemon_refuses_and_first_keeps_serving(self):
        a = self.start()
        self.assertTrue(a.alive())
        b = Daemon(self.env, self.L.sock)
        self.daemons.append(b)
        for _ in range(100):                                # b must exit by itself
            if not b.alive():
                break
            time.sleep(0.05)
        err = b.stop()
        self.assertNotEqual(b.proc.returncode, 0, "second daemon must not start on a live socket")
        self.assertIn("already", err.lower())
        self.assertNotIn("Traceback", err)
        self.assertTrue(a.alive())
        self.assertEqual(rpc(self.L.sock, {"cmd": "status"})["pid"], a.proc.pid)
        a.stop()
        self.assertFalse(os.path.exists(self.L.sock), "daemon must remove its socket on shutdown")

    def test_shutdown_leaves_socket_dir_but_not_socket(self):
        d = self.start()
        d.stop()
        self.assertFalse(os.path.exists(self.L.sock))
        self.assertTrue(os.path.isdir(os.path.dirname(self.L.sock)))
        d2 = self.start()                                   # restart on the same path
        self.assertTrue(rpc(self.L.sock, {"cmd": "status"})["ok"])
        d2.stop()

    def test_landlock_status_is_reported(self):
        d = self.start()
        sb = rpc(self.L.sock, {"cmd": "status"})["sandbox"]
        try:
            from organizer import sandbox
            sandbox.abi_version()
            self.assertTrue(sb["applied"], sb)
            self.assertIn(os.path.dirname(self.L.sock), sb["allowed"])
        except OSError:
            self.assertFalse(sb["applied"])
            self.assertIn("landlock", (sb["error"] or "").lower())
        err = d.stop()
        self.assertTrue("landlock ABI" in err or "no kernel sandbox" in err, err)


# ---------------------------------------------------------------- CLI paths

class CliFallback(unittest.TestCase):

    def setUp(self):
        self.L = Layout()
        self.addCleanup(self.L.cleanup)
        self.env = self.L.checkout_env()
        self.d = scan_dir(self.L.root)

    def cli(self, *argv, env=None, timeout=120):
        return subprocess.run([sys.executable, "-m", "organizer.cli"] + list(argv), env=env or self.env,
                              capture_output=True, text=True, cwd=REPO, timeout=timeout)

    def test_daemon_down_scan_falls_back_in_process(self):
        r = self.cli(self.d, "--no-ai", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("daemon not reachable", r.stderr)
        self.assertIn("running in-process", r.stderr)
        self.assertEqual(report_of(r.stdout)["summary"]["entries"], 4)
        r = self.cli("explain", os.path.join(self.d, "photo.jpg"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("history", self.d)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_daemon_down_daemon_only_commands_fail_clearly(self):
        r = self.cli("reload")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("daemon not running", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = self.cli("status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running", r.stdout)

    def test_no_daemon_never_touches_the_socket(self):
        d = Daemon(self.env, self.L.sock)
        try:
            self.assertTrue(d.alive())
            r = self.cli("--no-daemon", self.d, "--no-ai", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("daemon", r.stderr)
            self.assertEqual(report_of(r.stdout)["summary"]["entries"], 4)
            self.assertEqual(rpc(self.L.sock, {"cmd": "status"})["dirs_tracked"], 0)   # daemon never saw it
        finally:
            d.stop()

    def test_global_flags_are_honoured_before_and_after_the_subcommand(self):
        """`organizer --no-daemon status` is the documented form; the flag must not be
        silently dropped when it precedes the subcommand."""
        from organizer import cli
        ap = cli.build_parser()
        for argv in (["--no-daemon", "status"], ["status", "--no-daemon"], ["--no-daemon", "scan", "x"],
                     ["scan", "x", "--no-daemon"], ["--no-daemon", "--json", "memory", "show"]):
            a = ap.parse_args(argv)
            self.assertTrue(getattr(a, "no_daemon", False), argv)
        self.assertTrue(ap.parse_args(["--json", "history"]).json)
        self.assertFalse(getattr(ap.parse_args(["history"]), "no_daemon", False))
        self.assertFalse(getattr(ap.parse_args(["history"]), "json", False))
        # and end to end: the daemon is running but must not see this scan
        d = Daemon(self.env, self.L.sock)
        try:
            r = self.cli("--no-daemon", "scan", self.d, "--no-ai", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(report_of(r.stdout)["summary"]["entries"], 4)
            self.assertEqual(rpc(self.L.sock, {"cmd": "status"})["dirs_tracked"], 0)
        finally:
            d.stop()

    def test_no_daemon_status_reports_local_state(self):
        r = self.cli("--no-daemon", "status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("memory:", r.stdout)
        self.assertIn("sandbox:", r.stdout)
        self.assertIn("claude", r.stdout)
        self.assertIn("daemon:", r.stdout)

    def test_scan_via_daemon_matches_in_process(self):
        d = Daemon(self.env, self.L.sock)
        try:
            r = self.cli(self.d, "--no-ai", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("daemon not reachable", r.stderr)
            self.assertEqual(rpc(self.L.sock, {"cmd": "status"})["dirs_tracked"], 1)
        finally:
            d.stop()


class ClaudeUnavailable(unittest.TestCase):

    def setUp(self):
        self.L = Layout()
        self.addCleanup(self.L.cleanup)
        self.d = scan_dir(self.L.root)

    def scan(self, env, timeout=120):
        r = subprocess.run([sys.executable, "-m", "organizer.cli", "--no-daemon", self.d, "--json"], env=env,
                           capture_output=True, text=True, cwd=REPO, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = report_of(r.stdout)
        self.assertEqual(rep["summary"]["entries"], 4)
        self.assertEqual(sorted(os.listdir(self.d)), ["mystery_thing", "notes.txt", "photo.jpg", "report-final-v2.xyz"])
        return rep, r.stderr

    def set_timeout(self, env, seconds):
        os.makedirs(env["ORGANIZER_CONFIG_DIR"], exist_ok=True)
        with open(os.path.join(REPO, "seed", "memory.json")) as f:
            mem = json.load(f)
        mem["settings"]["ai_timeout_s"] = seconds
        with open(os.path.join(env["ORGANIZER_CONFIG_DIR"], "memory.json"), "w") as f:
            json.dump(mem, f)

    def test_claude_not_installed(self):
        env = self.L.checkout_env()                          # PATH has no claude, fake HOME has no ~/.local/bin/claude
        rep, err = self.scan(env)
        self.assertTrue(rep["ai"]["enabled"])
        self.assertFalse(rep["ai"]["used"])
        self.assertIn("claude CLI not found", rep["ai"]["error"])
        self.assertGreater(rep["ai"]["undecided"], 0)
        self.assertTrue(any(p["action"] == "review" for p in rep["proposals"]))
        # status must say so too, in and out of process
        r = subprocess.run([sys.executable, "-m", "organizer.cli", "--no-daemon", "status"], env=env,
                           capture_output=True, text=True, cwd=REPO)
        self.assertIn("NOT FOUND", r.stdout)

    def test_claude_exits_nonzero(self):
        env = self.L.checkout_env(claude="#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
        rep, err = self.scan(env)
        self.assertIn("claude exited 1", rep["ai"]["error"])
        self.assertIn("not logged in", rep["ai"]["error"])
        self.assertNotIn("Traceback", err)

    def test_claude_garbage_output(self):
        env = self.L.checkout_env(claude="#!/bin/sh\necho 'I am not JSON'\n")
        rep, _ = self.scan(env)
        self.assertIn("non-JSON", rep["ai"]["error"])

    def test_claude_timeout_is_enforced_and_report_still_produced(self):
        env = self.L.checkout_env(claude="#!/bin/sh\nexec sleep 30\n")
        self.set_timeout(env, 1)
        t0 = time.time()
        rep, _ = self.scan(env, timeout=60)
        self.assertLess(time.time() - t0, 20)
        self.assertIn("timed out", rep["ai"]["error"])


# ----------------------------------------------------------- unit vs. docs

class SystemdUnit(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(REPO, "systemd", "organizer.service")) as f:
            self.unit = f.read()

    def keys(self):
        out = {}
        for line in self.unit.splitlines():
            if "=" in line and not line.startswith(("#", "[")):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
        return out

    def test_sandbox_directives_match_security_md(self):
        k = self.keys()
        self.assertEqual(k["ProtectSystem"], ["strict"])
        self.assertEqual(k["PrivateTmp"], ["yes"])
        self.assertEqual(k["NoNewPrivileges"], ["yes"])
        self.assertEqual(k["RuntimeDirectory"], ["organizer"])
        self.assertEqual(k["RuntimeDirectoryMode"], ["0700"])
        rw = k["ReadWritePaths"][0].split()
        self.assertEqual(rw, ["%h/.config/organizer", "%h/.local/share/organizer", "%t/organizer",
                              "%h/.claude", "%h/.claude.json"])
        self.assertNotIn("%t", rw)                         # only the dedicated sub-dir, never the whole runtime dir
        with open(os.path.join(REPO, "SECURITY.md")) as f:
            sec = f.read()
        for d in ("ProtectSystem=strict", "PrivateTmp=yes", "NoNewPrivileges=yes", "RuntimeDirectory=organizer"):
            self.assertIn(d, sec, d)

    def test_unit_runs_installed_tree(self):
        k = self.keys()
        self.assertEqual(k["ExecStart"], ["/usr/bin/python3 -m organizer.daemon"])
        self.assertIn("PYTHONPATH=%h/.local/lib/organizer", k["Environment"])
        self.assertEqual(k["WantedBy"], ["default.target"])
        self.assertEqual(k["Restart"], ["on-failure"])

    def test_security_md_allow_list_matches_code(self):
        """SECURITY.md guarantee 1 must list exactly what sandbox.default_allowed() grants."""
        with open(os.path.join(REPO, "SECURITY.md")) as f:
            guarantee = f.read().split("## Defence in depth")[0]
        self.assertNotIn("`/tmp` and `/dev`", guarantee)
        self.assertIn("$XDG_RUNTIME_DIR/organizer", guarantee)


if __name__ == "__main__":
    unittest.main()
