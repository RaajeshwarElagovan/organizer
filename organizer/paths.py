"""Filesystem locations used by organizer (XDG-style)."""
import os

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


def socket_path() -> str:
    override = os.environ.get("ORGANIZER_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "organizer.sock")
    return "/tmp/organizer-%d.sock" % os.getuid()


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)


def dir_slug(path: str) -> str:
    slug = path.strip("/").replace("/", "_") or "root"
    return slug[:120]
