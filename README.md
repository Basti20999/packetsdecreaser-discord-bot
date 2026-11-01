# PacketShield / PacketsDecreaser – Discord Map CAPTCHA Bot

An **English-language Discord bot** for controlling the Minecraft Limbo CAPTCHA feature of the PacketShield Panel API (OpenAPI v410).

> **Terminology:** Your uploaded API describes a `captcha` flag in the Minecraft **Limbo checks** (`/api/targets/{ip}/mc-limbo-checks`). It does **not** explicitly call this feature "Map CAPTCHA". Confirm in the Shield panel that this is the feature you want.

## Roles and permissions

- Members with Discord's **Administrator** permission can use **all commands**, even if they have no configured staff role.
- Members with a staff role configured using `/config staff-add` can use only `/mapcaptcha on`, `/mapcaptcha off`, and `/mapcaptcha status`.
- Everyone else is denied. Authorization is checked on the server **every time a command runs**, not just via the Discord command interface.
- All command responses are ephemeral (visible only to the person issuing the command). A configurable log channel receives a short audit message without mentioning/pinging the user.

## Slash commands

| Command | Who | Action |
| --- | --- | --- |
| `/mapcaptcha on` | Staff or admin | Enables the Limbo CAPTCHA check and Limbo itself (if required); verifies the result. |
| `/mapcaptcha off` | Staff or admin | Disables only the CAPTCHA check, preserving the other checks and Limbo state. |
| `/mapcaptcha status` | Staff or admin | Shows Minecraft Filter/Gate, Limbo, CAPTCHA and legacy policy. |
| `/config show` | Admin | Displays current bot settings. |
| `/config target ip:<IPv4>` | Admin | Changes the protected target IP, after verifying it is accessible with the API key. |
| `/config staff-add role:<role>` | Admin | Allows a role to use Map CAPTCHA commands. |
| `/config staff-remove role:<role>` | Admin | Removes a staff role. |
| `/config logs channel:<channel>` | Admin | Sets the audit log channel. |
| `/config logs-off` | Admin | Disables audit logs. |
| `/protection status` | Admin | Shows supported Limbo checks and website verification. |
| `/protection check name:<check> enabled:<true/false>` | Admin | Sets one of `fall`, `explosion`, `keepalive`, `movement`, `jitter`, `captcha`. |
| `/protection legacy policy:<policy>` | Admin | Sets one of `kick`, `captcha`, `verify2fa`, `captcha_verify2fa`. |
| `/protection limbo enabled:<true/false>` | Admin | Switches Minecraft Limbo on/off. |
| `/protection website enabled:<true/false>` | Admin | Switches website verification on/off. |

### What administrators cannot change from Discord

- The **Discord bot token** and **Shield API key** are secrets and are only set in the server's `.env` file. They are **never pasted into Discord chat** and are never saved in `settings.json`.
- The Discord guild ID is set during installation in `.env` and determines where commands are registered.
- Minecraft **Filter** (`mc_enabled`) and **Gate** (`mc_gate_enabled`) should be set in the Shield panel first. The documented API upsert for target configuration replaces the **entire target policy**, so this bot deliberately does not use it to flip these two switches in order to avoid unintentionally wiping other protections.
- The API's `captcha_seconds`, `verify2fa_seconds`, and `pass_percent` are **Shield administrator-only** fields. Discord Administrator permission cannot override that API limitation.

## Setup

1. Install **Python 3.11+**.
2. Create a Discord application and bot at <https://discord.com/developers/applications>, then invite it to your server using the `bot` and `applications.commands` scopes. Enable **View Channel** and **Send Messages** permissions in the audit channel if you want logs. No privileged Member intent is needed.
3. Copy `.env.example` to `.env`. Fill in `DISCORD_TOKEN`, `DISCORD_GUILD_ID` and `SHIELD_API_KEY`. You can also set an initial `SHIELD_TARGET_IP`, `STAFF_ROLE_IDS` and `LOG_CHANNEL_ID` or configure them later from Discord. **Do not publish `.env`.**
4. Install dependencies and start the bot:

   ```bash
   python -m pip install -r requirements.txt
   python bot.py
   ```

5. In Discord, run `/config show`; if no target is configured, run `/config target ip:YOUR_PROTECTED_SERVER_IPV4` as an **Administrator**. You can configure staff roles and logs with `/config`.
6. In the Shield panel, enable the **Minecraft filter** and **Minecraft Gate** for this target; they are prerequisites for Limbo CAPTCHA. Then use `/mapcaptcha status` followed by `/mapcaptcha on` when needed.

### Persistence

Bot settings changed with `/config` are saved in `settings.json` and survive bot restarts. The file contains only the protected target IP, staff-role IDs, and log-channel ID. Settings loaded from `settings.json` take priority over optional initial values in `.env`. To reset the bot's non-secret settings, stop the bot, remove `settings.json`, and restart it.

The single-server bot uses `DISCORD_GUILD_ID` to determine the guild. Use **one running instance** with a shared settings file and API target; simultaneous independent instances are not coordinated.

## API behavior and safety

- Reads `/api/targets/{ip}` to check `mc_enabled` and `mc_gate_enabled`.
- Reads/changes `/api/targets/{ip}/mc-limbo` with `{ "enabled": true/false }`.
- Reads/changes `/api/targets/{ip}/mc-verify` for optional website verification.
- Reads `/api/targets/{ip}/mc-limbo-checks` **before** submitting a replacement check set. The bot preserves all other editable check values and the legacy policy; API administrator-only timing values are excluded.
- Verifies changes with a new API GET before reporting success.
- Uses HTTPS with no redirects, keeps the key out of Discord/console, and provides safe HTTP error messages.
- Only target IPs authorized for the configured Shield API key will work (403 otherwise).
- The bot's operation lock serializes API changes and admin configuration writes within this process.
- **Caution:** `/mapcaptcha off` does not change `legacy_policy`; if it is `captcha` or `captcha_verify2fa`, old clients may still be challenged. A Discord administrator can change the legacy policy using `/protection legacy`.

## Troubleshooting

- **401 Unauthorized:** Invalid/expired API key.
- **403 Forbidden:** Your Shield account does not own the IP or the API restricts the operation.
- **404 Not found:** Incorrect target IP or unavailable feature.
- **CAPTCHA will not enable:** Enable Minecraft Filter and Minecraft Gate in the Shield panel first.
- **Commands do not appear:** Check `DISCORD_GUILD_ID`, invite scopes and bot startup logs.
- **Changes not saved:** Check write permissions for the directory holding `settings.json`.

## Local tests (no live API key needed)

```bash
python -m unittest discover -s tests -v
```

The supplied OpenAPI spec was used for implementation; live API connectivity requires your own valid credentials.
