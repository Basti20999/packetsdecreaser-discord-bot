"""English-language Discord staff and admin bot for PacketShield Map CAPTCHA."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from settings import GuildSettings, SettingsStore, valid_ip
from shield_api import CHECK_FLAGS, CaptchaStatus, ShieldAPIError, ShieldClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("packetshield-discord-bot")


def env(name: str, *, required: bool = False) -> str:
    result = os.getenv(name, "").strip()
    if required and (not result or result.upper().startswith("YOUR_")):
        raise ValueError(f"Set {name} in the .env file.")
    if result.upper().startswith("YOUR_"):
        return ""
    return result


def discord_id(value: str, name: str) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a Discord ID (positive integer).") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return parsed


DISCORD_TOKEN = env("DISCORD_TOKEN", required=True)
DISCORD_GUILD_ID = discord_id(env("DISCORD_GUILD_ID", required=True), "DISCORD_GUILD_ID")
SHIELD_API_KEY = env("SHIELD_API_KEY", required=True)
_raw_roles = env("STAFF_ROLE_IDS")
try:
    _roles = [int(value.strip()) for value in _raw_roles.split(",") if value.strip()]
except ValueError as exc:
    raise ValueError("STAFF_ROLE_IDS must contain comma-separated role IDs.") from exc

DEFAULT_SETTINGS = GuildSettings(
    target_ip=env("SHIELD_TARGET_IP") or None,
    staff_role_ids=_roles,
    log_channel_id=discord_id(env("LOG_CHANNEL_ID"), "LOG_CHANNEL_ID"),
)
CONFIG_PATH = Path(env("CONFIG_PATH") or Path(__file__).with_name("settings.json"))


class StaffBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.session: aiohttp.ClientSession | None = None
        self.operation_lock = asyncio.Lock()
        self.store = SettingsStore(CONFIG_PATH, DEFAULT_SETTINGS)

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        guild = discord.Object(id=DISCORD_GUILD_ID)
        for group in (mapcaptcha, config, protection):
            self.tree.add_command(group, guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Registered English slash commands for guild %s", DISCORD_GUILD_ID)

    def client_for(self, target_ip: str | None = None) -> ShieldClient:
        if self.session is None:
            raise ShieldAPIError("The API client has not started.")
        ip = target_ip or self.store.config.target_ip
        if not ip:
            raise ShieldAPIError("No target IP is configured. An administrator can use /config target.")
        return ShieldClient(self.session, SHIELD_API_KEY, ip)

    def save_config(self, updated: GuildSettings) -> None:
        before = self.store.config
        self.store.config = updated
        try:
            self.store.save()
        except OSError:
            self.store.config = before
            logger.exception("Unable to save bot settings")
            raise ShieldAPIError("Cannot save bot settings. Check server filesystem permissions.")
        except Exception:
            self.store.config = before
            raise

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()


mapcaptcha = app_commands.Group(name="mapcaptcha", description="Control Minecraft Map CAPTCHA")
config = app_commands.Group(
    name="config", description="Configure the bot (Discord administrators only)",
    default_permissions=discord.Permissions(administrator=True),
)
protection = app_commands.Group(
    name="protection", description="Configure Minecraft protection (administrators only)",
    default_permissions=discord.Permissions(administrator=True),
)
bot = StaffBot()


def is_admin(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return bool(
        interaction.guild_id == DISCORD_GUILD_ID
        and isinstance(member, discord.Member)
        and member.guild_permissions.administrator
    )


def is_staff(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return bool(
        interaction.guild_id == DISCORD_GUILD_ID
        and isinstance(member, discord.Member)
        and (
            is_admin(interaction)
            or any(role.id in bot.store.config.staff_role_ids for role in member.roles)
        )
    )


async def authorize(interaction: discord.Interaction, *, admin_only: bool) -> bool:
    if is_admin(interaction) if admin_only else is_staff(interaction):
        return True
    message = (
        "⛔ You need the **Administrator** permission to use this command."
        if admin_only else "⛔ You need a configured **staff role** or **Administrator** permission."
    )
    await interaction.response.send_message(message, ephemeral=True)
    return False


async def audit(
    interaction: discord.Interaction, operation: str, success: bool, detail: str,
    *, channel_id: int | None = None,
) -> None:
    destination_id = channel_id if channel_id is not None else bot.store.config.log_channel_id
    if not destination_id:
        return
    try:
        channel = bot.get_channel(destination_id) or await bot.fetch_channel(destination_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning("The configured audit destination is not a text channel")
            return
        await channel.send(
            f"🛡️ **Shield bot: {operation}** — {'SUCCESS' if success else 'FAILED'}\n"
            f"User: <@{interaction.user.id}> (`{interaction.user.id}`)\n"
            f"Target: `{bot.store.config.target_ip or 'Not configured'}`\n"
            f"Details: {detail}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException:
        logger.warning("Could not send an audit log", exc_info=True)


def format_status(status: CaptchaStatus) -> str:
    def state(enabled: bool) -> str:
        return "ON" if enabled else "OFF"
    text = (
        f"Minecraft filter: **{state(status.minecraft_filter)}**\n"
        f"Minecraft Gate: **{state(status.minecraft_gate)}**\n"
        f"Limbo: **{state(status.limbo)}**\n"
        f"CAPTCHA check: **{state(status.captcha)}**\n"
        f"Legacy client policy: `{status.legacy_policy}`\n"
        f"**CAPTCHA fully active: {'YES' if status.active else 'NO'}**"
    )
    if status.legacy_policy in ("captcha", "captcha_verify2fa"):
        text += "\n⚠️ Legacy clients may still receive a CAPTCHA based on the legacy policy."
    return text


ApiAction = Callable[[ShieldClient], Awaitable[str]]


async def run_api(
    interaction: discord.Interaction, label: str, action: ApiAction, *, admin_only: bool = False
) -> None:
    if not await authorize(interaction, admin_only=admin_only):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.operation_lock:
        try:
            ip = bot.store.config.target_ip
            client = bot.client_for()
            output = await action(client)
            await interaction.followup.send(f"✅ {output}\nTarget: `{ip}`", ephemeral=True)
            await audit(interaction, label, True, output[:600])
        except ShieldAPIError as exc:
            await interaction.followup.send(f"❌ **Operation failed:** {exc}", ephemeral=True)
            await audit(interaction, label, False, str(exc))
        except Exception:
            logger.exception("Unexpected error in %s", label)
            await interaction.followup.send("❌ Internal error. Check the bot console.", ephemeral=True)
            await audit(interaction, label, False, "Internal error")


@mapcaptcha.command(name="on", description="Enable Map CAPTCHA during a bot attack")
async def captcha_on(interaction: discord.Interaction) -> None:
    async def action(client: ShieldClient) -> str:
        status = await client.turn_on()
        return "**Map CAPTCHA enabled.**\n" + format_status(status)
    await run_api(interaction, "Map CAPTCHA ON", action)


@mapcaptcha.command(name="off", description="Disable the CAPTCHA check, preserving all other checks")
async def captcha_off(interaction: discord.Interaction) -> None:
    async def action(client: ShieldClient) -> str:
        status = await client.turn_off()
        return "**Map CAPTCHA check disabled; other Limbo checks unchanged.**\n" + format_status(status)
    await run_api(interaction, "Map CAPTCHA OFF", action)


@mapcaptcha.command(name="status", description="Show Minecraft CAPTCHA protection status")
async def captcha_status(interaction: discord.Interaction) -> None:
    async def action(client: ShieldClient) -> str:
        status = await client.get_status()
        return "**Map CAPTCHA status**\n" + format_status(status)
    await run_api(interaction, "Map CAPTCHA STATUS", action)


def format_config() -> str:
    cfg = bot.store.config
    roles = ", ".join(f"<@&{rid}>" for rid in cfg.staff_role_ids) or "None — administrators only"
    logs = f"<#{cfg.log_channel_id}>" if cfg.log_channel_id else "Disabled"
    return (
        "**Bot configuration**\n"
        f"Target IP: `{cfg.target_ip or 'Not configured'}`\n"
        f"Staff roles: {roles}\n"
        f"Audit log channel: {logs}\n"
        "Administrators automatically have full access. API credentials are stored only in `.env`."
    )


@config.command(name="show", description="View current bot settings")
async def config_show(interaction: discord.Interaction) -> None:
    if await authorize(interaction, admin_only=True):
        await interaction.response.send_message(format_config(), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@config.command(name="target", description="Set the protected Minecraft target IPv4 address")
@app_commands.describe(ip="IPv4 address assigned to your PacketShield account")
async def config_target(interaction: discord.Interaction, ip: str) -> None:
    if not await authorize(interaction, admin_only=True):
        return
    try:
        ip = valid_ip(ip)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.operation_lock:
        try:
            await bot.client_for(ip).get_target()  # Verify ownership/API access before saving.
            current = bot.store.config.target_ip
            updated = copy.deepcopy(bot.store.config)
            updated.target_ip = ip
            bot.save_config(updated)
            await interaction.followup.send(f"✅ Target IP changed to `{ip}`.", ephemeral=True)
            await audit(interaction, "CONFIG TARGET", True, f"{current or '(unset)'} → {ip}")
        except (ShieldAPIError, ValueError) as exc:
            await interaction.followup.send(f"❌ Could not set target: {exc}", ephemeral=True)
        except Exception:
            logger.exception("Target configuration failed")
            await interaction.followup.send("❌ Internal error. Check the bot console.", ephemeral=True)


@config.command(name="staff-add", description="Give a Discord role permission to use Map CAPTCHA")
async def config_staff_add(interaction: discord.Interaction, role: discord.Role) -> None:
    if not await authorize(interaction, admin_only=True):
        return
    if role.is_default():
        await interaction.response.send_message("❌ You cannot add the @everyone role as staff.", ephemeral=True)
        return
    async with bot.operation_lock:
        try:
            updated = copy.deepcopy(bot.store.config)
            updated.staff_role_ids.append(role.id)
            bot.save_config(updated)
            await interaction.response.send_message(f"✅ Added {role.mention} as a staff role.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await audit(interaction, "CONFIG STAFF ADD", True, f"Role ID {role.id}")
        except ShieldAPIError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@config.command(name="staff-remove", description="Remove a role's Map CAPTCHA permission")
async def config_staff_remove(interaction: discord.Interaction, role: discord.Role) -> None:
    if not await authorize(interaction, admin_only=True):
        return
    async with bot.operation_lock:
        try:
            updated = copy.deepcopy(bot.store.config)
            updated.staff_role_ids = [rid for rid in updated.staff_role_ids if rid != role.id]
            bot.save_config(updated)
            await interaction.response.send_message(f"✅ Removed {role.mention} from staff roles.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await audit(interaction, "CONFIG STAFF REMOVE", True, f"Role ID {role.id}")
        except ShieldAPIError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@config.command(name="logs", description="Set the channel for audit logs")
async def config_logs(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await authorize(interaction, admin_only=True):
        return
    async with bot.operation_lock:
        try:
            updated = copy.deepcopy(bot.store.config)
            updated.log_channel_id = channel.id
            bot.save_config(updated)
            await interaction.response.send_message(f"✅ Audit logs will be sent to {channel.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await audit(interaction, "CONFIG LOGS", True, f"Channel ID {channel.id}")
        except ShieldAPIError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@config.command(name="logs-off", description="Disable audit logs")
async def config_logs_off(interaction: discord.Interaction) -> None:
    if not await authorize(interaction, admin_only=True):
        return
    async with bot.operation_lock:
        try:
            previous = bot.store.config.log_channel_id
            updated = copy.deepcopy(bot.store.config)
            updated.log_channel_id = None
            bot.save_config(updated)
            await interaction.response.send_message("✅ Audit logging disabled.", ephemeral=True)
            if previous is not None:
                await audit(interaction, "CONFIG LOGS OFF", True, "Audit logging disabled", channel_id=previous)
        except ShieldAPIError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@protection.command(name="status", description="Read all supported Minecraft Limbo protection settings")
async def protection_status(interaction: discord.Interaction) -> None:
    async def action(client: ShieldClient) -> str:
        status = await client.get_status()
        checks = await client.get_checks()
        verify = await client.get_web_verify()
        details = ", ".join(f"`{flag}`={'ON' if checks[flag] else 'OFF'}" for flag in CHECK_FLAGS)
        return (
            "**Minecraft protection settings**\n"
            + format_status(status) + "\n"
            + f"Limbo checks: {details}\n"
            + f"Website verification: **{'ON' if verify else 'OFF'}**"
        )
    await run_api(interaction, "PROTECTION STATUS", action, admin_only=True)


@protection.command(name="check", description="Enable or disable a specific Limbo check")
@app_commands.describe(name="Which check to change", enabled="Enable or disable this check")
@app_commands.choices(name=[app_commands.Choice(name=flag, value=flag) for flag in CHECK_FLAGS])
async def protection_check(interaction: discord.Interaction, name: app_commands.Choice[str], enabled: bool) -> None:
    async def action(client: ShieldClient) -> str:
        await client.set_check(name.value, enabled)
        return f"Limbo check `{name.value}` is now **{'ON' if enabled else 'OFF'}**."
    await run_api(interaction, "PROTECTION CHECK", action, admin_only=True)


@protection.command(name="legacy", description="Set the policy used for older Minecraft clients")
@app_commands.choices(policy=[
    app_commands.Choice(name="kick", value="kick"),
    app_commands.Choice(name="captcha", value="captcha"),
    app_commands.Choice(name="verify2fa", value="verify2fa"),
    app_commands.Choice(name="captcha_verify2fa", value="captcha_verify2fa"),
])
async def protection_legacy(interaction: discord.Interaction, policy: app_commands.Choice[str]) -> None:
    async def action(client: ShieldClient) -> str:
        await client.set_legacy_policy(policy.value)
        return f"Legacy client policy set to `{policy.value}`."
    await run_api(interaction, "PROTECTION LEGACY", action, admin_only=True)


@protection.command(name="limbo", description="Enable or disable the Minecraft Limbo backend")
async def protection_limbo(interaction: discord.Interaction, enabled: bool) -> None:
    async def action(client: ShieldClient) -> str:
        if enabled:
            await client.require_gate()
        await client.set_limbo(enabled)
        return f"Limbo is now **{'ON' if enabled else 'OFF'}**."
    await run_api(interaction, "PROTECTION LIMBO", action, admin_only=True)


@protection.command(name="website", description="Enable or disable Minecraft website verification")
async def protection_website(interaction: discord.Interaction, enabled: bool) -> None:
    async def action(client: ShieldClient) -> str:
        await client.set_web_verify(enabled)
        return f"Website verification is now **{'ON' if enabled else 'OFF'}**."
    await run_api(interaction, "PROTECTION WEBSITE", action, admin_only=True)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s", bot.user)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
