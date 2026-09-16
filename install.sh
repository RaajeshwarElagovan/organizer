#!/usr/bin/env bash
# Install organizer for the current user: package -> ~/.local/lib/organizer,
# launcher -> ~/.local/bin/organizer, systemd --user unit, seed memory.
# Everything it writes is under $HOME; re-running upgrades the code and keeps
# ~/.config/organizer (memory) and ~/.local/share/organizer (state, reports).
set -euo pipefail
trap 'echo "install.sh: failed at line $LINENO — the install is incomplete; fix the cause and re-run ./install.sh" >&2' ERR
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/lib/organizer"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
CFG="$HOME/.config/organizer"
ME="${USER:-$(id -un)}"

# The launcher and the unit run /usr/bin/python3 explicitly, so that is what must exist.
[ -x /usr/bin/python3 ] || { echo "/usr/bin/python3 is required (used by the launcher and the systemd unit)" >&2; exit 1; }
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "/usr/bin/python3 must be 3.8 or newer" >&2; exit 1; }

mkdir -p "$LIB" "$BIN" "$UNIT_DIR" "$CFG" "$HOME/.local/share/organizer"
rm -rf "$LIB/organizer" "$LIB/seed"
cp -r "$SRC/organizer" "$SRC/seed" "$LIB/"
cp "$SRC/README.md" "$SRC/MEMORY-GUIDE.md" "$LIB/"
find "$LIB" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# PYTHONSAFEPATH (3.11+) keeps the cwd off sys.path, so running `organizer` from
# inside a checkout still runs the installed copy, not the checkout.
cat > "$BIN/organizer" <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH="$HOME/.local/lib/organizer${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONSAFEPATH=1
exec /usr/bin/python3 -m organizer.cli "$@"
LAUNCHER
chmod +x "$BIN/organizer"

if [ ! -f "$CFG/memory.json" ]; then
  cp "$SRC/seed/memory.json" "$CFG/memory.json"
  echo "seeded $CFG/memory.json"
else
  echo "kept existing $CFG/memory.json"
fi
# A copy of the guide next to the memory so an agent editing it finds the schema.
cp "$SRC/MEMORY-GUIDE.md" "$CFG/MEMORY-GUIDE.md"

cp "$SRC/systemd/organizer.service" "$UNIT_DIR/organizer.service"
if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable organizer.service
  systemctl --user restart organizer.service     # (re)start on the freshly installed code
  # Lingering would keep the user's systemd instance — and this daemon — running from
  # boot and across logouts. The daemon is only useful while you are logged in (the CLI
  # falls back to in-process mode anyway), and lingering is a system-level, per-user
  # setting the uninstaller cannot safely undo, so it is opt-in: ORGANIZER_LINGER=1.
  if [ -n "${ORGANIZER_LINGER:-}" ]; then
    if [ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null)" = "yes" ]; then
      echo "lingering already enabled for $ME"
    elif loginctl enable-linger "$ME" 2>/dev/null; then
      echo "enabled lingering: organizer starts at boot even before login (undo: loginctl disable-linger $ME)"
    else
      echo "NOTE: could not enable lingering; run: sudo loginctl enable-linger $ME"
    fi
  else
    echo "service enabled for your login session (to also run it at boot without logging in: ORGANIZER_LINGER=1 ./install.sh)"
  fi
  sleep 1
  systemctl --user --no-pager --lines=3 status organizer.service || true
else
  echo "systemd --user not available; run the daemon manually:"
  echo "  PYTHONPATH=$LIB python3 -m organizer.daemon &"
  echo "(or just use 'organizer --no-daemon ...'; the CLI also falls back to in-process mode on its own)"
fi

case ":$PATH:" in *":$BIN:"*) ;; *) echo "NOTE: add $BIN to your PATH";; esac
echo
echo "installed. try:  cd ~/Downloads && organizer"
command -v claude >/dev/null || echo "WARNING: 'claude' CLI not on PATH; the AI stage will be skipped (rules still work)"
