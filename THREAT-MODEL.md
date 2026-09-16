# Threat model

This is the attacker's-eye view of `organizer`. `SECURITY.md` lists the
guarantees and controls; this document explains *why* those controls exist by
walking through assets, trust boundaries, actors and concrete threats, and
records what is deliberately out of scope.

## What we protect

| asset | why it matters |
|---|---|
| **A1 — the user's files** | The tool's whole value proposition is "it will never touch them". Any unintended write, move or delete is a critical failure. |
| **A2 — file contents** | Must never be read (beyond magic bytes on extensionless files) and never leave the machine. |
| **A3 — file/folder names and metadata** | Sent to Claude; names can reveal projects, clients, health, finances. |
| **A4 — `memory.json`** | Drives every future recommendation. A poisoned memory produces bad (e.g. mass-`delete`) proposals. |
| **A5 — reports** (`reports/<dir>/*.json`) | Consumed by a downstream agent that *will* act on them. Integrity matters more than confidentiality. |
| **A6 — the user's Claude Code login / spend** | organizer can trigger paid model calls. |
| **A7 — the daemon's availability** | Low value; a hung daemon degrades to the in-process fallback. |

## Trust boundaries

```
 [file names on disk] ──► scanner ──► classifier ──► brain ══► claude CLI ══► Anthropic API
        untrusted          trusted     trusted       trusted    semi-trusted    external
                                                        ▲
 [memory.json] ─────── validate() ──────────────────────┘
   semi-trusted (edited by humans, agents, and Claude rewrites)

 [CLI client] ──► Unix socket 0600 ──► daemon        same UID = same trust as the shell
 [report JSON] ──► downstream agent                  organizer's output becomes someone else's input
```

- **B1 file system → process**: names are attacker-controlled. Anything
  downloaded, extracted or synced can carry a hostile name.
- **B2 process → model**: metadata crosses to a third party.
- **B3 model → memory/report**: model output is untrusted structured data.
- **B4 memory file → process**: edited out-of-band by people and agents.
- **B5 report → applying agent**: organizer's recommendations are executed by
  a different, more powerful process.
- **B6 client → daemon**: local IPC, same user.

## Actors

| actor | capability | motivation |
|---|---|---|
| **T1 remote content author** | Controls file names that land in `~/Downloads` (attachments, zips, git clones, cloud sync). | Prompt-inject the model, get a valuable file marked `delete`, or exfiltrate names. |
| **T2 malicious/buggy memory editor** | Writes `memory.json` (a rogue agent, a bad merge, a typo). | Blanket rules such as `*.pdf → delete`, `move-to` outside `~`. |
| **T3 compromised model output** | Anthropic-side error, jailbroken by T1, or a tampered `claude` binary. | Same as T2 but automated every scan. |
| **T4 other local process, same UID** | Full access already (can read/write all user files directly). | Not meaningfully elevated by organizer; noted for completeness. |
| **T5 other local user** | Cannot connect to the `0600` socket; cannot read `~/.config/organizer` if home is `0700`. | Learn file names from reports. |
| **T6 organizer itself (bugs)** | Any code path in this repo. | Unintended writes (A1), unintended reads (A2). |

## Threats and mitigations

### A1 — unintended modification of user files

| threat | mitigation | residual risk |
|---|---|---|
| A bug in engine/scanner writes or unlinks a user file (T6). | No such code exists; every write goes through `paths.*`. **Landlock** denies write/create/delete/truncate/refer outside the allow-list at the kernel; `no_new_privs` prevents setuid escape. Applied in both daemon and `--no-daemon` mode and verified adversarially by `tests/test_sandbox.py` (70+ attacks incl. symlink chains, traversal, `openat`, rename in/out, `O_TMPFILE`, subprocesses; victim tree byte-identical). systemd `ProtectSystem=strict` is requested but not effective on the reference machine. | Kernels without Landlock or non-x86_64/aarch64 hosts run without the kernel layer (fail-open; logged, visible in `status`, not recorded in reports). The allow-list is exactly config dir, data dir and `$XDG_RUNTIME_DIR/organizer/` (or `/tmp/organizer-<uid>/`); `/tmp`, `/dev` and the rest of the runtime dir (portal/gvfs mounts, dbus, keyrings) are verified denied. Metadata (`chmod`/`utime`/xattr) is outside Landlock's scope. |
| The Claude subprocess writes user files (T3). | Started with `--tools ""`, `--strict-mcp-config`, `--setting-sources ""`, `--max-turns 1`; `CLAUDE*` env stripped so no hooks/MCP/CLAUDE.md are loaded. | The helper thread that spawns it is **not** Landlock-restricted (the CLI needs `~/.claude`). A tampered `claude` binary can do anything the user can. Out of scope: we trust the installed CLI as we trust any binary on `PATH`. |
| Downstream agent applies a bad report (B5). | Reports carry `"report_only": true` and per-entry `reasons`/`confidence`; `delete_policy: conservative` archives instead of deleting superseded builds; README prompt asks for confirmation before large deletes. | Ultimately the applying agent's responsibility. organizer cannot stop someone from `rm -rf`-ing on its advice. |

### A2 — reading file contents

| threat | mitigation | residual risk |
|---|---|---|
| Scanner opens a file. | Design rule: no `open()` on user paths in `scanner.py`. Only `stat` and `scandir`. | Regressions in future code; keep this a review item (see `CONTRIBUTING.md`). |
| `file --mime-type` on extensionless entries reads bytes. | Reads a small header only; gated by `settings.use_magic`; 5 s timeout. | Magic bytes of secrets (e.g. a PEM key) are read locally but never sent — only the resulting MIME string is. |

### A3 — metadata leakage to the model

| threat | mitigation | residual risk |
|---|---|---|
| Sensitive file names sent to Anthropic. | Only undecided entries (below `ai_threshold`) are sent, as compact fact lines; already-decided names are sent as `name -> action` for structural consistency. `--no-ai`, `ai_enabled: false` per memory, or `dir_overrides[...].ignore` keep names local. Cache avoids re-sending unchanged entries for 7 days. | The whole `memory.json` (rules, notes, last 20 confirmed outcomes) is included in every call; names that made it into rules are therefore resent. Users needing zero egress must disable AI. |
| Reports/memory readable on disk. | Written in the user's own XDG dirs; nothing world-readable is created by organizer (umask applies). | Depends on the user's home-directory permissions. |

### A4 — memory poisoning

| threat | mitigation | residual risk |
|---|---|---|
| T1 injects instructions via a file name, model emits broad `delete` rule. | Output constrained by JSON schema; new rules capped at 5/call, confidence clamped to 0.5–0.95, globs `*`, `*.*`, `*.xxx` rejected; proposals for unlisted names dropped; `confidence` must be a finite number; `category` keys and `category_dir` validated as relative paths; rule targets validated per action (`brain._check_new_rule`); a memory that fails `validate()` after a merge is never saved. Layer-1 learning lowers confidence of contradicted rules automatically. | A specific but wrong rule (e.g. `Invoice-*.pdf → delete`) can still be created. It surfaces in the report as `delete` with reason `claude: …` and can be reverted in `memory.json`; `hits`/`contradictions` make it visible. |
| Consolidation rewrite wipes memory (T3). | `apply_consolidation` rejects rewrites that are not well-shaped, drop >50 % of rules, fail validation, or introduce/change a `targets` value outside `~`; `.bak` kept; rewrite happens on a snapshot, merged under lock, pending outcomes gathered meanwhile are preserved. | A rewrite that keeps ≥50 % of rules but subtly degrades them is accepted; `organizer learn --dry-run` and `last_consolidation.rationale` exist for auditing. |
| Human/agent edit breaks the file (T2). | `validate()` on load; invalid file → last good copy kept, error shown in `status` and as a scan warning; `settings` keys are whitelisted. | Semantically valid but harmful rules are accepted — memory is trusted at the level of the user's own shell. |
| Race between agent edit and daemon save. | Hot-reload by (mtime, size, inode) before each request; saves are atomic (`tmp` + `os.replace`); an edit that fails `validate()` is never overwritten — the daemon keeps working from its last good copy, warns on every scan, and refuses consolidation until the file is repaired; guide tells agents not to edit `learned.pending` during consolidation. | A *valid* edit still loses to a daemon save that follows it (last writer wins; the edit is in `.bak`). An edit that keeps the same mtime, size and inode as the daemon's own write is not noticed until `organizer reload`. |

### A5 — report integrity

| threat | mitigation | residual risk |
|---|---|---|
| Report tampered between scan and apply (T4). | Same-UID actor can already do anything; out of scope. | — |
| Path traversal via model-supplied `target`. | Every Claude decision passes `brain._check_proposal` → `brain.validate_target` → the `memory.clean_*_target` helpers: `move` must be a relative path (no `/`, `\`, `C:`, `~`, `..`, control chars; `\` normalised to `/`) whose `realpath` stays under the scanned directory (catches symlinked sub-folders); `archive` must be relative and under `_archive/`; `move-to` must be a configured `targets` name or a `~/…` path under the home directory; `delete`/`keep`/`review` get no target. Rejected decisions become `review` with the reason shown and are never cached. The same helpers run in `validate()` for memory edits and consolidation rewrites; because a rule or category is validated without knowing the directory it will be applied to, `engine.run_scan` re-checks every stage-1 `move`/`archive` destination against the scanned directory's `realpath` and turns an escaping one into `review` — so a learned rule cannot reintroduce a symlink escape that a direct decision would have been refused. | An applying agent should still treat targets as untrusted input. A user-configured `targets` value outside `~` is honoured as-is (it is the user's own allow-list). |

### A6 — cost / credential abuse

| threat | mitigation | residual risk |
|---|---|---|
| Pattern-matching denial of service (T1 names a file, T3 supplies a rule). | Regexes are ≤ 200 chars and must compile; a quantified group ending in a quantifier (`(a+)+`, `(\w+\s?)*`, `(.+){2,}`) is refused by `memory.match_problems` on every path (hand edit, `new_rules`, consolidation). Globs are ≤ 128 chars; on Python ≥ 3.12 `fnmatch` compiles `*` to an atomic group, so globs cannot backtrack. | **Known limitation.** The regex check is a heuristic for the textbook form only: `(a|aa)+`, `(.*a){20}` and similar are accepted and hang the scan thread on a pathological 255-character name (measured: seconds to unbounded). On Python < 3.12 a glob with ≥ 3 `*` costs seconds per pathological name. The stdlib has no regex timeout; the daemon serialises scans, so one such rule blocks all scans until restart. Mitigation: regex rules are **user-only by policy, enforced in code** — `brain._check_new_rule` refuses any `new_rules` item carrying a `regex` and `memory.apply_consolidation` refuses a rewrite that adds a regex rule or changes an existing pattern (an existing user regex may only be kept verbatim or dropped). So a pathological regex can only come from T3 (a hand edit), and `organizer memory` lists every rule for review. |
| Runaway model spend. | One batched call per scan; `--max-budget-usd` (0.10), `--max-turns 1`, timeout 120 s; per-entry decision cache; consolidation only when due and single-flight. | A scan of a huge directory with many undecided entries is one large prompt; the budget cap bounds cost per call, not per day. |
| Credential handling. | organizer never sees an API key; it inherits the Claude Code login. | Whoever can run `claude` as the user can spend as the user — unchanged by organizer. |

### A7 — availability

| threat | mitigation |
|---|---|
| Hostile client sends a huge message. | 32 MiB cap in `protocol.read_message`; connection dropped. |
| Daemon crash or hang. | Per-request exception handling keeps the server alive; systemd `Restart=on-failure`; CLI falls back to in-process mode automatically. A stale socket file left by a crash is removed at the next start; a *live* socket (a second daemon, e.g. one started by hand next to the service) makes the newcomer exit instead of taking over the path, and each daemon unlinks only the socket it bound. |
| Symlink loops / huge directories. | Scan is non-recursive; symlinks are not followed for directory detection (`follow_symlinks=False`); `_find_elsewhere` is depth-2 only. |
| Another local user squats `/tmp/organizer-<uid>` (T5) so the Landlock rule lands on a path of their choosing. | `paths.ensure_dirs()` refuses a default socket dir that is not a real directory owned by this uid with mode 0700; startup aborts with a one-line error instead of sandboxing the wrong tree. `$XDG_RUNTIME_DIR` is only used when it is a directory owned by this uid (an inherited one from `sudo -u`/`su` falls back to `/tmp/organizer-<uid>`). Under systemd the dir is pre-created by `RuntimeDirectory=`. |

## Explicitly out of scope

- **Same-UID attackers (T4).** Any process running as the user can bypass
  organizer entirely; the socket and files are protected only against other
  users.
- **Root / kernel compromise.**
- **A tampered `claude` CLI or Python interpreter.** Trusted like any other
  binary on `PATH`.
- **Confidentiality against Anthropic.** Sending metadata to the model is the
  feature; users who cannot accept that run with AI disabled.
- **What the applying agent does with the report.** organizer is advisory.
- **Windows/macOS.** Landlock is Linux-only; the tool targets Linux.

## Hardening backlog

Items identified by this analysis that are not yet implemented:

1. **Make fail-open visible and optional**: record `sandbox.applied` in every
   report so an applying agent knows whether the kernel guard was active;
   add an opt-in fail-closed switch (e.g. `ORGANIZER_REQUIRE_SANDBOX=1`) and
   set it in the systemd unit, whose own mount sandboxing is not effective on
   Ubuntu with `apparmor_restrict_unprivileged_userns=1`.
2. Drop the `x86_64`/`aarch64` gate in `sandbox.restrict()`: Landlock syscall
   numbers 444–446 are identical on every Linux architecture, so other
   architectures currently get *no* sandbox for no reason.
3. Optional redaction/allow-list of directories whose names must never be
   sent to the model (today: `--no-ai` per run or `ignore` per directory).
4. Landlock-confine the `claude` subprocess with an allow-list of `~/.claude`
   only, instead of leaving the helper thread unrestricted.
5. Daily spend ledger in `state.json` with a configurable cap.
6. Sign or hash reports so an applying agent can detect modification.
7. Validate rules inside `dir_overrides` with the same checks as top-level
   rules (they are user-only today; consolidation does not write them).

Done: model-supplied `target` validation for `move`/`archive`/`move-to`
(absolute, `..`, `~`, control characters, symlink escape), enforced in code
rather than by the prompt — covered by `tests/test_claude_boundary.py`.
Done: adversarial verification of the Landlock boundary in both daemon and
in-process modes — `tests/test_sandbox.py` + `tests/sandbox_probe.py`.
Done: allow-list narrowed to config, data and a dedicated 0700 socket dir;
`/tmp`, `/dev` and the rest of `$XDG_RUNTIME_DIR` verified denied; default
socket dir guarded against squatting.

## Review triggers

Re-read this document and update it when a change:

- adds any write to a path outside `paths.CONFIG_DIR` / `paths.DATA_DIR`;
- adds any `open()` / `read()` of a user file in `scanner.py` or `engine.py`;
- changes the `claude` command line, the prompts, or the JSON schemas;
- adds a new IPC command or changes socket permissions;
- changes what is included in `build_propose_prompt` / `build_learn_prompt`.
