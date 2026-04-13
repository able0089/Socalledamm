"""
Downloads all Pokemon names from PokeAPI and saves them as a local JSON map.
Run once: python3 build_pokedex.py
Produces: pokedex.json  { "1": "Bulbasaur", "2": "Ivysaur", ... }
"""
import asyncio
import json
import aiohttp

POKEAPI = "https://pokeapi.co/api/v2/pokemon"
OUT = "pokedex.json"

async def fetch_all():
    pokedex: dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        # Fetch the full list in one request
        async with session.get(f"{POKEAPI}?limit=2000") as r:
            data = await r.json()

        results = data["results"]
        print(f"Fetching {len(results)} Pokemon names…")

        for entry in results:
            # URL format: https://pokeapi.co/api/v2/pokemon/25/
            parts = entry["url"].rstrip("/").split("/")
            dex_id = parts[-1]
            name = entry["name"].replace("-", " ").title()
            pokedex[dex_id] = name

    # Sort by numeric key for readability
    ordered = {str(k): pokedex[str(k)] for k in sorted(pokedex.keys(), key=lambda x: int(x) if x.isdigit() else 99999)}

    with open(OUT, "w") as f:
        json.dump(ordered, f, indent=2)

    print(f"Saved {len(ordered)} entries to {OUT}")
    print("Sample:", dict(list(ordered.items())[:5]))

asyncio.run(fetch_all())
