# organizer

Report-only file organizer for Linux. Run `organizer` in any directory and it
classifies what is there — **from names, types and metadata only, never file
contents** — and proposes a folder structure plus a per-file action: create a
folder and move into it, move elsewhere, archive, delete, keep, or review.

It **never modifies your files**. At startup the daemon (and the in-process
CLI) puts itself in a [Landlock](https://docs.kernel.org/userspace-api/landlock.html)
sandbox: it can read everything but can only create, change or delete files
under `~/.config/organizer`, `~/.local/share/organizer` and its own socket
dir `$XDG_RUNTIME_DIR/organizer/`. That promise is enforced by the kernel, not just by the code
(`organizer status` shows the active sandbox). Hand the JSON report to a Claude
agent (or read it yourself) when you want the plan applied.

## Brain

Two stages per scan:

1. **Rule engine** (offline, instant): memory rules, duplicate / version-series /
   already-extracted detection, extension & MIME category table, age policy.
2. **Claude** (one `claude -p` call, no tools, no file access): every entry the
   rules could not place with confidence ≥ 0.7 is sent as a batch of facts
   together with the memory and the already-decided structure. Claude returns
   structured JSON decisions plus new rules, which are merged into memory.
   Results are cached, so re-running on an unchanged directory is free.

## Memory that learns without being asked

`~/.config/organizer/memory.json` (schema: `MEMORY-GUIDE.md`).

- **Layer 1, deterministic**: each scan compares last time's proposal with what
  is in the directory now — files that ended up at the proposed target confirm
  a rule; files that went somewhere else, were deleted, or were left in place
  for several scans contradict it. Facts are recorded, confidences nudged.
- **Layer 2, Claude**: when enough evidence accumulates (5 outcomes, or a rule
  contradicted twice) the daemon asks Claude to rewrite the rules, categories
  and notes from the evidence — in a background thread, schema-validated,
  with `memory.json.bak` kept. `organizer learn --dry-run` shows what it would
  change; `organizer learn` forces it.
- The file is plain JSON meant to be edited by an agent; the daemon hot-reloads
  it and rejects invalid edits without losing the last good copy.

## Install

```sh
./install.sh            # -> ~/.local/lib/organizer, ~/.local/bin/organizer, systemd --user unit
cd ~/Downloads && organizer
```

The installer writes only under `$HOME`: `~/.local/lib/organizer` (code),
`~/.local/bin/organizer` (launcher), `~/.config/systemd/user/organizer.service`,
`~/.config/organizer/memory.json` (seeded once, never overwritten) and
`~/.local/share/organizer` (state, reports). It enables and (re)starts the
`systemd --user` unit (`WantedBy=default.target`), so the daemon runs while
you are logged in. Check with `systemctl --user status organizer`. Without a
usable `systemd --user` the CLI simply runs in-process. If you also want the
daemon up at boot before any login, opt in with `ORGANIZER_LINGER=1
./install.sh` (runs `loginctl enable-linger`; undo with
`loginctl disable-linger $USER`). `./uninstall.sh` removes the code, launcher
and unit and keeps your memory and reports; `./uninstall.sh --purge` removes
those too.

Requires python3 (stdlib only) and, for the AI stage, the `claude` CLI logged
in (Claude Code). Without it the tool still works; ambiguous entries show as
`review` with the tentative decision.

## Commands

```
organizer [path] [--all] [--no-ai] [--fresh] [--json]   scan (default command)
organizer explain <file>      why an entry is classified the way it is
organizer history [path]      saved reports for a directory
organizer status              daemon / memory / learning state
organizer learn [--dry-run]   consolidate observed outcomes into memory now
organizer memory [show|validate|path]
organizer reload              force memory reload
organizer --no-daemon ...     run in-process (also the automatic fallback)
```

Reports: `~/.local/share/organizer/reports/<dir>/<timestamp>.json` (+ `latest.json`).
Logs: `journalctl --user -u organizer -f`.

## Applying a report with Claude

```
claude "Read ~/.local/share/organizer/reports/home_me_Downloads/latest.json and apply the
proposals in ~/Downloads: create the folders, move the files, and delete only what is
marked delete. Ask me before deleting anything over 50 MB."
```

The next `organizer` run notices what moved and learns from it.

## Layout

```
organizer/scanner.py     facts: name, ext, mime, stat, name signals, cross-file groups
organizer/classifier.py  stage 1 rules + heuristics + category table + age policy
organizer/brain.py       stage 2 and consolidation via `claude -p --json-schema`
organizer/memory.py      memory schema, validation, atomic save, rule matching, bookkeeping
organizer/engine.py      pipeline, outcome detection, state, reports, consolidation
organizer/sandbox.py     Landlock write restriction (ctypes)
organizer/daemon.py      Unix-socket daemon (systemd --user)
organizer/cli.py         client + in-process fallback
organizer/report.py      terminal rendering
seed/memory.json         starting memory
```
