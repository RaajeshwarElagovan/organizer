"""Packaging tests: the Debian metadata under debian/, the packaged user unit,
seed resolution for a system install, and — when dpkg-buildpackage is
available — the built .deb itself.

Nothing here touches the real $HOME or the installed service; seed resolution
is exercised in subprocesses running a copy of the package without seed/.

Run: PYTHONPATH=. python3 -m unittest tests.test_packaging -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "organizer-tests")
SOURCE_UNIT = os.path.join(REPO, "systemd", "organizer.service")
PACKAGED_UNIT = os.path.join(REPO, "debian", "systemd-user", "organizer.service")
# The directives that make up the daemon's second sandbox layer. They must be
# present in both units with identical values (see SECURITY.md / THREAT-MODEL.md).
SECURITY_DIRECTIVES = ("ProtectSystem", "RuntimeDirectory", "RuntimeDirectoryMode", "ReadWritePaths",
                       "PrivateTmp", "NoNewPrivileges", "WorkingDirectory", "Restart", "Type", "ExecStart")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def directives(text):
    """[(section, key, value)] in file order, comments and blanks dropped."""
    out, section = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
            continue
        k, _, v = line.partition("=")
        out.append((section, k, v))
    return out


def version():
    return re.search(r'__version__ = "([^"]+)"', read(os.path.join(REPO, "organizer", "__init__.py"))).group(1)


# ------------------------------------------------------------------ unit files

class PackagedUnit(unittest.TestCase):
    """debian/systemd-user/organizer.service is the source unit adapted to the
    packaged layout and nothing else: same ExecStart, same environment minus
    PYTHONPATH, same security directives, only Documentation= re-pointed."""

    def setUp(self):
        self.src = directives(read(SOURCE_UNIT))
        self.pkg = directives(read(PACKAGED_UNIT))

    def test_only_documentation_and_pythonpath_differ(self):
        drop = {("[Service]", "Environment", "PYTHONPATH=%h/.local/lib/organizer")}
        self.assertIn(list(drop)[0], self.src, "source unit lost its PYTHONPATH line; update this test on purpose")
        src = [d for d in self.src if d not in drop and d[1] != "Documentation"]
        pkg = [d for d in self.pkg if d[1] != "Documentation"]
        self.assertEqual(src, pkg)
        self.assertNotIn("PYTHONPATH", read(PACKAGED_UNIT))

    def test_documentation_points_at_packaged_docs(self):
        self.assertIn(("[Unit]", "Documentation", "file:///usr/share/doc/organizer/README.md"), self.pkg)
        self.assertIn(("[Unit]", "Documentation", "file://%h/.local/lib/organizer/README.md"), self.src)

    def test_security_directives_identical(self):
        src = {k: v for _, k, v in self.src}
        pkg = {k: v for _, k, v in self.pkg}
        for key in SECURITY_DIRECTIVES:
            self.assertIn(key, pkg, key)
            self.assertEqual(src[key], pkg[key], key)
        self.assertEqual(pkg["NoNewPrivileges"], "yes")
        self.assertEqual(pkg["ProtectSystem"], "strict")
        self.assertEqual(pkg["RuntimeDirectory"], "organizer")
        self.assertEqual(pkg["RuntimeDirectoryMode"], "0700")
        self.assertEqual(pkg["PrivateTmp"], "yes")
        self.assertEqual(pkg["ExecStart"], "/usr/bin/python3 -m organizer.daemon")
        # the allow-list is the Landlock one (config, data, socket dir) plus claude's login
        self.assertEqual(pkg["ReadWritePaths"].split(),
                         ["%h/.config/organizer", "%h/.local/share/organizer", "%t/organizer",
                          "%h/.claude", "%h/.claude.json"])

    def test_environment_identical_except_pythonpath(self):
        env = lambda ds: sorted(v for s, k, v in ds if k == "Environment" and not v.startswith("PYTHONPATH="))
        self.assertEqual(env(self.src), env(self.pkg))
        self.assertIn("PYTHONSAFEPATH=1", env(self.pkg))
        self.assertIn("PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin", env(self.pkg))
        self.assertEqual([v for s, k, v in self.pkg if k == "WantedBy"], ["default.target"])


# ------------------------------------------------------------------ debian/ metadata

class DebianMetadata(unittest.TestCase):

    def setUp(self):
        self.control = read(os.path.join(REPO, "debian", "control"))
        self.changelog = read(os.path.join(REPO, "debian", "changelog"))

    def test_version_tracks_package(self):
        self.assertTrue(self.changelog.startswith("organizer (%s-1) " % version()), self.changelog.splitlines()[0])

    def test_control_essentials(self):
        self.assertIn("Package: organizer\n", self.control)
        self.assertIn("Architecture: all\n", self.control)
        self.assertIn("Rules-Requires-Root: no\n", self.control)
        self.assertIn("X-Python3-Version: >= 3.8\n", self.control)
        self.assertRegex(self.control, r"Depends: \$\{python3:Depends\}, \$\{misc:Depends\}\n")
        self.assertIn("Recommends: systemd, file\n", self.control)
        for forbidden in ("claude", "nodejs", "npm", "python3-"):
            self.assertNotRegex(self.control, r"^(Depends|Recommends|Suggests):.*%s" % forbidden)
        self.assertIn("not part of this package", self.control)     # Claude is external
        self.assertIn("not enabled\n automatically", self.control)

    def test_install_list_covers_the_runtime(self):
        entries = dict(l.split() for l in read(os.path.join(REPO, "debian", "install")).splitlines() if l.strip())
        self.assertEqual(entries["organizer"], "usr/lib/python3/dist-packages")
        self.assertEqual(entries["seed/memory.json"], "usr/share/organizer/seed")
        self.assertEqual(entries["debian/systemd-user/organizer.service"], "usr/lib/systemd/user")
        self.assertEqual(entries["debian/bin/organizer"], "usr/bin")
        for src in entries:
            self.assertTrue(os.path.exists(os.path.join(REPO, src)), src)
        docs = read(os.path.join(REPO, "debian", "docs")).split()
        for d in ("README.md", "SECURITY.md", "THREAT-MODEL.md", "MEMORY-GUIDE.md", "LICENSE"):
            self.assertIn(d, docs)

    def test_launcher(self):
        launcher = os.path.join(REPO, "debian", "bin", "organizer")
        self.assertTrue(os.access(launcher, os.X_OK))
        text = read(launcher)
        self.assertTrue(text.startswith("#!/usr/bin/python3\n"))
        self.assertIn("from organizer.cli import main", text)
        # no path manipulation, no home-relative paths: the module comes from dist-packages
        code = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(code, ["from organizer.cli import main", 'if __name__ == "__main__":', "    main()"])
        r = subprocess.run([launcher, "--version"], cwd=REPO, env={"PYTHONPATH": REPO, "HOME": REPO, "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "organizer " + version(), r.stderr)

    def test_no_hand_written_maintainer_scripts(self):
        # dh_python3 generates the byte-compile postinst/prerm; nothing else is allowed
        # (no enabling of the user service, no linger, no touching of user data).
        deb = os.path.join(REPO, "debian")
        for n in os.listdir(deb):
            self.assertNotRegex(n, r"\.(postinst|preinst|prerm|postrm|triggers|user\.service)$", n)
        rules = read(os.path.join(deb, "rules"))
        self.assertIn("override_dh_installsystemduser:", rules)
        self.assertIn("--with python3", rules)

    def test_copyright_matches_license(self):
        lic = read(os.path.join(REPO, "LICENSE"))
        cp = read(os.path.join(REPO, "debian", "copyright"))
        self.assertIn("License: MIT", cp)
        flat = " ".join(cp.replace("\n .", "\n").split())
        self.assertIn("Copyright: 2026 Raajeshwar Elagovan", flat)
        for para in lic.split("\n\n")[2:]:            # the licence text itself, paragraph by paragraph
            self.assertIn(" ".join(para.split()), flat)


# ------------------------------------------------------------------ seed resolution

class SeedResolution(unittest.TestCase):
    """paths.SEED_MEMORY for a package installed without a sibling seed/ dir."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(CACHE, exist_ok=True)
        cls.root = tempfile.mkdtemp(prefix="pk-", dir=CACHE)
        cls.lib = os.path.join(cls.root, "dist-packages")
        shutil.copytree(os.path.join(REPO, "organizer"), os.path.join(cls.lib, "organizer"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.share = os.path.join(cls.root, "usr", "share")
        os.makedirs(os.path.join(cls.share, "organizer", "seed"))
        shutil.copy(os.path.join(REPO, "seed", "memory.json"), os.path.join(cls.share, "organizer", "seed", "memory.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def resolve(self, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": self.root, "PYTHONPATH": self.lib, "PYTHONSAFEPATH": "1",
             "PYTHONDONTWRITEBYTECODE": "1"}
        e.update(env)
        r = subprocess.run([sys.executable, "-c", "from organizer import paths; print(paths.SEED_MEMORY)"],
                           env=e, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_checkout_uses_sibling_seed(self):
        r = subprocess.run([sys.executable, "-c", "from organizer import paths; print(paths.SEED_MEMORY)"],
                           env={"PATH": "/usr/bin:/bin", "HOME": self.root, "PYTHONPATH": REPO, "XDG_DATA_DIRS": self.share},
                           cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), os.path.join(REPO, "seed", "memory.json"))

    def test_xdg_data_dirs(self):
        self.assertEqual(self.resolve(XDG_DATA_DIRS="/nonexistent:" + self.share),
                         os.path.join(self.share, "organizer", "seed", "memory.json"))

    def test_default_data_dirs_when_unset(self):
        # no sibling seed, no XDG_DATA_DIRS: /usr/local/share, /usr/share are tried; when
        # neither has one, the sibling path is returned and seed_if_missing writes an empty memory
        got = self.resolve()
        expected = [os.path.join(self.lib, "seed", "memory.json"),
                    "/usr/local/share/organizer/seed/memory.json", "/usr/share/organizer/seed/memory.json"]
        self.assertIn(got, expected)
        if got != expected[0]:
            self.assertTrue(os.path.exists(got))

    def test_env_override_wins(self):
        self.assertEqual(self.resolve(ORGANIZER_SEED="/x/seed.json", XDG_DATA_DIRS=self.share), "/x/seed.json")

    def test_seed_if_missing_semantics_unchanged(self):
        cfg = os.path.join(self.root, "cfg-%d" % os.getpid())
        code = ("from organizer import memory, paths\n"
                "import json\n"
                "print(memory.seed_if_missing(paths.MEMORY_PATH))\n"
                "with open(paths.MEMORY_PATH) as f: m = json.load(f)\n"
                "print(len(m['rules']))\n")
        env = dict(PATH="/usr/bin:/bin", HOME=self.root, PYTHONPATH=self.lib, PYTHONSAFEPATH="1",
                   PYTHONDONTWRITEBYTECODE="1", XDG_DATA_DIRS=self.share, ORGANIZER_CONFIG_DIR=cfg,
                   ORGANIZER_DATA_DIR=os.path.join(cfg, "data"))
        run = lambda: subprocess.run([sys.executable, "-c", code], env=env, cwd=self.root,
                                     capture_output=True, text=True, timeout=60)
        with open(os.path.join(REPO, "seed", "memory.json")) as f:
            seed_rules = len(json.load(f)["rules"])
        r = run()
        self.assertEqual(r.stdout.split(), ["True", str(seed_rules)], r.stderr)
        with open(os.path.join(cfg, "memory.json"), "w") as f:      # user memory: must never be overwritten
            f.write('{"rules": []}')
        r = run()
        self.assertEqual(r.stdout.split(), ["False", "0"], r.stderr)


# ------------------------------------------------------------------ the built package

def can_build():
    return all(shutil.which(t) for t in ("dpkg-buildpackage", "dh", "dh_python3", "fakeroot"))


@unittest.skipUnless(can_build(), "needs dpkg-dev, debhelper, dh-python, fakeroot")
class BuiltPackage(unittest.TestCase):
    """Build the .deb from a copy of the tree and inspect it (no root needed)."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(CACHE, exist_ok=True)
        cls.root = tempfile.mkdtemp(prefix="pkb-", dir=CACHE)
        src = os.path.join(cls.root, "organizer-" + version())
        shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "__pycache__", "dist", "*.pyc"))
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": cls.root, "LANG": "C.UTF-8", "DEB_BUILD_OPTIONS": "nocheck"}
        r = subprocess.run(["dpkg-buildpackage", "-us", "-uc", "-b"], cwd=src, env=env, capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise RuntimeError(r.stdout + r.stderr)
        cls.deb = os.path.join(cls.root, "organizer_%s-1_all.deb" % version())
        cls.info = subprocess.run(["dpkg-deb", "--info", cls.deb], capture_output=True, text=True, check=True).stdout
        cls.files = [l.split()[-1].lstrip(".") for l in
                     subprocess.run(["dpkg-deb", "--contents", cls.deb], capture_output=True, text=True, check=True).stdout.splitlines()]
        cls.ctl = os.path.join(cls.root, "ctl")
        subprocess.run(["dpkg-deb", "--control", cls.deb, cls.ctl], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_fields(self):
        self.assertIn(" Package: organizer\n", self.info)
        self.assertIn(" Version: %s-1\n" % version(), self.info)
        self.assertIn(" Architecture: all\n", self.info)
        self.assertRegex(self.info, r" Depends: python3:any \(>= 3\.8~\)\n")

    def test_contents(self):
        for p in ("/usr/bin/organizer", "/usr/lib/python3/dist-packages/organizer/cli.py",
                  "/usr/lib/python3/dist-packages/organizer/daemon.py",
                  "/usr/lib/python3/dist-packages/organizer/prompts/system_propose.md",
                  "/usr/lib/python3/dist-packages/organizer/prompts/system_learn.md",
                  "/usr/share/organizer/seed/memory.json", "/usr/lib/systemd/user/organizer.service",
                  "/usr/share/doc/organizer/README.md", "/usr/share/doc/organizer/SECURITY.md",
                  "/usr/share/doc/organizer/MEMORY-GUIDE.md", "/usr/share/doc/organizer/LICENSE",
                  "/usr/share/doc/organizer/copyright", "/usr/share/doc/organizer/changelog.gz"):
            self.assertIn(p, self.files, p)
        for p in self.files:
            self.assertNotIn("__pycache__", p)
            self.assertFalse(p.endswith(".pyc"), p)
            self.assertNotIn("/tests/", p)
            self.assertNotIn("/home/", p)
        py = [p for p in self.files if p.startswith("/usr/lib/python3/dist-packages/organizer/") and p.endswith(".py")]
        self.assertEqual(sorted(os.path.basename(p) for p in py),
                         sorted(n for n in os.listdir(os.path.join(REPO, "organizer")) if n.endswith(".py")))

    def test_no_development_paths_inside(self):
        data = subprocess.run(["dpkg-deb", "--fsys-tarfile", self.deb], capture_output=True, check=True).stdout
        # docs legitimately mention /home/<user> and /tmp; the build machine's own paths must not appear
        for needle in (os.path.expanduser("~").encode(), REPO.encode(), CACHE.encode(), self.root.encode()):
            self.assertFalse(needle in data, "development path %r inside the package" % needle)

    def test_maintainer_scripts_are_only_dh_python3(self):
        scripts = sorted(n for n in os.listdir(self.ctl) if n not in ("control", "md5sums"))
        self.assertEqual(scripts, ["postinst", "prerm"])
        for n in scripts:
            text = read(os.path.join(self.ctl, n))
            body = [l for l in text.splitlines() if l.strip() and not l.startswith("#") and l.strip() != "set -e"]
            self.assertTrue(all(("py3compile" in l or "py3clean" in l or "pypy3" in l or l.strip() in ("fi", "else")
                                 or l.startswith(("if ", "\t", "    "))) for l in body), text)
            for forbidden in ("systemctl", "deb-systemd", "loginctl", "linger", ".config", "share/organizer", "rm -r"):
                self.assertNotIn(forbidden, text, n)

    def test_packaged_unit_and_seed_are_verbatim(self):
        tar = subprocess.run(["dpkg-deb", "--fsys-tarfile", self.deb], capture_output=True, check=True).stdout
        out = os.path.join(self.root, "x")
        os.makedirs(out)
        subprocess.run(["tar", "-x", "-C", out], input=tar, check=True)
        self.assertEqual(read(os.path.join(out, "usr/lib/systemd/user/organizer.service")), read(PACKAGED_UNIT))
        self.assertEqual(read(os.path.join(out, "usr/share/organizer/seed/memory.json")),
                         read(os.path.join(REPO, "seed", "memory.json")))
        self.assertEqual(read(os.path.join(out, "usr/bin/organizer")), read(os.path.join(REPO, "debian", "bin", "organizer")))


if __name__ == "__main__":
    unittest.main()
