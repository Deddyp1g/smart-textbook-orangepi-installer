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
if ! REPORT="$({ python3 - "$ROOT" "$MANIFEST" <<'PY'
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
 mode=str(item.get("mode") or "")
 if mode not in {"0o644","0o755"}: raise SystemExit(f"invalid course bundle mode: {rel}")
 if len(expected)!=64 or digest != expected: raise SystemExit(f"course bundle digest mismatch: {rel}")
 if size < 0 or source.stat().st_size != size: raise SystemExit(f"course bundle size mismatch: {rel}")
 checked.append({"path":rel,"size":size,"mode":mode,"sha256":digest})
if not required.issubset(seen): raise SystemExit("course bundle is missing a required experiment/data file")
data_spec=data.get("data") or {}
if data_spec.get("archive") != "data/mnist-verified.tar.zst" or data_spec.get("target_subdir") != "data/mnist": raise SystemExit("course bundle data contract mismatch")
if type(data_spec.get("unpacked_size")) is not int or data_spec["unpacked_size"] <= 0: raise SystemExit("course bundle unpacked size missing")
data_files=data_spec.get("files")
if not isinstance(data_files,list) or not data_files: raise SystemExit("course bundle MNIST file list missing")
data_seen=set()
for item in data_files:
 if not isinstance(item,dict): raise SystemExit("course bundle MNIST entry must be an object")
 rel=str(item.get("path") or "")
 if rel in data_seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*",rel): raise SystemExit(f"unsafe or duplicate MNIST path: {rel}")
 digest=str(item.get("sha256") or "")
 size=item.get("size")
 if not re.fullmatch(r"[0-9a-f]{64}",digest) or type(size) is not int or size <= 0: raise SystemExit(f"invalid MNIST checksum entry: {rel}")
 data_seen.add(rel)
if data_seen != {"train-images-idx3-ubyte.gz","train-labels-idx1-ubyte.gz","t10k-images-idx3-ubyte.gz","t10k-labels-idx1-ubyte.gz"}: raise SystemExit("course bundle MNIST file list is incomplete")
checksum_name=str(data.get("checksum_file") or "SHA256SUMS")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", checksum_name): raise SystemExit("unsafe course bundle checksum file name")
checksum_path=root/"course-bundle"/checksum_name
if not checksum_path.is_file() or checksum_path.is_symlink(): raise SystemExit(f"course bundle checksum file missing or symlink: {checksum_name}")
checksum_rows={}
for raw in checksum_path.read_text().splitlines():
 if not raw.strip(): continue
 match=re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", raw)
 if not match: raise SystemExit("invalid course bundle checksum line")
 digest,path=match.groups()
 if path in checksum_rows: raise SystemExit(f"duplicate course bundle checksum path: {path}")
 checksum_rows[path]=digest.lower()
expected_checksum_paths={item["path"] if not item["path"].startswith("data/") else "course-bundle/"+item["path"] for item in checked}
if set(checksum_rows) != expected_checksum_paths: raise SystemExit("course bundle checksum entries do not match manifest")
for item in checked:
 checksum_rel=item["path"] if not item["path"].startswith("data/") else "course-bundle/"+item["path"]
 if checksum_rows[checksum_rel] != item["sha256"]: raise SystemExit(f"course bundle checksum mismatch: {checksum_rel}")
contract=data.get("runtime_contract") or {}
for key in ("architecture","device_model","driver_version","firmware_version","cann_version","acl_version","python_abi","mindspore_version","numpy","scipy","safetensors"):
 if not str(contract.get(key) or "").strip(): raise SystemExit(f"course bundle runtime contract missing: {key}")
result={"schema":data["schema"],"bundle_revision":data.get("bundle_revision"),"artifact_revision":data.get("artifact_revision"),"files":checked,"file_count":len(checked),"data_unpacked_size":data_spec["unpacked_size"],"data_files":data_files,"checksum_file":checksum_name,"runtime_contract":contract}
print(json.dumps(result,ensure_ascii=False,separators=(",",":")))
PY
} 2>&1)"; then
  echo "$REPORT" >&2
  exit 1
fi
if [[ "$JSON" == true ]]; then
  printf '%s\n' "$REPORT"
elif [[ "$QUIET" != true ]]; then
  python3 - "$REPORT" <<'PY'
import json,sys
v=json.loads(sys.argv[1]); print(f"course bundle verified: {v['bundle_revision']} ({v['file_count']} files; artifact {v['artifact_revision']})")
for item in v["files"]: print(f"  {item['path']} {item['size']} bytes sha256={item['sha256']}")
print(f"  MNIST unpacked bytes={v['data_unpacked_size']} checksum={v['checksum_file']}")
PY
fi