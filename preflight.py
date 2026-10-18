#!/usr/bin/env python3
"""Read-only Orange Pi Ascend/CANN/MindSpore preflight."""
from __future__ import annotations
import argparse, datetime as _dt, importlib.metadata, importlib.util, json, os, platform, re, shutil, subprocess, sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "smart-textbook/orangepi-preflight/v1"
DEFAULT_TOOLKIT_ROOTS = ("/usr/local/Ascend/ascend-toolkit/latest", "/usr/local/Ascend/ascend-toolkit", "/usr/local/Ascend/ascend-toolkit/8.0.RC3")
VERSION_RE = re.compile(r"\d+(?:\.\d+){0,5}(?:[-+._][0-9A-Za-z.-]+)?")
KV_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$")

def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")

def _read_text(path: Path, limit: int = 256 * 1024) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, UnicodeError):
        return None

def _parse_kv(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if text:
        for line in text.splitlines():
            match = KV_RE.match(line)
            if match:
                result[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return result

def _first_version(value: Any) -> str | None:
    match = VERSION_RE.search(str(value)) if value is not None else None
    return match.group(0) if match else None

def _version_tuple(value: Any) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-._]?[A-Za-z]+\d*(?:\.\d+)*)?", str(value or "").strip())
    return tuple(map(int, match.group(1).split("."))) if match else None


def _version_key(value: Any) -> tuple | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-._]?([A-Za-z]+)(\d*(?:\.\d+)*))?", str(value or "").strip())
    if not match:
        return None
    core = tuple(map(int, match.group(1).split(".")))
    suffix = (match.group(2) or "").lower()
    rank = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "rc": -1, "": 0}.get(suffix, 1)
    tail = tuple(map(int, match.group(3).split("."))) if match.group(3) else ()
    return core + (0,) * (6-len(core)), rank, suffix, tail


def _compare(actual: Any, expected: Any) -> tuple[bool, str]:
    if expected is None:
        return True, ""
    if isinstance(expected, (list, tuple, set)):
        return any(_compare(actual, option)[0] for option in expected), "expected_one_of:" + ",".join(map(str, expected))
    if isinstance(expected, Mapping):
        if "one_of" in expected or "any" in expected:
            return _compare(actual, expected.get("one_of", expected.get("any")))
        for name, op in (("exact", "=="), ("min", ">="), ("minimum", ">="), ("max", "<="), ("maximum", "<=")):
            if name in expected:
                ok, reason = _compare(actual, op + str(expected[name]))
                if not ok:
                    return ok, reason
        return True, ""
    text = str(expected).strip()
    if "," in text:
        checks = [_compare(actual, part.strip()) for part in text.split(",") if part.strip()]
        return all(ok for ok, _ in checks), "constraint_mismatch:" + text
    if text.startswith("~") and not text.startswith("~="):
        wanted, got = _version_tuple(text[1:]), _version_tuple(actual)
        return wanted is not None and got is not None and got[:2] == wanted[:2], "outside_minor:" + text
    match = re.fullmatch(r"(>=|<=|==|=|>|<|~=)\s*(.+)", text)
    op, wanted = (match.group(1), match.group(2)) if match else ("==", text)
    if op in ("==", "="):
        return str(actual or "").strip().casefold() == wanted.casefold(), "expected_exact:" + wanted
    av, ev = _version_key(actual), _version_key(wanted)
    if av is None or ev is None:
        return False, "invalid_version_constraint:" + text
    if op == ">=": ok = av >= ev
    elif op == "<=": ok = av <= ev
    elif op == ">": ok = av > ev
    elif op == "<": ok = av < ev
    else:
        prefix = max(1, len(_version_tuple(wanted) or ()) - 1)
        ok = av[0][:prefix] == ev[0][:prefix] and av >= ev
    return ok, "constraint_mismatch:" + text


def _version_compare(actual: Any, expected: Any) -> bool | None:
    return None if actual is None else _compare(actual, expected)[0]


def _normalize_arch(value: Any) -> str:
    value = str(value or "").lower().replace("-", "_")
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(value, value)

def _run(command: Sequence[str], timeout: float = 10.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(list(command), check=False, capture_output=True, text=True, timeout=timeout)
        return {"command": list(command), "exit_code": completed.returncode, "stdout": completed.stdout[-16384:], "stderr": completed.stderr[-8192:]}
    except FileNotFoundError:
        return {"command": list(command), "exit_code": None, "stdout": "", "stderr": "command_not_found"}
    except subprocess.TimeoutExpired:
        return {"command": list(command), "exit_code": None, "stdout": "", "stderr": "timeout"}
    except OSError as exc:
        return {"command": list(command), "exit_code": None, "stdout": "", "stderr": str(exc)[:512]}

def _package(name: str) -> dict[str, Any]:
    if name == "python_abi":
        abi = getattr(sys.implementation, "cache_tag", None)
        return {"installed": bool(abi), "version": abi, "name": name}
    try:
        distribution = importlib.metadata.distribution(name)
        return {"installed": True, "version": distribution.version, "location": str(distribution.locate_file("")), "name": distribution.metadata.get("Name", name)}
    except importlib.metadata.PackageNotFoundError:
        return {"installed": False, "version": None, "name": name}
    except Exception as exc:
        return {"installed": False, "version": None, "name": name, "error": str(exc)[:256]}

def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False

def _find_toolkit_roots() -> list[Path]:
    values = [os.environ[key] for key in ("ASCEND_TOOLKIT_HOME", "ASCEND_HOME_PATH") if os.environ.get(key)]
    explicit = bool(values)
    if not explicit:
        values.extend(DEFAULT_TOOLKIT_ROOTS)
    found: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_file(): path = path.parent
        if path.is_dir() and path not in found: found.append(path)
    if explicit:
        return found
    latest = Path("/usr/local/Ascend/ascend-toolkit/latest")
    try:
        resolved = latest.resolve()
        if resolved.is_dir() and resolved not in found: found.insert(0, resolved)
    except OSError:
        pass
    return found

def _ascend_facts() -> dict[str, Any]:
    install_path = Path("/etc/ascend_install.info")
    roots = _find_toolkit_roots()
    toolkit: dict[str, Any] = {"roots": [str(item) for item in roots], "components": {}}
    for root in roots:
        paths: list[Path] = []
        if (root / "version.info").is_file(): paths.append(root / "version.info")
        try: paths.extend(sorted(root.glob("*/version.info")))
        except OSError: pass
        for path in paths:
            parsed = _parse_kv(_read_text(path))
            if not parsed: continue
            name = path.parent.name
            toolkit["components"].setdefault(name, parsed)
            toolkit["components"][name]["path"] = str(path)
            if name in ("toolkit", "runtime") or "version_dir" in parsed:
                toolkit.setdefault("version", parsed.get("Version"))
                toolkit.setdefault("version_dir", parsed.get("version_dir"))
    if roots:
        for candidate in (roots[0] / "toolkit/version.info", roots[0] / "version.info"):
            parsed = _parse_kv(_read_text(candidate))
            if parsed:
                toolkit["version"] = parsed.get("Version", toolkit.get("version"))
                toolkit["version_dir"] = parsed.get("version_dir", toolkit.get("version_dir"))
                toolkit["version_info_path"] = str(candidate)
                break
    return {"install_info_path": str(install_path), "install_info": _parse_kv(_read_text(install_path)), "toolkit": toolkit}

def _npu_facts() -> dict[str, Any]:
    candidates = [os.environ.get("NPU_SMI"), shutil.which("npu-smi"), "/usr/local/sbin/npu-smi", "/usr/local/bin/npu-smi", "/usr/bin/npu-smi"]
    executable = next((item for item in candidates if item and Path(item).exists()), None)
    result: dict[str, Any] = {"available": bool(executable), "executable": executable, "devices": [], "warnings": []}
    if not executable:
        result["warning"] = "npu_smi_missing"
        return result
    info, version = _run([executable, "info"], 8), _run([executable, "-v"], 8)
    raw = (info.get("stdout") or "") + "\n" + (version.get("stdout") or "")
    match = re.search(r"(?:npu-smi[^\n]*?Version:\s*|npu-smi version:\s*)([^\s|]+)", raw, re.I)
    result.update({"info": info, "version_command": version, "version": match.group(1) if match else None, "raw_version": _first_version(version.get("stdout"))})
    devices: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in (info.get("stdout") or "").splitlines():
        match = re.match(r"\s*([^:|]+?)\s*:\s*(.*?)\s*$", line)
        if match:
            key, value = match.group(1).strip().lower().replace(" ", "_"), match.group(2).strip()
            if key in {"npu_id", "chip_id"} and current and "npu_id" in current:
                devices.append(current); current = {}
            current[key] = value
    if current: devices.append(current)
    # npu-smi 25.x renders the primary device as a pipe-delimited table.
    for line in (info.get("stdout") or "").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        first = re.match(r"(\d+)\s+(.+)$", cells[0]) if cells else None
        if len(cells) >= 3 and first and cells[1] and not cells[1].lower().startswith(("name", "device")):
            if first.group(2).strip().isdigit() and cells[1].strip().upper() == "NA":
                continue
            candidate = {"npu_id": first.group(1), "name": first.group(2).strip(), "health": cells[1], "power(w)": cells[2].split()[0], "power_details": cells[2]}
            if not any(d.get("npu_id") == candidate["npu_id"] and d.get("health") == candidate["health"] for d in devices):
                devices.append(candidate)
    # Board detail carries firmware on Atlas images; keep it best-effort.
    board = _run([executable, "info", "-t", "board", "-i", "0"], 8)
    result["board"] = board
    board_text = board.get("stdout") or ""
    firmware = re.search(r"Firmware Version\s*:\s*([^\s]+)", board_text, re.I)
    if firmware and devices:
        devices[0]["firmware_version"] = firmware.group(1)
    result["devices"] = devices
    for device in devices:
        health = str(device.get("health", "")).lower()
        power = str(device.get("power(w)", device.get("power", ""))).lower()
        if health and health not in {"ok", "normal", "healthy", "na"}:
            result["warnings"].append({"code": "npu_health_advisory", "health": device.get("health")})
        if re.match(r"0+(?:\.0+)?\s*(?:w)?$", power):
            result["warnings"].append({"code": "npu_power_zero_advisory", "power": device.get("power(w)", device.get("power"))})
    return result

def collect_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {
        "schema": SCHEMA, "collected_at": utc_now(),
        "system": {"hostname": platform.node(), "machine": platform.machine(), "architecture": _normalize_arch(platform.machine()), "kernel": platform.release(), "platform": platform.platform(), "python_implementation": platform.python_implementation(), "os_release": _parse_kv(_read_text(Path("/etc/os-release")))},
        "python": {"executable": sys.executable, "version": platform.python_version(), "implementation": platform.python_implementation(), "abi": getattr(sys.implementation, "cache_tag", None), "prefix": sys.prefix, "env": {key: os.environ[key] for key in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "ASCEND_OPP_PATH") if os.environ.get(key)}},
        "packages": {name: _package(name) for name in ("mindspore", "mindvision", "numpy", "scipy", "te", "hccl")},
        "modules": {name: _module_available(name) for name in ("mindspore", "mindvision", "acl", "te", "hccl")},
        "ascend": _ascend_facts(), "npu_smi": _npu_facts(),
    }
    meminfo = _read_text(Path("/proc/meminfo"), 32 * 1024)
    if meminfo:
        facts["system"]["meminfo"] = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in meminfo.splitlines() if ":" in line}
    return facts

def _actual_values(facts: Mapping[str, Any]) -> dict[str, Any]:
    packages, ascend = facts.get("packages", {}), facts.get("ascend", {})
    toolkit = ascend.get("toolkit", {}) if isinstance(ascend, Mapping) else {}
    components = toolkit.get("components", {}) if isinstance(toolkit, Mapping) else {}
    return {
        "kernel": facts.get("system", {}).get("kernel"),
        "architecture": facts.get("system", {}).get("architecture"), "arch": facts.get("system", {}).get("architecture"),
        "python": facts.get("python", {}).get("version"), "python_version": facts.get("python", {}).get("version"), "python_abi": facts.get("python", {}).get("abi"),
        **{name: packages.get(name, {}).get("version") for name in ("mindspore", "mindvision", "numpy", "scipy", "te", "hccl")},
        "cann": toolkit.get("version_dir") or toolkit.get("version"), "ascend_toolkit": toolkit.get("version_dir") or toolkit.get("version"), "cann_component": toolkit.get("version"),
        "acl": next(((data.get("Version") or data.get("version")) for name, data in components.items() if str(name).lower() in {"pyacl", "acl"} and isinstance(data, Mapping) and (data.get("Version") or data.get("version"))), None),
        "driver": facts.get("npu_smi", {}).get("version"), "npu_smi": facts.get("npu_smi", {}).get("version"),
        "firmware": next((d.get("firmware_version") for d in facts.get("npu_smi", {}).get("devices", []) if d.get("firmware_version")), None),
        "npu_model": next((d.get("name") for d in facts.get("npu_smi", {}).get("devices", []) if d.get("name")), None),
        "model": next((d.get("name") for d in facts.get("npu_smi", {}).get("devices", []) if d.get("name")), None),
    }

def _requirements(manifest: Mapping[str, Any]) -> list[tuple[str, Any, str]]:
    reqs: list[tuple[str, Any, str]] = []
    aliases = {"kernel": "kernel", "architecture": "architecture", "arch": "architecture", "machine": "architecture", "device_model": "npu_model", "python": "python", "python_version": "python", "python_abi": "python_abi", "mindspore": "mindspore", "mindspore_version": "mindspore", "mindvision": "mindvision", "numpy": "numpy", "scipy": "scipy", "te": "te", "hccl": "hccl", "cann": "cann", "cann_version": "cann", "ascend_toolkit": "ascend_toolkit", "toolkit": "ascend_toolkit", "driver": "driver", "driver_version": "driver", "npu_smi": "npu_smi", "firmware": "firmware", "firmware_version": "firmware", "acl": "acl", "acl_version": "acl", "npu_model": "npu_model", "model": "npu_model", "device_model": "npu_model"}
    for key, actual in aliases.items():
        if key in manifest: reqs.append((actual, manifest[key], key))
    for section, actual in (("python", "python"), ("mindspore", "mindspore"), ("mindvision", "mindvision"), ("cann", "cann"), ("ascend", "ascend_toolkit"), ("ascend_toolkit", "ascend_toolkit"), ("driver", "driver"), ("npu_smi", "npu_smi")):
        value = manifest.get(section)
        if isinstance(value, Mapping):
            candidate = value.get("version", value.get("exact", value.get("constraint")))
            if candidate is not None: reqs.append((actual, candidate, f"{section}.version"))
            for subkey in ("min", "minimum", "max", "maximum"):
                if subkey in value: reqs.append((actual, {subkey: value[subkey]}, f"{section}.{subkey}"))
    system = manifest.get("system")
    if isinstance(system, Mapping) and system.get("architecture") is not None:
        reqs.append(("architecture", system["architecture"], "system.architecture"))
    package_map = manifest.get("packages", manifest.get("requires", manifest.get("required", manifest.get("requirements", {}))))
    if isinstance(package_map, Mapping):
        for package, expected in package_map.items():
            if package in {"mindspore", "mindvision", "numpy", "scipy", "te", "hccl"}: reqs.append((package, expected, f"packages.{package}"))
    return reqs

def evaluate_manifest(facts: Mapping[str, Any], manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    if manifest is None: return {"status": "not_checked", "ok": True, "reasons": [], "warnings": []}
    actual, reasons, warnings = _actual_values(facts), [], []
    normalized_manifest = dict(manifest)
    compatibility = manifest.get("compatibility")
    if isinstance(compatibility, Mapping):
        aliases = {"cann_version": "cann", "driver_version": "driver", "firmware_version": "firmware", "mindspore_version": "mindspore", "python_abi": "python_abi", "acl_version": "acl", "device_model": "device_model"}
        for key, value in compatibility.items(): normalized_manifest[aliases.get(key, key)] = value
        normalized_manifest.pop("compatibility", None)
    for field, expected, source in _requirements(normalized_manifest):
        observed = actual.get(field)
        if observed is None:
            reasons.append({"code": "fact_missing", "field": source, "expected": expected, "actual": None}); continue
        if field == "architecture":
            values = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if _normalize_arch(observed) not in {_normalize_arch(value) for value in values}: reasons.append({"code": "incompatible", "field": source, "expected": expected, "actual": observed})
        elif field == "npu_model":
            values = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if str(observed).lower() not in {str(value).lower() for value in values}:
                reasons.append({"code": "incompatible", "field": source, "expected": expected, "actual": observed})
        elif _version_compare(observed, expected) is not True:
            reasons.append({"code": "incompatible", "field": source, "expected": expected, "actual": observed})
    policy = manifest.get("runtime_install") or {}
    if isinstance(policy, Mapping):
        required = policy.get("required_packages") or {}
        if not isinstance(required, Mapping):
            reasons.append({"code": "invalid_required_packages", "field": "runtime_install.required_packages", "expected": "mapping", "actual": None})
        else:
            for name, expected in required.items():
                package = facts.get("packages", {}).get(name, {})
                if not isinstance(package, Mapping) or package.get("installed") is not True:
                    reasons.append({"code": "fact_missing", "field": "packages." + name, "expected": expected, "actual": None})
                elif not _version_compare(package.get("version"), expected):
                    reasons.append({"code": "incompatible", "field": "packages." + name, "expected": expected, "actual": package.get("version")})
        provided = policy.get("provides") or {}
        # A bundle's declared outputs remain strict requirements after install.
        if isinstance(provided, Mapping):
            aliases = {"cann_version": "cann", "acl_version": "acl", "mindspore_version": "mindspore", "python_abi": "python_abi"}
            for name, expected in provided.items():
                field = aliases.get(name, name)
                if field in {"cann", "acl", "mindspore", "python_abi"} and not _version_compare(actual.get(field), expected):
                    reasons.append({"code": "incompatible", "field": "runtime_install.provides." + name, "expected": expected, "actual": actual.get(field)})
    known = {"kernel", "architecture", "arch", "machine", "python", "python_version", "python_abi", "mindspore", "mindspore_version", "mindvision", "numpy", "scipy", "te", "hccl", "cann", "cann_version", "acl", "acl_version", "ascend", "ascend_toolkit", "toolkit", "driver", "driver_version", "npu_smi", "firmware", "firmware_version", "npu_model", "model", "device_model", "packages", "requires", "required", "requirements", "system", "compatibility", "schema_version", "artifact_revision", "files", "created_at", "install_policy", "runtime_install", "platform"}
    warnings.extend({"code": "unsupported_manifest_field", "field": key} for key in manifest if key not in known)
    warnings.extend(facts.get("npu_smi", {}).get("warnings", []))
    return {"status": "compatible" if not reasons else "incompatible", "ok": not reasons, "reasons": reasons, "warnings": warnings, "actual": actual}

def run_preflight(manifest: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    facts = collect_facts()
    policy = (manifest or {}).get("runtime_install") or {}
    required = policy.get("required_packages") if isinstance(policy, Mapping) else None
    if isinstance(required, Mapping):
        for name in required:
            facts["packages"][name] = _package(name)
    compatibility = evaluate_manifest(facts, manifest)
    return {**facts, "compatibility": compatibility}, 0 if compatibility.get("ok", False) else 2

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--manifest"); parser.add_argument("--pretty", action="store_true"); args = parser.parse_args(argv)
    manifest: Mapping[str, Any] | None = None
    if args.manifest:
        path = Path(args.manifest)
        try: data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(json.dumps({"schema": SCHEMA, "collected_at": utc_now(), "compatibility": {"status": "incompatible", "ok": False, "reasons": [{"code": "manifest_missing", "field": "manifest", "expected": str(path), "actual": None}], "warnings": []}}, ensure_ascii=False, indent=2 if args.pretty else None)); return 2
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"schema": SCHEMA, "collected_at": utc_now(), "compatibility": {"status": "incompatible", "ok": False, "reasons": [{"code": "manifest_invalid", "field": "manifest", "expected": "JSON object", "actual": str(exc)}], "warnings": []}}, ensure_ascii=False, indent=2 if args.pretty else None)); return 2
        if not isinstance(data, Mapping):
            print(json.dumps({"schema": SCHEMA, "collected_at": utc_now(), "compatibility": {"status": "incompatible", "ok": False, "reasons": [{"code": "manifest_invalid", "field": "manifest", "expected": "JSON object", "actual": type(data).__name__}], "warnings": []}}, ensure_ascii=False, indent=2 if args.pretty else None)); return 2
        manifest = data
    result, status = run_preflight(manifest); print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True)); return status

if __name__ == "__main__":
    raise SystemExit(main())
