"""
Take screenshots of each dashboard section using Playwright.
Dashboard must be running on http://127.0.0.1:8080 before calling this script.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL = "http://127.0.0.1:8080"
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SECTIONS = [
    ("overview",  "Overview"),
    ("symbols",   "Symbols"),
    ("orders",    "Orders"),
    ("fills",     "Fills"),
    ("positions", "Positions"),
    ("pnl",       "PnL"),
    ("risk",      "Risk"),
    ("events",    "Events"),
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})

        await page.goto(BASE_URL, wait_until="networkidle")

        # Wait for initial data to load (JS fetches from API on load)
        await page.wait_for_timeout(2500)

        # Debug: print page content
        content = await page.content()
        print("PAGE TITLE:", await page.title())
        print("nav-links found:", content.count("nav-link"))

        for section, label in SECTIONS:
            if section != "overview":
                # Navigate by JS directly
                await page.evaluate(f"""
                    const links = document.querySelectorAll('.nav-link');
                    for (const l of links) {{
                        if (l.textContent.trim() === '{label}') {{ l.click(); break; }}
                    }}
                """)
                # Wait for data fetch and render (API + JS render)
                await page.wait_for_timeout(1500)

            path = OUT_DIR / f"{section}.png"
            await page.screenshot(path=str(path), full_page=False)
            print(f"Saved {path}")

        await browser.close()
    print("All screenshots saved.")


if __name__ == "__main__":
    asyncio.run(main())
