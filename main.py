from fastapi import FastAPI, Query, Body
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import urllib.parse
import asyncio
import json
import re

app = FastAPI(title="Competitor Price Engine", version="4.1.0")

browser = None
playwright = None

@app.on_event("startup")
async def startup_event():
    global playwright, browser
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"
        ]
    )

@app.on_event("shutdown")
async def shutdown_event():
    global browser, playwright
    if browser:
        await browser.close()
    if playwright:
        await playwright.stop()

@app.get("/health")
def health():
    return {"status": "ok", "port": 5009}

@app.api_route("/get-price", methods=["GET", "POST"])
async def get_price(url: str = Query(None), payload: dict = Body(None)):
    target_url = url or (payload.get("url") if payload else None)
    if not target_url:
        return {"status": "error", "message": "Missing 'url' parameter"}

    cleaned_url = clean_tracking_params(target_url)

    try:
        html = await fetch_with_vercel_wait(cleaned_url)

        if is_out_of_stock_page(html):
            return {"status": "out_of_stock", "price": None, "url": cleaned_url}

        price = extract_price(cleaned_url, html)
        if price and price > 0:
            return {
                "status": "success",
                "price": price,
                "url": cleaned_url
            }

        return {"status": "attention_needed", "price": None, "url": cleaned_url}

    except Exception as e:
        return {"status": "error", "message": str(e), "url": cleaned_url}


async def fetch_with_vercel_wait(url: str) -> str:
    global browser
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-US,ar",
        viewport={"width": 1920, "height": 1080}
    )
    page = await context.new_page()

    # Mask navigator.webdriver
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)

        # Handle Vercel Security Checkpoint
        current_title = await page.title()
        if "Vercel" in current_title or "Security Checkpoint" in current_title:
            try:
                await page.wait_for_function("() => !document.title.includes('Vercel')", timeout=20000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2.0)
            except Exception:
                pass

        await asyncio.sleep(1.5)
        return await page.content()

    finally:
        await page.close()
        await context.close()


def clean_tracking_params(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if not parsed.query:
        return raw_url
    query_dict = urllib.parse.parse_qs(parsed.query)
    filtered = {
        k: v for k, v in query_dict.items()
        if k.startswith("attribute_") or k in ["variant", "promo"]
    }
    clean_query = urllib.parse.urlencode(filtered, doseq=True)
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, clean_query, parsed.fragment
    ))


def is_out_of_stock_page(html: str) -> bool:
    return bool(re.search(r"\b(out of stock|sold out|temporarily unavailable|notify me|currently unavailable|item unavailable|غير متوفر|نفدت الكمية|نفذت الكمية|غير متاح|مباع بالكامل)\b", html, re.I))


def extract_price(url: str, html: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")

    # 1. WooCommerce Variations (Match selected attribute like ?attribute_pa_colors=black)
    var_form = soup.select_one("form.variations_form")
    if var_form and var_form.get("data-product_variations"):
        try:
            raw_attr = var_form["data-product_variations"]
            variations = json.loads(raw_attr)
            parsed_url = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            attr_params = {k.lower(): v[0].lower().strip() for k, v in params.items() if k.startswith("attribute_")}

            if attr_params and isinstance(variations, list):
                for v in variations:
                    v_attrs = {str(k).lower(): str(val).lower().strip() for k, val in v.get("attributes", {}).items()}
                    if all(attr_params[k] in v_attrs.get(k, "") for k in attr_params):
                        if v.get("display_price"):
                            p = parse_clean_number(v["display_price"])
                            if p and p > 0: return p

            if isinstance(variations, list):
                for v in variations:
                    if v.get("is_in_stock") and v.get("display_price"):
                        p = parse_clean_number(v["display_price"])
                        if p and p > 0: return p

                if variations and variations[0].get("display_price"):
                    p = parse_clean_number(variations[0]["display_price"])
                    if p and p > 0: return p
        except Exception:
            pass

    # 2. Schema.org Product JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                graph = item.get("@graph", [item])
                for g in graph:
                    if g.get("@type") == "Product" and "offers" in g:
                        offers = g["offers"]
                        offer_list = offers if isinstance(offers, list) else [offers]
                        for off in offer_list:
                            raw = off.get("price") or off.get("lowPrice")
                            if raw:
                                p = parse_clean_number(raw)
                                if p and p > 0: return p
        except Exception:
            continue

    # 3. OpenGraph / Meta Tag Price
    for prop in ["product:price:amount", "og:price:amount", "price"]:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            p = parse_clean_number(tag["content"])
            if p and p > 0: return p

    # 4. Scoped Product Summary Price
    summary_price = soup.select(".summary p.price bdi, .product-summary .price bdi, div.entry-summary p.price bdi")
    for bdi in summary_price:
        p = parse_clean_number(bdi.get_text())
        if p and p > 0: return p

    # 5. Global BDI Tag Search
    for bdi in soup.find_all("bdi"):
        p = parse_clean_number(bdi.get_text())
        if p and p > 0: return p

    return None


def parse_clean_number(raw) -> float | None:
    if raw is None: return None
    s = str(raw).strip()

    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-zA-Z0-9#]+;", " ", s)

    for curr in ["جنيه مصري", "جنيه", "ج.م.", "ج.م", "جم", "EGP", "egp", "LE", "L.E.", "le", "l.e.", "E£"]:
        s = s.replace(curr, " ")

    arabic_map = {"٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9","،":","}
    for ar, en in arabic_map.items():
        s = s.replace(ar, en)
    s = s.strip()

    m_euro = re.search(r"\b(\d{1,3}(?:\.\d{3})+),(\d{1,2})\b", s)
    if m_euro:
        val = float(m_euro.group(1).replace(".", "") + "." + m_euro.group(2))
        return val if val > 0 else None

    m_dot = re.search(r"\b(\d{1,3})\.(\d{3})\b(?!\.\d)", s)
    if m_dot:
        val = float(m_dot.group(1) + m_dot.group(2))
        return val if val > 0 else None

    m_comma = re.search(r"\b(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?\b", s)
    if m_comma:
        int_part = m_comma.group(1).replace(",", "")
        dec_part = "." + m_comma.group(2) if m_comma.group(2) else ""
        val = float(int_part + dec_part)
        return val if val > 0 else None

    m_plain = re.search(r"\b\d+(?:\.\d+)?\b", s)
    if m_plain:
        val = float(m_plain.group(0))
        return val if val > 0 else None

    return None
