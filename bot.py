import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import json
import os
import tempfile

# Token is injected by GitHub Actions from the repository secret DISCORD_TOKEN.
# See .github/workflows/bot.yml — never hardcode this value.
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is not set.\n"
        "Add it as a repository secret in GitHub:\n"
        "  Settings → Secrets and variables → Actions → New repository secret\n"
        "  Name: DISCORD_TOKEN"
    )

# Per-guild NSFW moderation toggle  {guild_id: bool}
nsfw_mod_enabled: dict[int, bool] = {}

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────────
#  NSFW image check via nsfwjs (Node.js bridge)
# ─────────────────────────────────────────────
async def check_nsfw(image_path: str) -> dict:
    """
    Calls nsfw_checker.js with the path to a temp image file.
    Returns the nsfwjs prediction dict.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "nsfw_checker.js", image_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            print(f"[nsfw_checker] stderr: {stderr.decode()}")
            return {}
        return json.loads(stdout.decode())
    except Exception as e:
        print(f"[nsfw_checker] error: {e}")
        return {}


def is_nsfw_result(predictions: dict) -> bool:
    """
    Returns True if the image is considered NSFW.
    nsfwjs categories: Porn, Sexy, Hentai, Neutral, Drawing
    Flags if Porn + Hentai combined probability > 60 %
    """
    if not predictions:
        return False
    nsfw_score = predictions.get("Porn", 0) + predictions.get("Hentai", 0)
    return nsfw_score > 0.60


# ─────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced.")


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots
    if message.author.bot:
        return

    guild_id = message.guild.id if message.guild else None

    # Only scan if NSFW mod is enabled for this guild
    if guild_id and nsfw_mod_enabled.get(guild_id, False):
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext)
                   for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()

                # Write to temp file for nsfwjs
                with tempfile.NamedTemporaryFile(
                    suffix=os.path.splitext(attachment.filename)[1],
                    delete=False
                ) as tmp:
                    tmp.write(image_data)
                    tmp_path = tmp.name

                try:
                    predictions = await check_nsfw(tmp_path)
                    if is_nsfw_result(predictions):
                        await message.delete()
                        await message.channel.send(
                            f"🚫 **{message.author.mention}**, your image was removed "
                            f"because it was flagged as NSFW.",
                            delete_after=8,
                        )
                finally:
                    os.unlink(tmp_path)

    await bot.process_commands(message)


# ─────────────────────────────────────────────
#  Slash Commands
# ─────────────────────────────────────────────
@tree.command(name="nsfwmoderation", description="Enable or disable NSFW image moderation for this server.")
@app_commands.describe(enabled="Set to true to enable, false to disable NSFW moderation.")
@app_commands.checks.has_permissions(manage_guild=True)
async def nsfwmoderation(interaction: discord.Interaction, enabled: bool):
    guild_id = interaction.guild_id
    nsfw_mod_enabled[guild_id] = enabled
    status = "✅ **enabled**" if enabled else "❌ **disabled**"
    await interaction.response.send_message(
        f"NSFW moderation is now {status} for this server.",
        ephemeral=True,
    )


@nsfwmoderation.error
async def nsfwmoderation_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⛔ You need the **Manage Server** permission to use this command.",
            ephemeral=True,
        )


# ─────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
