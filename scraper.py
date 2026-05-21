import asyncio
from playwright.async_api import async_playwright
import json
import os
from datetime import datetime

# Список сторінок для моніторингу
PAGES = [
    {
        "service": "Amplitude",
        "url": "https://amplitude.com/releases",
        "wait_for": ".release-item, article, .changelog-entry"
    },
    {
        "service": "RevenueCat", 
        "url": "https://www.revenuecat.com/changelog",
        "wait_for": "article, .changelog-item, main"
    },
    {
        "service": "Sentry",
        "url": "https://sentry.io/changelog/",
        "wait_for": "article, .changelog, main"
    },
    {
        "service": "Customer.io",
        "url": "https://docs.customer.io/release-notes/",
        "wait_for": "article, main, .content"
    },
    {
        "service": "Algolia",
        "url": "https://www.algolia.com/changelog/",
        "wait_for": "article, .changelog, main"
    },
    {
        "service": "AppsFlyer",
        "url": "https://support.appsflyer.com/hc/en-us/sections/6551161473041",
        "wait_for": ".article-list, main"
    },
    {
        "service": "Firebase Android",
        "url": "https://firebase.google.com/support/release-notes/android",
        "wait_for": "article, main, .devsite-article"
    },
    {
        "service": "Firebase iOS",
        "url": "https://firebase.google.com/support/release-notes/ios",
        "wait_for": "article, main, .devsite-article"
    },
    {
        "service": "Apple Developer",
        "url": "https://developer.apple.com/news/releases/",
        "wait_for": "main, article, .column"
    },
    {
        "service": "Android Developer",
        "url": "https://developer.android.com/blog/posts/i-o-2026-what-s-new-in-google-play",
        "wait_for": "article, main, .devsite-article"
    },
]

async def scrape_page(page, service_info):
    print(f"Збираю дані з {service_info['service']}...")
    try:
        await page.goto(service_info["url"], wait_until="domcontentloaded", timeout=30000)
        # Чекаємо поки завантажиться контент
        try:
            await page.wait_for_selector(service_info["wait_for"], timeout=10000)
        except:
            pass
        await page.wait_for_timeout(2000)  # додаткові 2 сек для JS
        
        # Беремо текст з головної частини сторінки
        content = await page.evaluate("""() => {
            // Видаляємо непотрібне: навігацію, футер, скрипти
            const remove = document.querySelectorAll('nav, footer, script, style, header, .cookie-banner, .nav');
            remove.forEach(el => el.remove());
            
            // Беремо основний контент
            const main = document.querySelector('main, article, .content, .changelog, #content, body');
            return main ? main.innerText.slice(0, 8000) : document.body.innerText.slice(0, 8000);
        }""")
        
        return {
            "service": service_info["service"],
            "url": service_info["url"],
            "content": content,
            "scraped_at": datetime.now().isoformat(),
            "status": "ok"
        }
    except Exception as e:
        print(f"Помилка для {service_info['service']}: {e}")
        return {
            "service": service_info["service"],
            "url": service_info["url"],
            "content": "",
            "scraped_at": datetime.now().isoformat(),
            "status": f"error: {str(e)}"
        }

async def main():
    results = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        
        for service in PAGES:
            page = await context.new_page()
            result = await scrape_page(page, service)
            results.append(result)
            await page.close()
            await asyncio.sleep(1)  # пауза між запитами
        
        await browser.close()
    
    # Зберігаємо результат
    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Готово! Зібрано {len(results)} сервісів")
    
    # Надсилаємо в n8n якщо є webhook URL
    webhook_url = os.environ.get("N8N_WEBHOOK_URL")
    if webhook_url:
        import urllib.request
        data = json.dumps(results).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
        print("Дані надіслані в n8n!")

asyncio.run(main())
