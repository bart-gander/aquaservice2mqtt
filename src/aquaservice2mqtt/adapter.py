"""Trusted direct-library adapter for the existing Aquaservice integration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import aquaservice


class AdapterError(RuntimeError):
    """Safe adapter error: no credentials, response bodies, or upstream URLs."""


class NextDeliveryAdapter:
    """Call the installed aquaservice-api dependency's public functions."""

    def __init__(self, credentials_path: Path) -> None:
        self.credentials_path = credentials_path

    def fetch(self) -> str | None:
        try:
            credentials = aquaservice.load_credentials(self.credentials_path)
            value = aquaservice.get_next_delivery_date(credentials)
        except Exception:
            raise AdapterError("Aquaservice client invocation failed.") from None
        if value is None:
            return None
        # Existing integration returns datetime.date; do not accept arbitrary strings/objects.
        if type(value) is not dt.date:
            raise AdapterError("Aquaservice client returned unexpected result.")
        return value.isoformat()
