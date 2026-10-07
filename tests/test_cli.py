import datetime as dt
import json
from unittest.mock import Mock

import aquaservice
import paho.mqtt.client as mqtt

from aquaservice2mqtt.cli import main


def test_check_prints_planned_safe_mqtt_messages_without_connecting(
    tmp_path, capsys, monkeypatch
):
    load = Mock(return_value={})
    monkeypatch.setattr(aquaservice, "load_credentials", load)
    monkeypatch.setattr(
        aquaservice, "get_next_delivery_date", lambda credentials: dt.date(2027, 4, 2)
    )
    monkeypatch.setattr(
        mqtt, "Client", Mock(side_effect=AssertionError("check must not connect MQTT"))
    )
    config = tmp_path / "bridge.toml"
    config.write_text(
        """credentialspath = 'private-credentials.json'
instanceid = 'safe-instance'
topicprefix = 'house/bridges'
discoveryprefix = 'homeassistant'
brokerhost = 'not-a-real-broker'
pollinterval = 60
""",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "check"]) == 0
    load.assert_called_once_with(tmp_path / "private-credentials.json")
    output = capsys.readouterr()
    assert output.err == ""
    result = json.loads(output.out)
    assert result["state"] == {
        "topic": "house/bridges/safe-instance/next_delivery/state",
        "payload": "2027-04-02",
        "retain": True,
        "qos": 1,
    }
    assert result["discovery"]["payload"]["device_class"] == "date"


def test_check_credential_override_and_sanitized_error(tmp_path, capsys, monkeypatch):
    config = tmp_path / "bridge.toml"
    config.write_text("", encoding="utf-8")
    override = tmp_path / "override.json"
    load = Mock(side_effect=ValueError("synthetic secret"))
    monkeypatch.setattr(aquaservice, "load_credentials", load)
    assert main(["--config", str(config), "--credentials", str(override), "check"]) == 2
    load.assert_called_once_with(override)
    output = capsys.readouterr()
    assert not output.out
    assert output.err == "aquaservice2mqtt: Aquaservice client invocation failed.\n"
