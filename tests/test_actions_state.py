"""Only temporary dummy credentials are used; no real state is inspected."""
import base64
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import stat

from cryptography.fernet import Fernet, InvalidToken
import pytest

spec = importlib.util.spec_from_file_location("actions_state", Path(__file__).parents[1] / "scripts/actions_state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("HORIZON_STATE_KEY", key.decode())
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    return source, destination, tmp_path / "bundle.enc", key


def write(root, name, content=b"dummy private content"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def bundle(output, key, files, day=None):
    document = {"version": 1, "day": day or state._today(), "files": {name: base64.b64encode(value).decode() for name, value in files.items()}}
    output.write_bytes(Fernet(key).encrypt(json.dumps(document).encode()))


def test_roundtrip_private_permissions_and_binary_contents(roots):
    source, destination, output, key = roots
    names = set(state.FIXED_PATHS) | {"data/wechat_subscribers/abcdef012345/wechat_session.json", f"data/summaries/horizon-{state._today()}-zh.md"}
    for name in names:
        write(source, name, b"dummy\x00\xff" + name.encode())
    assert state.pack(source, output) == len(names)
    assert b"dummy" not in output.read_bytes()
    assert state.restore(destination, output) == len(names)
    for name in names:
        target = destination / name
        assert target.read_bytes() == (source / name).read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_strict_allowlist_excludes_config_env_logs_locks_and_old_reports(roots):
    source, _, output, key = roots
    allowed = "data/wechat_session.json"
    write(source, allowed)
    for name in ["config/config.json", ".env", "data/logs/secret.log", "data/wechat_subscribers/delivery.lock", "data/wechat_subscribers/abcdef012345/binding.lock", "data/wechat_subscribers/not-safe/wechat_session.json", "data/summaries/horizon-2000-01-01-zh.md", "data/other.json"]:
        write(source, name)
    state.save(source, output)
    document = json.loads(Fernet(key).decrypt(output.read_bytes()))
    assert set(document["files"]) == {allowed}


def test_tampered_ciphertext_never_writes(roots):
    source, destination, output, _ = roots
    write(source, "data/wechat_session.json")
    state.save(source, output)
    token = bytearray(output.read_bytes())
    token[len(token) // 2] ^= 1
    output.write_bytes(token)
    with pytest.raises(InvalidToken):
        state.restore(destination, output)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "data/../outside", "data//wechat_session.json", "data/./wechat_session.json", "data\\wechat_session.json", "data/wechat_subscribers/../../oops/wechat_session.json", "data/wechat_subscribers/INVALID/wechat_session.json", "data/wechat_subscribers/abcdef012345/binding.lock", "config/config.json", ".env"])
def test_authenticated_malicious_paths_rejected_before_any_write(roots, name):
    _, destination, output, key = roots
    bundle(output, key, {"data/wechat_session.json": b"valid", name: b"attack"})
    with pytest.raises(ValueError):
        state.restore(destination, output)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("component", ["data", "data/chatgpt-auth", "data/chatgpt-auth/credentials.json", "data/wechat_subscribers/abcdef012345"])
def test_pack_rejects_allowlisted_symlink_components(roots, component, tmp_path):
    source, _, output, _ = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    path = source / component
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        state.save(source, output)
    assert not output.exists()


def test_restore_rejects_symlink_before_any_file_written(roots, tmp_path):
    _, destination, output, key = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "data").symlink_to(outside, target_is_directory=True)
    bundle(output, key, {"data/wechat_session.json": b"dummy"})
    with pytest.raises(ValueError):
        state.restore(destination, output)
    assert not list(outside.iterdir())


def test_pack_size_limit_covers_base64_json_overhead(roots, monkeypatch):
    source, _, output, _ = roots
    monkeypatch.setattr(state, "MAX_PAYLOAD_BYTES", 500)
    write(source, "data/wechat_session.json", b"a" * 400)
    with pytest.raises(ValueError):
        state.save(source, output)
    assert not output.exists()


def test_restore_size_limit(roots, monkeypatch):
    _, destination, output, key = roots
    bundle(output, key, {"data/wechat_session.json": b"a" * 1000})
    monkeypatch.setattr(state, "MAX_PAYLOAD_BYTES", 500)
    with pytest.raises(ValueError):
        state.restore(destination, output)
    assert not list(destination.iterdir())


def test_missing_key_and_cli_only_print_exception_type(roots, monkeypatch, capsys):
    source, destination, output, _ = roots
    monkeypatch.delenv("HORIZON_STATE_KEY")
    with pytest.raises(RuntimeError):
        state.save(source, output)
    assert state.main(["save", "--root", str(source), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "actions_state: RuntimeError\n"
    assert not output.exists()


def test_cli_error_does_not_print_secret_exception_message(roots, monkeypatch, capsys):
    source, _, output, _ = roots
    def fail(*args):
        raise ValueError("DUMMY_SECRET_MUST_NOT_PRINT")
    monkeypatch.setattr(state, "save", fail)
    assert state.main(["pack", "--root", str(source), "--output", str(output)]) == 1
    assert capsys.readouterr().err == "actions_state: ValueError\n"


def test_previous_day_restores_ledger_but_not_stale_report(roots):
    _, destination, output, key = roots
    yesterday = (date.fromisoformat(state._today()) - timedelta(days=1)).isoformat()
    report = f"data/summaries/horizon-{yesterday}-zh.md"
    bundle(output, key, {"data/wechat_subscribers/delivery.json": b"ledger", report: b"stale report"}, day=yesterday)
    assert state.restore(destination, output) == 1
    assert (destination / "data/wechat_subscribers/delivery.json").read_bytes() == b"ledger"
    assert not (destination / report).exists()


def test_report_must_match_authenticated_bundle_day(roots):
    _, destination, output, key = roots
    bundle(output, key, {"data/summaries/horizon-2000-01-01-zh.md": b"dummy"})
    with pytest.raises(ValueError):
        state.restore(destination, output)


def test_atomic_replace_keeps_original_on_write_failure(roots, monkeypatch):
    source, _, output, _ = roots
    output.write_bytes(b"previous encrypted bundle")
    write(source, "data/wechat_session.json")
    def fail(*args):
        raise OSError("simulated failure")
    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(OSError):
        state.save(source, output)
    assert output.read_bytes() == b"previous encrypted bundle"
    assert not list(output.parent.glob(".state-*"))


def test_cli_roundtrip_is_silent(roots, capsys):
    source, destination, output, _ = roots
    write(source, "data/seen.json")
    assert state.main(["save", "--root", str(source), "--output", str(output)]) == 0
    assert state.main(["restore", "--root", str(destination), "--input", str(output)]) == 0
    assert capsys.readouterr() == ("", "")
