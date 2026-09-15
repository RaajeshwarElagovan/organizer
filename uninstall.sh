#!/usr/bin/env bash
# Remove organizer. Keeps ~/.config/organizer (memory) unless --purge is given.
set -uo pipefail
systemctl --user disable --now organizer.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/organizer.service"
systemctl --user daemon-reload 2>/dev/null
rm -rf "$HOME/.local/lib/organizer"
rm -f "$HOME/.local/bin/organizer"
rm -f "${XDG_RUNTIME_DIR:-/tmp}/organizer.sock"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.config/organizer" "$HOME/.local/share/organizer"
  echo "removed organizer including memory, state and reports"
else
  echo "removed organizer; memory kept at ~/.config/organizer (use --purge to delete)"
fi
