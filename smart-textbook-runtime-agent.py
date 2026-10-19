#!/usr/bin/env python3
from __future__ import annotations
import base64, ctypes, json, errno, fcntl, logging, os, platform, pty, re, secrets, select, signal, socket, struct, subprocess, sys, termios, threading, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Pin the loaded source before releases/current can be atomically replaced.
AGENT_SOURCE_PATH = Path(__file__).resolve()
VERSION = "0.2.0"
NAMESPACE = "/runtime-agent"
MAX_BACKOFF = 60.0
LOG = logging.getLogger("smart-textbook-runtime-agent")
ARTIFACT_STATE = {"status": "unknown"}
RUNTIME_ENVIRONMENT = {}
LAST_ARTIFACT_SYNC = 0.0
ALLOWED_RPCS = frozenset({"probe", "npu_smoke_test", "scrub", "terminate_generation", "execute_experiment", "pty"})
PTY_SANDBOX_LAUNCHER = "/opt/smart-textbook/runtime-agent/pty-sandbox-launcher"
PTY_LAUNCHER_TOKEN = "fixed:smart-textbook-pty-sandbox"
MAX_PTY_SESSIONS = 2
MAX_PTY_READ = 65536
MAX_PTY_WRITE = 65536
FIXED_EXPERIMENTS = {
    "reference_mindspore": {
        "training": "mindspore-training",
        "evaluate": "mindspore-evaluate",
        "infer": "mindspore-infer",
    }
}


def utcnow():
    return datetime.now(timezone.utc)


def b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def challenge_message(device_id, nonce, generation, issued_at):
    parsed = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    body = {
        "connection_generation": int(generation),
        "device_id": str(device_id),
        "issued_at": parsed.astimezone(timezone.utc).isoformat(),
        "nonce": b64(nonce),
        "protocol": "runtime-agent/v1",
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def server_origin(value, field="server_url", required=True):
    if not value or not value.strip():
        if required:
            raise ValueError(f"{field}_not_configured: device is not paired; pair the device or set its local environment configuration")
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and "?" not in value and "#" not in value
                 and not any(ch.isspace() or ord(ch) < 32 for ch in value)
                 and "\\" not in value)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{field}_invalid: expected an http(s) server origin without credentials, path, query or fragment")
    return value.rstrip("/")


def read_config():
    return {
        "server_url": server_origin(os.getenv("SMART_TEXTBOOK_AGENT_SERVER_URL", "")),
        "verify_tls": os.getenv("SMART_TEXTBOOK_AGENT_VERIFY_TLS", "true").lower() == "true",
        "private_key": Path(os.getenv("SMART_TEXTBOOK_AGENT_PRIVATE_KEY", "/etc/smart-textbook/runtime-agent.ed25519")),
        "device_id": os.getenv("SMART_TEXTBOOK_AGENT_DEVICE_ID", ""),
        "heartbeat_seconds": max(5.0, float(os.getenv("SMART_TEXTBOOK_AGENT_HEARTBEAT_SECONDS", "10"))),
        "artifact_server_url": server_origin(os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SERVER_URL", ""), "artifact_server_url", required=False),
        "artifact_token": os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_TOKEN", "").strip(),
        "artifact_public_key": os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SIGNING_PUBLIC_KEY", "").strip(),
        "artifact_sync_timeout": max(300.0, float(os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SYNC_TIMEOUT", "7200"))),
        "artifact_sync_interval": max(30.0, float(os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SYNC_INTERVAL", "300"))),
        "artifact_root": os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_ROOT", "/opt/smart-textbook/runtime-agent/releases"),
        "artifact_sync": os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SYNC", "true").lower() in ("1", "true", "yes", "on"),
    }


def runtime_process_environment(runtime=None):
    env = dict(os.environ)
    for key, value in (RUNTIME_ENVIRONMENT if runtime is None else runtime).items():
        env[key] = value + (os.pathsep + env[key] if key in {"PYTHONPATH", "LD_LIBRARY_PATH", "PATH"} and env.get(key) else "")
    return env


def load_runtime_environment(root):
    current = Path(root).resolve() / "current"
    release = current.resolve()
    if not current.is_symlink() or Path(root).resolve() not in release.parents:
        raise RuntimeError("runtime_current_invalid")
    value = json.loads((release / "runtime-environment.json").read_text())
    allowed = {"PYTHONPATH", "LD_LIBRARY_PATH", "PATH", "ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "ASCEND_OPP_PATH", "ASCEND_AICPU_PATH", "SMART_TEXTBOOK_DATASET_ROOT", "SMART_TEXTBOOK_RUNTIME_HELPERS_ROOT", "SMART_TEXTBOOK_RUNTIME_ROOT", "SMART_TEXTBOOK_RUNTIME_REVISION", "PYTHONDONTWRITEBYTECODE", "OPP_ROOT", "TBE_IMPL_PATH"}
    if not isinstance(value, dict) or any(key not in allowed or not isinstance(item, str) or "\x00" in item for key, item in value.items()):
        raise RuntimeError("runtime_environment_invalid")
    # Experiment wrappers live under the immutable release and use this prefix
    # to locate releases/current/runtime.env when launched by the agent.
    value["SMART_TEXTBOOK_AGENT_PREFIX"] = str(Path(root).resolve().parent)
    return value


def runtime_facts():
    """Collect experiments' system-Python facts, never the isolated agent venv."""
    try:
        report = subprocess.run(["/usr/bin/python3", str(Path(__file__).with_name("preflight.py"))], env=runtime_process_environment(), capture_output=True, text=True, timeout=90, check=False)
        return json.loads(report.stdout)
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}


def cached_artifact_state(config):
    # Return a previously verified release without rehashing its full tree.
    try:
        root = Path(config.get("artifact_root") or "").resolve()
        current = root / "current"
        release = current.resolve()
        if not current.is_symlink() or root not in release.parents:
            return None
        receipt = json.loads((release / ".installation.json").read_text())
        manifest = json.loads((release / "manifest.json").read_text())
        revision = str(manifest.get("artifact_revision") or "")
        expected = str(config.get("artifact_revision") or "")
        if receipt.get("phase") != "ready" or not revision or (expected and revision != expected):
            return None
        environment = load_runtime_environment(root)
        bundle = manifest.get("course_bundle")
        course_bundle = (
            {"status": "ready", "bundle_revision": str(bundle.get("bundle_revision") or bundle.get("revision") or ""),
             "file_count": len(bundle.get("files") or [])}
            if isinstance(bundle, dict) else {"status": "legacy_unmanaged"}
        )
        catalog = manifest.get("experiments")
        experiment_catalog = (
            {"status": "ready", "experiment_count": len(catalog)}
            if isinstance(catalog, dict) else {"status": "legacy_fixed"}
        )
        return {
            "status": "ready",
            "artifact_revision": revision,
            "cache_verified": True,
            "cached": True,
            "course_bundle": course_bundle,
            "experiment_catalog": experiment_catalog,
        }, environment
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def sync_runtime_assets(config, device_id, publish=True):
    global ARTIFACT_STATE, RUNTIME_ENVIRONMENT, LAST_ARTIFACT_SYNC
    if not config.get("artifact_sync") or not config.get("artifact_server_url"):
        value = {"status": "skipped", "reason": "artifact_sync_not_configured"}
        if publish:
            ARTIFACT_STATE = value
        return value
    # Reconnects within this process retain the result of the last real validation.
    # No state is promoted from offline_cached/error to ready by this throttle.
    if publish and LAST_ARTIFACT_SYNC and time.monotonic() - LAST_ARTIFACT_SYNC < config.get("artifact_sync_interval", 300):
        return dict(ARTIFACT_STATE)
    script = Path(__file__).with_name("sync-runtime-assets.py")
    if not script.is_file():
        value = {"status": "error", "error": "artifact_sync_script_missing"}
        if publish:
            ARTIFACT_STATE = value
        return value
    try:
        command = [sys.executable, str(script), "--server", config["artifact_server_url"], "--device-id", device_id, "--root", config["artifact_root"], "--install-userland"]
        if not config.get("verify_tls", True):
            command.append("--insecure")
        # Credentials stay in the child environment rather than process argv.
        child_env = runtime_process_environment()
        child_env["SMART_TEXTBOOK_AGENT_ARTIFACT_TOKEN"] = config.get("artifact_token", "")
        child_env["SMART_TEXTBOOK_AGENT_ARTIFACT_SIGNING_PUBLIC_KEY"] = config.get("artifact_public_key", "")
        result = subprocess.run(command, env=child_env, capture_output=True, text=True, timeout=config.get("artifact_sync_timeout", 7200), check=False)
        output = (result.stdout or "").strip().splitlines()
        value = json.loads(output[-1]) if output else {}
        if result.returncode != 0:
            # Preserve actionable sync failures (especially disk pressure) so
            # the server heartbeat/UI can show the root cause instead of a
            # generic permanent syncing state.
            detail = str(value.get("detail") or "")
            code = detail.split(":", 1)[0] if detail else ""
            known = code.startswith(("artifact_", "course_bundle_"))
            value.update({"status": "error", "error": code if known else "artifact_sync_failed"})
        elif value.get("status") == "deferred":
            current = cached_artifact_state({**config, "artifact_revision": ""})
            if current is not None:
                active, environment = current
                value = {**active, "update_pending": True,
                         "desired_artifact_revision": value.get("desired_artifact_revision") or config.get("artifact_revision"),
                         "reason": value.get("reason") or "runtime_in_use"}
        elif value.get("status") in {"synced", "offline_cached"}:
            environment = load_runtime_environment(config["artifact_root"])
            # The sync helper has already checked package versions and preflight
            # using this exact environment before atomically switching current.
            if value.get("status") == "synced" and value.get("compatibility", {}).get("ok") is True:
                value["status"] = "ready"
            elif value.get("status") == "synced":
                value.update({"status": "incompatible", "error": "runtime_preflight_failed"})
        else:
            value = {"status": "error", "error": "artifact_sync_invalid_response"}
        state = {key: value[key] for key in ("status", "artifact_revision", "error", "detail", "reason", "compatibility", "cache_verified", "requires_server_revalidation", "cached", "storage", "course_bundle", "experiment_catalog", "update_pending", "desired_artifact_revision") if key in value}
        if publish:
            ARTIFACT_STATE = state
            LAST_ARTIFACT_SYNC = time.monotonic()
            if "environment" in locals():
                RUNTIME_ENVIRONMENT = environment
        elif "environment" in locals():
            state["_environment"] = environment
        return state
    except Exception as exc:
        state = {"status": "error", "error": "artifact_sync_exception", "detail": type(exc).__name__}
        if publish:
            ARTIFACT_STATE = state
            LAST_ARTIFACT_SYNC = time.monotonic()
        return state


def load_private_key(path):
    raw = path.read_bytes()
    if raw.startswith(b"-----BEGIN"):
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private_key_not_ed25519")
        return key
    if len(raw) != 32:
        raise ValueError("raw_ed25519_key_must_be_32_bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def command_output(command, timeout=10, environment=None):
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, env=runtime_process_environment(environment))
        return {"exit_code": result.returncode, "stdout": result.stdout[-4096:], "stderr": result.stderr[-4096:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout": "", "stderr": "timeout"}
    except OSError as exc:
        return {"exit_code": None, "stdout": "", "stderr": str(exc)[:512]}


def acl_smoke_test():
    """Run ACL initialization in a clean child process.

    The resident agent must not keep an ACL context between probes.
    """
    probe = r"""
import ctypes, json
lib = ctypes.CDLL('libascendcl.so', mode=ctypes.RTLD_GLOBAL)
acl_init = lib.aclInit; acl_init.restype = ctypes.c_int32; acl_init.argtypes = [ctypes.c_char_p]
get_count = lib.aclrtGetDeviceCount; get_count.restype = ctypes.c_int32; get_count.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
set_device = lib.aclrtSetDevice; set_device.restype = ctypes.c_int32; set_device.argtypes = [ctypes.c_int32]
create_context = lib.aclrtCreateContext; create_context.restype = ctypes.c_int32; create_context.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int32]
destroy_context = lib.aclrtDestroyContext; destroy_context.restype = ctypes.c_int32; destroy_context.argtypes = [ctypes.c_void_p]
reset_device = lib.aclrtResetDevice; reset_device.restype = ctypes.c_int32; reset_device.argtypes = [ctypes.c_int32]
finalize = lib.aclFinalize; finalize.restype = ctypes.c_int32; finalize.argtypes = []
initialized = False; device_set = False; context = ctypes.c_void_p()
try:
    ret = int(acl_init(None))
    if ret != 0: print(json.dumps({'status':'error','error':'npu_acl_init_failed','acl_ret':ret})); raise SystemExit
    initialized = True
    count = ctypes.c_uint32(0); ret = int(get_count(ctypes.byref(count)))
    if ret != 0 or count.value < 1: print(json.dumps({'status':'error','error':'npu_acl_device_count_failed','acl_ret':ret,'device_count':int(count.value)})); raise SystemExit
    ret = int(set_device(0))
    if ret != 0: print(json.dumps({'status':'error','error':'npu_acl_set_device_failed','acl_ret':ret,'device_count':int(count.value)})); raise SystemExit
    device_set = True
    ret = int(create_context(ctypes.byref(context), 0))
    if ret != 0: print(json.dumps({'status':'error','error':'npu_acl_create_context_failed','acl_ret':ret,'device_count':int(count.value)})); raise SystemExit
    print(json.dumps({'status':'ok','device_count':int(count.value),'device_id':0}))
finally:
    if context.value: destroy_context(context)
    if device_set: reset_device(0)
    if initialized: finalize()
"""
    try:
        result = subprocess.run(['/usr/bin/python3', '-c', probe], capture_output=True, text=True, timeout=15, cwd='/home/student', env=runtime_process_environment())
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "npu_acl_probe_timeout"}
    except OSError as exc:
        return {"status": "error", "error": "npu_acl_probe_exception", "detail": str(exc)[:512]}
    output = (result.stdout or '').strip().splitlines()
    if output:
        try:
            value = json.loads(output[-1])
            if isinstance(value, dict):
                try:
                    Path('/var/lib/smart-textbook/runtime-agent/last-acl-probe.json').write_text(json.dumps(value, sort_keys=True))
                except OSError:
                    pass
                return value
        except json.JSONDecodeError:
            pass
    return {"status": "error", "error": "npu_acl_probe_failed", "acl_ret": result.returncode, "detail": (result.stderr or '')[-512:]}


def telemetry():
    data = {"hostname": socket.gethostname(), "arch": platform.machine(), "agent_version": VERSION}
    npu_smi = next((candidate for candidate in ("/usr/bin/npu-smi", "/usr/local/sbin/npu-smi", "/usr/local/bin/npu-smi") if os.path.exists(candidate)), "npu-smi")
    data["npu"] = command_output([npu_smi, "info"], 5)
    data["acl_device"] = command_output(["/bin/sh", "-c", "test -e /dev/davinci0"], 2)
    data["runtime"] = runtime_facts()
    data["artifact_sync"] = dict(ARTIFACT_STATE)
    try:
        stat = os.statvfs("/")
        data["disk_free_bytes"] = stat.f_bavail * stat.f_frsize
    except OSError:
        pass
    try:
        data["temperature_c"] = round(float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    return data


class SocketIOPolling:
    def __init__(self, base_url, verify_tls):
        self.base_url = base_url.rstrip("/") + "/socket.io/"
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.post_session = requests.Session()
        self.post_lock = threading.Lock()
        self.sid = None

    def _url(self):
        return self.base_url + "?" + urlencode({"EIO": "4", "transport": "polling", "sid": self.sid} if self.sid else {"EIO": "4", "transport": "polling", "t": str(time.time_ns())})

    def connect(self):
        opened = self.session.get(self._url(), timeout=20, verify=self.verify_tls)
        opened.raise_for_status()
        packet = opened.text
        if not packet.startswith("0"):
            raise RuntimeError("engineio_open_missing")
        details = json.loads(packet[1:].split("", 1)[0])
        self.sid = details["sid"]
        self._post("40" + NAMESPACE + ",")
        self.poll(timeout=10)

    def _post(self, packet):
        # Long-poll reads and background RPC/heartbeat writes use separate
        # Sessions. Engine.IO allows only one outstanding POST at a time.
        with self.post_lock:
            if not self.sid:
                raise RuntimeError("engineio_session_closed")
            response = self.post_session.post(self._url(), data=packet.encode(), headers={"Content-Type": "text/plain;charset=UTF-8"}, timeout=20, verify=self.verify_tls)
            response.raise_for_status()

    def emit(self, event, payload):
        packet = "42" + NAMESPACE + "," + json.dumps([event, payload], separators=(",", ":"))
        self._post(packet)

    def poll(self, timeout=30):
        response = self.session.get(self._url(), timeout=timeout + 10, verify=self.verify_tls)
        response.raise_for_status()
        for packet in response.text.split(""):
            if not packet:
                continue
            if packet == "2":
                self._post("3")
                continue
            if packet.startswith("4"):
                message = packet[1:]
                if message.startswith("2" + NAMESPACE + ","):
                    data = json.loads(message.split(",", 1)[1])
                    yield data[0], data[1] if len(data) > 1 else {}
                elif message.startswith("1" + NAMESPACE):
                    raise RuntimeError("socketio_namespace_closed")

    def close(self):
        if self.sid:
            try:
                self._post("41" + NAMESPACE + ",")
            except Exception:
                pass
            self.sid = None
        self.session.close()
        with self.post_lock:
            self.post_session.close()


class RuntimeAgent:
    def __init__(self, config):
        self.config = config
        self.key = load_private_key(config["private_key"])
        self.device_id = config["device_id"] or self._default_device_id()
        self.public_key = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.transport = None
        self.generation = None
        self.authenticated = False
        self.pty_sessions = {}
        self._ensure_background_state()

    def _ensure_background_state(self):
        if hasattr(self, "_state_lock"):
            return
        self._state_lock = threading.RLock()
        self._connection_epoch = 0
        self._sync_thread = None
        self._sync_requested = None
        self._rpc_slots = threading.BoundedSemaphore(4)
        self._telemetry_thread = None
        self._heartbeat_stop = threading.Event()
        self._telemetry_cache = {"hostname": socket.gethostname(), "arch": platform.machine(), "agent_version": VERSION}

    def _connection_matches(self, epoch, generation, transport):
        return self.authenticated and self._connection_epoch == epoch and self.generation == generation and self.transport is transport

    def _acquire_runtime_lock(self):
        """Serialize student execution/PTY lifetimes with artifact activation."""
        root = Path(self.config.get("artifact_root") or "/opt/smart-textbook/runtime-agent/releases").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / ".sync.lock"
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
        except Exception:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_runtime_lock(fd):
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _apply_artifact_access(self, access):
        if not isinstance(access, dict):
            return False
        changed = False
        for source, target in (
            ("artifact_token", "artifact_token"),
            ("artifact_server_url", "artifact_server_url"),
            ("artifact_signer_public_key", "artifact_public_key"),
            ("artifact_revision", "artifact_revision"),
        ):
            value = access.get(source)
            if isinstance(value, str) and value and self.config.get(target) != value:
                self.config[target] = value
                changed = True
        return changed

    def _schedule_sync(self):
        global ARTIFACT_STATE, RUNTIME_ENVIRONMENT, LAST_ARTIFACT_SYNC
        # A real authenticated transport always uses the background worker so a
        # multi-gigabyte runtime download cannot block Socket.IO heartbeats. The
        # synchronous fallback keeps the handler deterministic for embedders and
        # older unit fixtures that construct RuntimeAgent without a transport.
        transport = getattr(self, "transport", None)
        if transport is None:
            value = sync_runtime_assets(self.config, self.device_id)
            ARTIFACT_STATE = dict(value)
            return
        with self._state_lock:
            if not LAST_ARTIFACT_SYNC:
                cached = cached_artifact_state(self.config)
                if cached is not None:
                    state, environment = cached
                    ARTIFACT_STATE = state
                    RUNTIME_ENVIRONMENT = environment
                    LAST_ARTIFACT_SYNC = time.monotonic()
                    return
            fresh = LAST_ARTIFACT_SYNC and time.monotonic() - LAST_ARTIFACT_SYNC < self.config.get("artifact_sync_interval", 300)
            same_revision = not self.config.get("artifact_revision") or self.config["artifact_revision"] == ARTIFACT_STATE.get("artifact_revision")
            pending_revision = ARTIFACT_STATE.get("desired_artifact_revision")
            if fresh and ARTIFACT_STATE.get("status") == "ready" and (same_revision or (ARTIFACT_STATE.get("update_pending") and pending_revision == self.config.get("artifact_revision"))):
                return
            self._sync_requested = (self._connection_epoch, self.generation, transport, dict(self.config))
            if ARTIFACT_STATE.get("status") == "ready":
                ARTIFACT_STATE = {**ARTIFACT_STATE, "update_pending": True,
                                  "desired_artifact_revision": self.config.get("artifact_revision")}
            else:
                active = cached_artifact_state({**self.config, "artifact_revision": ""})
                if active is not None:
                    state, environment = active
                    ARTIFACT_STATE = {**state, "update_pending": True,
                                      "desired_artifact_revision": self.config.get("artifact_revision")}
                    RUNTIME_ENVIRONMENT = environment
                else:
                    ARTIFACT_STATE = {"status": "syncing"}
            if self._sync_thread is not None:
                return
            self._sync_thread = threading.Thread(target=self._sync_worker, name="runtime-sync", daemon=True)
            self._sync_thread.start()

    def _maybe_reload_agent(self, value):
        """Adopt a verified agent only while no student holds its runtime."""
        if value.get("status") != "ready":
            return
        root = Path(self.config.get("artifact_root") or "").resolve()
        lock_fd = os.open(str(root / ".sync.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            candidate = root / "current" / "smart-textbook-runtime-agent.py"
            if not candidate.is_file() or candidate.resolve() == AGENT_SOURCE_PATH:
                return
            LOG.info("restarting from verified runtime agent %s", candidate)
            os.execv(sys.executable, [sys.executable, str(candidate)])
        except OSError:
            LOG.warning("verified runtime agent restart failed")
        finally:
            os.close(lock_fd)

    def _sync_worker(self):
        global ARTIFACT_STATE, RUNTIME_ENVIRONMENT, LAST_ARTIFACT_SYNC
        while True:
            with self._state_lock:
                request = self._sync_requested
                self._sync_requested = None
                if request is None:
                    self._sync_thread = None
                    return
            epoch, generation, transport, config = request
            try:
                value = sync_runtime_assets(config, self.device_id, publish=False)
            except Exception as exc:
                value = {"status": "error", "error": "artifact_sync_exception", "detail": type(exc).__name__}
            environment = value.pop("_environment", None)
            with self._state_lock:
                if self._connection_matches(epoch, generation, transport):
                    ARTIFACT_STATE = dict(value)
                    LAST_ARTIFACT_SYNC = time.monotonic()
                    if environment is not None:
                        RUNTIME_ENVIRONMENT = environment
                    LOG.info("runtime artifact sync status=%s revision=%s", value.get("status"), value.get("artifact_revision", ""))
            self._maybe_reload_agent(value)

    def _schedule_rpc(self, payload):
        with self._state_lock:
            epoch, generation, transport = self._connection_epoch, self.generation, self.transport
        if not self._rpc_slots.acquire(blocking=False):
            transport.emit("agent:result", {"request_id": payload.get("request_id"), "device_id": self.device_id, "connection_generation": generation, "status": "error", "error": "rpc_workers_busy"})
            return
        def work():
            try:
                result = self.handle_rpc(payload)
                with self._state_lock:
                    valid = self._connection_matches(epoch, generation, transport)
                if valid:
                    transport.emit("agent:result", result)
            except Exception:
                LOG.warning("background RPC delivery failed")
            finally:
                self._rpc_slots.release()
        threading.Thread(target=work, name="runtime-rpc", daemon=True).start()

    def _schedule_telemetry(self):
        with self._state_lock:
            if self._telemetry_thread is not None:
                return
            def work():
                try:
                    value = telemetry()
                    with self._state_lock:
                        self._telemetry_cache = value
                except Exception:
                    LOG.warning("background telemetry failed")
                finally:
                    with self._state_lock:
                        self._telemetry_thread = None
            self._telemetry_thread = threading.Thread(target=work, name="runtime-telemetry", daemon=True)
            self._telemetry_thread.start()

    def _send_heartbeat(self):
        with self._state_lock:
            if not self.authenticated or self.transport is None:
                return
            epoch, generation, transport = self._connection_epoch, self.generation, self.transport
            data = dict(self._telemetry_cache)
            data["artifact_sync"] = dict(ARTIFACT_STATE)
        self._schedule_telemetry()
        if self._connection_matches(epoch, generation, transport):
            transport.emit("agent:heartbeat", {"device_id": self.device_id, "connection_generation": generation,
                           "timestamp": utcnow().isoformat(), "payload": data})

    def _heartbeat_worker(self):
        while not self._heartbeat_stop.wait(self.config["heartbeat_seconds"]):
            try:
                # Heartbeats also drive artifact discovery. The sync helper has
                # its own interval throttle, so this remains cheap while still
                # detecting a newly published experiment without reconnecting.
                if self.authenticated and self.transport is not None:
                    self._schedule_sync()
                self._send_heartbeat()
            except Exception:
                LOG.warning("heartbeat delivery failed")

    def _default_device_id(self):
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text().strip()
            except OSError:
                continue
            if value:
                return "orangepi-" + value[:96]
        return "orangepi-" + socket.gethostname()[:96]

    def hello(self):
        return {
            "device_id": self.device_id,
            "public_key": b64(self.public_key),
            "agent_version": VERSION,
            "capabilities": {
                "provider": "orangepi",
                "runtime_profile": "ascend-npu",
                "experiment_ids": ["reference_mindspore"],
                "npu": "/dev/davinci0",
                "rpc_allowlist": sorted(ALLOWED_RPCS),
                "facts": runtime_facts(),
                "artifact_sync_required": bool(self.config.get("artifact_sync") and self.config.get("artifact_server_url")),
            },
        }

    def handle_event(self, event, payload):
        self._ensure_background_state()
        if event == "agent:challenge":
            nonce = unb64(payload["challenge"])
            generation = int(payload["connection_generation"])
            signature = self.key.sign(challenge_message(self.device_id, nonce, generation, payload["issued_at"]))
            self.transport.emit("agent:authenticate", {
                "device_id": self.device_id,
                "connection_generation": generation,
                "signature": b64(signature),
            })
        elif event == "agent:authenticated":
            with self._state_lock:
                self._connection_epoch += 1
                self.generation = int(payload["connection_generation"])
                self.authenticated = True
            LOG.info("authenticated generation=%s", self.generation)
            self._apply_artifact_access(payload.get("artifact_access"))
            self._schedule_sync()
        elif event == "agent:heartbeat:ack":
            # The server renews the short-lived artifact token and advertises
            # the latest compatible revision in the heartbeat acknowledgement.
            # A changed revision is picked up on the next worker pass without a
            # reconnect or manual board action.
            if self._apply_artifact_access(payload.get("artifact_access")):
                self._schedule_sync()
        elif event == "agent:rpc":
            if str(payload.get("rpc") or "") == "pty":
                # PTY session state remains exclusively on the polling thread.
                self.transport.emit("agent:result", self.handle_rpc(payload))
            else:
                self._schedule_rpc(payload)

    def handle_rpc(self, payload):
        result = {"request_id": payload.get("request_id"), "device_id": self.device_id, "connection_generation": self.generation}
        rpc = str(payload.get("rpc") or "")
        args = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        if rpc not in ALLOWED_RPCS:
            result.update(status="error", error="rpc_not_allowed")
            return result
        if payload.get("connection_generation") != self.generation:
            result.update(status="error", error="stale_connection_generation")
            return result
        try:
            if rpc == "probe":
                value = telemetry()
            elif rpc == "npu_smoke_test":
                npu_smi = next((candidate for candidate in ("/usr/bin/npu-smi", "/usr/local/sbin/npu-smi", "/usr/local/bin/npu-smi") if os.path.exists(candidate)), "npu-smi")
                npu = command_output([npu_smi, "info"], 15)
                health = command_output([npu_smi, "info", "-t", "health", "-i", "0"], 8)
                value = {"npu": npu, "health": health, "device": command_output(["/bin/sh", "-c", "test -r /dev/davinci0"], 2), "acl": acl_smoke_test()}
                health_text = (health.get("stdout", "") + "\n" + health.get("stderr", "")).lower()
                npu_text = (npu.get("stdout", "") + "\n" + npu.get("stderr", "")).lower()
                device = value["device"]
                if (npu.get("exit_code") not in (None, 0)
                        or health.get("exit_code") not in (None, 0)
                        or device.get("exit_code") not in (None, 0)
                        or value["acl"].get("status") != "ok"):
                    value["status"] = "error"
                    value["error"] = value["acl"].get("error") if value["acl"].get("status") != "ok" else "npu_probe_failed"
                else:
                    warnings = []
                    if "alarm" in health_text or "fault" in health_text:
                        warnings.append("npu_health_alarm")
                    if "power" in npu_text and ("0w" in npu_text or "0 w" in npu_text):
                        warnings.append("npu_power_unavailable")
                    if warnings:
                        value["warnings"] = warnings
            elif rpc == "scrub":
                value = self.fixed_script("scrub-generation", args, 180)
            elif rpc == "terminate_generation":
                value = self.fixed_script("terminate-generation", args, 30)
            elif rpc == "execute_experiment":
                value = self.experiment(args)
            elif rpc == "pty":
                value = self.pty(args)
            else:
                value = {"status": "error", "error": "pty_requires_server_session"}
            result.update(status="ok", result=value)
        except Exception as exc:
            LOG.exception("rpc failed")
            result.update(status="error", error=type(exc).__name__, detail=str(exc)[:512])
        return result

    def fixed_script(self, name, args, timeout):
        generation = str(args.get("generation") or "")
        if not generation or "/" in generation or ".." in generation or len(generation) > 128:
            return {"status": "error", "error": "invalid_generation"}
        path = Path("/opt/smart-textbook/runtime-agent") / name
        if not path.is_file():
            return {"status": "error", "error": "script_missing"}
        return command_output([str(path), generation], timeout)

    def _experiment_catalog(self, release):
        manifest_path = release / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        catalog = manifest.get("experiments")
        if not isinstance(catalog, dict) or not catalog:
            return None
        declared = {
            str(item.get("path"))
            for item in (manifest.get("files") or [])
            if isinstance(item, dict) and item.get("path")
        }
        normalized = {}
        for experiment_id, steps in catalog.items():
            exp_id = str(experiment_id)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", exp_id) or not isinstance(steps, dict):
                return None
            normalized_steps = {}
            for step_name, spec in steps.items():
                step = str(step_name)
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", step):
                    return None
                if isinstance(spec, str):
                    path, timeout = spec, 300
                elif isinstance(spec, dict):
                    path, timeout = str(spec.get("path") or ""), spec.get("timeout_seconds", 300)
                else:
                    return None
                path_obj = Path(path)
                if (
                    not path
                    or path_obj.is_absolute()
                    or "\\" in path
                    or ":" in path
                    or ".." in path_obj.parts
                    or path not in declared
                    or isinstance(timeout, bool)
                    or not isinstance(timeout, int)
                    or timeout < 5
                    or timeout > 900
                ):
                    return None
                normalized_steps[step] = {"path": path, "timeout_seconds": timeout}
            if normalized_steps:
                normalized[exp_id] = normalized_steps
        return normalized or None

    def experiment(self, args):
        lock_fd = self._acquire_runtime_lock()
        try:
            experiment_id = str(args.get("experiment_id") or "")
            step = str(args.get("step") or "")
            root = Path(self.config.get("artifact_root") or "/opt/smart-textbook/runtime-agent/releases").resolve()
            current = root / "current"
            if not current.is_symlink():
                return {"status": "error", "error": "runtime_artifact_not_ready"}
            try:
                release = current.resolve()
            except OSError:
                return {"status": "error", "error": "runtime_artifact_not_ready"}
            if release == root or root not in release.parents:
                return {"status": "error", "error": "runtime_artifact_path_invalid"}

            catalog = self._experiment_catalog(release)
            if catalog is None:
                # v1 artifacts predate the manifest catalog and remain compatible
                # through the original fixed dispatcher.
                spec = FIXED_EXPERIMENTS.get(experiment_id, {}).get(step)
                if spec is None:
                    return {"status": "error", "error": "experiment_step_not_allowlisted"}
                path = release / "experiments" / spec
                timeout = 300
            else:
                entry = catalog.get(experiment_id, {}).get(step)
                if entry is None:
                    return {"status": "error", "error": "experiment_step_not_allowlisted"}
                path = release / entry["path"]
                timeout = entry["timeout_seconds"]

            if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                return {"status": "error", "error": "experiment_entrypoint_missing"}
            environment = load_runtime_environment(root) if (release / "runtime-environment.json").is_file() else None
            return command_output([str(path)], min(900, max(5, int(args.get("timeout_seconds", timeout)))), environment=environment)
        finally:
            self._release_runtime_lock(lock_fd)

    def _pty_session(self, pty_id):
        value = self.pty_sessions.get(str(pty_id))
        if not value:
            raise ValueError("pty_not_found")
        return value

    @staticmethod
    def _safe_generation(value):
        generation = str(value or "")
        if not generation or len(generation) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in generation):
            raise ValueError("invalid_workspace_generation")
        return generation

    @staticmethod
    def _safe_workspace(value):
        workspace = str(value or "/home/student")
        if workspace in {"/home/student", "/home/student/lab_reference_mindspore"}:
            return workspace
        match = re.fullmatch(r"/home/student/lab_assignment_([1-9][0-9]{0,9})", workspace)
        if match is None or int(match.group(1)) > 2147483647:
            raise ValueError("invalid_workspace")
        return workspace

    def _set_pty_size(self, fd, cols, rows):
        cols = max(20, min(int(cols), 240))
        rows = max(10, min(int(rows), 80))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def pty(self, args):
        if args.get("launcher") != PTY_LAUNCHER_TOKEN:
            return {"status": "error", "error": "pty_launcher_not_allowed"}
        action = str(args.get("action") or "")
        if action == "open":
            if len(self.pty_sessions) >= MAX_PTY_SESSIONS:
                return {"status": "error", "error": "pty_limit_reached"}
            generation = self._safe_generation(args.get("workspace_generation"))
            workspace = self._safe_workspace(args.get("workspace"))
            if not os.path.isfile(PTY_SANDBOX_LAUNCHER):
                return {"status": "error", "error": "pty_launcher_missing"}
            lock_fd = self._acquire_runtime_lock()
            master = slave = None
            try:
                root = Path(self.config.get("artifact_root") or "/opt/smart-textbook/runtime-agent/releases").resolve()
                environment = load_runtime_environment(root)
                master, slave = pty.openpty()
                self._set_pty_size(master, args.get("cols", 80), args.get("rows", 24))
                proc = subprocess.Popen(
                    [PTY_SANDBOX_LAUNCHER, "--generation", generation, "--workspace", workspace],
                    stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                    start_new_session=True, env=runtime_process_environment(environment),
                )
                os.close(slave)
                slave = None
            except Exception:
                if slave is not None:
                    os.close(slave)
                if master is not None:
                    os.close(master)
                self._release_runtime_lock(lock_fd)
                raise
            os.set_blocking(master, False)
            pty_id = "pty_" + secrets.token_urlsafe(18)
            self.pty_sessions[pty_id] = {"master": master, "process": proc, "generation": generation, "runtime_lock_fd": lock_fd}
            return {"status": "ok", "pty_id": pty_id}
        pty_id = args.get("pty_id")
        session = self._pty_session(pty_id)
        master, proc = session["master"], session["process"]
        if action == "write":
            encoded = args.get("data")
            if not isinstance(encoded, str):
                return {"status": "error", "error": "pty_data_required"}
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_PTY_WRITE or b"\x00" in raw:
                return {"status": "error", "error": "pty_write_invalid"}
            written = os.write(master, raw)
            return {"status": "ok", "written": written}
        if action == "read":
            timeout = max(0, min(int(args.get("timeout_ms", 100)), 1000)) / 1000.0
            maximum = max(1, min(int(args.get("max_bytes", 4096)), MAX_PTY_READ))
            readable, _, _ = select.select([master], [], [], timeout)
            if readable:
                try:
                    raw = os.read(master, maximum)
                except OSError as exc:
                    if exc.errno not in (errno.EIO, errno.EBADF):
                        raise
                    raw = b""
                if raw:
                    return {"status": "ok", "data": base64.b64encode(raw).decode("ascii"), "eof": False}
            exited = proc.poll()
            if exited is not None:
                self._close_pty(pty_id, terminate=False)
                return {"status": "ok", "data": "", "eof": True, "exit_code": int(exited)}
            return {"status": "ok", "data": "", "eof": False}
        if action == "resize":
            self._set_pty_size(master, args.get("cols", 80), args.get("rows", 24))
            return {"status": "ok"}
        if action == "close":
            self._close_pty(pty_id, terminate=True)
            return {"status": "ok"}
        return {"status": "error", "error": "pty_action_not_allowed"}

    def _close_pty(self, pty_id, terminate=True):
        session = self.pty_sessions.pop(str(pty_id), None)
        if not session:
            return
        proc, master = session["process"], session["master"]
        lock_fd = session.get("runtime_lock_fd")
        if terminate and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
        try:
            os.close(master)
        except OSError:
            pass
        self._release_runtime_lock(lock_fd)

    def run(self):
        self._ensure_background_state()
        threading.Thread(target=self._heartbeat_worker, name="runtime-heartbeat", daemon=True).start()
        backoff = 1.0
        while True:
            try:
                self.transport = SocketIOPolling(self.config["server_url"], self.config["verify_tls"])
                self.transport.connect()
                self.transport.emit("agent:hello", self.hello())
                backoff = 1.0
                while self.transport.sid:
                    for event, payload in self.transport.poll(timeout=25):
                        self.handle_event(event, payload)

            except Exception:
                LOG.exception("connection loop failed")
            finally:
                with self._state_lock:
                    self._connection_epoch += 1
                    old_transport = self.transport
                    self.transport = None
                    self.generation = None
                    self.authenticated = False
                if old_transport:
                    old_transport.close()
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2) * (0.8 + secrets.randbelow(41) / 100)

    def close(self):
        self._ensure_background_state()
        self._heartbeat_stop.set()
        with self._state_lock:
            self._connection_epoch += 1
            self.authenticated = False
            transport = self.transport
        if transport:
            transport.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("SMART_TEXTBOOK_AGENT_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    RuntimeAgent(read_config()).run()
