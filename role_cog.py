"""
role_cog.py — Full /role command suite for Aura moderation bot.

Sub-commands
────────────
  /role create   – Create a role with optional position (above / below another role)
  /role delete   – Delete a role (with hierarchy + permission check)
  /role add      – Assign a role to a member
  /role remove   – Remove a role from a member
  /role edit     – Edit a role's attributes; shows an interactive button / modal UI
                   when no optional arguments are provided
  /role reset    – Reset a role to defaults (bot-created OR top-5-role user check)
  /role info     – Show a detailed role info embed with a solid-color thumbnail

All responses are ephemeral by default so only the invoking moderator sees them.
"""

from __future__ import annotations

import datetime
import io
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ── Database ──────────────────────────────────────────────────────────────────

DB_FILE = "bot_data.db"


def init_role_db() -> None:
    """Create the bot_created_roles table if it does not yet exist."""
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


# ── Miscellaneous helpers ─────────────────────────────────────────────────────

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
    Parse a hex string (#RRGGBB / RRGGBB) or a named colour into discord.Color.
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
    guild = interaction.guild
    bot_top = guild.me.top_role

    if bot_top <= target_role:
        await interaction.response.send_message(
            embed=_error_embed(
                "Hierarchy Error",
                f"My highest role (**{bot_top.mention}**) is not above {target_role.mention}.\n"
                "I can't manage a role that sits at or above my own position in the hierarchy.",
            ),
            ephemeral=True,
        )
        return False

    user_top = interaction.user.top_role
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
    """
    Same hierarchy check but for use AFTER the interaction has been deferred.
    Uses interaction.followup instead of interaction.response.
    """
    guild = interaction.guild
    bot_top = guild.me.top_role

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

    user_top = interaction.user.top_role
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
    Move `role` to `target_position` using guild.edit_role_positions().
    Discord counts positions from the bottom (0 = @everyone).

    Returns None on success, or a human-readable error string on failure.
    """
    bot_top = guild.me.top_role
    max_pos = bot_top.position - 1

    if target_position > max_pos:
        return (
            f"Position **{target_position}** is at or above my highest role "
            f"(**{bot_top.mention}**, position {bot_top.position}). "
            "I can only place roles below my own position."
        )

    target_position = max(1, target_position)  # never move below @everyone

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
    """Generic single-field modal used by the RoleEditView buttons."""

    answer = discord.ui.TextInput(label="New value", max_length=100)

    def __init__(
        self,
        title: str,
        label: str,
        placeholder: str,
        callback_fn,
    ) -> None:
        super().__init__(title=title)
        self.answer.label = label
        self.answer.placeholder = placeholder
        self._callback_fn = callback_fn

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback_fn(interaction, self.answer.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            embed=_error_embed("Modal Error", f"Something went wrong: {error}"),
            ephemeral=True,
        )


class RoleEditView(discord.ui.View):
    """
    Interactive button panel shown when /role edit is called without optional args.
    Each button opens a modal for that specific attribute.
    """

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
                    embed=_error_embed("API Error", str(exc)),
                    ephemeral=True,
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
                        "Couldn't recognise that color. Try a hex code like `#FF5733` "
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
                    embed=_error_embed("API Error", str(exc)),
                    ephemeral=True,
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
                    embed=_error_embed(
                        "Invalid Value",
                        "Please enter `true` or `false` (also accepts `yes`/`no` or `1`/`0`).",
                    ),
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
                    embed=_error_embed("API Error", str(exc)),
                    ephemeral=True,
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
                    embed=_error_embed(
                        "Invalid Value",
                        "Please enter `true` or `false` (also accepts `yes`/`no` or `1`/`0`).",
                    ),
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
                    embed=_error_embed("API Error", str(exc)),
                    ephemeral=True,
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
                    embed=_error_embed("Hierarchy Error", err),
                    ephemeral=True,
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
    """All /role sub-commands for Aura."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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

        # Validate name
        name = name.strip()
        if not name:
            await interaction.followup.send(
                embed=_error_embed("Invalid Name", "The role name cannot be empty."),
                ephemeral=True,
            )
            return

        # Validate color
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

        # Validate position args
        if above_role and below_role:
            await interaction.followup.send(
                embed=_warn_embed(
                    "Conflicting Options",
                    "Please provide **either** `above_role` or `below_role`, not both.",
                ),
                ephemeral=True,
            )
            return

        # Determine target position and validate hierarchy
        target_position: int | None = None
        if above_role:
            if not await _hierarchy_check_deferred(interaction, above_role):
                return
            target_position = above_role.position + 1
        elif below_role:
            if not await _hierarchy_check_deferred(interaction, below_role):
                return
            target_position = max(1, below_role.position)

        # Make sure requested position is within bot's reach
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

        # Create the role
        try:
            new_role = await guild.create_role(
                name=name,
                color=parsed_color,
                hoist=hoist,
                mentionable=mentionable,
                reason=f"Created via /role create by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_error_embed(
                    "Permission Denied",
                    "I don't have permission to create roles in this server. "
                    "Please make sure I have the **Manage Roles** permission.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )
            return

        # Position the role if requested
        pos_err: str | None = None
        if target_position is not None:
            pos_err = await _move_role(
                guild, new_role, target_position,
                reason=f"Positioned via /role create by {interaction.user}",
            )

        # Track as bot-created in the database
        register_bot_role(new_role, str(interaction.user.id))

        lines = [
            f"{new_role.mention} has been created.",
            f"• **Color** → `#{parsed_color.value:06X}`",
            f"• **Hoisted** → `{hoist}`",
            f"• **Mentionable** → `{mentionable}`",
        ]
        if target_position is not None:
            if pos_err:
                lines.append(f"• **Position** → ⚠️ Could not set — {pos_err}")
            else:
                lines.append(f"• **Position** → `{new_role.position}` (as requested)")

        embed_fn = _warn_embed if pos_err else _success_embed
        title = "Role Created (Position Issue)" if pos_err else "Role Created"
        await interaction.followup.send(
            embed=embed_fn(title, "\n".join(lines)),
            ephemeral=True,
        )

    # ── /role delete ──────────────────────────────────────────────────────────

    @role_group.command(name="delete", description="Permanently delete a role from this server.")
    @app_commands.describe(
        role="The role you want to delete",
        reason="Reason for deletion (recorded in the audit log)",
    )
    async def role_delete(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str | None = None,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        # Prevent deleting managed roles (bot / integration roles)
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
        role_name = role.name  # save before it's gone

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
    @app_commands.describe(
        user="The member to receive the role",
        role="The role to assign",
    )
    async def role_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: discord.Role,
    ) -> None:
        if not await _hierarchy_check(interaction, role):
            return

        # Prevent assigning roles at or above the executor's own top role
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
                embed=_warn_embed(
                    "Already Assigned",
                    f"{user.mention} already has the {role.mention} role.",
                ),
                ephemeral=True,
            )
            return

        try:
            await user.add_roles(role, reason=f"Assigned via /role add by {interaction.user}")
            await interaction.response.send_message(
                embed=_success_embed(
                    "Role Assigned",
                    f"{role.mention} has been given to {user.mention}.",
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Permission Denied",
                    f"I couldn't assign {role.mention}. It may be higher than my top role.",
                ),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=_error_embed("API Error", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )

    # ── /role remove ──────────────────────────────────────────────────────────

    @role_group.command(name="remove", description="Remove a role from a server member.")
    @app_commands.describe(
        user="The member to remove the role from",
        role="The role to remove",
    )
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
                embed=_warn_embed(
                    "Role Not Found",
                    f"{user.mention} doesn't currently have the {role.mention} role.",
                ),
                ephemeral=True,
            )
            return

        try:
            await user.remove_roles(role, reason=f"Removed via /role remove by {interaction.user}")
            await interaction.response.send_message(
                embed=_success_embed(
                    "Role Removed",
                    f"{role.mention} has been removed from {user.mention}.",
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=_error_embed(
                    "Permission Denied",
                    f"I couldn't remove {role.mention}. It may be higher than my top role.",
                ),
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
        name="New name for the role",
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

        # No args → show interactive button panel
        if not any_provided:
            embed = discord.Embed(
                title=f"✏️  Edit Role — {role.name}",
                description=(
                    f"Click a button below to edit an attribute of {role.mention}.\n"
                    "Each button will open a small pop-up where you can enter the new value.\n\n"
                    "This panel expires after **2 minutes**."
                ),
                color=discord.Color.blurple(),
                timestamp=_now(),
            )
            await interaction.response.send_message(embed=embed, view=RoleEditView(role), ephemeral=True)
            return

        # Validate color before deferring so we can still send a response
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
                embed=_warn_embed(
                    "Conflicting Options",
                    "Please provide **either** `above_role` or `below_role`, not both.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
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

        # Determine new position
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

        # Apply attribute changes
        if changes:
            try:
                await role.edit(
                    reason=f"Edited via /role edit by {interaction.user}",
                    **changes,
                )
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

        # Apply position change separately
        pos_err: str | None = None
        if target_position is not None:
            pos_err = await _move_role(
                guild, role, target_position,
                reason=f"Position changed via /role edit by {interaction.user}",
            )

        desc_lines = [f"{role.mention} has been updated:"]
        if "name" in changes:
            desc_lines.append(f"**Name →** {changes['name']}")
        if "color" in changes:
            desc_lines.append(f"**Color →** `{color}`")
        if "hoist" in changes:
            desc_lines.append(f"**Hoist →** {changes['hoist']}")
        if "mentionable" in changes:
            desc_lines.append(f"**Mentionable →** {changes['mentionable']}")
        if target_position is not None:
            if pos_err:
                desc_lines.append(f"**Position →** ⚠️ Failed — {pos_err}")
            else:
                desc_lines.append(f"**Position →** {target_position}")

        embed_fn = _warn_embed if pos_err else _success_embed
        title = "Role Partially Updated" if pos_err else "Role Updated"
        await interaction.followup.send(
            embed=embed_fn(title, "\n".join(desc_lines)),
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

        guild = interaction.guild

        # Allow if (a) Aura originally created this role OR (b) the user holds a top-5 role
        bot_created = is_bot_created_role(role.id)
        user_in_top5 = False

        if not bot_created:
            sorted_roles = sorted(
                [r for r in guild.roles if r.id != guild.default_role.id],
                key=lambda r: r.position,
                reverse=True,
            )
            top5_ids = {r.id for r in sorted_roles[:5]}
            member = interaction.user  # type: discord.Member
            user_in_top5 = any(r.id in top5_ids for r in member.roles)

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

        everyone_perms = guild.default_role.permissions
        try:
            await role.edit(
                color=discord.Color.default(),
                permissions=everyone_perms,
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

        # Count members that hold this role
        member_count = sum(1 for m in guild.members if role in m.roles)

        # List enabled permissions
        allowed_perms = [
            perm.replace("_", " ").title()
            for perm, value in role.permissions
            if value
        ]
        perms_str = ", ".join(allowed_perms) if allowed_perms else "No special permissions"

        created_ts = int(role.created_at.timestamp())

        # Determine role type label
        if role.managed:
            role_type = "🤖 Managed / Integration Role"
        elif role.id == guild.default_role.id:
            role_type = "👥 Default Role (@everyone)"
        elif role.color.value == 0:
            role_type = "🌈 Possible Gradient / No-Color Role"
        else:
            role_type = "🎨 Standard Color Role"

        embed = discord.Embed(
            title=f"🏷️  Role Info — {role.name}",
            description=role_type,
            color=role.color if role.color.value != 0 else discord.Color.greyple(),
            timestamp=_now(),
        )
        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Members", value=str(member_count), inline=True)
        embed.add_field(name="Color", value=f"`#{role.color.value:06X}`", inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Managed", value="Yes" if role.managed else "No", inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)
        embed.add_field(name="Created", value=f"<t:{created_ts}:F>", inline=True)
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


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    """Entry point called by bot.load_extension('role_cog')."""
    init_role_db()
    await bot.add_cog(RoleCog(bot))
    print("🎭 RoleCog loaded")
