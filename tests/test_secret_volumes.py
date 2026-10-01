"""Synthetic Kubernetes AtomicWriter projections; no account/network access."""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from unittest.mock import Mock

import aquaservice
import pytest

from aquaservice2mqtt.adapter import NextDeliveryAdapter
from aquaservice2mqtt.cli import main
from aquaservice2mqtt.config import ENV_PREFIX, load_config


def credentials(token="first"):
    return {
        "application": "CLWEB",
        "token": token,
        "pin": "synthetic",
        "contract": "synthetic",
        "delegation": "synthetic",
        "accountFilter": "synthetic",
    }


def write_secret(path, mode, token="first"):
    path.write_text(json.dumps(credentials(token)), encoding="utf-8")
    path.chmod(mode)


def project(root, version, mode=0o440):
    target = root / (".." + version)
    target.mkdir()
    write_secret(target / "credentials.json", mode, version)
    (target / "config.toml").write_text("credentialspath = 'credentials.json'\n")
    pending = root / "..data-new"
    pending.symlink_to(target.name, target_is_directory=True)
    os.replace(pending, root / "..data")
    for name in ("credentials.json", "config.toml"):
        path = root / name
        if not path.is_symlink():
            path.symlink_to("..data/" + name)
    return target


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o440, 0o640, 0o444, 0o644])
@pytest.mark.parametrize("symlink", [False, True])
def test_readable_credentials_are_supported(tmp_path, mode, symlink):
    path = tmp_path / "target.json"
    write_secret(path, mode)
    if symlink:
        link = tmp_path / "credentials.json"
        link.symlink_to(path.name)
        path = link
    assert aquaservice.load_credentials(path) == credentials()


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o660, 0o666])
@pytest.mark.parametrize("symlink", [False, True])
def test_shared_write_permissions_are_rejected(tmp_path, mode, symlink):
    path = tmp_path / "target.json"
    write_secret(path, mode)
    if symlink:
        link = tmp_path / "credentials.json"
        link.symlink_to(path.name)
        path = link
    with pytest.raises(aquaservice.ClientError, match="group/world write"):
        aquaservice.load_credentials(path)


def test_directory_is_rejected(tmp_path):
    with pytest.raises(aquaservice.ClientError, match="regular file"):
        aquaservice.load_credentials(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_fifo_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path, 0o600)
    script = """import aquaservice, sys
try:
    aquaservice.load_credentials(sys.argv[1])
except aquaservice.ClientError:
    sys.exit(0)
sys.exit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0


def test_broken_symlink_is_rejected(tmp_path):
    path = tmp_path / "credentials.json"
    path.symlink_to("missing")
    with pytest.raises(OSError):
        aquaservice.load_credentials(path)


@pytest.mark.parametrize("source", ["relative", "absolute", "env", "projected-config"])
def test_adapter_rereads_rotated_projection(tmp_path, monkeypatch, source):
    root = tmp_path / "secret"
    root.mkdir()
    old = project(root, "first")
    config = tmp_path / "config.toml"
    mounted = root / "credentials.json"
    if source == "relative":
        config.write_text("credentialspath = 'secret/credentials.json'\n")
    elif source == "absolute":
        config.write_text(f"credentialspath = '{mounted}'\n")
    elif source == "env":
        config.write_text("")
        monkeypatch.setenv(ENV_PREFIX + "CREDENTIALS_PATH", "secret/credentials.json")
    else:
        config = root / "config.toml"
    settings = load_config(config)
    assert settings.credentials_path == mounted
    fetch = Mock(
        side_effect=lambda value: dt.date(
            2027, 1, 1 if value["token"] == "first" else 2
        )
    )
    monkeypatch.setattr(aquaservice, "get_next_delivery_date", fetch)
    adapter = NextDeliveryAdapter(settings.credentials_path)
    assert adapter.fetch() == "2027-01-01"
    project(root, "second")
    shutil.rmtree(old)
    assert adapter.fetch() == "2027-01-02"
    assert [call.args[0]["token"] for call in fetch.call_args_list] == [
        "first",
        "second",
    ]


def test_cli_override_preserves_projection_path(tmp_path, monkeypatch):
    old = project(tmp_path, "first")
    monkeypatch.chdir(tmp_path)
    seen = []

    class Bridge:
        def __init__(self, settings):
            self.path = settings.credentials_path
            seen.append(self.path)

        def run(self, stop):
            assert aquaservice.load_credentials(self.path)["token"] == "first"
            project(tmp_path, "second")
            shutil.rmtree(old)
            assert aquaservice.load_credentials(self.path)["token"] == "second"

    monkeypatch.setattr("aquaservice2mqtt.cli.MqttBridge", Bridge)
    assert main(["--credentials", "credentials.json", "run"]) == 0
    assert seen == [tmp_path / "credentials.json"]


def test_schema_validation_is_unchanged(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(aquaservice.ClientError, match="non-empty string fields"):
        aquaservice.load_credentials(path)
