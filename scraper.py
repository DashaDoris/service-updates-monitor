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


def _norm_url(u) -> str:
    """Нормалізує URL: прибирає query, hash, хвостовий слеш. Приймає рядок або urlref-dict."""
    if isinstance(u, dict):
        u = u.get("url") or u.get("name") or ""
    s = (u or "").strip().lower().split("#")[0].split("?")[0]
    return s[:-1] if s.endswith("/") else s


async def get_existing_features():
    """Читає Feature name + Source URL з Coda. Повертає (set назв, set норм. URL)."""
    coda_token = os.environ.get("CODA_API_TOKEN")
    if not coda_token:
        print("⚠️ No CODA_API_TOKEN — skipping Coda dedup")
        return set(), set()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"https://coda.io/apis/v1/docs/{CODA_DOC_ID}/tables/{CODA_TABLE_ID}/rows",
                headers={"Authorization": f"Bearer {coda_token}"},
                params={"useColumnNames": "true", "limit": "500"}
            )
            response.raise_for_status()
            rows = response.json().get("items", [])
            names, urls = set(), set()
            for row in rows:
                vals = row.get("values", {})
                name = (vals.get("Feature name") or "").lower().strip()
                url = _norm_url(vals.get("Source URL"))
                if name:
                    names.add(name)
                if url:
                    urls.add(url)
            print(f"📋 Existing in Coda: {len(names)} names, {len(urls)} urls")
            return names, urls
    except Exception as e:
        print(f"⚠️ Could not fetch Coda rows: {e}")
        return set(), set()


# ---------------------------
# REVENUECAT
# ---------------------------

async def scrape_revenuecat(page, cutoff_date: datetime, seen_urls=None) -> dict:
    seen_urls = seen_urls or set()
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

        # Дедуп по URL ще до Gemini: викидаємо релізи, які вже бачили (кеш)
        new_urls = [u for u in new_urls if _norm_url(u) not in seen_urls]
        print(f"  🆕 Fresh (unseen) releases: {len(new_urls)}")

        if not new_urls:
            return {
                "service": "RevenueCat",
                "url": "https://www.revenuecat.com/changelog",
                "content": "",
                "scraped_at": datetime.now().isoformat(),
                "status": "no_new_releases"
            }

        all_content = []
        scraped_urls = []
        for url in new_urls[:10]:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                text = await page.evaluate("""() => {
                    const main = document.querySelector('main');
                    return (main || document.body).innerText.slice(0, 1500);
                }""")
                all_content.append(f"{url}\n{text.strip()}")
                scraped_urls.append(_norm_url(url))
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
            "status": "ok",
            "_new_urls": scraped_urls
        }

    except Exception as e:
        print(f"  ❌ RevenueCat error: {e}")
        return _error_result(
            {"service": "RevenueCat", "url": "https://www.revenuecat.com/changelog"},
            str(e)
        )


# ---------------------------
# APPLE DEVELOPER
# ---------------------------

async def scrape_apple(page, cutoff_date: datetime, seen_urls=None) -> dict:
    seen_urls = seen_urls or set()
    print(f"🔍 Apple Developer")
    try:
        await page.goto(
            "https://developer.apple.com/news/",
            wait_until="domcontentloaded",
            timeout=30000
        )

        try:
            await page.wait_for_selector("a[href*='/news/?id=']", timeout=15000)
            print("  ✅ Article links found")
        except Exception:
            print("  ⚠️ Timeout waiting for article links")

        await page.wait_for_timeout(2000)

        # Збираємо пари {заголовок, url, дата} з заголовків-лінків статей.
        # Дата лежить у тексті одразу після заголовка (напр. "June 3, 2026").
        articles = await page.evaluate("""() => {
            const out = [];
            const seen = new Set();
            // заголовки статей — це <a href="/news/?id=..."> усередині <h2>/<h3>
            const links = document.querySelectorAll("a[href*='/news/?id=']");
            for (const a of links) {
                const href = a.href.split('#')[0];
                const title = (a.innerText || '').trim();
                // лишаємо тільки лінки, що є заголовками статей (мають текст і всередині heading)
                const inHeading = a.closest('h1,h2,h3,h4');
                if (!title || title.length < 5 || !inHeading) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                // текст статті: від заголовка до наступного заголовка
                let body = '';
                let node = inHeading.nextElementSibling;
                let steps = 0;
                while (node && steps < 12) {
                    if (node.querySelector && node.querySelector("a[href*='/news/?id=']")) break;
                    if (/^H[1-4]$/.test(node.tagName)) break;
                    const t = (node.innerText || '').trim();
                    if (t) body += t + '\\n';
                    node = node.nextElementSibling;
                    steps++;
                }
                out.push({ url: href, title: title, body: body.slice(0, 1200) });
            }
            return out;
        }""")

        print(f"  📋 Total articles found: {len(articles)}")

        # Парсимо дату з body (перший рядок виду "Month D, YYYY") і фільтруємо по cutoff
        def _extract_date(text):
            m = re.search(r'([A-Z][a-z]+ \d{1,2}, \d{4})', text or '')
            if not m:
                return None
            try:
                return datetime.strptime(m.group(1), "%B %d, %Y")
            except ValueError:
                return None

        fresh = []
        for art in articles:
            d = _extract_date(art["body"])
            if d and d >= cutoff_date:
                if _norm_url(art["url"]) not in seen_urls:
                    fresh.append(art)

        print(f"  📋 New articles after {cutoff_date.strftime('%B %d, %Y')}: "
              f"{sum(1 for a in articles if (_extract_date(a['body']) or datetime.min) >= cutoff_date)}")
        print(f"  🆕 Fresh (unseen) articles: {len(fresh)}")

        if not fresh:
            return {
                "service": "Apple Developer",
                "url": "https://developer.apple.com/news/",
                "content": "",
                "scraped_at": datetime.now().isoformat(),
                "status": "no_new_releases"
            }

        all_content = []
        scraped_urls = []
        for art in fresh[:15]:
            all_content.append(f"{art['url']}\n{art['title']}\n{art['body'].strip()}")
            scraped_urls.append(_norm_url(art["url"]))

        return {
            "service": "Apple Developer",
            "url": "https://developer.apple.com/news/",
            "content": "\n\n---\n\n".join(all_content),
            "scraped_at": datetime.now().isoformat(),
            "status": "ok",
            "_new_urls": scraped_urls
        }

    except Exception as e:
        print(f"  ❌ Apple error: {e}")
        return _error_result(
            {"service": "Apple Developer", "url": "https://developer.apple.com/news/"},
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

    # Читаємо існуючі фічі з Coda для дедуплікації (назви + URL)
    existing_names, existing_urls = await get_existing_features()

    # Раніше побачені RevenueCat-релізи (персистяться в seen_hashes через кеш)
    rc_seen_urls = set(seen_hashes.get("RevenueCat_seen_urls", []))

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
        revenuecat_result = await scrape_revenuecat(revenuecat_page, cutoff_date, rc_seen_urls)
        await revenuecat_page.close()

        apple_seen_urls = set(seen_hashes.get("Apple_seen_urls", []))
        apple_page = await context.new_page()
        apple_result = await scrape_apple(apple_page, cutoff_date, apple_seen_urls)
        await apple_page.close()

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

    all_results = [revenuecat_result, apple_result] + list(playwright_results)

    for result in all_results:
        if result["status"] not in ("ok",):
            print(f"⚠️  Skipped {result['service']}: {result['status']}")
            continue

        result["since_date"] = cutoff_date_str
        result["existing_features"] = list(existing_names)
        result["existing_urls"] = list(existing_urls)

        # RevenueCat: дедуп по URL вже зроблено в скрапері; завжди шлемо свіже,
        # бо суцільний content-хеш тут не має сенсу (вікно сповзає щодня)
        if result["service"] == "RevenueCat":
            included = result.pop("_new_urls", [])
            rc_seen_urls.update(included)
            seen_hashes["RevenueCat_seen_urls"] = list(rc_seen_urls)
            new_results.append(result)
            print(f"🆕 New content: RevenueCat ({len(included)} releases)")
            continue

        if result["service"] == "Apple Developer":
            included = result.pop("_new_urls", [])
            apple_seen_urls.update(included)
            seen_hashes["Apple_seen_urls"] = list(apple_seen_urls)
            new_results.append(result)
            print(f"🆕 New content: Apple Developer ({len(included)} articles)")
            continue

        # Інші сервіси: суцільний блок, дедуп по хешу всього контенту
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
