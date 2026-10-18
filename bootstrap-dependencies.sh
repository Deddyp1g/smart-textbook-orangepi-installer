#!/usr/bin/env bash
# Bootstrap the control-plane Python separately from the Ascend runtime Python.
set +x
set -Eeuo pipefail
umask 077
SOURCE_DIR=${SMART_TEXTBOOK_AGENT_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
PREFIX=${SMART_TEXTBOOK_AGENT_PREFIX:-/opt/smart-textbook/runtime-agent}
PYTHON=${SMART_TEXTBOOK_AGENT_PYTHON:-/usr/bin/python3}
WHEELHOUSE=${SMART_TEXTBOOK_AGENT_WHEELHOUSE:-}
SERVER_URL=${SMART_TEXTBOOK_AGENT_SERVER_URL:-}
ENROLLMENT_TOKEN=${SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN:-}
VERIFY_TLS=${SMART_TEXTBOOK_AGENT_VERIFY_TLS:-true}
trap 'echo "agent dependency bootstrap failed at line $LINENO; check wheelhouse, Python ABI and network availability" >&2' ERR
while (($#)); do
  case "$1" in
    --source-dir) SOURCE_DIR=${2:?}; shift 2;;
    --prefix) PREFIX=${2:?}; shift 2;;
    --wheelhouse) WHEELHOUSE=${2:?}; shift 2;;
    *) echo "unknown dependency option" >&2; exit 2;;
  esac
done
LOCK="$SOURCE_DIR/agent-requirements.lock"
[[ -s "$LOCK" ]] || { echo "agent dependency lock is missing: agent-requirements.lock" >&2; exit 1; }
if [[ -z "$WHEELHOUSE" ]]; then
  [[ -n "$SERVER_URL" && -n "$ENROLLMENT_TOKEN" ]] || { echo "server credentials or --wheelhouse are required for agent dependencies" >&2; exit 1; }
  WHEELHOUSE="$PREFIX/wheelhouse"
  mkdir -p "$WHEELHOUSE"
  CURL_ARGS=(--fail --silent --show-error --connect-timeout 20 --max-time 120)
  [[ "$VERIFY_TLS" == true ]] || CURL_ARGS+=(--insecure)
  curl "${CURL_ARGS[@]}" "$SERVER_URL/api/runtime-agent/bootstrap/wheelhouse/agent-wheels.json" -H "X-Runtime-Agent-Enrollment-Token: $ENROLLMENT_TOKEN" -o "$WHEELHOUSE/agent-wheels.json"
  "$PYTHON" - "$WHEELHOUSE/agent-wheels.json" "$WHEELHOUSE/download-list" <<'PY'
import json,re,sys
from pathlib import Path
index=json.loads(Path(sys.argv[1]).read_text())
if index.get('schema')!='smart-textbook/agent-wheels/v1': raise SystemExit('unsupported agent wheelhouse index')
files=index.get('files')
if not isinstance(files,list) or not 1<=len(files)<=64: raise SystemExit('invalid agent wheelhouse files')
rows=[]; seen=set()
for spec in files:
    name=spec.get('path','')
    if not re.fullmatch(r'wheelhouse/[A-Za-z0-9][A-Za-z0-9_.+\-]*\.whl',name) or name in seen: raise SystemExit('unsafe or duplicate agent wheel path')
    if not re.fullmatch(r'[0-9a-f]{64}',str(spec.get('sha256',''))): raise SystemExit('invalid agent wheel digest')
    size=spec.get('size')
    if not isinstance(size,int) or isinstance(size,bool) or not 0<size<=100*1024*1024: raise SystemExit('invalid agent wheel size')
    seen.add(name); rows.append(f"{name}\t{spec['sha256']}\t{size}\n")
Path(sys.argv[2]).write_text(''.join(rows))
PY
  while IFS=$'\t' read -r relative digest size; do
    target="$WHEELHOUSE/${relative##*/}"
    curl "${CURL_ARGS[@]}" "$SERVER_URL/api/runtime-agent/bootstrap/$relative" -H "X-Runtime-Agent-Enrollment-Token: $ENROLLMENT_TOKEN" -o "$target.part"
    "$PYTHON" - "$target.part" "$digest" "$size" <<'PY'
import hashlib,sys
from pathlib import Path
p=Path(sys.argv[1])
if p.stat().st_size!=int(sys.argv[3]) or hashlib.sha256(p.read_bytes()).hexdigest()!=sys.argv[2]: raise SystemExit('agent wheel integrity check failed')
PY
    mv -f "$target.part" "$target"
  done < "$WHEELHOUSE/download-list"
  rm -f "$WHEELHOUSE/download-list"
elif [[ -s "$WHEELHOUSE/agent-requirements.lock" ]]; then
  LOCK="$WHEELHOUSE/agent-requirements.lock"
fi
[[ -d "$WHEELHOUSE" ]] || { echo "agent dependency wheelhouse directory does not exist" >&2; exit 1; }
# No --system-site-packages and no global pip. An existing venv is reused.
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  env -u PYTHONHOME -u PYTHONPATH "$PYTHON" -m venv "$PREFIX/venv"
fi
if [[ -f "$PREFIX/venv/pyvenv.cfg" ]] && grep -Eiq '^include-system-site-packages[[:space:]]*=[[:space:]]*true' "$PREFIX/venv/pyvenv.cfg"; then
  echo "agent dependency venv exposes system packages; recreate it without --system-site-packages" >&2; exit 1
fi
env -u PYTHONHOME -u PYTHONPATH "$PREFIX/venv/bin/python" -m pip --isolated install --disable-pip-version-check --no-index --only-binary=:all: --require-hashes --find-links "$WHEELHOUSE" -r "$LOCK"
env -u PYTHONHOME -u PYTHONPATH "$PREFIX/venv/bin/python" -c 'import requests, cryptography; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; Ed25519PrivateKey.generate()'
echo "agent dependencies verified in isolated venv" >&2
