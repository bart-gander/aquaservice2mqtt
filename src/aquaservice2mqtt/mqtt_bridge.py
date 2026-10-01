"""MQTT lifecycle with safe retained discovery and non-retained state."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from .adapter import NextDeliveryAdapter
from .config import ConfigError, Settings

logger = logging.getLogger(__name__)
PUBLISH_RETRY_SECONDS = 5


@dataclass(frozen=True)
class Topics:
    topic_prefix: str
    instance_id: str
    discovery_prefix: str

    @property
    def base(self) -> str:
        return f"{self.topic_prefix}/{self.instance_id}/next_delivery"

    @property
    def state(self) -> str:
        return f"{self.base}/state"

    @property
    def availability(self) -> str:
        return f"{self.base}/availability"

    @property
    def discovery(self) -> str:
        return f"{self.discovery_prefix}/sensor/{self.instance_id}/next_delivery/config"


def discovery_payload(topics: Topics, interval: int) -> dict[str, object]:
    return {
        "name": "Aquaservice next delivery",
        "unique_id": f"{topics.instance_id}_next_delivery",
        "state_topic": topics.state,
        "availability_topic": topics.availability,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device_class": "date",
        "expire_after": interval * 2,
    }


class MqttBridge:
    """Paho callbacks enqueue only; all acknowledged publishing happens in tick()."""

    def __init__(
        self, settings: Settings, fetcher: Callable[[], str | None] | None = None
    ):
        self.settings, self.topics = (
            settings,
            Topics(
                settings.topic_prefix, settings.instance_id, settings.discovery_prefix
            ),
        )
        self.fetcher = fetcher or NextDeliveryAdapter(settings.credentials_path).fetch
        self.client: mqtt.Client | None = None
        self.events: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.connected = self.running = False
        self.cached_state: str | None = None
        self.cache_is_currently_valid = False
        self.cached_at = self.next_poll = 0.0
        self.needs_publish = False
        self.next_publish = 0.0

        self.clear_retained_state = True

    def _on_connect(self, client, userdata, flags, reason_code, properties):  # type: ignore[no-untyped-def]
        if reason_code == 0:
            client.subscribe(f"{self.settings.discovery_prefix}/status", qos=1)
            self.events.put("connected")

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):  # type: ignore[no-untyped-def]
        self.events.put("disconnected")

    def _on_message(self, client, userdata, message):  # type: ignore[no-untyped-def]
        if (
            message.topic == f"{self.settings.discovery_prefix}/status"
            and message.payload == b"online"
        ):
            self.events.put("birth")

    def start(self) -> None:
        if self.running:
            return
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"aquaservice2mqtt-{self.settings.instance_id}",
            protocol=mqtt.MQTTv311,
        )
        if self.settings.username:
            client.username_pw_set(self.settings.username, self.settings.password)
        if self.settings.tls:
            try:
                client.tls_set(
                    ca_certs=str(self.settings.ca_path)
                    if self.settings.ca_path
                    else None
                )
            except (OSError, ValueError):
                raise ConfigError(
                    "Unable to configure MQTT TLS; check the CA file and permissions."
                ) from None
        client.will_set(self.topics.availability, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.max_queued_messages_set(20)
        client.on_connect, client.on_disconnect, client.on_message = (
            self._on_connect,
            self._on_disconnect,
            self._on_message,
        )
        self.client, self.running = client, True
        client.connect_async(
            self.settings.broker_host, self.settings.broker_port, keepalive=60
        )
        client.loop_start()

    def _publish(self, topic: str, payload: str, *, retain: bool) -> None:
        if not self.client or not self.connected:
            raise RuntimeError("MQTT is not connected.")
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT publish was not accepted.")
        info.wait_for_publish(timeout=15)
        if not info.is_published():
            raise RuntimeError("MQTT publish was not acknowledged.")

    def _publish_discovery(self) -> None:
        self._publish(
            self.topics.discovery,
            json.dumps(
                discovery_payload(self.topics, self.settings.interval),
                separators=(",", ":"),
            ),
            retain=True,
        )

    def _publish_cached_state(self, now: float) -> None:
        if (
            self.cache_is_currently_valid
            and now - self.cached_at <= self.settings.interval * 2
        ):
            self._publish(self.topics.state, self.cached_state or "None", retain=False)
            self._publish(self.topics.availability, "online", retain=True)
        else:
            self._publish(self.topics.availability, "offline", retain=True)

    def _publish_snapshot(self, now: float) -> None:
        if self.clear_retained_state:
            # Erase state retained by any old bridge release on each connection.
            self._publish(self.topics.state, "", retain=True)
            self.clear_retained_state = False
        self._publish_discovery()
        self._publish_cached_state(now)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if event == "connected":
                self.connected = True
                self.clear_retained_state = True
                self.needs_publish = True
                self.next_publish = 0.0
            elif event == "disconnected":
                self.connected = False
                logger.warning("MQTT disconnected; waiting for automatic reconnect.")
            elif event == "birth" and self.connected:
                # A birth event must not bypass an existing publish retry backoff.
                if not self.needs_publish:
                    self.next_publish = 0.0
                self.needs_publish = True

    def tick(self, now: float | None = None) -> None:
        self._drain_events()
        use_live_clock = now is None
        now = time.monotonic() if now is None else now
        if not self.connected:
            return
        if now >= self.next_poll:
            try:
                value = self.fetcher()
                self.cached_state, self.cached_at = value, now
                self.cache_is_currently_valid = True
                self.next_poll = now + self.settings.interval
            except Exception:
                self.cache_is_currently_valid = False
                retry = min(self.settings.interval, 300)
                self.next_poll = now + retry
                # Exception text can contain credentials or private upstream URLs.
                logger.warning("Aquaservice poll failed; retry in %s seconds.", retry)
            self.needs_publish = True
        if self.needs_publish and now >= self.next_publish:
            try:
                self._publish_snapshot(now)
            except RuntimeError:
                # A missed PUBACK does not imply a disconnected transport. Retain
                # pending work and retry without waiting for another CONNACK.
                retry_from = time.monotonic() if use_live_clock else now
                self.next_publish = retry_from + PUBLISH_RETRY_SECONDS
                logger.warning(
                    "MQTT publish failed; retry in %s seconds.", PUBLISH_RETRY_SECONDS
                )
            else:
                self.needs_publish = False
                self.next_publish = 0.0

    def run(self, stop: threading.Event) -> None:
        try:
            self.start()
            while not stop.wait(0.2):
                self.tick()
        finally:
            self.stop()

    def stop(self) -> None:
        if not self.running:
            return
        try:
            if self.connected:
                self._publish(self.topics.availability, "offline", retain=True)
        except RuntimeError:
            logger.warning("Unable to acknowledge MQTT offline status during shutdown.")
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
        self.connected = self.running = False
