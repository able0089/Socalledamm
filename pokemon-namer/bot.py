import asyncio
import io
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import discord
import aiohttp
import imagehash
from PIL import Image
from aiohttp import web as aiohttp_web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

log.info("bot.py starting up")

# ── CONFIG ─────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    log.error("FATAL: DISCORD_TOKEN not set!")
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

DELAY_MIN = float(os.environ.get("DELAY_MIN", "2.0"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "4.5"))
COOLDOWN  = float(os.environ.get("COOLDOWN", "3.0"))

# Max Hamming distance to accept a match (out of 256 bits for hash_size=16)
# 0 = identical pixels, lower = stricter. 15 is very permissive for JPEG noise.
HASH_THRESHOLD = int(os.environ.get("HASH_THRESHOLD", "15"))
HASH_SIZE = 16  # must match build_hash_db.py

# ── HASH DATABASE ──────────────────────────────────────────────────────

HASH_DB: dict[str, str] = {}           # phash_str → pokemon_name
HASH_OBJ: list[tuple] = []             # [(imagehash_obj, pokemon_name), ...]

HASH_DB_PATH = Path(__file__).parent / "hash_db.json"


def load_hash_db() -> None:
    global HASH_DB, HASH_OBJ

    if not HASH_DB_PATH.exists():
        log.error(
            "hash_db.json not found! Run: python3 pokemon-namer/build_hash_db.py"
        )
        return

    with open(HASH_DB_PATH) as f:
        HASH_DB = json.load(f)

    HASH_OBJ = [(imagehash.hex_to_hash(h), name) for h, name in HASH_DB.items()]
    log.info("Hash DB loaded: %d Pokemon fingerprints", len(HASH_OBJ))


# ── IDENTIFICATION ─────────────────────────────────────────────────────

async def identify(
    session: aiohttp.ClientSession, image_url: str
) -> tuple[str | None, str]:
    """Download the spawn image and find the closest match in the hash DB."""

    if not HASH_OBJ:
        log.warning("Hash DB is empty — run build_hash_db.py first")
        return None, ""

    try:
        async with session.get(
            image_url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                log.warning("Image download failed (HTTP %d): %s", r.status, image_url)
                return None, ""
            data = await r.read()
    except Exception as exc:
        log.error("Error downloading spawn image: %s", exc)
        return None, ""

    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        query_hash = imagehash.phash(img, hash_size=HASH_SIZE)
    except Exception as exc:
        log.error("Error computing image hash: %s", exc)
        return None, ""

    best_name: str | None = None
    best_dist = 9999

    for stored_hash, name in HASH_OBJ:
        dist = query_hash - stored_hash
        if dist < best_dist:
            best_dist = dist
            best_name = name

    log.info(
        "Hash match: %s (distance=%d / threshold=%d)",
        best_name, best_dist, HASH_THRESHOLD,
    )

    if best_dist > HASH_THRESHOLD:
        log.warning(
            "Best match %s rejected (distance %d > threshold %d)",
            best_name, best_dist, HASH_THRESHOLD,
        )
        return None, ""

    display = best_name.replace("-", " ").title() if best_name else None
    return display, f"hash d={best_dist}"


# ── SPAWN DETECTION ────────────────────────────────────────────────────

def get_spawn_image_url(message: discord.Message) -> str | None:
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return att.url
    return None


def is_spawn_message(message: discord.Message) -> bool:
    if message.author.id != POKETWO_BOT_ID:
        return False
    for embed in message.embeds:
        text = ((embed.title or "") + (embed.description or "")).lower()
        if "wild" in text and "pokémon" in text:
            return True
    return False


# ── STATE ──────────────────────────────────────────────────────────────

http_session: aiohttp.ClientSession | None = None
channel_last_action: dict[int, float] = {}
semaphore = asyncio.Semaphore(3)


def on_cooldown(channel_id: int) -> bool:
    return (time.time() - channel_last_action.get(channel_id, 0)) < COOLDOWN


def update_cooldown(channel_id: int) -> None:
    channel_last_action[channel_id] = time.time()


# ── BOT ────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info(
            "Logged in as %s (ID: %s)", client.user,
            client.user.id if client.user else "?"
        )
        log.info(
            "Hash DB ready: %d fingerprints | threshold: %d",
            len(HASH_OBJ), HASH_THRESHOLD,
        )
        if WATCH_CHANNEL_IDS:
            log.info("Watching channels: %s", WATCH_CHANNEL_IDS)
        else:
            log.info("Watching ALL channels")

    @client.event
    async def on_message(message: discord.Message) -> None:
        await handle_message(message)

    async def handle_message(message: discord.Message) -> None:
        if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
            return

        content = message.content.strip()

        # ── !ping ──────────────────────────────────────────────────────
        if content.lower() == "!ping":
            await message.channel.send("Pong!")
            return

        # ── !correct <name> ────────────────────────────────────────────
        if content.lower().startswith("!correct "):
            correct_name = content[9:].strip()
            log.info("USER CORRECTION: correct name is '%s'", correct_name)
            await message.channel.send(
                f"Got it! The correct Pokemon was **{correct_name}**.",
                reference=message,
                mention_author=False,
            )
            return

        # ── !guess ─────────────────────────────────────────────────────
        if content.lower() == "!guess":
            ref = message.reference
            if not ref:
                await message.channel.send(
                    "Reply to a Poketwo spawn message with `!guess`.",
                    mention_author=False,
                )
                return

            try:
                target = (
                    ref.resolved
                    if isinstance(ref.resolved, discord.Message)
                    else await message.channel.fetch_message(ref.message_id)
                )
            except Exception as exc:
                log.error("Could not fetch referenced message: %s", exc)
                await message.channel.send(
                    "Couldn't fetch that message.", mention_author=False
                )
                return

            image_url = get_spawn_image_url(target)
            if not image_url and target.attachments:
                image_url = target.attachments[0].url
            if not image_url:
                await message.channel.send(
                    "No image found in that message.", mention_author=False
                )
                return

            log.info("!guess triggered — image URL: %s", image_url)
            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if name:
                log.info("RESULT: %s (via %s)", name, method)
                await message.channel.send(
                    f"That's **{name}**!",
                    reference=message,
                    mention_author=False,
                )
            else:
                log.warning("Could not identify Pokemon from: %s", image_url)
                await message.channel.send(
                    "Sorry, I couldn't identify that Pokemon. "
                    "Use `!correct <name>` if you know what it is.",
                    reference=message,
                    mention_author=False,
                )
            return

        # ── Auto-detect live Poketwo spawns ────────────────────────────
        if not is_spawn_message(message):
            return

        async with semaphore:
            if on_cooldown(message.channel.id):
                log.info(
                    "Skipping spawn in channel %s (cooldown)", message.channel.id
                )
                return

            image_url = get_spawn_image_url(message)
            if not image_url:
                log.warning("Spawn message had no image URL")
                return

            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if not name:
                log.warning(
                    "Could not identify Pokemon — image: %s", image_url
                )
                return

            log.info(
                "Auto-identified: %s (via %s) — image: %s",
                name, method, image_url,
            )

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log.info("Waiting %.2fs before posting…", delay)
            await asyncio.sleep(delay)

            update_cooldown(message.channel.id)

            try:
                await message.channel.send(
                    f"That's **{name}**!",
                    reference=message,
                    mention_author=False,
                )
            except discord.Forbidden:
                log.warning(
                    "No permission to send in channel %s", message.channel.id
                )
            except discord.HTTPException as exc:
                log.error("Failed to send message: %s", exc)

    await client.start(TOKEN)


# ── WEB SERVER ─────────────────────────────────────────────────────────

async def start_web() -> None:
    port = int(os.environ.get("PORT", 8000))

    async def health(_: aiohttp_web.Request) -> aiohttp_web.Response:
        return aiohttp_web.Response(text="OK")

    app = aiohttp_web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server listening on port %d", port)


# ── MAIN ───────────────────────────────────────────────────────────────

async def main() -> None:
    global http_session

    connector = aiohttp.TCPConnector(limit=20)
    http_session = aiohttp.ClientSession(connector=connector)

    await start_web()

    load_hash_db()

    await asyncio.sleep(2)

    log.info("Starting Discord bot…")
    try:
        await run_bot()
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
