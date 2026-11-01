"""Persist non-secret, single-guild bot settings to a local JSON file."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def valid_ip(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError("Please enter a valid IPv4 address.") from exc


def valid_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Discord IDs must be positive integers.")
    return value


@dataclass
class GuildSettings:
    target_ip: str | None = None
    staff_role_ids: list[int] = field(default_factory=list)
    log_channel_id: int | None = None

    def validate(self) -> None:
        if self.target_ip is not None:
            self.target_ip = valid_ip(self.target_ip)
        if not isinstance(self.staff_role_ids, list):
            raise ValueError("staff_role_ids must be a list.")
        self.staff_role_ids = sorted({valid_id(v) for v in self.staff_role_ids})
        if self.log_channel_id is not None:
            self.log_channel_id = valid_id(self.log_channel_id)

    def to_dict(self) -> dict:
        self.validate()
        return {
            "target_ip": self.target_ip,
            "staff_role_ids": self.staff_role_ids,
            "log_channel_id": self.log_channel_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "GuildSettings":
        if not isinstance(raw, dict):
            raise ValueError("Invalid bot settings file format.")
        # Missing keys are safe; unknown keys do not get copied into persisted settings.
        cfg = cls(
            target_ip=raw.get("target_ip"),
            staff_role_ids=raw.get("staff_role_ids", []),
            log_channel_id=raw.get("log_channel_id"),
        )
        cfg.validate()
        return cfg


class SettingsStore:
    def __init__(self, path: Path, defaults: GuildSettings):
        self.path = path
        defaults.validate()
        if path.exists():
            try:
                self.config = GuildSettings.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read bot settings file: {path}") from exc
        else:
            self.config = defaults

    def save(self) -> None:
        """Atomically persist only non-secret fields (and lock down new files)."""
        payload = json.dumps(self.config.to_dict(), indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
