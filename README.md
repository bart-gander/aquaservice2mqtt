# aquaservice2mqtt

A small, read-only MQTT bridge for **only** Aquaservice's next delivery date. It publishes Home Assistant MQTT discovery plus a date sensor state. It deliberately reuses the trusted `aquaservice-api` Python integration for credential loading, API authentication, request bounds, and response/date validation; this project does not implement Aquaservice HTTP requests itself.

## Install

Python 3.11+ and Git are required. `uv sync` automatically downloads and installs
`aquaservice-api` from its published **`v0.1.0`** GitHub tag. No separate checkout,
client-script path, or subprocess API invocation is needed.

```sh
cd aquaservice2mqtt
uv sync
cp -n config.example.toml config.toml
cp -n credentials.json.example credentials.json
chmod 600 credentials.json
```

The adapter uses normal `import aquaservice` and calls its public
`load_credentials(Path)` and `get_next_delivery_date(credentials)` functions.
The existing integration enforces a private regular credentials file and validates
the API's canonical date. `pyproject.toml` names the tag; `uv.lock` records its
resolved commit for reproducible installs. For an existing installation, run
`uv sync` and remove the old `clientscript` / `client_script` setting from your
configuration; those obsolete settings now produce a clear migration error.

Populate `credentials.json` using the existing integration's [documented export workflow](https://github.com/bart-gander/aquaservice-api/tree/v0.1.0#export-credentials-from-the-website). Never put a broker password in this config or on the command line; use `password_env`.

## Configure and check

`config.example.toml` documents all settings. Relative `credentialspath` and `ca` paths resolve from the configuration file's directory. Defaults are a six-hour `pollinterval` (minimum 60 seconds), `aquaservice` instance ID, `aquaservice2mqtt` topic prefix, and `homeassistant` discovery prefix.

Choose a stable **non-sensitive** `instanceid`: it is part of topics and Home Assistant's entity ID. Do not use an account number, address, customer name, token, or contract identifier.

Fetch once and display exactly the messages that would be sent, without opening an MQTT connection:

```sh
uv run aquaservice2mqtt --config config.toml check
# optional one-off private credential path for a controlled validation:
uv run aquaservice2mqtt --config config.toml --credentials /private/path/credentials.json check
```

Run the bridge:

```sh
uv run aquaservice2mqtt --config config.toml run
```

`SIGINT`/`SIGTERM` publishes retained `offline` when the broker connection is available, then cleanly stops the Paho loop. API/validation failures are reported on stderr without upstream bodies, URLs, or credentials; the bridge publishes retained `offline` and backs off failure polls to at most once per 300 seconds (or the configured interval when shorter). MQTT publish failures are reported separately and retried with a five-second backoff, without forcing another API request or waiting for a new connection event. Actual transport disconnections use Paho's automatic reconnect. The pending MQTT queue is bounded. Missing, unreadable, or invalid TLS CA files produce a sanitized configuration error and exit code 2.

## Container

Build the production image (Git and uv are confined to the builder stage):

```sh
podman build -f Containerfile -t localhost/aquaservice2mqtt:0.1.0 .
```

The build installs only runtime dependencies from `uv.lock`, including the locked
`aquaservice-api` release. `.dockerignore` allowlists build inputs so credentials,
configuration, tests, local environments, and Git history are not sent to the
builder. The runtime uses the image's non-root `app` identity (UID/GID 10001),
needs no writable application volume, and makes only outbound connections; no
port publishing is required. Docker can build the same file with `docker build
-f Containerfile ...`.

Optional build arguments are `PYTHON_IMAGE` (default
`docker.io/library/python:3.13.15-slim-bookworm`) and `UV_IMAGE` (default
`ghcr.io/astral-sh/uv:0.11.19`). Keep the Python image Debian-based and Python
3.11+; choose compatible images for the target architecture. Runtime account and
broker settings are **not** build arguments and must never be baked into layers.

### Runtime configuration

Every bridge setting can be set through environment variables, with or without
TOML. Precedence is **CLI credential override > environment > TOML > defaults**.
The config file is selected by `--config`, then `AQUASERVICE2MQTT_CONFIG`; otherwise
`./config.toml` is loaded if present. Without a file, defaults plus environment
are used. An explicitly selected missing/unreadable file is always an error.
Relative file paths resolve against the selected config directory, or the current
working directory without a file (`/app` in the container).

| Environment variable | TOML setting | Default / meaning |
|---|---|---|
| `AQUASERVICE2MQTT_CONFIG` | — | Optional TOML file path |
| `AQUASERVICE2MQTT_CREDENTIALS_PATH` | `credentialspath` | `credentials.json`; mount this private JSON file |
| `AQUASERVICE2MQTT_POLL_INTERVAL` | `pollinterval` | `21600` seconds; minimum `60` |
| `AQUASERVICE2MQTT_INSTANCE_ID` | `instanceid` | `aquaservice`; stable, non-sensitive identifier |
| `AQUASERVICE2MQTT_TOPIC_PREFIX` | `topicprefix` | `aquaservice2mqtt` |
| `AQUASERVICE2MQTT_DISCOVERY_PREFIX` | `discoveryprefix` | `homeassistant` |
| `AQUASERVICE2MQTT_BROKER_HOST` | `brokerhost` | `127.0.0.1`; set to a broker reachable from the container |
| `AQUASERVICE2MQTT_BROKER_PORT` | `brokerport` | `1883`; set explicitly to `8883` for a typical TLS broker |
| `AQUASERVICE2MQTT_TLS` | `tls` | `false`; environment accepts `true`, `false`, `1`, `0` |
| `AQUASERVICE2MQTT_CA_PATH` | `ca` | Optional mounted CA file; requires TLS; empty clears a TOML CA |
| `AQUASERVICE2MQTT_USERNAME` | `username` | Optional MQTT username; empty disables it |
| `AQUASERVICE2MQTT_PASSWORD_ENV` | `password_env` | Optional name of the environment variable containing the MQTT password |
| `AQUASERVICE2MQTT_PASSWORD` | — | Default MQTT password source when present and no `password_env` is selected |

A configured `password_env` must exist, and its selector can itself be overridden
with `AQUASERVICE2MQTT_PASSWORD_ENV`. Do not put literal secrets in shell arguments
or commit environment files. Aquaservice token/PIN/account fields remain in the
mounted JSON file described above, not separate environment variables.

For Podman, create secrets from private files and run without a TOML file:

```sh
podman secret create aquaservice-credentials /private/path/credentials.json
# Optional, if MQTT authentication is required:
podman secret create aquaservice-mqtt-password /private/path/mqtt-password

podman run -d --name aquaservice2mqtt \
  --read-only --cap-drop=all --security-opt=no-new-privileges \
  --stop-timeout 300 \
  --secret aquaservice-credentials,type=mount,target=credentials.json,uid=10001,gid=10001,mode=0400 \
  --secret aquaservice-mqtt-password,type=env,target=AQUASERVICE2MQTT_PASSWORD \
  -e AQUASERVICE2MQTT_CREDENTIALS_PATH=/run/secrets/credentials.json \
  -e AQUASERVICE2MQTT_BROKER_HOST=mqtt.example.net \
  -e AQUASERVICE2MQTT_BROKER_PORT=8883 \
  -e AQUASERVICE2MQTT_TLS=true \
  -e AQUASERVICE2MQTT_USERNAME=aquaservice \
  -e AQUASERVICE2MQTT_INSTANCE_ID=water \
  -e AQUASERVICE2MQTT_POLL_INTERVAL=21600 \
  localhost/aquaservice2mqtt:0.1.0
```

Omit the password secret and username when MQTT authentication is not used. Secret
file permissions must be `0400` or `0600`, readable by the image user; group/world
readable credentials are intentionally rejected by the API library. Bind mounts
are also supported, but preserve suitable ownership and permissions rather than
making credentials world-readable. With a remote container engine, bind-mount
paths refer to the engine host, not the client machine. Use a read-only mount and
`AQUASERVICE2MQTT_CONFIG=/config/config.toml` if TOML is preferred, and mount any
custom CA file read-only as well.

The default command is `run`. Append `check` to the same `podman run` invocation
(use `--rm` without `-d`) for a real read-only API check without connecting MQTT.
Arguments after the image are ordinary CLI arguments, for example
`--config /config/config.toml --credentials /run/secrets/credentials.json check`.
The exec-form entrypoint receives SIGTERM directly. Shutdown may wait for an
in-flight synchronous API call/retries; configure the orchestrator's stop grace
period accordingly. No API-polling healthcheck is added, avoiding extra account
requests.

## MQTT and Home Assistant behavior

For `topicprefix = "aquaservice2mqtt"` and `instanceid = "aquaservice"`:

| Purpose | Topic | Retained |
| --- | --- | --- |
| Discovery | `homeassistant/sensor/aquaservice/next_delivery/config` | yes |
| State | `aquaservice2mqtt/aquaservice/next_delivery/state` | **no** |
| Availability | `aquaservice2mqtt/aquaservice/next_delivery/availability` | yes |

All bridge publishes use QoS 1 and wait for PUBACK outside Paho network callbacks. Discovery identifies a Home Assistant MQTT sensor with `device_class: date` and `expire_after: pollinterval * 2`. State is the validated `YYYY-MM-DD` date, or `None` when the existing integration explicitly reports no delivery date; Home Assistant interprets `None` as unknown.

The bridge clears any retained state from an older release before publishing a non-retained state. Discovery and availability are retained; an MQTT Last Will sets availability `offline` for an unexpected connection loss. It only announces `online` after a validated API result. On connection/reconnection it resubscribes to `homeassistant/status`, reannounces retained discovery, and replays cached fresh state after a Home Assistant `online` birth message without making an extra API request.

## Verification

```sh
uv run --locked --group test pytest
uvx flake8 . --count --statistics
uv run --locked --with mypy mypy src --ignore-missing-imports --check-untyped-defs
uv lock --check
uv build
```

The test suite uses synthetic credentials/data only. The MQTT smoke test starts and stops a temporary loopback-only `amqtt` broker; it verifies discovery, non-retained state, availability, HA birth replay without a refetch, API-error offline handling, and broker reconnection. Regression tests cover PUBACK timeouts and publish rejection during connection, polling, and birth replay; bounded retry timing; safe diagnostics; cache expiration; topic normalization; and TLS CA errors. No real Aquaservice request or external MQTT broker is contacted by tests. Flake8's E501 line-length rule is intentionally disabled; other checks remain enabled.
