import asyncio
import hashlib
import httpx
import json
import os
import re
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ---------------------------
# PAGES
# ---------------------------

PAGES = [
    {"service": "Sentry",          "url": "https://sentry.io/changelog/"},
    {"service": "Firebase",        "url": "https://firebase.google.com/support/releases"},
    {"service": "Apple Developer", "url": "https://developer.apple.com/news/"},
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
# REVENUECAT: знаходимо нові релізи через HTML
# ---------------------------

async def scrape_revenuecat(cutoff_date: datetime) -> dict:
    print(f"🔍 RevenueCat")

    try:
        # Крок 1: завантажуємо головну changelog сторінку через httpx (без JS)
        # і шукаємо всі посилання /release/ з датою в URL
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        ) as client:
            response = await client.get("https://www.revenuecat.com/changelog")
            html = response.text

        # Знаходимо всі /release/ URL з датою в кінці (формат: -YYYY-MM-DD)
        release_urls = re.findall(
            r'href="(/release/[^"]+?(\d{4}-\d{2}-\d{2}))"',
            html
        )

        if not release_urls:
            print(f"  ⚠️ No release URLs found in changelog HTML")
            return _error_result({"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"}, "no release URLs found")

        # Крок 2: фільтруємо за датою
        new_releases = []
        for path, date_str in release_urls:
            try:
                release_date = datetime.strptime(date_str, "%Y-%m-%d")
                if release_date >= cutoff_date:
                    full_url = f"https://www.revenuecat.com{path}"
                    if full_url not in [r["url"] for r in new_releases]:
                        new_releases.append({"url": full_url, "date": date_str})
            except ValueError:
                continue

        print(f"  📋 Found {len(new_releases)} new releases after {cutoff_date.strftime('%B %d, %Y')}")

        if not new_releases:
            return {
                "service": "RevenueCat",
                "url": "https://www.revenuecat.com/changelog",
                "content": "",
                "scraped_at": datetime.now().isoformat(),
                "status": "no_new_releases"
            }

        # Крок 3: скрапимо кожен новий реліз через httpx (статичні сторінки)
        all_content = []
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        ) as client:
            for release in new_releases[:10]:  # максимум 10 релізів
                try:
                    r = await client.get(release["url"])
                    # Витягуємо текст з HTML без тегів
                    text = re.sub(r'<[^>]+>', ' ', r.text)
                    text = re.sub(r'\s+', ' ', text).strip()
                    # Беремо перші 1000 символів кожного релізу
                    all_content.append(f"[{release['date']}] {release['url']}\n{text[:1000]}")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"  ⚠️ Failed to scrape {release['url']}: {e}")

        if not all_content:
            return _error_result({"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"}, "failed to scrape releases")

        combined_content = "\n\n---\n\n".join(all_content)

        return {
            "service": "RevenueCat",
            "url": "https://www.revenuecat.com/changelog",
            "content": combined_content,
            "scraped_at": datetime.now().isoformat(),
            "status": "ok"
        }

    except Exception as e:
        print(f"  ❌ RevenueCat error: {e}")
        return _error_result({"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"}, str(e))


# ---------------------------
# SCRAPER
# ---------------------------

async def scrape_page(page, service_info: dict, retries: int = 2) -> dict:
    print(f"🔍 {service_info['service']}")

    for attempt in range(retries + 1):
        try:
            await page.goto(
                service_info["url"],
                wait_until="domcontentloaded",
                timeout=30000
            )

            for selector in ["main", "article", "[role='main']", "body"]:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    break
                except Exception:
                    continue

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

                const blocks = target.querySelectorAll('h1,h2,h3,h4,p,li,time,[class*="date"],[class*="release"]');
                if (blocks.length > 0) {
                    return Array.from(blocks)
                        .map(b => b.innerText?.trim())
                        .filter(t => t && t.length > 2)
                        .join('\\n')
                        .slice(0, 12000);
                }

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

        except Exception as e:
            if attempt < retries:
                print(f"  ⚠️ Error on attempt {attempt + 1}: {e}, retrying...")
                await page.wait_for_timeout(3000)
                continue
            return _error_result(service_info, str(e))

    return _error_result(service_info, "max retries exceeded")


def _error_result(service_info: dict, reason: str) -> dict:
    return {
        "service": service_info["service"],
        "url": service_info["url"],
        "content": "",
        "scraped_at": datetime.now().isoformat(),
        "status": f"error: {reason}"
    }


# ---------------------------
# MAIN
# ---------------------------

async def main():
    seen_hashes = load_seen_hashes()
    new_results = []

    cutoff_date = datetime.now() - timedelta(days=40)
    cutoff_date_str = cutoff_date.strftime("%B %d, %Y")

    # Скрапимо RevenueCat окремо
    revenuecat_result = await scrape_revenuecat(cutoff_date)

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
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9"
            }
        )

        semaphore = asyncio.Semaphore(2)

        async def scrape_with_limit(service):
            async with semaphore:
                page = await context.new_page()
                result = await scrape_page(page, service)
                await page.close()
                await asyncio.sleep(2)
                return result

        playwright_results = await asyncio.gather(*[scrape_with_limit(s) for s in PAGES])
        await browser.close()

    all_results = [revenuecat_result] + list(playwright_results)

    for result in all_results:
        if result["status"] not in ("ok",):
            print(f"⚠️  Skipped {result['service']}: {result['status']}")
            continue

        h = content_hash(result["content"])
        service_key = result["service"]

        if seen_hashes.get(service_key) != h:
            seen_hashes[service_key] = h
            result["since_date"] = cutoff_date_str
            new_results.append(result)
            print(f"🆕 New content: {result['service']}")
        else:
            print(f"⏭️  No changes: {result['service']}")

    save_seen_hashes(seen_hashes)

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(new_results, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done: {len(new_results)} services with new content (out of {len(all_results)} scraped)")

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
