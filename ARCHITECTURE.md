# Architecture

`organizer` is a small, dependency-free Python 3 program (~2 200 lines) split
into a long-running **daemon** and a thin **CLI client** that share one
pipeline. Everything it knows about your files comes from directory listings
and `stat()`; it never opens file contents, and it never modifies anything
outside its own config/data directories. That last promise is enforced by the
kernel (Landlock) rather than by code review alone — see `SECURITY.md`.

```
                 ┌───────────────────────────── user shell ──────────────────────────────┐
                 │  organizer [path]  ·  explain  ·  history  ·  status  ·  learn  ·  memory │
                 └────────────────────────────────┬─────────────────────────────────────┘
                                                  │ JSON-lines over Unix socket
                                                  │ ($XDG_RUNTIME_DIR/organizer/organizer.sock, 0600 in a 0700 dir)
                                                  ▼
                        ┌────────────── organizerd (systemd --user) ──────────────┐
                        │ daemon.py   request dispatch, RLock, consolidation flag  │
                        │ engine.py   scan → outcomes → classify → Claude → persist │
                        │ scanner.py  classifier.py  brain.py  memory.py           │
                        │ sandbox.py  Landlock applied at startup                  │
                        └───────┬──────────────────────┬──────────────────────────┘
                                │                      │ subprocess (unrestricted helper thread)
                                ▼                      ▼
              ~/.config/organizer/memory.json    claude -p --tools "" --json-schema …
              ~/.local/share/organizer/state.json          (no tools, no file access)
              ~/.local/share/organizer/reports/<dir>/*.json
```

If the daemon is not reachable the CLI runs the same pipeline **in-process**
(`--no-daemon` forces this); it applies the same Landlock sandbox to itself
first.

## Modules

| file | role |
|---|---|
| `organizer/paths.py` | XDG-style locations; `ORGANIZER_CONFIG_DIR`, `ORGANIZER_DATA_DIR`, `ORGANIZER_SOCKET` overrides; `socket_dir()`; `ensure_dirs()` creates the three writable dirs before the sandbox is applied and refuses a squatted socket dir; `dir_slug()` for report folders |
| `organizer/scanner.py` | Collects **facts** per entry: name, stem/ext (multi-ext aware), MIME guess, size, mtime/atime age, mode bits, name **signals** (regex table), and cross-file signals (identical duplicates, revision duplicates, version series, archive already extracted) |
| `organizer/classifier.py` | **Stage 1**: dir ignore list → redundancy heuristics → memory rules → directory handling → category table → age policy. Produces a proposal per entry with `action`, `target`, `confidence`, `reasons`, `rule_id`, `decided_by` |
| `organizer/brain.py` | **Stage 2** and **consolidation**: builds prompts, runs `claude -p` with a JSON schema, merges returned decisions / new rules into memory. Owns the unrestricted `_Runner` thread |
| `organizer/memory.py` | `memory.json` schema, `validate()`, `normalize()`, atomic `save()` with `.bak`, `MemoryStore` hot-reload, `rule_matches()`, rule precedence, outcome bookkeeping, `apply_consolidation()` guards, and the destination validators (`clean_relative_target`, `clean_archive_target`, `clean_move_to_target`, `clean_target_value`) shared by the classifier, brain and validator |
| `organizer/engine.py` | The pipeline (`run_scan`), learning layer 1 (`detect_outcomes`), per-entry AI cache, state persistence, report building/saving, `explain`, `history`, learning layer 2 driver (`consolidate`) |
| `organizer/protocol.py` | One JSON object per line over `AF_UNIX`; 32 MiB message cap; `DaemonUnavailable` |
| `organizer/daemon.py` | `ThreadingUnixStreamServer`; commands `status`, `reload`, `scan`, `explain`, `history`, `learn`; background consolidation thread |
| `organizer/sandbox.py` | Landlock via raw syscalls; `default_allowed()` = config dir, data dir, socket dir — nothing else |
| `organizer/cli.py` | argparse front-end, daemon client with in-process fallback, `memory show/validate/path` |
| `organizer/report.py` | Terminal rendering of a report dict |
| `organizer/prompts/*.md` | System prompts for the propose and learn calls |
| `seed/memory.json` | Initial memory installed on first run |
| `tests/test_claude_boundary.py` | Unit + integration tests for the model-output boundary (`python3 -m unittest discover -s tests`) |
| `tests/test_sandbox.py`, `tests/sandbox_probe.py` | Adversarial tests of the Landlock write boundary in daemon and in-process mode |
| `tests/test_learning.py` | Learning layers 1 and 2 against the real pipeline with Claude mocked |
| `tests/test_lifecycle.py` | `install.sh` / `uninstall.sh` / daemon / CLI lifecycle in a throw-away `$HOME` with `systemctl` shims |
| `systemd/organizer.service` | User unit; `RuntimeDirectory=organizer` (0700) for the socket; `ProtectSystem=strict` + `ReadWritePaths` as a second sandbox layer where supported |
| `install.sh` / `uninstall.sh` | Copy to `~/.local/lib/organizer`, launcher in `~/.local/bin` (`PYTHONSAFEPATH=1`), enable + restart unit; lingering only with `ORGANIZER_LINGER=1`. Uninstall keeps config/data unless `--purge` |

## Request lifecycle for `organizer` (scan)

1. **CLI** resolves `cwd`, sends `{"cmd":"scan","cwd":…,"opts":{no_ai,fresh}}` (`--all` only affects rendering).
2. **Daemon** takes the global `RLock` and calls `engine.run_scan`.
3. `MemoryStore.get()` checks `memory.json`'s mtime and hot-reloads it if it
   changed; an invalid file is reported as a warning and the last good copy is
   used.
4. `scanner.scan_dir` lists the directory (non-recursive), builds facts and
   cross-file signals. The only I/O beyond `stat()` is an optional
   `file -b --mime-type` on extensionless files (`settings.use_magic`).
5. **Learning layer 1** — `detect_outcomes` compares the *previous* proposals
   for this directory (from `state.json`) with what is present now and records
   `confirmed` / `moved_elsewhere:<path>` / `deleted` / `ignored` outcomes,
   nudging the confidence of the rule that made each proposal.
6. **Stage 1** — `classifier.classify` gives every entry a tentative action.
   Entries with `confidence >= settings.ai_threshold` (default 0.7) are
   *decided*.
7. **Stage 2** — undecided entries are first served from the per-entry AI
   cache in `state.json` (keyed by size+mtime, 7-day TTL). The rest go to
   Claude in **one** batched `claude -p` call along with the already-decided
   structure and the memory. Each answer is validated in code
   (`brain._check_proposal`: known action, finite confidence, destination
   checked by the `memory.clean_*_target` helpers and confined to the
   scanned directory / configured targets) before it is overlaid; up to 5
   new specific rules pass `brain._check_new_rule`. Rejected answers turn
   the entry into `review` with the reason. If Claude is unavailable, the
   entries become `review` with the tentative decision shown.
8. Proposals are persisted to `state.json` for next time's outcome detection;
   memory is committed (atomic write + `.bak`).
9. A **report** dict is built (summary, warnings, `structure` tree, sorted
   proposals) and written to `reports/<slug>/<timestamp>.json` and
   `latest.json` (max 20 kept per directory).
10. After replying, the daemon checks `needs_consolidation()` and, if due,
    starts **learning layer 2** in a background thread.

## Stage-1 precedence

Documented in detail in `MEMORY-GUIDE.md`. In short: dir ignore → redundancy
heuristics → dir-scoped rules → global rules (highest confidence first, first
match wins) → directories → category table (signals, ext, mime prefix) → age
policy → below threshold → Claude.

## Learning

Two independent layers, both driven by evidence rather than by asking the user.

- **Layer 1 (deterministic, every scan)** — `engine.detect_outcomes` +
  `memory.adjust_rule`. Adjusts `hits`, `contradictions`, `confidence`
  (+0.05 confirm, −0.10 contradiction, −0.05 ignored, floors 0.5/0.2) and
  appends the outcome to `learned.pending`. Never invents rules.
- **Layer 2 (Claude, when due)** — `engine.consolidate` snapshots memory, asks
  Claude for a full rewrite of `rules`, `categories`, `targets`,
  `claude_notes` (schema-validated by the CLI's `--json-schema`), then
  `memory.apply_consolidation` rejects it if it drops >50 % of rules or fails
  `validate()`. Pending outcomes that arrived during the call are preserved.
  Triggered when `pending >= learn_batch` (5), any rule reaches
  `learn_contradictions` (2), or `organizer learn`.

## Concurrency model

- One `RLock` in the daemon serialises `scan`, `explain`, and the apply phase
  of consolidation. `status`, `history`, `reload` run lock-free.
- Consolidation runs in a daemon thread guarded by a `threading.Event` so only
  one is in flight; the slow Claude call happens on a deep copy **outside**
  the lock, and the lock is taken only to merge the result.
- The `claude` CLI is executed from a dedicated `_Runner` thread created
  *before* `sandbox.restrict()`. Landlock is per-thread and inherited by
  descendants, so this is the only thread that can spawn a process able to
  write to `~/.claude` (which the Claude Code CLI needs for its own state).

## Persistence

| path | owner | contents |
|---|---|---|
| `~/.config/organizer/memory.json` (+`.bak`) | daemon, agents, humans | rules, categories, targets, settings, learned outcomes, notes |
| `~/.local/share/organizer/state.json` | daemon | per-directory last proposals, AI decision cache |
| `~/.local/share/organizer/reports/<slug>/` | daemon | timestamped reports + `latest.json` |
| `$XDG_RUNTIME_DIR/organizer/organizer.sock` (fallback `/tmp/organizer-<uid>/`) | daemon | Unix socket `0600` in a dedicated `0700` dir — the only Landlock-writable root besides config/data |

`memory.json`, `state.json` and `latest.json` are written *tmp + `os.replace`*
so a crash never leaves a half-written copy of a file that is read back;
timestamped report files are written directly. `memory.json` is the only file meant for external editing; the daemon
validates it on reload and keeps serving the last good copy if it is broken.

## Claude integration

`brain.run_claude` shells out to the Claude Code CLI:

```
claude -p --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence
       --output-format json --model <ai_model> --max-turns 1
       --max-budget-usd <ai_max_budget_usd> --json-schema <schema> --system-prompt <prompt>
```

No API key is handled by organizer; it relies on the user's Claude Code
login. `CLAUDE*` environment variables are stripped so project settings,
hooks and MCP servers cannot leak in. The model receives the **absolute path of the scanned directory**, the
names of its existing sub-folders, compact facts per undecided entry
(`name | ext | size | age | mime | signals | tentative`), the already-decided
`name -> action` pairs, and the memory (categories, `targets`, rules,
`claude_notes`, recent outcomes) — never file contents.

## Design constraints worth keeping

- **stdlib only** — no `pip`, installs by copying files.
- **Report only** — the code path that would move/delete files does not
  exist; Landlock backstops that. Do not add an "apply" mode to this process;
  applying is delegated to an agent reading the report.
- **Names, not contents** — the scanner must stay free of `open()` on user
  files (the `file` magic probe is the deliberate exception, off-switchable).
- **One Claude call per scan**, budget-capped, schema-constrained, cached.
- **Memory is a document, not a database** — plain JSON that a human or an
  agent can rewrite in full.
