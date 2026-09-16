# Security

`organizer` reads directory listings on your machine and sends compact
metadata about them to Claude. This document describes what it does and does
not do, the enforcement mechanisms, the assumptions they rest on, and how to
report a problem. The attacker's view is in `THREAT-MODEL.md`.

## Guarantees

1. **Never modifies user files.** There is no code path that renames, moves,
   truncates or deletes anything outside organizer's own directories. On top
   of that the process applies a **Landlock** ruleset to itself at startup
   (`organizer/sandbox.py`) that lets it read everything but only
   create/modify/delete beneath:
   - `~/.config/organizer`
   - `~/.local/share/organizer`
   - its own socket directory `$XDG_RUNTIME_DIR/organizer/` (fallback
     `/tmp/organizer-<uid>/`), mode 0700

   Nothing else — not `/tmp`, not `/dev`, not the rest of `$XDG_RUNTIME_DIR`.

   `PR_SET_NO_NEW_PRIVS` is set, so the restriction cannot be escaped via
   setuid binaries. `organizer status` shows `sandbox.applied`, the Landlock
   ABI and the allowed paths.
2. **Never reads file contents.** The scanner uses `os.scandir` and `stat()`
   only. The single exception is `file -b --mime-type` for *extensionless*
   regular files, which reads magic bytes; disable with
   `settings.use_magic: false`.
3. **Claude gets no tools and no file access.** The CLI is invoked with
   `--tools "" --setting-sources "" --strict-mcp-config --max-turns 1`, a
   JSON schema, a spend cap (`ai_max_budget_usd`, default $0.10) and a
   timeout (`ai_timeout_s`, default 120 s). Only names, extensions, sizes,
   ages, MIME guesses, derived signals and the memory file are sent.
4. **Memory edits are validated.** `memory.json` is schema-checked on every
   load and before every save; an invalid file is never loaded (the last good
   copy stays in use) and a `.bak` is written before each daemon-side save.
   A Claude consolidation is rejected if it fails validation or would drop
   more than half the rules.
5. **Local-only IPC.** The daemon listens on a Unix socket created with mode
   `0600` (umask `077` from the moment it exists) inside its 0700 socket
   directory. No TCP, no network listeners. At startup a leftover socket file
   is probed: a dead one is removed, a live one (another daemon) makes the new
   process exit instead of hijacking the path; on shutdown the daemon unlinks
   only the socket it bound itself.

## Defence in depth

| layer | mechanism | where |
|---|---|---|
| kernel | Landlock write restriction + `no_new_privs` | `sandbox.py`, applied by daemon and in-process CLI |
| systemd | `ProtectSystem=strict`, `ReadWritePaths=…`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `RuntimeDirectory=organizer` (0700) | `systemd/organizer.service` (mount sandboxing may be unavailable for user units on some distros — see below; `NoNewPrivileges` and `RuntimeDirectory` always apply; Landlock still applies) |
| process | Claude CLI runs with no tools/MCP/hooks, `CLAUDE*` env stripped, spend + turn + time caps | `brain.run_claude` |
| data | JSON schema validation of model output and of memory; atomic writes; backups | `brain.py`, `memory.py` |
| code | scanner has no `open()` on user files; engine writes only under `paths.*` | `scanner.py`, `engine.py` |

## What the sandbox is verified to block

`tests/test_sandbox.py` starts a fresh process through the real
`--no-daemon` startup and through the real daemon, then attacks a victim
directory outside the allow-list from the restricted thread, from a thread
started afterwards (the consolidation path) and from a subprocess. The
kernel denied all of the following with `EACCES`/`EXDEV`, and the victim tree
was byte-identical afterwards (lstat + content hash):

- writes: `open(w/a/r+)`, `O_WRONLY`, `O_RDWR`, `O_TRUNC`, `truncate()`;
- creation: files, `O_CREAT|O_EXCL`, `mkdir`, nested `makedirs`, symlinks,
  hard links (including hard-linking a victim file *into* the allowed dir),
  FIFOs, `O_TMPFILE`, `mkstemp`/`mkdtemp`/`NamedTemporaryFile(dir=victim)`;
- deletion: `unlink`, `rmdir`, `rmtree`, unlinking existing symlinks;
- rename: within the victim, `os.replace`, into a proposed sub-folder, victim
  → allowed dir (exfiltration), allowed dir → victim (injection);
- symlink escapes: a symlink placed in the allowed dir that points at the
  victim, a two-level symlink chain, a directory swapped for a symlink after
  creation, existing relative and absolute symlinks inside the victim;
- traversal: `allowed/../victim`, `openat(dirfd, "../victim")`, `chdir` +
  relative path;
- absolute paths: `$HOME`, `~/.bashrc`, `~/.ssh`, `/etc`, `/var/tmp`;
- organizer's own write patterns aimed at the scanned directory (report
  file, `_archive/`, `Documents/Finance`, moving or deleting an entry,
  `x.tmp` + `os.replace`);
- shell subprocesses (`touch`, `rm`, `mv`, `mkdir -p`) started from the
  restricted thread.

Reads (`open(r)`, `listdir`, `file --mime-type`) keep working.

Also verified blocked since the allow-list was narrowed: creating files or
directories anywhere in `/tmp` (including a scanned directory under `/tmp`
and the legacy `/tmp/organizer-<uid>.sock` path), `tempfile` defaults
(`NamedTemporaryFile()`, `mkdtemp()` fail with "No usable temporary
directory"), `/dev/shm`, opening `/dev/null` or `/dev/tty` for writing, and
anything in `$XDG_RUNTIME_DIR` outside `organizer/` — its root, the legacy
`organizer.sock`, the `bus` socket, and the `doc/` (document portal),
`gvfs/`, `keyring/`, `gnupg/`, `pulse/`, `systemd/` sub-trees. The
legitimate writes (`memory.json` tmp+replace and `.bak`, `state.json`,
`reports/<slug>/…` create and prune, socket bind/chmod/unlink) are asserted
to still succeed in the same run.

## What the sandbox does not cover (verified)

- **Metadata.** Landlock's filesystem rights do not include `chmod`,
  `chown`, `utime` or xattrs; those calls succeed on any file. organizer
  never issues them.
- **The `_Runner` thread** (below).
- **`truncate(2)` on Landlock ABI < 3** (kernels before 6.2) and cross-directory
  `rename`/`link` refinements on ABI < 2 (before 5.19). `open(O_TRUNC)` is
  covered on every ABI.

## The pre-Landlock `_Runner` thread — exact boundary

`brain.start_runner()` creates one daemon thread *before* `sandbox.restrict()`
in both the daemon (`daemon.main`) and the in-process CLI
(`cli._fallback_context`), so it exists even for `--no-ai` runs. Landlock
domains are per-thread and inherited only by threads/processes created
afterwards, so this thread and anything it spawns are **not confined**
(`test_pre_landlock_runner_thread_is_unrestricted` proves it can create and
delete files outside the allow-list, and so can a subprocess it starts).

What actually reaches it:

- `_Runner.call(fn, *args)` is a generic executor, but the only caller in
  the code base is `brain.run_claude`, which submits `_exec` — a
  `subprocess.run` of the `claude` binary found on `PATH` (or
  `~/.local/bin/claude`) with `--tools "" --setting-sources ""
  --strict-mcp-config --no-session-persistence --max-turns 1`, a JSON schema,
  a budget cap, `cwd=$HOME`, and an environment with every `CLAUDE*`
  variable removed.
- Its argv is built from package constants plus three values from
  `memory.json` `settings` (`ai_model`, `ai_max_budget_usd`, `ai_timeout_s`).
  Claude output can never reach argv: consolidation and rule merging never
  write `settings`.
- The prompt (file names, metadata, memory) goes to the subprocess on
  stdin; its stdout is parsed as JSON and validated (see above).

So the boundary is: *organizer trusts the installed `claude` CLI as much as
any binary on `PATH`, and relies on the CLI honouring `--tools ""` so that a
prompt-injected model cannot act on the filesystem.* Nothing an attacker
controls (file names, model output, cached decisions) selects what the
runner executes. The residual risk is a tampered or buggy `claude` binary,
which is outside organizer's threat model.

## When Landlock is unavailable — current behaviour

`sandbox.restrict()` returns `False` (and `status()` carries the reason) if
the machine is not `x86_64`/`aarch64`, the kernel has no Landlock (< 5.13,
or `landlock` missing from the `lsm=` list → `ENOSYS`/`EOPNOTSUPP`), or the
ruleset cannot be built. In every case organizer **continues without a
kernel guard**: the daemon logs `WARNING: no kernel sandbox (...)`, the CLI
prints `organizer: no kernel sandbox (...)` to stderr, and `organizer status`
shows `sandbox: NONE (...)`. `tests/test_sandbox.py::LandlockUnavailable`
pins this fail-open behaviour so any change is deliberate. Reports do not
currently record whether the sandbox was active.

## Known limitations and assumptions

- **Landlock availability.** Requires Linux ≥ 5.13 (ABI 1); `truncate`
  is covered from ABI 3 (≥ 6.2), `refer` (cross-directory rename/link) from
  ABI 2 (≥ 5.19). On kernels without Landlock, or on architectures other
  than `x86_64`/`aarch64`, the sandbox is **not** applied; the tool logs a
  warning and `organizer status` reports the error. The read-only behaviour
  then relies on the code alone.
- **The Claude helper thread is unrestricted.** Landlock is per-thread, so a
  helper thread started *before* `restrict()` is used to spawn the `claude`
  CLI (which must write its own state under `~/.claude`). That subprocess is
  launched with no tools, but it is not Landlock-confined. A compromised
  `claude` binary on `PATH` is therefore outside the sandbox.
- **Data leaves the machine.** File and folder *names*, sizes, ages, MIME
  guesses and the contents of `memory.json` (including `claude_notes` and
  recent outcomes) are sent to Anthropic via the Claude Code CLI. File names
  can themselves be sensitive. Use `--no-ai` or `ai_enabled: false` for
  directories where that is not acceptable; the rule engine still works.
- **Reports and memory are plaintext** under `~/.local/share/organizer` and
  `~/.config/organizer`, readable by the user (and root). They contain file
  names and proposed actions, not contents.
- **Anyone with your UID can talk to the daemon.** The socket is `0600`, so
  other users cannot, but any process running as you can request scans of
  any directory you can read. That is the same trust boundary as your shell.
- **The unit's mount sandboxing may be inert.** It asks for `PrivateTmp=yes`
  and `ProtectSystem=strict`, but on this reference machine (Ubuntu,
  `apparmor_restrict_unprivileged_userns=1`) the daemon runs in the host
  mount namespace (verified: `/proc/<pid>/ns/mnt` equals the login shell's),
  so `ProtectSystem`, `PrivateTmp` and `ReadWritePaths` are **not** in
  effect — systemd silently drops them when an unprivileged user manager
  cannot create a mount namespace. `NoNewPrivileges=yes` and
  `RuntimeDirectory=organizer` (0700, removed when the unit stops) do
  apply. Landlock is the only enforced write restriction. Do not count on
  the unit's mount sandboxing.
- **The default socket dir is guarded against squatting.** Because it is a
  Landlock-writable root, `paths.ensure_dirs()` refuses to use
  `$XDG_RUNTIME_DIR/organizer` or `/tmp/organizer-<uid>` unless it is a real
  directory owned by the current uid with mode 0700 (a symlink or a
  world-accessible directory planted there aborts startup). The systemd unit
  pre-creates it via `RuntimeDirectory=organizer` / `RuntimeDirectoryMode=0700`.
  An explicit `ORGANIZER_SOCKET` override is trusted as the user's choice.
- **The tool proposes deletions.** It never executes them, but a downstream
  agent asked to "apply the report" will. Review `delete` entries — the
  README's suggested prompt asks Claude to confirm before deleting anything
  over 50 MB, and `delete_policy: conservative` (default) prefers archiving.

## Prompt-injection surface

File names are attacker-controlled input that reach the model. Mitigations:

- The model's output is constrained by a JSON schema *and* re-validated in
  code (`brain._check_proposal`, `brain._check_new_rule`) — the schema is
  treated as a hint, not a guarantee. Proposals for names not in the
  undecided list are dropped; a second proposal for the same name is ignored.
- Every destination goes through `memory.clean_relative_target` /
  `clean_archive_target` / `clean_move_to_target`: `move` and `archive`
  targets must be relative, free of `..`, `~`, drive letters and control
  characters, with `\` normalised to `/`, and must `realpath` to a location
  under the scanned directory (a symlinked sub-folder pointing elsewhere is
  rejected); `archive` must stay under `_archive/`; `move-to` must be a
  configured `targets` name or a `~/…` path under the home directory.
  `delete`/`keep`/`review` never carry a target.
- `action` must be one of the six known actions; `confidence` must be a
  finite number (clamped to 0–1; strings, booleans, NaN rejected); `reason`
  and notes are whitespace/control-character-collapsed and length-capped.
- A decision that fails any check turns the entry into `review` with the
  rejection reason in the report, is never written to the decision cache, and
  a cached decision that fails on replay is purged.
- New rules are capped at 5 per call, deduplicated, clamped to confidence
  0.5–0.95; over-broad globs (`*`, `*.*`, `*.pdf`), non-string or
  non-compiling patterns, traversal in `category`/`category_dir`, and invalid
  targets are rejected. The memory is validated again before it is saved.
- Consolidation rewrites may keep existing `targets` verbatim but may only
  add or change destinations under `~`.
- Model-written rules are glob-only. A `new_rules` item with a `regex` is
  rejected outright, and a consolidation rewrite that adds a regex rule or
  edits an existing pattern is rejected whole; `regex` remains available to
  hand-edited rules, with the same compile/length/backtracking checks.
- Nothing the model returns is executed; it only changes what the report
  *recommends*.

A malicious file name can still bias a recommendation. The report is meant to
be read (by you or by an agent with its own confirmation step) before any
action is taken.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Email the
maintainer listed in `git log` (Raajeshwar Elagovan) with:

- a description of the issue and its impact,
- steps or a minimal setup to reproduce,
- kernel version and `organizer status` output if the sandbox is involved.

You should get an acknowledgement within a few days. Fixes are released as a
new version noted in `CHANGELOG.md`; credit is given unless you prefer
otherwise.

## Verifying the sandbox yourself

```sh
organizer status                       # sandbox.applied should be true, with the allowed paths
# From a scan, try to make the daemon misbehave — it cannot:
python3 - <<'PY'
from organizer import sandbox, paths
sandbox.restrict()
open('/home/you/should-not-exist', 'w')   # -> PermissionError
open(paths.DATA_DIR + '/ok', 'w')         # -> works
PY
```
