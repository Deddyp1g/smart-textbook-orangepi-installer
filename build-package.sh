#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid VERSION" >&2; exit 2; }
OUT="$ROOT/dist/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
rm -rf -- "$ROOT/dist"
mkdir -p -- "$ROOT/dist"
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  --exclude='./dist' --exclude='./.git' --exclude='./__pycache__' --exclude='*/__pycache__' --exclude='*.pyc' \
  -czf "$OUT" -C "$ROOT" .
( cd "$ROOT/dist" && sha256sum "$(basename -- "$OUT")" > "$(basename -- "$OUT").sha256" )
printf 'created %s\n' "$OUT"
printf 'created %s\n' "$OUT.sha256"
