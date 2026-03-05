"""
server_cog.py — Server channel management commands for Aura.

Required permissions to use any command: Manage Channels + Manage Roles
(Both are needed — Manage Channels to edit overwrites, Manage Roles to touch
role-based overwrites.)

Hierarchy rule: The executor cannot lock/hide channels for a role or member
whose top role is equal to or higher than the executor's own top role.
The server owner is exempt from this check.

Commands
────────
  /lock      [channel] [target]   — Disable messaging for everyone or a target
  /unlock    [channel] [target]   — Restore messaging for everyone or a target
  /hide      [channel] [target]   — Remove view_channel for everyone or a target
  /unhide    [channel] [target]   — Restore view_channel for everyone or a target
  /lockall   [except]  [target]   — Lock every channel, optionally skipping some
  /unlockall [except]  [target]   — Unlock every channel, optionally skipping some
  /hideall   [except]  [target]   — Hide every channel, optionally skipping some
  /unhideall [except]  [target]   — Unhide every channel, optionally skipping some

The `except_channels` parameter accepts a comma-separated list of channel IDs,
e.g. "123456789,987654321".
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Union

import discord
from discord import app_commands
from discord.ext import commands


# ── Permission constants ───────────────────────────────────────────────────────

# Permissions toggled by lock / unlock
LOCK_PERMS: dict[str, bool | None] = {
    "send_messages":            False,
    "send_messages_in_threads": False,
    "create_public_threads":    False,
    "create_private_threads":   False,
    "add_reactions":            False,
}
UNLOCK_PERMS: dict[str, bool | None] = {
    "send_messages":            None,   # None = reset to inherit from category/server
    "send_messages_in_threads": None,
    "create_public_threads":    None,
    "create_private_threads":   None,
    "add_reactions":            None,
}

# Permissions toggled by hide / unhide
HIDE_PERMS:   dict[str, bool | None] = {"view_channel": False}
UNHIDE_PERMS: dict[str, bool | None] = {"view_channel": None}

# Type alias for a role-or-member target
Target = Union[discord.Role, discord.Member, None]


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _ts() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _ok(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"✅  {title}", description=desc,
        color=discord.Color.green(), timestamp=_ts(),
    )


def _err(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌  {title}", description=desc,
        color=discord.Color.red(), timestamp=_ts(),
    )


def _warn(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"⚠️  {title}", description=desc,
        color=discord.Color.yellow(), timestamp=_ts(),
    )


def _info(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"🔒  {title}", description=desc,
        color=discord.Color.blurple(), timestamp=_ts(),
    )


# ── Shared logic helpers ───────────────────────────────────────────────────────

def _parse_except(raw: str | None) -> set[int]:
    """
    Parse a comma-separated string of channel IDs into a set of ints.
    Non-numeric tokens are silently ignored.
    """
    if not raw:
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def _effective_target(
    guild: discord.Guild,
    target: Target,
) -> discord.Role | discord.Member:
    """Return the target, falling back to @everyone if none was specified."""
    return target if target is not None else guild.default_role


def _executor_can_target(
    interaction: discord.Interaction,
    target: discord.Role | discord.Member,
) -> bool:
    """
    Return True if the executor's top role is strictly above the target's top role.
    The server owner always passes this check.
    """
    if interaction.user.id == interaction.guild.owner_id:
        return True

    executor_top = interaction.user.top_role
    target_top = target if isinstance(target, discord.Role) else target.top_role
    return executor_top > target_top


def _target_label(target: discord.Role | discord.Member) -> str:
    """Return a Discord mention string for the target."""
    return target.mention


async def _apply_overwrite(
    channel: discord.abc.GuildChannel,
    target: discord.Role | discord.Member,
    perms: dict[str, bool | None],
    reason: str,
) -> str | None:
    """
    Apply a permission overwrite to a channel for the given target.
    Returns None on success, or a human-readable error string on failure.
    """
    try:
        existing = channel.overwrites_for(target)
        for perm, value in perms.items():
            setattr(existing, perm, value)
        await channel.set_permissions(target, overwrite=existing, reason=reason)
        return None
    except discord.Forbidden:
        return f"No permission to edit {channel.mention}."
    except discord.HTTPException as exc:
        return f"API error on {channel.mention}: {exc}"


async def _bulk_apply(
    guild: discord.Guild,
    target: discord.Role | discord.Member,
    perms: dict[str, bool | None],
    excluded_ids: set[int],
    reason: str,
    delay: float = 0.35,
) -> tuple[int, int, list[str]]:
    """
    Apply `perms` overwrite to every text / voice / forum / stage channel in the guild,
    skipping channels whose IDs are in `excluded_ids`.

    Returns (success_count, skipped_count, error_messages).
    The 0.35 s delay between API calls keeps us safely under Discord's rate limits.
    """
    success = 0
    errors: list[str] = []

    channels = [
        c for c in guild.channels
        if isinstance(c, (
            discord.TextChannel,
            discord.VoiceChannel,
            discord.ForumChannel,
            discord.StageChannel,
        ))
        and c.id not in excluded_ids
    ]

    for ch in channels:
        err = await _apply_overwrite(ch, target, perms, reason)
        if err:
            errors.append(err)
        else:
            success += 1
        await asyncio.sleep(delay)

    skipped = len(excluded_ids)
    return success, skipped, errors


def _bulk_result_embed(
    action_ok: str,
    action_warn: str,
    success: int,
    skipped: int,
    errors: list[str],
    target: discord.Role | discord.Member,
) -> discord.Embed:
    """Build a consistent embed for bulk operations."""
    desc = (
        f"**{success}** channel(s) affected for {_target_label(target)}.\n"
        f"**{skipped}** channel(s) skipped."
    )
    if errors:
        shown = errors[:5]
        desc += f"\n\n⚠️ **{len(errors)} error(s):**\n" + "\n".join(shown)
        if len(errors) > 5:
            desc += f"\n…and {len(errors) - 5} more."

    if errors:
        return _warn(action_warn, desc)
    return _ok(action_ok, desc)


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerCog(commands.Cog, name="Server Management"):
    """Channel lock, unlock, hide, and unhide commands for Aura."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Shared pre-checks ─────────────────────────────────────────────────────

    async def _perm_check(self, interaction: discord.Interaction) -> bool:
        """
        Gate: the invoking user must have Manage Channels AND Manage Roles.
        The server owner always passes.
        """
        member = interaction.user
        if member.id == interaction.guild.owner_id:
            return True

        perms = interaction.channel.permissions_for(member)
        if perms.manage_channels and perms.manage_roles:
            return True

        await interaction.response.send_message(
            embed=_err(
                "Missing Permissions",
                "You need both **Manage Channels** and **Manage Roles** to use this command.",
            ),
            ephemeral=True,
        )
        return False

    async def _target_check(
        self,
        interaction: discord.Interaction,
        target: discord.Role | discord.Member,
    ) -> bool:
        """
        Gate: the executor can't act on roles / members at or above their own top role.
        """
        if _executor_can_target(interaction, target):
            return True

        label = target.mention if hasattr(target, "mention") else str(target)
        await interaction.response.send_message(
            embed=_err(
                "Hierarchy Error",
                f"You can't perform this action on {label} — their role is at or above yours.\n"
                f"Your highest role: **{interaction.user.top_role.mention}**",
            ),
            ephemeral=True,
        )
        return False

    async def _target_check_deferred(
        self,
        interaction: discord.Interaction,
        target: discord.Role | discord.Member,
    ) -> bool:
        """
        Same hierarchy check but for use AFTER the interaction has been deferred.
        Uses interaction.followup instead of interaction.response.
        """
        if _executor_can_target(interaction, target):
            return True

        label = target.mention if hasattr(target, "mention") else str(target)
        await interaction.followup.send(
            embed=_err(
                "Hierarchy Error",
                f"You can't perform this action on {label} — their role is at or above yours.\n"
                f"Your highest role: **{interaction.user.top_role.mention}**",
            ),
            ephemeral=True,
        )
        return False

    # ── /lock ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="lock",
        description="Lock a channel — disables messaging for everyone or a specific role/member.",
    )
    @app_commands.describe(
        channel="Channel to lock (defaults to the current channel)",
        target="Role or member to lock for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        ch = channel or interaction.channel
        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        reason = f"Locked by {interaction.user} ({interaction.user.id})"
        err = await _apply_overwrite(ch, effective, LOCK_PERMS, reason)

        if err:
            await interaction.response.send_message(
                embed=_err("Lock Failed", f"Couldn't lock {ch.mention}:\n{err}"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=_ok(
                    "Channel Locked",
                    f"{ch.mention} is now **locked** for {_target_label(effective)}.\n"
                    "Disabled: `send_messages`, `send_messages_in_threads`, "
                    "`create_public_threads`, `create_private_threads`, `add_reactions`.",
                )
            )

    # ── /unlock ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unlock",
        description="Unlock a channel — restores messaging permissions for everyone or a role/member.",
    )
    @app_commands.describe(
        channel="Channel to unlock (defaults to the current channel)",
        target="Role or member to unlock for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        ch = channel or interaction.channel
        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        reason = f"Unlocked by {interaction.user} ({interaction.user.id})"
        err = await _apply_overwrite(ch, effective, UNLOCK_PERMS, reason)

        if err:
            await interaction.response.send_message(
                embed=_err("Unlock Failed", f"Couldn't unlock {ch.mention}:\n{err}"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=_ok(
                    "Channel Unlocked",
                    f"{ch.mention} is now **unlocked** for {_target_label(effective)}.\n"
                    "Messaging permission overwrites have been reset to inherit.",
                )
            )

    # ── /hide ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="hide",
        description="Hide a channel — removes view access for everyone or a role/member.",
    )
    @app_commands.describe(
        channel="Channel to hide (defaults to the current channel)",
        target="Role or member to hide the channel from (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def hide(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        ch = channel or interaction.channel
        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        reason = f"Hidden by {interaction.user} ({interaction.user.id})"
        err = await _apply_overwrite(ch, effective, HIDE_PERMS, reason)

        if err:
            await interaction.response.send_message(
                embed=_err("Hide Failed", f"Couldn't hide {ch.mention}:\n{err}"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=_ok(
                    "Channel Hidden",
                    f"{ch.mention} is now **hidden** from {_target_label(effective)}.\n"
                    "Removed: `view_channel`.",
                )
            )

    # ── /unhide ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unhide",
        description="Unhide a channel — restores view access for everyone or a role/member.",
    )
    @app_commands.describe(
        channel="Channel to unhide (defaults to the current channel)",
        target="Role or member to restore access for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def unhide(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        ch = channel or interaction.channel
        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        reason = f"Unhidden by {interaction.user} ({interaction.user.id})"
        err = await _apply_overwrite(ch, effective, UNHIDE_PERMS, reason)

        if err:
            await interaction.response.send_message(
                embed=_err("Unhide Failed", f"Couldn't unhide {ch.mention}:\n{err}"),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=_ok(
                    "Channel Unhidden",
                    f"{ch.mention} is now **visible** to {_target_label(effective)}.\n"
                    "`view_channel` overwrite has been reset to inherit.",
                )
            )

    # ── /lockall ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="lockall",
        description="Lock every channel in the server for everyone or a role/member.",
    )
    @app_commands.describe(
        except_channels="Comma-separated channel IDs to skip, e.g. 123456,789012",
        target="Role or member to lock for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def lockall(
        self,
        interaction: discord.Interaction,
        except_channels: str | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        excluded = _parse_except(except_channels)
        await interaction.response.defer()

        reason = f"Lockall by {interaction.user} ({interaction.user.id})"
        success, skipped, errors = await _bulk_apply(
            interaction.guild, effective, LOCK_PERMS, excluded, reason,
        )

        await interaction.followup.send(
            embed=_bulk_result_embed(
                "Server Locked", "Server Locked (with errors)",
                success, skipped, errors, effective,
            )
        )

    # ── /unlockall ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unlockall",
        description="Unlock every channel in the server for everyone or a role/member.",
    )
    @app_commands.describe(
        except_channels="Comma-separated channel IDs to skip",
        target="Role or member to unlock for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def unlockall(
        self,
        interaction: discord.Interaction,
        except_channels: str | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        excluded = _parse_except(except_channels)
        await interaction.response.defer()

        reason = f"Unlockall by {interaction.user} ({interaction.user.id})"
        success, skipped, errors = await _bulk_apply(
            interaction.guild, effective, UNLOCK_PERMS, excluded, reason,
        )

        await interaction.followup.send(
            embed=_bulk_result_embed(
                "Server Unlocked", "Server Unlocked (with errors)",
                success, skipped, errors, effective,
            )
        )

    # ── /hideall ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="hideall",
        description="Hide every channel in the server from everyone or a role/member.",
    )
    @app_commands.describe(
        except_channels="Comma-separated channel IDs to skip",
        target="Role or member to hide channels from (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def hideall(
        self,
        interaction: discord.Interaction,
        except_channels: str | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        excluded = _parse_except(except_channels)
        await interaction.response.defer()

        reason = f"Hideall by {interaction.user} ({interaction.user.id})"
        success, skipped, errors = await _bulk_apply(
            interaction.guild, effective, HIDE_PERMS, excluded, reason,
        )

        await interaction.followup.send(
            embed=_bulk_result_embed(
                "Channels Hidden", "Channels Hidden (with errors)",
                success, skipped, errors, effective,
            )
        )

    # ── /unhideall ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unhideall",
        description="Unhide every channel in the server for everyone or a role/member.",
    )
    @app_commands.describe(
        except_channels="Comma-separated channel IDs to skip",
        target="Role or member to restore visibility for (defaults to @everyone)",
    )
    @app_commands.guild_only()
    async def unhideall(
        self,
        interaction: discord.Interaction,
        except_channels: str | None = None,
        target: discord.Role | discord.Member | None = None,
    ) -> None:
        if not await self._perm_check(interaction):
            return

        effective = _effective_target(interaction.guild, target)

        if not await self._target_check(interaction, effective):
            return

        excluded = _parse_except(except_channels)
        await interaction.response.defer()

        reason = f"Unhideall by {interaction.user} ({interaction.user.id})"
        success, skipped, errors = await _bulk_apply(
            interaction.guild, effective, UNHIDE_PERMS, excluded, reason,
        )

        await interaction.followup.send(
            embed=_bulk_result_embed(
                "Channels Unhidden", "Channels Unhidden (with errors)",
                success, skipped, errors, effective,
            )
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerCog(bot))
    print("🛡️  ServerCog loaded")
