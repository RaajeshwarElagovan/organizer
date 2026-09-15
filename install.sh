#!/usr/bin/env bash
# Install organizer for the current user: package -> ~/.local/lib/organizer,
# launcher -> ~/.local/bin/organizer, systemd --user unit, seed memory.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/lib/organizer"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
CFG="$HOME/.config/organizer"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

mkdir -p "$LIB" "$BIN" "$UNIT_DIR" "$CFG" "$HOME/.local/share/organizer"
rm -rf "$LIB/organizer" "$LIB/seed"
cp -r "$SRC/organizer" "$SRC/seed" "$LIB/"
cp "$SRC/README.md" "$SRC/MEMORY-GUIDE.md" "$LIB/"
find "$LIB" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$BIN/organizer" <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH="$HOME/.local/lib/organizer${PYTHONPATH:+:$PYTHONPATH}"
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
  systemctl --user enable --now organizer.service
  systemctl --user restart organizer.service
  # Lingering keeps the user's systemd instance (and this service) running from
  # boot, without waiting for a login. Skip with ORGANIZER_NO_LINGER=1.
  if [ -z "${ORGANIZER_NO_LINGER:-}" ] && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    if loginctl enable-linger "$USER" 2>/dev/null; then
      echo "enabled lingering: organizer starts at boot even before login"
    else
      echo "NOTE: could not enable lingering; run: sudo loginctl enable-linger $USER"
    fi
  fi
  sleep 1
  systemctl --user --no-pager --lines=3 status organizer.service || true
else
  echo "systemd --user not available; run the daemon manually:"
  echo "  PYTHONPATH=$LIB python3 -m organizer.daemon &"
fi

case ":$PATH:" in *":$BIN:"*) ;; *) echo "NOTE: add $BIN to your PATH";; esac
echo
echo "installed. try:  cd ~/Downloads && organizer"
command -v claude >/dev/null || echo "WARNING: 'claude' CLI not on PATH; the AI stage will be skipped (rules still work)"
