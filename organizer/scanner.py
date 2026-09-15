"""Collect facts about directory entries. Read-only; never opens file contents.

The single exception is an optional `file -b --mime-type` call for
extensionless regular files, which inspects magic bytes only. It is gated by
settings["use_magic"].
"""
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import time

MULTI_EXT = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

# name-signal regexes: signal -> compiled pattern (matched against the name)
SIGNAL_PATTERNS = {
    "screenshot": re.compile(r"^(screenshot|screen shot|capture)", re.I),
    "ai_image": re.compile(r"^(chatgpt image|dall.?e|midjourney|gemini_generated|firefly)", re.I),
    "drive_download": re.compile(r"^drive-download-\d{8}T\d{6}Z", re.I),
    "backup": re.compile(r"backup", re.I),
    "release": re.compile(r"(release|firmware|build|rc\d*)[_\-\s]", re.I),
    "arch_token": re.compile(r"[_\-](all|amd64|x86_64|arm64|armhf|aarch64|i386)(\.|$|[_\-])", re.I),
    "version_token": re.compile(r"(^|[_\-\s])v?\d+([._]\d+){2,}([_\-.]|$)", re.I),
    "date_token": re.compile(r"(\d{4}[-_.]?\d{2}[-_.]?\d{2})|(\d{2}[A-Z][a-z]{2}\d{4})|([A-Z][a-z]{2} \d{1,2}, \d{4})"),
    "generic_name": re.compile(r"^(image|img|photo|document|doc|untitled|new file|download|file|scan)([ _-]?\(?\d*\)?)?$", re.I),
    "temp_marker": re.compile(r"(temp|tmp|draft|old|copy|test)([_\-\s.]|$)", re.I),
}
DUP_SUFFIX = re.compile(r"^(?P<base>.+?)(?: \((?P<n>\d+)\)| (?P<m>\d))$")
DIGIT_RUN = re.compile(r"\d+([._-]\d+)*")
VERSION_NUMS = re.compile(r"\d+")


def split_name(name: str):
    lower = name.lower()
    for me in MULTI_EXT:
        if lower.endswith(me):
            return name[: -len(me)], me[1:]
    stem, ext = os.path.splitext(name)
    if ext and len(ext) <= 12 and not ext[1:].isdigit():
        return stem, ext[1:].lower()
    return name, ""


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return str(n)


def _magic_mime(path: str):
    exe = shutil.which("file")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-b", "--mime-type", path], capture_output=True,
                             text=True, timeout=5)
        m = out.stdout.strip()
        return m or None
    except (subprocess.SubprocessError, OSError):
        return None


def entry_facts(dirpath: str, name: str, now: float, settings: dict) -> dict:
    full = os.path.join(dirpath, name)
    try:
        st = os.lstat(full)
    except OSError as e:
        return {"name": name, "error": str(e), "signals": ["stat_error"]}
    is_link = stat.S_ISLNK(st.st_mode)
    if is_link:
        try:
            st = os.stat(full)
        except OSError:
            pass
    is_dir = stat.S_ISDIR(st.st_mode)
    stem, ext = split_name(name) if not is_dir else (name, "")
    mime = None
    if not is_dir:
        mime = mimetypes.guess_type(name, strict=False)[0]
        if mime is None and not ext and settings.get("use_magic", True) and stat.S_ISREG(st.st_mode):
            mime = _magic_mime(full)
            if mime:
                mime = mime + " (magic)"
    size = st.st_size
    child_count = None
    if is_dir:
        try:
            with os.scandir(full) as it:
                children = list(it)
            child_count = len(children)
            size = 0
            for c in children:
                try:
                    if c.is_file(follow_symlinks=False):
                        size += c.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
        except OSError:
            child_count = -1
    last_touch = max(st.st_mtime, st.st_atime)
    signals = [s for s, pat in SIGNAL_PATTERNS.items() if pat.search(stem)]
    if is_dir:
        signals.append("directory")
    if is_link:
        signals.append("symlink")
    if name.startswith("."):
        signals.append("hidden")
    if not is_dir and (st.st_mode & 0o111):
        signals.append("executable")
    if not is_dir and not ext:
        signals.append("no_extension")
    m = DUP_SUFFIX.match(stem)
    dup_base = None
    if m:
        dup_base = m.group("base")
        signals.append("dup_suffix")
    return {
        "name": name,
        "stem": stem,
        "ext": ext,
        "is_dir": is_dir,
        "mime": mime,
        "size": size,
        "size_h": human_size(size),
        "child_count": child_count,
        "mtime": int(st.st_mtime),
        "atime": int(st.st_atime),
        "ctime": int(st.st_ctime),
        "age_days": int((now - st.st_mtime) // 86400),
        "untouched_days": int((now - last_touch) // 86400),
        "signals": signals,
        "dup_base": dup_base,
        "group": {},
    }


def _series_key(stem: str) -> str:
    return DIGIT_RUN.sub("#", stem).lower()


def _version_tuple(stem: str):
    return tuple(int(x) for x in VERSION_NUMS.findall(stem))


def add_cross_file_signals(entries: list) -> None:
    by_name = {e["name"]: e for e in entries}
    by_stem = {}
    for e in entries:
        if not e.get("is_dir"):
            by_stem.setdefault(e["stem"].lower(), []).append(e)

    # duplicates: "X (1).ext" / "X 1.ext" whose original "X.ext" exists
    for e in entries:
        if e.get("dup_base") and not e.get("is_dir"):
            base_name = e["dup_base"] + ("." + e["ext"] if e["ext"] else "")
            orig = by_name.get(base_name)
            if orig and not orig.get("is_dir"):
                same = orig["size"] == e["size"]
                e["group"]["duplicate_of"] = orig["name"]
                e["group"]["same_size"] = same
                e["signals"].append("duplicate_identical" if same else "duplicate_revision")
                orig["group"].setdefault("duplicates", []).append(e["name"])
                if "has_duplicates" not in orig["signals"]:
                    orig["signals"].append("has_duplicates")

    # extracted archives: X.zip alongside directory X
    for e in entries:
        if e.get("is_dir"):
            for other in entries:
                if not other.get("is_dir") and other["stem"] == e["name"] and other["ext"] in (
                        "zip", "tar.gz", "tgz", "tar.bz2", "tar.xz", "7z", "rar", "tar"):
                    other["group"]["extracted_to"] = e["name"]
                    other["signals"].append("extracted_dir_present")
                    e["group"]["extracted_from"] = other["name"]
                    e["signals"].append("archive_present")

    # version series: same non-digit skeleton + same ext, version_token present, >=2 members
    series = {}
    for e in entries:
        if e.get("is_dir") or "version_token" not in e["signals"] or "duplicate_of" in e["group"]:
            continue
        series.setdefault((_series_key(e["stem"]), e["ext"]), []).append(e)
    for members in series.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: (_version_tuple(x["stem"]), x["mtime"]))
        newest = members[-1]
        for mem in members:
            mem["group"]["series"] = [m["name"] for m in members]
            mem["group"]["series_newest"] = newest["name"]
            mem["signals"].append("series_newest" if mem is newest else "series_older")


def scan_dir(dirpath: str, settings: dict) -> dict:
    dirpath = os.path.abspath(dirpath)
    now = time.time()
    with os.scandir(dirpath) as it:
        names = sorted(e.name for e in it)
    entries = [entry_facts(dirpath, n, now, settings) for n in names]
    entries = [e for e in entries if "error" not in e]
    add_cross_file_signals(entries)
    existing_dirs = [e["name"] for e in entries if e.get("is_dir")]
    return {"cwd": dirpath, "scanned_at": int(now), "entries": entries,
            "existing_dirs": existing_dirs}
