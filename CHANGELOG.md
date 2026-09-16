# Changelog

All notable changes to `organizer` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Version numbers track
`__version__` in `organizer/__init__.py`.

## [Unreleased]

## [0.1.0] - 2026-09-16

Initial release.

### Added
- **Report-only scan** of a directory (`organizer [path]`): classifies each
  entry from name, extension, MIME guess and `stat()` metadata only, and
  proposes a folder structure plus a per-file action — `move`, `move-to`,
  `archive`, `delete`, `keep` or `review`. No file is ever modified.
- **Kernel-enforced write restriction** via Landlock (`organizer/sandbox.py`,
  ctypes, no dependencies): after startup the daemon and the in-process CLI
  can read anywhere but write only under `~/.config/organizer`,
  `~/.local/share/organizer`, the socket dir and `/tmp`. Falls back with a
  visible warning on kernels without Landlock.
- **Stage 1 rule engine** (`classifier.py`): memory rules with dir-scoped
  precedence, identical/revision duplicate detection, version-series
  detection (older builds → archive or delete), already-extracted archive
  detection, extension/MIME/signal category table, age policy
  (`archive_after_days`, `stale_after_days`, `recent_access_veto_days`).
- **Scanner signals** (`scanner.py`): `screenshot`, `ai_image`,
  `drive_download`, `backup`, `release`, `arch_token`, `version_token`,
  `date_token`, `generic_name`, `temp_marker`, `dup_suffix`,
  `duplicate_identical`, `duplicate_revision`, `has_duplicates`,
  `extracted_dir_present`, `archive_present`, `series_newest`,
  `series_older`, `directory`, `symlink`, `hidden`, `executable`,
  `no_extension`; optional `file --mime-type` probe for extensionless files.
- **Stage 2 via Claude Code** (`brain.py`): entries below `ai_threshold`
  are sent in one batched `claude -p` call with `--tools ""`,
  `--strict-mcp-config`, `--max-turns 1`, a JSON schema and a spend cap.
  Decisions are overlaid on the proposals; up to 5 specific new rules per
  call are merged into memory (over-broad globs rejected). Per-entry
  decision cache (size+mtime key, 7-day TTL) makes re-scans free;
  `--fresh` bypasses it, `--no-ai` skips the stage.
- **Self-learning memory** (`memory.py`, `engine.py`):
  - layer 1 — every scan compares last time's proposals with the directory
    now and records `confirmed` / `moved_elsewhere` / `deleted` / `ignored`
    outcomes, adjusting rule `hits`, `contradictions` and `confidence`;
  - layer 2 — when `learn_batch` outcomes accumulate or a rule reaches
    `learn_contradictions`, Claude rewrites `rules`, `categories`,
    `targets` and `claude_notes` in a background thread; rewrites are
    schema-validated and rejected if they drop more than half the rules;
    `memory.json.bak` kept.
- **`memory.json`** as a plain, agent-editable document with hot-reload,
  validation on load/save, atomic writes and a last-good-copy fallback;
  schema and editing advice in `MEMORY-GUIDE.md` (also copied next to the
  memory file on install).
- **Daemon** (`daemon.py`): `systemd --user` service on a `0600` Unix
  socket (`$XDG_RUNTIME_DIR/organizer.sock`), JSON-lines protocol,
  commands `scan`, `explain`, `history`, `status`, `learn`, `reload`.
  The CLI falls back to in-process mode when the daemon is unreachable
  (`--no-daemon` forces it).
- **CLI** (`cli.py`): `organizer [path] [--all] [--no-ai] [--fresh]
  [--json]`, `explain <file>`, `history [path]`, `status`,
  `learn [--dry-run]`, `memory [show|validate|path]`, `reload`.
- **Reports** saved per directory under
  `~/.local/share/organizer/reports/<slug>/<timestamp>.json` with a
  `latest.json`, 20 kept per directory; colour terminal rendering
  (`report.py`).
- **Seed memory** (`seed/memory.json`) with starting categories and rules.
- **Install/uninstall scripts** targeting `~/.local` (no root), systemd unit
  with `ProtectSystem=strict`, `ReadWritePaths`, `PrivateTmp`,
  `NoNewPrivileges` as a second sandbox layer.
- `tests/test_lifecycle.py`: install / reinstall / upgrade / failed-install
  / uninstall / `--purge` / partial-uninstall runs of the real scripts in a
  throw-away `$HOME` with `systemctl`/`loginctl`/`rm` shims; daemon socket
  permissions, stale and live socket handling, shutdown cleanup, runtime-dir
  variants; CLI fallback, `--no-daemon`, Claude missing / failing / garbage
  output / timeout; unit-file directives cross-checked against `SECURITY.md`.
- `tests/test_sandbox.py` + `tests/sandbox_probe.py`: adversarial Landlock
  tests — 70+ filesystem attacks (write, create, delete, rename, symlink
  escapes and chains, `..`/`openat` traversal, absolute paths, temp files,
  organizer's own write patterns aimed at the scanned dir, subprocesses)
  run inside a really restricted process in both `--no-daemon` and daemon
  mode, with a byte-level before/after snapshot of the victim tree; plus
  pinned documentation of the gaps (`/tmp` and `$XDG_RUNTIME_DIR` in the
  allow-list, metadata calls, the pre-Landlock `_Runner` thread, fail-open
  when Landlock is unavailable).
- `tests/test_claude_boundary.py`: 46 unit tests for malicious and
  malformed model output (path traversal, invalid destinations, bad types,
  poisoned cache, hostile consolidation rewrites, end-to-end scan).
- Project documentation: `ARCHITECTURE.md`, `SECURITY.md`,
  `THREAT-MODEL.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, MIT `LICENSE`.
- GitHub Actions workflow (`.github/workflows/ci.yml`): compile checks, the
  full unittest suite (`-X dev`, `ResourceWarning`s fail the run) and an
  automated CLI / in-process / dev-daemon smoke run
  (`.github/scripts/smoke.sh`) on Python 3.8, 3.12 and 3.14, in a throw-away
  `$HOME` without Claude; new tests for the glob-only policy on Claude rules.

### Changed
- **Socket path moved** from `$XDG_RUNTIME_DIR/organizer.sock` to
  `$XDG_RUNTIME_DIR/organizer/organizer.sock` (and `/tmp/organizer-<uid>.sock`
  to `/tmp/organizer-<uid>/organizer.sock`). `./install.sh` restarts the
  service so CLI and daemon move together; `ORGANIZER_SOCKET` still overrides.
  The unit gains `RuntimeDirectory=organizer` / `RuntimeDirectoryMode=0700`;
  `uninstall.sh` removes the new dir.
- `install.sh` no longer changes `loginctl` lingering by default: the daemon
  is only useful while a session exists (the CLI falls back to in-process
  mode) and lingering is a per-user system setting the uninstaller cannot
  safely revert. Opt in with `ORGANIZER_LINGER=1 ./install.sh` (replaces the
  short-lived `ORGANIZER_NO_LINGER=1` opt-out); `uninstall.sh` leaves
  lingering as is and says how to check/disable it.
- `install.sh` checks for `/usr/bin/python3` ≥ 3.8 (what the launcher and
  unit actually run), enables the unit and starts it once (`enable` +
  `restart` instead of `enable --now` + `restart`), prints a "re-run" hint
  if a step fails, and writes `PYTHONSAFEPATH=1` into the launcher (the unit
  sets it too) so `organizer` run from inside a checkout still executes the
  installed copy (Python ≥ 3.11).
- `organizer --no-daemon status` reports the local view (memory file, `claude`
  CLI, Landlock availability, socket it would use) with exit 0 instead of
  "daemon: not running".

### Fixed
- `sandbox.py`: the packed `landlock_path_beneath_attr` ctypes struct now
  declares `_layout_ = "ms"` (the layout `_pack_` always implied), which
  silences Python 3.14's `DeprecationWarning` (an error from 3.19). Layout
  verified unchanged on 3.8, 3.12 and 3.14: 12 bytes, `parent_fd` at 8.
- Learning layer 1: a `delete` proposal whose file was moved into another
  folder is now recorded as `moved_elsewhere:<path>` (a contradiction), not
  as `confirmed`.
- Learning layer 1: a contradiction or `ignored` outcome can no longer
  *raise* the confidence of a rule that is already below its floor.
- Consolidation: outcomes recorded while the Claude call was running stay
  pending and are no longer also copied into `learned.confirmed`.
- `memory.validate()` rejects non-integer `hits`/`contradictions`,
  non-string `note`/`category` and non-string rule ids instead of accepting
  them (which crashed a later scan) or crashing itself on an unhashable id.
- An invalid `memory.json` (corrupt or failing validation, at startup or
  after an edit) is no longer overwritten by the next scan or consolidation:
  the daemon keeps working from its last good copy, warns on every scan and
  leaves the file for the user to repair. Previously the first scan replaced
  it with the in-memory copy (an *empty* memory if it was invalid at startup)
  and the second scan pushed the original out of `.bak`.
- Outcome detection uses the recorded byte size as well as the name when
  looking for a proposed file: an unrelated same-name file of another size
  at the destination or elsewhere no longer produces a false `confirmed` /
  `moved_elsewhere`; such cases are logged as ambiguous and teach nothing.
- `memory.validate()` type-checks `settings` values (bool/str/non-negative
  number, `ai_threshold` 0..1) so a hand-edited `"learn_batch": "5"` is
  refused instead of crashing `needs_consolidation()`.
- Hot-reload change detection uses (mtime, size, inode) instead of mtime
  alone.
- `organizer learn` no longer crashes on a malformed rewrite before it can
  reject it.
- `uninstall.sh` with `XDG_RUNTIME_DIR` unset removed `/tmp/organizer` — a
  path organizer never uses (the fallback is `/tmp/organizer-<uid>`), which
  could be the user's own checkout. It now removes only
  `$XDG_RUNTIME_DIR/organizer` (when set) and `/tmp/organizer-<uid>`.
- `install.sh` / `uninstall.sh` aborted with `USER: unbound variable` when
  `$USER` was not exported (e.g. from cron or a minimal shell); both now fall
  back to `id -un`. In `uninstall.sh` this happened *before* `--purge`.
- Global flags given before the subcommand (`organizer --no-daemon status`,
  `organizer --no-daemon scan DIR`, `--json …`) were silently ignored because
  the parent parser's defaults were re-applied by the subparser; the documented
  forms now work.
- The daemon unconditionally unlinked whatever socket file it found at
  startup, so a second daemon (e.g. `python3 -m organizer.daemon` next to
  the service) hijacked the path and, on shutdown, deleted the *other*
  daemon's socket. It now probes the file: a stale one is removed, a live one
  makes the newcomer exit with a clear message, and only the socket the
  process bound itself is unlinked on exit. The socket is created under
  umask 077 so it is never briefly group/world-connectable.
- `XDG_RUNTIME_DIR` pointing at a directory owned by another uid (kept by
  `sudo -u` / `su`) crashed startup with `EACCES`; it now falls back to
  `/tmp/organizer-<uid>` like an unset variable.
- A refused (squatted) socket dir or an unwritable config dir printed a
  Python traceback from both the daemon and the in-process CLI; both now
  exit 1 with a one-line message.
- `SECURITY.md` guarantee 1 still listed `/tmp` and `/dev` in the Landlock
  allow-list; it now matches `sandbox.default_allowed()` (config, data and
  the dedicated socket dir only) and states which systemd directives are
  actually in effect on hosts without unprivileged mount namespaces.

### Security
- Claude-generated rules are glob-only: a `new_rules` item carrying a `regex`
  is rejected (even alongside a valid glob), and a consolidation rewrite that
  adds a regex rule or changes an existing pattern is rejected as a whole. A
  rewrite may keep a hand-written regex rule verbatim or drop it. `regex`
  stays supported for user-edited rules. The `new_rules` JSON schema and
  both prompts no longer offer the key.
- Rule regexes that quantify a group ending in a quantifier (`(a+)+`) are
  refused on every input path as a catastrophic-backtracking risk. This is a
  heuristic for the textbook form only; see THREAT-MODEL.md for what it does
  not catch.
- Stage-1 destinations taken from rules and categories are confined to the
  scanned directory like Claude's decisions: a `move`/`archive` target that
  resolves outside it through a symlink becomes `review`.
- **Landlock allow-list narrowed** to exactly three organizer-owned
  directories: the config dir, the data dir and a dedicated socket dir
  `$XDG_RUNTIME_DIR/organizer/` (fallback `/tmp/organizer-<uid>/`, mode
  0700). `/tmp`, `/dev` and the rest of `$XDG_RUNTIME_DIR` (document-portal
  and gvfs mounts, dbus, keyrings) are no longer writable; verified by the
  adversarial suite. The default socket dir is refused if it is a symlink or
  not a 0700 directory owned by the current user.
- Claude-generated proposals and rules are now validated in code, not only
  by the prompt and `--json-schema`. Destinations are rejected when absolute,
  home-relative (`~`), containing `..` or control characters, or (for `move`
  / `archive`) resolving outside the scanned directory through a symlink;
  `archive` must stay under `_archive/`, `move-to` must be a configured
  target name or a `~/…` path. Unknown actions, non-numeric/NaN confidences
  and traversal in `category` / `category_dir` are rejected. Rejected
  decisions show as `review` with the reason and are never cached; stale
  cached decisions that fail validation are purged.
- `memory.validate()` now checks category dirs, `targets` values, rule
  targets per action, match-key types and `scope`, so a bad edit or
  consolidation rewrite cannot crash classification or introduce an escaping
  destination. Consolidation may only add/change `targets` under `~`.
- A non-object JSON reply from the `claude` CLI is a `BrainError` (entries
  fall back to `review`) instead of an unhandled exception.

[Unreleased]: https://github.com/RaajeshwarElagovan/organizer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/RaajeshwarElagovan/organizer/releases/tag/v0.1.0
