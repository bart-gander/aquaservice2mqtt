import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from aquaservice2mqtt.config import Settings
from aquaservice2mqtt.mqtt_bridge import MqttBridge


def unused_settings(port: int, tmp_path: Path) -> Settings:
    return Settings(
        tmp_path / "credentials.json",
        60,
        "test-bridge",
        "test/aquaservice",
        "homeassistant",
        "127.0.0.1",
        port,
        False,
        None,
        None,
        None,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_broker(port: int, tmp_path: Path) -> subprocess.Popen:
    config = tmp_path / f"broker-{port}.yaml"
    config.write_text(
        f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{port}\nsys_interval: 0\nauth:\n  allow-anonymous: true\ntopic-check:\n  enabled: false\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "amqtt"), "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.05)
    proc.terminate()
    proc.wait(timeout=5)
    raise AssertionError("temporary loopback broker did not start")


def stop_broker(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def wait_for(predicate, bridge: MqttBridge, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bridge.tick()
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for MQTT event: connected={bridge.connected}, next_poll={bridge.next_poll}"
    )


def test_loopback_broker_discovery_state_birth_reconnect_and_error(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="aquaservice2mqtt")
    port, broker = free_port(), None
    subscriber = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="test-subscriber",
        protocol=mqtt.MQTTv311,
    )
    messages, subscribed = [], False

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal subscribed
        client.subscribe("#", qos=1)
        subscribed = True

    def on_message(client, userdata, message):
        messages.append((message.topic, message.payload.decode(), message.retain))

    subscriber.on_connect, subscriber.on_message = on_connect, on_message
    outcome = {"date": "2027-04-02", "calls": 0}

    def fetch():
        outcome["calls"] += 1
        if outcome["date"] == "error":
            raise RuntimeError("synthetic upstream failure")
        return outcome["date"]

    bridge = MqttBridge(unused_settings(port, tmp_path), fetch)
    try:
        broker = start_broker(port, tmp_path)
        subscriber.connect("127.0.0.1", port)
        subscriber.loop_start()
        deadline = time.monotonic() + 5
        while not subscribed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert subscribed
        bridge.start()
        wait_for(
            lambda: (
                any(t.endswith("/config") for t, p, r in messages)
                and any(
                    t.endswith("/state") and p == "2027-04-02" and not r
                    for t, p, r in messages
                )
                and any(
                    t.endswith("/availability") and p == "online"
                    for t, p, r in messages
                )
            ),
            bridge,
        )
        assert outcome["calls"] == 1
        publisher = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-birth",
            protocol=mqtt.MQTTv311,
        )
        publisher.connect("127.0.0.1", port)
        publisher.loop_start()
        publisher.publish("homeassistant/status", "online", qos=1).wait_for_publish(
            timeout=5
        )
        before = len(messages)
        wait_for(
            lambda: (
                len(messages) > before
                and any(t.endswith("/config") for t, p, r in messages[before:])
            ),
            bridge,
        )
        assert outcome["calls"] == 1
        before_reconnect = len(messages)
        stop_broker(broker)
        broker = None
        wait_for(lambda: not bridge.connected, bridge, timeout=5)
        broker = start_broker(port, tmp_path)
        wait_for(
            lambda: (
                bridge.connected
                and any(
                    t.endswith("/state") and p == "2027-04-02" and not r
                    for t, p, r in messages[before_reconnect:]
                )
            ),
            bridge,
            timeout=12,
        )
        assert outcome["calls"] == 1
        outcome["date"] = "error"
        before_error = len(messages)
        bridge.next_poll = 0
        wait_for(
            lambda: any(
                t.endswith("/availability") and p == "offline"
                for t, p, r in messages[before_error:]
            ),
            bridge,
        )
        publisher.loop_stop()
        publisher.disconnect()
    finally:
        bridge.stop()
        subscriber.loop_stop()
        subscriber.disconnect()
        if broker:
            stop_broker(broker)
    assert "MQTT connected." in caplog.text
    assert "Aquaservice poll succeeded;" in caplog.text
    assert "MQTT snapshot acknowledged." in caplog.text
    assert "Home Assistant online; scheduling cached snapshot replay." in caplog.text
    assert "MQTT disconnected; waiting for automatic reconnect." in caplog.text
    assert "Aquaservice MQTT bridge stopped." in caplog.text
    assert "2027-04-02" not in caplog.text
    assert "synthetic upstream failure" not in caplog.text
