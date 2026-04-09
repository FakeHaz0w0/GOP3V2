import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web
import asyncio
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlparse

# NudeNet import with friendly error
try:
    from nudenet import NudeDetector
    nude_detector = NudeDetector()
    NUDENET_AVAILABLE = True
except ImportError:
    print("[WARNING] nudenet not installed. Run: pip install nudenet")
    NUDENET_AVAILABLE = False

# Token
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is not set.\n"
        "Add it as a repository secret in GitHub:\n"
        "  Settings → Secrets and variables → Actions → New repository secret\n"
        "  Name: DISCORD_TOKEN"
    )

# ─────────────────────────────────────────────
#  UptimeRobot keep-alive web server
# ─────────────────────────────────────────────
def run_web():
    async def handle(request):
        return web.Response(text="Bot is alive!")
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    loop.run_until_complete(site.start())
    loop.run_forever()

threading.Thread(target=run_web, daemon=True).start()

# ─────────────────────────────────────────────
#  NudeNet body part labels to flag
# ─────────────────────────────────────────────
NUDE_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}

# ─────────────────────────────────────────────
#  Keyword blocklist (text messages)
#  Catches mentions of NSFW sites, slurs, and
#  virtual/Roblox porn terms
# ─────────────────────────────────────────────
BLOCKED_KEYWORDS = [
    # NSFW site names typed in chat
    "pornhub", "xvideos", "xnxx", "xhamster", "onlyfans",
    "chaturbate", "brazzers", "bangbros", "nhentai", "rule34",
    "e621", "gelbooru", "hentaihaven",
    # Virtual / Roblox porn terms
    "roblox porn", "roblox sex", "roblox r34", "roblox rule34",
    "roblox nsfw", "roblox nude", "roblox hentai",
    "minecraft porn", "minecraft sex", "minecraft r34",
    "fortnite porn", "fortnite sex", "fortnite r34",
    "vrchat porn", "vrchat sex", "vrchat nsfw",
    "gmod porn", "garrys mod porn",
    "lego porn", "lego sex",
    "cartoon porn", "cartoon sex", "drawn porn",
    # Generic explicit terms
    "send nudes", "send nude", "dick pic", "cock pic",
    "nude pic", "nude photo", "naked pic",
]

# ─────────────────────────────────────────────
#  Known NSFW domain blocklist
# ─────────────────────────────────────────────
BLOCKED_DOMAINS = {
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
    "redtube.com", "youporn.com", "tube8.com", "spankbang.com",
    "eporner.com", "tnaflix.com", "porntrex.com", "beeg.com",
    "drtuber.com", "nuvid.com", "txxx.com", "fuq.com",
    "4tube.com", "porndig.com", "slutload.com", "thumbzilla.com",
    "cliphunter.com", "hardsextube.com", "pornone.com", "anysex.com",
    "upornia.com", "bravotube.net", "gotporn.com", "sunporno.com",
    "rule34.xxx", "rule34.paheal.net", "gelbooru.com",
    "danbooru.donmai.us", "e621.net", "hentai-foundry.com",
    "nhentai.net", "hentaihaven.xxx",
    "chaturbate.com", "cam4.com", "myfreecams.com", "livejasmin.com",
    "stripchat.com", "bongacams.com", "camsoda.com", "streamate.com",
    "onlyfans.com", "fansly.com", "manyvids.com", "clips4sale.com",
    "naughtyamerica.com", "brazzers.com", "bangbros.com",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Per-guild NSFW moderation toggle
nsfw_mod_enabled: dict[int, bool] = {}

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────────
#  Scanner 1 — NudeNet
# ─────────────────────────────────────────────
def nudenet_is_nsfw(image_path: str) -> bool:
    if not NUDENET_AVAILABLE:
        return False
    try:
        detections = nude_detector.detect(image_path)
        for d in detections:
            if d.get("class") in NUDE_LABELS and d.get("score", 0) > 0.5:
                return True
        return False
    except Exception as e:
        print(f"[nudenet] error: {e}")
        return False


# ─────────────────────────────────────────────
#  Scanner 2 — nsfwjs (also catches virtual/drawn porn via Hentai class)
# ─────────────────────────────────────────────
async def nsfwjs_check(image_path: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "nsfw_checker.js", image_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            print(f"[nsfwjs] stderr: {stderr.decode()}")
            return {}
        return json.loads(stdout.decode())
    except Exception as e:
        print(f"[nsfwjs] error: {e}")
        return {}


def nsfwjs_is_nsfw(predictions: dict) -> bool:
    if not predictions:
        return False
    # Lower Hentai threshold to 0.45 to catch virtual/Roblox-style porn
    porn_score = predictions.get("Porn", 0)
    hentai_score = predictions.get("Hentai", 0)
    return porn_score > 0.60 or hentai_score > 0.45


# ─────────────────────────────────────────────
#  Combined image scan
# ─────────────────────────────────────────────
async def is_nsfw_image(image_path: str) -> bool:
    if nudenet_is_nsfw(image_path):
        print(f"[scan] NudeNet flagged: {image_path}")
        return True
    predictions = await nsfwjs_check(image_path)
    if nsfwjs_is_nsfw(predictions):
        print(f"[scan] nsfwjs flagged: {image_path}")
        return True
    return False


# ─────────────────────────────────────────────
#  Keyword check
# ─────────────────────────────────────────────
def contains_blocked_keyword(text: str) -> str | None:
    lower = text.lower()
    for keyword in BLOCKED_KEYWORDS:
        if keyword in lower:
            return keyword
    return None


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
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
        return await is_nsfw_image(tmp_path)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"NudeNet available: {NUDENET_AVAILABLE}")
    print("Slash commands synced.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild_id = message.guild.id if message.guild else None

    if guild_id and nsfw_mod_enabled.get(guild_id, False):

        # ── 1. Keyword check in message text ──
        keyword = contains_blocked_keyword(message.content)
        if keyword:
            await message.delete()
            await message.channel.send(
                f"🚫 {message.author.mention} your message was removed "
                f"because it contained blocked content.",
                delete_after=8,
            )
            return

        # ── 2. Uploaded image attachments ──
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
                    if await is_nsfw_image(tmp_path):
                        await message.delete()
                        await message.channel.send(
                            f"🚫 {message.author.mention} your image was removed "
                            f"because it was flagged as NSFW.",
                            delete_after=8,
                        )
                        return
                finally:
                    os.unlink(tmp_path)

        # ── 3. URLs in message content ──
        for url in URL_REGEX.findall(message.content):

            blocked = is_blocked_domain(url)
            if blocked:
                await message.delete()
                await message.channel.send(
                    f"🚫 {message.author.mention} your message was removed "
                    f"because it contained a link to a known NSFW site (`{blocked}`).",
                    delete_after=8,
                )
                return

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


if __name__ == "__main__":
    bot.run(TOKEN)
