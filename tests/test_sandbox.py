"""Adversarial tests for the Landlock write boundary (see tests/sandbox_probe.py).

Each test spawns a fresh Python process that goes through organizer's real
startup — the `--no-daemon` path (cli._fallback_context) or the daemon
(daemon.main) — and then attacks a victim directory that lies outside the
allowed roots. The verdicts come from the kernel (EACCES/EXDEV), and the
victim tree is snapshotted before and after so "blocked" is verified by the
filesystem, not by the process's own report.

Everything lives under ~/.cache/organizer-tests (not /tmp, so that the probes
proving /tmp is denied stay meaningful).
Run: PYTHONPATH=. python3 -m unittest tests.test_sandbox -v
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox_probe  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(REPO, "tests", "sandbox_probe.py")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "organizer-tests")


def kernel_has_landlock():
    try:
        from organizer import sandbox
        sandbox.abi_version()
        return True
    except OSError:
        return False


class Box:
    """One isolated config/data/victim layout plus a victim under /tmp."""

    def __init__(self):
        os.makedirs(CACHE, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="sbx-", dir=CACHE)
        self.config = os.path.join(self.root, "config")
        self.data = os.path.join(self.root, "data")
        self.victim = os.path.join(self.root, "victim")
        self.escape = os.path.join(self.root, "escape")     # sibling of victim: control + runner probes
        for d in (self.config, self.data, self.victim, self.escape):
            os.makedirs(d)
        sandbox_probe.prepare_victim(self.victim, self.data)
        self.tmp_victim = tempfile.mkdtemp(prefix="organizer-sbx-victim-")
        with open(os.path.join(self.tmp_victim, "existing.txt"), "w") as f:
            f.write("tmp victim")
        self.before = sandbox_probe.snapshot(self.victim)
        self.env = dict(os.environ, PYTHONPATH=REPO, ORGANIZER_CONFIG_DIR=self.config,
                        ORGANIZER_DATA_DIR=self.data, ORGANIZER_SOCKET=os.path.join(self.data, "sock"))
        for k in list(self.env):
            if k.startswith("CLAUDE"):
                del self.env[k]

    def unchanged(self):
        return sandbox_probe.snapshot(self.victim) == self.before

    def diff(self):
        after = sandbox_probe.snapshot(self.victim)
        return {k: (self.before.get(k), after.get(k)) for k in set(self.before) | set(after)
                if self.before.get(k) != after.get(k)}

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.tmp_victim, ignore_errors=True)


def run_probe(box, mode, timeout=120):
    r = subprocess.run([sys.executable, PROBE, mode, box.victim, box.data, box.tmp_victim],
                       env=box.env, capture_output=True, text=True, timeout=timeout, cwd=REPO)
    if r.returncode != 0:
        raise AssertionError("probe failed (%d):\n%s" % (r.returncode, r.stderr[-2000:]))
    return json.loads(r.stdout)


def failures(results):
    return ["%s (blocked=%s errno=%s, expected blocked=%s)" % (a["name"], a["blocked"], a["errno"], a["expect_blocked"])
            for a in results if not a["ok"]]


@unittest.skipUnless(kernel_has_landlock(), "kernel without Landlock: enforcement tests cannot run here")
class InProcessSandbox(unittest.TestCase):
    """`organizer --no-daemon`: cli._fallback_context() must confine the process."""

    @classmethod
    def setUpClass(cls):
        cls.box = Box()
        cls.out = run_probe(cls.box, "inprocess")

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    def test_sandbox_reported_active(self):
        st = self.out["status"]
        self.assertTrue(st["applied"], st)
        self.assertIsNone(st["error"])
        self.assertGreaterEqual(st["abi"], 1)
        self.assertTrue(self.out["no_new_privs"], "PR_SET_NO_NEW_PRIVS must be set")

    def test_allowed_roots_are_only_state_dirs(self):
        allowed = self.out["status"]["allowed"]
        self.assertIn(self.box.config, allowed)
        self.assertIn(self.box.data, allowed)
        for root in allowed:
            self.assertFalse((self.box.victim + "/").startswith(root.rstrip("/") + "/"),
                             "victim %s is under allowed root %s" % (self.box.victim, root))
        self.assertFalse(any(os.path.expanduser("~") == r.rstrip("/") for r in allowed), "home must not be allowed")

    def test_every_attack_blocked_from_restricted_thread(self):
        self.assertEqual(failures(self.out["same_thread"]), [])
        self.assertGreater(len([a for a in self.out["same_thread"] if a["expect_blocked"]]), 50)

    def test_every_attack_blocked_from_thread_started_later(self):
        self.assertEqual(failures(self.out["new_thread"]), [])

    def test_victim_tree_byte_identical(self):
        self.assertEqual(self.box.diff(), {}, "victim directory was modified")

    def test_real_scan_ran_and_changed_nothing(self):
        self.assertTrue(self.out["scan_ok"])
        self.assertTrue(self.box.unchanged())

    def test_pre_landlock_runner_thread_is_unrestricted(self):
        """Documents the boundary: the thread created before restrict() — used only to
        spawn the `claude` CLI — and processes it spawns are NOT confined."""
        self.assertTrue(self.out["runner_thread_can_write"])
        self.assertTrue(self.out["runner_subprocess_can_write"])
        # ...and it left nothing behind
        self.assertFalse(os.path.exists(os.path.join(self.box.escape, "runner-wrote-this")))
        self.assertTrue(self.box.unchanged())

    def test_allow_list_is_exactly_config_data_socket(self):
        allowed = [a.rstrip("/") for a in self.out["status"]["allowed"]]
        self.assertEqual(sorted(allowed), sorted({self.box.config, self.box.data, os.path.dirname(self.box.env["ORGANIZER_SOCKET"])}))
        for forbidden in ("/tmp", "/dev", os.environ.get("XDG_RUNTIME_DIR", "/run/user/none")):
            self.assertNotIn(forbidden.rstrip("/"), allowed)

    def test_tmp_and_dev_are_blocked(self):
        names = {a["name"]: a for a in self.out["same_thread"]}
        for key in ("absolute: /tmp create", "absolute: /tmp mkdir", "absolute: /dev/shm create",
                    "absolute: /dev/null open for write", "tempfile: default tempdir (/tmp)",
                    "scanned dir under /tmp: write existing", "scanned dir under /tmp: delete"):
            self.assertTrue(names[key]["blocked"], key)
        with open(os.path.join(self.box.tmp_victim, "existing.txt")) as f:
            self.assertEqual(f.read(), "tmp victim")

    def test_legitimate_writes_still_work(self):
        self.assertEqual(failures(self.out["legit"]), [])
        self.assertGreaterEqual(len(self.out["legit"]), 6)


@unittest.skipUnless(kernel_has_landlock(), "kernel without Landlock: enforcement tests cannot run here")
class DaemonSandbox(unittest.TestCase):
    """organizerd: daemon.main() must confine every request-handler thread."""

    @classmethod
    def setUpClass(cls):
        cls.box = Box()
        cls.proc = subprocess.Popen([sys.executable, PROBE, "daemon", cls.box.victim, cls.box.data, cls.box.tmp_victim],
                                    env=cls.box.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=REPO)
        sock = cls.box.env["ORGANIZER_SOCKET"]
        for _ in range(100):
            if os.path.exists(sock) or cls.proc.poll() is not None:
                break
            time.sleep(0.05)
        if cls.proc.poll() is not None:
            raise AssertionError("daemon exited early:\n" + cls.proc.stderr.read())
        os.environ["ORGANIZER_SOCKET"] = sock           # protocol.socket_path() reads it at call time
        from organizer import protocol
        cls.rpc = protocol.send_request
        cls.handler = cls.rpc({"cmd": "_probe", "via": "handler"})
        cls.thread = cls.rpc({"cmd": "_probe", "via": "thread"})
        cls.runner = cls.rpc({"cmd": "_probe", "via": "runner"})
        cls.legit = cls.rpc({"cmd": "_probe", "via": "legit"})
        cls.status = cls.rpc({"cmd": "status"})
        cls.scan = cls.rpc({"cmd": "scan", "cwd": cls.box.victim, "opts": {"no_ai": True}})

    @classmethod
    def tearDownClass(cls):
        cls.proc.send_signal(signal.SIGTERM)
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log = cls.proc.stderr.read()
        cls.proc.stderr.close()
        cls.box.cleanup()

    def test_daemon_logs_and_reports_landlock(self):
        sb = self.status["sandbox"]
        self.assertTrue(sb["applied"], sb)
        self.assertTrue(self.handler["no_new_privs"])
        self.assertIn(self.box.data, sb["allowed"])

    def test_handler_thread_blocked(self):
        self.assertEqual(failures(self.handler["result"]), [])

    def test_thread_spawned_by_handler_blocked(self):
        """Consolidation runs in a thread started after restrict(); it inherits the domain."""
        self.assertEqual(failures(self.thread["result"]), [])

    def test_runner_thread_unrestricted_documented(self):
        self.assertTrue(self.runner["result"])

    def test_legitimate_writes_still_work_in_daemon(self):
        self.assertEqual(failures(self.legit["result"]), [])

    def test_socket_lives_in_dedicated_dir_with_0700(self):
        sock = self.box.env["ORGANIZER_SOCKET"]
        self.assertTrue(os.path.exists(sock))
        self.assertEqual(os.stat(sock).st_mode & 0o777, 0o600)

    def test_real_scan_through_daemon_changes_nothing(self):
        self.assertTrue(self.scan["ok"], self.scan)
        self.assertTrue(self.scan["report"]["report_only"])
        self.assertEqual(self.box.diff(), {})

    def test_victim_tree_byte_identical(self):
        self.assertEqual(self.box.diff(), {})


@unittest.skipUnless(kernel_has_landlock() and os.environ.get("XDG_RUNTIME_DIR") and
                     os.path.isdir(os.environ.get("XDG_RUNTIME_DIR", "")), "needs Landlock and XDG_RUNTIME_DIR")
class RuntimeDirDefaultSocket(unittest.TestCase):
    """With the default socket location only $XDG_RUNTIME_DIR/organizer/ is writable —
    not the runtime dir itself, which on desktops also holds the document-portal
    (`doc/`) and gvfs FUSE mounts, dbus/pipewire sockets and keyrings."""

    def test_only_dedicated_subdir_is_writable(self):
        box = Box()
        try:
            env = dict(box.env)
            env.pop("ORGANIZER_SOCKET")      # default: $XDG_RUNTIME_DIR/organizer/organizer.sock
            code = ("import json,os,sys\nfrom organizer import cli,sandbox,paths\n"
                    "sys.stderr=open(os.devnull,'w')\ncli._fallback_context()\n"
                    "rt=os.environ['XDG_RUNTIME_DIR']\n"
                    "def t(p):\n"
                    "    try:\n        open(p,'x').close(); os.remove(p); return True\n"
                    "    except OSError: return False\n"
                    "u='organizer-sandbox-probe-%d'%os.getpid()\n"
                    "subs={s:t(os.path.join(rt,s,u)) for s in ('doc','gvfs','keyring','gnupg','systemd') if os.path.isdir(os.path.join(rt,s))}\n"
                    "print(json.dumps({'allowed':sandbox.status()['allowed'],'sock_dir':os.path.dirname(paths.socket_path()),"
                    "'root_writable':t(os.path.join(rt,u)),'legacy_sock_writable':t(os.path.join(rt,'organizer.sock')),"
                    "'sock_dir_writable':t(os.path.join(os.path.dirname(paths.socket_path()),u)),'subs':subs}))")
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, cwd=REPO)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            rt = os.environ["XDG_RUNTIME_DIR"].rstrip("/")
            self.assertEqual(out["sock_dir"], os.path.join(rt, "organizer"))
            self.assertIn(out["sock_dir"], out["allowed"])
            self.assertNotIn(rt, [a.rstrip("/") for a in out["allowed"]])
            self.assertTrue(out["sock_dir_writable"])
            self.assertFalse(out["root_writable"], "runtime dir root must not be writable")
            self.assertFalse(out["legacy_sock_writable"])
            self.assertEqual([k for k, v in out["subs"].items() if v], [], out["subs"])
            self.assertEqual(os.stat(out["sock_dir"]).st_mode & 0o777, 0o700)
        finally:
            box.cleanup()


class LandlockUnavailable(unittest.TestCase):
    """Pins the CURRENT behaviour when Landlock cannot be established: organizer warns
    and continues without a kernel guard (fail-open). See THREAT-MODEL.md."""

    @classmethod
    def setUpClass(cls):
        cls.box = Box()
        cls.out = run_probe(cls.box, "nolandlock")

    @classmethod
    def tearDownClass(cls):
        cls.box.cleanup()

    def test_status_is_honest(self):
        st = self.out["status"]
        self.assertFalse(st["applied"])
        self.assertIn("landlock unavailable", st["error"])
        self.assertEqual(st["allowed"], [])

    def test_fail_open_is_current_behaviour(self):
        res = self.out["same_thread"]
        self.assertTrue(all(not a["blocked"] for a in res), res)
        self.assertNotEqual(self.box.diff(), {}, "without Landlock the victim IS writable (fail-open)")

    def test_startup_did_not_abort(self):
        self.assertEqual(self.out["mode"], "nolandlock")


class SandboxUnit(unittest.TestCase):
    """Cheap checks on sandbox.py itself (no restriction applied in this process)."""

    def test_default_allowed_excludes_home_and_only_existing_dirs(self):
        from organizer import sandbox
        allowed = sandbox.default_allowed()
        self.assertTrue(all(os.path.isdir(d) for d in allowed))
        self.assertNotIn(os.path.expanduser("~"), [d.rstrip("/") for d in allowed])
        self.assertNotIn("/", allowed)

    def test_default_socket_dir_refuses_squatting(self):
        """The default socket dir is a Landlock-writable root: a pre-existing symlink or
        a directory with the wrong owner/mode must be refused, a fresh one created 0700."""
        root = tempfile.mkdtemp(prefix="sockdir-", dir=CACHE if os.path.isdir(CACHE) else None)
        try:
            code = ("import os,sys\nfrom organizer import paths\n"
                    "try:\n    paths.ensure_dirs(); print('ok %o' % (os.lstat(paths.socket_dir()).st_mode & 0o777))\n"
                    "except RuntimeError as e:\n    print('refused')")
            def run(rt):
                env = dict(os.environ, PYTHONPATH=REPO, XDG_RUNTIME_DIR=rt, ORGANIZER_CONFIG_DIR=os.path.join(root, "c"),
                           ORGANIZER_DATA_DIR=os.path.join(root, "d"))
                env.pop("ORGANIZER_SOCKET", None)
                return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=REPO).stdout.strip()
            rt = os.path.join(root, "rt"); os.mkdir(rt)
            self.assertEqual(run(rt), "ok 700")                       # fresh: created 0700
            self.assertEqual(run(rt), "ok 700")                       # existing, ours, 0700: fine
            os.chmod(os.path.join(rt, "organizer"), 0o755)
            self.assertEqual(run(rt), "refused")                      # group/other accessible
            os.rmdir(os.path.join(rt, "organizer"))
            os.symlink(root, os.path.join(rt, "organizer"))
            self.assertEqual(run(rt), "refused")                      # symlink squat
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_status_before_restrict_is_not_applied(self):
        from organizer import sandbox
        self.assertFalse(sandbox.status()["applied"])
        self.assertNotIn("thread", sandbox.status())

    def test_handled_bits_cover_all_write_operations(self):
        from organizer import sandbox
        names = {n for n, _ in sandbox.WRITE_BITS_BY_ABI}
        for need in ("write_file", "remove_dir", "remove_file", "make_dir", "make_reg", "make_sym",
                     "make_fifo", "make_sock", "make_char", "make_block", "refer", "truncate"):
            self.assertIn(need, names)
        self.assertNotIn("read_file", names)
        self.assertNotIn("read_dir", names)
        self.assertNotIn("execute", names)


if __name__ == "__main__":
    unittest.main()
