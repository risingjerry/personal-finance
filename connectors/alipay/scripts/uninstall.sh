#!/bin/sh
# Remove the entry point. Private data under ~/.alipay-cli is kept unless --purge.
set -eu
PREFIX="$HOME/.local/bin"
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
rm -f "$PREFIX/alipay"
echo "removed $PREFIX/alipay"
# Optional launchd agents this CLI may have installed.
launchctl bootout "gui/$(id -u)/com.alipay-cli.keepalive" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.alipay-cli.keepalive.plist"
launchctl bootout "gui/$(id -u)/com.alipay-cli.sync-retry" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.alipay-cli.sync-retry.plist"
if [ "$PURGE" = 1 ]; then
  printf "delete ~/.alipay-cli (cookie vault, profile, bills)? [y/N] "
  read -r a
  case "$a" in y|Y) rm -rf "$HOME/.alipay-cli"; echo "purged ~/.alipay-cli" ;; *) echo "kept ~/.alipay-cli" ;; esac
else
  echo "kept ~/.alipay-cli (pass --purge to delete)"
fi
