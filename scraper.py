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
    {"service": "BytePlus VOD",    "url": "https://docs.byteplus.com/en/docs/byteplus-vod/docs-rn-player-release-notes"},
]

CODA_DOC_ID = "TZR3xqmlHs"
CODA_TABLE_ID = "grid-zCs69WXyTd"

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


async def get_existing_features() -> set:
    """Читає існуючі feature_name з Coda таблиці"""
    coda_token = os.environ.get("CODA_API_TOKEN")
    if not coda_token:
        print("⚠️ No CODA_API_TOKEN — skipping Coda dedup")
        return set()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"https://coda.io/apis/v1/docs/{CODA_DOC_ID}/tables/{CODA_TABLE_ID}/rows",
                headers={"Authorization": f"Bearer {coda_token}"},
                params={"useColumnNames": "true", "limit": "500"}
            )
            response.raise_for_status()
            rows = response.json().get("items", [])
            existing = {
                row["values"].get("Feature name", "").lower().strip()
                for row in rows
                if row.get("values", {}).get("Feature name")
            }
            print(f"📋 Existing features in Coda: {len(existing)}")
            return existing
    except Exception as e:
        print(f"⚠️ Could not fetch Coda rows: {e}")
        return set()


def is_duplicate(feature_name: str, existing_features: set) -> bool:
    name = feature_name.lower().strip()
    for existing in existing_features:
        if existing == name:
            return True
        # Fuzzy: одна назва містить іншу
        if len(name) > 10 and len(existing) > 10:
            if existing in name or name in existing:
                return True
    return False


# ---------------------------
# REVENUECAT
# ---------------------------

async def scrape_revenuecat(page, cutoff_date: datetime) -> dict:
    print(f"🔍 RevenueCat")
    try:
        await page.goto(
            "https://www.revenuecat.com/changelog",
            wait_until="domcontentloaded",
            timeout=30000
        )

        try:
            await page.wait_for_selector("a[href*='/release/']", timeout=15000)
            print("  ✅ Release links found")
        except Exception:
            print("  ⚠️ Timeout waiting for release links")

        await page.wait_for_timeout(3000)

        release_links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href*="/release/"]'))
                .map(a => a.href)
                .filter((v, i, arr) => arr.indexOf(v) === i);
        }""")

        print(f"  📋 Total release links found: {len(release_links)}")

        new_urls = []
        for url in release_links:
            match = re.search(r'(\d{4}-\d{2}-\d{2})$', url)
            if match:
                try:
                    date = datetime.strptime(match.group(1), "%Y-%m-%d")
                    if date >= cutoff_date:
                        new_urls.append(url)
                except ValueError:
                    continue

        print(f"  📋 New releases after {cutoff_date.strftime('%B %d, %Y')}: {len(new_urls)}")

        if not new_urls:
            return {
                "service": "RevenueCat",
                "url": "https://www.revenuecat.com/changelog",
                "content": "",
                "scraped_at": datetime.now().isoformat(),
                "status": "no_new_releases"
            }

        all_content = []
        for url in new_urls[:10]:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                text = await page.evaluate("""() => {
                    const main = document.querySelector('main');
                    return (main || document.body).innerText.slice(0, 1500);
                }""")
                all_content.append(f"{url}\n{text.strip()}")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"  ⚠️ Failed to scrape {url}: {e}")

        if not all_content:
            return _error_result(
                {"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"},
                "failed to scrape individual releases"
            )

        return {
            "service": "RevenueCat",
            "url": "https://www.revenuecat.com/changelog",
            "content": "\n\n---\n\n".join(all_content),
            "scraped_at": datetime.now().isoformat(),
            "status": "ok"
        }

    except Exception as e:
        print(f"  ❌ RevenueCat error: {e}")
        return _error_result(
            {"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"},
            str(e)
        )


# ---------------------------
# SCRAPER
# ---------------------------

async def scrape_page(page, service_info: dict, retries: int = 2) -> dict:
    print(f"🔍 {service_info['service']}")

    for attempt in range(retries + 1):
        try:
            if service_info["service"] == "BytePlus VOD":
                await page.goto(
                    service_info["url"],
                    wait_until="domcontentloaded",
                    timeout=30000
                )
                try:
                    await page.wait_for_selector(
                        "article, .content, main, [class*='release'], [class*='changelog']",
                        timeout=15000
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
            else:
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

    # Читаємо існуючі фічі з Coda для дедуплікації
    existing_features = await get_existing_features()

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

        revenuecat_page = await context.new_page()
        revenuecat_result = await scrape_revenuecat(revenuecat_page, cutoff_date)
        await revenuecat_page.close()

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
            result["existing_features"] = list(existing_features)
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
