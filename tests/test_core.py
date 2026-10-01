import datetime as dt
import json
import traceback
from unittest.mock import Mock

import aquaservice
import pytest

from aquaservice2mqtt.adapter import AdapterError, NextDeliveryAdapter
from aquaservice2mqtt.config import ConfigError, load_config
from aquaservice2mqtt.mqtt_bridge import MqttBridge, Topics, discovery_payload


def test_adapter_calls_installed_library_functions(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials.json"
    load = Mock(return_value={"opaque": "credentials"})
    fetch = Mock(return_value=dt.date(2027, 4, 2))
    monkeypatch.setattr(aquaservice, "load_credentials", load)
    monkeypatch.setattr(aquaservice, "get_next_delivery_date", fetch)
    assert NextDeliveryAdapter(credentials).fetch() == "2027-04-02"
    load.assert_called_once_with(credentials)
    fetch.assert_called_once_with({"opaque": "credentials"})


def test_adapter_accepts_existing_no_date_result(monkeypatch, tmp_path):
    monkeypatch.setattr(aquaservice, "load_credentials", lambda path: {})
    monkeypatch.setattr(aquaservice, "get_next_delivery_date", lambda credentials: None)
    assert NextDeliveryAdapter(tmp_path / "credentials.json").fetch() is None


@pytest.mark.parametrize("value", ["2027-04-02", dt.datetime(2027, 4, 2), 3])
def test_adapter_fails_closed_on_unexpected_result(monkeypatch, tmp_path, value):
    monkeypatch.setattr(aquaservice, "load_credentials", lambda path: {})
    monkeypatch.setattr(
        aquaservice, "get_next_delivery_date", lambda credentials: value
    )
    with pytest.raises(AdapterError, match="unexpected"):
        NextDeliveryAdapter(tmp_path / "credentials.json").fetch()


@pytest.mark.parametrize(
    "failing_function", ["load_credentials", "get_next_delivery_date"]
)
def test_adapter_sanitizes_library_failure(monkeypatch, tmp_path, failing_function):
    monkeypatch.setattr(aquaservice, "load_credentials", lambda path: {})
    monkeypatch.setattr(
        aquaservice,
        failing_function,
        Mock(side_effect=RuntimeError("https://private.example/secret")),
    )
    with pytest.raises(AdapterError, match="failed") as exc:
        NextDeliveryAdapter(tmp_path / "credentials.json").fetch()
    rendered = "".join(traceback.format_exception(exc.value))
    assert "private.example" not in rendered and "secret" not in str(exc.value)


def test_config_relative_paths_interval_and_env_password(tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate", encoding="utf-8")
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        """credentialspath = 'credentials.json'
pollinterval = 60
instanceid = 'kitchen-water'
topicprefix = 'house/bridges'
discoveryprefix = 'ha'
brokerhost = '127.0.0.1'
brokerport = 1883
tls = true
ca = 'ca.pem'
password_env = 'SYNTHETIC_MQTT_PASSWORD'
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNTHETIC_MQTT_PASSWORD", "not-exposed")
    cfg = load_config(config_path)
    assert cfg.credentials_path == tmp_path / "credentials.json"
    assert not hasattr(cfg, "client_script")
    assert cfg.ca_path == ca and cfg.password == "not-exposed" and cfg.interval == 60
    config_path.write_text("pollinterval = 59\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="60"):
        load_config(config_path)


@pytest.mark.parametrize("key", ["clientscript", "client_script"])
def test_obsolete_script_setting_has_migration_error(tmp_path, key):
    path = tmp_path / "config.toml"
    path.write_text(f"{key} = 'unused.py'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="installed dependency"):
        load_config(path)


def test_empty_credentials_path_is_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("credentialspath = ''\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(path)


def test_default_bridge_fetcher_uses_installed_library(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    load = Mock(return_value={})
    monkeypatch.setattr(aquaservice, "load_credentials", load)
    monkeypatch.setattr(
        aquaservice, "get_next_delivery_date", lambda credentials: dt.date(2027, 4, 2)
    )
    bridge = MqttBridge(load_config(path))
    assert bridge.fetcher() == "2027-04-02"
    load.assert_called_once_with(tmp_path / "credentials.json")


def test_discovery_has_only_safe_topics_and_nonretained_state():
    topics = Topics("house/bridges", "kitchen-water", "ha")
    payload = discovery_payload(topics, 120)
    assert payload["device_class"] == "date" and payload["expire_after"] == 240
    assert payload["state_topic"] == "house/bridges/kitchen-water/next_delivery/state"
    assert (
        "token" not in json.dumps(payload).lower()
        and "account" not in json.dumps(payload).lower()
    )
    assert topics.discovery == "ha/sensor/kitchen-water/next_delivery/config"
