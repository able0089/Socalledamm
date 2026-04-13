import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
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

OWNER_ID       = int(os.environ.get("OWNER_ID", "1396815034247806999"))
POKETWO_BOT_ID = int(os.environ.get("POKETWO_BOT_ID", "716390085896962058"))

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

DELAY_MIN = float(os.environ.get("DELAY_MIN", "1.5"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "3.0"))
COOLDOWN  = float(os.environ.get("COOLDOWN",  "2.0"))

PREFIX = "pk!"

# ── DATA STORE ─────────────────────────────────────────────────────────

DATA_PATH = Path(__file__).parent / "data.json"
_data_lock = threading.Lock()

_DEFAULT_DATA: dict = {
    "guild_settings":   {},   # gid -> {rare_role, regional_role, rare_pokemon, regional_pokemon}
    "channel_settings": {},   # cid -> {naming, rareping, regionalpinging, shinyhunt, collectionping}
    "user_collections": {},   # uid -> [pokemon, ...]
    "user_shiny_hunts": {},   # uid -> pokemon
}

_data: dict = {}


def _load() -> None:
    global _data
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            _data = json.load(f)
        for k, v in _DEFAULT_DATA.items():
            _data.setdefault(k, v)
    else:
        _data = {k: dict(v) if isinstance(v, dict) else list(v) for k, v in _DEFAULT_DATA.items()}


def _save() -> None:
    with _data_lock:
        with open(DATA_PATH, "w") as f:
            json.dump(_data, f, indent=2)


def _guild_cfg(guild_id: int) -> dict:
    g = str(guild_id)
    if g not in _data["guild_settings"]:
        _data["guild_settings"][g] = {
            "rare_role": None, "regional_role": None,
            "rare_pokemon": [], "regional_pokemon": [],
        }
    return _data["guild_settings"][g]


def _ch_cfg(channel_id: int) -> dict:
    c = str(channel_id)
    if c not in _data["channel_settings"]:
        _data["channel_settings"][c] = {
            "naming": True, "rareping": True,
            "regionalpinging": True, "shinyhunt": True, "collectionping": True,
        }
    return _data["channel_settings"][c]


def _user_col(user_id: int) -> list:
    u = str(user_id)
    if u not in _data["user_collections"]:
        _data["user_collections"][u] = []
    return _data["user_collections"][u]


# ── DEFAULT RARE / REGIONAL LISTS ──────────────────────────────────────

DEFAULT_RARES = {
    "mewtwo","mew","lugia","ho-oh","celebi","kyogre","groudon","rayquaza",
    "jirachi","deoxys","dialga","palkia","giratina","arceus","victini",
    "reshiram","zekrom","kyurem","keldeo","meloetta","genesect",
    "xerneas","yveltal","zygarde","diancie","hoopa","volcanion",
    "cosmog","cosmoem","solgaleo","lunala","necrozma","magearna",
    "marshadow","zeraora","meltan","melmetal",
    "zacian","zamazenta","eternatus","kubfu","urshifu","zarude",
    "regieleki","regidrago","glastrier","spectrier","calyrex",
    "enamorus","koraidon","miraidon","wo-chien","chien-pao","ting-lu","chi-yu",
    "iron-valiant","iron-leaves","walking-wake","gouging-fire","raging-bolt",
    "iron-boulder","iron-crown","terapagos","pecharunt",
}

REGIONAL_SUFFIXES = ("-galar", "-alola", "-hisui", "-paldea")

REGIONAL_PREFIX_MAP = {
    "-galar":  "Galarian",
    "-alola":  "Alolan",
    "-hisui":  "Hisuian",
    "-paldea": "Paldean",
}

NAME_FIXES = {
    "farfetchd": "Farfetch'd",
    "sirfetchd":  "Sirfetch'd",
    "mr-mime":    "Mr. Mime",
    "mr-rime":    "Mr. Rime",
    "ho-oh":      "Ho-Oh",
    "porygon-z":  "Porygon-Z",
    "type-null":  "Type: Null",
    "jangmo-o":   "Jangmo-o",
    "hakamo-o":   "Hakamo-o",
    "kommo-o":    "Kommo-o",
    "wo-chien":   "Wo-Chien",
    "chien-pao":  "Chien-Pao",
    "ting-lu":    "Ting-Lu",
    "chi-yu":     "Chi-Yu",
    "flutter-mane": "Flutter Mane",
    "iron-valiant": "Iron Valiant",
    "iron-leaves":  "Iron Leaves",
    "iron-boulder": "Iron Boulder",
    "iron-crown":   "Iron Crown",
    "walking-wake": "Walking Wake",
    "gouging-fire": "Gouging Fire",
    "raging-bolt":  "Raging Bolt",
    "sandy-shocks": "Sandy Shocks",
    "scream-tail":  "Scream Tail",
    "brute-bonnet": "Brute Bonnet",
    "slither-wing": "Slither Wing",
    "roaring-moon": "Roaring Moon",
    "great-tusk":   "Great Tusk",
    "iron-treads":  "Iron Treads",
    "iron-moth":    "Iron Moth",
    "iron-hands":   "Iron Hands",
    "iron-jugulis": "Iron Jugulis",
    "iron-thorns":  "Iron Thorns",
    "iron-bundle":  "Iron Bundle",
    "nidoran-f":    "Nidoran♀",
    "nidoran-m":    "Nidoran♂",
    "mime-jr":      "Mime Jr.",
    "mr-rime":      "Mr. Rime",
    "flabebe":      "Flabébé",
}


def label_to_display(label: str) -> str:
    """Convert model label (e.g. 'farfetchd-galar') to display name ('Galarian Farfetch\\'d')."""
    label = label.lower()
    prefix = ""
    for suffix, pfx in REGIONAL_PREFIX_MAP.items():
        if label.endswith(suffix):
            prefix = pfx + " "
            label = label[: -len(suffix)]
            break

    # Remove other form suffixes for display
    for form in ("-zen", "-gmax", "-mega", "-primal"):
        if label.endswith(form):
            label = label[: -len(form)]
            break

    if label in NAME_FIXES:
        base = NAME_FIXES[label]
    else:
        base = " ".join(p.capitalize() for p in label.split("-"))

    return prefix + base


def normalize_query(name: str) -> str:
    """Lowercase, strip spaces and convert ' to nothing for matching."""
    return re.sub(r"['\s]", "", name.lower().replace("♀", "-f").replace("♂", "-m"))


def label_matches_query(label: str, query: str) -> bool:
    """Check if a model label matches a user-typed query."""
    q = normalize_query(query)
    lbl = normalize_query(label)
    disp = normalize_query(label_to_display(label))
    return q == lbl or q == disp or lbl.startswith(q) or disp.startswith(q)


def is_rare(label: str, guild_id: int) -> bool:
    cfg = _data["guild_settings"].get(str(guild_id), {})
    custom = cfg.get("rare_pokemon", [])
    if custom:
        return any(label_matches_query(label, x) for x in custom)
    return label.lower() in DEFAULT_RARES


def is_regional(label: str, guild_id: int) -> bool:
    cfg = _data["guild_settings"].get(str(guild_id), {})
    custom = cfg.get("regional_pokemon", [])
    if custom:
        return any(label_matches_query(label, x) for x in custom)
    return any(label.lower().endswith(s) for s in REGIONAL_SUFFIXES)


# ── ONNX CLASSIFIER ────────────────────────────────────────────────────

MODEL_DIR   = Path(__file__).parent / "model"
ONNX_PATH   = MODEL_DIR / "pokemon_cnn_v2.onnx"
LABELS_PATH = MODEL_DIR / "labels_v2.json"

MODEL_ONNX_URL   = "https://raw.githubusercontent.com/senko-sleep/Poketwo-AutoNamer/main/model/pokemon_cnn_v2.onnx"
MODEL_LABELS_URL = "https://raw.githubusercontent.com/senko-sleep/Poketwo-AutoNamer/main/model/labels_v2.json"

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)
CONFIDENCE_THRESHOLD = 0.40

ort_session: ort.InferenceSession | None = None
class_names: list[str] = []
_executor = ThreadPoolExecutor(max_workers=2)


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
        return
    with open(LABELS_PATH) as f:
        data = json.load(f)
    class_names = [data[k] for k in sorted(data, key=lambda x: int(x))] if isinstance(data, dict) else data
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.inter_op_num_threads = 1
    ort_session = ort.InferenceSession(
        str(ONNX_PATH), sess_options=opts, providers=["CPUExecutionProvider"]
    )
    log.info("ONNX model loaded: %d classes", len(class_names))


def _classify_sync(img_bytes: bytes) -> tuple[str | None, float]:
    if ort_session is None:
        return None, 0.0
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((224, 224), Image.Resampling.BILINEAR)
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - MEAN) / STD
    inp = np.expand_dims(np.transpose(arr, (2, 0, 1)), 0)
    logits = ort_session.run(None, {ort_session.get_inputs()[0].name: inp})[0][0]
    exp   = np.exp(logits - logits.max())
    probs = exp / exp.sum()
    idx   = int(np.argmax(probs))
    return class_names[idx] if idx < len(class_names) else None, float(probs[idx])


async def classify_async(img_bytes: bytes) -> tuple[str | None, float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _classify_sync, img_bytes)


# ── IDENTIFICATION ──────────────────────────────────────────────────────

CDN_PATTERN = re.compile(r"poketwo\.net/images/(\d+)\.", re.IGNORECASE)
POKEDEX: dict[str, str] = {}


def load_pokedex() -> None:
    global POKEDEX
    p = Path(__file__).parent / "pokedex.json"
    if p.exists():
        with open(p) as f:
            POKEDEX = json.load(f)
        log.info("Pokedex loaded: %d entries", len(POKEDEX))


def _url_fast_path(url: str) -> str | None:
    m = CDN_PATTERN.search(url)
    if m:
        return POKEDEX.get(m.group(1))
    return None


async def identify_spawn(
    session: aiohttp.ClientSession, image_url: str
) -> tuple[str | None, str]:
    """Returns (raw_label, method) or (None, '')."""
    name = _url_fast_path(image_url)
    if name:
        return name.lower(), "url"

    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None, ""
            img_bytes = await r.read()
    except Exception as exc:
        log.error("Image download error: %s", exc)
        return None, ""

    label, prob = await classify_async(img_bytes)
    log.info("ONNX → %s (%.1f%%)", label, prob * 100)
    if label and prob >= CONFIDENCE_THRESHOLD:
        return label, f"onnx/{prob*100:.0f}%"
    return None, ""


# ── HELPERS ─────────────────────────────────────────────────────────────

def get_spawn_image_url(msg: discord.Message) -> str | None:
    for emb in msg.embeds:
        if emb.image and emb.image.url:
            return emb.image.url
        if emb.thumbnail and emb.thumbnail.url:
            return emb.thumbnail.url
    for att in msg.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return att.url
    return None


def is_spawn_message(msg: discord.Message) -> bool:
    if msg.author.id != POKETWO_BOT_ID:
        return False
    for emb in msg.embeds:
        text = ((emb.title or "") + (emb.description or "")).lower()
        if "wild" in text and "pokémon" in text:
            return True
    return False


def parse_role(guild: discord.Guild, arg: str) -> discord.Role | None:
    """Accept @role mention or raw role ID."""
    m = re.search(r"\d{15,20}", arg)
    if m:
        return guild.get_role(int(m.group()))
    return None


def parse_user_id(arg: str) -> int | None:
    """Extract user ID from mention or raw number."""
    m = re.search(r"\d{15,20}", arg)
    return int(m.group()) if m else None


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def is_owner(user: discord.User | discord.Member) -> bool:
    return user.id == OWNER_ID


def pokemon_list_from_args(text: str) -> list[str]:
    """Parse comma/space-separated Pokemon names from user input."""
    return [p.strip().lower() for p in re.split(r"[,]+", text) if p.strip()]


# ── CHANNEL / GUILD STATE ───────────────────────────────────────────────

channel_last_action: dict[int, float] = {}
semaphore = asyncio.Semaphore(5)


def on_cooldown(channel_id: int) -> bool:
    return (time.time() - channel_last_action.get(channel_id, 0)) < COOLDOWN


def update_cooldown(channel_id: int) -> None:
    channel_last_action[channel_id] = time.time()


http_session: aiohttp.ClientSession | None = None

# ── BOT ─────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (%s) | Classifier: %s",
                 client.user, client.user.id if client.user else "?",
                 "READY" if ort_session else "DISABLED")

    @client.event
    async def on_message(msg: discord.Message) -> None:
        if msg.author.bot and msg.author.id != POKETWO_BOT_ID:
            return
        if msg.author.id == POKETWO_BOT_ID:
            await handle_spawn(msg)
            return
        await handle_command(msg, client)

    await client.start(TOKEN)


# ── COMMAND ROUTER ───────────────────────────────────────────────────────

async def handle_command(msg: discord.Message, client: discord.Client) -> None:
    if not msg.guild:
        return

    content = msg.content.strip()

    # Detect prefix: pk! or @mention
    mention_prefix = f"<@{client.user.id}>" if client.user else None
    mention_prefix2 = f"<@!{client.user.id}>" if client.user else None

    if content.lower().startswith(PREFIX):
        cmd_body = content[len(PREFIX):].strip()
    elif mention_prefix and content.startswith(mention_prefix):
        cmd_body = content[len(mention_prefix):].strip()
    elif mention_prefix2 and content.startswith(mention_prefix2):
        cmd_body = content[len(mention_prefix2):].strip()
    else:
        return

    parts = cmd_body.split(None, 1)
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1].strip() if len(parts) > 1 else ""

    # ── pk!ping / pk!help ─────────────────────────────────────────────
    if cmd in ("ping", "help"):
        await cmd_help(msg)

    # ── pk!cl ─────────────────────────────────────────────────────────
    elif cmd == "cl":
        await cmd_collection(msg, args)

    # ── pk!sh ─────────────────────────────────────────────────────────
    elif cmd == "sh":
        await cmd_shiny(msg, args)

    # ── pk!settings ───────────────────────────────────────────────────
    elif cmd == "settings":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_settings(msg, args)

    # ── pk!rares ──────────────────────────────────────────────────────
    elif cmd == "rares":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_rares(msg, args)

    # ── pk!regionals ──────────────────────────────────────────────────
    elif cmd == "regionals":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_regionals(msg, args)

    # ── pk!channelsettings ────────────────────────────────────────────
    elif cmd == "channelsettings":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_channelsettings(msg, args)

    # ── pk!admin ──────────────────────────────────────────────────────
    elif cmd == "admin":
        if not is_owner(msg.author):
            return await msg.channel.send("❌ Owner only.", delete_after=8)
        await cmd_admin(msg, args)

    # ── pk!debug ──────────────────────────────────────────────────────
    elif cmd == "debug":
        if not is_owner(msg.author):
            return await msg.channel.send("❌ Owner only.", delete_after=8)
        await cmd_debug(msg)

    # ── pk!guess ──────────────────────────────────────────────────────
    elif cmd == "guess":
        await cmd_guess(msg)

    # ── pk!correct ────────────────────────────────────────────────────
    elif cmd == "correct":
        log.info("USER CORRECTION: '%s'", args)
        await msg.channel.send(f"Got it! The correct Pokémon was **{args.title()}**.",
                               reference=msg, mention_author=False)


# ── COMMANDS ─────────────────────────────────────────────────────────────

async def cmd_help(msg: discord.Message) -> None:
    embed = discord.Embed(title="Pickel's Assistant — Help", color=0x5865F2)
    embed.add_field(name="Identification", value=(
        "`pk!guess` — reply to a spawn to identify it\n"
        "`pk!correct <name>` — report a wrong identification"
    ), inline=False)
    embed.add_field(name="Collection Pings", value=(
        "`pk!cl add <pokemon, ...>` — add Pokémon to your list\n"
        "`pk!cl remove <pokemon, ...>` — remove from list\n"
        "`pk!cl list` — view your collection list"
    ), inline=False)
    embed.add_field(name="Shiny Hunting", value=(
        "`pk!sh <pokemon>` — set your shiny hunt target (1 at a time)\n"
        "`pk!sh clear` — clear your hunt"
    ), inline=False)
    embed.add_field(name="Admin — Server Settings", value=(
        "`pk!settings rare-role @role` — set rare ping role\n"
        "`pk!settings regional-role @role` — set regional ping role\n"
        "`pk!rares <list>` — override rare Pokémon list\n"
        "`pk!regionals <list>` — override regional Pokémon list\n"
        "`pk!channelsettings` — view/toggle channel features"
    ), inline=False)
    await msg.channel.send(embed=embed)


async def cmd_collection(msg: discord.Message, args: str) -> None:
    uid = str(msg.author.id)
    sub_parts = args.split(None, 1)
    sub  = sub_parts[0].lower() if sub_parts else "list"
    rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    col = _user_col(msg.author.id)

    if sub == "add":
        if not rest:
            return await msg.channel.send("Usage: `pk!cl add <pokemon, ...>`")
        added = []
        for p in pokemon_list_from_args(rest):
            if p and p not in col:
                col.append(p)
                added.append(p)
        _save()
        await msg.channel.send(
            f"✅ Added to your collection: **{', '.join(t.title() for t in added)}**" if added
            else "Those Pokémon are already in your collection.",
            reference=msg, mention_author=False
        )

    elif sub == "remove":
        if not rest:
            return await msg.channel.send("Usage: `pk!cl remove <pokemon, ...>`")
        removed = []
        for p in pokemon_list_from_args(rest):
            if p in col:
                col.remove(p)
                removed.append(p)
        _save()
        await msg.channel.send(
            f"✅ Removed: **{', '.join(t.title() for t in removed)}**" if removed
            else "None of those were in your collection.",
            reference=msg, mention_author=False
        )

    elif sub == "list":
        if not col:
            return await msg.channel.send("Your collection list is empty. Use `pk!cl add <pokemon>`.")
        embed = discord.Embed(
            title=f"{msg.author.display_name}'s Collection Pings",
            description=", ".join(p.title() for p in col),
            color=0x57F287
        )
        await msg.channel.send(embed=embed)

    else:
        await msg.channel.send("Usage: `pk!cl add/remove/list <pokemon>`")


async def cmd_shiny(msg: discord.Message, args: str) -> None:
    uid = str(msg.author.id)
    if not args or args.lower() == "clear":
        _data["user_shiny_hunts"].pop(uid, None)
        _save()
        return await msg.channel.send("✅ Shiny hunt cleared.", reference=msg, mention_author=False)

    pokemon = args.strip().lower()
    prev = _data["user_shiny_hunts"].get(uid)
    _data["user_shiny_hunts"][uid] = pokemon
    _save()
    msg_text = f"✅ Shiny hunt set to **{pokemon.title()}**!"
    if prev:
        msg_text += f" (replaced **{prev.title()}**)"
    await msg.channel.send(msg_text, reference=msg, mention_author=False)


async def cmd_settings(msg: discord.Message, args: str) -> None:
    parts = args.split(None, 1)
    if len(parts) < 2:
        return await msg.channel.send("Usage: `pk!settings rare-role @role` or `pk!settings regional-role @role`")
    key, value = parts[0].lower(), parts[1].strip()
    cfg = _guild_cfg(msg.guild.id)

    if key in ("rare-role", "regional-role"):
        role = parse_role(msg.guild, value)
        if not role:
            return await msg.channel.send("❌ Role not found. Use a mention or role ID.")
        field = "rare_role" if key == "rare-role" else "regional_role"
        cfg[field] = str(role.id)
        _save()
        label = "Rare" if key == "rare-role" else "Regional"
        await msg.channel.send(f"✅ {label} ping role set to {role.mention}.", reference=msg, mention_author=False)
    else:
        await msg.channel.send("Unknown setting. Available: `rare-role`, `regional-role`")


async def cmd_rares(msg: discord.Message, args: str) -> None:
    if not args:
        cfg = _guild_cfg(msg.guild.id)
        cfg["rare_pokemon"] = []
        _save()
        return await msg.channel.send("✅ Rare list reset to defaults.")
    names = pokemon_list_from_args(args)
    _guild_cfg(msg.guild.id)["rare_pokemon"] = names
    _save()
    await msg.channel.send(
        f"✅ Rare Pokémon list set to: **{', '.join(n.title() for n in names)}**",
        reference=msg, mention_author=False
    )


async def cmd_regionals(msg: discord.Message, args: str) -> None:
    if not args:
        cfg = _guild_cfg(msg.guild.id)
        cfg["regional_pokemon"] = []
        _save()
        return await msg.channel.send("✅ Regional list reset to defaults (auto-detect by form).")
    names = pokemon_list_from_args(args)
    _guild_cfg(msg.guild.id)["regional_pokemon"] = names
    _save()
    await msg.channel.send(
        f"✅ Regional Pokémon list set to: **{', '.join(n.title() for n in names)}**",
        reference=msg, mention_author=False
    )


CHANNEL_TOGGLES = {
    "naming":          "Auto Naming",
    "rareping":        "Rare Pings",
    "regionalpinging": "Regional Pings",
    "shinyhunt":       "Shiny Hunt Pings",
    "collectionping":  "Collection Pings",
}


async def cmd_channelsettings(msg: discord.Message, args: str) -> None:
    ch   = msg.channel
    cfg  = _ch_cfg(ch.id)
    parts = args.split()

    if not args:
        embed = discord.Embed(
            title=f"#{ch.name} — Channel Settings",
            color=0xFEE75C
        )
        for key, label in CHANNEL_TOGGLES.items():
            val = cfg.get(key, True)
            embed.add_field(name=label, value="🟢 On" if val else "🔴 Off", inline=True)
        embed.set_footer(text="Toggle with: pk!channelsettings <setting> true/false")
        return await msg.channel.send(embed=embed)

    if len(parts) < 2:
        opts = " | ".join(CHANNEL_TOGGLES.keys())
        return await msg.channel.send(f"Usage: `pk!channelsettings <{opts}> true/false`")

    key, val_str = parts[0].lower(), parts[1].lower()
    if key not in CHANNEL_TOGGLES:
        return await msg.channel.send(f"Unknown setting. Options: {', '.join(CHANNEL_TOGGLES.keys())}")
    if val_str not in ("true", "false", "on", "off", "1", "0", "yes", "no"):
        return await msg.channel.send("Value must be `true` or `false`.")

    enabled = val_str in ("true", "on", "1", "yes")
    cfg[key] = enabled
    _save()
    label = CHANNEL_TOGGLES[key]
    await msg.channel.send(
        f"{'🟢' if enabled else '🔴'} **{label}** is now {'on' if enabled else 'off'} in this channel.",
        reference=msg, mention_author=False
    )


async def cmd_admin(msg: discord.Message, args: str) -> None:
    parts = args.split()
    if len(parts) < 3 or parts[0].lower() != "view":
        return await msg.channel.send(
            "Usage:\n"
            "`pk!admin view cl @user/id` — view collection list\n"
            "`pk!admin view sh @user/id` — view shiny hunt\n"
            "`pk!admin view stats` — overall stats"
        )

    sub  = parts[1].lower()
    rest = " ".join(parts[2:])

    if sub == "stats":
        cols  = sum(len(v) for v in _data["user_collections"].values())
        hunts = len(_data["user_shiny_hunts"])
        guilds = len(_data["guild_settings"])
        await msg.channel.send(
            f"**Bot Stats**\nUsers with collections: {len(_data['user_collections'])}\n"
            f"Total collection entries: {cols}\nActive shiny hunts: {hunts}\nConfigured guilds: {guilds}"
        )
        return

    uid = parse_user_id(rest)
    if not uid:
        return await msg.channel.send("❌ Couldn't parse user ID.")

    if sub == "cl":
        col = _data["user_collections"].get(str(uid), [])
        if not col:
            return await msg.channel.send(f"User `{uid}` has no collection pings set.")
        embed = discord.Embed(
            title=f"Collection list for {uid}",
            description=", ".join(p.title() for p in col),
            color=0x57F287
        )
        await msg.channel.send(embed=embed)

    elif sub == "sh":
        hunt = _data["user_shiny_hunts"].get(str(uid))
        if not hunt:
            return await msg.channel.send(f"User `{uid}` has no active shiny hunt.")
        await msg.channel.send(f"User `{uid}` is hunting: **{hunt.title()}**")

    else:
        await msg.channel.send("Unknown subcommand. Use `cl` or `sh`.")


async def cmd_debug(msg: discord.Message) -> None:
    ref = msg.reference
    if not ref:
        return await msg.channel.send("Reply to a spawn message with `pk!debug`.")
    try:
        target = (
            ref.resolved if isinstance(ref.resolved, discord.Message)
            else await msg.channel.fetch_message(ref.message_id)
        )
    except Exception as exc:
        return await msg.channel.send(f"Couldn't fetch: {exc}")
    lines = [f"Author: {target.author} ({target.author.id})", f"Embeds: {len(target.embeds)}"]
    for i, emb in enumerate(target.embeds):
        lines += [
            f"[{i}] title={emb.title!r}",
            f"[{i}] description={str(emb.description or '')[:80]!r}",
            f"[{i}] image={emb.image.url if emb.image else None}",
            f"[{i}] thumbnail={emb.thumbnail.url if emb.thumbnail else None}",
        ]
    log.info("DEBUG:\n%s", "\n".join(lines))
    await msg.channel.send("```\n" + "\n".join(lines) + "\n```")


async def cmd_guess(msg: discord.Message) -> None:
    ref = msg.reference
    if not ref:
        return await msg.channel.send("Reply to a Poketwo spawn with `pk!guess`.")
    try:
        target = (
            ref.resolved if isinstance(ref.resolved, discord.Message)
            else await msg.channel.fetch_message(ref.message_id)
        )
    except Exception as exc:
        return await msg.channel.send(f"Couldn't fetch that message: {exc}")

    image_url = get_spawn_image_url(target)
    if not image_url:
        return await msg.channel.send("No image found in that message.")

    assert http_session is not None
    label, method = await identify_spawn(http_session, image_url)
    if label:
        display = label_to_display(label)
        log.info("!guess → %s (%s)", display, method)
        await msg.channel.send(f"That's **{display}**!", reference=msg, mention_author=False)
    else:
        await msg.channel.send(
            "Couldn't identify that one. Use `pk!correct <name>` if you know.",
            reference=msg, mention_author=False
        )


# ── SPAWN HANDLER ─────────────────────────────────────────────────────────

async def handle_spawn(msg: discord.Message) -> None:
    if not is_spawn_message(msg):
        return
    if not msg.guild:
        return
    if WATCH_CHANNEL_IDS and msg.channel.id not in WATCH_CHANNEL_IDS:
        return

    ch_cfg = _ch_cfg(msg.channel.id)

    # Check if anything at all is enabled in this channel
    anything_on = (
        ch_cfg.get("naming", True) or
        ch_cfg.get("rareping", True) or
        ch_cfg.get("regionalpinging", True) or
        ch_cfg.get("shinyhunt", True) or
        ch_cfg.get("collectionping", True)
    )
    if not anything_on:
        return

    async with semaphore:
        if on_cooldown(msg.channel.id):
            log.info("Cooldown — skipping channel %s", msg.channel.id)
            return

        image_url = get_spawn_image_url(msg)
        if not image_url:
            return

        assert http_session is not None
        label, method = await identify_spawn(http_session, image_url)
        if not label:
            log.warning("Could not identify spawn in #%s", msg.channel.id)
            return

        display = label_to_display(label)
        log.info("Spawn identified: %s via %s", display, method)

        # Build response
        lines: list[str] = []

        if ch_cfg.get("naming", True):
            lines.append(f"That's **{display}**!")

        guild_id = msg.guild.id

        # Rare ping
        if ch_cfg.get("rareping", True) and is_rare(label, guild_id):
            cfg    = _guild_cfg(guild_id)
            rid    = cfg.get("rare_role")
            if rid:
                role = msg.guild.get_role(int(rid))
                lines.append(f"🌟 Rare: {role.mention if role else f'<@&{rid}>'}")

        # Regional ping
        if ch_cfg.get("regionalpinging", True) and is_regional(label, guild_id):
            cfg = _guild_cfg(guild_id)
            rid = cfg.get("regional_role")
            if rid:
                role = msg.guild.get_role(int(rid))
                lines.append(f"🗺️ Regional: {role.mention if role else f'<@&{rid}>'}")

        # Shiny hunt pings
        if ch_cfg.get("shinyhunt", True):
            hunters = [
                uid for uid, hunt in _data["user_shiny_hunts"].items()
                if label_matches_query(label, hunt)
            ]
            if hunters:
                mentions = " ".join(f"<@{uid}>" for uid in hunters)
                lines.append(f"✨ Shiny hunt pings: {mentions}")

        # Collection pings
        if ch_cfg.get("collectionping", True):
            collectors = [
                uid for uid, col in _data["user_collections"].items()
                if any(label_matches_query(label, p) for p in col)
            ]
            if collectors:
                mentions = " ".join(f"<@{uid}>" for uid in collectors)
                lines.append(f"📦 Collection pings: {mentions}")

        if not lines:
            return

        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        await asyncio.sleep(delay)
        update_cooldown(msg.channel.id)

        try:
            await msg.channel.send(
                "\n".join(lines),
                reference=msg, mention_author=False,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True)
            )
        except discord.Forbidden:
            log.warning("No send permission in #%s", msg.channel.id)
        except discord.HTTPException as exc:
            log.error("Send failed: %s", exc)


# ── WEB SERVER ────────────────────────────────────────────────────────────

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


# ── MAIN ──────────────────────────────────────────────────────────────────

async def main() -> None:
    global http_session
    _load()
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
