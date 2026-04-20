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
from collections import deque
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

BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0") or "0")
# Response delay: randomised to look human and avoid bursts
DELAY_MIN = float(os.environ.get("DELAY_MIN", "1.5"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "3.0"))
# Per-channel cooldown (seconds) — prevents two responses in the same channel
# within this window even if spawns arrive back-to-back.
COOLDOWN  = float(os.environ.get("COOLDOWN", "2.0"))
# Global send-rate cap: max messages across ALL channels per rolling window
GLOBAL_RATE_LIMIT     = int(os.environ.get("GLOBAL_RATE_LIMIT", "4"))    # messages
GLOBAL_RATE_WINDOW    = float(os.environ.get("GLOBAL_RATE_WINDOW", "6")) # seconds
# How long to pause when Discord tells us to slow down (floor; actual uses retry_after)
RATE_LIMIT_BACKOFF    = float(os.environ.get("RATE_LIMIT_BACKOFF", "2.0"))

PREFIX = "pk!"

# ── DATA STORE ─────────────────────────────────────────────────────────

DATA_PATH = Path(__file__).parent / "data.json"
_data_lock = threading.Lock()

_DEFAULT_DATA: dict = {
    "guild_settings":   {},
    "channel_settings": {},
    "user_collections": {},
    "user_shiny_hunts": {},
}
_data: dict = {}


def _load() -> None:
    global _data
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            _data = json.load(f)
        for k, v in _DEFAULT_DATA.items():
            _data.setdefault(k, type(v)())
    else:
        _data = {k: type(v)() for k, v in _DEFAULT_DATA.items()}


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


# ── IDENTIFICATION LOG (in-memory, last 50) ────────────────────────────

recent_ids: deque = deque(maxlen=50)  # (timestamp, label, display, channel_id, method)

# ── DISPLAY NAME MAPPING ───────────────────────────────────────────────
# Explicit overrides so every label matches Poketwo's exact naming.
# Fallback logic handles simple regionals automatically.

LABEL_TO_DISPLAY: dict[str, str] = {
    # ── Regionals with double form (Galarian Zen etc.) ─────────────────
    "darmanitan-zen-galar":     "Galarian Zen Darmanitan",
    "darmanitan-galar":         "Galarian Darmanitan",
    "darmanitan-zen":           "Zen Darmanitan",
    # ── Oricorio ───────────────────────────────────────────────────────
    "oricorio-baile":           "Baile Oricorio",
    "oricorio-pom-pom":         "Pom-Pom Oricorio",
    "oricorio-pau":             "Pa\u2019u Oricorio",
    "oricorio-sensu":           "Sensu Oricorio",
    # ── Tatsugiri ──────────────────────────────────────────────────────
    "tatsugiri-droopy":         "Droopy Tatsugiri",
    "tatsugiri-stretchy":       "Stretchy Tatsugiri",
    "tatsugiri-curly":          "Curly Tatsugiri",
    # ── Lycanroc ───────────────────────────────────────────────────────
    "lycanroc-midday":          "Midday Lycanroc",
    "lycanroc-midnight":        "Midnight Lycanroc",
    "lycanroc-dusk":            "Dusk Lycanroc",
    # ── Rotom ──────────────────────────────────────────────────────────
    "rotom-heat":               "Heat Rotom",
    "rotom-wash":               "Wash Rotom",
    "rotom-frost":              "Frost Rotom",
    "rotom-fan":                "Fan Rotom",
    "rotom-mow":                "Mow Rotom",
    # ── Calyrex ────────────────────────────────────────────────────────
    "calyrex-ice":              "Ice Rider Calyrex",
    "calyrex-shadow":           "Shadow Rider Calyrex",
    # ── Kyurem ─────────────────────────────────────────────────────────
    "kyurem-black":             "Black Kyurem",
    "kyurem-white":             "White Kyurem",
    # ── Necrozma ───────────────────────────────────────────────────────
    "necrozma-dusk-mane":       "Dusk Mane Necrozma",
    "necrozma-dawn-wings":      "Dawn Wings Necrozma",
    # ── Forces of Nature ───────────────────────────────────────────────
    "tornadus-therian":         "Therian Tornadus",
    "thundurus-therian":        "Therian Thundurus",
    "landorus-therian":         "Therian Landorus",
    "enamorus-therian":         "Therian Enamorus",
    # ── Deoxys ─────────────────────────────────────────────────────────
    "deoxys-normal":            "Normal Deoxys",
    "deoxys-attack":            "Attack Deoxys",
    "deoxys-defense":           "Defense Deoxys",
    "deoxys-speed":             "Speed Deoxys",
    # ── Giratina / Shaymin / misc legendary forms ──────────────────────
    "giratina-origin":          "Origin Giratina",
    "shaymin-sky":              "Sky Shaymin",
    "hoopa-unbound":            "Unbound Hoopa",
    "keldeo-resolute":          "Resolute Keldeo",
    "meloetta-pirouette":       "Pirouette Meloetta",
    "zygarde-10":               "10% Zygarde",
    "zygarde-complete":         "Complete Zygarde",
    "zygarde-cell":             "Zygarde Cell",
    "zygarde-core":             "Zygarde Core",
    # ── Origin forms ───────────────────────────────────────────────────
    "dialga-origin":            "Origin Dialga",
    "palkia-origin":            "Origin Palkia",
    # ── Urshifu ────────────────────────────────────────────────────────
    "urshifu-rapid-strike":     "Rapid Strike Urshifu",
    # ── Zacian / Zamazenta ─────────────────────────────────────────────
    "zacian-crowned":           "Crowned Sword Zacian",
    "zamazenta-crowned":        "Crowned Shield Zamazenta",
    # ── Toxtricity ─────────────────────────────────────────────────────
    "toxtricity-amped":         "Amped Toxtricity",
    "toxtricity-low-key":       "Low Key Toxtricity",
    # ── Wishiwashi / Aegislash ─────────────────────────────────────────
    "wishiwashi-school":        "School Wishiwashi",
    "aegislash-blade":          "Blade Aegislash",
    # ── Palafin ────────────────────────────────────────────────────────
    "palafin-hero":             "Hero Palafin",
    # ── Ursaluna ───────────────────────────────────────────────────────
    "ursaluna-bloodmoon":       "Blood Moon Ursaluna",
    # ── Wormadam ───────────────────────────────────────────────────────
    "wormadam-sandy":           "Sandy Wormadam",
    "wormadam-trash":           "Trash Wormadam",
    # ── Castform ───────────────────────────────────────────────────────
    "castform-sunny":           "Sunny Castform",
    "castform-rainy":           "Rainy Castform",
    "castform-snowy":           "Snowy Castform",
    # ── Cramorant ──────────────────────────────────────────────────────
    "cramorant-gulping":        "Gulping Cramorant",
    "cramorant-gorging":        "Gorging Cramorant",
    # ── Morpeko ────────────────────────────────────────────────────────
    "morpeko-hangry":           "Hangry Morpeko",
    # ── Eiscue ─────────────────────────────────────────────────────────
    "eiscue-noice":             "Noice Face Eiscue",
    # ── Ogerpon ────────────────────────────────────────────────────────
    "ogerpon-wellspring-mask":  "Wellspring Mask Ogerpon",
    "ogerpon-hearthflame-mask": "Hearthflame Mask Ogerpon",
    "ogerpon-cornerstone-mask": "Cornerstone Mask Ogerpon",
    # ── Tauros Paldean breeds ──────────────────────────────────────────
    "tauros-paldea-combat-breed": "Combat Breed Tauros",
    "tauros-paldea-blaze-breed":  "Blaze Breed Tauros",
    "tauros-paldea-aqua-breed":   "Aqua Breed Tauros",
    # ── Terapagos ──────────────────────────────────────────────────────
    "terapagos-terastal":       "Terastal Terapagos",
    # ── Maushold ───────────────────────────────────────────────────────
    "maushold-family-of-four":  "Family of Four Maushold",
    "maushold-family-of-three": "Family of Three Maushold",
    # ── Dudunsparce ────────────────────────────────────────────────────
    "dudunsparce-three-segment": "Three-Segment Dudunsparce",
    # ── Squawkabilly ───────────────────────────────────────────────────
    "squawkabilly-blue-plumage":   "Blue Plumage Squawkabilly",
    "squawkabilly-yellow-plumage": "Yellow Plumage Squawkabilly",
    "squawkabilly-white-plumage":  "White Plumage Squawkabilly",
    # ── Basculin ───────────────────────────────────────────────────────
    "basculin-red-striped":     "Red-Striped Basculin",
    "basculin-blue-striped":    "Blue-Striped Basculin",
    "basculin-white-striped":   "White-Striped Basculin",
    # ── Gender forms ───────────────────────────────────────────────────
    "basculegion-male":         "Male Basculegion",
    "basculegion-female":       "Female Basculegion",
    "indeedee-male":            "Male Indeedee",
    "indeedee-female":          "Female Indeedee",
    "meowstic-male":            "Male Meowstic",
    "meowstic-female":          "Female Meowstic",
    "pyroar-female":            "Female Pyroar",
    "frillish-female":          "Female Frillish",
    "jellicent-female":         "Female Jellicent",
    "hippopotas-female":        "Female Hippopotas",
    "hippowdon-female":         "Female Hippowdon",
    "unfezant-female":          "Female Unfezant",
    "oinkologne-female":        "Female Oinkologne",
    "oinkologne-male":          "Male Oinkologne",
    # ── Misc ───────────────────────────────────────────────────────────
    "greninja-ash":             "Ash-Greninja",
    "zarude-dada":              "Dada Zarude",
    "gimmighoul-roaming":       "Roaming Form Gimmighoul",
    "partner-pikachu":          "Partner Pikachu",
    "partner-eevee":            "Partner Eevee",
    "spiky-eared_pichu":        "Spiky-Eared Pichu",
    "busted-mimikyu":           "Busted Mimikyu",
    "high-speed_flight_configuration_genesect": "High-Speed Flight Genesect",
    # ── Special characters ─────────────────────────────────────────────
    "nidoran-f":  "Nidoran\u2640",
    "nidoran-m":  "Nidoran\u2642",
    "farfetchd":  "Farfetch\u2019d",
    "farfetchd-galar": "Galarian Farfetch\u2019d",
    "sirfetchd":  "Sirfetch\u2019d",
    "ho-oh":      "Ho-Oh",
    "mr-mime":    "Mr. Mime",
    "mr-mime-galar": "Galarian Mr. Mime",
    "mr-rime":    "Mr. Rime",
    "mime-jr":    "Mime Jr.",
    "porygon-z":  "Porygon-Z",
    "type-null":  "Type: Null",
    "jangmo-o":   "Jangmo-o",
    "hakamo-o":   "Hakamo-o",
    "kommo-o":    "Kommo-o",
    "wo-chien":   "Wo-Chien",
    "chien-pao":  "Chien-Pao",
    "ting-lu":    "Ting-Lu",
    "chi-yu":     "Chi-Yu",
    "tapu-koko":  "Tapu Koko",
    "tapu-lele":  "Tapu Lele",
    "tapu-bulu":  "Tapu Bulu",
    "tapu-fini":  "Tapu Fini",
    "flutter-mane":   "Flutter Mane",
    "iron-valiant":   "Iron Valiant",
    "iron-leaves":    "Iron Leaves",
    "iron-boulder":   "Iron Boulder",
    "iron-crown":     "Iron Crown",
    "iron-treads":    "Iron Treads",
    "iron-hands":     "Iron Hands",
    "iron-moth":      "Iron Moth",
    "iron-jugulis":   "Iron Jugulis",
    "iron-thorns":    "Iron Thorns",
    "iron-bundle":    "Iron Bundle",
    "walking-wake":   "Walking Wake",
    "gouging-fire":   "Gouging Fire",
    "raging-bolt":    "Raging Bolt",
    "sandy-shocks":   "Sandy Shocks",
    "scream-tail":    "Scream Tail",
    "brute-bonnet":   "Brute Bonnet",
    "slither-wing":   "Slither Wing",
    "roaring-moon":   "Roaring Moon",
    "great-tusk":     "Great Tusk",
    "flabebe":        "Flab\u00e9b\u00e9",
}

REGIONAL_PREFIX_MAP = {
    "-galar":  "Galarian",
    "-alola":  "Alolan",
    "-hisui":  "Hisuian",
    "-paldea": "Paldean",
}


def label_to_display(label: str) -> str:
    """Convert model label to Poketwo display name."""
    label = label.lower().strip()

    # Explicit override table first
    if label in LABEL_TO_DISPLAY:
        return LABEL_TO_DISPLAY[label]

    # Regional prefix fallback (e.g. sandshrew-alola → Alolan Sandshrew)
    for suffix, prefix in REGIONAL_PREFIX_MAP.items():
        if label.endswith(suffix):
            base = label[: -len(suffix)]
            # Check if the base itself is in the override table
            base_display = LABEL_TO_DISPLAY.get(base) or base.replace("-", " ").title()
            return f"{prefix} {base_display}"

    # Generic: replace hyphens, title case
    return label.replace("-", " ").replace("_", " ").title()


def normalize_query(name: str) -> str:
    return re.sub(r"[\s_\-'\u2019\u2640\u2642]", "", name.lower())


def label_matches_query(label: str, query: str) -> bool:
    q    = normalize_query(query)
    lbl  = normalize_query(label)
    disp = normalize_query(label_to_display(label))
    return q in (lbl, disp) or lbl.startswith(q) or disp.startswith(q)


# ── DEFAULT RARE / REGIONAL LISTS ──────────────────────────────────────

DEFAULT_RARES: set[str] = {
    # Gen 1
    "mewtwo","mew","articuno","zapdos","moltres",
    "articuno-galar","zapdos-galar","moltres-galar",
    # Gen 2
    "raikou","entei","suicune","lugia","ho-oh","celebi",
    # Gen 3
    "regirock","regice","registeel","latias","latios",
    "kyogre","groudon","rayquaza","jirachi",
    "deoxys","deoxys-normal","deoxys-attack","deoxys-defense","deoxys-speed",
    # Gen 4
    "uxie","mesprit","azelf","dialga","palkia","dialga-origin","palkia-origin",
    "heatran","regigigas","giratina","giratina-origin","cresselia",
    "phione","manaphy","darkrai","shaymin","shaymin-sky","arceus",
    # Gen 5
    "victini","cobalion","terrakion","virizion",
    "tornadus","tornadus-therian","thundurus","thundurus-therian",
    "reshiram","zekrom","landorus","landorus-therian",
    "kyurem","kyurem-black","kyurem-white",
    "keldeo","keldeo-resolute","meloetta","meloetta-pirouette",
    "genesect","high-speed_flight_configuration_genesect",
    # Gen 6
    "xerneas","xerneas-neutral","xerneas-active","yveltal",
    "zygarde","zygarde-10","zygarde-complete","zygarde-cell","zygarde-core",
    "diancie","hoopa","hoopa-unbound","volcanion",
    # Gen 7
    "type-null","silvally",
    "tapu-koko","tapu-lele","tapu-bulu","tapu-fini",
    "cosmog","cosmoem","solgaleo","lunala",
    "necrozma","necrozma-dusk-mane","necrozma-dawn-wings",
    "nihilego","buzzwole","pheromosa","xurkitree","celesteela",
    "kartana","guzzlord","poipole","naganadel","stakataka","blacephalon",
    "magearna","marshadow","zeraora","meltan","melmetal",
    # Gen 8
    "zacian","zacian-crowned","zamazenta","zamazenta-crowned","eternatus",
    "kubfu","urshifu","urshifu-rapid-strike",
    "regieleki","regidrago","glastrier","spectrier",
    "calyrex","calyrex-ice","calyrex-shadow",
    "enamorus","enamorus-therian","zarude","zarude-dada",
    # Gen 9
    "koraidon","miraidon",
    "wo-chien","ting-lu","chien-pao","chi-yu",
    "munkidori","okidogi","fezandipiti",
    "ogerpon","ogerpon-wellspring-mask","ogerpon-hearthflame-mask","ogerpon-cornerstone-mask",
    "terapagos","terapagos-terastal","pecharunt",
}

DEFAULT_REGIONALS: set[str] = {
    # Alolan forms
    "diglett-alola","dugtrio-alola","exeggutor-alola","geodude-alola",
    "golem-alola","graveler-alola","grimer-alola","marowak-alola",
    "meowth-alola","muk-alola","ninetales-alola","persian-alola",
    "raichu-alola","raticate-alola","rattata-alola","sandshrew-alola",
    "sandslash-alola","vulpix-alola",
    # Galarian forms
    "corsola-galar","darmanitan-galar","darmanitan-zen-galar","darumaka-galar",
    "farfetchd-galar","linoone-galar","meowth-galar","mr-mime-galar",
    "ponyta-galar","rapidash-galar","slowbro-galar","slowking-galar",
    "slowpoke-galar","stunfisk-galar","weezing-galar","yamask-galar",
    "zigzagoon-galar",
    # Hisuian forms
    "arcanine-hisui","avalugg-hisui","braviary-hisui","decidueye-hisui",
    "electrode-hisui","goodra-hisui","growlithe-hisui","lilligant-hisui",
    "qwilfish-hisui","samurott-hisui","sliggoo-hisui","sneasel-hisui",
    "typhlosion-hisui","voltorb-hisui","zoroark-hisui","zorua-hisui",
    # Paldean forms & breeds
    "wooper-paldea",
    "tauros-paldea-combat-breed","tauros-paldea-blaze-breed","tauros-paldea-aqua-breed",
    # Regional evolutions (no form suffix)
    "cursola","overqwil","runerigus","perrserker",
    "basculegion","basculegion-male","basculegion-female",
    "sneasler","mr-rime","obstagoon","sirfetchd",
}


def is_rare(label: str, _guild_id: int = 0) -> bool:
    return label.lower().replace("_", "-") in DEFAULT_RARES


def is_regional(label: str, _guild_id: int = 0) -> bool:
    return label.lower() in DEFAULT_REGIONALS


def find_all_forms(species: str) -> list[str]:
    """Return all model labels for a species including every form.
    e.g. 'tatsugiri' → ['tatsugiri', 'tatsugiri-droopy', 'tatsugiri-stretchy']
    """
    q = normalize_query(species)
    results = []
    for lbl in class_names:
        lbl_norm = normalize_query(lbl)
        if lbl_norm == q or lbl_norm.startswith(q + "-") or lbl_norm.startswith(q + "_"):
            results.append(lbl)
    return results


def _resolve_pokemon_query(query: str) -> tuple[list[str], list[str]]:
    """Resolve a single query string to (matched_labels, invalid_terms).
    Supports 'all <species>' expansion.
    """
    query = query.strip().lower()
    if query.startswith("all "):
        species = query[4:].strip()
        forms = find_all_forms(species)
        if forms:
            return forms, []
        return [], [query]
    matched = next((lbl for lbl in class_names if label_matches_query(lbl, query)), None)
    if matched:
        return [matched], []
    return [], [query]




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
        log.error("Model download failed: %s", exc)
        return
    if not ONNX_PATH.exists():
        return
    with open(LABELS_PATH) as f:
        data = json.load(f)
    class_names = [data[k] for k in sorted(data, key=lambda x: int(x))] if isinstance(data, dict) else data
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.inter_op_num_threads = 1
    ort_session = ort.InferenceSession(str(ONNX_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
    log.info("ONNX model loaded: %d classes", len(class_names))


def _classify_sync(img_bytes: bytes) -> tuple[str | None, float]:
    if ort_session is None:
        return None, 0.0
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((224, 224), Image.Resampling.BILINEAR)
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - MEAN) / STD
    inp = np.expand_dims(np.transpose(arr, (2, 0, 1)), 0)
    logits = ort_session.run(None, {ort_session.get_inputs()[0].name: inp})[0][0]
    exp = np.exp(logits - logits.max()); probs = exp / exp.sum()
    idx = int(np.argmax(probs))
    return (class_names[idx] if idx < len(class_names) else None), float(probs[idx])


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


def _url_fast_path(url: str) -> str | None:
    m = CDN_PATTERN.search(url)
    return POKEDEX.get(m.group(1)) if m else None


async def identify_spawn(session: aiohttp.ClientSession, image_url: str) -> tuple[str | None, str]:
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


async def identify_from_url_direct(session: aiohttp.ClientSession, image_url: str) -> tuple[str | None, float]:
    """Used by admin identify command — returns (label, confidence)."""
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None, 0.0
            img_bytes = await r.read()
    except Exception as exc:
        return None, 0.0
    return await classify_async(img_bytes)


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
    m = re.search(r"\d{15,20}", arg)
    return guild.get_role(int(m.group())) if m else None


def parse_user_id(arg: str) -> int | None:
    m = re.search(r"\d{15,20}", arg)
    return int(m.group()) if m else None


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def is_owner(user: discord.User | discord.Member) -> bool:
    return user.id == OWNER_ID


def pokemon_list_from_args(text: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,]+", text) if p.strip()]


# ── CHANNEL STATE & GLOBAL RATE LIMITER ────────────────────────────────

channel_last_action: dict[int, float] = {}

# Semaphore: at most 2 spawns processed concurrently.
# With DELAY_MIN=1.5 s this caps burst throughput well below Discord's
# per-bot global limit of ~50 msg/s while leaving headroom for commands.
semaphore = asyncio.Semaphore(2)

http_session: aiohttp.ClientSession | None = None


class GlobalRateLimiter:
    """Token-bucket style limiter: allows at most `rate` sends per `per` seconds
    across the entire bot, independent of channel.  Any coroutine that wants to
    send a message must acquire a slot first; if the bucket is full it waits.
    """

    def __init__(self, rate: int, per: float) -> None:
        self._rate = rate          # max tokens (messages) in the window
        self._per  = per           # window length in seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Drop timestamps outside the rolling window
            while self._timestamps and now - self._timestamps[0] >= self._per:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._rate:
                # Wait until the oldest slot expires
                wait_for = self._per - (now - self._timestamps[0])
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                # Re-prune after sleeping
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._per:
                    self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


# Single global instance — shared by handle_spawn and everything that sends
_global_rl: GlobalRateLimiter | None = None


def on_cooldown(channel_id: int) -> bool:
    return (time.time() - channel_last_action.get(channel_id, 0)) < COOLDOWN


def update_cooldown(channel_id: int) -> None:
    channel_last_action[channel_id] = time.time()


async def safe_send(
    channel: discord.TextChannel,
    content: str,
    *,
    reference: discord.Message | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
    max_retries: int = 3,
) -> None:
    """Send a message with automatic 429 retry logic and global rate-limit gating.

    Waits for the global token-bucket slot first, then attempts the send.
    On HTTP 429 it sleeps for ``retry_after`` (or RATE_LIMIT_BACKOFF as a
    floor) before re-acquiring a slot and retrying.  Other HTTP errors are
    logged and not retried.
    """
    if _global_rl is not None:
        await _global_rl.acquire()

    kwargs: dict = {"content": content}
    if reference is not None:
        kwargs["reference"] = reference
        kwargs["mention_author"] = False
    if allowed_mentions is not None:
        kwargs["allowed_mentions"] = allowed_mentions

    for attempt in range(1, max_retries + 1):
        try:
            await channel.send(**kwargs)
            return
        except discord.Forbidden:
            log.warning("No send permission in #%s — dropping message", channel.id)
            return
        except discord.HTTPException as exc:
            if exc.status == 429:
                # Discord's retry_after can be on the exception or in the response json
                retry_after: float = RATE_LIMIT_BACKOFF
                if hasattr(exc, "retry_after") and exc.retry_after:
                    retry_after = max(float(exc.retry_after), RATE_LIMIT_BACKOFF)
                log.warning(
                    "Rate limited on #%s (attempt %d/%d) — sleeping %.2fs",
                    channel.id, attempt, max_retries, retry_after,
                )
                await asyncio.sleep(retry_after)
                # Re-acquire a global slot after the sleep
                if _global_rl is not None:
                    await _global_rl.acquire()
            else:
                log.error("Send failed in #%s: %s", channel.id, exc)
                return
    log.error("Gave up sending to #%s after %d attempts", channel.id, max_retries)



async def _auto_backup(client: discord.Client) -> None:
    """Backup bot data to a Discord channel every 5 minutes."""
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(300)
        if not BACKUP_CHANNEL_ID:
            continue
        try:
            ch = client.get_channel(BACKUP_CHANNEL_ID)
            if not ch:
                continue
            payload = "BOT_DATA_BACKUP\n```json\n" + json.dumps(_data) + "\n```"
            async for m in ch.history(limit=30):
                if m.author.id == (client.user.id if client.user else 0) and m.content.startswith("BOT_DATA_BACKUP"):
                    await m.edit(content=payload)
                    break
            else:
                await ch.send(payload)
        except Exception as exc:
            log.error("Auto-backup failed: %s", exc)


async def _restore_from_channel(client: discord.Client) -> bool:
    """Restore data from Discord backup channel on startup."""
    if not BACKUP_CHANNEL_ID:
        return False
    try:
        ch = client.get_channel(BACKUP_CHANNEL_ID)
        if not ch:
            return False
        async for m in ch.history(limit=30):
            if m.author.id == (client.user.id if client.user else 0) and m.content.startswith("BOT_DATA_BACKUP"):
                raw = m.content
                start = raw.index("```json\n") + 8
                end = raw.rindex("\n```")
                global _data
                _data = json.loads(raw[start:end])
                for k, v in _DEFAULT_DATA.items():
                    _data.setdefault(k, type(v)())
                _save()
                log.info("Restored data from Discord backup channel")
                return True
    except Exception as exc:
        log.error("Restore failed: %s", exc)
    return False


# ── BOT ─────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info("Logged in as %s (%s) | Classifier: %s | Watching: %s",
                 client.user, client.user.id if client.user else "?",
                 "READY" if ort_session else "DISABLED",
                 WATCH_CHANNEL_IDS or "all channels")
        if not DATA_PATH.exists():
            log.info("No local data — attempting restore from backup channel...")
            await _restore_from_channel(client)
        asyncio.ensure_future(_auto_backup(client))

    @client.event
    async def on_message(msg: discord.Message) -> None:
        if msg.author.bot and msg.author.id != POKETWO_BOT_ID:
            return
        if msg.author.id == POKETWO_BOT_ID:
            await handle_spawn(msg)
        else:
            await handle_command(msg, client)

    await client.start(TOKEN)


# ── COMMAND ROUTER ───────────────────────────────────────────────────────

async def handle_command(msg: discord.Message, client: discord.Client) -> None:
    if not msg.guild:
        return
    content = msg.content.strip()
    uid_str = str(client.user.id) if client.user else ""
    if content.lower().startswith(PREFIX):
        cmd_body = content[len(PREFIX):].strip()
    elif uid_str and (content.startswith(f"<@{uid_str}>") or content.startswith(f"<@!{uid_str}>")):
        cmd_body = re.sub(rf"^<@!?{uid_str}>", "", content).strip()
    else:
        return

    parts = cmd_body.split(None, 1)
    cmd  = parts[0].lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("ping", "help"):
        await cmd_help(msg)
    elif cmd == "cl":
        await cmd_collection(msg, args)
    elif cmd == "sh":
        await cmd_shiny(msg, args)
    elif cmd == "settings":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_settings(msg, args)
    elif cmd == "channelsettings":
        if not is_admin(msg.author):
            return await msg.channel.send("❌ Administrator permission required.", delete_after=8)
        await cmd_channelsettings(msg, args)
    elif cmd == "admin":
        if not is_owner(msg.author):
            return await msg.channel.send("❌ Owner only.", delete_after=8)
        await cmd_admin(msg, args, client)
    elif cmd == "debug":
        if not is_owner(msg.author):
            return await msg.channel.send("❌ Owner only.", delete_after=8)
        await cmd_debug(msg)
    elif cmd == "guess":
        await cmd_guess(msg)
    elif cmd == "correct":
        log.info("USER CORRECTION: '%s'", args)
        await msg.channel.send(f"Got it! The correct Pokémon was **{args.title()}**.",
                               reference=msg, mention_author=False)


# ── USER COMMANDS ─────────────────────────────────────────────────────────

async def cmd_help(msg: discord.Message) -> None:
    embed = discord.Embed(title="Pickel\u2019s Assistant \u2014 Commands", color=0x5865F2)
    embed.add_field(name="\U0001f50d Identification", value=(
        "`pk!guess` — reply to a spawn to identify it\n"
        "`pk!correct <name>` — correct a wrong name"
    ), inline=False)
    embed.add_field(name="\U0001f4e6 Collection Pings", value=(
        "`pk!cl add <pokemon, ...>` — add to your list\n"
        "`pk!cl add all <species>` — add all forms (e.g. `all tatsugiri`)\n"
        "`pk!cl remove <pokemon, ...>` — remove from list\n"
        "`pk!cl clear` — clear your entire list\n"
        "`pk!cl list` — view your list"
    ), inline=False)
    embed.add_field(name="\u2728 Shiny Hunting", value=(
        "`pk!sh <pokemon>` — set your hunt target\n"
        "`pk!sh all <species>` — hunt all forms (e.g. `all wooper`)\n"
        "`pk!sh clear` — clear your hunt"
    ), inline=False)
    embed.add_field(name="\u2699\ufe0f Admin — Server", value=(
        "`pk!settings rare-role <@role/id>` — set rare ping role\n"
        "`pk!settings regional-role <@role/id>` — set regional ping role\n"
        "`pk!channelsettings` — view/toggle channel features"
    ), inline=False)
    embed.set_footer(text="Prefix: pk! or @mention the bot")
    await msg.channel.send(embed=embed)


async def cmd_collection(msg: discord.Message, args: str) -> None:
    sub_parts = args.split(None, 1)
    sub  = sub_parts[0].lower() if sub_parts else "list"
    rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""
    col  = _user_col(msg.author.id)

    if sub == "add":
        if not rest:
            return await msg.channel.send("Usage: `pk!cl add <pokemon, ...>` or `pk!cl add all <species>`")
        added: list[str] = []
        invalid: list[str] = []
        already: list[str] = []
        for p in pokemon_list_from_args(rest):
            if not p:
                continue
            matches, inv = _resolve_pokemon_query(p)
            invalid.extend(inv)
            for matched in matches:
                if matched in col:
                    already.append(matched)
                else:
                    col.append(matched)
                    added.append(matched)
        _save()
        lines = []
        if added:
            lines.append(f"✅ Added: **{', '.join(label_to_display(p) for p in added)}**")
        if already:
            lines.append(f"Already in your list: {', '.join(label_to_display(p) for p in already)}")
        if invalid:
            lines.append("\n".join(f"Invalid Pokémon: **{p.title()}**" for p in invalid))
        await msg.channel.send("\n".join(lines) if lines else "Nothing changed.",
                               reference=msg, mention_author=False)

    elif sub == "remove":
        if not rest:
            return await msg.channel.send("Usage: `pk!cl remove <pokemon, ...>`")
        removed = [p for p in pokemon_list_from_args(rest) if p in col]
        for p in removed:
            col.remove(p)
        _save()
        await msg.channel.send(
            f"✅ Removed: **{', '.join(label_to_display(p) for p in removed)}**" if removed
            else "None of those were in your list.",
            reference=msg, mention_author=False)

    elif sub == "clear":
        uid = str(msg.author.id)
        _data["user_collections"][uid] = []
        _save()
        await msg.channel.send("✅ Your collection list has been cleared.", reference=msg, mention_author=False)

    elif sub == "list":
        if not col:
            return await msg.channel.send("Your collection list is empty. Use `pk!cl add <pokemon>`.")
        embed = discord.Embed(
            title=f"{msg.author.display_name}'s Collection Pings",
            description=", ".join(label_to_display(p) for p in col),
            color=0x57F287)
        await msg.channel.send(embed=embed)
    else:
        await msg.channel.send("Usage: `pk!cl add/remove/clear/list <pokemon>`")


def _hunt_matches(label: str, hunt) -> bool:
    """Check if a pokemon label matches a shiny hunt entry (string or list)."""
    if isinstance(hunt, list):
        return any(label_matches_query(label, h) for h in hunt)
    return label_matches_query(label, hunt)


def _hunt_display(hunt) -> str:
    """Return a display string for a hunt entry (string or list)."""
    if isinstance(hunt, list):
        return ", ".join(label_to_display(h) for h in hunt)
    return label_to_display(hunt)


async def cmd_shiny(msg: discord.Message, args: str) -> None:
    uid = str(msg.author.id)
    if not args or args.lower() == "clear":
        _data["user_shiny_hunts"].pop(uid, None)
        _save()
        return await msg.channel.send("✅ Shiny hunt cleared.", reference=msg, mention_author=False)
    query = args.strip().lower()
    if query.startswith("all "):
        species = query[4:].strip()
        forms = find_all_forms(species)
        if not forms:
            return await msg.channel.send(
                f"No forms found for **{species.title()}**. Check the spelling and try again.",
                reference=msg, mention_author=False)
        prev = _data["user_shiny_hunts"].get(uid)
        _data["user_shiny_hunts"][uid] = forms
        _save()
        text = f"✅ Shiny hunt set to all forms of **{species.title()}**: {', '.join(label_to_display(f) for f in forms)}!"
        if prev:
            text += f" (replaced **{_hunt_display(prev)}**)"
        return await msg.channel.send(text, reference=msg, mention_author=False)
    matched = next((lbl for lbl in class_names if label_matches_query(lbl, query)), None)
    if matched is None:
        return await msg.channel.send(
            f"Invalid Pokémon: **{query.title()}**. Check the spelling and try again.",
            reference=msg, mention_author=False)
    prev = _data["user_shiny_hunts"].get(uid)
    _data["user_shiny_hunts"][uid] = matched
    _save()
    text = f"✅ Shiny hunt set to **{label_to_display(matched)}**!"
    if prev:
        text += f" (replaced **{_hunt_display(prev)}**)"
    await msg.channel.send(text, reference=msg, mention_author=False)


async def cmd_settings(msg: discord.Message, args: str) -> None:
    parts = args.split(None, 1)
    if len(parts) < 2:
        return await msg.channel.send("Usage: `pk!settings rare-role <@role/id>` or `pk!settings regional-role <@role/id>`")
    key, value = parts[0].lower(), parts[1].strip()
    cfg = _guild_cfg(msg.guild.id)
    if key in ("rare-role", "regional-role"):
        role = parse_role(msg.guild, value)
        if not role:
            return await msg.channel.send("❌ Role not found. Mention the role or paste its ID.")
        field = "rare_role" if key == "rare-role" else "regional_role"
        cfg[field] = str(role.id)
        _save()
        label = "Rare" if key == "rare-role" else "Regional"
        await msg.channel.send(f"✅ **{label}** ping role set to {role.mention}.", reference=msg, mention_author=False)
    else:
        await msg.channel.send("Available settings: `rare-role`, `regional-role`")




CHANNEL_TOGGLES = {
    "naming":          "Auto Naming",
    "rareping":        "Rare Pings",
    "regionalpinging": "Regional Pings",
    "shinyhunt":       "Shiny Hunt Pings",
    "collectionping":  "Collection Pings",
}


async def cmd_channelsettings(msg: discord.Message, args: str) -> None:
    cfg   = _ch_cfg(msg.channel.id)
    parts = args.split()
    if not args:
        embed = discord.Embed(title=f"#{msg.channel.name} — Channel Settings", color=0xFEE75C)
        for key, label in CHANNEL_TOGGLES.items():
            embed.add_field(name=label, value="🟢 On" if cfg.get(key, True) else "🔴 Off", inline=True)
        embed.set_footer(text="Toggle: pk!channelsettings <setting> true/false")
        return await msg.channel.send(embed=embed)
    if len(parts) < 2:
        return await msg.channel.send(f"Usage: `pk!channelsettings <{'|'.join(CHANNEL_TOGGLES)}> true/false`")
    key, val_str = parts[0].lower(), parts[1].lower()
    if key not in CHANNEL_TOGGLES:
        return await msg.channel.send(f"Unknown setting. Options: {', '.join(CHANNEL_TOGGLES)}")
    enabled = val_str in ("true", "on", "1", "yes")
    cfg[key] = enabled
    _save()
    await msg.channel.send(
        f"{'🟢' if enabled else '🔴'} **{CHANNEL_TOGGLES[key]}** is now **{'on' if enabled else 'off'}** in this channel.",
        reference=msg, mention_author=False)


async def cmd_guess(msg: discord.Message) -> None:
    ref = msg.reference
    if not ref:
        return await msg.channel.send("Reply to a Poketwo spawn with `pk!guess`.")
    try:
        target = ref.resolved if isinstance(ref.resolved, discord.Message) \
                 else await msg.channel.fetch_message(ref.message_id)
    except Exception as exc:
        return await msg.channel.send(f"Couldn't fetch that message: {exc}")
    image_url = get_spawn_image_url(target)
    if not image_url:
        return await msg.channel.send("No image found in that message.")
    assert http_session is not None
    label, method = await identify_spawn(http_session, image_url)
    if label:
        await msg.channel.send(f"That's **{label_to_display(label)}**!", reference=msg, mention_author=False)
    else:
        await msg.channel.send("Couldn't identify that one. Use `pk!correct <name>` if you know.",
                               reference=msg, mention_author=False)


async def cmd_debug(msg: discord.Message) -> None:
    ref = msg.reference
    if not ref:
        return await msg.channel.send("Reply to a spawn message with `pk!debug`.")
    try:
        target = ref.resolved if isinstance(ref.resolved, discord.Message) \
                 else await msg.channel.fetch_message(ref.message_id)
    except Exception as exc:
        return await msg.channel.send(f"Couldn't fetch: {exc}")
    lines = [f"Author: {target.author} ({target.author.id})", f"Embeds: {len(target.embeds)}"]
    for i, emb in enumerate(target.embeds):
        lines += [
            f"[{i}] title={emb.title!r}",
            f"[{i}] desc={str(emb.description or '')[:80]!r}",
            f"[{i}] image={emb.image.url if emb.image else None}",
            f"[{i}] thumbnail={emb.thumbnail.url if emb.thumbnail else None}",
        ]
    log.info("DEBUG:\n%s", "\n".join(lines))
    await msg.channel.send("```\n" + "\n".join(lines) + "\n```")


# ── OWNER / ADMIN COMMANDS ────────────────────────────────────────────────

ADMIN_HELP = """
**pk!admin** — Owner-only management commands

**Viewing data**
`pk!admin stats` — bot-wide statistics
`pk!admin view cl @user/id` — user's collection list
`pk!admin view sh @user/id` — user's shiny hunt
`pk!admin listhunts` — all active shiny hunts
`pk!admin listcols` — all users with collection pings
`pk!admin guildsettings` — this guild's rare/regional config
`pk!admin channelinfo [#ch/id]` — channel toggle states
`pk!admin logs` — last 20 spawn identifications

**Editing user data**
`pk!admin addcol @user/id <pokemon, ...>` — add to user's collection
`pk!admin removecol @user/id <pokemon>` — remove from user's collection
`pk!admin clearcol @user/id` — clear user's collection
`pk!admin setsh @user/id <pokemon>` — set user's shiny hunt (supports `all <species>`)
`pk!admin clearsh @user/id` — clear user's shiny hunt

**Server management**
`pk!admin resetguild` — reset this guild's settings
`pk!admin resetchannel [#ch/id]` — reset a channel's settings
`pk!admin cooldown clear` — clear all channel cooldowns

**Testing & tools**
`pk!admin identify <url>` — identify a Pokémon from an image URL
`pk!admin testspawn <pokemon>` — preview what the bot would post
`pk!admin backup` — DM you the full data file
""".strip()


async def cmd_admin(msg: discord.Message, args: str, client: discord.Client) -> None:
    parts = args.split(None, 2)
    sub   = parts[0].lower() if parts else "help"

    # ── help ──────────────────────────────────────────────────────────
    if sub == "help" or not sub:
        embed = discord.Embed(title="Admin Commands", description=ADMIN_HELP, color=0xED4245)
        await msg.channel.send(embed=embed)

    # ── stats ─────────────────────────────────────────────────────────
    elif sub == "stats":
        total_col_entries = sum(len(v) for v in _data["user_collections"].values())
        users_with_col    = sum(1 for v in _data["user_collections"].values() if v)
        embed = discord.Embed(title="Bot Statistics", color=0x5865F2)
        embed.add_field(name="Model", value="READY ✅" if ort_session else "DISABLED ❌")
        embed.add_field(name="Collection users", value=str(users_with_col))
        embed.add_field(name="Collection entries", value=str(total_col_entries))
        embed.add_field(name="Active shiny hunts", value=str(len(_data["user_shiny_hunts"])))
        embed.add_field(name="Configured guilds", value=str(len(_data["guild_settings"])))
        embed.add_field(name="Channel cooldowns", value=str(len(channel_last_action)))
        embed.add_field(name="Spawn IDs logged", value=str(len(recent_ids)))
        embed.add_field(name="Watching channels", value=str(WATCH_CHANNEL_IDS) if WATCH_CHANNEL_IDS else "All")
        await msg.channel.send(embed=embed)

    # ── view cl / sh ──────────────────────────────────────────────────
    elif sub == "view":
        if len(parts) < 3:
            return await msg.channel.send("Usage: `pk!admin view cl/sh @user/id`")
        what = parts[1].lower()
        uid  = parse_user_id(parts[2])
        if not uid:
            return await msg.channel.send("❌ Couldn't parse user ID.")
        if what == "cl":
            col = _data["user_collections"].get(str(uid), [])
            if not col:
                return await msg.channel.send(f"`{uid}` has no collection list.")
            embed = discord.Embed(title=f"Collection — {uid}", color=0x57F287,
                                  description=", ".join(label_to_display(p) for p in col))
            await msg.channel.send(embed=embed)
        elif what == "sh":
            hunt = _data["user_shiny_hunts"].get(str(uid))
            if not hunt:
                return await msg.channel.send(f"`{uid}` has no active shiny hunt.")
            await msg.channel.send(f"**{uid}** is hunting: **{_hunt_display(hunt)}**")
        else:
            await msg.channel.send("Use `cl` or `sh`.")

    # ── listhunts ─────────────────────────────────────────────────────
    elif sub == "listhunts":
        hunts = _data["user_shiny_hunts"]
        if not hunts:
            return await msg.channel.send("No active shiny hunts.")
        lines = [f"<@{uid}> → **{_hunt_display(p)}**" for uid, p in list(hunts.items())[:20]]
        embed = discord.Embed(title=f"Active Shiny Hunts ({len(hunts)})", description="\n".join(lines), color=0xFEE75C)
        if len(hunts) > 20:
            embed.set_footer(text=f"Showing 20/{len(hunts)}")
        await msg.channel.send(embed=embed)

    # ── listcols ──────────────────────────────────────────────────────
    elif sub == "listcols":
        active = {uid: col for uid, col in _data["user_collections"].items() if col}
        if not active:
            return await msg.channel.send("No collection lists set up.")
        lines = [f"<@{uid}>: {', '.join(label_to_display(p) for p in col[:5])}"
                 + (f" +{len(col)-5} more" if len(col) > 5 else "")
                 for uid, col in list(active.items())[:15]]
        embed = discord.Embed(title=f"Collection Lists ({len(active)} users)",
                              description="\n".join(lines), color=0x57F287)
        await msg.channel.send(embed=embed)

    # ── guildsettings ─────────────────────────────────────────────────
    elif sub == "guildsettings":
        cfg = _data["guild_settings"].get(str(msg.guild.id), {})
        rr  = cfg.get("rare_role")
        rer = cfg.get("regional_role")
        embed = discord.Embed(title="Guild Settings", color=0xEB459E)
        embed.add_field(name="Rare role",     value=f"<@&{rr}>" if rr else "Not set")
        embed.add_field(name="Regional role", value=f"<@&{rer}>" if rer else "Not set")
        embed.add_field(name="Rare list",     value="Built-in (hardcoded)", inline=False)
        embed.add_field(name="Regional list", value="Built-in (hardcoded)", inline=False)
        await msg.channel.send(embed=embed)

    # ── channelinfo ───────────────────────────────────────────────────
    elif sub == "channelinfo":
        ch_id = msg.channel.id
        if len(parts) >= 3:
            uid2 = parse_user_id(parts[2])
            if uid2:
                ch_id = uid2
        cfg = _ch_cfg(ch_id)
        embed = discord.Embed(title=f"Channel Settings — <#{ch_id}>", color=0xFEE75C)
        for key, label in CHANNEL_TOGGLES.items():
            embed.add_field(name=label, value="🟢 On" if cfg.get(key, True) else "🔴 Off", inline=True)
        await msg.channel.send(embed=embed)

    # ── logs ──────────────────────────────────────────────────────────
    elif sub == "logs":
        if not recent_ids:
            return await msg.channel.send("No spawn identifications logged yet.")
        lines = []
        for ts, label, display, ch_id, method in list(recent_ids)[-20:][::-1]:
            t = time.strftime("%H:%M:%S", time.gmtime(ts))
            lines.append(f"`{t}` **{display}** in <#{ch_id}> ({method})")
        embed = discord.Embed(title="Recent Spawn IDs", description="\n".join(lines), color=0x99AAB5)
        await msg.channel.send(embed=embed)

    # ── addcol ────────────────────────────────────────────────────────
    elif sub == "addcol":
        if len(parts) < 3:
            return await msg.channel.send("Usage: `pk!admin addcol @user/id <pokemon, ...>`")
        uid = parse_user_id(parts[1])
        if not uid:
            return await msg.channel.send("❌ Couldn't parse user ID.")
        col   = _user_col(uid)
        names = pokemon_list_from_args(parts[2])
        added = [p for p in names if p not in col]
        col.extend(added)
        _save()
        await msg.channel.send(f"✅ Added **{', '.join(label_to_display(p) for p in added)}** to `{uid}`'s list.")

    # ── removecol ─────────────────────────────────────────────────────
    elif sub == "removecol":
        if len(parts) < 3:
            return await msg.channel.send("Usage: `pk!admin removecol @user/id <pokemon>`")
        uid = parse_user_id(parts[1])
        if not uid:
            return await msg.channel.send("❌ Couldn't parse user ID.")
        col  = _user_col(uid)
        name = parts[2].strip().lower()
        if name in col:
            col.remove(name)
            _save()
            await msg.channel.send(f"✅ Removed **{label_to_display(name)}** from `{uid}`'s list.")
        else:
            await msg.channel.send(f"That Pokémon isn't in `{uid}`'s list.")

    # ── clearcol ──────────────────────────────────────────────────────
    elif sub == "clearcol":
        uid = parse_user_id(parts[1]) if len(parts) > 1 else None
        if not uid:
            return await msg.channel.send("Usage: `pk!admin clearcol @user/id`")
        _data["user_collections"].pop(str(uid), None)
        _save()
        await msg.channel.send(f"✅ Cleared collection for `{uid}`.")

    # ── setsh ─────────────────────────────────────────────────────────
    elif sub == "setsh":
        if len(parts) < 3:
            return await msg.channel.send("Usage: `pk!admin setsh @user/id <pokemon>` or `pk!admin setsh @user/id all <species>`")
        uid = parse_user_id(parts[1])
        if not uid:
            return await msg.channel.send("❌ Couldn't parse user ID.")
        query = " ".join(parts[2:]).strip().lower()
        if query.startswith("all "):
            species = query[4:].strip()
            forms = find_all_forms(species)
            if not forms:
                return await msg.channel.send(f"❌ No forms found for **{species.title()}**. Check the spelling.")
            _data["user_shiny_hunts"][str(uid)] = forms
            _save()
            await msg.channel.send(f"✅ Set `{uid}`'s shiny hunt to all forms of **{species.title()}**: {', '.join(label_to_display(f) for f in forms)}.")
        else:
            matched = next((lbl for lbl in class_names if label_matches_query(lbl, query)), None)
            if not matched:
                return await msg.channel.send(f"❌ No Pokémon found for **{query.title()}**. Check the spelling.")
            _data["user_shiny_hunts"][str(uid)] = matched
            _save()
            await msg.channel.send(f"✅ Set `{uid}`'s shiny hunt to **{label_to_display(matched)}**.")

    # ── clearsh ───────────────────────────────────────────────────────
    elif sub == "clearsh":
        uid = parse_user_id(parts[1]) if len(parts) > 1 else None
        if not uid:
            return await msg.channel.send("Usage: `pk!admin clearsh @user/id`")
        _data["user_shiny_hunts"].pop(str(uid), None)
        _save()
        await msg.channel.send(f"✅ Cleared shiny hunt for `{uid}`.")

    # ── resetguild ────────────────────────────────────────────────────
    elif sub == "resetguild":
        _data["guild_settings"].pop(str(msg.guild.id), None)
        _save()
        await msg.channel.send("✅ Guild settings reset to defaults.")

    # ── resetchannel ──────────────────────────────────────────────────
    elif sub == "resetchannel":
        ch_id = msg.channel.id
        if len(parts) >= 2:
            uid2 = parse_user_id(parts[1])
            if uid2:
                ch_id = uid2
        _data["channel_settings"].pop(str(ch_id), None)
        _save()
        await msg.channel.send(f"✅ Channel settings reset for <#{ch_id}>.")

    # ── cooldown clear ────────────────────────────────────────────────
    elif sub == "cooldown":
        if len(parts) > 1 and parts[1].lower() == "clear":
            channel_last_action.clear()
            await msg.channel.send("✅ All channel cooldowns cleared.")
        else:
            now = time.time()
            lines = [f"<#{ch}>: {int(COOLDOWN - (now - ts))}s left"
                     for ch, ts in channel_last_action.items() if now - ts < COOLDOWN]
            await msg.channel.send("\n".join(lines) or "No active cooldowns.")

    # ── identify ──────────────────────────────────────────────────────
    elif sub == "identify":
        url = parts[1].strip() if len(parts) > 1 else ""
        if not url:
            return await msg.channel.send("Usage: `pk!admin identify <image_url>`")
        assert http_session is not None
        async with msg.channel.typing():
            label, prob = await identify_from_url_direct(http_session, url)
        if label:
            display = label_to_display(label)
            await msg.channel.send(f"**{display}** (confidence: {prob*100:.1f}%)\nRaw label: `{label}`")
        else:
            await msg.channel.send("❌ Could not identify the Pokémon from that URL.")

    # ── testspawn ─────────────────────────────────────────────────────
    elif sub == "testspawn":
        pokemon = parts[1].strip().lower() if len(parts) > 1 else ""
        if not pokemon:
            return await msg.channel.send("Usage: `pk!admin testspawn <pokemon>`")
        display  = label_to_display(pokemon)
        gid      = msg.guild.id
        cfg_g    = _guild_cfg(gid)
        ch_cfg   = _ch_cfg(msg.channel.id)
        lines    = [f"That's **{display}**!"]

        if ch_cfg.get("rareping", True) and is_rare(pokemon, gid):
            rid  = cfg_g.get("rare_role")
            lines.append(f"🌟 Rare ping: {f'<@&{rid}>' if rid else '*(rare role not set)*'}")

        if ch_cfg.get("regionalpinging", True) and is_regional(pokemon, gid):
            rid  = cfg_g.get("regional_role")
            lines.append(f"🗺️ Regional ping: {f'<@&{rid}>' if rid else '*(regional role not set)*'}")

        hunters = [uid for uid, h in _data["user_shiny_hunts"].items()
                   if _hunt_matches(pokemon, h)]
        if ch_cfg.get("shinyhunt", True) and hunters:
            lines.append(f"✨ Shiny hunt pings: {' '.join(f'<@{u}>' for u in hunters)}")

        collectors = [uid for uid, col in _data["user_collections"].items()
                      if any(label_matches_query(pokemon, p) for p in col)]
        if ch_cfg.get("collectionping", True) and collectors:
            lines.append(f"Collection pings: {' '.join(f'<@{u}>' for u in collectors)}")

        embed = discord.Embed(title="Test Spawn Preview", description="\n".join(lines), color=0xFEE75C)
        embed.set_footer(text=f"Label: {pokemon} | Rare: {is_rare(pokemon, gid)} | Regional: {is_regional(pokemon, gid)}")
        await msg.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ── generate ──────────────────────────────────────────────────────
    elif sub == "generate":
        query = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if not query:
            return await msg.channel.send("Usage: `pk!admin generate <pokemon>`")
        matched = next((lbl for lbl in class_names if label_matches_query(lbl, query)), None)
        if not matched:
            return await msg.channel.send(f"❌ No Pokémon found for **{query.title()}**.")
        display = label_to_display(matched)
        dex_num = next((k for k, v in POKEDEX.items() if v.lower() == display.lower()), None)
        if not dex_num:
            base = re.split(r'[-_]', matched)[0]
            dex_num = next((k for k, v in POKEDEX.items() if v.lower() == base.lower()), None)
        if not dex_num:
            return await msg.channel.send(f"❌ Couldn't find dex number for **{display}**.")
        url = f"https://cdn.poketwo.net/images/{dex_num}.png"
        embed = discord.Embed(title=display, description=f"**Spawn URL:**\n`{url}`", color=0x5865F2)
        embed.set_image(url=url)
        embed.set_footer(text=f"Label: {matched} | Dex #{dex_num}")
        await msg.channel.send(embed=embed)

    # ── backup ────────────────────────────────────────────────────────
    elif sub == "backup":
        if not DATA_PATH.exists():
            return await msg.channel.send("No data file yet.")
        try:
            await msg.author.send(
                "Here's your bot data backup:",
                file=discord.File(str(DATA_PATH), filename="bot_data_backup.json")
            )
            await msg.channel.send("✅ Sent the data file to your DMs.")
        except discord.Forbidden:
            await msg.channel.send("❌ Couldn't DM you. Enable DMs from server members.")

    else:
        embed = discord.Embed(title="Admin Commands", description=ADMIN_HELP, color=0xED4245)
        await msg.channel.send(embed=embed)


# ── SPAWN HANDLER ─────────────────────────────────────────────────────────

async def handle_spawn(msg: discord.Message) -> None:
    if not is_spawn_message(msg) or not msg.guild:
        return
    if WATCH_CHANNEL_IDS and msg.channel.id not in WATCH_CHANNEL_IDS:
        return

    ch_cfg = _ch_cfg(msg.channel.id)
    if not any(ch_cfg.get(k, True) for k in CHANNEL_TOGGLES):
        return

    # semaphore=2 means at most 2 spawns are in-flight simultaneously,
    # which prevents aiohttp and Discord request bursts.
    async with semaphore:
        if on_cooldown(msg.channel.id):
            return

        image_url = get_spawn_image_url(msg)
        if not image_url:
            return

        assert http_session is not None
        label, method = await identify_spawn(http_session, image_url)
        if not label:
            log.warning("Could not identify spawn in #%s", msg.channel.id)
            return

        display  = label_to_display(label)
        guild_id = msg.guild.id

        # Log to recent_ids
        recent_ids.append((time.time(), label, display, msg.channel.id, method))
        log.info("Spawn → %s (%s)", display, method)

        lines: list[str] = []

        if ch_cfg.get("naming", True):
            lines.append(f"That's **{display}**!")

        if ch_cfg.get("rareping", True) and is_rare(label, guild_id):
            rid  = _guild_cfg(guild_id).get("rare_role")
            if rid:
                role = msg.guild.get_role(int(rid))
                lines.append(f"🌟 Rare ping: {role.mention if role else f'<@&{rid}>'}")

        if ch_cfg.get("regionalpinging", True) and is_regional(label, guild_id):
            rid  = _guild_cfg(guild_id).get("regional_role")
            if rid:
                role = msg.guild.get_role(int(rid))
                lines.append(f"🗺️ Regional ping: {role.mention if role else f'<@&{rid}>'}")

        if ch_cfg.get("shinyhunt", True):
            hunters = [uid for uid, h in _data["user_shiny_hunts"].items()
                       if _hunt_matches(label, h)]
            if hunters:
                lines.append(f"✨ Shiny hunt pings: {' '.join(f'<@{u}>' for u in hunters)}")

        if ch_cfg.get("collectionping", True):
            collectors = [uid for uid, col in _data["user_collections"].items()
                          if any(label_matches_query(label, p) for p in col)]
            if collectors:
                lines.append(f"Collection pings: {' '.join(f'<@{u}>' for u in collectors)}")

        if not lines:
            return

        # Human-like delay before responding, then set cooldown so back-to-back
        # spawns in the same channel don't both fire within the cooldown window.
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        await asyncio.sleep(delay)
        update_cooldown(msg.channel.id)

        await safe_send(
            msg.channel,
            "\n".join(lines),
            reference=msg,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )


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
    global http_session, _global_rl
    _load()
    setup_classifier()
    load_pokedex()
    _global_rl = GlobalRateLimiter(rate=GLOBAL_RATE_LIMIT, per=GLOBAL_RATE_WINDOW)
    # limit=5: keeps outbound HTTP connections modest; the bot only needs
    # a handful of concurrent image downloads at most.
    connector  = aiohttp.TCPConnector(limit=5)
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
