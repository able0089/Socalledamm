import asyncio
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
import numpy as np
import onnxruntime as ort
from aiohttp import web as aiohttp_web
from PIL import Image

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
COOLDOWN  = float(os.environ.get("COOLDOWN",  "3.0"))

# ── ONNX CLASSIFIER ────────────────────────────────────────────────────

MODEL_DIR   = Path(__file__).parent / "model"
ONNX_PATH   = MODEL_DIR / "pokemon_cnn_v2.onnx"
LABELS_PATH = MODEL_DIR / "labels_v2.json"

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)

ort_session:  ort.InferenceSession | None = None
class_names:  list[str] = []


MODEL_ONNX_URL   = "https://raw.githubusercontent.com/senko-sleep/Poketwo-AutoNamer/main/model/pokemon_cnn_v2.onnx"
MODEL_LABELS_URL = "https://raw.githubusercontent.com/senko-sleep/Poketwo-AutoNamer/main/model/labels_v2.json"


def _download_model() -> None:
    import urllib.request
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if not LABELS_PATH.exists():
        log.info("Downloading labels…")
        urllib.request.urlretrieve(MODEL_LABELS_URL, LABELS_PATH)

    if not ONNX_PATH.exists():
        log.info("Downloading ONNX model (~45 MB)…")
        urllib.request.urlretrieve(MODEL_ONNX_URL, ONNX_PATH)
        log.info("Model downloaded")


def setup_classifier() -> None:
    global ort_session, class_names

    try:
        _download_model()
    except Exception as exc:
        log.error("Model download failed: %s — inference disabled", exc)
        return

    if not ONNX_PATH.exists():
        log.error("ONNX model not found at %s — inference disabled", ONNX_PATH)
        return

    with open(LABELS_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict):
        class_names = [data[k] for k in sorted(data, key=lambda x: int(x))]
    else:
        class_names = data

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.inter_op_num_threads = 1
    ort_session = ort.InferenceSession(
        str(ONNX_PATH),
        sess_options=opts,
        providers=["CPUExecutionProvider"],
    )
    log.info("ONNX model loaded: %d classes", len(class_names))


def _preprocess(img_bytes: bytes) -> np.ndarray:
    img = Image.open(__import__("io").BytesIO(img_bytes)).convert("RGB")
    img = img.resize((224, 224), Image.Resampling.BILINEAR)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = (arr - MEAN) / STD
    return np.expand_dims(np.transpose(arr, (2, 0, 1)), 0)


def classify_bytes(img_bytes: bytes) -> tuple[str | None, float]:
    if ort_session is None:
        return None, 0.0
    inp = _preprocess(img_bytes)
    logits = ort_session.run(None, {ort_session.get_inputs()[0].name: inp})[0][0]
    exp    = np.exp(logits - logits.max())
    probs  = exp / exp.sum()
    idx    = int(np.argmax(probs))
    prob   = float(probs[idx])
    name   = class_names[idx] if idx < len(class_names) else f"unknown_{idx}"
    return name, prob


# ── URL FAST PATH ──────────────────────────────────────────────────────

POKEDEX: dict[str, str] = {}
POKEDEX_PATH = Path(__file__).parent / "pokedex.json"
CDN_PATTERN  = re.compile(r"poketwo\.net/images/(\d+)\.", re.IGNORECASE)


def load_pokedex() -> None:
    global POKEDEX
    if POKEDEX_PATH.exists():
        with open(POKEDEX_PATH) as f:
            POKEDEX = json.load(f)
        log.info("Pokedex loaded: %d entries (fast-path)", len(POKEDEX))


def identify_from_url(image_url: str) -> tuple[str | None, str]:
    m = CDN_PATTERN.search(image_url)
    if m:
        name = POKEDEX.get(m.group(1))
        if name:
            return name, f"url/dex#{m.group(1)}"
    return None, ""


# ── SPAWN IDENTIFICATION ───────────────────────────────────────────────

CONFIDENCE_THRESHOLD = 0.40  # accept if model is at least 40% confident


async def identify(
    session: aiohttp.ClientSession, image_url: str
) -> tuple[str | None, str]:
    # Fast path: old-style CDN URL with dex ID
    name, method = identify_from_url(image_url)
    if name:
        return name, method

    # Download image
    try:
        async with session.get(
            image_url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                log.warning("Image download failed (HTTP %d)", r.status)
                return None, ""
            img_bytes = await r.read()
    except Exception as exc:
        log.error("Image download error: %s", exc)
        return None, ""

    # ONNX inference
    try:
        name, prob = classify_bytes(img_bytes)
        log.info("ONNX → %s (%.1f%%)", name, prob * 100)
        if name and prob >= CONFIDENCE_THRESHOLD:
            return name.title(), f"onnx/{prob*100:.0f}%"
        log.warning("Low confidence (%.1f%%) — skipping", prob * 100)
    except Exception as exc:
        log.error("ONNX inference error: %s", exc)

    return None, ""


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
            "Classifier: %s | Watching: %s",
            "READY" if ort_session else "DISABLED",
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
            log.info("USER CORRECTION: '%s'", correct_name)
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
            log.info("DEBUG:\n%s", "\n".join(lines))
            await message.channel.send("```\n" + "\n".join(lines) + "\n```", mention_author=False)
            return

        # ── !guess ─────────────────────────────────────────────────────
        if content.lower() == "!guess":
            ref = message.reference
            if not ref:
                await message.channel.send(
                    "Reply to a Poketwo spawn with `!guess`.", mention_author=False
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
            if not image_url:
                await message.channel.send("No image found in that message.", mention_author=False)
                return

            log.info("!guess — %s", image_url)
            assert http_session is not None
            name, method = await identify(http_session, image_url)

            if name:
                log.info("RESULT: %s (%s)", name, method)
                await message.channel.send(
                    f"That's **{name}**!",
                    reference=message, mention_author=False,
                )
            else:
                await message.channel.send(
                    "Couldn't identify that one. Use `!correct <name>` if you know.",
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

            log.info("Auto-identified: %s (%s)", name, method)

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
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

    setup_classifier()
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
