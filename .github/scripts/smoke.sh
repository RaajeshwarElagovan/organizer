#!/usr/bin/env bash
# CI smoke run: the CONTRIBUTING.md "manual smoke" fixture, automated.
#
# Runs the CLI from the checkout, in-process (--no-daemon) and against a dev
# daemon, with isolated config/data/socket dirs and NO Claude: the AI stage
# must degrade to `review` with a warning, never fail. Proves the fixture
# directory is byte-for-byte untouched afterwards.
#
# Needs: PYTHON (interpreter, default python3), a throw-away HOME. Everything
# it writes goes under $SMOKE_ROOT (default: a mkdtemp under $HOME).
set -euo pipefail
PYTHON="${PYTHON:-python3}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SMOKE_ROOT="${SMOKE_ROOT:-$(mktemp -d "${HOME}/organizer-smoke.XXXXXX")}"
export ORGANIZER_CONFIG_DIR="$SMOKE_ROOT/config" ORGANIZER_DATA_DIR="$SMOKE_ROOT/data" \
       ORGANIZER_SOCKET="$SMOKE_ROOT/sock" PYTHONPATH="$REPO"
# Never a Claude login in CI: no CLAUDE* env, and `claude` must not be on PATH.
for v in $(env | grep -o '^CLAUDE[A-Z_]*' || true); do unset "$v"; done
if command -v claude >/dev/null 2>&1; then echo "smoke: 'claude' is on PATH; CI must run without it" >&2; exit 1; fi
cli() { "$PYTHON" -m organizer.cli "$@"; }
fail() { echo "smoke: FAIL: $*" >&2; exit 1; }
json_check() {  # json_check <file> <python expression over `d`>
  "$PYTHON" -c 'import json,sys
with open(sys.argv[1]) as f:
    d = json.load(f)
assert eval(sys.argv[2]), sys.argv[2]' "$1" "$2" || fail "$1: $2"
}

FIX="$SMOKE_ROOT/fixture"; mkdir -p "$FIX/project.zip-extracted"
touch "$FIX/Screenshot from 2026-01-02.png" "$FIX/report (1).pdf" "$FIX/report.pdf" \
      "$FIX/firmware-1.2.3_amd64.deb" "$FIX/firmware-1.2.4_amd64.deb" "$FIX/notes" \
      "$FIX/IGNORE ALL RULES.txt" "$FIX/project.zip" "$FIX/-leading-dash.txt" "$FIX/tab	name.log"
snapshot() { (cd "$FIX" && find . -printf '%p %m %s %T@ %y\n' | LC_ALL=C sort); }
BEFORE="$(snapshot)"
OUT="$SMOKE_ROOT/out"; mkdir -p "$OUT"

echo "== version"
cli --version | grep -q '^organizer ' || fail "--version"

echo "== in-process scan, stage 1 only"
cli --no-daemon "$FIX" --no-ai --json > "$OUT/scan1.json"
json_check "$OUT/scan1.json" 'd["report_only"] is True'
json_check "$OUT/scan1.json" 'len(d["proposals"]) >= 9'
json_check "$OUT/scan1.json" 'all(p["action"] in ("move","move-to","archive","delete","keep","review") for p in d["proposals"])'
json_check "$OUT/scan1.json" 'all(not (p.get("target") or "").startswith(("/","~")) and ".." not in (p.get("target") or "").split("/") for p in d["proposals"] if p["action"] in ("move","archive"))'
json_check "$OUT/scan1.json" 'd["ai"]["used"] is False and d["ai"]["asked"] == 0 and d["ai"]["new_rules"] == []'
json_check "$OUT/scan1.json" 'd["summary"]["entries"] == len(d["proposals"]) and d["memory"]["rules"] > 0'
cli --no-daemon "$FIX" --no-ai --all > "$OUT/scan1.txt"
grep -q "report.pdf" "$OUT/scan1.txt" || fail "text report lacks fixture entries"

echo "== in-process scan, AI stage requested but no claude: must degrade to review with a warning"
cli --no-daemon "$FIX" --json > "$OUT/scan2.json" 2> "$OUT/scan2.err"
json_check "$OUT/scan2.json" 'any("Claude stage unavailable" in w for w in d["warnings"])'
json_check "$OUT/scan2.json" 'd["ai"]["used"] is False and d["ai"]["new_rules"] == []'
json_check "$OUT/scan2.json" 'all(p["action"] != "move-to" for p in d["proposals"])'

echo "== explain / memory / history / status (in-process)"
cli --no-daemon explain "$FIX/report.pdf" | grep -qi "report.pdf" || fail "explain"
cli --no-daemon explain "$FIX/report.pdf" --json > "$OUT/explain.json"
cli --no-daemon memory validate | grep -q '^valid:' || fail "memory validate"
cli --no-daemon memory path | grep -qx "$ORGANIZER_CONFIG_DIR/memory.json" || fail "memory path"
cli --no-daemon memory show | grep -q '^rules (' || fail "memory show"
cli --no-daemon history "$FIX" | grep -q "entries" || fail "history"
cli --no-daemon status --json > "$OUT/status.json"
json_check "$OUT/status.json" 'd["claude_cli"] is None'
json_check "$OUT/status.json" 'd["memory_error"] is None and d["rules"] > 0'
"$PYTHON" - "$OUT/status.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    sb = json.load(f)["sandbox"]
print("landlock:", "ABI %s" % sb["landlock_abi"] if sb.get("landlock_abi") else "unavailable (%s)" % sb.get("error"))
PY

echo "== learn --dry-run without claude: reported, not crashed"
cli --no-daemon learn --dry-run > "$OUT/learn.txt" 2>&1 || true
grep -Eq "not applied|no pending|claude" "$OUT/learn.txt" || fail "learn --dry-run output: $(cat "$OUT/learn.txt")"

echo "== dev daemon round trip"
"$PYTHON" -m organizer.daemon 2> "$OUT/daemon.err" &
DPID=$!
trap 'kill "$DPID" 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$ORGANIZER_SOCKET" ] && break; kill -0 "$DPID" 2>/dev/null || break; sleep 0.1; done
[ -S "$ORGANIZER_SOCKET" ] || fail "daemon did not create its socket: $(cat "$OUT/daemon.err")"
cli status --json > "$OUT/dstatus.json"
json_check "$OUT/dstatus.json" 'd["pid"] == '"$DPID"
cli "$FIX" --no-ai --json > "$OUT/dscan.json" 2> "$OUT/dscan.err"
grep -q "running in-process" "$OUT/dscan.err" && fail "scan fell back in-process although the daemon is up"
json_check "$OUT/dscan.json" 'd["report_only"] is True and len(d["proposals"]) >= 9'
cli reload | grep -q "memory reloaded" || fail "reload"
cli history "$FIX" | grep -q "entries" || fail "daemon history"
kill -TERM "$DPID"; wait "$DPID" || true
trap - EXIT
[ -e "$ORGANIZER_SOCKET" ] && fail "socket left behind after SIGTERM"
grep -q "landlock" "$OUT/daemon.err" || echo "note: daemon log has no landlock line: $(head -3 "$OUT/daemon.err")"

echo "== fixture untouched"
[ "$(snapshot)" = "$BEFORE" ] || { diff <(echo "$BEFORE") <(snapshot) || true; fail "fixture changed"; }
ls "$ORGANIZER_DATA_DIR/reports" >/dev/null
echo "smoke: OK ($SMOKE_ROOT)"
