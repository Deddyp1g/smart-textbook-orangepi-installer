#!/usr/bin/env python3
"""Fail closed before publishing server-neutral installer sources or packages."""
import argparse
import ipaddress
from pathlib import Path
import re
import sys
import tarfile

IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
DOCUMENTATION = tuple(ipaddress.ip_network(value) for value in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
EXCLUDED = {'.git', 'dist', '__pycache__'}
REQUIRED = ('pair-server.py', 'preflight.py')

def check_text(name, raw, errors):
    if '\x00'.encode() in raw:
        return
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return
    for match in IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if not address.is_loopback and not any(address in network for network in DOCUMENTATION):
            errors.append(f'{name}: hardcoded IPv4 address is forbidden')
    if Path(name).name == 'runtime-agent.env.example':
        settings = dict(re.findall(r'^\s*(SMART_TEXTBOOK_AGENT_(?:SERVER_URL|ARTIFACT_SERVER_URL))=(.*)$', text, re.MULTILINE))
        for key in ('SMART_TEXTBOOK_AGENT_SERVER_URL', 'SMART_TEXTBOOK_AGENT_ARTIFACT_SERVER_URL'):
            if key not in settings or settings[key].strip():
                errors.append(f'{name}: {key} must be present and empty')

def private_config(path):
    name = path.name
    return name == '.env' or name.endswith('.env') or name.startswith('.env.') or name.endswith(('.key', '.pem'))

def verify(root, package=None):
    errors = []
    for name in (*REQUIRED, 'runtime-agent.env.example'):
        if not (root / name).is_file():
            errors.append(f'{name}: required installer source missing')
    for path in root.rglob('*'):
        if any(part in EXCLUDED for part in path.relative_to(root).parts) or path.is_symlink() or not path.is_file() or private_config(path):
            continue
        check_text(str(path.relative_to(root)), path.read_bytes(), errors)
    if package:
        with tarfile.open(package, 'r:*') as archive:
            files = {member.name.rstrip('/').split('/')[-1] for member in archive.getmembers() if member.isfile()}
            for name in REQUIRED:
                if name not in files:
                    errors.append(f'{name}: required file missing from package')
    return errors

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    try:
        errors = verify(args.root.resolve(), args.package)
    except (OSError, tarfile.TarError) as exc:
        print(f'public configuration check failed: {exc}', file=sys.stderr)
        return 1
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Public installer configuration verified')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
