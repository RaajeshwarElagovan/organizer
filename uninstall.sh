#!/usr/bin/env bash
# Remove organizer. Keeps ~/.config/organizer (memory) and ~/.local/share/organizer
# (state, reports) unless --purge is given. Safe to run on a partial install.
set -uo pipefail
ME="${USER:-$(id -un)}"
systemctl --user disable --now organizer.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/organizer.service"
systemctl --user daemon-reload 2>/dev/null
rm -rf "$HOME/.local/lib/organizer"
rm -f "$HOME/.local/bin/organizer"
# Socket dirs (see paths.socket_dir): $XDG_RUNTIME_DIR/organizer when set, else the
# /tmp/organizer-<uid> fallback. Never a bare /tmp/organizer — that is not ours.
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
  rm -rf "$XDG_RUNTIME_DIR/organizer"
fi
rm -rf "/tmp/organizer-$(id -u)"
echo "lingering left as is (check: loginctl show-user $ME -p Linger; disable with: loginctl disable-linger $ME)"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.config/organizer" "$HOME/.local/share/organizer"
  echo "removed organizer including memory, state and reports"
else
  echo "removed organizer; memory kept at ~/.config/organizer (use --purge to delete)"
fi
