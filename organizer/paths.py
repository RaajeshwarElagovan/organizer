"""Filesystem locations used by organizer (XDG-style)."""
import os
import stat

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("ORGANIZER_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "organizer")
DATA_DIR = os.environ.get("ORGANIZER_DATA_DIR") or os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "organizer")

MEMORY_PATH = os.path.join(CONFIG_DIR, "memory.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(PKG_DIR, "prompts")
SEED_MEMORY = os.path.join(os.path.dirname(PKG_DIR), "seed", "memory.json")


def socket_dir() -> str:
    """A directory owned by organizer alone: it is the only place outside
    CONFIG_DIR/DATA_DIR that the Landlock sandbox lets the process write
    (see sandbox.default_allowed), so it must not be shared with anything else."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and _owned_dir(runtime):
        return os.path.join(runtime, "organizer")
    return "/tmp/organizer-%d" % os.getuid()


def _owned_dir(path: str) -> bool:
    """A runtime dir inherited from another account (`sudo -u`, `su`) is not ours."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def socket_path() -> str:
    override = os.environ.get("ORGANIZER_SOCKET")
    if override:
        return override
    return os.path.join(socket_dir(), "organizer.sock")


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)
    sd = os.path.dirname(socket_path())
    if not os.path.isdir(sd):
        os.makedirs(sd, mode=0o700, exist_ok=True)
    if not os.environ.get("ORGANIZER_SOCKET"):
        # The default socket dir becomes a Landlock-writable root and, under /tmp,
        # could be squatted or symlinked by another local user beforehand.
        st = os.lstat(sd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeError("refusing socket dir %s: must be a directory owned by uid %d with mode 0700"
                               % (sd, os.getuid()))


def dir_slug(path: str) -> str:
    slug = path.strip("/").replace("/", "_") or "root"
    return slug[:120]
