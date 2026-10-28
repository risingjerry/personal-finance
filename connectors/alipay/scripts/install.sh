#!/bin/sh
# Install alipay-cli: symlink the entry point and run an offline self-check.
# Usage: scripts/install.sh [--prefix DIR]      (default prefix: ~/.local/bin)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
PREFIX="$HOME/.local/bin"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

NODE_BIN=$(command -v node || echo /usr/local/bin/node)
"$NODE_BIN" -e 'const m=+process.versions.node.split(".")[0]; if(m<22){console.error("node >= 22 required, found "+process.versions.node);process.exit(1)}'

mkdir -p "$PREFIX"
ln -sfn "$HERE/bin/alipay" "$PREFIX/alipay"
echo "linked $PREFIX/alipay -> $HERE/bin/alipay"

# The CLI reads nothing at install time and never talks to Alipay here.
"$PREFIX/alipay" doctor --offline >/dev/null 2>&1 || true
"$PREFIX/alipay" --version
echo
echo "next:"
echo "  alipay auth login && alipay sync     # first run only (one QR scan)"
echo "  alipay bills summary --by month      # everything else is local"
