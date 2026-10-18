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
STAGE=arguments
BOOTSTRAP_DIR=""
cleanup() { [[ -z "$BOOTSTRAP_DIR" ]] || rm -rf -- "$BOOTSTRAP_DIR"; }
trap cleanup EXIT
trap 'echo "Orange Pi installation failed during $STAGE (line $LINENO); inspect this stage before rerunning" >&2' ERR
usage() { cat <<'EOF'
Usage: install.sh --server URL --enrollment-token TOKEN [--device-id ID] [--display-name NAME] [--insecure] [--timeout SECONDS] [--wheelhouse DIR] [--source-dir DIR] [--no-start] [--host-stack MODE]
--no-start installs and verifies the runtime without starting/enabling systemd (for chroot/rootfs acceptance).
--host-stack never|verify|auto controls host driver/firmware/CANN provisioning (default: auto).
Agent dependencies use the server's locked ARM64/CP310 wheelhouse; --wheelhouse overrides it with an offline directory.
EOF
}
while (($#)); do
  case "$1" in
    --server) SERVER_URL="${2:?missing URL}"; shift 2 ;;
    --enrollment-token) ENROLLMENT_TOKEN="${2:?missing token}"; shift 2 ;;
    --device-id) DEVICE_ID="${2:?missing device id}"; shift 2 ;;
    --display-name) DISPLAY_NAME="${2:?missing display name}"; shift 2 ;;
    --insecure) VERIFY_TLS=false; shift ;;
    --timeout) TIMEOUT_SECONDS="${2:?missing timeout}"; shift 2 ;;
    --source-dir) SOURCE_DIR="${2:?missing source dir}"; shift 2 ;;
    --wheelhouse) WHEELHOUSE="${2:?missing wheelhouse}"; shift 2 ;;
    --host-stack) HOST_STACK_MODE="${2:?missing host-stack mode}"; shift 2 ;;
    --no-start) NO_START=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option" >&2; usage >&2; exit 2 ;;
  esac
done
PACKAGE_SOURCE_DIR="$SOURCE_DIR"
[[ "$EUID" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ARCH="$(uname -m)"
[[ "$ARCH" == aarch64 || "$ARCH" == arm64 ]] || { echo "unsupported architecture: $ARCH" >&2; exit 1; }
[[ -n "$SERVER_URL" && -n "$ENROLLMENT_TOKEN" ]] || { usage >&2; exit 2; }
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ && "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "timeout and poll seconds must be positive integers" >&2; exit 2; }
[[ "$VERIFY_TLS" == true || "$VERIFY_TLS" == false ]] || { echo "verify TLS must be true or false" >&2; exit 2; }
[[ "$HOST_STACK_MODE" == never || "$HOST_STACK_MODE" == verify || "$HOST_STACK_MODE" == auto ]] || { echo "host-stack must be never, verify, or auto" >&2; exit 2; }
SERVER_URL=${SERVER_URL%/}
if [[ "$NO_START" == false && ! -d "${SMART_TEXTBOOK_AGENT_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}" ]]; then
  echo "systemd is not running; use --no-start for rootfs verification, then start the installed service after boot" >&2; exit 1
fi
STAGE="base dependencies"
# This block intentionally precedes HTTP bootstrap: clean Ubuntu images may lack curl.
BASE_PACKAGES=(ca-certificates curl openssl python3 python3-venv tar zstd util-linux passwd coreutils systemd)
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
for cmd in curl openssl tar zstd systemctl setpriv; do command -v "$cmd" >/dev/null || { echo "$cmd unavailable after base dependency installation" >&2; exit 1; }; done
[[ -x "$PYTHON" ]] || { echo "system Python unavailable after dependency installation" >&2; exit 1; }
CURL_ARGS=(--fail --silent --show-error --connect-timeout 20 --max-time 60)
[[ "$VERIFY_TLS" == true ]] || CURL_ARGS+=(--insecure)
STAGE="bootstrap source files"
bootstrap_files=(bootstrap-dependencies.sh agent-requirements.lock preflight.py sync-runtime-assets.py smart-textbook-runtime-agent.py smart-textbook-runtime-agent.service pty-sandbox-launcher scrub-generation terminate-generation requirements.txt runtime-manifest.schema.json provision-host-stack.py install-course-bundle.sh verify-bundle.sh course-bundle/manifest.json course-bundle/SHA256SUMS course-bundle/data/mnist-verified.tar.zst experiments/mindspore-training experiments/mindspore-evaluate experiments/mindspore-infer reference-labs/train-lenet.py)
need_bootstrap=0
for file in "${bootstrap_files[@]}"; do [[ -f "$SOURCE_DIR/$file" ]] || { need_bootstrap=1; break; }; done
if ((need_bootstrap)); then
  BOOTSTRAP_DIR="$(mktemp -d /tmp/smart-textbook-agent.XXXXXX)"
  SOURCE_DIR="$BOOTSTRAP_DIR"
  for file in "${bootstrap_files[@]}"; do
    mkdir -p "$SOURCE_DIR/$(dirname "$file")"
    curl "${CURL_ARGS[@]}" "$SERVER_URL/api/runtime-agent/bootstrap/$file" -H "X-Runtime-Agent-Enrollment-Token: $ENROLLMENT_TOKEN" -o "$SOURCE_DIR/$file"
  done
fi
COURSE_SOURCE_DIR="$PACKAGE_SOURCE_DIR"
if [[ ! -x "$COURSE_SOURCE_DIR/install-course-bundle.sh" || ! -f "$COURSE_SOURCE_DIR/course-bundle/manifest.json" ]]; then
  COURSE_SOURCE_DIR="$SOURCE_DIR"
fi
STAGE="course bundle"
# The release must carry its own verified course payload.  The helper records a
# persistent phase marker and repairs any interrupted copy on the next run.
install -d -m 0700 "$STATE_DIR"
if [[ -x "$COURSE_SOURCE_DIR/install-course-bundle.sh" ]]; then
  "$COURSE_SOURCE_DIR/install-course-bundle.sh" --source-root "$COURSE_SOURCE_DIR" --bundle-root "$COURSE_BUNDLE_ROOT" --data-root "$COURSE_DATA_ROOT" --state-file "$STATE_DIR/course-bundle-state.json"
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
STAGE="device identity"
if [[ -z "$DEVICE_ID" ]]; then DEVICE_ID="$(tr -d '[:space:]' < /etc/machine-id 2>/dev/null || true)"; [[ -n "$DEVICE_ID" ]] || DEVICE_ID="$(hostname)"; DEVICE_ID="orangepi-$DEVICE_ID"; fi
[[ "$DEVICE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] || { echo "invalid device id" >&2; exit 1; }
if [[ ! -s "$KEY_PATH" ]]; then tmp="$KEY_PATH.tmp.$BASHPID"; openssl genpkey -algorithm ED25519 -out "$tmp" >/dev/null 2>&1; chmod 600 "$tmp"; mv -f "$tmp" "$KEY_PATH"; fi
chmod 600 "$KEY_PATH"
PUBLIC_KEY="$(openssl pkey -in "$KEY_PATH" -pubout -outform DER 2>/dev/null | tail -c 32 | base64 | tr -d '\n' | tr '+/' '-_' | tr -d '=')"
[[ "${#PUBLIC_KEY}" -eq 43 ]] || { echo "could not derive Ed25519 public key" >&2; exit 1; }
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
FACTS_JSON="$("$PYTHON" "$SOURCE_DIR/preflight.py" --pretty)"
PAYLOAD="$("$PYTHON" - "$DEVICE_ID" "$DISPLAY_NAME" "$PUBLIC_KEY" "$FACTS_JSON" <<'PY'
import json,sys
device_id,display_name,public_key,raw=sys.argv[1:]
facts=json.loads(raw)
print(json.dumps({'device_id':device_id,'display_name':display_name,'public_key':public_key,'capabilities':{'provider':'orangepi','runtime_profile':'ascend-npu','artifact_sync_required':True,'facts':facts}},separators=(',',':')))
PY
)"
STAGE=enrollment
ENROLL_RESPONSE="$(curl "${CURL_ARGS[@]}" -X POST "$SERVER_URL/api/runtime-agent/enroll" -H 'Content-Type: application/json' -H "X-Runtime-Agent-Enrollment-Token: $ENROLLMENT_TOKEN" --data "$PAYLOAD")"
ENROLLMENT_ID="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("enrollment_id",""))' <<<"$ENROLL_RESPONSE")"
[[ -n "$ENROLLMENT_ID" ]] || { echo "server did not return enrollment_id" >&2; exit 1; }
DEADLINE="$(( $(date +%s) + TIMEOUT_SECONDS ))"; READY_JSON=""
while (( $(date +%s) < DEADLINE )); do
  STATUS_RESPONSE="$(curl "${CURL_ARGS[@]}" "$SERVER_URL/api/runtime-agent/enroll/$DEVICE_ID" -H "X-Runtime-Agent-Enrollment-Token: $ENROLLMENT_TOKEN" 2>/dev/null || true)"
  STATUS="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' <<<"$STATUS_RESPONSE" 2>/dev/null || true)"
  case "$STATUS" in
    ready) READY_JSON="$STATUS_RESPONSE"; break ;;
    approved_waiting_for_compatible_artifact|approved_waiting_for_artifact_secret) echo "enrollment approved; waiting for runtime artifact" >&2 ;;
    pending|approved|"") ;;
    *) echo "enrollment is not ready" >&2 ;;
  esac
  sleep "$POLL_SECONDS"
done
[[ -n "$READY_JSON" ]] || { echo "enrollment not approved/prepared before timeout" >&2; exit 1; }
# Extract separately: empty optional fields must not shift positional credentials.
json_field() { "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1" <<<"$READY_JSON"; }
ARTIFACT_TOKEN=$(json_field artifact_token); ARTIFACT_REVISION=$(json_field artifact_revision)
ARTIFACT_SERVER=$(json_field artifact_server_url); SIGNER_PUBLIC_KEY=$(json_field artifact_signer_public_key)
[[ -n "$ARTIFACT_TOKEN" && -n "$ARTIFACT_REVISION" ]] || { echo "incomplete artifact credentials" >&2; exit 1; }
if [[ -f "$COURSE_SOURCE_DIR/course-bundle/manifest.json" ]]; then
  EXPECTED_ARTIFACT_REVISION="$($PYTHON - "$COURSE_SOURCE_DIR/course-bundle/manifest.json" <<'PY'
import json,sys
try:
 print(str(json.loads(open(sys.argv[1]).read()).get("artifact_revision") or ""))
except Exception:
 print("")
PY
)"
  if [[ -n "$EXPECTED_ARTIFACT_REVISION" && "$ARTIFACT_REVISION" != "$EXPECTED_ARTIFACT_REVISION" ]]; then
    echo "server artifact $ARTIFACT_REVISION does not match this installer's course bundle $EXPECTED_ARTIFACT_REVISION; download the matching release" >&2
    exit 1
  fi
fi
[[ -n "$ARTIFACT_SERVER" ]] || ARTIFACT_SERVER="$SERVER_URL"
STAGE="agent and student initialization"
for file in smart-textbook-runtime-agent.py sync-runtime-assets.py preflight.py pty-sandbox-launcher terminate-generation scrub-generation; do install -m 0755 "$SOURCE_DIR/$file" "$PREFIX/$file"; done
install -d -m 0755 "$PREFIX/experiments" "$SHARED_LABS_DIR" "$SYSTEMD_DIR" "$PROFILE_DIR"
for file in mindspore-training mindspore-evaluate mindspore-infer; do install -m 0755 "$SOURCE_DIR/experiments/$file" "$PREFIX/experiments/$file"; done
install -m 0644 "$SOURCE_DIR/reference-labs/train-lenet.py" "$SHARED_LABS_DIR/train-lenet.py"
if ! id student >/dev/null 2>&1; then useradd --create-home --home-dir "$STUDENT_HOME" --shell /bin/bash student; fi
install -d -m 0700 "$STUDENT_HOME" "$STUDENT_HOME/lab_reference_mindspore"
chown student:student "$STUDENT_HOME" "$STUDENT_HOME/lab_reference_mindspore"
# Add existing device groups; never grant broad permissions to all host devices.
for group in HwHiAiUser davinci render video; do
  if getent group "$group" >/dev/null 2>&1; then usermod -a -G "$group" student; fi
done
export SMART_TEXTBOOK_AGENT_SERVER_URL="$SERVER_URL" SMART_TEXTBOOK_AGENT_VERIFY_TLS="$VERIFY_TLS" SMART_TEXTBOOK_AGENT_PRIVATE_KEY="$KEY_PATH" SMART_TEXTBOOK_AGENT_DEVICE_ID="$DEVICE_ID" SMART_TEXTBOOK_AGENT_ARTIFACT_SERVER_URL="$ARTIFACT_SERVER" SMART_TEXTBOOK_AGENT_ARTIFACT_TOKEN="$ARTIFACT_TOKEN" SMART_TEXTBOOK_AGENT_ARTIFACT_ROOT="$PREFIX/releases" SMART_TEXTBOOK_AGENT_ARTIFACT_SIGNING_PUBLIC_KEY="$SIGNER_PUBLIC_KEY"
"$PYTHON" - "$ENV_PATH" "$CANN_ENV" <<'PY'
import os,shlex,sys
from pathlib import Path
keys=['SERVER_URL','VERIFY_TLS','PRIVATE_KEY','DEVICE_ID','ARTIFACT_SERVER_URL','ARTIFACT_TOKEN','ARTIFACT_ROOT','ARTIFACT_SIGNING_PUBLIC_KEY']
values={'SMART_TEXTBOOK_AGENT_'+key:os.environ['SMART_TEXTBOOK_AGENT_'+key] for key in keys}
values.update(SMART_TEXTBOOK_AGENT_HEARTBEAT_SECONDS='10',SMART_TEXTBOOK_AGENT_ARTIFACT_SYNC='true',SMART_TEXTBOOK_AGENT_LOG_LEVEL='INFO',SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT=sys.argv[2])
p=Path(sys.argv[1]); p.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items())); p.chmod(0o600)
PY
"$PYTHON" - "$PREFIX" "$ENV_PATH" "$SYSTEMD_DIR/$SERVICE_NAME" "$SOURCE_DIR/$SERVICE_NAME" <<'PY'
import shlex,sys
from pathlib import Path
prefix,env_path,unit_path,template=sys.argv[1:]
p=shlex.quote(prefix); env=shlex.quote(env_path)
wrapper=Path(prefix)/'start-agent.sh'
wrapper.write_text('#!/usr/bin/env bash\nset -Eeuo pipefail\nset -a\n. '+env+'\nset +u\nif [[ -n "${SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT:-}" && -f "$SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT" ]]; then . "$SMART_TEXTBOOK_AGENT_CANN_ENV_SCRIPT"; fi\nset -u\n. '+p+'/releases/current/runtime.env\nset +a\nexec '+p+'/venv/bin/python '+p+'/smart-textbook-runtime-agent.py\n')
wrapper.chmod(0o755)
text=Path(template).read_text().replace('EnvironmentFile=-/etc/smart-textbook/runtime-agent.env','EnvironmentFile='+env_path).replace('ExecStart=/opt/smart-textbook/runtime-agent/start-agent.sh','ExecStart='+prefix+'/start-agent.sh')
Path(unit_path).write_text(text); Path(unit_path).chmod(0o644)
PY
STAGE="runtime artifact synchronization"
SYNC_ARGS=(--server "$ARTIFACT_SERVER" --device-id "$DEVICE_ID" --root "$PREFIX/releases" --install-userland)
[[ "$VERIFY_TLS" == true ]] || SYNC_ARGS+=(--insecure)
# Sync validates installed userland before atomically selecting the new release.
"$AGENT_PYTHON" "$PREFIX/sync-runtime-assets.py" "${SYNC_ARGS[@]}"
load_runtime_environment
STAGE="runtime preflight"
"$PYTHON" "$PREFIX/preflight.py" --manifest "$PREFIX/releases/current/manifest.json"
# Runtime assets are root-owned and readable/executable by the student sandbox.
chmod o+rx "$PREFIX" "$PREFIX/experiments" "$SHARED_LABS_DIR"
if [[ "$NO_START" == true ]]; then
  echo "Orange Pi enrollment, artifact sync and preflight completed for $DEVICE_ID; service startup deferred (--no-start)"
  exit 0
fi
STAGE="service startup"
systemctl daemon-reload
systemctl restart "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME" || { echo "agent service did not become active; inspect journalctl -u $SERVICE_NAME" >&2; exit 1; }
systemctl enable "$SERVICE_NAME"
echo "Orange Pi enrollment, artifact sync, preflight, and agent startup completed for $DEVICE_ID"
