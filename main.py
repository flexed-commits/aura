"""
main.py — Entry point for Aura, a professional Discord moderation bot.

Setup
─────
  1. Copy .env.example → .env and fill in your DISCORD_TOKEN_AURA
  2. Install dependencies:
       pip install -U discord.py python-dotenv Pillow
  3. Run:
       python main.py

Loaded cogs
───────────
  • owner.py      — /eval · /console  (owner / team-member restricted)
  • role_cog.py   — /role create · delete · add · remove · edit · reset · info
  • server_cog.py — /lock · /unlock · /hide · /unhide
                    /lockall · /unlockall · /hideall · /unhideall

Owner detection
───────────────
  On startup, Aura fetches its own application info from Discord and populates
  bot.owner_ids with the correct set of authorised users:

    • Solo application  → the single owner's ID.
    • Team application  → every team member whose role is ADMIN.
                          Falls back to all team members if no ADMIN role exists
                          (older team setups) so the bot is never locked out.

  This means /eval and /console work correctly regardless of whether the bot
  is owned by an individual or shared inside a Discord Developer Team.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; set DISCORD_TOKEN as a real environment variable


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("aura.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("aura")


# ── Cog list ──────────────────────────────────────────────────────────────────

EXTENSIONS: list[str] = [
    "owner",        # /eval, /console  — load first so owners can debug other cogs
    "role_cog",     # /role suite
    "server_cog",   # /lock /unlock /hide /unhide + bulk variants
]


# ── Bot ───────────────────────────────────────────────────────────────────────

class Aura(commands.Bot):
    """Aura — Professional Discord Moderation Bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # required: role assignment, member lookups
        intents.message_content = True  # future-proofing for prefix / log commands

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            description="Aura — Professional Discord Moderation Bot",
        )

    # ── Startup ───────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """
        Called once before the WebSocket connects.
        Resolves ownership, loads cogs, then syncs slash commands globally.
        """
        await self._resolve_owners()
        await self._load_extensions()

        log.info("Syncing application commands globally…")
        try:
            synced = await self.tree.sync()
            log.info("  ✔  Synced %d slash command(s).", len(synced))
        except discord.HTTPException as exc:
            log.error("  ✘  Command sync failed: %s", exc)

    async def _resolve_owners(self) -> None:
        """
        Fetch application info and populate bot.owner_ids so that
        commands.Bot.is_owner() works correctly for both solo and team bots.

        Team handling
        ─────────────
        Discord team members each have a `role` field (TeamMemberRole enum).
        We grant owner access to members whose role is ADMIN.
        If for any reason the role field is absent or the team has no admins
        (shouldn't happen in practice), we fall back to ALL team members so
        the bot is never locked out.
        """
        log.info("Resolving application ownership…")
        try:
            app_info = await self.application_info()
        except discord.HTTPException as exc:
            log.error("  ✘  Could not fetch application info: %s", exc)
            return

        if app_info.team:
            # ── Team-owned application ────────────────────────────────────
            team = app_info.team
            log.info("  ℹ  Application owned by team: %s (ID: %s)", team.name, team.id)

            # TeamMemberRole.admin was added in discord.py 2.3; guard for older builds
            try:
                admin_role = discord.TeamMemberRole.admin
                admin_ids = {
                    m.id for m in team.members
                    if m.role == admin_role
                }
            except AttributeError:
                admin_ids = set()

            # Fallback: if no admins found, authorise all team members
            if not admin_ids:
                admin_ids = {m.id for m in team.members}
                log.warning(
                    "  ⚠  No ADMIN team members found — "
                    "granting owner access to all %d team member(s).",
                    len(admin_ids),
                )

            self.owner_ids = admin_ids
            log.info(
                "  ✔  Owner IDs set to %d team admin(s): %s",
                len(admin_ids),
                ", ".join(str(i) for i in admin_ids),
            )

        else:
            # ── Solo-owned application ────────────────────────────────────
            owner = app_info.owner
            self.owner_id = owner.id
            log.info("  ✔  Owner set to: %s (ID: %s)", owner, owner.id)

    async def _load_extensions(self) -> None:
        log.info("Loading extensions…")
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("  ✔  Loaded  : %s", ext)
            except commands.ExtensionNotFound:
                log.error(
                    "  ✘  Not found : %s  — make sure the file exists next to main.py.", ext
                )
            except commands.ExtensionAlreadyLoaded:
                log.warning("  ⚠  Already loaded : %s", ext)
            except commands.NoEntryPointError:
                log.error("  ✘  No setup() function found in : %s", ext)
            except Exception as exc:  # noqa: BLE001
                log.error("  ✘  Failed to load %s: %s", ext, exc, exc_info=True)

    # ── Ready ─────────────────────────────────────────────────────────────────

    async def on_ready(self) -> None:
        log.info("━" * 56)
        log.info("  ✨  Aura is online!")
        log.info("  User     : %s  (ID: %s)", self.user, self.user.id)
        log.info("  Guilds   : %d", len(self.guilds))
        log.info("  d.py ver : %s", discord.__version__)
        if self.owner_ids:
            log.info("  Owners   : %s", ", ".join(str(i) for i in self.owner_ids))
        elif self.owner_id:
            log.info("  Owner    : %s", self.owner_id)
        log.info("━" * 56)

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="over the server ✦",
            ),
            status=discord.Status.online,
        )

    # ── Global slash-command error handler ────────────────────────────────────

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """
        Catch any unhandled slash-command error and reply with a clear,
        human-readable embed so users always know what went wrong.
        """
        embed = discord.Embed(
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Aura Moderation Bot")

        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            embed.title = "❌  Missing Permissions"
            missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
            embed.description = (
                f"You don't have the required permissions to run this command.\n"
                f"**Required:** {missing}"
            )

        elif isinstance(error, app_commands.BotMissingPermissions):
            embed.title = "❌  I'm Missing Permissions"
            missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
            embed.description = (
                f"I don't have all the permissions I need to do that.\n"
                f"**Missing:** {missing}\n\n"
                "Please ask a server administrator to update my role permissions."
            )

        elif isinstance(error, app_commands.CommandOnCooldown):
            embed.title = "⏳  Slow Down!"
            embed.color = discord.Color.orange()
            embed.description = (
                f"This command is on cooldown.\n"
                f"Please wait **{error.retry_after:.1f} second(s)** and try again."
            )

        elif isinstance(error, app_commands.NoPrivateMessage):
            embed.title = "🚫  Server Only"
            embed.description = "This command can only be used inside a server, not in DMs."

        elif isinstance(error, app_commands.CommandNotFound):
            embed.title = "❓  Unknown Command"
            embed.description = (
                "That command doesn't seem to exist. Use `/` to browse available commands."
            )

        elif isinstance(error, app_commands.TransformerError):
            embed.title = "⚠️  Invalid Argument"
            embed.color = discord.Color.yellow()
            embed.description = (
                f"One of the values you entered wasn't valid or recognised.\n"
                f"**Details:** {error}"
            )

        elif isinstance(error, app_commands.CheckFailure):
            embed.title = "🚫  Access Denied"
            embed.description = "You don't have permission to use this command."

        elif isinstance(original, discord.Forbidden):
            embed.title = "🔒  Forbidden"
            embed.description = (
                "Discord denied my request to perform that action.\n"
                "Please make sure my role is positioned high enough in the role hierarchy."
            )

        elif isinstance(original, discord.HTTPException):
            embed.title = "🌐  Discord API Error"
            embed.description = (
                "Discord returned an error while processing the request.\n"
                f"**Code:** `{original.status}` — "
                f"{original.text or 'No further details were provided.'}"
            )

        else:
            embed.title = "💥  Unexpected Error"
            embed.description = (
                "Something went wrong while running that command.\n"
                "Please try again in a moment. If this keeps happening, "
                "contact a server administrator."
            )
            cmd_name = interaction.command.name if interaction.command else "unknown"
            log.error(
                "Unhandled error in /%s used by %s: %s",
                cmd_name,
                interaction.user,
                error,
                exc_info=error,
            )

        # Send the embed — handle both deferred and non-deferred interactions
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass  # Interaction expired or channel is inaccessible

    # ── Guild logging ─────────────────────────────────────────────────────────

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info(
            "Joined guild  : %s  (ID: %s, Members: %d)",
            guild.name, guild.id, guild.member_count,
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Left guild    : %s  (ID: %s)", guild.name, guild.id)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    token = os.getenv("DISCORD_TOKEN_AURA", "").strip()
    if not token:
        log.critical(
            "DISCORD_TOKEN_AURA is not set!\n"
            "  • Create a .env file in this directory.\n"
            "  • Add this line:  DISCORD_TOKEN=your_bot_token_here\n"
            "  • Then restart Aura."
        )
        sys.exit(1)

    async with Aura() as bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested — Aura is going offline. Goodbye! 👋")
