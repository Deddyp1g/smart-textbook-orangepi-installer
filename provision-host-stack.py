#!/usr/bin/env python3
"""Provision and verify the host-side Ascend stack for an Orange Pi.

The host stack is deliberately separate from the signed userland artifact. A
server manifest may only name fixed package types (deb, Ascend .run, or a
tar.gz archive); arbitrary shell commands/hooks are rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import sys
import tarfile
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCHEMA = "smart-textbook/host-stack/v1"
STATE_SCHEMA = "smart-textbook/orangepi-host-stack-state/v1"
ALLOWED_KINDS = {"deb", "run", "tar.gz"}
ALLOWED_INSTALLERS = {"dpkg", "ascend_run", "tar_extract"}
SAFE_ARCHIVE_ROOTS = (Path("/usr/local/Ascend"), Path("/opt/ascend"))
REBOOT_EXIT = 75
# These identify the physical host.  A package manifest cannot make a board
# with a different identity compatible by installing more software.
HARD_IDENTITY_FIELDS = {"architecture", "device_model"}


class HostStackError(RuntimeError):
    pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _safe_relative(value: Any) -> str:
    """Return a strictly normalized, relative POSIX manifest path.

    A manifest path is later joined to the local cache directory.  Rejecting
    repeated/empty components here prevents ``host-stack//evil`` from turning
    into an absolute path when the namespace prefix is removed.
    """
    if not isinstance(value, str):
        raise HostStackError(f"unsafe host-stack path: {value!r}")
    text = value.replace("\\", "/")
    parts = text.split("/")
    if (
        not text
        or "\x00" in text
        or text.startswith("/")
        or text.endswith("/")
        or "//" in text
        or any(part in {"", ".", ".."} for part in parts)
        or Path(text).is_absolute()
        or re.match(r"^[A-Za-z]:", text)
    ):
        raise HostStackError(f"unsafe host-stack path: {text!r}")
    return text


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise HostStackError("host_stack_manifest_invalid_schema")
    revision = value.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", revision):
        raise HostStackError("host_stack_manifest_invalid_revision")
    packages = value.get("packages")
    if not isinstance(packages, list):
        raise HostStackError("host_stack_manifest_packages_missing")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in packages:
        if not isinstance(item, dict):
            raise HostStackError("host_stack_manifest_package_invalid")
        path = _safe_relative(item.get("path"))
        if not path.startswith("host-stack/"):
            raise HostStackError(f"host_stack_package_outside_namespace:{path}")
        kind = item.get("kind")
        if kind not in ALLOWED_KINDS:
            raise HostStackError(f"host_stack_package_kind_not_allowed:{kind}")
        installer = item.get("installer")
        expected_installer = {"deb": "dpkg", "run": "ascend_run", "tar.gz": "tar_extract"}[kind]
        if installer is None:
            installer = expected_installer
        if installer not in ALLOWED_INSTALLERS or installer != expected_installer:
            raise HostStackError(f"host_stack_installer_not_allowed:{installer}")
        if path in seen:
            raise HostStackError(f"host_stack_package_duplicate:{path}")
        seen.add(path)
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise HostStackError(f"host_stack_package_digest_invalid:{path}")
        if type(size) is not int or size < 1:
            raise HostStackError(f"host_stack_package_size_invalid:{path}")
        destination = item.get("destination")
        if kind == "tar.gz":
            if not isinstance(destination, str) or not any(
                Path(destination).resolve() == root or root in Path(destination).resolve().parents
                for root in SAFE_ARCHIVE_ROOTS
            ):
                raise HostStackError(f"host_stack_archive_destination_not_allowed:{path}")
        elif destination is not None:
            raise HostStackError(f"host_stack_package_destination_not_allowed:{path}")
        normalized.append(dict(item, path=path, kind=kind, installer=installer, sha256=digest, size=size))
    return dict(value, revision=revision, packages=normalized)


def _version_key(value: Any) -> tuple[tuple[int, ...], int, str, tuple[int, ...]] | None:
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-._]?([A-Za-z]+)(\d*(?:\.\d+)*))?", text)
    if not m:
        return None
    core = tuple(int(x) for x in m.group(1).split("."))
    rank = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2, "rc": -1, "": 0}.get((m.group(2) or "").lower(), 1)
    tail = tuple(int(x) for x in m.group(3).split(".")) if m.group(3) else ()
    return core, rank, (m.group(2) or "").lower(), tail


def _compare(actual: Any, expected: Any) -> bool:
    if expected is None:
        return True
    if isinstance(expected, (list, tuple, set)):
        return any(_compare(actual, x) for x in expected)
    if isinstance(expected, dict):
        return all(_compare(actual, op + str(value)) for op, value in ((">=", expected.get("min")), ("<=", expected.get("max"))) if value is not None)
    text = str(expected).strip()
    m = re.fullmatch(r"(>=|<=|==|=|>|<|~=)\s*(.+)", text)
    op, wanted = (m.group(1), m.group(2)) if m else ("==", text)
    if op in {"=", "=="}:
        return str(actual or "").strip().casefold() == wanted.casefold()
    av, ev = _version_key(actual), _version_key(wanted)
    if av is None or ev is None:
        return False
    if op == ">=":
        return av >= ev
    if op == "<=":
        return av <= ev
    if op == ">":
        return av > ev
    if op == "<":
        return av < ev
    prefix = max(1, len(ev[0]) - 1)
    return av[0][:prefix] == ev[0][:prefix] and av >= ev


def _flatten_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    system = facts.get("system") if isinstance(facts.get("system"), Mapping) else {}
    python = facts.get("python") if isinstance(facts.get("python"), Mapping) else {}
    npu = facts.get("npu_smi") if isinstance(facts.get("npu_smi"), Mapping) else {}
    devices = npu.get("devices") if isinstance(npu.get("devices"), list) else []
    device = devices[0] if devices and isinstance(devices[0], Mapping) else {}
    ascend = facts.get("ascend") if isinstance(facts.get("ascend"), Mapping) else {}
    toolkit = ascend.get("toolkit") if isinstance(ascend.get("toolkit"), Mapping) else {}
    components = toolkit.get("components") if isinstance(toolkit.get("components"), Mapping) else {}
    install = ascend.get("install_info") if isinstance(ascend.get("install_info"), Mapping) else {}
    pyacl = components.get("pyACL") if isinstance(components.get("pyACL"), Mapping) else {}
    values = {
        "architecture": system.get("architecture") or facts.get("architecture"),
        "kernel": system.get("kernel") or facts.get("kernel"),
        "python_abi": python.get("abi") or facts.get("python_abi"),
        "device_model": device.get("name") or facts.get("device_model"),
        "firmware_version": device.get("firmware_version") or facts.get("firmware_version"),
        "driver_version": facts.get("driver_version") or npu.get("driver_version") or npu.get("version") or install.get("Driver_ Version"),
        "npu_smi_version": npu.get("version"),
        "cann_version": toolkit.get("version") or toolkit.get("version_dir") or facts.get("cann_version"),
        "acl_version": pyacl.get("Version") or facts.get("acl_version"),
    }
    return {k: v for k, v in values.items() if v is not None}


def compatibility(facts: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    actual = _flatten_facts(facts)
    expected = manifest.get("compatibility") or {}
    if not isinstance(expected, Mapping):
        raise HostStackError("host_stack_manifest_compatibility_invalid")
    reasons = []
    aliases = {"arch": "architecture", "device": "device_model", "driver": "driver_version", "firmware": "firmware_version", "cann": "cann_version", "acl": "acl_version"}
    for name, wanted in expected.items():
        field = aliases.get(name, name)
        observed = actual.get(field)
        if observed is None:
            reasons.append({"field": field, "code": "missing", "expected": wanted})
        elif field == "architecture":
            values = wanted if isinstance(wanted, (list, tuple, set)) else [wanted]
            norm = str(observed).replace("-", "_").lower()
            if not any(norm == str(v).replace("-", "_").lower() for v in values):
                reasons.append({"field": field, "code": "mismatch", "expected": wanted, "actual": observed})
        elif field == "device_model":
            values = wanted if isinstance(wanted, (list, tuple, set)) else [wanted]
            if not any(str(observed).casefold() == str(v).casefold() for v in values):
                reasons.append({"field": field, "code": "mismatch", "expected": wanted, "actual": observed})
        elif not _compare(observed, wanted):
            reasons.append({"field": field, "code": "mismatch", "expected": wanted, "actual": observed})
    return {"ok": not reasons, "actual": actual, "reasons": reasons}


def _fetch(url: str, token: str, insecure: bool = False) -> bytes:
    request = Request(url, headers={"X-Runtime-Agent-Enrollment-Token": token})
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urlopen(request, timeout=60, context=context) as response:
            return response.read(4 * 1024 * 1024)
    except (HTTPError, URLError, OSError) as exc:
        raise HostStackError(f"host_stack_asset_unavailable:{getattr(exc, 'code', '') or type(exc).__name__}") from exc


def _download_to(url: str, token: str, destination: Path, insecure: bool = False, expected_size: int | None = None) -> None:
    if expected_size is None or type(expected_size) is not int or expected_size < 1:
        raise HostStackError("host_stack_package_size_invalid")
    request = Request(url, headers={"X-Runtime-Agent-Enrollment-Token": token})
    context = ssl._create_unverified_context() if insecure else None
    temporary = destination.with_name(destination.name + f".part.{os.getpid()}")
    try:
        with urlopen(request, timeout=120, context=context) as response, temporary.open("wb") as stream:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > expected_size:
                        raise HostStackError("host_stack_asset_too_large")
                except ValueError:
                    pass
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise HostStackError("host_stack_asset_too_large")
                stream.write(chunk)
        os.replace(temporary, destination)
    except (HTTPError, URLError, OSError, HostStackError, ValueError) as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(exc, HostStackError):
            raise
        raise HostStackError(f"host_stack_asset_unavailable:{getattr(exc, 'code', '') or type(exc).__name__}") from exc


def _run(command: list[str]) -> None:
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode:
        raise HostStackError(f"host_stack_install_failed:{Path(command[0]).name}:{result.returncode}")


def _extract_safe(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as stream:
        for member in stream.getmembers():
            name = _safe_relative(member.name)
            target = (destination / name).resolve()
            if destination != target and destination not in target.parents:
                raise HostStackError("host_stack_archive_path_escape")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise HostStackError("host_stack_archive_member_not_allowed")
        if sys.version_info >= (3, 12):
            stream.extractall(destination, filter="data")
        else:
            stream.extractall(destination)


def _install(item: Mapping[str, Any], package: Path) -> None:
    kind = item["kind"]
    if kind == "deb":
        _run(["dpkg", "--install", str(package)])
    elif kind == "run":
        package.chmod(package.stat().st_mode | 0o100)
        _run(["bash", str(package), "--quiet", "--nox11"])
    elif kind == "tar.gz":
        _extract_safe(package, Path(str(item["destination"])))
    else:
        raise HostStackError(f"host_stack_package_kind_not_allowed:{kind}")


def _write_resume_unit(state_dir: Path, cache_dir: Path) -> None:
    unit_dir = Path(os.environ.get("SMART_TEXTBOOK_AGENT_SYSTEMD_DIR", "/etc/systemd/system"))
    unit = unit_dir / "smart-textbook-host-stack-resume.service"
    # Both the helper and manifest are persisted under state_dir. Bootstrap
    # installs may source them from a temporary directory that is cleaned up
    # when install.sh exits, so a reboot must never point at that source path.
    script = Path(__file__).resolve()
    manifest = state_dir / "manifest.json"
    facts_file = state_dir / "preflight-facts.json"
    command = " ".join(shlex.quote(str(value)) for value in ("/usr/bin/python3", script, "--mode", "auto", "--resume", "--manifest", manifest, "--facts-file", facts_file, "--cache-dir", cache_dir, "--state-dir", state_dir))
    text = """[Unit]
Description=Smart Textbook Orange Pi host stack resume
After=network-online.target
Wants=network-online.target
ConditionPathExists=%s

[Service]
Type=oneshot
ExecStart=%s
RemainAfterExit=yes
""" % (state_dir / "recovery.json", command)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit.write_text(text, encoding="utf-8")
    os.chmod(unit, 0o644)
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
        subprocess.run(["systemctl", "enable", unit.name], check=False, capture_output=True)
    except OSError:
        pass


def _clear_resume_artifacts(state_dir: Path) -> None:
    """Remove the one-shot reboot continuation after successful recovery."""
    try:
        (state_dir / "recovery.json").unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    unit_dir = Path(os.environ.get("SMART_TEXTBOOK_AGENT_SYSTEMD_DIR", "/etc/systemd/system"))
    unit = unit_dir / "smart-textbook-host-stack-resume.service"
    try:
        unit.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        subprocess.run(["systemctl", "disable", unit.name], check=False, capture_output=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
    except OSError:
        pass


def _package_target(revision_cache: Path, manifest_path: str) -> Path:
    """Resolve a package path and prove it stays under the revision cache."""
    relative = manifest_path.removeprefix("host-stack/")
    base = revision_cache.resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HostStackError(f"host_stack_package_path_escape:{manifest_path}") from exc
    return target


def _state_path(state_dir: Path) -> Path:
    return state_dir / "host-stack-state.json"


def _load_state(state_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_state_path(state_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def provision(*, mode: str, server: str = "", token: str = "", state_dir: Path, cache_dir: Path, manifest_path: Path | None = None, facts: Mapping[str, Any] | None = None, insecure: bool = False, resume: bool = False) -> dict[str, Any]:
    if mode not in {"never", "verify", "auto"}:
        raise HostStackError("host_stack_mode_invalid")
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(state_dir)
    facts = facts or {}
    if resume and not facts:
        # install.sh persists a fresh preflight report before provisioning.  A
        # systemd resume therefore rechecks the current host after reboot even
        # though the unit does not retain the enrollment token.
        cached_facts = state_dir / "preflight-facts.json"
        try:
            loaded_facts = json.loads(cached_facts.read_text(encoding="utf-8"))
            if isinstance(loaded_facts, Mapping):
                facts = loaded_facts
        except (OSError, json.JSONDecodeError):
            pass
    if mode == "never":
        result = {"status": "skipped", "ready": False, "fallback": "existing_host_environment", "revision": state.get("revision")}
        _write_json(_state_path(state_dir), {"schema": STATE_SCHEMA, **result, "updated_at": int(time.time())})
        return result

    manifest: dict[str, Any] | None = None
    manifest_error: str | None = None
    try:
        if manifest_path:
            manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        elif server and token and not (resume and state.get("manifest")):
            body = _fetch(server.rstrip("/") + "/api/runtime-agent/bootstrap/host-stack/manifest", token, insecure)
            manifest = validate_manifest(json.loads(body.decode("utf-8")))
        elif state.get("manifest"):
            manifest = validate_manifest(state["manifest"])
        else:
            manifest_error = "host_stack_asset_unavailable:manifest_missing"
    except (OSError, json.JSONDecodeError, HostStackError) as exc:
        manifest_error = str(exc)

    if manifest is None:
        result = {"status": "unavailable", "ready": False, "fallback": "existing_host_environment", "diagnostic": manifest_error or "host_stack_manifest_missing"}
        _write_json(_state_path(state_dir), {"schema": STATE_SCHEMA, **result, "updated_at": int(time.time())})
        if mode == "verify":
            raise HostStackError(result["diagnostic"])
        return result

    check = compatibility(facts, manifest)
    resuming_recovery = bool(resume and state.get("phase") == "reboot_pending" and state.get("revision") == manifest["revision"])
    if check["ok"]:
        result = {"status": "verified", "ready": True, "revision": manifest["revision"], "compatibility": check}
        _write_json(_state_path(state_dir), {"schema": STATE_SCHEMA, "manifest": manifest, **result, "updated_at": int(time.time())})
        # A later successful install/verification can supersede a stale
        # reboot continuation left by an older revision. Remove it before it
        # can run the old manifest on the next boot.
        if resuming_recovery or state.get("phase") == "reboot_pending":
            _clear_resume_artifacts(state_dir)
        return result
    if mode == "verify":
        result = {"status": "incompatible", "ready": False, "revision": manifest["revision"], "compatibility": check}
        _write_json(_state_path(state_dir), {"schema": STATE_SCHEMA, "manifest": manifest, **result, "updated_at": int(time.time())})
        raise HostStackError("host_stack_incompatible:" + json.dumps(check["reasons"], separators=(",", ":")))

    identity_reasons = [
        reason for reason in check["reasons"]
        if reason.get("field") in HARD_IDENTITY_FIELDS and reason.get("code") == "mismatch"
    ]
    if identity_reasons:
        result = {
            "status": "failed",
            "ready": False,
            "revision": manifest["revision"],
            "phase": "failed",
            "error": "host_stack_identity_mismatch",
            "compatibility": check,
        }
        _write_json(_state_path(state_dir), {"schema": STATE_SCHEMA, "manifest": manifest, **result, "updated_at": int(time.time())})
        raise HostStackError("host_stack_identity_mismatch:" + json.dumps(identity_reasons, separators=(",", ":")))

    revision = manifest["revision"]
    revision_cache = cache_dir / revision
    revision_cache.mkdir(parents=True, exist_ok=True)
    if resuming_recovery:
        state = dict(state)
        state.update({"schema": STATE_SCHEMA, "manifest": manifest, "revision": revision, "phase": "resuming", "compatibility_after_reboot": check, "updated_at": int(time.time())})
        state.setdefault("installed", [])
    else:
        state = {"schema": STATE_SCHEMA, "manifest": manifest, "revision": revision, "phase": "prepared", "compatibility_before": check, "updated_at": int(time.time())}
    _write_json(_state_path(state_dir), state)
    try:
        for item in manifest["packages"]:
            rel = item["path"]
            target = _package_target(revision_cache, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                _download_to(server.rstrip("/") + "/api/runtime-agent/bootstrap/" + rel, token, target, insecure, expected_size=item["size"])
            if target.stat().st_size != item["size"] or _digest(target) != item["sha256"]:
                raise HostStackError(f"host_stack_package_integrity_failed:{rel}")
            if resuming_recovery and rel in state.get("installed", []):
                # The package was installed before the reboot.  Its cache and
                # digest are verified above, so rerunning the vendor installer
                # could only create a second reboot cycle.
                continue
            state["phase"] = "installing"
            state["current_package"] = rel
            _write_json(_state_path(state_dir), state)
            _install(item, target)
            state.setdefault("installed", []).append(rel)
            _write_json(_state_path(state_dir), state)
    except Exception as exc:
        state.update({"phase": "failed", "ready": False, "error": str(exc), "updated_at": int(time.time())})
        _write_json(_state_path(state_dir), state)
        raise
    if any(bool(item.get("requires_reboot")) for item in manifest["packages"]) and not resuming_recovery:
        state.update({"phase": "reboot_pending", "ready": False, "reboot_required": True, "updated_at": int(time.time())})
        _write_json(_state_path(state_dir), state)
        saved_manifest = state_dir / "manifest.json"
        _write_json(saved_manifest, manifest)
        _write_json(state_dir / "recovery.json", {"schema": STATE_SCHEMA, "revision": revision, "manifest": manifest, "cache_dir": str(cache_dir), "state_dir": str(state_dir)})
        _write_resume_unit(state_dir, cache_dir)
        return {"status": "reboot_required", "ready": False, "revision": revision}
    state.update({"phase": "installed_unverified", "ready": False, "updated_at": int(time.time())})
    _write_json(_state_path(state_dir), state)
    if resuming_recovery:
        _clear_resume_artifacts(state_dir)
    return {"status": "installed_unverified", "ready": False, "revision": revision, "compatibility": check}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("never", "verify", "auto"), default=os.environ.get("SMART_TEXTBOOK_AGENT_HOST_STACK_MODE", "auto"))
    parser.add_argument("--server", default=os.environ.get("SMART_TEXTBOOK_AGENT_SERVER_URL", ""))
    parser.add_argument("--enrollment-token", default=os.environ.get("SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN", ""))
    parser.add_argument("--state-dir", default=os.environ.get("SMART_TEXTBOOK_AGENT_HOST_STACK_STATE_DIR", "/var/lib/smart-textbook/runtime-agent/host-stack"))
    parser.add_argument("--cache-dir", default=os.environ.get("SMART_TEXTBOOK_AGENT_HOST_STACK_CACHE_DIR", "/var/cache/smart-textbook/orangepi/host-stack"))
    parser.add_argument("--manifest")
    parser.add_argument("--facts-file")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    facts = {}
    if args.facts_file:
        try:
            facts = json.loads(Path(args.facts_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"host-stack facts invalid: {exc}", file=sys.stderr)
            return 2
    try:
        result = provision(mode=args.mode, server=args.server, token=args.enrollment_token, state_dir=Path(args.state_dir), cache_dir=Path(args.cache_dir), manifest_path=Path(args.manifest) if args.manifest else None, facts=facts, insecure=args.insecure, resume=args.resume)
    except HostStackError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("status") == "reboot_required":
        print("host_stack_reboot_required: reboot and let smart-textbook-host-stack-resume.service continue", file=sys.stderr)
        return REBOOT_EXIT
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
