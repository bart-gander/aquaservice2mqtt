"""Operational logs are visible by default and never expose private values."""

import logging
import os
import subprocess
import sys
import time
from dataclasses import replace
from unittest.mock import Mock

import paho.mqtt.client as mqtt
import pytest
from test_regressions import mock_bridge

from aquaservice2mqtt.config import load_config
from aquaservice2mqtt.mqtt_bridge import MqttBridge


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_config()


@pytest.mark.parametrize("value", ["2027-01-02", None])
def test_successful_poll_and_ack_are_logged_without_payload(settings, caplog, value):
    caplog.set_level(logging.INFO, logger="aquaservice2mqtt")
    bridge, info, fetch = mock_bridge(settings)
    fetch.return_value = value
    bridge.tick(now=100)
    assert "MQTT connected." in caplog.text
    assert "Aquaservice poll started." in caplog.text
    assert (
        f"Aquaservice poll succeeded; next poll in {settings.interval} seconds."
        in caplog.text
    )
    assert "MQTT snapshot acknowledged." in caplog.text
    assert "2027-01-02" not in caplog.text
    caplog.clear()
    bridge.tick(now=101)
    assert not caplog.records  # No per-tick noise while idle.
    bridge.events.put("birth")
    bridge.tick(now=102)
    assert "Home Assistant online; scheduling cached snapshot replay." in caplog.text
    assert "MQTT snapshot acknowledged." in caplog.text
    fetch.assert_called_once()


def test_no_success_log_before_puback(settings, caplog):
    caplog.set_level(logging.INFO, logger="aquaservice2mqtt")
    bridge, info, fetch = mock_bridge(settings)
    info.is_published.return_value = False
    bridge.tick(now=100)
    assert "MQTT publish failed" in caplog.text
    assert "MQTT snapshot acknowledged." not in caplog.text
    info.is_published.return_value = True
    bridge.tick(now=105)
    assert "MQTT snapshot acknowledged." in caplog.text


def test_connection_failures_are_enqueued_and_logged_safely(
    settings, monkeypatch, caplog
):
    client = Mock()
    monkeypatch.setattr(mqtt, "Client", Mock(return_value=client))
    fetch = Mock()
    bridge = MqttBridge(settings, fetch)
    bridge.start()
    # Test the registered callback, not just a manually inserted event.
    assert callable(client.on_connect_fail)
    client.on_connect_fail(client, None)
    bridge.tick(now=100)
    assert (
        "MQTT connection attempt failed; automatic reconnect enabled (backoff up to 5 seconds)."
        in caplog.text
    )
    caplog.clear()
    reason = Mock()
    reason.__str__ = Mock(return_value="private-reason-marker")
    client.on_connect(client, None, None, reason, None)
    bridge.tick(now=101)
    assert (
        "MQTT connection rejected by broker; check authentication and broker policy."
        in caplog.text
    )
    assert "private-reason-marker" not in caplog.text
    assert not bridge.connected
    fetch.assert_not_called()
    bridge.stop()


def test_start_stop_logs_omit_settings(settings, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="aquaservice2mqtt")
    client = Mock()
    monkeypatch.setattr(mqtt, "Client", Mock(return_value=client))
    bridge = MqttBridge(
        replace(
            settings,
            username="private-user-marker",
            password="private-pass-marker",
            broker_host="private-host-marker",
        )
    )
    bridge.start()
    bridge.start()
    bridge.stop()
    bridge.stop()
    assert (
        f"Starting Aquaservice MQTT bridge; poll interval={settings.interval} seconds."
        in caplog.text
    )
    assert (
        "MQTT connection requested; automatic reconnect enabled (backoff up to 5 seconds)."
        in caplog.text
    )
    assert "Stopping Aquaservice MQTT bridge." in caplog.text
    assert "Aquaservice MQTT bridge stopped." in caplog.text
    assert caplog.text.count("Starting Aquaservice MQTT bridge;") == 1
    assert "private-" not in caplog.text


def test_cli_default_stderr_logging_and_sigterm(tmp_path):
    # Bind but do not listen: keep an unused loopback port reserved for the test.
    import socket

    env = {k: v for k, v in os.environ.items() if not k.startswith("AQUASERVICE2MQTT_")}
    with socket.socket() as reserved, (tmp_path / "stderr.log").open("w+") as log:
        reserved.bind(("127.0.0.1", 0))
        env.update(
            AQUASERVICE2MQTT_BROKER_HOST="127.0.0.1",
            AQUASERVICE2MQTT_BROKER_PORT=str(reserved.getsockname()[1]),
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "aquaservice2mqtt.cli", "run"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            text = ""
            while time.monotonic() < deadline:
                log.seek(0)
                text = log.read()
                if "MQTT connection attempt failed" in text:
                    break
                assert proc.poll() is None
                time.sleep(0.05)
            assert (
                "INFO aquaservice2mqtt.mqtt_bridge: Starting Aquaservice MQTT bridge;"
                in text
            )
            assert (
                "WARNING aquaservice2mqtt.mqtt_bridge: MQTT connection attempt failed"
                in text
            )
            proc.terminate()
            stdout, _ = proc.communicate(timeout=10)
            assert proc.returncode == 0 and stdout == ""
            log.seek(0)
            text = log.read()
            assert "Aquaservice MQTT bridge stopped." in text
            assert "Traceback" not in text
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
