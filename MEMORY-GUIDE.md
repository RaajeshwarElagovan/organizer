# organizer memory — editing guide

File: `~/.config/organizer/memory.json`. The daemon hot-reloads it (mtime/size/inode check before every request), keeps `memory.json.bak` on every write it makes itself, and refuses to load an invalid file: it keeps the last good copy in memory (an empty default memory if it was invalid at startup), reports the error in `organizer status` and in every scan's `warnings`, and **never writes over the invalid file** — outcomes learned meanwhile stay in memory only and are dropped when the repaired file is reloaded. Fix the file (or `cp memory.json.bak memory.json`), then `organizer reload`. Validate after editing: `organizer memory validate`.

First run: when the file does not exist it is copied from the seed — `seed/memory.json` next to the installed package (checkout, `~/.local/lib/organizer`) or, for the Debian package, `/usr/share/organizer/seed/memory.json` (found via `$XDG_DATA_DIRS`; `ORGANIZER_SEED=<file>` overrides). An existing `memory.json` is never replaced by the seed, valid or not.

This file is meant to be rewritten by a Claude agent (or by hand). Everything except `settings` is fair game.

## Schema

```jsonc
{
  "version": 1,
  "settings": { ... },            // tunables, see below. Not touched by consolidation.
  "categories": {                 // category key -> folder (relative to the scanned dir)
    "documents/finance": {"dir": "Documents/Finance", "confidence": 0.8, "note": "..."},
    "images/reference":  {"dir": "Images", "ext": ["png","jpg"], "confidence": 0.6},
    "backups":           {"dir": "Backups", "signals": ["backup"], "confidence": 0.85}
  },
  "targets": {"backups": "~/Documents/Backups"},   // named absolute destinations for "move-to"
  "rules": [
    {
      "id": "r-statement-pdf",             // unique, stable
      "match": {"glob": "statement-*.pdf"}, // see match keys
      "category": "documents/finance",       // key of categories (gives the folder for "move")
      "action": "move",                      // move | move-to | archive | delete | keep | review
      "target": null,                        // optional: folder override; required for move-to (name in targets or ~/path)
      "scope": "global",                     // "global" or an absolute directory path
      "confidence": 0.85,                    // 0..1; >= settings.ai_threshold means "decided without Claude"
      "hits": 0, "contradictions": 0,        // maintained by outcome learning
      "source": "claude",                    // seed | claude | observed | user
      "note": "why this rule exists"
    }
  ],
  "dir_overrides": {                         // per-directory tweaks
    "/home/me/Downloads": {"ignore": ["keep-me"], "rules": [ ...same rule shape... ]}
  },
  "learned": {"pending": [...], "confirmed": [...]},   // observed outcomes; managed by the daemon
  "claude_notes": "free text: durable insight about how this user organises files",
  "last_consolidation": {"ts": 0, "rationale": "..."}  // written by the daemon
}
```

### match keys (all given keys must hold)
| key | meaning |
|---|---|
| `glob` | case-insensitive shell glob on the file name |
| `regex` | Python regex, `re.search`, case-insensitive, ≤ 200 chars. A quantified group that ends in a quantifier (`(a+)+`, `(\w+\s?)*`, `(\.\d+)+`) is refused as a backtracking risk even when it happens to be safe — write `v\d+(\.\d+)?(\.\d+)?` instead of `v\d+(\.\d+)+`. Prefer `glob`. **User-only key:** rules Claude suggests (`new_rules`) and consolidation rewrites cannot add a regex or change an existing one — they are rejected in code; a rewrite may only keep your regex rule verbatim or drop it. |
| `ext` | list of extensions without dot (`tar.gz` allowed) |
| `mime_prefix` | e.g. `image/` |
| `is_dir` | true/false |
| `min_size_mb`, `max_size_mb` | size bounds |
| `older_than_days` | untouched for more than N days |
| `signals` | list; every listed signal must be present |

Signals produced by the scanner: `screenshot`, `ai_image`, `drive_download`, `backup`, `release`, `arch_token`, `version_token`, `date_token`, `generic_name`, `temp_marker`, `dup_suffix`, `duplicate_identical`, `duplicate_revision`, `has_duplicates`, `extracted_dir_present`, `archive_present`, `series_newest`, `series_older`, `directory`, `symlink`, `hidden`, `executable`, `no_extension`.

## Precedence (classifier stage 1)
1. `dir_overrides[cwd].ignore` → keep.
2. Redundancy heuristics: identical duplicate → delete; zip already extracted → delete; older build in a version series → archive (or delete if `delete_policy: aggressive`).
3. Rules: dir-scoped (override rules + `scope == cwd`) first, then global; within each, highest confidence first; first match wins.
4. Directories: extracted contents / organizer folders → keep; others → review (Claude decides).
5. Category table by `signals`, then `ext`, then `mime_prefix`; highest confidence wins.
6. Age policy on move proposals: older than `stale_after_days` and disposable → delete; older than `archive_after_days` → archive to `_archive/<year>` (unless accessed in the last `recent_access_veto_days`).
7. Anything under `ai_threshold` → Claude stage (one batched `claude -p` call). Without Claude → `review` with the tentative decision shown.

## How learning changes this file
- Layer 1 (deterministic, every scan): compares the last proposal for that directory with what is there now. Records to `learned.pending`: `confirmed`, `moved_elsewhere:<path>`, `deleted`, `ignored`. Immediately bumps `hits`/`confidence` (+0.05 on confirm, −0.1 on contradiction, −0.05 on ignored; floor 0.5 for `source: claude`, 0.2 otherwise — a floor only stops the decline, it never raises a rule that is already below it). It never invents rules.
  - `confirmed`: the file is at the proposed folder, or — for a `delete` proposal — is gone and was not found elsewhere. A file is "found" only if it has the name **and the byte size** the proposal recorded (a move keeps the size); a same-name file of another size anywhere makes the case *ambiguous* and nothing is recorded — this covers an unrelated same-name file at the destination, and a file edited after being moved. Two same-name files of the same size are indistinguishable with names and `stat()` alone; the worst that can happen then is one wrong `confirmed` (+0.05, `hits`+1) or `moved_elsewhere` (−0.1), never a new rule.
  - `moved_elsewhere:<path>`: the file turned up in another folder (searched two levels under the scanned directory, plus configured `targets`). A `delete` proposal whose file was filed away instead counts here, not as confirmed.
  - `deleted`: the file is gone and was not found. A rename, or a move deeper than the search looks, is indistinguishable from a delete and is recorded as one.
  - `ignored`: the file is still there after `ignored_after_scans` further scans **and** `ignored_after_days` days since the proposal was first made. Recorded once per proposal; a changed proposal restarts both counters. Fewer scans, or the same number of scans within fewer days, records nothing.
  - `keep` and `review` proposals (including undecided entries left as `review` because Claude was unavailable) are not tracked at all — nothing about them is learned, whatever the user does.
  - Only the rule named by the proposal's `rule_id` is adjusted. Decisions from the category table, the redundancy heuristics or Claude carry no `rule_id`, so they are recorded but move no counter.
- Layer 2 (Claude, when `pending >= learn_batch` or a rule reaches `learn_contradictions`, or `organizer learn`): rewrites `rules`, `categories`, `targets`, `claude_notes` from the evidence. Rejected if it drops more than half the rules or fails validation. Backup kept in `memory.json.bak`.

## Advice for an agent editing this file
- Keep rule `id`s stable; add new ones with the `r-<something>` convention.
- Globs must be specific. `*.pdf` is never a rule — that is what `categories` are for.
- Put a folder in `categories` and point rules at it by key, rather than hard-coding `target` on many rules.
- Use `move-to` (with `targets`) only for destinations outside the scanned directory.
- After editing: `organizer memory validate && organizer reload`.
- Do not edit `learned.pending` while a consolidation is running (`organizer status` shows it).

## settings
Types are validated on load: booleans must be `true`/`false`, `delete_policy`/`ai_model` strings, everything else a non-negative number (`ai_threshold` 0..1). `"learn_batch": "5"` makes the file invalid rather than crashing a scan.

| key | default | meaning |
|---|---|---|
| `archive_after_days` | 180 | move → archive when older than this |
| `stale_after_days` | 365 | disposable + older than this → delete |
| `recent_access_veto_days` | 7 | accessed this recently → never archived |
| `delete_policy` | conservative | `aggressive` deletes superseded builds instead of archiving |
| `use_magic` | true | `file --mime-type` on extensionless files (magic bytes only) |
| `ai_enabled` / `ai_model` | true / sonnet | Claude stage |
| `ai_threshold` | 0.7 | below this a proposal is sent to Claude |
| `ai_max_budget_usd` / `ai_timeout_s` | 0.10 / 120 | per call |
| `learn_batch` / `learn_contradictions` | 5 / 2 | consolidation triggers |
| `ignored_after_scans` / `ignored_after_days` | 3 / 3 | when a still-present file counts as "ignored" |
| `max_confirmed_history` | 200 | outcomes kept after consolidation |
