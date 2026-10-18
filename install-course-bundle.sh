#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SOURCE_ROOT=""
BUNDLE_ROOT="/usr/local/share/smart-textbook/course-bundle"
DATA_ROOT="/usr/local/share/smart-textbook/data/mnist"
STATE_FILE="/var/lib/smart-textbook/runtime-agent/course-bundle-state.json"
VERIFY_ONLY=false
usage() {
  cat <<'EOF'
Usage: install-course-bundle.sh --source-root DIR [--bundle-root DIR] [--data-root DIR] [--state-file FILE] [--verify]
Install the embedded MindSpore course bundle atomically and record resumable state.
EOF
}
while (($#)); do
  case "$1" in
    --source-root) SOURCE_ROOT="${2:?missing source root}"; shift 2 ;;
    --bundle-root) BUNDLE_ROOT="${2:?missing bundle root}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:?missing data root}"; shift 2 ;;
    --state-file) STATE_FILE="${2:?missing state file}"; shift 2 ;;
    --verify) VERIFY_ONLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$SOURCE_ROOT" ]] || { echo "--source-root is required" >&2; exit 2; }
SOURCE_ROOT="$(cd -- "$SOURCE_ROOT" && pwd)"
[[ -x "$SOURCE_ROOT/verify-bundle.sh" ]] || { echo "course bundle verifier is missing" >&2; exit 1; }
REPORT="$("$SOURCE_ROOT/verify-bundle.sh" --root "$SOURCE_ROOT" --json)"
MANIFEST="$SOURCE_ROOT/course-bundle/manifest.json"
PYTHON="${PYTHON:-python3}"
MANIFEST_SHA256="$(sha256sum "$MANIFEST" | awk '{print $1}')"
BUNDLE_REVISION="$($PYTHON -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bundle_revision", ""))' "$MANIFEST")"
CHECKSUM_NAME="$($PYTHON -c 'import json,sys; print(json.load(open(sys.argv[1])).get("checksum_file", "SHA256SUMS"))' "$MANIFEST")"
ARCHIVE="$SOURCE_ROOT/course-bundle/data/mnist-verified.tar.zst"
STATE_PARENT="$(dirname -- "$STATE_FILE")"
write_state() {
  local phase="$1" detail="${2:-}"
  install -d -m 0700 "$STATE_PARENT"
  "$PYTHON" - "$STATE_FILE" "$phase" "$detail" "$MANIFEST_SHA256" "$BUNDLE_REVISION" "$BUNDLE_ROOT" "$DATA_ROOT" "$CHECKSUM_NAME" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
path, phase, detail, manifest_sha, revision, bundle_root, data_root, checksum_name = sys.argv[1:]
payload = {
    "schema": "smart-textbook/orangepi-course-bundle-state/v1",
    "phase": phase,
    "detail": detail,
    "manifest_sha256": manifest_sha,
    "bundle_revision": revision,
    "checksum_file": checksum_name,
    "bundle_root": bundle_root,
    "data_root": data_root,
}
target=Path(path); target.parent.mkdir(parents=True, exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix=target.name+'.', dir=str(target.parent), text=True)
os.close(fd)
try:
    Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp,target)
finally:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
PY
}
atomic_copy() {
  local src="$1" dest="$2" mode="$3" tmp
  install -d -m 0755 "$(dirname -- "$dest")"
  tmp="${dest}.tmp.$$.$RANDOM"
  install -m "$mode" "$src" "$tmp"
  mv -f -- "$tmp" "$dest"
}
verify_installed() {
  "$SOURCE_ROOT/verify-bundle.sh" --root "$SOURCE_ROOT" --quiet
  "$PYTHON" - "$SOURCE_ROOT" "$BUNDLE_ROOT" "$DATA_ROOT" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path
source,bundle_root,data_root=map(Path,sys.argv[1:])
manifest=json.loads((source/'course-bundle/manifest.json').read_text())
for item in manifest['files']:
    rel=item['path']
    expected=source/rel if not rel.startswith('data/') else source/'course-bundle'/rel
    installed=bundle_root/rel
    if not installed.is_file() or installed.is_symlink(): raise SystemExit(f'missing installed course file: {installed}')
    expected_mode = int(str(item.get('mode', '0o644')), 8)
    if installed.stat().st_size != item['size'] or hashlib.sha256(installed.read_bytes()).hexdigest() != item['sha256'] or stat.S_IMODE(installed.stat().st_mode) != expected_mode:
        raise SystemExit(f'installed course file digest or mode mismatch: {installed}')
for item in manifest.get('data',{}).get('files',[]):
    installed=data_root/item['path']
    if not installed.is_file() or installed.is_symlink(): raise SystemExit(f'missing installed MNIST file: {installed}')
    if installed.stat().st_size != item['size'] or hashlib.sha256(installed.read_bytes()).hexdigest() != item['sha256']:
        raise SystemExit(f'installed MNIST digest mismatch: {installed}')
PY
}
if [[ "$VERIFY_ONLY" == true ]]; then
  verify_installed
  echo "course bundle verified at $BUNDLE_ROOT; MNIST data verified at $DATA_ROOT"
  exit 0
fi
write_state staging "verified embedded source bundle; preparing atomic copies"
install -d -m 0755 "$BUNDLE_ROOT" "$BUNDLE_ROOT/data" "$BUNDLE_ROOT/experiments" "$BUNDLE_ROOT/reference-labs" "$DATA_ROOT"
# Every payload is copied through a same-filesystem temporary path and renamed.
atomic_copy "$MANIFEST" "$BUNDLE_ROOT/manifest.json" 0644
atomic_copy "$SOURCE_ROOT/course-bundle/$CHECKSUM_NAME" "$BUNDLE_ROOT/$CHECKSUM_NAME" 0644
atomic_copy "$SOURCE_ROOT/course-bundle/data/mnist-verified.tar.zst" "$BUNDLE_ROOT/data/mnist-verified.tar.zst" 0644
for file in mindspore-training mindspore-evaluate mindspore-infer; do
  atomic_copy "$SOURCE_ROOT/experiments/$file" "$BUNDLE_ROOT/experiments/$file" 0755
done
atomic_copy "$SOURCE_ROOT/reference-labs/train-lenet.py" "$BUNDLE_ROOT/reference-labs/train-lenet.py" 0755
write_state extracting "copied manifest, checksum, archive and experiment entrypoints"
COURSE_TMP="$(mktemp -d "${DATA_ROOT}.staging.XXXXXX")"
cleanup() { rm -rf -- "$COURSE_TMP"; }
trap cleanup EXIT
if ! zstd -dc "$ARCHIVE" | tar --no-same-owner --no-same-permissions -x -f - -C "$COURSE_TMP"; then
  write_state extracting "MNIST archive extraction interrupted or failed"
  exit 1
fi
mapfile -t EXPECTED_FILES < <("$PYTHON" - "$MANIFEST" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1])).get('data',{}).get('files',[]): print(item['path'])
PY
)
((${#EXPECTED_FILES[@]} > 0)) || { write_state extracting "manifest has no MNIST files"; exit 1; }
for file in "${EXPECTED_FILES[@]}"; do
  [[ "$file" != /* && "$file" != *..* && "$file" != */* ]] || { write_state extracting "unsafe MNIST path: $file"; exit 1; }
  [[ -f "$COURSE_TMP/$file" && ! -L "$COURSE_TMP/$file" ]] || { write_state extracting "MNIST file missing: $file"; exit 1; }
done
for file in "${EXPECTED_FILES[@]}"; do
  atomic_copy "$COURSE_TMP/$file" "$DATA_ROOT/$file" 0644
done
write_state verifying "copied MNIST files; validating installed digests"
verify_installed
write_state complete "verified $((${#EXPECTED_FILES[@]})) MNIST files and all course payloads"
echo "course bundle installed at $BUNDLE_ROOT; MNIST data at $DATA_ROOT (state: $STATE_FILE)"