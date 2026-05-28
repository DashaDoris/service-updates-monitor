import asyncio
import hashlib
import httpx
import json
import os
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ---------------------------
# PAGES
# ---------------------------

PAGES = [
    {"service": "Amplitude",          "url": "https://amplitude.com/releases"},
    {"service": "RevenueCat",         "url": "https://www.revenuecat.com/changelog"},
    {"service": "Sentry",             "url": "https://sentry.io/changelog/"},
    {"service": "Customer.io",        "url": "https://customer.io/docs/release-notes/"},
    {"service": "Algolia",            "url": "https://changelog.algolia.com/"},
    {"service": "Firebase",           "url": "https://firebase.google.com/support/releases"},
    {"service": "Apple Developer",    "url": "https://developer.apple.com/news/"},
    {"service": "Google Play Console","url": "https://play.google.com/console/about/whats-new/"},
]

# ---------------------------
# DEDUPLICATION
# ---------------------------

HASHES_FILE = "seen_hashes.json"


def load_seen_hashes() -> dict:
    if os.path.exists(HASHES_FILE):
        with open(HASHES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen_hashes(hashes: dict):
    with open(HASHES_FILE, "w", encoding="utf-8") as f:
        json.dump(hashes, f, ensure_ascii=False, indent=2)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------
# SCRAPER
# ---------------------------

async def scrape_page(page, service_info: dict, retries: int = 2) -> dict:
    print(f"🔍 {service_info['service']}")

    for attempt in range(retries + 1):
        try:
            # SPA сайти потребують networkidle, решта — domcontentloaded
            spa_services = {"RevenueCat", "Algolia", "Amplitude"}
            wait_until = "networkidle" if service_info["service"] in spa_services else "domcontentloaded"

            await page.goto(
                service_info["url"],
                wait_until=wait_until,
                timeout=30000
            )

            # Додаткове очікування для SPA
            if service_info["service"] in spa_services:
                await page.wait_for_timeout(2000)

            # Чекаємо появи основного контенту
            for selector in ["main", "article", "[role='main']", "body"]:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    break
                except Exception:
                    continue

            # Витягуємо текст зі збереженням структури
            content = await page.evaluate("""() => {
                const selectors = [
                    'main',
                    'article',
                    '[role="main"]',
                    '.content',
                    '.devsite-article-body',
                    '.changelog',
                    '.release-notes'
                ];

                let el = null;
                for (const s of selectors) {
                    el = document.querySelector(s);
                    if (el) break;
                }

                const target = el || document.body;

                // Зберігаємо структуру: заголовки і параграфи через \n
                const blocks = target.querySelectorAll('h1,h2,h3,h4,p,li,time,[class*="date"],[class*="release"]');
                if (blocks.length > 0) {
                    return Array.from(blocks)
                        .map(b => b.innerText?.trim())
                        .filter(t => t && t.length > 2)
                        .join('\n')
                        .slice(0, 12000); // більше символів зі структурою
                }

                // Fallback: звичайний текст
                return (target.innerText || '').slice(0, 12000);
            }""")

            content = content.strip()

            if not content or len(content) < 100:
            if attempt < retries:
                print(f"  ⚠️ Empty content, retry {attempt + 1}...")
                await page.wait_for_timeout(3000)
                continue
            return _error_result(service_info, "empty content after retries")

        return {
            "service": service_info["service"],
            "url": service_info["url"],
            "content": content,
            "scraped_at": datetime.now().isoformat(),
            "status": "ok"
        }
