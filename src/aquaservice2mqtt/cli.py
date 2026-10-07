"""Console interface for the bridge."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .adapter import AdapterError, NextDeliveryAdapter
from .config import ConfigError, Settings, load_config
from .mqtt_bridge import MqttBridge, Topics, discovery_payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Aquaservice next-delivery MQTT bridge."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML file (otherwise AQUASERVICE2MQTT_CONFIG, local config.toml if present, or defaults)",
    )
    parser.add_argument(
        "--credentials", type=Path, help="Override credentials path for this invocation"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the MQTT polling bridge")
    sub.add_parser(
        "check", help="Fetch once and print planned MQTT messages without connecting"
    )
    return parser


def _planned(settings: Settings, value: str | None) -> dict[str, object]:
    topics = Topics(
        settings.topic_prefix, settings.instance_id, settings.discovery_prefix
    )
    return {
        "discovery": {
            "topic": topics.discovery,
            "payload": discovery_payload(topics, settings.interval),
            "qos": 1,
            "retain": True,
        },
        "state": {
            "topic": topics.state,
            "payload": value or "None",
            "qos": 1,
            "retain": True,
        },
        "availability": {
            "topic": topics.availability,
            "payload": "online",
            "qos": 1,
            "retain": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.credentials:
            settings = replace(
                settings, credentials_path=args.credentials.expanduser().absolute()
            )
        if args.command == "check":
            value = NextDeliveryAdapter(settings.credentials_path).fetch()
            print(json.dumps(_planned(settings, value), sort_keys=True))
            return 0
        stop = threading.Event()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )

        def request_stop(signum, frame):  # type: ignore[no-untyped-def]
            stop.set()

        previous_int = signal.signal(signal.SIGINT, request_stop)
        previous_term = signal.signal(signal.SIGTERM, request_stop)
        try:
            MqttBridge(settings).run(stop)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        return 0
    except (ConfigError, AdapterError) as exc:
        print(f"aquaservice2mqtt: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
