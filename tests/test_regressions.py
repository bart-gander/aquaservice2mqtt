"""Failure-path regressions using synthetic data and no external services."""

from dataclasses import replace
from unittest.mock import Mock

import paho.mqtt.client as mqtt
import pytest

from aquaservice2mqtt.cli import main
from aquaservice2mqtt.config import ConfigError, load_config
from aquaservice2mqtt.mqtt_bridge import PUBLISH_RETRY_SECONDS, MqttBridge


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_config()


def mock_bridge(settings):
    fetch = Mock(return_value="2027-01-02")
    bridge = MqttBridge(settings, fetch)
    info = Mock(rc=mqtt.MQTT_ERR_SUCCESS)
    info.is_published.return_value = True
    bridge.client = Mock()
    bridge.client.is_connected.return_value = True
    bridge.client.publish.return_value = info
    bridge.running = True
    bridge.events.put("connected")
    return bridge, info, fetch


@pytest.mark.parametrize("failure", ["puback", "rejected", "exception"])
@pytest.mark.parametrize("phase", ["connect", "birth", "poll"])
def test_publish_failure_retries_on_live_transport(settings, caplog, phase, failure):
    bridge, info, fetch = mock_bridge(settings)
    now = 100.0
    expected_fetches = 1
    if phase != "connect":
        bridge.tick(now=now)
        bridge.client.publish.reset_mock()
        now += 1
        if phase == "birth":
            bridge.events.put("birth")
        else:
            now = bridge.next_poll
            expected_fetches = 2
    if failure == "puback":
        info.is_published.return_value = False
    elif failure == "rejected":
        info.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    else:
        info.wait_for_publish.side_effect = RuntimeError("private-secret-marker")
    bridge.tick(now=now)
    assert bridge.connected and bridge.client.is_connected()
    assert bridge.needs_publish
    assert fetch.call_count == expected_fetches
    attempts = bridge.client.publish.call_count
    bridge.events.put("birth")
    bridge.tick(now=now + PUBLISH_RETRY_SECONDS - 0.1)
    assert bridge.client.publish.call_count == attempts
    info.is_published.return_value = True
    info.rc = mqtt.MQTT_ERR_SUCCESS
    info.wait_for_publish.side_effect = None
    bridge.tick(now=now + PUBLISH_RETRY_SECONDS)
    assert not bridge.needs_publish
    bridge.client.publish.assert_any_call(
        bridge.topics.state, "2027-01-02", qos=1, retain=True
    )
    bridge.client.publish.assert_any_call(
        bridge.topics.availability, "online", qos=1, retain=True
    )
    assert fetch.call_count == expected_fetches
    bridge.client.reconnect.assert_not_called()
    assert "MQTT publish failed; retry in 5 seconds." in caplog.text
    assert "private-secret-marker" not in caplog.text


def test_publish_backoff_starts_after_ack_wait(settings, monkeypatch):
    bridge, info, fetch = mock_bridge(settings)
    clock = Mock(return_value=100.0)
    monkeypatch.setattr("aquaservice2mqtt.mqtt_bridge.time.monotonic", clock)
    info.is_published.return_value = False
    info.wait_for_publish.side_effect = lambda **kwargs: setattr(
        clock, "return_value", 115.0
    )
    bridge.tick()
    assert bridge.next_publish == 120.0
    attempts = bridge.client.publish.call_count
    clock.return_value = 119.0
    bridge.tick()
    assert bridge.client.publish.call_count == attempts
    clock.return_value = 120.0
    info.wait_for_publish.side_effect = None
    info.is_published.return_value = True
    bridge.tick()
    assert not bridge.needs_publish
    fetch.assert_called_once()


def test_start_bounds_pending_queue(settings, monkeypatch):
    client = Mock()
    monkeypatch.setattr(mqtt, "Client", Mock(return_value=client))
    bridge = MqttBridge(settings)
    bridge.start()
    client.max_queued_messages_set.assert_called_once_with(20)
    bridge.stop()
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()


def test_api_failure_logs_safely_and_recovers(settings, caplog):
    bridge, info, fetch = mock_bridge(settings)
    fetch.side_effect = RuntimeError("https://private.example/private-secret-marker")
    bridge.tick(now=100)
    retry = min(settings.interval, 300)
    assert not bridge.cache_is_currently_valid
    assert bridge.next_poll == 100 + retry
    assert f"Aquaservice poll failed; retry in {retry} seconds." in caplog.text
    assert "private.example" not in caplog.text
    assert "private-secret-marker" not in caplog.text
    bridge.client.publish.assert_any_call(
        bridge.topics.availability, "offline", qos=1, retain=True
    )
    bridge.events.put("birth")
    bridge.tick(now=101)
    fetch.assert_called_once()
    assert not any(
        call.args[1] == "online" for call in bridge.client.publish.call_args_list
    )
    fetch.side_effect = None
    bridge.tick(now=100 + retry)
    assert fetch.call_count == 2
    assert bridge.cache_is_currently_valid
    bridge.client.publish.assert_any_call(
        bridge.topics.availability, "online", qos=1, retain=True
    )


def test_api_failure_and_offline_publish_timeout_recover_independently(settings):
    bridge, info, fetch = mock_bridge(settings)
    fetch.side_effect = RuntimeError("synthetic failure")
    info.is_published.return_value = False
    bridge.tick(now=100)
    assert bridge.needs_publish and bridge.connected
    info.is_published.return_value = True
    bridge.tick(now=100 + PUBLISH_RETRY_SECONDS)
    fetch.assert_called_once()
    assert not bridge.needs_publish
    bridge.client.publish.assert_any_call(
        bridge.topics.availability, "offline", qos=1, retain=True
    )


def test_transport_disconnect_waits_for_reconnect(settings):
    bridge, info, fetch = mock_bridge(settings)
    info.is_published.return_value = False
    bridge.tick(now=100)
    bridge.events.put("disconnected")
    bridge.tick(now=106)
    attempts = bridge.client.publish.call_count
    bridge.tick(now=120)
    assert bridge.client.publish.call_count == attempts
    info.is_published.return_value = True
    bridge.events.put("connected")
    bridge.tick(now=121)
    assert bridge.connected and not bridge.needs_publish
    fetch.assert_called_once()


def test_stale_cache_is_not_replayed(settings):
    bridge, info, fetch = mock_bridge(settings)
    bridge.tick(now=100)
    bridge.client.publish.reset_mock()
    bridge.next_poll = float("inf")
    bridge.events.put("birth")
    bridge.tick(now=101 + settings.interval * 2)
    assert not any(
        call.args[0] == bridge.topics.state
        for call in bridge.client.publish.call_args_list
    )
    bridge.client.publish.assert_any_call(
        bridge.topics.availability, "offline", qos=1, retain=True
    )


@pytest.mark.parametrize("field", ["topicprefix", "discoveryprefix", "instanceid"])
@pytest.mark.parametrize("value", ["/", "///", "   ", "/ /", ""])
def test_empty_normalized_topics_rejected(tmp_path, field, value):
    config = tmp_path / "config.toml"
    config.write_text(f"{field} = '{value}'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config)


def test_valid_nested_topic_normalization(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("topicprefix = '/house/water/'\n", encoding="utf-8")
    assert load_config(config).topic_prefix == "house/water"


@pytest.mark.parametrize("kind", ["missing", "invalid", "directory"])
def test_bad_ca_is_sanitized_cli_error(tmp_path, capsys, kind):
    ca = tmp_path / "private-ca-marker.pem"
    if kind == "invalid":
        ca.write_text("not a certificate", encoding="utf-8")
    elif kind == "directory":
        ca.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f"tls = true\nca = '{ca.name}'\n", encoding="utf-8")
    assert main(["--config", str(config), "run"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "Unable to configure MQTT TLS" in output.err
    assert "Traceback" not in output.err and "private-ca-marker" not in output.err


def test_unreadable_ca_is_config_error(settings, monkeypatch):
    client = Mock()
    client.tls_set.side_effect = PermissionError("private-secret-marker")
    monkeypatch.setattr(mqtt, "Client", Mock(return_value=client))
    bridge = MqttBridge(replace(settings, tls=True))
    with pytest.raises(ConfigError, match="MQTT TLS") as error:
        bridge.start()
    assert "private-secret-marker" not in str(error.value)
    assert not bridge.running
    client.connect_async.assert_not_called()
