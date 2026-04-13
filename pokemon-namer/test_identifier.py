"""
Tests the Pokemon identifier using real Poketwo CDN image URLs.
This validates the URL-parsing approach works end-to-end.
"""
import asyncio
import json
import re
import aiohttp
from pathlib import Path

POKEDEX_PATH = Path(__file__).parent / "pokedex.json"
CDN_PATTERN = re.compile(r"poketwo\.net/images/(\d+)\.", re.IGNORECASE)

# Real Poketwo CDN URLs → expected name
TEST_CASES = [
    ("Pikachu",    "https://cdn.poketwo.net/images/25.png"),
    ("Charizard",  "https://cdn.poketwo.net/images/6.png"),
    ("Mewtwo",     "https://cdn.poketwo.net/images/150.png"),
    ("Eevee",      "https://cdn.poketwo.net/images/133.png"),
    ("Gengar",     "https://cdn.poketwo.net/images/94.png"),
    ("Bulbasaur",  "https://cdn.poketwo.net/images/1.png"),
    ("Snorlax",    "https://cdn.poketwo.net/images/143.png"),
    ("Dragonite",  "https://cdn.poketwo.net/images/149.png"),
    ("Mew",        "https://cdn.poketwo.net/images/151.png"),
    ("Umbreon",    "https://cdn.poketwo.net/images/197.png"),
]

def identify_from_url(pokedex: dict, image_url: str) -> str | None:
    m = CDN_PATTERN.search(image_url)
    if m:
        return pokedex.get(m.group(1))
    return None

async def check_images_accessible(urls: list[str]) -> None:
    """Verify CDN images are actually reachable."""
    print("Checking CDN image accessibility…")
    async with aiohttp.ClientSession() as session:
        for url in urls[:3]:  # Just spot-check first 3
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                status = "✓ reachable" if r.status == 200 else f"✗ HTTP {r.status}"
                print(f"  {url.split('/')[-1]:12s} {status}")

async def main():
    print("=" * 55)
    print("  Pokemon Namer — Identification Test")
    print("=" * 55)

    # Load pokedex
    if not POKEDEX_PATH.exists():
        print("ERROR: pokedex.json not found. Run build_pokedex.py first.")
        return

    with open(POKEDEX_PATH) as f:
        pokedex = json.load(f)
    print(f"  Pokedex loaded: {len(pokedex)} Pokemon")
    print()

    # Spot-check CDN accessibility
    await check_images_accessible([url for _, url in TEST_CASES])
    print()

    # Run identification tests
    print("Identification results:")
    print("-" * 55)
    passed = 0
    for expected, url in TEST_CASES:
        name = identify_from_url(pokedex, url)
        match = name and expected.lower() in name.lower()
        status = "PASS ✓" if match else "FAIL ✗"
        result = name or "NOT FOUND"
        print(f"  {expected:12s} → {result:20s}  {status}")
        if match:
            passed += 1

    print()
    print("=" * 55)
    total = len(TEST_CASES)
    pct = 100 * passed // total
    print(f"  Results: {passed}/{total} correct  ({pct}% accuracy)")
    if passed == total:
        print("  All Pokemon identified correctly! Bot is ready.")
    elif passed / total >= 0.8:
        print("  Good accuracy — bot should work well.")
    else:
        print("  Issues detected — check the URL patterns.")
    print("=" * 55)

if __name__ == "__main__":
    asyncio.run(main())
