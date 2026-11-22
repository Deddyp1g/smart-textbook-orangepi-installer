"""One isolated lab container per physical board, with a durable retirement record."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4


PROTOCOL = "orangepi-container-v2"
PREFIX = Path("/opt/smart-textbook/runtime-agent")
CONTROL = Path("/var/lib/smart-textbook/runtime-agent/container-workspaces")
IMAGE_FILE = PREFIX / "workspace-image-id"
ASCEND_DEVICES = (
    ("/dev/davinci0", "/dev/davinci0"),
    ("/dev/davinci_manager_docker", "/dev/davinci_manager"),
    ("/dev/svm0", "/dev/svm0"),
)
GENERATION = re.compile(r"st2-[0-9a-f]{32}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
LABEL = "smart-textbook.workspace-generation"
USER = "20000:20000"
WORKSPACE = "/home/student"


class WorkspaceUnavailable(RuntimeError):
    pass


def is_generation(value):
    return isinstance(value, str) and GENERATION.fullmatch(value) is not None


def docker(*args, timeout=15):
    result = subprocess.run(["/usr/bin/docker", "--host=unix:///var/run/docker.sock", *args],
                            capture_output=True, text=True, check=True, timeout=timeout,
                            env={"PATH": "/usr/bin:/bin", "HOME": "/root"})
    return result.stdout.strip()


def verify_root_path(path, predicate):
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not predicate(info.st_mode):
        raise WorkspaceUnavailable("untrusted_runtime_path")
    return path


def runtime_image():
    path = verify_root_path(IMAGE_FILE, stat.S_ISREG)
    image = path.read_text(encoding="ascii").strip()
    if not image.startswith("sha256:") or not CONTAINER_ID.fullmatch(image[7:]):
        raise WorkspaceUnavailable("workspace_image_invalid")
    details = docker("image", "inspect", "--format={{.Id}} {{.Architecture}} {{.Config.User}}", image)
    if details != f"{image} arm64 {USER}":
        raise WorkspaceUnavailable("workspace_image_invalid")
    return image


@contextmanager
def locked(*, nonblocking=False):
    CONTROL.mkdir(mode=0o700, exist_ok=True)
    info = CONTROL.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkspaceUnavailable("workspace_control_invalid")
    fd = os.open(CONTROL / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def write_record(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=CONTROL, delete=False, encoding="ascii") as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
            sync_control()
        finally:
            temporary.unlink(missing_ok=True)


def sync_control():
    fd = os.open(CONTROL, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_record(path):
    try:
        return json.loads(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return None


def list_managed():
    entries = docker("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}").splitlines()
    if any(not CONTAINER_ID.fullmatch(item) for item in entries):
        raise WorkspaceUnavailable("workspace_inventory_invalid")
    return entries


def container_name(generation):
    return f"textbook-{generation}"


def container(generation):
    name = container_name(generation)
    ids = docker("container", "ls", "--all", "--quiet", "--no-trunc",
                 "--filter", f"name=^/{name}$").splitlines()
    if not ids:
        return None
    if len(ids) != 1 or not CONTAINER_ID.fullmatch(ids[0]):
        raise WorkspaceUnavailable("workspace_container_identity_invalid")
    details, = json.loads(docker("container", "inspect", ids[0]))
    if (details["Id"] != ids[0] or details["Name"] != f"/{name}"
            or details["Config"]["Labels"].get(LABEL) != generation):
        raise WorkspaceUnavailable("workspace_container_identity_invalid")
    return details


def readonly_mount(source, destination=None):
    path = Path(source).resolve(strict=True)
    verify_root_path(path, lambda mode: stat.S_ISDIR(mode) or stat.S_ISREG(mode))
    return ["--mount", f"type=bind,source={path},target={destination or source},readonly"]


def ascend_mounts():
    artifacts = (PREFIX / "releases" / "current").resolve(strict=True)
    if artifacts.parent != PREFIX / "releases" or not (artifacts / "runtime/python/site-packages/mindspore").is_dir():
        raise WorkspaceUnavailable("mindspore_runtime_missing")
    options = readonly_mount(artifacts, "/opt/textbook")
    options += readonly_mount(artifacts / "reference-labs", "/usr/local/share/reference-labs")
    for path in ("/usr/local/Ascend", "/usr/lib64", "/var/davinci/driver/lib64",
                 "/usr/local/sbin/npu-smi", "/etc/ascend_install.info", "/etc/hdcBasic.cfg"):
        options += readonly_mount(path)
    for source, target in ASCEND_DEVICES:
        info = Path(source).stat()
        if not stat.S_ISCHR(info.st_mode):
            raise WorkspaceUnavailable("ascend_device_missing")
        options.extend(["--device", f"{source}:{target}:rw", "--group-add", str(info.st_gid)])
    return options


def npu_status():
    status = subprocess.run([str(PREFIX / "npu-scoped-ready")], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=12, check=False).returncode
    return {0: "ready", 10: "occupied"}.get(status, "unavailable")


def npu_fingerprint(image):
    checks = (
        Path(__file__).resolve(),
        Path("/etc/ascend_install.info"),
        Path("/etc/hdcBasic.cfg"),
        Path("/usr/local/Ascend/driver/lib64/libascend_hal.so"),
        Path("/usr/local/Ascend/ascend-toolkit/latest/version.cfg"),
        PREFIX / "releases/current/runtime/python/site-packages/mindspore/version.py",
    )
    return {
        "image": image,
        "kernel": os.uname().release,
        "devices": [Path(path).stat().st_rdev for path, _ in ASCEND_DEVICES],
        "runtime": [hashlib.sha256(path.read_bytes()).hexdigest() for path in checks],
    }


def npu_verified(image):
    receipt = CONTROL / "npu-verified.json"
    try:
        return read_record(verify_root_path(receipt, stat.S_ISREG)) == npu_fingerprint(image)
    except (FileNotFoundError, ValueError):
        return False


def readiness():
    result = {"cpu": "unavailable", "npu": "unavailable"}
    try:
        with locked(nonblocking=True):
            image = runtime_image()
            if read_record(CONTROL / "active.json") or list_managed():
                return {"cpu": "occupied", "npu": "occupied"}
            result["cpu"] = "ready"
            ascend_mounts()
            status = npu_status()
            if status != "ready":
                result["npu"] = "npu_occupied" if status == "occupied" else status
            elif npu_verified(image):
                result["npu"] = "ready"
            else:
                result["npu"] = "awaiting_validation"
    except BlockingIOError:
        return {"cpu": "occupied", "npu": "occupied"}
    except (OSError, ValueError, subprocess.SubprocessError, WorkspaceUnavailable):
        pass
    return result


def proof(generation, state, **fields):
    return {"protocol": PROTOCOL, "runtime_generation": generation, "state": state, **fields}


def prepare(generation, profile):
    if not is_generation(generation) or profile not in {"cpu", "npu"}:
        raise ValueError("invalid_workspace_identity")
    with locked():
        active_path = CONTROL / "active.json"
        active = read_record(active_path)
        if read_record(CONTROL / f"{generation}.closed.json") is not None:
            return proof(generation, "closed")
        if active and (active.get("generation"), active.get("profile")) != (generation, profile):
            return proof(generation, "unavailable", reason="workspace_slot_occupied")
        image = runtime_image()
        details = container(generation)
        if details is None:
            if active or list_managed():
                raise WorkspaceUnavailable("workspace_create_unconfirmed")
            options = []
            if profile == "npu":
                status = npu_status()
                if status != "ready":
                    return proof(generation, "unavailable", reason=f"npu_{status}")
                if not npu_verified(image):
                    return proof(generation, "unavailable", reason="npu_awaiting_validation")
                options = ascend_mounts()
            active = {"generation": generation, "profile": profile, "container_id": None, "image": image}
            write_record(active_path, active)
            identifier = docker("container", "create", "--name", container_name(generation),
                                "--label", f"{LABEL}={generation}",
                                *workspace_options(profile), *options, image, timeout=30)
            details = container(generation)
            if details is None or details["Id"] != identifier:
                raise WorkspaceUnavailable("workspace_create_unconfirmed")
        if (not active or details["Image"] != active["image"]
                or active.get("container_id") not in (None, details["Id"])):
            raise WorkspaceUnavailable("workspace_container_identity_invalid")
        identifier = details["Id"]
        active["container_id"] = identifier
        write_record(active_path, active)
        if not details["State"]["Running"]:
            if profile == "npu":
                status = npu_status()
                if status != "ready":
                    raise WorkspaceUnavailable(f"npu_{status}")
                if not npu_verified(image):
                    raise WorkspaceUnavailable("npu_awaiting_validation")
            docker("container", "start", identifier, timeout=25)
        state = container(generation)
        if not state or state["Id"] != identifier or not state["State"]["Running"]:
            raise WorkspaceUnavailable("workspace_start_unconfirmed")
        return proof(generation, "ready", profile=profile, container_id=identifier)


def workspace_options(profile):
    executable = ",exec" if profile == "npu" else ""
    memory = "10g" if profile == "npu" else "4g"
    return [
        "--network=none", "--ipc=private", "--init", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=256", "--cpus=3", f"--memory={memory}", f"--memory-swap={memory}",
        "--log-driver=json-file", "--log-opt=max-size=8m", "--log-opt=max-file=2",
        "--tmpfs", f"/tmp:rw,nosuid,nodev{executable},size=384m,mode=1777",
        "--tmpfs", f"/home/student:rw,nosuid,nodev{executable},size=768m,uid=20000,gid=20000,mode=0700",
        "--env", f"SMART_TEXTBOOK_PROFILE={profile}",
    ]


NPU_SMOKE = """set -e
source /etc/smart-textbook-ascend.bashrc
atc --help >/tmp/textbook-atc-help
test -s /tmp/textbook-atc-help
echo "ATC compiler help OK"
python3 -u - <<'PY'
import acl

assert acl.init() == 0
try:
    assert acl.rt.get_device_count() == (1, 0)
    assert acl.rt.set_device(0) == 0
    assert acl.rt.reset_device(0) == 0
finally:
    assert acl.finalize() == 0
print("ACL device 0: init, count, set, reset, finalize OK", flush=True)
PY
python3 -u - <<'PY'
import mindspore as ms
from mindspore import Tensor, ops

ms.set_context(mode=ms.GRAPH_MODE, device_target="Ascend", device_id=0)
value = ops.add(Tensor([1, 2], ms.float32), Tensor([3, 4], ms.float32)).asnumpy()
assert value.tolist() == [4.0, 6.0]
print(f"MindSpore {ms.__version__} Ascend:0: {value.tolist()}", flush=True)
PY
"""


def verify_npu_runtime():
    if os.geteuid() != 0:
        raise WorkspaceUnavailable("root_required_for_npu_verification")
    with locked(nonblocking=True):
        if read_record(CONTROL / "active.json") is not None or list_managed():
            raise WorkspaceUnavailable("workspace_slot_occupied")
        if npu_status() != "ready":
            raise WorkspaceUnavailable("npu_not_idle")
        image = runtime_image()
        options = ascend_mounts()
        receipt = CONTROL / "npu-verified.json"
        receipt.unlink(missing_ok=True)
        sync_control()
        generation = f"st2-{uuid4().hex}"
        name = container_name(generation)
        try:
            output = docker("container", "run", "--name", name, "--label", f"{LABEL}={generation}",
                            *workspace_options("npu"), *options, image, "/bin/bash", "-c", NPU_SMOKE,
                            timeout=300)
        finally:
            state = container(generation)
            if state is not None:
                docker("container", "rm", "--force", "--volumes", state["Id"], timeout=30)
            if container(generation) is not None:
                raise WorkspaceUnavailable("workspace_cleanup_unconfirmed")
        for line in output.splitlines():
            if line.startswith(("ATC", "ACL device", "MindSpore")):
                print(line)
        write_record(receipt, npu_fingerprint(image))
        print("NPU verification recorded for this device and workspace image")


def cleanup(generation, expected_id):
    if not is_generation(generation) or (expected_id is not None and not CONTAINER_ID.fullmatch(expected_id)):
        raise ValueError("invalid_workspace_identity")
    with locked():
        active_path = CONTROL / "active.json"
        closed_path = CONTROL / f"{generation}.closed.json"
        active = read_record(active_path)
        owns_slot = active is not None and active.get("generation") == generation
        closed = read_record(closed_path)
        details = container(generation)
        stored_id = active.get("container_id") if owns_slot else None
        actual_id = details["Id"] if details else None
        identity = stored_id or actual_id or (closed or {}).get("container_id")
        if expected_id is not None and expected_id != identity:
            raise WorkspaceUnavailable("workspace_container_identity_changed")
        if actual_id is not None and (not owns_slot or identity != actual_id or details["Image"] != active["image"]):
            raise WorkspaceUnavailable("workspace_container_identity_changed")
        if owns_slot and identity is None:
            # The daemon might have accepted a create whose response was lost.
            raise WorkspaceUnavailable("workspace_create_unconfirmed")
        completed = proof(generation, "closed", container_id=identity)
        write_record(closed_path, completed)
        if actual_id is not None:
            docker("container", "rm", "--force", "--volumes", actual_id, timeout=30)
        if container(generation) is not None:
            raise WorkspaceUnavailable("workspace_cleanup_unconfirmed")
        if owns_slot:
            active_path.unlink()
            sync_control()
        return completed


def pty_command(generation, directory):
    if not is_generation(generation) or directory not in {WORKSPACE, f"{WORKSPACE}/lab_reference_mindspore"}:
        raise ValueError("invalid_workspace_identity")
    with locked():
        active = read_record(CONTROL / "active.json")
        if not active or active["generation"] != generation or read_record(CONTROL / f"{generation}.closed.json"):
            raise WorkspaceUnavailable("workspace_not_open")
        identity = active.get("container_id")
        details = container(generation)
        if not details or not details["State"]["Running"] or not identity or details["Id"] != identity:
            raise WorkspaceUnavailable("workspace_not_running")
        # An exec racing cleanup uses the immutable container ID. Docker will
        # refuse that exec or stop its process when this exact container dies.
        return ["/usr/bin/docker", "--host=unix:///var/run/docker.sock", "exec", "-it", "--user", USER,
                "--workdir", WORKSPACE, identity, "/bin/bash", "-c",
                'mkdir -p -- "$1" && cd -- "$1" && exec /bin/bash --rcfile /etc/smart-textbook-ascend.bashrc -i',
                "textbook-shell", directory]


if __name__ == "__main__":
    if sys.argv[1:] != ["--verify-npu"]:
        raise SystemExit("usage: scoped_container.py --verify-npu")
    try:
        verify_npu_runtime()
    except subprocess.CalledProcessError as exc:
        raise SystemExit((exc.stderr or exc.output or str(exc))[-4000:])
    except (WorkspaceUnavailable, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
