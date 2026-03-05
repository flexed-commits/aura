"""
role_cog.py — Full /role command suite + /temp-role for Aura moderation bot.

Slash commands
──────────────
  /role create        – Create a role (optional position above/below)
  /role delete        – Delete a role (hierarchy + managed-role check)
  /role add           – Assign a role to a member
  /role remove        – Remove a role from a member
  /role edit          – Edit role attributes; interactive UI when no args given
  /role reset         – Reset a role to defaults
  /role info          – Detailed role info embed with solid-color thumbnail

  /temp-role create   – Create a temporary role that auto-deletes after a duration
  /temp-role list     – List all active temporary roles in this server
  /temp-role cancel   – Delete a temporary role immediately

Duration format (for /temp-role create)
───────────────────────────────────────
  Combine any number of units:  30s · 5m · 2h · 3d · 1w · 2mo · 1yr
  Examples:  "1h30m"  "2d12h"  "1mo"  "90s"
  Minimum: 10 seconds   Maximum: 2 years

Persistence
───────────
  Active temp roles are stored in SQLite (bot_data.db → temp_roles table).
  A background task checks for expired roles every 30 seconds.
  On restart, all surviving temp roles are automatically re-scheduled so
  nothing is silently orphaned even if the bot goes offline mid-timer.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import re
import sqlite3
from typing import NamedTuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

DB_FILE              = "bot_data.db"
TEMP_ROLE_MIN_SECS   = 10                          # 10 seconds
TEMP_ROLE_MAX_SECS   = 2 * 365 * 24 * 3600        # 2 years
EXPIRY_CHECK_SECS    = 30                          # background task interval


# ── Duration parsing ──────────────────────────────────────────────────────────

# Maps suffix → seconds multiplier.  Order matters: "mo" before "m", "yr" before "y".
_UNIT_MAP: list[tuple[str, int]] = [
    ("yr",  365 * 24 * 3600),
    ("y",   365 * 24 * 3600),
    ("mo",  30  * 24 * 3600),
    ("w",   7   * 24 * 3600),
    ("d",   24  * 3600),
    ("h",   3600),
    ("m",   60),
    ("s",   1),
]

# Pre-built regex:  one or more (digits + optional-whitespace + unit) groups
_DURATION_RE = re.compile(
    r"(?P<value>\d+)\s*(?P<unit>" +
    "|".join(re.escape(u) for u, _ in _UNIT_MAP) +
    r")",
    re.IGNORECASE,
)


class ParsedDuration(NamedTuple):
    total_seconds: int
    human: str          # e.g. "1 day, 6 hours, 30 minutes"


def _parse_duration(raw: str) -> ParsedDuration | None:
    """
    Parse a compound duration string into total seconds and a human-readable label.

    Accepts any combination of:
      s (seconds), m (minutes), h (hours), d (days),
      w (weeks), mo (months ≈ 30 d), yr / y (years ≈ 365 d)

    Examples:
      "1h30m"   → 5400 s
      "2d"      → 172 800 s
      "1yr2mo"  → 425 days in seconds
      "90s"     → 90 s

    Returns None if no valid unit tokens are found.
    """
    matches = _DURATION_RE.findall(raw.strip())
    if not matches:
        return None

    unit_lookup: dict[str, int] = {u: s for u, s in _UNIT_MAP}
    total = 0
    parts: list[str] = []

    for value_str, unit_str in matches:
        value   = int(value_str)
        mult    = unit_lookup[unit_str.lower()]
        total  += value * mult

        # Build a human-friendly label for this chunk
        if unit_str.lower() in ("yr", "y"):
            label = f"{value} year{'s' if value != 1 else ''}"
        elif unit_str.lower() == "mo":
            label = f"{value} month{'s' if value != 1 else ''}"
        elif unit_str.lower() == "w":
            label = f"{value} week{'s' if value != 1 else ''}"
        elif unit_str.lower() == "d":
            label = f"{value} day{'s' if value != 1 else ''}"
        elif unit_str.lower() == "h":
            label = f"{value} hour{'s' if value != 1 else ''}"
        elif unit_str.lower() == "m":
            label = f"{value} minute{'s' if value != 1 else ''}"
        else:
            label = f"{value} second{'s' if value != 1 else ''}"
        parts.append(label)

    if total == 0:
        return None

    return ParsedDuration(total_seconds=total, human=", ".join(parts))


# ── Database ──────────────────────────────────────────────────────────────────

def init_role_db() -> None:
    """Create the bot_created_roles and temp_roles tables if they do not yet exist."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_created_roles (
                role_id    TEXT PRIMARY KEY,
                guild_id   TEXT NOT NULL,
                role_name  TEXT,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS temp_roles (
                role_id    TEXT PRIMARY KEY,
                guild_id   TEXT NOT NULL,
                role_name  TEXT NOT NULL,
                created_by TEXT NOT NULL,
                expires_at TEXT NOT NULL   -- ISO-8601 UTC timestamp
            )
        """)
        conn.commit()


def register_bot_role(role: discord.Role, created_by: str) -> None:
    """Record that Aura created this role so it can be identified later."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_created_roles "
            "(role_id, guild_id, role_name, created_by) VALUES (?, ?, ?, ?)",
            (str(role.id), str(role.guild.id), role.name, created_by),
        )
        conn.commit()


def is_bot_created_role(role_id: int) -> bool:
    """Return True if Aura originally created this role."""
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT role_id FROM bot_created_roles WHERE role_id = ?",
            (str(role_id),),
        ).fetchone()
    return bool(row)


# ── Temp-role DB helpers ──────────────────────────────────────────────────────

def _register_temp_role(
    role: discord.Role,
    created_by: str,
    expires_at: datetime.datetime,
) -> None:
    """Persist a new temp role record."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO temp_roles "
            "(role_id, guild_id, role_name, created_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(role.id),
                str(role.guild.id),
                role.name,
                created_by,
                expires_at.isoformat(),
            ),
        )
        conn.commit()


def _remove_temp_role(role_id: int) -> None:
    """Delete the temp role record (called after the role is deleted)."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "DELETE FROM temp_roles WHERE role_id = ?",
            (str(role_id),),
        )
        conn.commit()


def _load_all_temp_roles() -> list[dict]:
    """
    Return every temp role row as a dict.
    Used on startup to re-hydrate the expiry task.
    """
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT role_id, guild_id, role_name, created_by, expires_at "
            "FROM temp_roles"
        ).fetchall()
    return [
        {
            "role_id":    int(r[0]),
            "guild_id":   int(r[1]),
            "role_name":  r[2],
            "created_by": r[3],
            "expires_at": datetime.datetime.fromisoformat(r[4]),
        }
        for r in rows
    ]


def _load_guild_temp_roles(guild_id: int) -> list[dict]:
    """Return all temp role rows for a specific guild."""
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT role_id, guild_id, role_name, created_by, expires_at "
            "FROM temp_roles WHERE guild_id = ?",
            (str(guild_id),),
        ).fetchall()
    return [
        {
            "role_id":    int(r[0]),
            "guild_id":   int(r[1]),
            "role_name":  r[2],
            "created_by": r[3],
            "expires_at": datetime.datetime.fromisoformat(r[4]),
        }
        for r in rows
    ]


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=f"✅  {title}", description=description,
        color=discord.Color.green(), timestamp=_now(),
    )


def _error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌  {title}", description=description,
        color=discord.Color.red(), timestamp=_now(),
    )


def _warn_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=f"⚠️  {title}", description=description,
        color=discord.Color.yellow(), timestamp=_now(),
    )


def _info_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=f"⏳  {title}", description=description,
        color=discord.Color.blurple(), timestamp=_now(),
    )


# ── Misc helpers ──────────────────────────────────────────────────────────────

def _color_thumbnail(color: discord.Color) -> discord.File | None:
    """Generate a 128×128 solid-color PNG and return it as a discord.File."""
    if not PIL_AVAILABLE:
        return None
    img = Image.new("RGB", (128, 128), (color.r, color.g, color.b))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="role_color.png")


def _parse_color(value: str) -> discord.Color | None:
    """
    Parse a hex string (#RRGGBB / RRGGBB) or a named color into discord.Color.
    Returns None if the value cannot be interpreted.
    """
    value = value.strip().lstrip("#")
    try:
        return discord.Color(int(value, 16))
    except ValueError:
        pass
    named: dict[str, discord.Color] = {
        "red":     discord.Color.red(),
        "blue":    discord.Color.blue(),
        "green":   discord.Color.green(),
        "yellow":  discord.Color.yellow(),
        "orange":  discord.Color.orange(),
        "purple":  discord.Color.purple(),
        "magenta": discord.Color.magenta(),
        "gold":    discord.Color.gold(),
        "teal":    discord.Color.teal(),
        "white":   discord.Color(0xFFFFFF),
        "black":   discord.Color.default(),
        "pink":    discord.Color(0xFF69B4),
        "cyan":    discord.Color(0x00FFFF),
    }
    return named.get(value.lower())


async def _hierarchy_check(
    interaction: discord.Interaction,
    target_role: discord.Role,
) -> bool:
    """
    Verify that BOTH the bot AND the invoking user outrank target_role.
    Sends an ephemeral error embed and returns False if either check fails.
    """
    guild    = interaction.guild
    bot_top  = guild.me.top_role
    user_top = interaction.user.top_role

    if bot_top <= target_role:
        await interaction.response.send_message(
            embed=_error_embed(
                "Hierarchy Error",
                f"My highest role (**{bot_top.mention}**) is not above {target_role.mention}.\n"
                "I can't manage a role that sits at or above my own position.",
            ),
            ephemeral=True,
        )
        return False

    if user_top <= target_role and interaction.user.id != guild.owner_id:
        await interaction.response.send_message(
            embed=_error_embed(
                "Hierarchy Error",
                f"Your highest role (**{user_top.mention}**) is not above {target_role.mention}.\n"
                "You can only manage roles that are below your own top role.",
            ),
            ephemeral=True,
        )
        return False

    return True


async def _hierarchy_check_deferred(
    interaction: discord.Interaction,
    target_role: discord.Role,
) -> bool:
    """Same as _hierarchy_check but uses followup (for deferred interactions)."""
    guild    = interaction.guild
    bot_top  = guild.me.top_role
    user_top = interaction.user.top_role

    if bot_top <= target_role:
        await interaction.followup.send(
            embed=_error_embed(
                "Hierarchy Error",
                f"My highest role (**{bot_top.mention}**) is not above {target_role.mention}.\n"
                "I can't manage a role that sits at or above my own position.",
            ),
            ephemeral=True,
        )
        return False

    if user_top <= target_role and interaction.user.id != guild.owner_id:
        await interaction.followup.send(
            embed=_error_embed(
                "Hierarchy Error",
                f"Your highest role (**{user_top.mention}**) is not above {target_role.mention}.\n"
                "You can only manage roles that are below your own top role.",
            ),
            ephemeral=True,
        )
        return False

    return True


async def _move_role(
    guild: discord.Guild,
    role: discord.Role,
    target_position: int,
    reason: str = "",
) -> str | None:
    """
    Move `role` to `target_position` via guild.edit_role_positions().
    Returns None on success, or a human-readable error string on failure.
    """
    bot_top  = guild.me.top_role
    max_pos  = bot_top.position - 1

    if target_position > max_pos:
        return (
            f"Position **{target_position}** is at or above my highest role "
            f"(**{bot_top.mention}**, position {bot_top.position}). "
            "I can only place roles below my own position."
        )

    target_position = max(1, target_position)

    try:
        await guild.edit_role_positions(
            positions={role: target_position},
            reason=reason,
        )
        return None
    except discord.Forbidden:
        return "I don't have permission to reposition roles."
    except discord.HTTPException as exc:
        return f"Discord API error while repositioning: {exc}"


# ── Interactive edit modals ───────────────────────────────────────────────────

class SingleFieldModal(discord.ui.Modal):
    answer = discord.ui.TextInput(label="New value", max_length=100)

    def __init__(self, title: str, label: str, placeholder: str, callback_fn) -> None:
        super().__init__(title=title)
        self.answer.label       = label
        self.answer.placeholder = placeholder
        self._callback_fn       = callback_fn

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback_fn(interaction, self.answer.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            embed=_error_embed("Modal Error", f"Something went wrong: {error}"),
            ephemeral=True,
        )


class RoleEditView(discord.ui.View):
    """Interactive button panel for /role edit (no args path)."""

    def __init__(self, role: discord.Role) -> None:
        super().__init__(timeout=120)
        self.role = role

    # ── Name ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label="✏️ Name", style=discord.ButtonStyle.primary)
    async def btn_name(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def apply(inter: discord.Interaction, value: str) -> None:
            if not value.strip():
                await inter.response.send_message(
                    embed=_error_embed("Invalid Name", "The role name cannot be empty."),
                    ephemeral=True,
                )
                return
            try:
                await self.role.edit(name=value.strip(), reason=f"Name edited by {inter.user}")
                await inter.response.send_message(
                    embed=_success_embed("Role Updated", f"Name changed to **{value.strip()}**."),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await inter.response.send_message(
                    embed=_error_embed("Permission Denied", "I don't have permission to edit this role."),
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await inter.response.send_message(
                    embed=_error_embed("API Error", str(exc)), ephemeral=True,
                )

        await interaction.response.send_modal(
            SingleFieldModal("Edit Role Name", "New Name", self.role.name, apply)
        )

    # ── Color ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label="🎨 Color", style=discord.ButtonStyle.primary)
    async def btn_color(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def apply(inter: discord.Interaction, value: str) -> None:
            color = _parse_color(value)
            if color is None:
                await inter.response.send_message(
                    embed=_error_embed(
                        "Invalid Color",
                        "Couldn't recognise that color. Try a hex like `#FF5733` "
                        "or a name like `red`, `blue`, `gold`.",
                    ),
                    ephemeral=True,
                )
                return
            try:
                await self.role.edit(color=color, reason=f"Color edited by {inter.user}")
                await inter.response.send_message(
                    embed=_success_embed("Role Updated", f"Color changed to `{value}`."),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await inter.response.send_message(
                    embed=_error_embed("Permission Denied", "I don't have permission to edit this role."),
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await inter.response.send_message(
                    embed=_error_embed("API Error", str(exc)), ephemeral=True,
                )

        await interaction.response.send_modal(
            SingleFieldModal("Edit Role Color", "Hex or Color Name", "#FF5733  or  red", apply)
        )

    # ── Hoist ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label="📌 Hoist", style=discord.ButtonStyle.secondary)
    async def btn_hoist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def apply(inter: discord.Interaction, value: str) -> None:
            val = value.strip().lower()
            if val not in ("true", "false", "yes", "no", "1", "0"):
                await inter.response.send_message(
                    embed=_error_embed("Invalid Value", "Please enter `true` or `false`."),
                    ephemeral=True,
                )
                return
            hoist = val in ("true", "yes", "1")
            try:
                await self.role.edit(hoist=hoist, reason=f"Hoist edited by {inter.user}")
                await inter.response.send_message(
                    embed=_success_embed("Role Updated", f"Hoist set to **{hoist}**."),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await inter.response.send_message(
                    embed=_error_embed("Permission Denied", "I don't have permission to edit this role."),
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await inter.response.send_message(
                    embed=_error_embed("API Error", str(exc)), ephemeral=True,
                )

        await interaction.response.send_modal(
            SingleFieldModal("Edit Hoist Setting", "true / false", "true", apply)
        )

    # ── Mentionable ───────────────────────────────────────────────────────────
    @discord.ui.button(label="📣 Mentionable", style=discord.ButtonStyle.secondary)
    async def btn_mentionable(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def apply(inter: discord.Interaction, value: str) -> None:
            val = value.strip().lower()
            if val not in ("true", "false", "yes", "no", "1", "0"):
                await inter.response.send_message(
                    embed=_error_embed("Invalid Value", "Please enter `true` or `false`."),
                    ephemeral=True,
                )
                return
            mentionable = val in ("true", "yes", "1")
            try:
                await self.role.edit(mentionable=mentionable, reason=f"Mentionable edited by {inter.user}")
                await inter.response.send_message(
                    embed=_success_embed("Role Updated", f"Mentionable set to **{mentionable}**."),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await inter.response.send_message(
                    embed=_error_embed("Permission Denied", "I don't have permission to edit this role."),
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                await inter.response.send_message(
                    embed=_error_embed("API Error", str(exc)), ephemeral=True,
                )

        await interaction.response.send_modal(
            SingleFieldModal("Edit Mentionable Setting", "true / false", "false", apply)
        )

    # ── Position ──────────────────────────────────────────────────────────────
    @discord.ui.button(label="🔢 Position", style=discord.ButtonStyle.secondary)
    async def btn_position(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def apply(inter: discord.Interaction, value: str) -> None:
            try:
                pos = int(value.strip())
                if pos < 1:
                    raise ValueError
            except ValueError:
                await inter.response.send_message(
                    embed=_error_embed("Invalid Value", "Position must be a whole number greater than 0."),
                    ephemeral=True,
                )
                return
            err = await _move_role(inter.guild, self.role, pos, reason=f"Position edited by {inter.user}")
            if err:
                await inter.response.send_message(
                    embed=_error_embed("Hierarchy Error", err), ephemeral=True,
                )
            else:
                await inter.response.send_message(
                    embed=_success_embed("Role Updated", f"Role moved to position **{pos}**."),
                    ephemeral=True,
                )

        await interaction.response.send_modal(
            SingleFieldModal("Edit Role Position", "Numeric position", "e.g. 3", apply)
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


# ── Cog ───────────────────────────────────────────────────────────────────────

class RoleCog(commands.Cog, name="Role Management"):
    """All /role and /temp-role commands for Aura."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Background expiry task ────────────────────────────────────────────────

    @tasks.loop(seconds=EXPIRY_CHECK_SECS)
    async def _expiry_loop(self) -> None:
        """
        Runs every 30 seconds.  Deletes any temp roles whose expires_at has passed.
        Stale DB entries (role already gone) are cleaned up silently.
        """
        now      = _now()
        pending  = _load_all_temp_roles()

        for record in pending:
            expires_at: datetime.datetime = record["expires_at"]

            # Make sure it's timezone-aware so comparisons work regardless of
            # whether it was stored with or without a UTC offset
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

            if now < expires_at:
                continue  # not yet expired

            guild = self.bot.get_guild(record["guild_id"])
            if guild is None:
                # Bot is no longer in this guild — just clean up the record
                _remove_temp_role(record["role_id"])
                continue

            role = guild.get_role(record["role_id"])
            if role is None:
                # Already deleted externally
                _remove_temp_role(record["role_id"])
                continue

            try:
                await role.delete(reason="Temporary role expired — auto-deleted by Aura")
            except (discord.Forbidden, discord.HTTPException):
                pass  # best-effort; remove the record either way
            finally:
                _remove_temp_role(record["role_id"])

    @_expiry_loop.before_loop
    async def _before_expiry_loop(self) -> None:
        await self.bot.wait_until_ready()

    def cog_unload(self) -> None:
        self._expiry_loop.cancel()

    # ── /role group ───────────────────────────────────────────────────────────

    role_group = app_commands.Group(
        name="role",
        description="Manage roles — create, delete, assign, edit, and inspect.",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    # ── /role create ──────────────────────────────────────────────────────────

    @role_group.command(name="create", description="Create a new role in this server.")
    @app_commands.describe(
        name="Name for the new role",
        color="Color — hex code (#FF5733) or a color name (red, blue, gold…)",
        hoist="Show role members separately in the member list",
        mentionable="Allow everyone to @mention this role",
        above_role="Place the new role ABOVE this existing role",
        below_role="Place the new role BELOW this existing role",
    )
    async def role_create(
        self,
        interaction: discord.Interaction,
        name: str,
        color: str = "#000000",
        hoist: bool = False,
        mentionable: bool = False,
        above_role: discord.Role | None = None,
        below_role: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        name = name.strip()
        if not name:
            await interaction.followup.send(
                embed=_error_embed("Invalid Name", "The role name cannot be empty."),
                ephemeral=True,
            )
            return

        parsed_color = _parse_color(color)
        if parsed_color is None:
            await interaction.followup.send(
                embed=_error_embed(
                    "Invalid Color",
                    f"`{color}` is not a valid hex code or color name.\n"
                    "Examples: `#FF5733`, `red`, `blue`, `gold`.",
                ),
                ephemeral=True,
            )
            return

        if above_role and below_role:
            await interaction.followup.send(
                embed=_warn_embed("Conflicting Options", "Provide either `above_role` or `below_role`, not both."),
                ephemeral=True,
            )
            return

        target_position: int | None = None
        if above_role:
            if not await _hierarchy_check_deferred(interaction, above_role):
                return
            target_position = above_role.position + 1
        elif below_role:
            if not await _hierarchy_check_deferred(interaction, below_role):
                return
            target_position = max(1, below_role.position)

        if target_position is not None:
            bot_top = guild.me.top_role
            if target_position >= bot_top.position:
                await interaction.followup.send(
                    embed=_error_embed(
                        "Hierarchy Error",
                        f"Cannot place the new role at or above my highest role "
                        f"(**{bot_top.mention}**, position {bot_top.position}).",
                    ),
                    ephemeral=True,
                )
                return

        try:
            new_role = await guild.create_role(
                name=name, color=parsed_color, hoist=hoist, mentionable=mentionable,
                reason=f"Created via /role create by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_error_embed("Permission Denied", "I don't have permission to create roles here."),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )
            return

        pos_err: str | None = None
        if target_position is not None:
            pos_err = await _move_role(
                guild, new_role, target_position,
                reason=f"Positioned via /role create by {interaction.user}",
            )

        register_bot_role(new_role, str(interaction.user.id))

        lines = [
            f"{new_role.mention} has been created.",
            f"• **Color** → `#{parsed_color.value:06X}`",
            f"• **Hoisted** → `{hoist}`",
            f"• **Mentionable** → `{mentionable}`",
        ]
        if target_position is not None:
            lines.append(
                f"• **Position** → ⚠️ Could not set — {pos_err}"
                if pos_err else
                f"• **Position** → `{new_role.position}` (as requested)"
            )

        embed_fn = _warn_embed if pos_err else _success_embed
        await interaction.followup.send(
            embed=embed_fn(
                "Role Created (Position Issue)" if pos_err else "Role Created",
                "\n".join(lines),
            ),
            ephemeral=True,
        )

    # ── /role delete ──────────────────────────────────────────────────────────

    @role_group.command(name="delete", description="Permanently delete a role from this server.")
    @app_commands.describe(
        role="The role to delete",
        reason="Reason for deletion (shown in the audit log)",
    )
    async def role_delete(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str | None = None,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        if role.managed:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Managed Role",
                    f"{role.mention} is a managed role (created by an integration or bot) "
                    "and cannot be deleted this way.",
                ),
                ephemeral=True,
            )
            return

        audit_reason = (
            f"Deleted via /role delete by {interaction.user}"
            + (f" — {reason}" if reason else "")
        )
        role_name = role.name

        try:
            await role.delete(reason=audit_reason)
            await interaction.response.send_message(
                embed=_success_embed(
                    "Role Deleted",
                    f"The role **{role_name}** has been permanently deleted.\n"
                    + (f"**Reason:** {reason}" if reason else "No reason was provided."),
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Permission Denied",
                    "I don't have permission to delete that role. "
                    "Make sure my role is above it in the hierarchy.",
                ),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )

    # ── /role add ─────────────────────────────────────────────────────────────

    @role_group.command(name="add", description="Assign a role to a server member.")
    @app_commands.describe(user="The member to receive the role", role="The role to assign")
    async def role_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: discord.Role,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        executor = interaction.user
        if executor.id != interaction.guild.owner_id and executor.top_role <= role:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Hierarchy Error",
                    f"You can't assign {role.mention} — it's at or above your own highest role "
                    f"(**{executor.top_role.mention}**).",
                ),
                ephemeral=True,
            )
            return

        if role in user.roles:
            await interaction.response.send_message(
                embed=_warn_embed("Already Assigned", f"{user.mention} already has the {role.mention} role."),
                ephemeral=True,
            )
            return

        try:
            await user.add_roles(role, reason=f"Assigned via /role add by {interaction.user}")
            await interaction.response.send_message(
                embed=_success_embed("Role Assigned", f"{role.mention} has been given to {user.mention}."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed("Permission Denied", f"I couldn't assign {role.mention}. It may be higher than my top role."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )

    # ── /role remove ──────────────────────────────────────────────────────────

    @role_group.command(name="remove", description="Remove a role from a server member.")
    @app_commands.describe(user="The member to remove the role from", role="The role to remove")
    async def role_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: discord.Role,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        if role not in user.roles:
            await interaction.response.send_message(
                embed=_warn_embed("Role Not Found", f"{user.mention} doesn't have the {role.mention} role."),
                ephemeral=True,
            )
            return

        try:
            await user.remove_roles(role, reason=f"Removed via /role remove by {interaction.user}")
            await interaction.response.send_message(
                embed=_success_embed("Role Removed", f"{role.mention} has been removed from {user.mention}."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed("Permission Denied", f"I couldn't remove {role.mention}. It may be higher than my top role."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )

    # ── /role edit ────────────────────────────────────────────────────────────

    @role_group.command(name="edit", description="Edit a role's attributes.")
    @app_commands.describe(
        role="The role to edit",
        name="New name",
        color="New color — hex code or color name",
        hoist="Show members of this role separately in the member list",
        mentionable="Allow @mentions for this role",
        above_role="Move this role ABOVE another role",
        below_role="Move this role BELOW another role",
    )
    async def role_edit(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: str | None = None,
        color: str | None = None,
        hoist: bool | None = None,
        mentionable: bool | None = None,
        above_role: discord.Role | None = None,
        below_role: discord.Role | None = None,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        any_provided = any(
            v is not None for v in [name, color, hoist, mentionable, above_role, below_role]
        )

        if not any_provided:
            embed = discord.Embed(
                title=f"✏️  Edit Role — {role.name}",
                description=(
                    f"Click a button below to edit an attribute of {role.mention}.\n"
                    "Each button will open a small pop-up for you to enter the new value.\n\n"
                    "This panel expires after **2 minutes**."
                ),
                color=discord.Color.blurple(),
                timestamp=_now(),
            )
            await interaction.response.send_message(embed=embed, view=RoleEditView(role), ephemeral=True)
            return

        parsed_color: discord.Color | None = None
        if color is not None:
            parsed_color = _parse_color(color)
            if parsed_color is None:
                await interaction.response.send_message(
                    embed=_error_embed(
                        "Invalid Color",
                        f"`{color}` is not a valid hex code or color name.\n"
                        "Examples: `#FF5733`, `red`, `blue`, `gold`.",
                    ),
                    ephemeral=True,
                )
                return

        if above_role and below_role:
            await interaction.response.send_message(
                embed=_warn_embed("Conflicting Options", "Provide either `above_role` or `below_role`, not both."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild   = interaction.guild
        changes: dict = {}

        if name is not None:
            name = name.strip()
            if not name:
                await interaction.followup.send(
                    embed=_error_embed("Invalid Name", "The role name cannot be empty."),
                    ephemeral=True,
                )
                return
            changes["name"] = name
        if parsed_color is not None:
            changes["color"] = parsed_color
        if hoist is not None:
            changes["hoist"] = hoist
        if mentionable is not None:
            changes["mentionable"] = mentionable

        target_position: int | None = None
        if above_role:
            target_position = above_role.position + 1
        elif below_role:
            target_position = max(1, below_role.position)

        if target_position is not None:
            bot_top = guild.me.top_role
            if target_position >= bot_top.position:
                await interaction.followup.send(
                    embed=_error_embed(
                        "Hierarchy Error",
                        f"Cannot move {role.mention} to position **{target_position}** — "
                        f"that would place it at or above my highest role (**{bot_top.mention}**).",
                    ),
                    ephemeral=True,
                )
                return

        if changes:
            try:
                await role.edit(reason=f"Edited via /role edit by {interaction.user}", **changes)
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=_error_embed("Permission Denied", "I don't have permission to edit that role."),
                    ephemeral=True,
                )
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(
                    embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                    ephemeral=True,
                )
                return

        pos_err: str | None = None
        if target_position is not None:
            pos_err = await _move_role(
                guild, role, target_position,
                reason=f"Position changed via /role edit by {interaction.user}",
            )

        desc_lines = [f"{role.mention} has been updated:"]
        if "name" in changes:       desc_lines.append(f"**Name →** {changes['name']}")
        if "color" in changes:      desc_lines.append(f"**Color →** `{color}`")
        if "hoist" in changes:      desc_lines.append(f"**Hoist →** {changes['hoist']}")
        if "mentionable" in changes: desc_lines.append(f"**Mentionable →** {changes['mentionable']}")
        if target_position is not None:
            desc_lines.append(
                f"**Position →** ⚠️ Failed — {pos_err}"
                if pos_err else
                f"**Position →** {target_position}"
            )

        embed_fn = _warn_embed if pos_err else _success_embed
        await interaction.followup.send(
            embed=embed_fn(
                "Role Partially Updated" if pos_err else "Role Updated",
                "\n".join(desc_lines),
            ),
            ephemeral=True,
        )

    # ── /role reset ───────────────────────────────────────────────────────────

    @role_group.command(name="reset", description="Reset a role back to its default settings.")
    @app_commands.describe(role="The role to reset")
    async def role_reset(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        guild      = interaction.guild
        bot_created = is_bot_created_role(role.id)
        user_in_top5 = False

        if not bot_created:
            sorted_roles = sorted(
                [r for r in guild.roles if r.id != guild.default_role.id],
                key=lambda r: r.position,
                reverse=True,
            )
            top5_ids = {r.id for r in sorted_roles[:5]}
            user_in_top5 = any(r.id in top5_ids for r in interaction.user.roles)

        if not bot_created and not user_in_top5:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Permission Denied",
                    "You can only reset a role if one of the following is true:\n"
                    "• The role was **created by Aura**, or\n"
                    "• Your highest role is within the **top 5 roles** of this server.",
                ),
                ephemeral=True,
            )
            return

        try:
            await role.edit(
                color=discord.Color.default(),
                permissions=guild.default_role.permissions,
                hoist=False,
                mentionable=False,
                reason=f"Reset via /role reset by {interaction.user}",
            )
            await interaction.response.send_message(
                embed=_success_embed(
                    "Role Reset",
                    f"{role.mention} has been reset to defaults:\n"
                    f"• **Color** → `#000000` (none)\n"
                    f"• **Permissions** → inherited from @everyone\n"
                    f"• **Hoist** → `False`\n"
                    f"• **Mentionable** → `False`\n"
                    f"• **Name** → unchanged (`{role.name}`)",
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed("Permission Denied", "I don't have permission to edit that role."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )

    # ── /role info ────────────────────────────────────────────────────────────

    @role_group.command(name="info", description="Display detailed information about a role.")
    @app_commands.describe(role="The role to inspect")
    async def role_info(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        await interaction.response.defer()
        guild = interaction.guild

        member_count = sum(1 for m in guild.members if role in m.roles)
        allowed_perms = [
            perm.replace("_", " ").title()
            for perm, value in role.permissions if value
        ]
        perms_str   = ", ".join(allowed_perms) if allowed_perms else "No special permissions"
        created_ts  = int(role.created_at.timestamp())

        if role.managed:
            role_type = "🤖 Managed / Integration Role"
        elif role.id == guild.default_role.id:
            role_type = "👥 Default Role (@everyone)"
        elif role.color.value == 0:
            role_type = "🌈 Possible Gradient / No-Color Role"
        else:
            role_type = "🎨 Standard Color Role"

        # Check if it's also a temp role
        temp_records = _load_guild_temp_roles(guild.id)
        temp_info    = next((r for r in temp_records if r["role_id"] == role.id), None)

        embed = discord.Embed(
            title=f"🏷️  Role Info — {role.name}",
            description=role_type,
            color=role.color if role.color.value != 0 else discord.Color.greyple(),
            timestamp=_now(),
        )
        embed.add_field(name="Role ID",     value=f"`{role.id}`",                        inline=True)
        embed.add_field(name="Members",     value=str(member_count),                      inline=True)
        embed.add_field(name="Color",       value=f"`#{role.color.value:06X}`",           inline=True)
        embed.add_field(name="Hoisted",     value="Yes" if role.hoist else "No",          inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No",    inline=True)
        embed.add_field(name="Managed",     value="Yes" if role.managed else "No",        inline=True)
        embed.add_field(name="Position",    value=str(role.position),                     inline=True)
        embed.add_field(name="Created",     value=f"<t:{created_ts}:F>",                  inline=True)

        if temp_info:
            exp_ts = int(temp_info["expires_at"].timestamp())
            embed.add_field(
                name="⏳ Temporary Role",
                value=f"Expires <t:{exp_ts}:R> (<t:{exp_ts}:F>)",
                inline=False,
            )

        embed.add_field(name="Permissions", value=perms_str, inline=False)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}  •  Aura")

        thumb_file: discord.File | None = None
        if role.color.value != 0:
            thumb_file = _color_thumbnail(role.color)
            if thumb_file:
                embed.set_thumbnail(url="attachment://role_color.png")

        if thumb_file:
            await interaction.followup.send(embed=embed, file=thumb_file)
        else:
            await interaction.followup.send(embed=embed)

    # =========================================================================
    # ── /temp-role group ──────────────────────────────────────────────────────
    # =========================================================================

    temp_role_group = app_commands.Group(
        name="temp-role",
        description="Create and manage roles that automatically expire.",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    # ── /temp-role create ─────────────────────────────────────────────────────

    @temp_role_group.command(
        name="create",
        description="Create a temporary role that auto-deletes after a set duration.",
    )
    @app_commands.describe(
        name="Name for the temporary role",
        duration=(
            "How long the role should last — e.g. 30s, 5m, 2h, 1d, 1w, 2mo, 1yr. "
            "Combine units: 1d12h, 2h30m"
        ),
        color="Color — hex code (#FF5733) or a color name (red, blue, gold…)",
        hoist="Show role members separately in the member list",
        mentionable="Allow everyone to @mention this role",
    )
    async def temp_role_create(
        self,
        interaction: discord.Interaction,
        name: str,
        duration: str,
        color: str = "#000000",
        hoist: bool = False,
        mentionable: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # ── Validate name ─────────────────────────────────────────────────────
        name = name.strip()
        if not name:
            await interaction.followup.send(
                embed=_error_embed("Invalid Name", "The role name cannot be empty."),
                ephemeral=True,
            )
            return

        # ── Parse duration ────────────────────────────────────────────────────
        parsed = _parse_duration(duration)
        if parsed is None:
            await interaction.followup.send(
                embed=_error_embed(
                    "Invalid Duration",
                    f"`{duration}` couldn't be understood as a duration.\n\n"
                    "**Valid units:** `s` seconds · `m` minutes · `h` hours · `d` days · "
                    "`w` weeks · `mo` months · `yr` years\n\n"
                    "**Examples:** `30s` · `5m` · `2h` · `1d` · `1w` · `2mo` · `1yr` · `1d12h`",
                ),
                ephemeral=True,
            )
            return

        if parsed.total_seconds < TEMP_ROLE_MIN_SECS:
            await interaction.followup.send(
                embed=_error_embed(
                    "Duration Too Short",
                    f"The minimum duration for a temporary role is **10 seconds**.\n"
                    f"You entered: `{duration}` ({parsed.total_seconds}s).",
                ),
                ephemeral=True,
            )
            return

        if parsed.total_seconds > TEMP_ROLE_MAX_SECS:
            await interaction.followup.send(
                embed=_error_embed(
                    "Duration Too Long",
                    f"The maximum duration for a temporary role is **2 years**.\n"
                    f"You entered: `{duration}`.",
                ),
                ephemeral=True,
            )
            return

        # ── Validate color ────────────────────────────────────────────────────
        parsed_color = _parse_color(color)
        if parsed_color is None:
            await interaction.followup.send(
                embed=_error_embed(
                    "Invalid Color",
                    f"`{color}` is not a valid hex code or color name.\n"
                    "Examples: `#FF5733`, `red`, `blue`, `gold`.",
                ),
                ephemeral=True,
            )
            return

        # ── Create the role ───────────────────────────────────────────────────
        expires_at = _now() + datetime.timedelta(seconds=parsed.total_seconds)
        exp_ts     = int(expires_at.timestamp())

        try:
            new_role = await guild.create_role(
                name=name,
                color=parsed_color,
                hoist=hoist,
                mentionable=mentionable,
                reason=(
                    f"Temporary role created via /temp-role create by {interaction.user} — "
                    f"expires in {parsed.human}"
                ),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_error_embed(
                    "Permission Denied",
                    "I don't have permission to create roles in this server. "
                    "Please ensure I have the **Manage Roles** permission.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=_error_embed("API Error", f"Discord returned an error while creating the role: {exc}"),
                ephemeral=True,
            )
            return

        # ── Persist to DB (survives restarts) ─────────────────────────────────
        _register_temp_role(new_role, str(interaction.user.id), expires_at)
        register_bot_role(new_role, str(interaction.user.id))

        await interaction.followup.send(
            embed=_info_embed(
                "Temporary Role Created",
                f"{new_role.mention} will automatically be deleted <t:{exp_ts}:R>.\n\n"
                f"• **Duration** → {parsed.human}\n"
                f"• **Expires** → <t:{exp_ts}:F>\n"
                f"• **Color** → `#{parsed_color.value:06X}`\n"
                f"• **Hoisted** → `{hoist}`\n"
                f"• **Mentionable** → `{mentionable}`\n\n"
                f"Use `/temp-role cancel` to remove it early.",
            ),
            ephemeral=True,
        )

    # ── /temp-role list ───────────────────────────────────────────────────────

    @temp_role_group.command(
        name="list",
        description="Show all active temporary roles in this server.",
    )
    async def temp_role_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild   = interaction.guild
        records = _load_guild_temp_roles(guild.id)

        if not records:
            await interaction.followup.send(
                embed=_info_embed(
                    "No Active Temporary Roles",
                    "There are no temporary roles currently scheduled in this server.",
                ),
                ephemeral=True,
            )
            return

        # Filter out any records whose roles no longer exist in Discord
        live: list[dict] = []
        stale_ids: list[int] = []
        for rec in records:
            if guild.get_role(rec["role_id"]) is not None:
                live.append(rec)
            else:
                stale_ids.append(rec["role_id"])

        for stale_id in stale_ids:
            _remove_temp_role(stale_id)

        if not live:
            await interaction.followup.send(
                embed=_info_embed(
                    "No Active Temporary Roles",
                    "There are no temporary roles currently scheduled in this server.",
                ),
                ephemeral=True,
            )
            return

        now   = _now()
        lines = []
        for rec in sorted(live, key=lambda r: r["expires_at"]):
            role    = guild.get_role(rec["role_id"])
            exp_ts  = int(rec["expires_at"].timestamp())
            expires = rec["expires_at"] if rec["expires_at"].tzinfo else rec["expires_at"].replace(tzinfo=datetime.timezone.utc)
            remaining_s = int((expires - now).total_seconds())
            remaining_s = max(0, remaining_s)

            lines.append(
                f"**{role.mention}** (ID: `{role.id}`)\n"
                f"  Expires <t:{exp_ts}:R>  —  <t:{exp_ts}:F>"
            )

        embed = _info_embed(
            f"Active Temporary Roles — {len(live)}",
            "\n\n".join(lines),
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}  •  Aura")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /temp-role cancel ─────────────────────────────────────────────────────

    @temp_role_group.command(
        name="cancel",
        description="Immediately delete a temporary role before it expires.",
    )
    @app_commands.describe(role="The temporary role to cancel and delete")
    async def temp_role_cancel(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        # Check it's actually registered as a temp role
        records  = _load_guild_temp_roles(interaction.guild.id)
        is_temp  = any(r["role_id"] == role.id for r in records)

        if not is_temp:
            await interaction.response.send_message(
                embed=_warn_embed(
                    "Not a Temporary Role",
                    f"{role.mention} is not tracked as a temporary role.\n"
                    "If you want to delete a permanent role, use `/role delete` instead.",
                ),
                ephemeral=True,
            )
            return

        role_name = role.name
        try:
            await role.delete(reason=f"Temporary role cancelled early by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Permission Denied",
                    "I don't have permission to delete that role.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )
            return

        _remove_temp_role(role.id)

        await interaction.response.send_message(
            embed=_success_embed(
                "Temporary Role Cancelled",
                f"The temporary role **{role_name}** has been deleted early.",
            ),
            ephemeral=True,
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    """Entry point called by bot.load_extension('role_cog')."""
    init_role_db()
    cog = RoleCog(bot)
    await bot.add_cog(cog)
    cog._expiry_loop.start()
    print("🎭 RoleCog loaded")
