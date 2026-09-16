# organizer

Report-only file organizer for Linux. Run `organizer` in any directory and it
classifies what is there — **from names, types and metadata only, never file
contents** — and proposes a folder structure plus a per-file action: create a
folder and move into it, move elsewhere, archive, delete, keep, or review.

It **never modifies your files** — there is no code path that moves, renames
or deletes anything outside its own directories. On top of that, at startup
the daemon (and the in-process CLI) puts itself in a
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) sandbox: it can
read everything but can only create, change or delete files under
`~/.config/organizer`, `~/.local/share/organizer` and its own socket dir
`$XDG_RUNTIME_DIR/organizer/`. On a Linux kernel ≥ 5.13 with Landlock enabled
(x86_64 or aarch64 — most distributions released since 2022) that promise is
enforced by the kernel, not just by the code; `organizer status` shows whether
the sandbox is active. On older kernels organizer still runs, prints
`no kernel sandbox` on stderr, and relies on the code alone. Hand the JSON
report to a Claude agent (or read it yourself) when you want the plan applied.

"Never file contents" has one exception: for files *without an extension* it
runs `file --mime-type`, which reads a few magic bytes (`settings.use_magic:
false` turns it off). The full security model, including what is *not*
covered, is in [SECURITY.md](SECURITY.md).

## What leaves your machine, and what it costs

The AI stage is **on by default** and uses your Claude Code login. For every
scan with entries the rules could not decide, one `claude -p` call sends:

- the absolute path of the scanned directory and the names of its sub-folders;
- for each undecided entry: name, extension, size, age, MIME guess and derived
  signals — never contents; already-decided entries as `name -> action`;
- your whole `memory.json` (rules, categories, `targets`, notes, recent
  outcomes).

File names alone can be sensitive. Each call is billed to your Claude Code
account (typically a few cents; capped by `ai_max_budget_usd`, default $0.10,
per call). Decisions are cached per entry for 7 days, so re-scanning an
unchanged directory is free. To keep everything local: `organizer --no-ai`
for one run, or `"ai_enabled": false` in `memory.json` — the rule engine
still works and undecided entries show as `review`.

## Brain

Two stages per scan:

1. **Rule engine** (offline, instant): memory rules, duplicate / version-series /
   already-extracted detection, extension & MIME category table, age policy.
   All of it works on names and `stat()` only: a "duplicate" is `X (1).ext`
   next to an `X.ext` **of the same size** (contents are never compared); the
   age policy proposes `archive` for anything older than `archive_after_days`
   (default 180) and `delete` for disposable files older than
   `stale_after_days` (365) — tune both in `memory.json` before pointing it at
   a working folder rather than a downloads folder.
2. **Claude** (one `claude -p` call, no tools, no file access): every entry the
   rules could not place with confidence ≥ 0.7 is sent as a batch of facts
   together with the memory and the already-decided structure. Claude returns
   structured JSON decisions plus new rules, which are merged into memory
   after validation (see `SECURITY.md`, *Prompt-injection surface*).

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

Requirements: Linux, `/usr/bin/python3` ≥ 3.8 (stdlib only, nothing to
`pip install`; tested on 3.8–3.14), optionally `systemd --user` for the
daemon, optionally `file` for magic-byte MIME detection, and the `claude`
CLI logged in for the AI stage. `claude` is external and optional in both
install methods — it is never bundled.

### Source installation (per user)

```sh
git clone https://github.com/RaajeshwarElagovan/organizer.git && cd organizer
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

### Debian / Ubuntu package (system-wide code, per-user data)

```sh
sudo dpkg -i organizer_0.1.0-1_all.deb     # or: sudo apt install ./organizer_0.1.0-1_all.deb
systemctl --user enable --now organizer    # per user, optional: start the daemon for your login session
cd ~/Downloads && organizer
```

The package depends only on `python3 (>= 3.8)` (`file` and `systemd` are
Recommends) and installs the code to `/usr/lib/python3/dist-packages/organizer`,
the launcher to `/usr/bin/organizer`, the immutable starting memory to
`/usr/share/organizer/seed/memory.json`, the user unit to
`/usr/lib/systemd/user/organizer.service` and the docs to
`/usr/share/doc/organizer/`. It does **not** enable or start the daemon for
anyone, does not enable lingering, and never creates, changes or deletes
anything under a user's home: each user's `~/.config/organizer/memory.json`
is seeded from the system seed on that user's first run, and
`~/.config/organizer` / `~/.local/share/organizer` survive `dpkg -r` and
`dpkg -P` (delete them yourself if you want them gone). Without the unit
enabled the CLI runs in-process, exactly like the source install without
`systemd --user`. The only maintainer scripts are the standard Python
byte-compile hooks (`py3compile` / `py3clean` on
`/usr/lib/python3/dist-packages/organizer`). The source install and the
package can coexist; `~/.local/bin` usually precedes `/usr/bin` on `PATH`, so
run `./uninstall.sh` first if you switch.

Release artifacts (`organizer_0.1.0-1_all.deb`, `organizer-0.1.0.tar.gz`) are
built with `./build-dist.sh` into `dist/` — see `CONTRIBUTING.md` → *Releasing*.

Without the `claude` CLI the tool still works; ambiguous entries show as
`review` with the tentative decision. Note that the **daemon** looks for
`claude` on its own `PATH` (`~/.local/bin`, `/usr/local/bin`, `/usr/bin`,
`/bin`): a `claude` installed via npm/nvm under `~/.nvm` or `~/.npm-global`
works with `organizer --no-daemon` but not through the service —
`organizer status` then shows `claude=NOT FOUND`. Either install the native
`claude` into `~/.local/bin`, or `systemctl --user edit organizer` and add
`[Service]` / `Environment=PATH=/path/to/bin:…`.

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
organizer --version | --help
```

`--json` and `--no-daemon` may come before or after the subcommand.

Reports: `~/.local/share/organizer/reports/<dir>/<timestamp>.json` (+ `latest.json`).
Logs: `journalctl --user -u organizer -f`.

## Applying a report with Claude

```
claude "Read ~/.local/share/organizer/reports/home_me_Downloads/latest.json and apply the
proposals in ~/Downloads: create the folders and move the files. List everything marked
delete and ask me before deleting any of it."
```

Treat `delete` proposals as suggestions: "identical duplicate" means same
name pattern and size, not verified identical contents, and nothing stops
the rules and Claude from marking every copy of a file for deletion. The
next `organizer` run notices what moved and learns from it.

## Limitations

- Scans one directory, non-recursively; sub-folders are entries, not descended.
- Everything is inferred from names and metadata, so it can be wrong; every
  proposal carries its confidence and reasons, and `organizer explain <file>`
  shows the chain.
- The kernel sandbox needs Landlock (Linux ≥ 5.13, x86_64/aarch64); elsewhere
  it runs without it and says so.
- The `claude` process itself is not sandboxed (it needs `~/.claude`); it is
  run with no tools, so the model cannot touch files — see `SECURITY.md`.
- Linux only.

## Security and licence

Security model and residual risks: [SECURITY.md](SECURITY.md) and
[THREAT-MODEL.md](THREAT-MODEL.md); please report vulnerabilities as
described there rather than in a public issue. Licence: MIT ([LICENSE](LICENSE)).

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
systemd/organizer.service  user unit for the source install (debian/systemd-user/ has the packaged one)
debian/                  Debian packaging (dpkg-buildpackage -b); build-dist.sh builds dist/
```
