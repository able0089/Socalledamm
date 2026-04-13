import asyncio
import os
import sys
import random
import time
import re
import json
import logging
from pathlib import Path

import discord
import aiohttp
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

HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_MODEL = os.environ.get("HF_MODEL", "imjeffhi/pokemon_classifier")
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "30"))

POKETWO_BOT_ID = 716390085896962058

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

DELAY_MIN = float(os.environ.get("DELAY_MIN", "2.0"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "4.5"))
COOLDOWN = float(os.environ.get("COOLDOWN", "3.0"))

# ── POKEDEX ────────────────────────────────────────────────────────────

POKEDEX: dict[str, str] = {}
POKEDEX_PATH = Path(__file__).parent / "pokedex.json"
POKEAPI_URL = "https://pokeapi.co/api/v2/pokemon?limit=2000"


async def load_pokedex(session: aiohttp.ClientSession) -> None:
    global POKEDEX

    if POKEDEX_PATH.exists():
        with open(POKEDEX_PATH) as f:
            POKEDEX = json.load(f)
        log.info("Pokedex loaded from disk: %d entries", len(POKEDEX))
        return

    log.info("pokedex.json not found, fetching from PokeAPI…")
    try:
        async with session.get(POKEAPI_URL, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
        for entry in data["results"]:
            parts = entry["url"].rstrip("/").split("/")
            dex_id = parts[-1]
            name = entry["name"].replace("-", " ").title()
            POKEDEX[dex_id] = name
        with open(POKEDEX_PATH, "w") as f:
            json.dump(POKEDEX, f, indent=2)
        log.info("Pokedex fetched and saved: %d entries", len(POKEDEX))
    except Exception as exc:
        log.error("Failed to load Pokedex from PokeAPI: %s", exc)


def lookup_pokemon_name(dex_id: str) -> str | None:
    return POKEDEX.get(dex_id)


# ── IDENTIFICATION ─────────────────────────────────────────────────────
# Method 1: Parse dex number from Poketwo CDN URL (fast, works if URL contains dex id)
# Method 2: Hugging Face image classification (works for Discord CDN URLs)

CDN_PATTERN = re.compile(r"poketwo\.net/images/(\d+)\.", re.IGNORECASE)

HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"


def normalize_hf_label(label: str) -> str:
    """Convert HF model label like 'mr-mime' or 'pikachu' to 'Mr Mime' / 'Pikachu'."""
    return label.replace("-", " ").replace("_", " ").title()


def identify_from_url(image_url: str) -> tuple[str | None, str]:
    """Try to identify a Pokemon directly from the Poketwo CDN URL (fast path)."""
    m = CDN_PATTERN.search(image_url)
    if m:
        dex_id = m.group(1)
        name = lookup_pokemon_name(dex_id)
        if name:
            return name, f"dex #{dex_id}"
    return None, ""


async def identify_from_image(session: aiohttp.ClientSession, image_url: str) -> tuple[str | None, str]:
    """Download the image and classify it via Hugging Face Inference API."""
    # Download image bytes
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("Failed to download image (HTTP %d): %s", r.status, image_url)
                return None, ""
            image_bytes = await r.read()
    except Exception as exc:
        log.error("Error downloading image: %s", exc)
        return None, ""

    # Build HF API request
    headers: dict[str, str] = {"Content-Type": "application/octet-stream"}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"

    try:
        async with session.post(
            HF_API_URL,
            data=image_bytes,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status == 503:
                # Model is loading — wait and retry once
                log.info("HF model loading (503), waiting 10s and retrying…")
                await asyncio.sleep(10)
                async with session.post(
                    HF_API_URL,
                    data=image_bytes,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r2:
                    if r2.status != 200:
                        log.warning("HF API error after retry: HTTP %d", r2.status)
                        return None, ""
                    results = await r2.json()
            elif r.status != 200:
                body = await r.text()
                log.warning("HF API error HTTP %d: %s", r.status, body[:200])
                return None, ""
            else:
                results = await r.json()
    except Exception as exc:
        log.error("HF API request failed: %s", exc)
        return None, ""

    # results is a list like [{"label": "pikachu", "score": 0.97}, ...]
    if not isinstance(results, list) or not results:
        log.warning("Unexpected HF API response: %s", results)
        return None, ""

    top = results[0]
    label = top.get("label", "")
    score = float(top.get("score", 0)) * 100

    log.info("HF top result: %s (%.1f%%)", label, score)

    if score < MIN_CONFIDENCE:
        log.warning("Confidence too low (%.1f%% < %.1f%%): %s", score, MIN_CONFIDENCE, label)
        return None, ""

    name = normalize_hf_label(label)
    return name, f"AI {score:.0f}%"


async def identify(session: aiohttp.ClientSession, image_url: str) -> tuple[str | None, str]:
    """Try URL-based identification first, fall back to AI classification."""
    name, method = identify_from_url(image_url)
    if name:
        return name, method
    return await identify_from_image(session, image_url)


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
        log.info("Logged in as %s (ID: %s)", client.user, client.user.id if client.user else "?")
        log.info("Pokedex ready: %d Pokemon", len(POKEDEX))
        log.info("HF model: %s (min confidence: %.0f%%)", HF_MODEL, MIN_CONFIDENCE)
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
                f"Got it! The correct Pokemon was **{correct_name}**. I'll note that.",
                reference=message,
                mention_author=False,
            )
            return

        # ── !guess ─────────────────────────────────────────────────────
        if content.lower() == "!guess":
            ref = message.reference
            if not ref:
                await message.channel.send("Reply to a Poketwo spawn message with `!guess`.", mention_author=False)
                return

            try:
                target = ref.resolved if isinstance(ref.resolved, discord.Message) else await message.channel.fetch_message(ref.message_id)
            except Exception as exc:
                log.error("Could not fetch referenced message: %s", exc)
                await message.channel.send("Couldn't fetch that message.", mention_author=False)
                return

            image_url = get_spawn_image_url(target)
            if not image_url and target.attachments:
                image_url = target.attachments[0].url
            if not image_url:
                await message.channel.send("No image found in that message.", mention_author=False)
                return

            log.info("!guess triggered — image URL: %s", image_url)
            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if name:
                log.info("RESULT: %s (via %s)", name, method)
                await message.channel.send(
                    f"That's **{name}**! *(identified via {method})*",
                    reference=message,
                    mention_author=False,
                )
            else:
                log.warning("Could not identify Pokemon from: %s", image_url)
                await message.channel.send(
                    "Sorry, I couldn't identify that Pokemon. Try `!correct <name>` to teach me.",
                    reference=message,
                    mention_author=False,
                )
            return

        # ── Auto-detect live Poketwo spawns ────────────────────────────
        if not is_spawn_message(message):
            return

        async with semaphore:
            if on_cooldown(message.channel.id):
                log.info("Skipping spawn in channel %s (cooldown)", message.channel.id)
                return

            image_url = get_spawn_image_url(message)
            if not image_url:
                log.warning("Spawn message had no image URL")
                return

            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if not name:
                log.warning("Could not identify Pokemon — image: %s", image_url)
                return

            log.info("Auto-identified: %s (via %s) — image: %s", name, method, image_url)

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
                log.warning("No permission to send in channel %s", message.channel.id)
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
    await load_pokedex(http_session)

    await asyncio.sleep(2)

    log.info("Starting Discord bot…")
    try:
        await run_bot()
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
