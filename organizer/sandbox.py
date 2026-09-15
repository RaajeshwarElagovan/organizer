"""Kernel-enforced write restriction via Landlock (ctypes, no dependencies).

After restrict() the calling thread — and every thread/process it creates
afterwards — can read anything but can only create/modify/delete files beneath
the allowed paths (organizer's own config/data dirs, the socket dir, /tmp).
Landlock is per-thread, so a helper thread started *before* restrict() stays
unrestricted; brain.py uses one to run the `claude` CLI, which needs to write
its own state under ~/.claude. That process gets no tools, so it cannot touch
files either.

Falls back silently (status() reports it) on kernels without Landlock.
"""
import ctypes
import os
import threading

from . import paths

SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

FS = {
    "execute": 1 << 0, "write_file": 1 << 1, "read_file": 1 << 2, "read_dir": 1 << 3,
    "remove_dir": 1 << 4, "remove_file": 1 << 5, "make_char": 1 << 6, "make_dir": 1 << 7,
    "make_reg": 1 << 8, "make_sock": 1 << 9, "make_fifo": 1 << 10, "make_block": 1 << 11,
    "make_sym": 1 << 12, "refer": 1 << 13, "truncate": 1 << 14, "ioctl_dev": 1 << 15,
}
WRITE_BITS_BY_ABI = [
    ("write_file", 1), ("remove_dir", 1), ("remove_file", 1), ("make_char", 1), ("make_dir", 1),
    ("make_reg", 1), ("make_sock", 1), ("make_fifo", 1), ("make_block", 1), ("make_sym", 1),
    ("refer", 2), ("truncate", 3), ("ioctl_dev", 4),
]
FILE_ONLY_BITS = ("write_file", "truncate", "ioctl_dev")

_STATE = {"applied": False, "abi": None, "error": None, "allowed": [], "thread": None}
_libc = None


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _syscall(*args):
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    r = _libc.syscall(*args)
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r


def abi_version():
    return int(_syscall(ctypes.c_long(SYS_landlock_create_ruleset), None, ctypes.c_size_t(0),
                        ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION)))


def default_allowed():
    dirs = [paths.CONFIG_DIR, paths.DATA_DIR, os.path.dirname(paths.socket_path()), "/tmp", "/dev"]
    return [d for d in dirs if d and os.path.isdir(d)]


def restrict(allowed_dirs=None):
    """Apply Landlock to the calling thread. Returns True if enforced."""
    if os.uname().machine not in ("x86_64", "aarch64"):
        _STATE["error"] = "landlock helper only knows x86_64/aarch64 syscall numbers"
        return False
    try:
        abi = abi_version()
    except OSError as e:
        _STATE["error"] = "landlock unavailable: %s" % e
        return False
    handled = 0
    for name, min_abi in WRITE_BITS_BY_ABI:
        if abi >= min_abi:
            handled |= FS[name]
    file_bits = sum(FS[n] for n, a in WRITE_BITS_BY_ABI if n in FILE_ONLY_BITS and abi >= a)
    allowed = allowed_dirs if allowed_dirs is not None else default_allowed()
    try:
        attr = _RulesetAttr(handled)
        rfd = _syscall(ctypes.c_long(SYS_landlock_create_ruleset), ctypes.byref(attr),
                       ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
        try:
            for p in allowed:
                fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
                try:
                    rule = _PathBeneath(handled if os.path.isdir(p) else (handled & file_bits), fd)
                    _syscall(ctypes.c_long(SYS_landlock_add_rule), ctypes.c_int(rfd),
                             ctypes.c_uint32(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule), ctypes.c_uint32(0))
                finally:
                    os.close(fd)
            _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            _syscall(ctypes.c_long(SYS_landlock_restrict_self), ctypes.c_int(rfd), ctypes.c_uint32(0))
        finally:
            os.close(rfd)
    except OSError as e:
        _STATE["error"] = "landlock setup failed: %s" % e
        return False
    _STATE.update({"applied": True, "abi": abi, "error": None, "allowed": list(allowed),
                   "thread": threading.get_ident()})
    return True


def status() -> dict:
    return {k: v for k, v in _STATE.items() if k != "thread"}
