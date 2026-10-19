#!/usr/bin/env python3
"""Verify signed runtime assets and switch current only after system-Python preflight."""
from __future__ import annotations
import argparse
import base64
import fcntl
import hashlib
import math
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
import requests

SYSTEM_PYTHON = "/usr/bin/python3"
STATE_FILE = ".installation.json"
RESERVED = {"manifest.json", "manifest.sig", "runtime.env", "runtime-environment.json", STATE_FILE}
PATH_LISTS = {"PYTHONPATH", "LD_LIBRARY_PATH", "PATH"}
SINGLE_PATHS = {"ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "ASCEND_OPP_PATH", "ASCEND_AICPU_PATH", "SMART_TEXTBOOK_DATASET_ROOT", "SMART_TEXTBOOK_RUNTIME_HELPERS_ROOT", "OPP_ROOT", "TBE_IMPL_PATH"}

# A signed archive manifest carries compressed bytes; extraction needs a second
# allocation. Keep a deterministic safety margin so a new board fails before
# downloading when its filesystem cannot complete the import.
STORAGE_SAFETY_RATIO = 0.10
STORAGE_MIN_SAFETY_BYTES = 256 * 1024 * 1024
ARCHIVE_FALLBACK_EXPANSION_FACTOR = 4


def _python_abi() -> str | None:
    implementation = getattr(sys.implementation, "name", "") or ""
    version = sys.version_info
    return getattr(sys.implementation, "cache_tag", None) or f"{implementation.lower()}-{version.major}{version.minor}"


def _expected_matches(actual: str | None, expected: Any) -> bool:
    if expected in (None, "", "*"):
        return True
    if isinstance(expected, (list, tuple, set)):
        return any(_expected_matches(actual, item) for item in expected)
    if isinstance(expected, dict):
        return _expected_matches(actual, expected.get("one_of", expected.get("any")))
    return str(actual or "").strip().casefold() == str(expected).strip().casefold()


def facts() -> dict[str, Any]:
    value = json.loads(subprocess.check_output([SYSTEM_PYTHON, str(Path(__file__).with_name("preflight.py"))], text=True))
    architecture = value.get("system", {}).get("architecture")
    if architecture == "arm64":
        architecture = "aarch64"
    first = next((d for d in value.get("npu_smi", {}).get("devices", []) if isinstance(d, dict) and d.get("name")), {})
    toolkit = value.get("ascend", {}).get("toolkit", {})
    acl = next((item.get("Version") or item.get("version") for name, item in toolkit.get("components", {}).items() if str(name).lower() in {"acl", "pyacl"} and isinstance(item, dict)), None)
    return {"architecture": architecture, "device_model": first.get("name"), "driver_version": value.get("npu_smi", {}).get("version"), "firmware_version": first.get("firmware_version"), "cann_version": toolkit.get("version_dir") or toolkit.get("version"), "python_abi": value.get("python", {}).get("abi"), "mindspore_version": value.get("packages", {}).get("mindspore", {}).get("version"), "acl_version": acl}


def canonical_manifest(value: dict[str, Any]) -> bytes:
    clean = {key: item for key, item in value.items() if key not in {"signature", "manifest_sha256", "signer_key_id"}}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def verify_signature(manifest: dict[str, Any], signature: str, public_key: str) -> bool:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            raw_key = Path(public_key).read_bytes() if Path(public_key).is_file() else public_key.encode()
        except OSError:
            raw_key = public_key.encode()
        if raw_key.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(raw_key)
        else:
            if len(raw_key) != 32:
                text = raw_key.decode().strip()
                raw_key = bytes.fromhex(text) if re.fullmatch(r"[a-fA-F0-9]{64}", text) else base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            key = Ed25519PublicKey.from_public_bytes(raw_key)
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), canonical_manifest(manifest))
        return True
    except Exception:
        return False


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or "\\" in relative or ".." in path.parts or ":" in relative or "\x00" in relative:
        raise RuntimeError("artifact_path_invalid")
    candidate = root / path
    resolved = candidate.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise RuntimeError("artifact_path_escape")
    return candidate



def experiment_catalog_manifest(manifest):
    """Validate the signed manifest-driven experiment dispatcher contract."""
    catalog = manifest.get("experiments")
    if catalog is None:
        return None
    if not isinstance(catalog, dict) or not catalog or len(catalog) > 128:
        raise RuntimeError("experiment_catalog_invalid")
    declared = {
        str(item.get("path")): item
        for item in (manifest.get("files") or [])
        if isinstance(item, dict) and item.get("path")
    }
    normalized = {}
    for experiment_id, steps in catalog.items():
        exp_id = str(experiment_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", exp_id):
            raise RuntimeError("experiment_id_invalid")
        if not isinstance(steps, dict) or not steps or len(steps) > 64:
            raise RuntimeError("experiment_steps_invalid")
        normalized_steps = {}
        for step_name, spec in steps.items():
            step = str(step_name)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", step):
                raise RuntimeError("experiment_step_invalid")
            if isinstance(spec, str):
                path, timeout = spec, 300
            elif isinstance(spec, dict):
                path, timeout = str(spec.get("path") or ""), spec.get("timeout_seconds", 300)
            else:
                raise RuntimeError("experiment_entry_invalid")
            path_obj = PurePosixPath(path)
            if (
                not path
                or path_obj.is_absolute()
                or "\\" in path
                or ":" in path
                or ".." in path_obj.parts
                or path in {".", ".."}
                or path not in declared
            ):
                raise RuntimeError("experiment_path_invalid")
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 5 or timeout > 900:
                raise RuntimeError("experiment_timeout_invalid")
            normalized_steps[step] = {"path": path, "timeout_seconds": timeout}
        normalized[exp_id] = normalized_steps
    return normalized


def course_bundle_manifest(manifest):
    """Return the signed course experiment/data bundle declaration."""
    bundle = manifest.get("course_bundle")
    if bundle is None:
        return None
    if not isinstance(bundle, dict):
        raise RuntimeError("course_bundle_invalid")
    revision = str(bundle.get("bundle_revision") or bundle.get("revision") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", revision) or revision in {".", "..", "current"}:
        raise RuntimeError("course_bundle_revision_invalid")
    entries = bundle.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("course_bundle_files_missing")
    seen = set()
    normalized = []
    declared = {str(item.get("path")): item for item in (manifest.get("files") or []) if isinstance(item, dict)}
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("course_bundle_file_invalid")
        relative = str(item.get("path") or "")
        if not relative or relative in {".", ".."} or relative in seen or relative.startswith("/") or "\\" in relative or ".." in PurePosixPath(relative).parts or ":" in relative:
            raise RuntimeError("course_bundle_path_invalid")
        size = item.get("size")
        digest = str(item.get("sha256") or "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RuntimeError("course_bundle_integrity_metadata_invalid")
        parent = declared.get(relative)
        if parent is not None and (parent.get("size") != size or str(parent.get("sha256") or "") != digest):
            raise RuntimeError("course_bundle_manifest_mismatch")
        seen.add(relative)
        normalized.append({"path": relative, "size": size, "sha256": digest})
    return {"bundle_revision": revision, "files": normalized}


def verify_course_bundle(release, manifest, receipt=None):
    """Verify every signed course asset after extraction and before activation."""
    bundle = course_bundle_manifest(manifest)
    if bundle is None:
        return {"status": "legacy_unmanaged"}
    consumed = set()
    if receipt is not None and valid_consumed_archives(release, manifest, receipt) and validate_tree(release, receipt.get("tree", {})):
        consumed = {record["path"] for record in receipt.get("consumed_archives", [])}
    for item in bundle["files"]:
        path = safe_path(release, item["path"])
        # A signed course bundle may also name a runtime archive that was
        # deliberately consumed. Its root-only installation receipt proves
        # the verified source bytes and complete unchanged extracted tree.
        if item["path"] in consumed:
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("course_bundle_missing:" + item["path"])
        if path.stat().st_size != item["size"]:
            raise RuntimeError("course_bundle_size_mismatch:" + item["path"])
        if hash_file(path) != item["sha256"]:
            raise RuntimeError("course_bundle_hash_mismatch:" + item["path"])
    return {"status": "ready", "bundle_revision": bundle["bundle_revision"], "file_count": len(bundle["files"])}


def validate_manifest(manifest, artifact, public_key):
    signature = str(artifact.get("signature") or manifest.get("signature") or "")
    if not signature or not public_key:
        raise RuntimeError("artifact_manifest_signature_required")
    if not verify_signature(manifest, signature, public_key):
        raise RuntimeError("artifact_manifest_signature_invalid")
    revision = str(manifest.get("artifact_revision") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", revision) or revision in {".", "..", "current"} or artifact.get("artifact_revision", revision) != revision:
        raise RuntimeError("artifact_revision_invalid")
    digest = hashlib.sha256(canonical_manifest(manifest)).hexdigest()
    if artifact.get("manifest_sha256") and artifact["manifest_sha256"] != digest:
        raise RuntimeError("artifact_manifest_digest_invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("artifact_files_invalid")
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("artifact_file_invalid")
        relative = str(item.get("path") or "")
        safe_path(Path("/nonexistent-runtime-validation"), relative)
        if relative in seen or PurePosixPath(relative).parts[0] in RESERVED:
            raise RuntimeError("artifact_file_path_reserved_or_duplicate")
        seen.add(relative)
        if not isinstance(item.get("size"), int) or item["size"] < 0 or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or "")):
            raise RuntimeError("artifact_file_integrity_metadata_invalid")
    course_bundle_manifest(manifest)
    experiment_catalog_manifest(manifest)
    return digest, signature


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


DOWNLOAD_MAX_ATTEMPTS = 4
DOWNLOAD_RETRY_BACKOFF_SECONDS = 2


def _partial_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def _partial_size(path: Path, expected_size: int) -> int:
    if path.is_symlink():
        path.unlink()
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size < 0 or size > expected_size or path.is_symlink() or not path.is_file():
        path.unlink(missing_ok=True)
        return 0
    return size


def download(url, destination, expected_size, expected_sha256, headers, verify_tls):
    """Download a signed asset with persistent partials and HTTP range retries."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == expected_size and hash_file(destination) == expected_sha256:
        return
    partial = _partial_path(destination)
    offset = _partial_size(partial, expected_size)
    if partial.is_file() and offset == expected_size:
        if hash_file(partial) == expected_sha256:
            partial.chmod(0o644)
            os.replace(partial, destination)
            return
        partial.unlink(missing_ok=True)
        offset = 0
    last_error = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        request_headers = dict(headers or {})
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        try:
            with requests.get(url, headers=request_headers, stream=True, timeout=(20, 120), verify=verify_tls) as response:
                response.raise_for_status()
                # A server that ignores Range must be treated as a fresh body;
                # appending it would silently duplicate the partial prefix.
                append = bool(offset and getattr(response, "status_code", None) == 206)
                if not append:
                    offset = 0
                digest = hashlib.sha256()
                size = 0
                if append:
                    with partial.open("rb") as existing:
                        for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                            digest.update(chunk)
                    size = offset
                with partial.open("ab" if append else "wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            size += len(chunk)
                            if size > expected_size:
                                raise RuntimeError("artifact_file_size_exceeded")
                            digest.update(chunk)
                            stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if size == expected_size and digest.hexdigest() == expected_sha256:
                partial.chmod(0o644)
                os.replace(partial, destination)
                return
            last_error = RuntimeError("artifact_file_integrity_failed")
            # A complete but invalid body cannot be resumed. Start over while
            # retaining short partials for the next range request.
            if size >= expected_size:
                partial.unlink(missing_ok=True)
                offset = 0
            else:
                offset = size
        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            offset = _partial_size(partial, expected_size)
            if offset == expected_size:
                if hash_file(partial) == expected_sha256:
                    partial.chmod(0o644)
                    os.replace(partial, destination)
                    return
                partial.unlink(missing_ok=True)
                offset = 0
        if attempt < DOWNLOAD_MAX_ATTEMPTS:
            time.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
    if isinstance(last_error, RuntimeError):
        raise last_error
    raise RuntimeError("artifact_download_failed") from last_error


def extract_archive(source: Path, target: Path):
    """Stream tar members; permit internal relative symlinks but never follow them."""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    process = None
    try:
        if source.name.endswith((".zst", ".zstd")):
            process = subprocess.Popen(["zstd", "-d", "-q", "-c", str(source)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            archive = tarfile.open(fileobj=process.stdout, mode="r|")
        else:
            archive = tarfile.open(source, mode="r|*")
        with archive:
            for member in archive:
                name = member.name
                if name in {".", "./"} and member.isdir():
                    continue
                destination = safe_path(root, name)
                for parent in destination.parents:
                    if parent == root:
                        break
                    if parent.is_symlink():
                        raise RuntimeError("runtime_archive_symlink_parent")
                if destination.exists() or destination.is_symlink():
                    if member.isdir() and destination.is_dir() and not destination.is_symlink():
                        continue
                    raise RuntimeError("runtime_archive_duplicate_member")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    destination.mkdir(mode=member.mode & 0o777)
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError("runtime_archive_unreadable")
                    with stream, destination.open("xb") as output:
                        shutil.copyfileobj(stream, output, 1024 * 1024)
                    destination.chmod(member.mode & 0o777)
                elif member.issym():
                    link = member.linkname
                    if not link or PurePosixPath(link).is_absolute() or "\\" in link or "\x00" in link:
                        raise RuntimeError("runtime_archive_link_invalid")
                    resolved = (destination.parent / link).resolve()
                    if resolved != root and root not in resolved.parents:
                        raise RuntimeError("runtime_archive_link_escape")
                    destination.symlink_to(link)
                else:
                    raise RuntimeError("runtime_archive_member_type_forbidden")
        # Resolve all chains after extraction too, including previously dangling links.
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                item = Path(directory) / name
                if item.is_symlink():
                    resolved = item.resolve()
                    if resolved != root and root not in resolved.parents:
                        raise RuntimeError("runtime_archive_link_escape")
        if process and process.wait(timeout=30) != 0:
            raise RuntimeError("runtime_archive_decompression_failed")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise RuntimeError("runtime_archive_extract_failed") from exc
    finally:
        if process:
            if process.stdout:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def runtime_environment(release, manifest):
    policy = manifest.get("runtime_install") or {}
    if not isinstance(policy, dict):
        raise RuntimeError("runtime_install_invalid")
    env = {}
    def path(value):
        value = str(value)
        if value.startswith("${RELEASE_ROOT}/"):
            value = value[len("${RELEASE_ROOT}/"):]
        try:
            return str(safe_path(release, value))
        except RuntimeError as exc:
            raise RuntimeError("runtime_environment_path_invalid") from exc
    for field, key in (("python_paths", "PYTHONPATH"), ("library_paths", "LD_LIBRARY_PATH"), ("bin_paths", "PATH")):
        values = policy.get(field, [])
        if not isinstance(values, list):
            raise RuntimeError("runtime_environment_paths_invalid")
        if values:
            env[key] = os.pathsep.join(path(value) for value in values)
    # Backwards-compatible default is relative to this signed release, never the build host.
    if not env.get("PYTHONPATH") and policy.get("archives"):
        env["PYTHONPATH"] = path(policy.get("target_subdir", "runtime/python/site-packages"))
    for field, keys in (("toolkit_subdir", ("ASCEND_TOOLKIT_HOME", "ASCEND_HOME_PATH")), ("opp_subdir", ("ASCEND_OPP_PATH",)), ("data_subdir", ("SMART_TEXTBOOK_DATASET_ROOT",)), ("helpers_subdir", ("SMART_TEXTBOOK_RUNTIME_HELPERS_ROOT",))):
        if policy.get(field):
            for key in keys:
                env[key] = path(policy[field])
    if policy.get("toolkit_subdir"):
        toolkit = path(policy["toolkit_subdir"])
        opp = path(policy.get("opp_subdir") or str(PurePosixPath(policy["toolkit_subdir"]) / "opp"))
        env.setdefault("ASCEND_OPP_PATH", opp)
        env.setdefault("OPP_ROOT", opp)
        env.setdefault("ASCEND_AICPU_PATH", toolkit)
        env.setdefault("TBE_IMPL_PATH", str(Path(opp) / "built-in/op_impl/ai_core/tbe"))
        # Host kernel driver libraries stay on the host. They are never supplied
        # or overwritten by a user-space artifact.
        host_driver_paths = ["/var/davinci/driver/lib64", "/usr/local/Ascend/driver/lib64", "/usr/local/Ascend/driver/lib64/common", "/usr/local/Ascend/driver/lib64/driver"]
        present = [item for item in host_driver_paths if Path(item).is_dir()]
        if present:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [env.get("LD_LIBRARY_PATH", ""), *present]))
    custom = policy.get("environment") or {}
    if not isinstance(custom, dict):
        raise RuntimeError("runtime_environment_invalid")
    for key, value in custom.items():
        if key in PATH_LISTS:
            env[key] = os.pathsep.join(path(part) for part in str(value).split(os.pathsep))
        elif key in SINGLE_PATHS:
            env[key] = path(value)
        else:
            raise RuntimeError("runtime_environment_variable_forbidden")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SMART_TEXTBOOK_RUNTIME_ROOT"] = str(release)
    env["SMART_TEXTBOOK_RUNTIME_REVISION"] = str(manifest.get("artifact_revision") or release.name)
    return env



def storage_requirements(manifest, install_userland=True):
    """Return the signed download/extraction budget for a candidate release.

    ``unpacked_size`` is recorded per runtime archive when the bundle is built.
    Older manifests may omit it; use a conservative four-times expansion
    estimate so they still fail closed before any network write.
    """
    files = manifest.get("files") or []
    file_map = {item.get("path"): item for item in files if isinstance(item, dict)}
    try:
        download_bytes = sum(int(item["size"]) for item in files)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("runtime_storage_estimate_invalid") from exc
    extraction_bytes = 0
    archives = []
    policy = manifest.get("runtime_install") or {}
    specs = policy.get("archives", []) if isinstance(policy, dict) else []
    if install_userland:
        if not isinstance(specs, list):
            raise RuntimeError("runtime_storage_estimate_invalid")
        for spec in specs:
            if not isinstance(spec, dict):
                raise RuntimeError("runtime_storage_estimate_invalid")
            name = str(spec.get("path") or "")
            item = file_map.get(name)
            if not item:
                raise RuntimeError("runtime_storage_archive_not_in_manifest")
            try:
                compressed = int(item["size"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("runtime_storage_estimate_invalid") from exc
            declared = spec.get("unpacked_size")
            if declared is None:
                unpacked = compressed * ARCHIVE_FALLBACK_EXPANSION_FACTOR
                estimated = True
            elif isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                raise RuntimeError("runtime_storage_estimate_invalid")
            else:
                unpacked = declared
                estimated = False
            extraction_bytes += unpacked
            archives.append({"path": name, "compressed_bytes": compressed,
                             "unpacked_bytes": unpacked, "estimated": estimated})
    working_bytes = download_bytes + extraction_bytes
    safety_bytes = max(STORAGE_MIN_SAFETY_BYTES, math.ceil(working_bytes * STORAGE_SAFETY_RATIO))
    return {"download_bytes": download_bytes, "extraction_bytes": extraction_bytes,
            "working_bytes": working_bytes, "safety_bytes": safety_bytes,
            "required_bytes": working_bytes + safety_bytes, "archives": archives}


def check_storage_capacity(root, manifest, install_userland=True):
    requirements = storage_requirements(manifest, install_userland=install_userland)
    try:
        available = int(shutil.disk_usage(str(root)).free)
    except OSError as exc:
        raise RuntimeError("runtime_storage_probe_failed") from exc
    report = dict(requirements, available_bytes=available)
    if available < requirements["required_bytes"]:
        detail = json.dumps(report, sort_keys=True, separators=(",", ":"))
        raise RuntimeError("artifact_insufficient_disk_space:" + detail)
    return report

def process_environment(runtime):
    env = dict(os.environ)
    for key, value in runtime.items():
        env[key] = value + (os.pathsep + env[key] if key in PATH_LISTS and env.get(key) else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def write_environment(release, runtime):
    (release / "runtime-environment.json").write_text(json.dumps(runtime, sort_keys=True) + "\n")
    (release / "runtime-environment.json").chmod(0o644)
    lines = []
    for key, value in runtime.items():
        suffix = ':"${' + key + ':-}"' if key in PATH_LISTS else ""
        lines.append("export " + key + "=" + shlex.quote(value) + suffix + "\n")
    (release / "runtime.env").write_text("".join(lines))
    # Paths only, no secrets; student experiment wrappers must be able to source this.
    (release / "runtime.env").chmod(0o644)


def archive_record(spec, item, policy):
    return {"path": item["path"], "size": item["size"], "sha256": item["sha256"],
            "target_subdir": str(spec.get("target_subdir") or policy.get("target_subdir") or "runtime/python/site-packages"),
            "kind": spec.get("kind", "python")}


def install_userland(release, manifest, root=None, consumed_archives=None):
    policy = manifest.get("runtime_install") or {}
    installed = []
    consumed = consumed_archives if consumed_archives is not None else []
    file_map = {item["path"]: item for item in manifest.get("files", [])}
    for spec in policy.get("archives", []):
        if not isinstance(spec, dict) or spec.get("path") not in file_map:
            raise RuntimeError("runtime_archive_not_in_manifest")
        if spec.get("kind", "python") not in {"python", "toolkit", "data", "helpers"}:
            raise RuntimeError("runtime_archive_kind_forbidden")
        target = safe_path(release, str(spec.get("target_subdir") or policy.get("target_subdir") or "runtime/python/site-packages"))
        if target == release:
            raise RuntimeError("runtime_target_invalid")
        if any(target == previous or target in previous.parents or previous in target.parents for previous in installed):
            raise RuntimeError("runtime_archive_target_overlap")
        source = safe_path(release, spec["path"])
        item = file_map[spec["path"]]
        # Consume only exact bytes proven by the signed manifest. This check is
        # also required for callers that invoke installation without download().
        if source.is_symlink() or not source.is_file() or source.stat().st_size != item["size"] or hash_file(source) != item["sha256"]:
            raise RuntimeError("runtime_archive_integrity_failed")
        try:
            extract_archive(source, target)
        except Exception:
            # Extraction is a candidate-only operation. Remove partial output,
            # retaining the verified compressed source for diagnosis/recovery.
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            raise
        installed.append(target)
        if spec.get("discard_after_extract") is True:
            # Never delete before extract_archive has validated all members and
            # links and observed a successful decompressor exit.
            source.unlink()
            consumed.append(archive_record(spec, item, policy))
    runtime = runtime_environment(release, manifest)
    write_environment(release, runtime)
    return {"archives": [str(path.relative_to(release)) for path in installed], "consumed_archives": consumed,
            "environment": runtime, "runtime_env": str(release / "runtime.env")}


def check_required_packages(manifest, env):
    required = (manifest.get("runtime_install") or {}).get("required_packages") or {}
    if not required:
        return {}
    script = r"""
import importlib.metadata, json, sys
from preflight import _version_compare
requirements = json.loads(sys.argv[1]); packages = {}; ok = True
def expected_matches(actual, expected):
    if expected in (None, "", "*"):
        return True
    if isinstance(expected, (list, tuple, set)):
        return any(expected_matches(actual, item) for item in expected)
    if isinstance(expected, dict):
        return expected_matches(actual, expected.get("one_of", expected.get("any")))
    return str(actual or "").strip().casefold() == str(expected).strip().casefold()
for name, expected in requirements.items():
    if name == "python_abi":
        version = getattr(sys.implementation, "cache_tag", None)
        packages[name] = {"installed": bool(version), "version": version, "ok": bool(version) and expected_matches(version, expected)}
        ok = ok and packages[name]["ok"]
        continue
    try:
        version = importlib.metadata.version(name)
        valid = expected in (None, "", "*") or _version_compare(version, expected) is True
        packages[name] = {"installed": True, "version": version, "ok": valid}
    except importlib.metadata.PackageNotFoundError:
        packages[name] = {"installed": False, "version": None, "ok": False}
    ok = ok and packages[name]["ok"]
print(json.dumps({"ok": ok, "packages": packages})); sys.exit(0 if ok else 2)
"""
    checked_env = dict(env)
    checked_env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + checked_env.get("PYTHONPATH", "")
    result = subprocess.run([SYSTEM_PYTHON, "-c", script, json.dumps(required)], capture_output=True, text=True, env=checked_env, timeout=180, check=False)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        report = {}
    if result.returncode != 0 or not report.get("ok"):
        raise RuntimeError("runtime_required_packages_failed:" + json.dumps(report, separators=(",", ":"))[:500])
    return report


def preflight_release(release, manifest, env):
    packages = check_required_packages(manifest, env)
    result = subprocess.run([SYSTEM_PYTHON, str(Path(__file__).with_name("preflight.py")), "--manifest", str(release / "manifest.json")], env=env, capture_output=True, text=True, timeout=180, check=False)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        report = {}
    compatibility = report.get("compatibility", {})
    if result.returncode != 0 or not compatibility.get("ok"):
        raise RuntimeError("runtime_preflight_failed:" + json.dumps(compatibility, separators=(",", ":"))[:500])
    return {"compatibility": compatibility, "packages": packages}


def fingerprint(path):
    value = path.lstat()
    return [value.st_mode, value.st_size if not path.is_dir() else None, value.st_mtime_ns if not path.is_dir() else None, value.st_ctime_ns if not path.is_dir() else None, value.st_ino, os.readlink(path) if path.is_symlink() else hash_file(path) if path.is_file() else None]


def snapshot_tree(release):
    # Root-owned, mode-0600 receipt records complete hashes of assets verified
    # during installation, including bytecode and internal symlink chains.
    result = {}
    for directory, dirs, files in os.walk(release, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            relative = str(item.relative_to(release))
            if relative == STATE_FILE:
                continue
            result[relative] = fingerprint(item)
    return result


def validate_tree(release, snapshot):
    try:
        return snapshot_tree(release) == snapshot
    except OSError:
        return False


def write_receipt(release, receipt):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=".receipt-", dir=release, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, release / STATE_FILE)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def valid_consumed_archives(release, manifest, receipt):
    policy = manifest.get("runtime_install") or {}
    specs = {item["path"]: item for item in policy.get("archives", [])}
    files = {item["path"]: item for item in manifest["files"]}
    consumed = receipt.get("consumed_archives", [])
    if not isinstance(consumed, list):
        return False
    seen = set()
    for record in consumed:
        if not isinstance(record, dict):
            return False
        name = record.get("path")
        spec, item = specs.get(name), files.get(name)
        if name in seen or not spec or not item or spec.get("discard_after_extract") is not True:
            return False
        if record != archive_record(spec, item, policy):
            return False
        source = safe_path(release, name)
        target = safe_path(release, record["target_subdir"])
        if source.exists() or source.is_symlink() or not target.is_dir() or record["target_subdir"] not in receipt.get("tree", {}):
            return False
        seen.add(name)
    # A missing signed file is legal only when the protected receipt proves the
    # exact imported archive. No file merely absent from disk can be inferred consumed.
    return all(safe_path(release, name).is_file() or name in seen for name in files)


def validated_cache(release, public_key, expected_digest=None, allow_pending=False):
    try:
        manifest = json.loads((release / "manifest.json").read_text())
        signature = (release / "manifest.sig").read_text().strip()
        digest, _ = validate_manifest(manifest, {"signature": signature}, public_key)
        receipt_path = release / STATE_FILE
        protection = receipt_path.lstat()
        if not stat.S_ISREG(protection.st_mode) or protection.st_uid != 0 or stat.S_IMODE(protection.st_mode) != 0o600:
            return None
        receipt = json.loads(receipt_path.read_text())
        phase = receipt.get("phase", "ready")
        if phase != "ready" and not (allow_pending and phase in {"staged", "preflight_failed"}):
            return None
        if receipt.get("manifest_sha256") != digest or (expected_digest and digest != expected_digest):
            return None
        if not valid_consumed_archives(release, manifest, receipt) or not validate_tree(release, receipt.get("tree", {})):
            return None
        verify_course_bundle(release, manifest, receipt)
        return manifest
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def verify_and_activate(root, release, manifest):
    receipt = json.loads((release / STATE_FILE).read_text())
    try:
        bundle = verify_course_bundle(release, manifest, receipt)
        catalog = experiment_catalog_manifest(manifest)
        report = preflight_release(release, manifest, process_environment(runtime_environment(release, manifest)))
        report["course_bundle"] = bundle
        report["experiment_catalog"] = (
            {"status": "ready", "experiment_count": len(catalog)}
            if catalog is not None else {"status": "legacy_fixed"}
        )
    except Exception as exc:
        receipt.update(phase="preflight_failed", error=str(exc)[:600] if isinstance(exc, RuntimeError) else type(exc).__name__)
        write_receipt(release, receipt)
        raise
    receipt["phase"] = "ready"
    receipt.pop("error", None)
    # Preflight imports the bundled runtime and may materialize bytecode or
    # other interpreter metadata.  Refresh the integrity snapshot after that
    # read-only validation so a verified release can be reused on the next
    # heartbeat instead of being downloaded and extracted again.
    receipt["tree"] = snapshot_tree(release)
    write_receipt(release, receipt)
    if (root / "current").is_symlink() and (root / "current").resolve() == release:
        return report
    # Downloads and candidate validation may proceed during student execution.
    # Only switching current needs the execution lock, and must never wait on
    # an open PTY. Keep this verified candidate for a later activation attempt.
    with (root / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current_revision = None
            try:
                current_revision = json.loads((root / "current" / "manifest.json").read_text()).get("artifact_revision")
            except (OSError, ValueError, AttributeError):
                pass
            report.update(status="deferred", update_pending=True,
                          desired_artifact_revision=manifest["artifact_revision"],
                          artifact_revision=current_revision)
            return report
        activate(root, release)
    return report


def activate(root, release):
    temporary = root / (".current-" + next(tempfile._get_candidate_names()))
    try:
        temporary.symlink_to(release, target_is_directory=True)
        os.replace(temporary, root / "current")
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_download_cache(root, kept_releases):
    """Remove only hash-verified cache files no kept release references."""
    cache = root / ".downloads"
    if not cache.is_dir() or cache.is_symlink():
        return {"removed": 0, "retained": 0}
    referenced = set()
    for release in kept_releases:
        try:
            manifest = json.loads((release / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        for item in manifest.get("files", []):
            if isinstance(item, dict) and re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or "")):
                referenced.add(item["sha256"])
    removed = retained = 0
    for item in list(cache.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r"[a-f0-9]{64}", item.name):
            retained += 1
            continue
        try:
            verified = hash_file(item) == item.name
        except OSError:
            verified = False
        if not verified or item.name in referenced:
            retained += 1
            continue
        try:
            item.unlink()
            removed += 1
        except OSError:
            retained += 1
    return {"removed": removed, "retained": retained}


def prune_releases(root, public_key, keep_verified=2, preserve=None):
    """Keep current plus one newest verified fallback; never remove control paths."""
    preserve = {Path(item).resolve() for item in (preserve or ())}
    current = root / "current"
    try:
        current_target = current.resolve(strict=True) if current.is_symlink() else None
    except OSError:
        current_target = None
    candidates = []
    for item in root.iterdir():
        if item.name in {"current", ".downloads", ".sync.lock"} or item.name.startswith("."):
            continue
        if item.is_symlink() or not item.is_dir():
            continue
        try:
            verified = validated_cache(item, public_key)
            mtime = item.stat().st_mtime_ns
        except OSError:
            verified, mtime = None, 0
        if verified is not None:
            candidates.append((item, mtime))
    candidates.sort(key=lambda pair: pair[1], reverse=True)
    keep = []
    if current_target is not None and current_target.is_dir() and root in current_target.parents:
        keep.append(current_target)
    for item, _mtime in candidates:
        if item in keep:
            continue
        if len(keep) >= max(1, int(keep_verified)):
            break
        keep.append(item)
    keep_set = set(keep)
    removed = []
    for item in root.iterdir():
        if item.name in {"current", ".downloads", ".sync.lock"} or item.name.startswith("."):
            continue
        if item.is_symlink() or not item.is_dir() or item in keep_set or item.resolve() in preserve:
            continue
        try:
            shutil.rmtree(item)
            removed.append(item.name)
        except OSError:
            continue
    cache = cleanup_download_cache(root, keep)
    return {
        "kept": [item.name for item in keep],
        "fallback": next((item.name for item in keep if current_target is None or item != current_target), None),
        "removed": removed,
        "downloads_removed": cache["removed"],
        "downloads_retained": cache["retained"],
    }


def offline_cache(root, public_key, reason):
    current = root / "current"
    if not current.is_symlink():
        raise RuntimeError("runtime_offline_no_verified_cache")
    release = current.resolve()
    if root not in release.parents:
        raise RuntimeError("runtime_cached_path_invalid")
    manifest = validated_cache(release, public_key)
    if manifest is None:
        raise RuntimeError("runtime_offline_cache_invalid")
    report = preflight_release(release, manifest, process_environment(runtime_environment(release, manifest)))
    return {"status": "offline_cached", "reason": reason, "artifact_revision": manifest["artifact_revision"], "cache_verified": True, "requires_server_revalidation": True, **report}


def synchronize(args):
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    headers = {"X-Runtime-Agent-Device": args.device_id, "X-Runtime-Agent-Token": args.token}
    server = args.server.rstrip("/")
    device_facts = facts()
    params = {"device_id": args.device_id, "architecture": device_facts.get("architecture") or "", "device_model": device_facts.get("device_model") or "", "facts": json.dumps(device_facts, separators=(",", ":"))}
    try:
        response = requests.get(server + "/api/runtime-agent/artifacts/resolve", params=params, headers=headers, timeout=60, verify=not args.insecure)
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # Compatibility rejection / malformed signed responses must fail closed.
        if status and status not in {401, 403, 408, 429, 500, 502, 503, 504}:
            raise RuntimeError("runtime_resolve_rejected:" + str(status)) from exc
        return offline_cache(root, args.artifact_public_key, "artifact_token_expired_or_rejected" if status in {401, 403} else "artifact_server_unavailable")
    payload = response.json()
    artifact, manifest = payload["artifact"], payload["manifest"]
    digest, signature = validate_manifest(manifest, artifact, args.artifact_public_key)
    revision = manifest["artifact_revision"]
    release = root / (revision + "-" + digest[:16])
    # Preserve a staged candidate so a failed preflight can be retried without
    # downloading its verified archives again.
    pending = validated_cache(release, args.artifact_public_key, digest, allow_pending=True) if release.is_dir() else None
    # Reclaim verified stale releases before measuring the candidate budget.
    # A board may retain a current release plus a fallback; keeping older
    # generations would make a valid update fail its own storage preflight.
    cleanup_before = prune_releases(root, args.artifact_public_key, preserve={release} if pending is not None else None)
    cached = pending
    if cached is not None:
        report = verify_and_activate(root, release, cached)
        cleanup = {} if report.get("status") == "deferred" else prune_releases(root, args.artifact_public_key)
        return {"status": "synced", "artifact_revision": revision, "cached": True, "file_count": len(manifest["files"]), "storage": {"cleanup": cleanup, "cleanup_before": cleanup_before}, **report}
    # A previous successful install may use an immutable suffix when the
    # canonical directory was incomplete. Reuse that verified current release
    # after server revalidation instead of downloading a duplicate copy.
    current = root / "current"
    try:
        current_release = current.resolve(strict=True) if current.is_symlink() else None
    except OSError:
        current_release = None
    if current_release is not None and current_release != release and root in current_release.parents:
        current_cached = validated_cache(current_release, args.artifact_public_key, digest)
        if current_cached is not None:
            report = verify_and_activate(root, current_release, current_cached)
            cleanup = {} if report.get("status") == "deferred" else prune_releases(root, args.artifact_public_key)
            return {"status": "synced", "artifact_revision": revision, "cached": True, "file_count": len(manifest["files"]), "storage": {"cleanup": cleanup, "cleanup_before": cleanup_before}, **report}
    storage = check_storage_capacity(root, manifest, install_userland=args.install_userland)
    storage["cleanup_before"] = cleanup_before
    # Never repair an installed/current directory in place. An invalid immutable
    # candidate gets a new suffix, preserving the last known working current.
    if release.exists():
        release = root / (release.name + "-" + next(tempfile._get_candidate_names()))
    release.mkdir(mode=0o755)
    release.chmod(0o755)
    consumed_archives = []
    try:
        cache = root / ".downloads"
        cache.mkdir(mode=0o700, exist_ok=True)
        for item in manifest["files"]:
            stored = cache / item["sha256"]
            url = server + "/api/runtime-agent/artifacts/" + quote(revision, safe="") + "/files/" + quote(item["path"], safe="/")
            download(url, stored, item["size"], item["sha256"], headers, not args.insecure)
            destination = safe_path(release, item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Move verified bytes on the same filesystem: a 32 GB board cannot
            # afford compressed payload copies in both download cache and release.
            os.replace(stored, destination)
            destination.chmod(int(item.get("mode", 0o644)) & 0o777)
        (release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False) + "\n")
        (release / "manifest.sig").write_text(signature + "\n")
        installed = install_userland(release, manifest, root, consumed_archives) if args.install_userland else {}
        env = runtime_environment(release, manifest)
        if not args.install_userland:
            write_environment(release, env)
        # Archives contain public runtime/data assets. Installer umask must not
        # make them unreadable to the unprivileged student experiment process.
        for directory, dirs, files in os.walk(release, followlinks=False):
            Path(directory).chmod((Path(directory).stat().st_mode & 0o777) | 0o555)
            for name in files:
                item = Path(directory) / name
                if not item.is_symlink():
                    item.chmod((item.stat().st_mode & 0o777) | 0o444)
        # Persist completed verified imports before preflight. A failed hardware
        # probe can retry this candidate without redownloading consumed payloads,
        # but pending candidates are never accepted as an offline runtime.
        receipt = {"manifest_sha256": digest, "phase": "staged", "consumed_archives": consumed_archives, "tree": snapshot_tree(release)}
        write_receipt(release, receipt)
        report = verify_and_activate(root, release, manifest)
        cleanup = {} if report.get("status") == "deferred" else prune_releases(root, args.artifact_public_key)
        storage["cleanup"] = cleanup
        return {"status": "synced", "artifact_revision": revision, "cached": False, "file_count": len(manifest["files"]), "userland": installed, "storage": storage, **report}
    except Exception as exc:
        if not (release / STATE_FILE).exists():
            write_receipt(release, {"manifest_sha256": digest, "phase": "installation_failed",
                                  "consumed_archives": consumed_archives,
                                  "error": str(exc)[:600] if isinstance(exc, RuntimeError) else type(exc).__name__})
        # A preflight failure already has a complete, hash-verified pending receipt.
        # An incomplete install is diagnostic only and cannot satisfy cache checks.
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--token", default=os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_TOKEN", ""))
    parser.add_argument("--root", default="/opt/smart-textbook/runtime-agent/releases")
    parser.add_argument("--install-userland", action="store_true")
    parser.add_argument("--artifact-public-key", default=os.getenv("SMART_TEXTBOOK_AGENT_ARTIFACT_SIGNING_PUBLIC_KEY", ""))
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = synchronize(args)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never include HTTP request URLs/headers, which can contain credentials.
        detail = str(exc)[:600] if isinstance(exc, RuntimeError) else type(exc).__name__
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": detail}, ensure_ascii=False))
        raise SystemExit(1)
