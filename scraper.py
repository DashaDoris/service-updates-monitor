import asyncio
from playwright.async_api import async_playwright
import json
import os
from datetime import datetime

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
# SCRAPER
# ---------------------------

async def scrape_page(page, service_info):
    print(f"🔍 {service_info['service']}")

    try:
        await page.goto(
            service_info["url"],
            wait_until="networkidle",
            timeout=30000
        )

        wait_for = get_wait_for(service_info["url"])

        try:
            await page.wait_for_selector(wait_for, timeout=8000)
        except:
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

            return text.slice(0, 8000);
        }""")

        # cleanup noise
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

        for service in PAGES:
            page = await context.new_page()
            result = await scrape_page(page, service)
            results.append(result)
            await page.close()
            await asyncio.sleep(1)

        await browser.close()

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"✅ Done: {len(results)} services scraped")

    webhook_url = os.environ.get("N8N_WEBHOOK_URL")

    if webhook_url:
        import urllib.request

        data = json.dumps(results).encode("utf-8")

        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        urllib.request.urlopen(req)

        print("📤 Sent to n8n")


asyncio.run(main())
