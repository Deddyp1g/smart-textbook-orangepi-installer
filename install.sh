#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
SERVER_URL="${SMART_TEXTBOOK_AGENT_SERVER_URL:-}"
ENROLLMENT_TOKEN="${SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN:-}"
DEVICE_ID="${SMART_TEXTBOOK_AGENT_DEVICE_ID:-}"
DISPLAY_NAME="${SMART_TEXTBOOK_AGENT_DISPLAY_NAME:-Orange Pi}"
VERIFY_TLS="${SMART_TEXTBOOK_AGENT_VERIFY_TLS:-true}"
POLL_SECONDS="${SMART_TEXTBOOK_AGENT_ENROLLMENT_POLL_SECONDS:-5}"
TIMEOUT_SECONDS="${SMART_TEXTBOOK_AGENT_ENROLLMENT_TIMEOUT_SECONDS:-1800}"
SOURCE_DIR="${SMART_TEXTBOOK_AGENT_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
PREFIX="${SMART_TEXTBOOK_AGENT_PREFIX:-/opt/smart-textbook/runtime-agent}"
ETC_DIR="${SMART_TEXTBOOK_AGENT_ETC_DIR:-/etc/smart-textbook}"
STATE_DIR="${SMART_TEXTBOOK_AGENT_STATE_DIR:-/var/lib/smart-textbook/runtime-agent}"
SYSTEMD_DIR="${SMART_TEXTBOOK_AGENT_SYSTEMD_DIR:-/etc/systemd/system}"
PROFILE_DIR="${SMART_TEXTBOOK_AGENT_PROFILE_DIR:-/etc/profile.d}"
SHARED_LABS_DIR="${SMART_TEXTBOOK_AGENT_SHARED_LABS_DIR:-/usr/local/share/reference-labs}"
COURSE_BUNDLE_ROOT="${SMART_TEXTBOOK_AGENT_COURSE_BUNDLE_ROOT:-/usr/local/share/smart-textbook/course-bundle}"
COURSE_DATA_ROOT="${SMART_TEXTBOOK_AGENT_COURSE_DATA_ROOT:-/usr/local/share/smart-textbook/data/mnist}"
STUDENT_HOME="${SMART_TEXTBOOK_AGENT_STUDENT_HOME:-/home/student}"
PYTHON="${SMART_TEXTBOOK_AGENT_PYTHON:-/usr/bin/python3}"
WHEELHOUSE="${SMART_TEXTBOOK_AGENT_WHEELHOUSE:-}"
HOST_STACK_MODE="${SMART_TEXTBOOK_AGENT_HOST_STACK_MODE:-auto}"
HOST_STACK_STATE_DIR="${SMART_TEXTBOOK_AGENT_HOST_STACK_STATE_DIR:-$STATE_DIR/host-stack}"
HOST_STACK_CACHE_DIR="${SMART_TEXTBOOK_AGENT_HOST_STACK_CACHE_DIR:-/var/cache/smart-textbook/orangepi/host-stack}"
HOST_STACK_MANIFEST="${SMART_TEXTBOOK_AGENT_HOST_STACK_MANIFEST:-}"
KEY_PATH="${SMART_TEXTBOOK_AGENT_PRIVATE_KEY:-${ETC_DIR}/runtime-agent.ed25519}"
ENV_PATH="${SMART_TEXTBOOK_AGENT_ENV_FILE:-${ETC_DIR}/runtime-agent.env}"
SERVICE_NAME="smart-textbook-runtime-agent.service"
NO_START=false
PAIR_ONLY=false
REBIND=false
STAGE=arguments
WORKSPACE_IMAGE=""
WORKSPACE_BASE_ARCHIVE=""
VERIFY_NPU=false
STAGED=""
cleanup() { [[ -z "$STAGED" ]] || rm -rf -- "$STAGED"; }
trap cleanup EXIT
trap 'echo "Orange Pi installation failed during $STAGE (line $LINENO); inspect this stage before rerunning" >&2' ERR
usage() { cat <<'EOF'
Usage: install.sh --server URL --pairing-code CODE [--pair-only] [--rebind] [--device-id ID] [--display-name NAME] [--insecure] [--timeout SECONDS] [--wheelhouse DIR] [--source-dir DIR] [--no-start] [--host-stack MODE]
--pair-only waits for server approval without deploying course, host stack or runtime.
--rebind explicitly allows replacing an existing server after the new server approves.
--enrollment-token is an alias for --pairing-code. Missing URL/code prompt only on a terminal.
--no-start installs without starting/enabling the agent service; Docker must already be available.
--host-stack never|verify|auto controls host driver/firmware/CANN provisioning (default: auto).
--workspace-image sha256:ID uses a verified local ARM64 workspace image instead of building it.
--workspace-base-archive FILE builds using a local copy of the verified Ubuntu Base archive.
--verify-npu runs an optional real Ascend and MindSpore container acceptance after installation.
Agent dependencies use the server's locked ARM64/CP310 wheelhouse; --wheelhouse overrides it with an offline directory.
EOF
}
while (($#)); do
  case "$1" in
    --server) SERVER_URL="${2:?missing URL}"; shift 2 ;;
    --enrollment-token|--pairing-code) ENROLLMENT_TOKEN="${2:?missing token}"; shift 2 ;;
    --device-id) DEVICE_ID="${2:?missing device id}"; shift 2 ;;
    --display-name) DISPLAY_NAME="${2:?missing display name}"; shift 2 ;;
    --insecure) VERIFY_TLS=false; shift ;;
    --timeout) TIMEOUT_SECONDS="${2:?missing timeout}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?missing source dir}"; shift 2 ;;
    --wheelhouse) WHEELHOUSE="${2:?missing wheelhouse}"; shift 2 ;;
    --host-stack) HOST_STACK_MODE="${2:?missing host-stack mode}"; shift 2 ;;
    --pair-only) PAIR_ONLY=true; shift ;;
    --rebind) REBIND=true; shift ;;
    --no-start) NO_START=true; shift ;;
    --workspace-image) WORKSPACE_IMAGE="${2:?missing workspace image ID}"; shift 2 ;;
    --workspace-base-archive) WORKSPACE_BASE_ARCHIVE="${2:?missing Ubuntu Base archive}"; shift 2 ;;
    --verify-npu) VERIFY_NPU=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option" >&2; usage >&2; exit 2 ;;
  esac
done
if [[ -n "$WORKSPACE_IMAGE" && ! "$WORKSPACE_IMAGE" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "--workspace-image requires an immutable local sha256 image ID" >&2; exit 2
fi
if [[ -n "$WORKSPACE_IMAGE" && -n "$WORKSPACE_BASE_ARCHIVE" ]]; then
  echo "choose --workspace-image or --workspace-base-archive" >&2; exit 2
fi
if [[ "$PAIR_ONLY" == false && ( "$PREFIX" != /opt/smart-textbook/runtime-agent || "$STATE_DIR" != /var/lib/smart-textbook/runtime-agent ) ]]; then
  echo "the container adapter requires the standard agent prefix and state directory" >&2; exit 2
fi
[[ "$EUID" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ARCH="$(uname -m)"
[[ "$ARCH" == aarch64 || "$ARCH" == arm64 ]] || { echo "unsupported architecture: $ARCH" >&2; exit 1; }
if [[ -z "$SERVER_URL" ]]; then
  [[ -t 0 ]] || { echo "--server is required in non-interactive mode" >&2; exit 2; }
  read -r -p "Server URL: " SERVER_URL
fi
if [[ -z "$ENROLLMENT_TOKEN" ]]; then
  [[ -t 0 ]] || { echo "--pairing-code is required in non-interactive mode" >&2; exit 2; }
  read -r -s -p "Pairing code: " ENROLLMENT_TOKEN; echo >&2
fi
[[ -n "$SERVER_URL" && -n "$ENROLLMENT_TOKEN" ]] || { echo "server and pairing code cannot be empty" >&2; exit 2; }
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ && "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "timeout and poll seconds must be positive integers" >&2; exit 2; }
[[ "$VERIFY_TLS" == true || "$VERIFY_TLS" == false ]] || { echo "verify TLS must be true or false" >&2; exit 2; }
[[ "$HOST_STACK_MODE" == never || "$HOST_STACK_MODE" == verify || "$HOST_STACK_MODE" == auto ]] || { echo "host-stack must be never, verify, or auto" >&2; exit 2; }
SERVER_URL=${SERVER_URL%/}
if [[ "$PAIR_ONLY" == false && "$NO_START" == false && ! -d "${SMART_TEXTBOOK_AGENT_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}" ]]; then
  echo "systemd is not running; use --no-start for rootfs verification, then start the installed service after boot" >&2; exit 1
fi
STAGE="server pairing"
[[ -f "$SOURCE_DIR/pair-server.py" && -f "$SOURCE_DIR/preflight.py" ]] || { echo "pairing helpers missing; download the complete installer release package" >&2; exit 1; }
if [[ "$PAIR_ONLY" == false ]]; then
  required_files=(bootstrap-dependencies.sh agent-requirements.lock install-workspace.sh install-course-bundle.sh verify-bundle.sh provision-host-stack.py sync-runtime-assets.py smart-textbook-runtime-agent.py smart-textbook-runtime-agent.service pty-sandbox-launcher scrub-generation terminate-generation device/scoped_container.py device/npu-scoped-ready device/Dockerfile device/ascend.bashrc)
  for file in "${required_files[@]}"; do
    [[ -f "$SOURCE_DIR/$file" && ! -L "$SOURCE_DIR/$file" ]] || { echo "missing installer file $file; use the complete release package" >&2; exit 1; }
  done
fi
# Only these transport/identity tools may be installed before approval.
if [[ ! -x "$PYTHON" ]] || ! command -v openssl >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends python3 openssl ca-certificates
fi
export PAIR_SERVER="$SERVER_URL" PAIR_TOKEN="$ENROLLMENT_TOKEN"
"$PYTHON" "$SOURCE_DIR/pair-server.py" --validate-only
STAGE="device identity"
if [[ -z "$DEVICE_ID" ]]; then DEVICE_ID="$(tr -d '[:space:]' < /etc/machine-id 2>/dev/null || true)"; [[ -n "$DEVICE_ID" ]] || DEVICE_ID="$(hostname)"; DEVICE_ID="orangepi-$DEVICE_ID"; fi
[[ "$DEVICE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] || { echo "invalid device id" >&2; exit 1; }
install -d -m 0700 "$ETC_DIR" "$STATE_DIR"
if [[ ! -s "$KEY_PATH" ]]; then tmp="$KEY_PATH.tmp.$BASHPID"; openssl genpkey -algorithm ED25519 -out "$tmp" >/dev/null 2>&1; chmod 600 "$tmp"; mv -f "$tmp" "$KEY_PATH"; fi
chmod 600 "$KEY_PATH"
PUBLIC_KEY="$(openssl pkey -in "$KEY_PATH" -pubout -outform DER 2>/dev/null | tail -c 32 | base64 | tr -d '\n' | tr '+/' '-_' | tr -d '=')"
[[ "${#PUBLIC_KEY}" -eq 43 ]] || { echo "could not derive Ed25519 public key" >&2; exit 1; }
export PAIR_DEVICE="$DEVICE_ID" PAIR_KEY="$KEY_PATH" PAIR_STATE="$STATE_DIR" PAIR_ENV="$ENV_PATH" PAIR_NAME="$DISPLAY_NAME" PAIR_TLS="$VERIFY_TLS" PAIR_TIMEOUT="$TIMEOUT_SECONDS" PAIR_POLL="$POLL_SECONDS"
PAIR_ARGS=(); [[ "$REBIND" == false ]] || PAIR_ARGS+=(--rebind)
"$PYTHON" "$SOURCE_DIR/pair-server.py" "${PAIR_ARGS[@]}" >/dev/null
if [[ "$PAIR_ONLY" == true ]]; then echo "Server pairing approved for $DEVICE_ID"; exit 0; fi
STAGE="base dependencies"
BASE_PACKAGES=(ca-certificates curl openssl python3 python3-venv tar zstd util-linux passwd coreutils systemd)
[[ -x /usr/bin/docker ]] || BASE_PACKAGES+=(docker.io docker-buildx)
missing=()
for package in "${BASE_PACKAGES[@]}"; do
  [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" == 'install ok installed' ]] || missing+=("$package")
done
if ((${#missing[@]})); then
  command -v apt-get >/dev/null || { echo "missing base dependencies; automatic installation requires Ubuntu apt-get" >&2; exit 1; }
  echo "Installing missing base dependencies: ${missing[*]}" >&2
  APT_ARGS=(-o DPkg::Lock::Timeout=120 -o Acquire::Retries=0 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)
  if ! DEBIAN_FRONTEND=noninteractive apt-get "${APT_ARGS[@]}" update; then
    echo "base dependencies: apt update failed; check DNS, default route and configured Ubuntu mirror" >&2; exit 1
  fi
  if ! DEBIAN_FRONTEND=noninteractive apt-get "${APT_ARGS[@]}" install -y --no-install-recommends "${missing[@]}"; then
    echo "base dependencies: apt install failed; check mirror connectivity and free disk space, then rerun" >&2; exit 1
  fi
fi
for cmd in curl openssl tar zstd systemctl setpriv flock timeout; do command -v "$cmd" >/dev/null || { echo "$cmd unavailable after base dependency installation" >&2; exit 1; }; done
[[ -x "$PYTHON" ]] || { echo "system Python unavailable after dependency installation" >&2; exit 1; }
STAGE="workspace image"
if [[ "$NO_START" == false ]] && ! env -i PATH=/usr/bin:/bin /usr/bin/docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1; then
  systemctl enable --now docker.service
fi
WORKSPACE_IMAGE="$(bash "$SOURCE_DIR/install-workspace.sh" "$SOURCE_DIR" "$STATE_DIR" "$WORKSPACE_IMAGE" "$WORKSPACE_BASE_ARCHIVE")"
STAGE="course bundle"
# The release must carry its own verified course payload.  The helper records a
# persistent phase marker and repairs any interrupted copy on the next run.
install -d -m 0700 "$STATE_DIR"
if [[ -x "$SOURCE_DIR/install-course-bundle.sh" ]]; then
  "$SOURCE_DIR/install-course-bundle.sh" --source-root "$SOURCE_DIR" --bundle-root "$COURSE_BUNDLE_ROOT" --data-root "$COURSE_DATA_ROOT" --state-file "$STATE_DIR/course-bundle-state.json"
else
  echo "course bundle installer is absent; use a release package that includes course-bundle/" >&2
  exit 1
fi
STAGE="host stack provisioning"
install -d -m 0700 "$STATE_DIR" "$HOST_STACK_STATE_DIR" "$HOST_STACK_CACHE_DIR"
# The bootstrap source may live in a temporary directory which is removed by
# the EXIT trap. Persist the host-stack helper before it can schedule a
# reboot-resume unit, so a fresh board can recover after reboot.
HOST_STACK_HELPER="$HOST_STACK_STATE_DIR/provision-host-stack.py"
install -m 0755 "$SOURCE_DIR/provision-host-stack.py" "$HOST_STACK_HELPER"
HOST_FACTS_PATH="$HOST_STACK_STATE_DIR/preflight-facts.json"
if ! "$PYTHON" "$SOURCE_DIR/preflight.py" --pretty > "$HOST_FACTS_PATH"; then
  echo "host-stack: unable to collect host facts; use --host-stack never only when host prerequisites are managed separately" >&2
  exit 1
fi
HOST_ARGS=(--mode "$HOST_STACK_MODE" --server "$SERVER_URL" --enrollment-token "$ENROLLMENT_TOKEN" --state-dir "$HOST_STACK_STATE_DIR" --cache-dir "$HOST_STACK_CACHE_DIR" --facts-file "$HOST_FACTS_PATH")
[[ -z "$HOST_STACK_MANIFEST" ]] || HOST_ARGS+=(--manifest "$HOST_STACK_MANIFEST")
[[ "$VERIFY_TLS" == true ]] || HOST_ARGS+=(--insecure)
set +e
HOST_RESULT="$("$PYTHON" "$HOST_STACK_HELPER" "${HOST_ARGS[@]}" 2>&1)"
HOST_STATUS=$?
set -e
printf '%s\n' "$HOST_RESULT" >&2
HOST_REPORTED_STATUS="$("$PYTHON" -c 'import json,sys; value=json.load(sys.stdin); print(value.get("status",""))' <<<"$HOST_RESULT" 2>/dev/null || true)"
if [[ "$HOST_REPORTED_STATUS" == reboot_required ]]; then
  echo "host-stack installed packages that require reboot; reboot, then smart-textbook-host-stack-resume.service will continue from the verified cache" >&2
  exit 75
fi
if ((HOST_STATUS == 75)); then
  echo "host-stack installed packages that require reboot; reboot, then smart-textbook-host-stack-resume.service will continue from the verified cache" >&2
  exit 75
fi
if ((HOST_STATUS != 0)); then
  echo "host-stack provisioning did not complete; inspect $HOST_STACK_STATE_DIR/host-stack-state.json and rerun with the same enrollment token" >&2
  exit "$HOST_STATUS"
fi
STAGE="agent dependencies"
install -d -m 0700 "$ETC_DIR" "$STATE_DIR"
# Students must traverse this directory to reach fixed experiment entrypoints.
install -d -m 0755 "$PREFIX"
SMART_TEXTBOOK_AGENT_SOURCE_DIR="$SOURCE_DIR" SMART_TEXTBOOK_AGENT_PREFIX="$PREFIX" SMART_TEXTBOOK_AGENT_PYTHON="$PYTHON" SMART_TEXTBOOK_AGENT_WHEELHOUSE="$WHEELHOUSE" SMART_TEXTBOOK_AGENT_SERVER_URL="$SERVER_URL" SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN="$ENROLLMENT_TOKEN" SMART_TEXTBOOK_AGENT_VERIFY_TLS="$VERIFY_TLS" bash "$SOURCE_DIR/bootstrap-dependencies.sh"
AGENT_PYTHON="$PREFIX/venv/bin/python"
CANN_ENV=${SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT:-}
if [[ -z "$CANN_ENV" ]]; then
  for candidate in /usr/local/Ascend/ascend-toolkit/set_env.sh /usr/local/Ascend/ascend-toolkit/latest/set_env.sh; do [[ ! -f "$candidate" ]] || { CANN_ENV=$candidate; break; }; done
fi
load_runtime_environment() {
  set +u
  if [[ -n "$CANN_ENV" && -f "$CANN_ENV" ]]; then . "$CANN_ENV"; fi
  set -u
  if [[ -f "$PREFIX/releases/current/runtime.env" ]]; then
    set -a; . "$PREFIX/releases/current/runtime.env"; set +a
  fi
}
load_runtime_environment
STAGE=enrollment
# Refresh hardware facts after approved host provisioning, preserving server approval.
READY_JSON="$("$PYTHON" "$SOURCE_DIR/pair-server.py" "${PAIR_ARGS[@]}" --ready)"
# Extract separately: empty optional fields must not shift positional credentials.
json_field() { "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1" <<<"$READY_JSON"; }
ARTIFACT_TOKEN=$(json_field artifact_token); ARTIFACT_REVISION=$(json_field artifact_revision)
ARTIFACT_SERVER=$(json_field artifact_server_url); SIGNER_PUBLIC_KEY=$(json_field artifact_signer_public_key)
[[ -n "$ARTIFACT_TOKEN" && -n "$ARTIFACT_REVISION" && -n "$SIGNER_PUBLIC_KEY" ]] || { echo "incomplete signed artifact credentials" >&2; exit 1; }
[[ -n "$ARTIFACT_SERVER" ]] || ARTIFACT_SERVER="$SERVER_URL"
STAGE="agent and student initialization"
install -d -m 0755 "$PREFIX/experiments" "$SHARED_LABS_DIR" "$SYSTEMD_DIR" "$PROFILE_DIR"
for file in mindspore-training mindspore-evaluate mindspore-infer; do install -m 0755 "$SOURCE_DIR/experiments/$file" "$PREFIX/experiments/$file"; done
install -m 0644 "$SOURCE_DIR/experiments/catalog.json" "$PREFIX/experiments/catalog.json"
install -m 0644 "$SOURCE_DIR/reference-labs/train-lenet.py" "$SHARED_LABS_DIR/train-lenet.py"
if ! id student >/dev/null 2>&1; then useradd --create-home --home-dir "$STUDENT_HOME" --shell /bin/bash student; fi
install -d -m 0700 "$STUDENT_HOME" "$STUDENT_HOME/lab_reference_mindspore"
chown student:student "$STUDENT_HOME" "$STUDENT_HOME/lab_reference_mindspore"
export SMART_TEXTBOOK_AGENT_SERVER_URL="$SERVER_URL" SMART_TEXTBOOK_AGENT_VERIFY_TLS="$VERIFY_TLS" SMART_TEXTBOOK_AGENT_PRIVATE_KEY="$KEY_PATH" SMART_TEXTBOOK_AGENT_DEVICE_ID="$DEVICE_ID" SMART_TEXTBOOK_AGENT_ARTIFACT_SERVER_URL="$ARTIFACT_SERVER" SMART_TEXTBOOK_AGENT_ARTIFACT_TOKEN="$ARTIFACT_TOKEN" SMART_TEXTBOOK_AGENT_ARTIFACT_ROOT="$PREFIX/releases" SMART_TEXTBOOK_AGENT_ARTIFACT_SIGNING_PUBLIC_KEY="$SIGNER_PUBLIC_KEY"
STAGED="$(mktemp -d "$STATE_DIR/installer.XXXXXX")"
"$PYTHON" - "$STAGED/runtime-agent.env" "$CANN_ENV" <<'PY'
import os,shlex,sys
from pathlib import Path
keys=['SERVER_URL','VERIFY_TLS','PRIVATE_KEY','DEVICE_ID','ARTIFACT_SERVER_URL','ARTIFACT_TOKEN','ARTIFACT_ROOT','ARTIFACT_SIGNING_PUBLIC_KEY']
values={'SMART_TEXTBOOK_AGENT_'+key:os.environ['SMART_TEXTBOOK_AGENT_'+key] for key in keys}
values.update(SMART_TEXTBOOK_AGENT_HEARTBEAT_SECONDS='10',SMART_TEXTBOOK_AGENT_ARTIFACT_SYNC='true',SMART_TEXTBOOK_AGENT_LOG_LEVEL='INFO',SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT=sys.argv[2])
p=Path(sys.argv[1]); p.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items())); p.chmod(0o600)
PY
"$PYTHON" - "$PREFIX" "$ENV_PATH" "$STAGED/$SERVICE_NAME" "$SOURCE_DIR/$SERVICE_NAME" "$STAGED" <<'PY'
import shlex,sys
from pathlib import Path
prefix,env_path,unit_path,template,stage=sys.argv[1:]
p=shlex.quote(prefix); env=shlex.quote(env_path)
wrapper=Path(stage)/'start-agent.sh'
wrapper.write_text('#!/usr/bin/env bash\nset -Eeuo pipefail\nset -a\n. '+env+'\nset +u\nif [[ -n "${SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT:-}" && -f "$SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT" ]]; then . "$SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT"; fi\n. '+p+'/releases/current/runtime.env\nset -u\nset +a\nexport SMART_TEXTBOOK_AGENT_PINNED_SOURCE=true\nexec '+p+'/venv/bin/python -B '+p+'/smart-textbook-runtime-agent.py\n')
wrapper.chmod(0o755)
text=Path(template).read_text().replace('EnvironmentFile=-/etc/smart-textbook/runtime-agent.env','EnvironmentFile='+env_path).replace('ExecStart=/opt/smart-textbook/runtime-agent/start-agent.sh','ExecStart='+prefix+'/start-agent.sh')
Path(unit_path).write_text(text); Path(unit_path).chmod(0o644)
PY
STAGE="runtime artifact synchronization"
SYNC_ARGS=(--server "$ARTIFACT_SERVER" --device-id "$DEVICE_ID" --root "$PREFIX/releases" --install-userland --expected-revision "$ARTIFACT_REVISION")
[[ "$VERIFY_TLS" == true ]] || SYNC_ARGS+=(--insecure)
# Sync validates installed userland before atomically selecting the new release.
"$AGENT_PYTHON" -B "$SOURCE_DIR/sync-runtime-assets.py" "${SYNC_ARGS[@]}"
load_runtime_environment
STAGE="runtime preflight"
"$PYTHON" - "$PREFIX/releases/current/manifest.json" "$ARTIFACT_REVISION" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    if json.load(stream).get('artifact_revision') != sys.argv[2]:
        raise SystemExit('signed runtime selection changed or activation was deferred')
PY
"$PYTHON" -B "$SOURCE_DIR/preflight.py" --manifest "$PREFIX/releases/current/manifest.json"
# Runtime assets are root-owned and readable/executable by the student sandbox.
chmod o+rx "$PREFIX" "$PREFIX/experiments" "$SHARED_LABS_DIR"
STAGE="workspace activation"
CONTROL="$STATE_DIR/container-workspaces"
[[ ! -L "$CONTROL" && ! -L "$CONTROL/lock" ]] || { echo "untrusted workspace control path" >&2; exit 1; }
install -d -m 0700 "$CONTROL"
exec 9>"$CONTROL/lock"
flock -n 9 || { echo "workspace activity in progress; retry after the board is idle" >&2; exit 1; }
MANAGED="$(env -i PATH=/usr/bin:/bin /usr/bin/docker --host=unix:///var/run/docker.sock container ls --all --quiet --filter label=smart-textbook.workspace-generation)"
if [[ -e "$CONTROL/active.json" || -n "$MANAGED" ]]; then
  echo "workspace lease still active; release it before updating the agent" >&2; exit 1
fi
printf '%s\n' "$WORKSPACE_IMAGE" > "$STAGED/workspace-image-id"
install -d -m 0700 "$STAGED/previous"
TARGETS=()
replace_file() {
  local source="$1" target="$2" mode="$3" temp
  temp="$(mktemp "$(dirname -- "$target")/.installer.XXXXXX")"
  install -m "$mode" "$source" "$temp" || { rm -f -- "$temp"; return 1; }
  mv -Tf -- "$temp" "$target" || { rm -f -- "$temp"; return 1; }
}
rollback() {
  local status="$1" i target backup temp failed=false
  trap - ERR
  set +e
  for ((i=${#TARGETS[@]}-1; i>=0; i--)); do
    target="${TARGETS[i]}"; backup="$STAGED/previous/$i"
    if [[ -e "$backup" || -L "$backup" ]]; then
      temp="$(mktemp "$(dirname -- "$target")/.installer-restore.XXXXXX")" &&
        cp -aT -- "$backup" "$temp" && mv -Tf -- "$temp" "$target" || failed=true
    else
      rm -f -- "$target" || failed=true
    fi
  done
  if [[ "$NO_START" == false ]]; then
    systemctl daemon-reload || failed=true
    if [[ "$WAS_ACTIVE" == true ]]; then
      systemctl restart "$SERVICE_NAME" || failed=true
    else
      systemctl stop "$SERVICE_NAME" || failed=true
    fi
  fi
  if [[ "$failed" == false ]]; then
    echo "Orange Pi agent update failed during $STAGE; previous agent files restored" >&2
  else
    echo "Orange Pi agent update failed during $STAGE and rollback needs inspection: $STAGED/previous" >&2
    trap - EXIT
  fi
  exit "$status"
}
activate_file() {
  local source="$1" target="$2" mode="$3" index="${#TARGETS[@]}"
  [[ ! -e "$target" && ! -L "$target" ]] || cp -a -- "$target" "$STAGED/previous/$index"
  TARGETS+=("$target")
  replace_file "$source" "$target" "$mode"
}
WAS_ACTIVE=false
if systemctl is-active --quiet "$SERVICE_NAME"; then
  [[ "$NO_START" == false ]] || { echo "stop the installed agent before using --no-start" >&2; exit 1; }
  WAS_ACTIVE=true
fi
trap 'rollback "$?"' ERR
for file in smart-textbook-runtime-agent.py sync-runtime-assets.py preflight.py pty-sandbox-launcher terminate-generation scrub-generation; do
  activate_file "$SOURCE_DIR/$file" "$PREFIX/$file" 0755
done
activate_file "$SOURCE_DIR/device/scoped_container.py" "$PREFIX/scoped_container.py" 0644
activate_file "$SOURCE_DIR/device/npu-scoped-ready" "$PREFIX/npu-scoped-ready" 0755
activate_file "$STAGED/workspace-image-id" "$PREFIX/workspace-image-id" 0644
activate_file "$STAGED/runtime-agent.env" "$ENV_PATH" 0600
activate_file "$STAGED/start-agent.sh" "$PREFIX/start-agent.sh" 0755
if [[ -f "$PREFIX/start-scoped-agent.sh" ]]; then
  activate_file "$STAGED/start-agent.sh" "$PREFIX/start-scoped-agent.sh" 0755
fi
activate_file "$STAGED/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME" 0644
if [[ "$NO_START" == true ]]; then
  flock -u 9
  trap 'echo "Orange Pi installation failed during $STAGE (line $LINENO); inspect this stage before rerunning" >&2' ERR
  echo "Orange Pi enrollment, artifact sync and preflight completed for $DEVICE_ID; service startup deferred (--no-start)"
  if [[ "$VERIFY_NPU" == true ]]; then
    STAGE="container NPU verification"
    "$AGENT_PYTHON" -B "$PREFIX/scoped_container.py" --verify-npu
  fi
  exit 0
fi
STAGE="service startup"
systemctl daemon-reload
STARTED="$(date +%s)"
timeout 50 systemctl restart "$SERVICE_NAME"
authenticated=false
for ((check=0; check<210; check++)); do
  PID="$(systemctl show "$SERVICE_NAME" --property=MainPID --value)"
  if [[ "$PID" =~ ^[1-9][0-9]*$ ]] &&
     journalctl -q -b -u "$SERVICE_NAME" "_PID=$PID" --since "@$STARTED" -o cat --no-pager |
       grep -E 'INFO authenticated generation=[0-9]+$' >/dev/null; then
    authenticated=true
    break
  fi
  sleep 1
done
[[ "$authenticated" == true ]]
systemctl enable "$SERVICE_NAME"
flock -u 9
trap 'echo "Orange Pi installation failed during $STAGE (line $LINENO); inspect this stage before rerunning" >&2' ERR
echo "Orange Pi enrollment, artifact sync, preflight, and agent startup completed for $DEVICE_ID"
if [[ "$VERIFY_NPU" == true ]]; then
  STAGE="container NPU verification"
  "$AGENT_PYTHON" -B "$PREFIX/scoped_container.py" --verify-npu
fi
