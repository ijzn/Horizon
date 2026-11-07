"""Encrypt only the runtime state needed by ephemeral Actions jobs.

No archive extraction, credentials on stdout, configuration, logs or lock files.
Writes are atomic per file; callers must serialize jobs while taking snapshots.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_FILES = 1024
FIXED_PATHS = frozenset({
    "data/chatgpt-auth/credentials.json", "data/chatgpt-auth/host.json",
    "data/wechat_session.json", "data/wechat_subscribers/subscribers.json",
    "data/wechat_subscribers/delivery.json", "data/seen.json", "data/ready.json",
})
SUBSCRIBER_ID = re.compile(r"(?:owner|[a-f0-9]{12})\Z")
SUBSCRIBER_PATH = re.compile(r"data/wechat_subscribers/(?:owner|[a-f0-9]{12})/wechat_session\.json\Z")
REPORT_PATH = re.compile(r"data/summaries/horizon-(\d{4}-\d{2}-\d{2})-(?:zh|en)\.md\Z")


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _cipher() -> Fernet:
    key = os.environ.get("HORIZON_STATE_KEY")
    if not key:
        raise RuntimeError("Missing state encryption key")
    return Fernet(key.encode("ascii"))


def _root(value: str | Path) -> Path:
    root = Path(value).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid state root")
    return root.resolve()


def _allowed(name: str, day: str) -> bool:
    if not isinstance(name, str) or "\\" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(p in {".", ".."} for p in path.parts):
        return False
    if name in FIXED_PATHS or SUBSCRIBER_PATH.fullmatch(name):
        return True
    report = REPORT_PATH.fullmatch(name)
    return bool(report and report.group(1) == day)


def _check_path(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are forbidden in state paths")
        if current.exists() and current != root / relative and not current.is_dir():
            raise ValueError("Invalid state parent")
    return current


def _read(path: Path, limit: int) -> bytes:
    # O_NOFOLLOW closes the final-component symlink race; do not follow FIFOs.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid or oversized state file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("State size limit exceeded")
        return data


def _atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("Invalid output path")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_parents(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlinks are forbidden in state paths")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def _candidates(root: Path, day: str) -> list[str]:
    names = set(FIXED_PATHS)
    directory = _check_path(root, "data/wechat_subscribers")
    if directory.exists():
        for child in directory.iterdir():
            if SUBSCRIBER_ID.fullmatch(child.name):
                names.add(f"data/wechat_subscribers/{child.name}/wechat_session.json")
    for language in ("zh", "en"):
        names.add(f"data/summaries/horizon-{day}-{language}.md")
    return sorted(names)


def save(root: str | Path, output: str | Path) -> int:
    """Save an authenticated encrypted allowlist snapshot; return file count."""
    cipher = _cipher()
    root = _root(root)
    day = _today()
    files = {}
    total = 0
    for relative in _candidates(root, day):
        path = _check_path(root, relative)
        if not path.exists():
            continue
        content = _read(path, MAX_PAYLOAD_BYTES - total)
        total += len(content)
        files[relative] = base64.b64encode(content).decode("ascii")
        if len(files) > MAX_FILES:
            raise ValueError("Too many state files")
    plaintext = json.dumps({"version": 1, "day": day, "files": files}, separators=(",", ":")).encode("utf-8")
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise ValueError("State payload limit exceeded")
    _atomic_write(Path(output).absolute(), cipher.encrypt(plaintext))
    return len(files)


# Explicit synonym for initial local packaging.
pack = save


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate state key")
        result[key] = value
    return result


def restore(root: str | Path, input_path: str | Path) -> int:
    """Validate the whole authenticated payload before restoring private files.

    Yesterday's report is deliberately skipped, preserving same-day retry only.
    Other state, including deduplication ledgers, is retained across days.
    """
    cipher = _cipher()
    root = _root(root)
    token = _read(Path(input_path), ((MAX_PAYLOAD_BYTES + 128) * 4 // 3) + 1024)
    plaintext = cipher.decrypt(token)
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise ValueError("State payload limit exceeded")
    document = json.loads(plaintext, object_pairs_hook=_unique_object)
    if not isinstance(document, dict) or set(document) != {"version", "day", "files"} or type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("Invalid state document")
    day, files = document["day"], document["files"]
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid state date")
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("Invalid state files")
    validated = []
    total = 0
    for relative, encoded in files.items():
        if not _allowed(relative, day) or not isinstance(encoded, str):
            raise ValueError("Forbidden state path or encoding")
        path = _check_path(root, relative)
        if path.exists() and not path.is_file():
            raise ValueError("Invalid state target")
        content = base64.b64decode(encoded, validate=True)
        total += len(content)
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("State size limit exceeded")
        if not REPORT_PATH.fullmatch(relative) or day == _today():
            validated.append((path, content))
    for path, content in validated:
        _private_parents(root, path)
        _atomic_write(path, content)
    return len(validated)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted Horizon Actions runtime state")
    parser.add_argument("operation", choices=("save", "pack", "restore"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "restore":
            if args.input is None or args.output is not None:
                raise ValueError("Restore requires input only")
            restore(args.root, args.input)
        else:
            if args.output is None or args.input is not None:
                raise ValueError("Save requires output only")
            save(args.root, args.output)
    except Exception as error:
        # Exception messages can include secrets or attacker-controlled contents.
        print(f"actions_state: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
