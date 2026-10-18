#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
while (($#)); do
  case "$1" in
    --root) ROOT="$(cd -- "${2:?missing root}" && pwd)"; shift 2 ;;
    --quiet) QUIET=true; shift ;;
    --json) JSON=true; shift ;;
    -h|--help) echo "Usage: verify-bundle.sh [--root PACKAGE_ROOT] [--quiet] [--json]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
: "${QUIET:=false}"
: "${JSON:=false}"
MANIFEST="$ROOT/course-bundle/manifest.json"
[[ -f "$MANIFEST" ]] || { echo "course bundle manifest is missing: $MANIFEST" >&2; exit 1; }
REPORT="$({ python3 - "$ROOT" "$MANIFEST" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); manifest_path=Path(sys.argv[2]).resolve()
try: data=json.loads(manifest_path.read_text())
except Exception as exc: raise SystemExit(f"invalid course bundle manifest: {exc}")
if data.get("schema") != "smart-textbook/orangepi-course-bundle/v1": raise SystemExit("unsupported course bundle schema")
files=data.get("files")
if not isinstance(files,list) or not files: raise SystemExit("course bundle files must be a non-empty list")
required={"experiments/mindspore-training","experiments/mindspore-evaluate","experiments/mindspore-infer","reference-labs/train-lenet.py","data/mnist-verified.tar.zst"}
seen=set(); checked=[]
for item in files:
 if not isinstance(item,dict): raise SystemExit("course bundle entry must be an object")
 rel=str(item.get("path") or "")
 if rel in seen or not re.fullmatch(r"(?:experiments|reference-labs|data)/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*",rel): raise SystemExit(f"unsafe or duplicate course bundle path: {rel}")
 seen.add(rel)
 source=(root/rel) if not rel.startswith("data/") else (root/"course-bundle"/rel)
 if not source.is_file() or source.is_symlink(): raise SystemExit(f"course bundle file missing or symlink: {rel}")
 digest=hashlib.sha256(source.read_bytes()).hexdigest(); expected=str(item.get("sha256") or "")
 size=int(item.get("size")) if type(item.get("size")) is int else -1
 if len(expected)!=64 or digest != expected: raise SystemExit(f"course bundle digest mismatch: {rel}")
 if size < 0 or source.stat().st_size != size: raise SystemExit(f"course bundle size mismatch: {rel}")
 checked.append({"path":rel,"size":size,"sha256":digest})
if not required.issubset(seen): raise SystemExit("course bundle is missing a required experiment/data file")
data_spec=data.get("data") or {}
if data_spec.get("archive") != "data/mnist-verified.tar.zst" or data_spec.get("target_subdir") != "data/mnist": raise SystemExit("course bundle data contract mismatch")
if type(data_spec.get("unpacked_size")) is not int or data_spec["unpacked_size"] <= 0: raise SystemExit("course bundle unpacked size missing")
contract=data.get("runtime_contract") or {}
for key in ("architecture","device_model","driver_version","firmware_version","cann_version","acl_version","python_abi","mindspore_version","numpy","scipy","safetensors"):
 if not str(contract.get(key) or "").strip(): raise SystemExit(f"course bundle runtime contract missing: {key}")
result={"schema":data["schema"],"bundle_revision":data.get("bundle_revision"),"artifact_revision":data.get("artifact_revision"),"files":checked,"file_count":len(checked),"data_unpacked_size":data_spec["unpacked_size"],"runtime_contract":contract}
print(json.dumps(result,ensure_ascii=False,separators=(",",":")))
PY
} 2>&1)"
if [[ "$JSON" == true ]]; then
  printf '%s\n' "$REPORT"
elif [[ "$QUIET" != true ]]; then
  python3 - "$REPORT" <<'PY'
import json,sys
v=json.loads(sys.argv[1]); print(f"course bundle verified: {v['bundle_revision']} ({v['file_count']} files; artifact {v['artifact_revision']})")
for item in v["files"]: print(f"  {item['path']} {item['size']} bytes sha256={item['sha256']}")
print(f"  MNIST unpacked bytes={v['data_unpacked_size']}")
PY
fi