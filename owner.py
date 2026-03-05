"""
owner.py — Restricted owner-only commands for Aura.

Owner detection
───────────────
  • If the application belongs to a single user  → that user is the owner.
  • If the application belongs to a Discord Team → every team member with the
    ADMIN role (or any member if the team has no role concept) is treated as an
    owner.  This is resolved at startup by fetching the application info and
    caching the authorised IDs inside the bot instance.

Commands
────────
  /eval    — Execute a shell command and display stdout / stderr inline.
             Output is capped at Discord's embed limits; excess is shown as a
             file attachment so nothing is silently dropped.
  /console — Display the last 50 lines of aura.log directly in Discord.
             Falls back gracefully if the log file is missing or empty.

All responses are ephemeral and restricted to authorised owners only.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

# Maximum characters shown inline before the output is attached as a file
_INLINE_LIMIT = 3800

# Shell-command timeout in seconds
_EVAL_TIMEOUT = 30

# Log file written by main.py
_LOG_FILE = "aura.log"

# How many tail lines to show in /console
_CONSOLE_LINES = 50


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _ts() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _err(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌  {title}", description=desc,
        color=discord.Color.red(), timestamp=_ts(),
    )


def _ok(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"✅  {title}", description=desc,
        color=discord.Color.green(), timestamp=_ts(),
    )


def _info(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"🖥️  {title}", description=desc,
        color=discord.Color.blurple(), timestamp=_ts(),
    )


def _warn(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"⚠️  {title}", description=desc,
        color=discord.Color.yellow(), timestamp=_ts(),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _code_block(text: str, lang: str = "ansi") -> str:
    """Wrap text in a Discord code block."""
    return f"```{lang}\n{text}\n```"


def _tail(path: str, n: int) -> list[str]:
    """
    Return the last `n` lines of the file at `path`.
    Returns an empty list if the file is absent or unreadable.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except (FileNotFoundError, OSError):
        return []


# ── Cog ───────────────────────────────────────────────────────────────────────

class OwnerCog(commands.Cog, name="Owner"):
    """Owner-restricted utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Owner gate ────────────────────────────────────────────────────────────

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        """
        Verify the invoking user is an authorised owner.

        discord.py's built-in Bot.is_owner() already handles both the
        single-owner case and the team-member case, provided the bot's
        owner_ids / owner_id have been populated at startup (see main.py).
        """
        if await self.bot.is_owner(interaction.user):
            return True

        await interaction.response.send_message(
            embed=_err(
                "Access Denied",
                "This command is reserved for Aura's owner(s) only.\n"
                "If you believe this is a mistake, please contact the bot developer.",
            ),
            ephemeral=True,
        )
        return False

    # ── /eval ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="eval",
        description="[OWNER] Execute a shell command on the host machine.",
    )
    @app_commands.describe(
        command="The shell command to run (e.g. ls -la, pip list, echo hello)",
    )
    async def eval_cmd(
        self,
        interaction: discord.Interaction,
        command: str,
    ) -> None:
        if not await self._owner_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        start = time.perf_counter()
        timed_out = False
        stdout_text = ""
        stderr_text = ""
        return_code: int | None = None

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                raw_out, raw_err = await asyncio.wait_for(
                    proc.communicate(), timeout=_EVAL_TIMEOUT
                )
                return_code = proc.returncode
                stdout_text = raw_out.decode("utf-8", errors="replace").strip()
                stderr_text = raw_err.decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                proc.kill()
                timed_out = True
                return_code = -1

        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - start
            await interaction.followup.send(
                embed=_err(
                    "Execution Failed",
                    f"Could not start the subprocess.\n**Error:** `{exc}`\n"
                    f"**Elapsed:** `{elapsed:.2f}s`",
                ),
                ephemeral=True,
            )
            return

        elapsed = time.perf_counter() - start

        # ── Build the result embed ─────────────────────────────────────────
        if timed_out:
            embed = _warn(
                "Command Timed Out",
                f"The command exceeded the **{_EVAL_TIMEOUT}s** time limit and was killed.\n"
                f"**Command:** `{command}`",
            )
            embed.set_footer(text=f"Elapsed: {elapsed:.2f}s  •  Aura Owner Tools")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        success = return_code == 0
        color = discord.Color.green() if success else discord.Color.red()
        status_icon = "✅" if success else "❌"

        embed = discord.Embed(
            title=f"{status_icon}  Eval — Exit Code {return_code}",
            color=color,
            timestamp=_ts(),
        )
        embed.set_footer(text=f"Elapsed: {elapsed:.3f}s  •  Aura Owner Tools")

        # Truncate command preview for the embed field
        cmd_preview = command if len(command) <= 200 else command[:197] + "…"
        embed.add_field(name="Command", value=f"`{cmd_preview}`", inline=False)

        # Decide how to present stdout / stderr
        files: list[discord.File] = []

        def _format_stream(label: str, text: str) -> tuple[str | None, discord.File | None]:
            """Return (inline_value, file_attachment) for a stream."""
            if not text:
                return None, None
            if len(text) <= _INLINE_LIMIT:
                return _code_block(text, ""), None
            # Too long → attach as a file
            buf = io.BytesIO(text.encode("utf-8"))
            fname = f"{label.lower()}.txt"
            return (
                f"Output too long — see attached `{fname}`.",
                discord.File(buf, filename=fname),
            )

        stdout_val, stdout_file = _format_stream("stdout", stdout_text)
        stderr_val, stderr_file = _format_stream("stderr", stderr_text)

        if stdout_val:
            embed.add_field(name="stdout", value=stdout_val, inline=False)
        else:
            embed.add_field(name="stdout", value="*(no output)*", inline=False)

        if stderr_val:
            embed.add_field(name="stderr", value=stderr_val, inline=False)

        if stdout_file:
            files.append(stdout_file)
        if stderr_file:
            files.append(stderr_file)

        await interaction.followup.send(embed=embed, files=files, ephemeral=True)

    # ── /console ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="console",
        description=f"[OWNER] Show the last {_CONSOLE_LINES} lines of Aura's console log.",
    )
    @app_commands.describe(
        lines=f"How many lines to show (1–100, default {_CONSOLE_LINES})",
    )
    async def console_cmd(
        self,
        interaction: discord.Interaction,
        lines: app_commands.Range[int, 1, 100] = _CONSOLE_LINES,
    ) -> None:
        if not await self._owner_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        tail_lines = _tail(_LOG_FILE, lines)

        if not tail_lines:
            await interaction.followup.send(
                embed=_warn(
                    "Log Empty or Missing",
                    f"Could not find or read `{_LOG_FILE}`.\n"
                    "The log file is created when Aura starts and writes its first entry.",
                ),
                ephemeral=True,
            )
            return

        content = "".join(tail_lines).strip()

        embed = discord.Embed(
            title=f"🖥️  Console — Last {len(tail_lines)} Line(s)",
            color=discord.Color.dark_grey(),
            timestamp=_ts(),
        )
        embed.set_footer(text=f"Log file: {_LOG_FILE}  •  Aura Owner Tools")

        files: list[discord.File] = []

        if len(content) <= _INLINE_LIMIT:
            embed.description = _code_block(content, "")
        else:
            embed.description = (
                f"The log output is too long to display inline "
                f"— see attached `console.txt`."
            )
            buf = io.BytesIO(content.encode("utf-8"))
            files.append(discord.File(buf, filename="console.txt"))

        await interaction.followup.send(embed=embed, files=files, ephemeral=True)


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OwnerCog(bot))
    print("👑 OwnerCog loaded")
