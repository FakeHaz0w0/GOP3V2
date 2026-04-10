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
from supabase import create_client, Client

# NudeNet
try:
    from nudenet import NudeDetector
    nude_detector = NudeDetector()
    NUDENET_AVAILABLE = True
except ImportError:
    print("[WARNING] nudenet not installed.")
    NUDENET_AVAILABLE = False

# ─────────────────────────────────────────────
#  Environment variables
# ─────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN secret is not set.")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_KEY secrets are not set.\n"
        "Add them in GitHub: Settings → Secrets and variables → Actions"
    )

# ─────────────────────────────────────────────
#  Supabase client
# ─────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────
def get_setting(guild_id: int, key: str, default=False) -> bool:
    try:
        res = supabase.table("settings") \
            .select(key) \
            .eq("guild_id", str(guild_id)) \
            .execute()
        if res.data:
            return res.data[0].get(key, default)
        return default
    except Exception as e:
        print(f"[db] get_setting error: {e}")
        return default

def set_setting(guild_id: int, key: str, value):
    try:
        supabase.table("settings").upsert({
            "guild_id": str(guild_id),
            key: value
        }).execute()
    except Exception as e:
        print(f"[db] set_setting error: {e}")

def add_warning(guild_id: int, user: discord.Member, reason: str | None) -> int:
    try:
        supabase.table("warnings").insert({
            "guild_id": str(guild_id),
            "user_id": str(user.id),
            "user_name": user.display_name,
            "reason": reason or "No reason given"
        }).execute()
        # Count total warnings for this user in this guild
        res = supabase.table("warnings") \
            .select("user_id", count="exact") \
            .eq("guild_id", str(guild_id)) \
            .eq("user_id", str(user.id)) \
            .execute()
        return res.count or 1
    except Exception as e:
        print(f"[db] add_warning error: {e}")
        return 0

def get_warnings(guild_id: int) -> list:
    try:
        res = supabase.table("warnings") \
            .select("*") \
            .eq("guild_id", str(guild_id)) \
            .order("created_at", desc=False) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[db] get_warnings error: {e}")
        return []

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
#  Anti-slur patterns
# ─────────────────────────────────────────────
def _slur_pattern(variants):
    return re.compile("|".join(variants), re.IGNORECASE)

SLUR_PATTERNS = [
    (_slur_pattern([r"n[i1!|]+[g9q]+[e3]+r", r"n[i1!|]+[g9q]{2,}[a@4]+", r"nigg[a@4e3]", r"nigger", r"nigga"]), "n****r"),
    (_slur_pattern([r"f[a@4]+g+[o0]+t", r"f[a@4]+gg?[o0i1]+t?", r"fagg?[oi]t?"]), "f****t"),
    (_slur_pattern([r"r[e3]+t[a@4]+rd", r"ret[a@4]rd"]), "r****d"),
    (_slur_pattern([r"k[i1!]+k[e3]+", r"kike"]), "k**e"),
    (_slur_pattern([r"ch[i1!]+nk", r"ch[i1!]nk"]), "c**nk"),
    (_slur_pattern([r"sp[i1!]+c", r"spick?"]), "s**c"),
    (_slur_pattern([r"gypp?[o0]", r"g[i1!]+pp?[o0]"]), "g**o"),
    (_slur_pattern([r"tr[a@4]nn[yi1!]e?", r"tranny"]), "t****y"),
    (_slur_pattern([r"w[e3]+tb[a@4]+ck", r"wetback"]), "w*****k"),
]

def detect_slur(text: str) -> tuple[bool, str | None]:
    for pattern, display in SLUR_PATTERNS:
        if pattern.search(text):
            return True, display
    return False, None

# ─────────────────────────────────────────────
#  NudeNet labels
# ─────────────────────────────────────────────
NUDE_LABELS = {
    "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED", "BUTTOCKS_EXPOSED",
}

# ─────────────────────────────────────────────
#  NSFW keyword & domain blocklists
# ─────────────────────────────────────────────
BLOCKED_KEYWORDS = [
    "pornhub", "xvideos", "xnxx", "xhamster", "onlyfans",
    "chaturbate", "brazzers", "bangbros", "nhentai", "rule34",
    "e621", "gelbooru", "hentaihaven",
    "roblox porn", "roblox sex", "roblox r34", "roblox rule34",
    "roblox nsfw", "roblox nude", "roblox hentai",
    "minecraft porn", "minecraft sex", "minecraft r34",
    "fortnite porn", "fortnite sex", "fortnite r34",
    "vrchat porn", "vrchat sex", "vrchat nsfw",
    "gmod porn", "garrys mod porn", "lego porn", "lego sex",
    "cartoon porn", "cartoon sex", "drawn porn",
    "send nudes", "send nude", "dick pic", "cock pic",
    "nude pic", "nude photo", "naked pic",
]

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

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─────────────────────────────────────────────
#  Image scanners
# ─────────────────────────────────────────────
def nudenet_is_nsfw(image_path: str) -> bool:
    if not NUDENET_AVAILABLE:
        return False
    try:
        for d in nude_detector.detect(image_path):
            if d.get("class") in NUDE_LABELS and d.get("score", 0) > 0.5:
                return True
        return False
    except Exception as e:
        print(f"[nudenet] error: {e}")
        return False

async def nsfwjs_check(image_path: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "nsfw_checker.js", image_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return json.loads(stdout.decode()) if proc.returncode == 0 else {}
    except Exception as e:
        print(f"[nsfwjs] error: {e}")
        return {}

def nsfwjs_is_nsfw(p: dict) -> bool:
    return p.get("Porn", 0) > 0.60 or p.get("Hentai", 0) > 0.45

async def is_nsfw_image(image_path: str) -> bool:
    if nudenet_is_nsfw(image_path):
        return True
    return nsfwjs_is_nsfw(await nsfwjs_check(image_path))

def contains_blocked_keyword(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in BLOCKED_KEYWORDS)

def is_blocked_domain(url: str) -> str | None:
    try:
        domain = (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
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
        print(f"[url_scan] {e}")
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
    print(f"NudeNet: {NUDENET_AVAILABLE}")
    print("Slash commands synced.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    guild_id = message.guild.id if message.guild else None
    if not guild_id:
        return

    # Anti-slur
    if get_setting(guild_id, "antislur"):
        found, slur_display = detect_slur(message.content)
        if found:
            await message.delete()
            await message.channel.send(
                f"🚫 {message.author.mention} has said a slur (`{slur_display}`). "
                f"That is not allowed here.",
                delete_after=10,
            )
            return

    # NSFW mod
    if get_setting(guild_id, "nsfw"):
        if contains_blocked_keyword(message.content):
            await message.delete()
            await message.channel.send(
                f"🚫 {message.author.mention} your message was removed "
                f"because it contained blocked content.",
                delete_after=8,
            )
            return

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
@tree.command(name="nsfwmoderation", description="Enable or disable NSFW image moderation.")
@app_commands.describe(enabled="true to enable, false to disable")
@app_commands.checks.has_permissions(manage_guild=True)
async def nsfwmoderation(interaction: discord.Interaction, enabled: bool):
    set_setting(interaction.guild_id, "nsfw", enabled)
    status = "✅ **enabled**" if enabled else "❌ **disabled**"
    await interaction.response.send_message(f"NSFW moderation is now {status}.", ephemeral=True)

@nsfwmoderation.error
async def nsfwmoderation_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⛔ You need **Manage Server** permission.", ephemeral=True)


@tree.command(name="antislurmode", description="Enable or disable automatic slur detection.")
@app_commands.describe(enabled="true to enable, false to disable")
@app_commands.checks.has_permissions(manage_guild=True)
async def antislurmode(interaction: discord.Interaction, enabled: bool):
    set_setting(interaction.guild_id, "antislur", enabled)
    status = "✅ **enabled**" if enabled else "❌ **disabled**"
    await interaction.response.send_message(f"Anti-slur mode is now {status}.", ephemeral=True)

@antislurmode.error
async def antislurmode_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⛔ You need **Manage Server** permission.", ephemeral=True)


@tree.command(name="warn", description="Warn a user.")
@app_commands.describe(user="The user to warn", reason="Reason for the warning (optional)")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    if user.bot:
        await interaction.response.send_message("❌ You cannot warn a bot.", ephemeral=True)
        return
    count = add_warning(interaction.guild_id, user, reason)
    if reason:
        msg = f"⚠️ {user.mention} has been warned for **{reason}**. (Warning #{count})"
    else:
        msg = f"⚠️ {user.mention} has been warned. No reason given. (Warning #{count})"
    await interaction.response.send_message(msg)

@warn.error
async def warn_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⛔ You need **Moderate Members** permission.", ephemeral=True)


@tree.command(name="warnings", description="List all warnings for this server.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction):
    rows = get_warnings(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("✅ No warnings on record.", ephemeral=True)
        return

    # Group by user
    users: dict[str, dict] = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in users:
            users[uid] = {"name": row["user_name"], "reasons": []}
        users[uid]["reasons"].append(row["reason"])

    embed = discord.Embed(
        title=f"⚠️ Warnings — {interaction.guild.name}",
        color=discord.Color.orange()
    )
    for uid, data in sorted(users.items(), key=lambda x: len(x[1]["reasons"]), reverse=True):
        reasons = "\n".join(f"• {r}" for r in data["reasons"][-5:])
        embed.add_field(
            name=f"{data['name']} (ID: {uid})",
            value=f"**{len(data['reasons'])} warning(s)**\n{reasons}",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@warnings.error
async def warnings_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⛔ You need **Moderate Members** permission.", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
