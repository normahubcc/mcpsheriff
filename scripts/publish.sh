#!/usr/bin/env bash
# Publish mcpsheriff to PyPI. The token is read from a file, never from argv.
#
#   bash scripts/publish.sh --check     # build + twine check, no upload, no token
#   bash scripts/publish.sh             # publish to PyPI
#
# Token file: $HOME/.private/pypi-token (or $NORMAHUB_PRIVATE_DIR/pypi-token),
# must be readable by the owner only (600/400).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON="${PYTHON:-python3}"
PRIVATE_DIR="${NORMAHUB_PRIVATE_DIR:-$HOME/.private}"
TOKEN_FILE="$PRIVATE_DIR/pypi-token"
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "publish: $*" >&2; exit 1; }

"$PYTHON" -c 'import tomllib' 2>/dev/null \
  || die "needs Python 3.11+ (tomllib). Set PYTHON=/path/to/python3.12."

VERSION=$(grep -E '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)
CODE_VERSION=$("$PYTHON" -c 'import mcpsheriff; print(mcpsheriff.__version__)')
[ "$VERSION" = "$CODE_VERSION" ] \
  || die "version mismatch: pyproject=$VERSION, mcpsheriff.__version__=$CODE_VERSION"

echo "== tests ($VERSION)"
"$PYTHON" -m pytest -q

rm -rf dist build ./*.egg-info
"$PYTHON" -m build
"$PYTHON" -m twine check dist/*

if [ "$CHECK_ONLY" = "1" ]; then
  echo "== check only: artifacts are ready, nothing uploaded"
  exit 0
fi

[ -f "$TOKEN_FILE" ] || die "no token file: $TOKEN_FILE (mode 600, owner only)"

PERMS=$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null || stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null)
case "$PERMS" in
  600|400) ;;
  *) die "wrong permissions on $TOKEN_FILE: $PERMS (expected 600 or 400)" ;;
esac

TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
[ -n "$TOKEN" ] || die "token file is empty: $TOKEN_FILE"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT INT TERM
umask 077
cat > "$WORKDIR/.pypirc" <<PYRC
[distutils]
index-servers = pypi

[pypi]
username = __token__
password = $TOKEN
PYRC

"$PYTHON" -m twine upload --config-file "$WORKDIR/.pypirc" dist/*
echo "== published. verify: https://pypi.org/project/mcpsheriff/$VERSION/"