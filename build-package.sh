#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid VERSION" >&2; exit 2; }
OUT="$ROOT/dist/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
PACKAGE_DIR="smart-textbook-orangepi-installer-${VERSION}"
mkdir -p -- "$ROOT/dist"
"$ROOT/verify-bundle.sh" --root "$ROOT" --quiet
python3 "$ROOT/verify-public-config.py" --root "$ROOT"
[[ -x "$ROOT/install-course-bundle.sh" ]] || { echo "install-course-bundle.sh is missing or not executable" >&2; exit 1; }
PACKAGE_FILES=(
  README.md UPDATES.md VERSION agent-requirements.lock bootstrap-dependencies.sh build-package.sh
  course-bundle/SHA256SUMS course-bundle/manifest.json course-bundle/data/mnist-verified.tar.zst
  device/Dockerfile device/ascend.bashrc device/npu-scoped-ready device/scoped_container.py
  experiments/catalog.json experiments/mindspore-evaluate experiments/mindspore-infer
  experiments/mindspore-training install-course-bundle.sh install-workspace.sh install.sh
  pair-server.py preflight.py provision-host-stack.py pty-sandbox-launcher reference-labs/train-lenet.py
  requirements.txt runtime-agent.env.example runtime-manifest.schema.json scrub-generation
  smart-textbook-runtime-agent.py smart-textbook-runtime-agent.service sync-runtime-assets.py
  terminate-generation verify-bundle.sh verify-public-config.py
)
TEMP_OUT="$(mktemp "$ROOT/dist/.installer.XXXXXX")"
trap 'rm -f -- "$TEMP_OUT"' EXIT
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  --transform="s,^,${PACKAGE_DIR}/," \
  -czf "$TEMP_OUT" -C "$ROOT" -- "${PACKAGE_FILES[@]}"
python3 "$ROOT/verify-public-config.py" --root "$ROOT" --package "$TEMP_OUT"
chmod 0644 "$TEMP_OUT"
mv -f -- "$TEMP_OUT" "$OUT"
( cd "$ROOT/dist" && sha256sum "$(basename -- "$OUT")" > "$(basename -- "$OUT").sha256" )
printf 'created %s\n' "$OUT"
printf 'created %s\n' "$OUT.sha256"
