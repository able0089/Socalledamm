import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import discord
import aiohttp
from aiohttp import web as aiohttp_web
from openai import AsyncOpenAI

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

AI_BASE_URL = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
AI_API_KEY  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "dummy")

POKETWO_BOT_ID = 716390085896962058

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

DELAY_MIN = float(os.environ.get("DELAY_MIN", "2.0"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "4.5"))
COOLDOWN  = float(os.environ.get("COOLDOWN", "3.0"))

# ── OPENAI CLIENT ──────────────────────────────────────────────────────

openai_client: AsyncOpenAI | None = None

def setup_openai() -> None:
    global openai_client
    if AI_BASE_URL:
        openai_client = AsyncOpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)
        log.info("OpenAI vision client ready (Replit AI Integrations)")
    else:
        log.warning("AI_INTEGRATIONS_OPENAI_BASE_URL not set — vision identification disabled")


# ── URL FAST PATH ──────────────────────────────────────────────────────
# If Poketwo ever reverts to embedding dex IDs in their CDN URL, use it instantly.

POKEDEX: dict[str, str] = {}
POKEDEX_PATH = Path(__file__).parent / "pokedex.json"
CDN_PATTERN  = re.compile(r"poketwo\.net/images/(\d+)\.", re.IGNORECASE)


def load_pokedex() -> None:
    global POKEDEX
    if POKEDEX_PATH.exists():
        with open(POKEDEX_PATH) as f:
            POKEDEX = json.load(f)
        log.info("Pokedex loaded: %d entries (fast-path fallback)", len(POKEDEX))


def identify_from_url(image_url: str) -> tuple[str | None, str]:
    m = CDN_PATTERN.search(image_url)
    if m:
        name = POKEDEX.get(m.group(1))
        if name:
            return name, f"dex #{m.group(1)}"
    return None, ""


# ── VISION IDENTIFICATION ──────────────────────────────────────────────

VISION_PROMPT = (
    "This is a screenshot from the Poketwo Discord bot showing a wild Pokemon spawn. "
    "Identify the Pokemon species shown in the image. "
    "Reply with ONLY the Pokemon's name — no punctuation, no explanation, nothing else. "
    "Examples of valid replies: Pikachu  |  Charizard  |  Mr. Mime  |  Sinistea"
)


async def identify_via_vision(
    session: aiohttp.ClientSession, image_url: str
) -> tuple[str | None, str]:
    if not openai_client:
        log.warning("Vision client not configured")
        return None, ""

    # Download image and encode as base64
    try:
        async with session.get(
            image_url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                log.warning("Image download failed (HTTP %d)", r.status)
                return None, ""
            img_bytes = await r.read()
            content_type = r.content_type or "image/jpeg"
    except Exception as exc:
        log.error("Image download error: %s", exc)
        return None, ""

    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:{content_type};base64,{b64}"

    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o",
            max_completion_tokens=32,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text",      "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        log.info("Vision raw reply: %r", raw)

        # Clean up any stray punctuation GPT might sneak in
        name = raw.strip(".,!?*_`\"'").title()
        if name:
            return name, "vision"
    except Exception as exc:
        log.error("OpenAI vision error: %s", exc)

    return None, ""


async def identify(
    session: aiohttp.ClientSession, image_url: str
) -> tuple[str | None, str]:
    """Try URL fast-path first, then AI vision."""
    name, method = identify_from_url(image_url)
    if name:
        return name, method
    return await identify_via_vision(session, image_url)


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
            "Logged in as %s (ID: %s)",
            client.user, client.user.id if client.user else "?"
        )
        log.info(
            "Vision: %s | Watching: %s",
            "enabled" if openai_client else "DISABLED",
            WATCH_CHANNEL_IDS or "all channels",
        )

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
                reference=message, mention_author=False,
            )
            return

        # ── !debug ─────────────────────────────────────────────────────
        if content.lower() == "!debug":
            ref = message.reference
            if not ref:
                await message.channel.send("Reply to a spawn message with `!debug`.", mention_author=False)
                return
            try:
                target = (
                    ref.resolved if isinstance(ref.resolved, discord.Message)
                    else await message.channel.fetch_message(ref.message_id)
                )
            except Exception as exc:
                await message.channel.send(f"Couldn't fetch: {exc}", mention_author=False)
                return
            lines = [f"Embeds: {len(target.embeds)}"]
            for i, emb in enumerate(target.embeds):
                lines += [
                    f"[{i}] image={emb.image.url if emb.image else None}",
                    f"[{i}] thumb={emb.thumbnail.url if emb.thumbnail else None}",
                    f"[{i}] title={emb.title}",
                ]
            log.info("DEBUG: %s", "\n".join(lines))
            await message.channel.send("```\n" + "\n".join(lines) + "\n```", mention_author=False)
            return

        # ── !guess ─────────────────────────────────────────────────────
        if content.lower() == "!guess":
            ref = message.reference
            if not ref:
                await message.channel.send(
                    "Reply to a Poketwo spawn message with `!guess`.", mention_author=False
                )
                return
            try:
                target = (
                    ref.resolved if isinstance(ref.resolved, discord.Message)
                    else await message.channel.fetch_message(ref.message_id)
                )
            except Exception as exc:
                log.error("Fetch error: %s", exc)
                await message.channel.send("Couldn't fetch that message.", mention_author=False)
                return

            image_url = get_spawn_image_url(target)
            if not image_url and target.attachments:
                image_url = target.attachments[0].url
            if not image_url:
                await message.channel.send("No image found in that message.", mention_author=False)
                return

            log.info("!guess — URL: %s", image_url)
            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if name:
                log.info("RESULT: %s (via %s)", name, method)
                await message.channel.send(
                    f"That's **{name}**!",
                    reference=message, mention_author=False,
                )
            else:
                await message.channel.send(
                    "Sorry, I couldn't identify that Pokemon. Use `!correct <name>` if you know.",
                    reference=message, mention_author=False,
                )
            return

        # ── Auto-detect live Poketwo spawns ────────────────────────────
        if not is_spawn_message(message):
            return

        async with semaphore:
            if on_cooldown(message.channel.id):
                log.info("Cooldown — skipping channel %s", message.channel.id)
                return

            image_url = get_spawn_image_url(message)
            if not image_url:
                log.warning("Spawn with no image URL")
                return

            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if not name:
                log.warning("Could not identify spawn — %s", image_url)
                return

            log.info("Auto-identified: %s (via %s)", name, method)

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log.info("Waiting %.2fs…", delay)
            await asyncio.sleep(delay)

            update_cooldown(message.channel.id)

            try:
                await message.channel.send(
                    f"That's **{name}**!",
                    reference=message, mention_author=False,
                )
            except discord.Forbidden:
                log.warning("No send permission in channel %s", message.channel.id)
            except discord.HTTPException as exc:
                log.error("Send failed: %s", exc)

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
    await aiohttp_web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web server on port %d", port)


# ── MAIN ───────────────────────────────────────────────────────────────

async def main() -> None:
    global http_session

    setup_openai()
    load_pokedex()

    connector = aiohttp.TCPConnector(limit=20)
    http_session = aiohttp.ClientSession(connector=connector)

    await start_web()
    await asyncio.sleep(2)

    log.info("Starting Discord bot…")
    try:
        await run_bot()
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
