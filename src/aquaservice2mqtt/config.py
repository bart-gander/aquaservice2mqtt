"""Minimal, path-safe TOML configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tomllib


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    credentials_path: Path
    interval: int
    instance_id: str
    topic_prefix: str
    discovery_prefix: str
    broker_host: str
    broker_port: int
    tls: bool
    ca_path: Path | None
    username: str | None
    password: str | None


def _value(data: dict, compact: str, snake: str, default=None):
    return data.get(compact, data.get(snake, default))


ENV_PREFIX = "AQUASERVICE2MQTT_"
ENV_FIELDS = {
    "CREDENTIALS_PATH": ("credentialspath", str),
    "POLL_INTERVAL": ("pollinterval", int),
    "INSTANCE_ID": ("instanceid", str),
    "TOPIC_PREFIX": ("topicprefix", str),
    "DISCOVERY_PREFIX": ("discoveryprefix", str),
    "BROKER_HOST": ("brokerhost", str),
    "BROKER_PORT": ("brokerport", int),
    "TLS": ("tls", bool),
    "CA_PATH": ("ca", str),
    "USERNAME": ("username", str),
    "PASSWORD_ENV": ("password_env", str),
}


def _environment_overrides(data: dict) -> dict:
    data = dict(data)
    for suffix, (field, kind) in ENV_FIELDS.items():
        name = ENV_PREFIX + suffix
        if name not in os.environ:
            continue
        value = os.environ[name]
        if kind is bool:
            normalized = value.strip().lower()
            if normalized not in ("true", "false", "1", "0"):
                raise ConfigError(f"{name} must be true, false, 1 or 0.")
            data[field] = normalized in ("true", "1")
        elif kind is int:
            try:
                data[field] = int(value)
            except ValueError:
                raise ConfigError(f"{name} must be an integer.") from None
        else:
            data[field] = value
    return data


def _relative(base: Path, value: object, name: str) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string path.")
    path = Path(value).expanduser()
    # Keep projected Secret/ConfigMap symlinks intact across atomic rotations.
    return path if path.is_absolute() else base / path


def _topic_part(value: object, name: str, *, allow_slash: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "#" in value
        or "+" in value
        or "\x00" in value
    ):
        raise ConfigError(f"{name} must be a non-empty MQTT topic name.")
    if not allow_slash and "/" in value:
        raise ConfigError(f"{name} must not contain '/'.")
    normalized = value.strip("/")
    if not normalized.strip():
        raise ConfigError(f"{name} must be non-empty after normalization.")
    return normalized


def load_config(path: Path | None = None) -> Settings:
    """Load optional TOML, then runtime environment overrides (never print values)."""
    configured = path if path is not None else os.environ.get(ENV_PREFIX + "CONFIG")
    if configured == "":
        raise ConfigError("AQUASERVICE2MQTT_CONFIG must be a non-empty path.")
    explicit = configured is not None
    path = Path(configured) if configured is not None else Path("config.toml")
    path = path.expanduser().absolute()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if explicit:
            raise ConfigError("Could not read valid TOML configuration.") from None
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("Could not read valid TOML configuration.") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a TOML table.")
    data = _environment_overrides(data)
    credentials = _relative(
        path.parent,
        _value(data, "credentialspath", "credentials_path", "credentials.json"),
        "credentialspath",
    )
    if "clientscript" in data or "client_script" in data:
        raise ConfigError(
            "Remove clientscript/client_script; aquaservice-api is now an installed dependency."
        )
    if credentials is None:
        raise ConfigError("credentialspath must be a non-empty path.")
    interval = _value(data, "pollinterval", "poll_interval", 21600)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 60:
        raise ConfigError("pollinterval must be an integer of at least 60 seconds.")
    port = _value(data, "brokerport", "broker_port", 1883)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("brokerport must be between 1 and 65535.")
    tls = _value(data, "tls", "tls", False)
    if not isinstance(tls, bool):
        raise ConfigError("tls must be true or false.")
    ca = _relative(path.parent, _value(data, "ca", "ca_path"), "ca")
    if ca and not tls:
        raise ConfigError("ca requires tls = true.")
    username = _value(data, "username", "username")
    if username is not None and not isinstance(username, str):
        raise ConfigError("username must be a string.")
    default_password_env = (
        ENV_PREFIX + "PASSWORD" if ENV_PREFIX + "PASSWORD" in os.environ else None
    )
    password_env = _value(data, "password_env", "password_env", default_password_env)
    if password_env is not None and (
        not isinstance(password_env, str) or not password_env
    ):
        raise ConfigError("password_env must be a non-empty environment-variable name.")
    password = os.environ.get(password_env) if password_env else None
    if password_env and password is None:
        raise ConfigError(
            "Configured MQTT password environment variable is unavailable."
        )
    broker_host = _value(data, "brokerhost", "broker_host", "127.0.0.1")
    if not isinstance(broker_host, str) or not broker_host.strip():
        raise ConfigError("brokerhost must be a non-empty string.")
    return Settings(
        credentials,
        interval,
        _topic_part(
            _value(data, "instanceid", "instance_id", "aquaservice"), "instanceid"
        ),
        _topic_part(
            _value(data, "topicprefix", "topic_prefix", "aquaservice2mqtt"),
            "topicprefix",
            allow_slash=True,
        ),
        _topic_part(
            _value(data, "discoveryprefix", "discovery_prefix", "homeassistant"),
            "discoveryprefix",
            allow_slash=True,
        ),
        broker_host,
        port,
        tls,
        ca,
        username or None,
        password,
    )
