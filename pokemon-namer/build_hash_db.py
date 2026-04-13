"""
Build the perceptual hash database from Poketwo's CDN images.

Downloads each Pokemon image directly from cdn.poketwo.net (the exact same
source Poketwo uses for spawns), computes a perceptual hash, and saves
the result to hash_db.json.

Run once:  python3 build_hash_db.py
Re-run at any time to refresh the database.
"""

import asyncio
import io
import json
import sys
from pathlib import Path

import aiohttp
import imagehash
from PIL import Image

POKEDEX_PATH = Path(__file__).parent / "pokedex.json"
OUT_PATH = Path(__file__).parent / "hash_db.json"
CDN_BASE = "https://cdn.poketwo.net/images/{}.png"
CONCURRENCY = 20   # simultaneous downloads
HASH_SIZE = 16     # larger = more bits = more accurate (16 → 256-bit hash)


async def hash_one(session: aiohttp.ClientSession, dex_id: str, name: str) -> tuple[str, str] | None:
    url = CDN_BASE.format(dex_id)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            data = await r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        h = str(imagehash.phash(img, hash_size=HASH_SIZE))
        return h, name
    except Exception as exc:
        print(f"  WARNING: skipped #{dex_id} {name}: {exc}", file=sys.stderr)
        return None


async def build():
    with open(POKEDEX_PATH) as f:
        pokedex: dict[str, str] = json.load(f)

    print(f"Building hash DB for {len(pokedex)} Pokemon from Poketwo CDN…")

    sem = asyncio.Semaphore(CONCURRENCY)
    hash_db: dict[str, str] = {}
    done = 0

    async def bounded(dex_id: str, name: str):
        async with sem:
            return await hash_one(session, dex_id, name)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [bounded(dex_id, name) for dex_id, name in pokedex.items()]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            done += 1
            if result:
                phash, name = result
                hash_db[phash] = name
            if done % 100 == 0 or done == len(pokedex):
                print(f"  {done}/{len(pokedex)} done, {len(hash_db)} hashed")

    with open(OUT_PATH, "w") as f:
        json.dump(hash_db, f)

    print(f"\nDone! {len(hash_db)} entries saved to {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(build())
