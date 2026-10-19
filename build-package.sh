#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid VERSION" >&2; exit 2; }
OUT="$ROOT/dist/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
PACKAGE_DIR="smart-textbook-orangepi-installer-${VERSION}"
rm -rf -- "$ROOT/dist"
mkdir -p -- "$ROOT/dist"
"$ROOT/verify-bundle.sh" --root "$ROOT" --quiet
python3 "$ROOT/verify-public-config.py" --root "$ROOT"
[[ -x "$ROOT/install-course-bundle.sh" ]] || { echo "install-course-bundle.sh is missing or not executable" >&2; exit 1; }
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  --transform="s,^,${PACKAGE_DIR}/," \
  --exclude='./dist' --exclude='./.git' --exclude='./__pycache__' --exclude='*/__pycache__' --exclude='*.pyc' \
  -czf "$OUT" -C "$ROOT" .
python3 "$ROOT/verify-public-config.py" --root "$ROOT" --package "$OUT"
( cd "$ROOT/dist" && sha256sum "$(basename -- "$OUT")" > "$(basename -- "$OUT").sha256" )
printf 'created %s\n' "$OUT"
printf 'created %s\n' "$OUT.sha256"
