# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`organizer` is a **report-only** file classifier for Linux: a `systemd --user` daemon plus a thin CLI that scans a directory (names, types, `stat()` — never contents), proposes folders/actions, and keeps a self-learning `memory.json`. It never moves or deletes user files; a Landlock sandbox (`organizer/sandbox.py`) enforces that at the kernel level. Applying a report is delegated to an external agent reading the JSON.

Pure Python 3.8+ **stdlib only** (no `pip`, no third-party deps). ~2 200 lines. Detailed docs already exist and are authoritative — read them before non-trivial changes:

- `ARCHITECTURE.md` — module map, full scan request lifecycle, concurrency model, Claude CLI invocation
- `MEMORY-GUIDE.md` — `memory.json` schema, stage-1 precedence, scanner signal names
- `CONTRIBUTING.md` — ground rules, where to update when adding signals/heuristics/schema keys/daemon commands, release steps
- `SECURITY.md` / `THREAT-MODEL.md` — trust boundaries (must be updated in the same PR as any change crossing them)

## Commands

```sh
# Unit tests (unittest, stdlib; use isolated dirs, never touch ~/.config/organizer)
PYTHONPATH=. python3 -m unittest discover -s tests -v
# Single test
PYTHONPATH=. python3 -m unittest tests.test_claude_boundary.<TestClass>.<test_name> -v

# Syntax check
python3 -m py_compile organizer/*.py

# Run from checkout, in-process, isolated config/data (never hits the installed service)
export ORGANIZER_CONFIG_DIR=/tmp/org-dev/config ORGANIZER_DATA_DIR=/tmp/org-dev/data
PYTHONPATH=. python3 -m organizer.cli --no-daemon <dir> --no-ai    # stage 1 only, no Claude
PYTHONPATH=. python3 -m organizer.cli --no-daemon <dir>            # with Claude
PYTHONPATH=. python3 -m organizer.cli --no-daemon status | explain <file> | memory validate | learn --dry-run

# Dev daemon without touching the installed one
export ORGANIZER_SOCKET=/tmp/org-dev/sock
PYTHONPATH=. python3 -m organizer.daemon &      # logs to stderr
PYTHONPATH=. python3 -m organizer.cli <dir> --json

# Dog-food: installs to ~/.local/lib/organizer and restarts the real service
./install.sh            # ./uninstall.sh to remove
journalctl --user -u organizer -f
```

CI (`.github/workflows/ci.yml`) runs the suite + `.github/scripts/smoke.sh` on Python 3.8/3.12/3.14 under a throw-away `$HOME`, no Claude. The manual smoke fixture (tricky filenames, before/after `ls -la` to prove nothing changed, `sandbox.applied: true`, second run shows `ai.cached > 0`) is in `CONTRIBUTING.md` → *Testing*.

## Hard rules (non-negotiable — see CONTRIBUTING.md)

1. **Report only.** No code may create/rename/move/delete files outside `paths.CONFIG_DIR`, `paths.DATA_DIR` and the dedicated socket dir (`paths.socket_dir()`) — not `/tmp`, not `/dev`; the Landlock allow-list is exactly those three. Do not add an "apply" mode.
2. **Names, not contents.** `scanner.py`/`engine.py` must not `open()` user files. Sole exception: `file -b --mime-type` on extensionless entries, gated by `settings.use_magic`.
3. **stdlib only**, Python 3.8-compatible syntax (`/usr/bin/python3`).
4. **One Claude call per scan**, schema-constrained (`--json-schema`), budget-capped, no tools. No per-file calls, no extra turns.
5. **Sandbox first.** Anything that needs to write outside the allow-list (threads/processes) must be created *before* `sandbox.restrict()` and justified in `THREAT-MODEL.md`. Today the only such thing is `brain._Runner`, the thread that shells out to `claude`.

## Architecture in brief

Pipeline (`engine.run_scan`), shared by daemon and `--no-daemon` CLI fallback:

```
scanner.scan_dir  →  engine.detect_outcomes  →  classifier.classify  →  brain.propose  →  persist
  (facts+signals)     (learning layer 1:        (stage 1: rules,        (stage 2: ONE      (state.json,
                       compare last proposal     category table,         claude -p call     memory.json,
                       vs. what's there now)     age policy)             for conf < 0.7)    reports/)
```

- **Stage-1 precedence** (`classifier.classify_entry`, documented in `MEMORY-GUIDE.md`): dir ignore → redundancy heuristics (dupes, version series, already-extracted) → dir-scoped rules → global rules (highest confidence, first match) → directories → category table → age policy. Anything below `settings.ai_threshold` goes to Claude — set honest confidences, don't inflate to skip stage 2.
- **Claude output boundary** is the security-critical seam: `brain.merge_proposals` / `brain._check_proposal` / `brain.merge_new_rules` / `brain._check_new_rule` and the `memory.clean_*_target` validators constrain what the model can inject. `tests/test_claude_boundary.py` covers it; any change to those functions, `memory.validate`, or `memory.apply_consolidation` needs a test case there. Prompts in `organizer/prompts/*.md` and the `*_SCHEMA` dicts in `brain.py` are part of this boundary.
- **Learning layer 2** (`engine.consolidate`): Claude rewrites rules/categories/targets/notes from `learned.pending` evidence in a background daemon thread; `memory.apply_consolidation` rejects rewrites that drop >50 % of rules or fail `validate()`. Triggered at `learn_batch` (5) pending outcomes, `learn_contradictions` (2), or `organizer learn`.
- **Daemon** (`daemon.py`): `ThreadingUnixStreamServer`, one `RLock` serialising `scan`/`explain`/consolidation-apply; `status`/`history`/`reload` are lock-free. Protocol is one JSON object per line over `AF_UNIX`, 32 MiB cap (`protocol.py`).
- **Memory** (`memory.py`): `memory.json` is a plain document meant to be rewritten whole by humans/agents; `MemoryStore` hot-reloads on mtime and keeps the last good copy if the file is invalid. All writes are tmp + `os.replace` with `.bak`.
- **Paths** (`paths.py`): `ORGANIZER_CONFIG_DIR`, `ORGANIZER_DATA_DIR`, `ORGANIZER_SOCKET` env overrides — always set these when running locally.

## Where to update when changing…

- **Scanner signal**: regex in `scanner.SIGNAL_PATTERNS` + signal list in `MEMORY-GUIDE.md` + optionally a `signals:` category in `seed/memory.json`.
- **Settings key**: `memory.DEFAULT_SETTINGS` (validator whitelists keys) + settings table in `MEMORY-GUIDE.md`.
- **Rule/match key**: `memory.RULE_KEYS`/`MATCH_KEYS`, `memory.rule_matches()`, `brain.LEARN_SCHEMA`/`RULE_SCHEMA`, `MEMORY-GUIDE.md`.
- **Memory schema (incompatible)**: bump `memory.MEMORY_VERSION` and add a migration in `normalize()`.
- **Daemon command**: branch in `daemon.handle`, client fn in `cli.py` (with in-process fallback), README *Commands* block.
- **Anything user-visible**: `CHANGELOG.md` under **Unreleased**. Version lives in `organizer/__init__.py`.

## Style

Short modules with a docstring stating the invariant the file upholds; functions over classes (stateful objects are only `MemoryStore`, the daemon server, `brain._Runner`); `%`-formatting; `log(msg)` callbacks passed down instead of a logging framework; `report.py` output ≤ 100 columns. Commits: `area: what changed`, imperative, ≤ 72 chars; branch from and PR to `main`.
