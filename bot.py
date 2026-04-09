import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import json
import os
import re
import tempfile
from urllib.parse import urlparse

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

# ─────────────────────────────────────────────
#  Known NSFW domain blocklist
# ─────────────────────────────────────────────
BLOCKED_DOMAINS = {
    # Major adult sites
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
    "redtube.com", "youporn.com", "tube8.com", "spankbang.com",
    "eporner.com", "tnaflix.com", "porntrex.com", "beeg.com",
    "drtuber.com", "nuvid.com", "txxx.com", "fuq.com",
    "4tube.com", "porndig.com", "slutload.com", "thumbzilla.com",
    "cliphunter.com", "hardsextube.com", "pornone.com", "anysex.com",
    "upornia.com", "bravotube.net", "gotporn.com", "sunporno.com",
    # Adult image boards
    "rule34.xxx", "rule34.paheal.net", "gelbooru.com",
    "danbooru.donmai.us", "e621.net", "hentai-foundry.com",
    "nhentai.net", "hentaihaven.xxx",
    # Webcam / live
    "chaturbate.com", "cam4.com", "myfreecams.com", "livejasmin.com",
    "stripchat.com", "bongacams.com", "camsoda.com", "streamate.com",
    # Subscription adult content
    "onlyfans.com", "fansly.com", "manyvids.com", "clips4sale.com",
    "naughtyamerica.com", "brazzers.com", "bangbros.com",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

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
    if not predictions:
        return False
    return predictions.get("Porn", 0) + predictions.get("Hentai", 0) > 0.60


def extract_domain(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or ""
        return hostname.removeprefix("www.")
    except Exception:
        return ""


def is_blocked_domain(url: str) -> str | None:
    domain = extract_domain(url)
    if not domain:
        return None
    for blocked in BLOCKED_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked):
            return blocked
    return None


async def download_and_scan(url: str) -> bool:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False
                if int(resp.headers.get("Content-Length", 0)) > 10_000_000:
                    return False
                image_data = await resp.read()
    except Exception as e:
        print(f"[url_scan] failed to download {url}: {e}")
        return False

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(image_data)
        tmp_path = tmp.name

    try:
        return is_nsfw_result(await check_nsfw(tmp_path))
    finally:
        os.unlink(tmp_path)


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
    if message.author.bot:
        return

    guild_id = message.guild.id if message.guild else None

    if guild_id and nsfw_mod_enabled.get(guild_id, False):

        # ── 1. Uploaded image attachments ──
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(IMAGE_EXTENSIONS):
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()

                with tempfile.NamedTemporaryFile(
                    suffix=os.path.splitext(attachment.filename)[1], delete=False
                ) as tmp:
                    tmp.write(image_data)
                    tmp_path = tmp.name

                try:
                    if is_nsfw_result(await check_nsfw(tmp_path)):
                        await message.delete()
                        await message.channel.send(
                            f"🚫 {message.author.mention} your image was removed "
                            f"because it was flagged as NSFW.",
                            delete_after=8,
                        )
                        return
                finally:
                    os.unlink(tmp_path)

        # ── 2. URLs in message content ──
        for url in URL_REGEX.findall(message.content):

            # 2a. Blocked domain — instant, no download needed
            blocked = is_blocked_domain(url)
            if blocked:
                await message.delete()
                await message.channel.send(
                    f"🚫 {message.author.mention} your message was removed "
                    f"because it contained a link to a known NSFW site (`{blocked}`).",
                    delete_after=8,
                )
                return

            # 2b. Direct image URL — download and scan with nsfwjs
            if url.lower().endswith(IMAGE_EXTENSIONS):
                if await download_and_scan(url):
                    await message.delete()
                    await message.channel.send(
                        f"🚫 {message.author.mention} your image link was removed "
                        f"because it was flagged as NSFW.",
                        delete_after=8,
                    )
                    return

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
