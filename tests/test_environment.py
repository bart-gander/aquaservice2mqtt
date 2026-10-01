import os
from pathlib import Path
from unittest.mock import Mock

import aquaservice
import pytest

from aquaservice2mqtt.cli import main
from aquaservice2mqtt.config import ENV_FIELDS, ENV_PREFIX, ConfigError, load_config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in os.environ:
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_env_only_defaults(tmp_path):
    settings = load_config()
    assert settings.credentials_path == tmp_path / "credentials.json"
    assert settings.interval == 21600 and settings.broker_port == 1883
    assert settings.password is None


def test_all_env_fields_override_toml_aliases(tmp_path, monkeypatch):
    config = tmp_path / "custom.toml"
    config.write_text("""credentials_path = 'old.json'
poll_interval = 60
instance_id = 'old'
topic_prefix = 'old'
discovery_prefix = 'old'
broker_host = 'old'
broker_port = 1883
tls = false
ca_path = 'old.pem'
username = 'old'
password_env = 'OLD_PASSWORD'
""")
    values = {
        "CREDENTIALS_PATH": "private/new.json",
        "POLL_INTERVAL": "120",
        "INSTANCE_ID": "water",
        "TOPIC_PREFIX": "bridges/water",
        "DISCOVERY_PREFIX": "ha",
        "BROKER_HOST": "broker",
        "BROKER_PORT": "8883",
        "TLS": "true",
        "CA_PATH": "new.pem",
        "USERNAME": "mqtt",
        "PASSWORD_ENV": "TEST_MQTT_PASSWORD",
    }
    assert set(values) == set(ENV_FIELDS)
    for key, value in values.items():
        monkeypatch.setenv(ENV_PREFIX + key, value)
    monkeypatch.setenv("TEST_MQTT_PASSWORD", "synthetic")
    settings = load_config(config)
    assert settings.credentials_path == tmp_path / "private/new.json"
    assert settings.interval == 120 and settings.instance_id == "water"
    assert (
        settings.topic_prefix == "bridges/water" and settings.discovery_prefix == "ha"
    )
    assert settings.broker_host == "broker" and settings.broker_port == 8883
    assert settings.tls and settings.ca_path == tmp_path / "new.pem"
    assert settings.username == "mqtt" and settings.password == "synthetic"


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False), ("1", True), ("0", False), (" FALSE ", False)],
)
def test_tls_environment_boolean(monkeypatch, value, expected):
    monkeypatch.setenv(ENV_PREFIX + "TLS", value)
    assert load_config().tls is expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("TLS", "secret-invalid"),
        ("POLL_INTERVAL", "secret-invalid"),
        ("BROKER_PORT", "secret-invalid"),
        ("POLL_INTERVAL", "59"),
        ("BROKER_PORT", "65536"),
        ("BROKER_HOST", ""),
        ("CREDENTIALS_PATH", ""),
        ("CONFIG", ""),
    ],
)
def test_invalid_environment_fails_without_leaking_value(monkeypatch, name, value):
    monkeypatch.setenv(ENV_PREFIX + name, value)
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "secret-invalid" not in str(exc.value)


def test_environment_config_selector_and_cli_precedence(tmp_path, monkeypatch):
    env_config = tmp_path / "env.toml"
    explicit = tmp_path / "cli.toml"
    env_config.write_text("instanceid = 'env-file'\n")
    explicit.write_text("instanceid = 'cli-file'\n")
    monkeypatch.setenv(ENV_PREFIX + "CONFIG", str(env_config))
    assert load_config().instance_id == "env-file"
    assert load_config(explicit).instance_id == "cli-file"


def test_explicit_missing_config_is_not_ignored(tmp_path, monkeypatch):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
    monkeypatch.setenv(ENV_PREFIX + "CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError):
        load_config()


def test_implicit_toml_still_loads(tmp_path):
    (tmp_path / "config.toml").write_text("instanceid = 'local-file'\n")
    assert load_config().instance_id == "local-file"


def test_password_environment_default_and_selector(monkeypatch):
    monkeypatch.setenv(ENV_PREFIX + "PASSWORD", "synthetic-default")
    assert load_config().password == "synthetic-default"
    monkeypatch.setenv(ENV_PREFIX + "PASSWORD_ENV", "OTHER_MQTT_PASSWORD")
    with pytest.raises(ConfigError, match="unavailable"):
        load_config()
    monkeypatch.setenv("OTHER_MQTT_PASSWORD", "synthetic-custom")
    assert load_config().password == "synthetic-custom"


def test_cli_credentials_override_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_PREFIX + "CREDENTIALS_PATH", "/env/credentials.json")
    load = Mock(return_value={})
    monkeypatch.setattr(aquaservice, "load_credentials", load)
    monkeypatch.setattr(aquaservice, "get_next_delivery_date", lambda credentials: None)
    assert main(["--credentials", "cli.json", "check"]) == 0
    load.assert_called_once_with(tmp_path / "cli.json")
    assert '"payload": "None"' in capsys.readouterr().out


def test_container_contract():
    root = Path(__file__).resolve().parents[1]
    container = (root / "Containerfile").read_text()
    assert 'ENTRYPOINT ["aquaservice2mqtt"]' in container
    assert 'CMD ["run"]' in container and "USER app" in container
    assert "--locked --no-dev --no-default-groups --no-editable" in container
    assert "COPY . " not in container and "credentials.json" not in container
    runtime = container.split("AS runtime", 1)[1]
    assert "apt-get" not in runtime and "git " not in runtime
    ignored = (root / ".dockerignore").read_text().splitlines()
    assert "**" in ignored and "!src/**" in ignored
    assert "!credentials.json" not in ignored and "!config.toml" not in ignored
