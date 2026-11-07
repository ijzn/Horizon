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


def test_analysis_snapshot_is_private_state_not_public_archive(roots):
    source, destination, output, key = roots
    name = 'data/analysis-snapshot.json'
    snapshot = b'{"items":[{"score":6,"reason":"public source lacks concrete steps"}]}'
    write(source, name, snapshot)
    write(source, 'data/public-briefings/analysis-snapshot.json', b'must not publish')
    assert state.pack(source, output) == 1
    document = json.loads(Fernet(key).decrypt(output.read_bytes()))
    assert set(document['files']) == {name}
    assert state.restore(destination, output) == 1
    assert (destination / name).read_bytes() == snapshot
    assert stat.S_IMODE((destination / name).stat().st_mode) == 0o600
    assert not (destination / 'data/public-briefings').exists()


def test_archive_pack_retains_90_days_and_dedicated_latest_files(roots, monkeypatch):
    source, destination, output, key = roots
    monkeypatch.setattr(state, "_today", lambda: "2026-11-07")
    today = date(2026, 11, 7)
    retained = {f"data/public-briefings/{(today - timedelta(days=age)).isoformat()}.html" for age in (0, 1, 89)}
    retained |= {"data/public-briefings/2026-11-07-abcdef012345.html", "data/public-briefings/2026-11-07-012345abcdef.html"}
    retained |= {"data/public-briefings/index.html", "data/wechat-preview.json"}
    excluded = {f"data/public-briefings/{(today - timedelta(days=90)).isoformat()}.html", "data/public-briefings/2026-11-08.html", "data/public-briefings/2026-02-30.html", "data/public-briefings/2026-1-01.html", "data/public-briefings/secret.json", "data/public-briefings/nested/2026-11-07.html"}
    for name in retained | excluded:
        write(source, name, name.encode())
    assert state.pack(source, output) == len(retained)
    assert set(json.loads(Fernet(key).decrypt(output.read_bytes()))["files"]) == retained
    assert state.restore(destination, output) == len(retained)
    for name in retained:
        assert (destination / name).read_bytes() == name.encode()


@pytest.mark.parametrize("name", [
    "data/public-briefings/2026-02-30.html",
    "data/public-briefings/2026-13-01.html",
    "data/public-briefings/2026-1-01.html",
    "data/public-briefings/2026-11-08.html",
    "data/public-briefings/nested/2026-11-07.html",
    "data/public-briefings/2026-11-07.htm",
    "data/public-briefings/../index.html",
    "data/public-briefings/2026-11-07-ABCDEF012345.html",
    "data/public-briefings/2026-11-07-abcdef.html",
    "data/public-briefings/2026-11-07-abcdef0123456.html",
    "data/public-briefings/2026-11-08-abcdef012345.html",
])
def test_restore_rejects_invalid_or_future_archive_before_writes(roots, name):
    _, destination, output, key = roots
    bundle(output, key, {"data/seen.json": b"valid", name: b"invalid"}, day="2026-11-07")
    with pytest.raises(ValueError):
        state.restore(destination, output)
    assert not list(destination.iterdir())


def test_cross_day_archive_survives_while_old_report_is_skipped(roots, monkeypatch):
    _, destination, output, key = roots
    monkeypatch.setattr(state, "_today", lambda: "2026-11-08")
    report = "data/summaries/horizon-2026-11-07-zh.md"
    archive = "data/public-briefings/2026-11-07-abcdef012345.html"
    bundle(output, key, {report: b"old report", archive: b"permanent link", "data/wechat-preview.json": b"preview", "data/public-briefings/index.html": b"latest"}, day="2026-11-07")
    assert state.restore(destination, output) == 3
    assert (destination / archive).read_bytes() == b"permanent link"
    assert not (destination / report).exists()


def test_restore_ages_archive_out_relative_to_current_day(roots, monkeypatch):
    _, destination, output, key = roots
    monkeypatch.setattr(state, "_today", lambda: "2026-11-08")
    archive = "data/public-briefings/2026-08-10.html"
    bundle(output, key, {archive: b"was 89 days old", "data/seen.json": b"ledger"}, day="2026-11-07")
    assert state.restore(destination, output) == 1
    assert not (destination / archive).exists()


def test_archive_symlink_is_rejected(roots, tmp_path, monkeypatch):
    source, _, output, _ = roots
    monkeypatch.setattr(state, "_today", lambda: "2026-11-07")
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"do not read")
    path = source / "data/public-briefings/2026-11-07.html"
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        state.pack(source, output)
    assert not output.exists()
