from fastapi import FastAPI, Query, Body
from curl_cffi import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import urllib.parse
import asyncio
import json
import re

app = FastAPI(title="Competitor Price Engine", version="2.0.0")

# Persistent browser instance across requests
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

    # -------------------------------------------------------------
    # TIER 1: FAST PATH via curl_cffi (No custom headers to prevent 403)
    # -------------------------------------------------------------
    try:
        session = requests.Session(impersonate="chrome")
        resp = session.get(cleaned_url, timeout=12, allow_redirects=True)
        html = resp.text
        code = resp.status_code

        # Check for Cloudflare interstitial or hard blocks
        is_cf = (
            code in [403, 503] or
            "One moment, please..." in html or
            "Just a moment..." in html or
            "Attention Required!" in html or
            "cloudflare" in html.lower() and code != 200
        )

        if not is_cf and code == 200:
            price = extract_price(cleaned_url, html)
            if price and price > 0:
                return {"status": "success", "price": price, "method": "curl_cffi", "url": cleaned_url}
            if is_out_of_stock_page(html):
                return {"status": "out_of_stock", "price": None, "url": cleaned_url}
    except Exception:
        pass

    # -------------------------------------------------------------
    # TIER 2: BROWSER FALLBACK (Bypasses Cloudflare Turnstile)
    # -------------------------------------------------------------
    try:
        page_html = await fetch_with_browser(cleaned_url)
        price = extract_price(cleaned_url, page_html)

        if price and price > 0:
            return {"status": "success", "price": price, "method": "browser_bypass", "url": cleaned_url}

        if is_out_of_stock_page(page_html):
            return {"status": "out_of_stock", "price": None, "url": cleaned_url}

        return {"status": "attention_needed", "price": None, "url": cleaned_url}
    except Exception as e:
        return {"status": "error", "message": str(e), "url": cleaned_url}


async def fetch_with_browser(url: str) -> str:
    global browser
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-US",
        viewport={"width": 1920, "height": 1080}
    )
    page = await context.new_page()
    try:
        # Load page and wait for Turnstile JS challenge to clear
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(3.5)
        content = await page.content()
        return content
    finally:
        await page.close()
        await context.close()


def is_out_of_stock_page(html: str) -> bool:
    return bool(re.search(r"\b(out of stock|غير متوفر|نفذت الكمية|غير متاح|مباع بالكامل)\b", html, re.I))


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


def extract_price(url: str, html: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Sharaf DG Patterns
    if "sharafdg.com" in url:
        m_save = re.search(r"SAVE\s+(?:\d+%\s+|EGP\s*[\d,.]+\.?\s+)+EGP\s*([\d,.]+)", html, re.I)
        if m_save:
            p = parse_clean_number(m_save.group(1))
            if p: return p

        m_egp = re.search(r"EGP\s*([\d,.]+)\.?\s*(?:Easy Payment Plans|Inclusive of VAT|Standard Delivery)", html, re.I)
        if m_egp:
            p = parse_clean_number(m_egp.group(1))
            if p: return p

    # 2. Dubai Phone & WooCommerce Variations
    var_form = soup.select_one("form.variations_form")
    if var_form and var_form.get("data-product_variations"):
        try:
            variations = json.loads(var_form["data-product_variations"])
            parsed_url = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            attr_params = {k: v[0].lower() for k, v in params.items() if k.startswith("attribute_")}

            if attr_params:
                for v in variations:
                    v_attrs = {str(k).lower(): str(val).lower() for k, val in v.get("attributes", {}).items()}
                    if all(attr_params[k] in v_attrs.get(k, "") for k in attr_params):
                        if v.get("display_price"):
                            return parse_clean_number(v["display_price"])

            for v in variations:
                if v.get("is_in_stock") and v.get("display_price"):
                    return parse_clean_number(v["display_price"])

            if variations and variations[0].get("display_price"):
                return parse_clean_number(variations[0]["display_price"])
        except Exception:
            pass

    # 3. Schema.org Product JSON-LD
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
                                if p: return p
        except Exception:
            continue

    # 4. OpenGraph & Meta Tags
    for prop in ["product:price:amount", "og:price:amount", "price"]:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            p = parse_clean_number(tag["content"])
            if p: return p

    # 5. Scoped WooCommerce Product Summary
    summary = soup.select_one(".summary.entry-summary, .product-info, .entry-summary")
    search_context = summary if summary else soup

    ins = search_context.select_one("ins .woocommerce-Price-amount bdi, ins .amount bdi")
    if ins:
        p = parse_clean_number(ins.text)
        if p: return p

    regular = search_context.select_one(".price .woocommerce-Price-amount bdi, .price .amount bdi")
    if regular:
        p = parse_clean_number(regular.text)
        if p: return p

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

    # European format: 28.999,00 -> 28999.00
    m_euro = re.search(r"\b(\d{1,3}(?:\.\d{3})+),(\d{1,2})\b", s)
    if m_euro:
        val = float(m_euro.group(1).replace(".", "") + "." + m_euro.group(2))
        return val if val > 0 else None

    # Dot-thousands: 28.999 -> 28999
    m_dot = re.search(r"\b(\d{1,3})\.(\d{3})\b(?!\.\d)", s)
    if m_dot:
        val = float(m_dot.group(1) + m_dot.group(2))
        return val if val > 0 else None

    # Comma-thousands: 28,999 or 28,999.00
    m_comma = re.search(r"\b(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?\b", s)
    if m_comma:
        int_part = m_comma.group(1).replace(",", "")
        dec_part = "." + m_comma.group(2) if m_comma.group(2) else ""
        val = float(int_part + dec_part)
        return val if val > 0 else None

    # Plain integers and floats
    m_plain = re.search(r"\b\d+(?:\.\d+)?\b", s)
    if m_plain:
        val = float(m_plain.group(0))
        return val if val > 0 else None

    return None
