"""Asynchronous PacketShield API client (per supplied OpenAPI v410).

"Map CAPTCHA" is interpreted as the Minecraft Limbo ``captcha`` check.
The API replaces all Limbo checks on POST, so existing checks are read first
and sent back unchanged except for the one field the user selected.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any

import aiohttp

API_ORIGIN = "https://shield.packets-decreaser.net"
CHECK_FLAGS = ("fall", "explosion", "keepalive", "movement", "jitter", "captcha")
LEGACY_POLICIES = frozenset({"kick", "captcha", "verify2fa", "captcha_verify2fa"})


class ShieldAPIError(Exception):
    """An error safe to show in Discord (does not expose API responses/tokens)."""


@dataclass(frozen=True)
class CaptchaStatus:
    minecraft_filter: bool
    minecraft_gate: bool
    limbo: bool
    captcha: bool
    legacy_policy: str

    @property
    def active(self) -> bool:
        return all((self.minecraft_filter, self.minecraft_gate, self.limbo, self.captcha))


class ShieldClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, target_ip: str) -> None:
        self.session = session
        self.token = token
        try:
            self.target_ip = str(ipaddress.IPv4Address(target_ip))
        except (ipaddress.AddressValueError, ValueError) as exc:
            raise ValueError("The target must be a valid IPv4 address.") from exc
        self.root = f"/api/targets/{self.target_ip}"

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        allowed = {
            self.root,
            self.root + "/mc-limbo",
            self.root + "/mc-limbo-checks",
            self.root + "/mc-verify",
        }
        if path not in allowed or method not in {"GET", "POST"}:
            raise ShieldAPIError("Invalid API endpoint.")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            async with self.session.request(
                method, API_ORIGIN + path,
                json=body, headers=headers, allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise ShieldAPIError("Invalid or expired Shield API key (HTTP 401).")
                if response.status == 403:
                    raise ShieldAPIError("This API account cannot manage the target or switch (HTTP 403).")
                if response.status == 404:
                    raise ShieldAPIError("Target IP or API feature not found (HTTP 404).")
                if not 200 <= response.status < 300:
                    raise ShieldAPIError(f"Shield API rejected the request (HTTP {response.status}).")
                if response.status == 204:
                    return None
                raw = await response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ShieldAPIError("Shield API returned invalid JSON.") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ShieldAPIError("Shield API could not be reached or timed out.") from exc

    async def get_target(self) -> dict[str, Any]:
        return _dict_or_error(await self._request("GET", self.root), "target")

    async def get_limbo(self) -> bool:
        return self._get_enabled(await self._request("GET", self.root + "/mc-limbo"), "Limbo")

    async def set_limbo(self, enabled: bool) -> None:
        await self._request("POST", self.root + "/mc-limbo", {"enabled": enabled})
        if await self.get_limbo() is not enabled:
            raise ShieldAPIError("Limbo setting was not confirmed by the API.")

    async def get_web_verify(self) -> bool:
        return self._get_enabled(await self._request("GET", self.root + "/mc-verify"), "Web verification")

    async def set_web_verify(self, enabled: bool) -> None:
        if enabled:
            await self.require_gate()
        await self._request("POST", self.root + "/mc-verify", {"enabled": enabled})
        if await self.get_web_verify() is not enabled:
            raise ShieldAPIError("Website verification setting was not confirmed by the API.")

    @staticmethod
    def _get_enabled(response: Any, label: str) -> bool:
        record = _dict_or_error(response, label)
        if type(record.get("enabled")) is not bool:
            raise ShieldAPIError(f"{label} response has no valid 'enabled' value.")
        return record["enabled"]

    async def get_checks(self) -> dict[str, Any]:
        checks = _dict_or_error(await self._request("GET", self.root + "/mc-limbo-checks"), "Limbo checks")
        # POST replaces the entire set; abort rather than silently resetting other flags.
        for key in CHECK_FLAGS:
            if type(checks.get(key)) is not bool:
                raise ShieldAPIError(f"Incomplete Limbo check settings: '{key}' is missing or invalid.")
        if checks.get("legacy_policy") not in LEGACY_POLICIES:
            raise ShieldAPIError("Incomplete Limbo check settings: 'legacy_policy' is missing or invalid.")
        return checks

    @staticmethod
    def _editable_checks(checks: dict[str, Any]) -> dict[str, Any]:
        # Never send API administrator-only timing values; those are immutable to customers.
        return {**{key: checks[key] for key in CHECK_FLAGS}, "legacy_policy": checks["legacy_policy"]}

    async def set_check(self, name: str, enabled: bool) -> dict[str, Any]:
        if name not in CHECK_FLAGS or type(enabled) is not bool:
            raise ShieldAPIError("Invalid Limbo check or value.")
        checks = await self.get_checks()
        if checks[name] != enabled:
            body = self._editable_checks(checks)
            body[name] = enabled
            await self._request("POST", self.root + "/mc-limbo-checks", body)
        confirmed = await self.get_checks()
        if confirmed[name] is not enabled:
            raise ShieldAPIError("The Limbo check change was not confirmed by the API.")
        return confirmed

    async def set_legacy_policy(self, policy: str) -> dict[str, Any]:
        if policy not in LEGACY_POLICIES:
            raise ShieldAPIError("Invalid legacy client policy.")
        checks = await self.get_checks()
        if checks["legacy_policy"] != policy:
            body = self._editable_checks(checks)
            body["legacy_policy"] = policy
            await self._request("POST", self.root + "/mc-limbo-checks", body)
        confirmed = await self.get_checks()
        if confirmed["legacy_policy"] != policy:
            raise ShieldAPIError("The legacy policy change was not confirmed by the API.")
        return confirmed

    async def require_gate(self) -> None:
        target = await self.get_target()
        if target.get("mc_enabled") is not True or target.get("mc_gate_enabled") is not True:
            raise ShieldAPIError(
                "Enable the Minecraft filter (mc_enabled) and Minecraft Gate "
                "(mc_gate_enabled) in the Shield panel first. The bot will not overwrite "
                "your complete target protection policy."
            )

    async def get_status(self) -> CaptchaStatus:
        target = await self.get_target()
        for key in ("mc_enabled", "mc_gate_enabled"):
            if type(target.get(key)) is not bool:
                raise ShieldAPIError(f"The target is missing the valid '{key}' field.")
        limbo = await self.get_limbo()
        checks = await self.get_checks()
        return CaptchaStatus(
            target["mc_enabled"], target["mc_gate_enabled"], limbo,
            checks["captcha"], checks["legacy_policy"],
        )

    async def turn_on(self) -> CaptchaStatus:
        await self.require_gate()
        await self.set_check("captcha", True)
        if not await self.get_limbo():
            await self.set_limbo(True)
        status = await self.get_status()
        if not status.active:
            raise ShieldAPIError("CAPTCHA could not be fully enabled. Check /mapcaptcha status.")
        return status

    async def turn_off(self) -> CaptchaStatus:
        await self.set_check("captcha", False)
        # Limbo and every other check deliberately remain as they were.
        status = await self.get_status()
        if status.captcha:
            raise ShieldAPIError("CAPTCHA could not be disabled. Check /mapcaptcha status.")
        return status


def _dict_or_error(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ShieldAPIError(f"Shield API did not return valid {label} data.")
    return data
