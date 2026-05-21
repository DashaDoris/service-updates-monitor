import asyncio
import hashlib
import httpx
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

# ---------------------------
# SMART WAIT STRATEGY
# ---------------------------

def detect_page_type(url: str) -> str:
    url = url.lower()

    if any(x in url for x in ["blog", "news"]):
        return "blog"

    if any(x in url for x in ["changelog", "release", "whats-new", "announcements"]):
        return "changelog"

    if any(x in url for x in ["docs", "developer.apple.com/documentation"]):
        return "docs"

    return "unknown"


WAIT_MAP = {
    "blog": "article",
    "changelog": "article, main",
    "docs": "main",
    "unknown": "main"
}


def get_wait_for(url: str):
    page_type = detect_page_type(url)
    return WAIT_MAP.get(page_type, "main")


# ---------------------------
# PAGES
# ---------------------------

PAGES = [
    {"service": "Amplitude", "url": "https://amplitude.com/releases"},
    {"service": "RevenueCat", "url": "https://www.revenuecat.com/blog/"},
    {"service": "Sentry", "url": "https://sentry.io/changelog/"},
    {"service": "Customer.io", "url": "https://customer.io/blog/"},
    {"service": "Algolia", "url": "https://changelog.algolia.com/"},
    {"service": "AppsFlyer", "url": "https://support.appsflyer.com/hc/en-us/sections/6551161473041"},
    {"service": "Firebase Android", "url": "https://firebase.google.com/docs/android/setup"},
    {"service": "Firebase iOS", "url": "https://firebase.google.com/docs/ios/setup"},
    {"service": "Apple Developer", "url": "https://developer.apple.com/documentation/ios"},
    {"service": "Apple News", "url": "https://developer.apple.com/news/"},
    {"service": "Google Play", "url": "https://play.google.com/console/about/whats-new/"},
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

async def scrape_page(page, service_info):
    print(f"🔍 {service_info['service']}")

    try:
        await page.goto(
            service_info["url"],
            wait_until="domcontentloaded",
            timeout=30000
        )

        wait_for = get_wait_for(service_info["url"])

        try:
            await page.wait_for_selector(wait_for, timeout=8000)
        except Exception:
            pass

        await page.wait_for_timeout(1500)

        content = await page.evaluate("""() => {
            const selectors = [
                'main',
                'article',
                '[role="main"]',
                '.content',
                '.devsite-article-body'
            ];

            let el = null;

            for (const s of selectors) {
                el = document.querySelector(s);
                if (el) break;
            }

            const text = (el || document.body).innerText || "";

            // trim at last paragraph boundary within 8000 chars
            const raw = text.slice(0, 8000);
            const lastBreak = Math.max(raw.lastIndexOf("\\n\\n"), raw.lastIndexOf(". "));
            return lastBreak > 4000 ? raw.slice(0, lastBreak + 1) : raw;
        }""")

        content = " ".join(content.split())

        if not content or len(content) < 100:
            status = "empty"
        else:
            status = "ok"

        return {
            "service": service_info["service"],
            "url": service_info["url"],
            "content": content,
            "scraped_at": datetime.now().isoformat(),
            "status": status
        }

    except Exception as e:
        return {
            "service": service_info["service"],
            "url": service_info["url"],
            "content": "",
            "scraped_at": datetime.now().isoformat(),
            "status": f"error: {str(e)}"
        }


# ---------------------------
# MAIN
# ---------------------------

async def main():
    results = []
    seen_hashes = load_seen_hashes()
    new_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        semaphore = asyncio.Semaphore(3)

        async def scrape_with_limit(service):
            async with semaphore:
                page = await context.new_page()
                result = await scrape_page(page, service)
                await page.close()
                await asyncio.sleep(1)
                return result

        results = await asyncio.gather(*[scrape_with_limit(s) for s in PAGES])

        await browser.close()

    # deduplicate: keep only results whose content changed since last run
    for result in results:
        if result["status"] != "ok":
            continue
        h = content_hash(result["content"])
        service_key = result["service"]
        if seen_hashes.get(service_key) != h:
            seen_hashes[service_key] = h
            new_results.append(result)
            print(f"🆕 New content: {result['service']}")
        else:
            print(f"⏭️  No changes: {result['service']}")

    save_seen_hashes(seen_hashes)

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(new_results, f, ensure_ascii=False, indent=2)

    print(f"✅ Done: {len(new_results)} services with new content (out of {len(results)} scraped)")

    webhook_url = os.environ.get("N8N_WEBHOOK_URL")

    if webhook_url and new_results:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    webhook_url,
                    json=new_results,
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
            print("📤 Sent to n8n")
        except httpx.HTTPError as e:
            print(f"❌ Webhook failed: {e}")
    elif not new_results:
        print("📭 Nothing new to send")


asyncio.run(main())
