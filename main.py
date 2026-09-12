from fastapi import FastAPI, Query, Body
from playwright.async_api import async_playwright
import urllib.parse
import asyncio
import json
import re

app = FastAPI(title="Competitor Price Engine", version="2.1.0")

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
        result = await fetch_and_evaluate(cleaned_url)
        raw_price = result.get("price")
        is_out_of_stock = result.get("is_out_of_stock", False)

        if raw_price:
            cleaned_price = parse_clean_number(raw_price)
            if cleaned_price and cleaned_price > 0:
                return {
                    "status": "success",
                    "price": cleaned_price,
                    "source": result.get("source"),
                    "url": cleaned_url
                }

        if is_out_of_stock:
            return {"status": "out_of_stock", "price": None, "url": cleaned_url}

        return {"status": "attention_needed", "price": None, "url": cleaned_url}

    except Exception as e:
        return {"status": "error", "message": str(e), "url": cleaned_url}


async def fetch_and_evaluate(url: str) -> dict:
    global browser
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-US,ar",
        viewport={"width": 1920, "height": 1080}
    )
    page = await context.new_page()

    try:
        # Load page and allow dynamic JavaScript/Turnstile to settle
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2.5)

        # Execute extraction directly in browser runtime
        eval_result = await page.evaluate("""() => {
            const bodyText = document.body ? document.body.innerText : "";

            // 1. Comprehensive Out of Stock Detection (English & Arabic)
            const outOfStockRegex = /(?:out of stock|sold out|temporarily unavailable|notify me|currently unavailable|item unavailable|غير متوفر|نفدت الكمية|نفذت الكمية|غير متاح|مباع بالكامل)/i;
            const isOutOfStock = outOfStockRegex.test(bodyText);

            // 2. Dubai Phone & WooCommerce: Variation active price
            const activeVarPrice = document.querySelector('.single_variation_wrap .woocommerce-Price-amount bdi, .woocommerce-variation-price .woocommerce-Price-amount bdi');
            if (activeVarPrice && activeVarPrice.innerText.trim()) {
                return { price: activeVarPrice.innerText, source: "woo_active_variation", is_out_of_stock: false };
            }

            // 3. Dubai Phone & WooCommerce: Primary Product Summary Price
            const summaryIns = document.querySelector('.summary ins .woocommerce-Price-amount bdi, .product-summary ins .woocommerce-Price-amount bdi, .entry-summary ins .woocommerce-Price-amount bdi');
            if (summaryIns && summaryIns.innerText.trim()) {
                return { price: summaryIns.innerText, source: "woo_summary_sale", is_out_of_stock: false };
            }

            const summaryPrice = document.querySelector('.summary .price .woocommerce-Price-amount bdi, .product-summary .price .woocommerce-Price-amount bdi, .entry-summary .price .woocommerce-Price-amount bdi, .price .woocommerce-Price-amount bdi');
            if (summaryPrice && summaryPrice.innerText.trim()) {
                return { price: summaryPrice.innerText, source: "woo_summary_regular", is_out_of_stock: false };
            }

            // 4. Dubai Phone: WooCommerce dataset fallback
            const varForm = document.querySelector('form.variations_form');
            if (varForm && varForm.dataset && varForm.dataset.product_variations) {
                try {
                    const variations = JSON.parse(varForm.dataset.product_variations);
                    if (Array.isArray(variations) && variations.length > 0) {
                        for (const v of variations) {
                            if (v.is_in_stock && v.display_price) {
                                return { price: v.display_price, source: "woo_dataset_instock", is_out_of_stock: false };
                            }
                        }
                        if (variations[0].display_price) {
                            return { price: variations[0].display_price, source: "woo_dataset_first", is_out_of_stock: false };
                        }
                    }
                } catch(e) {}
            }

            // 5. Sharaf DG Specific Selectors
            const sharafSpecial = document.querySelector('.special-price .price, .product-info-price .special-price, .pdp-price .special-price');
            if (sharafSpecial && sharafSpecial.innerText.trim()) {
                return { price: sharafSpecial.innerText, source: "sharaf_special", is_out_of_stock: false };
            }

            const sharafGeneral = document.querySelector('.product-info-price .price, .pdp-price .price');
            if (sharafGeneral && sharafGeneral.innerText.trim()) {
                return { price: sharafGeneral.innerText, source: "sharaf_general", is_out_of_stock: false };
            }

            // 6. Schema.org Product JSON-LD fallback
            const jsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of jsonScripts) {
                try {
                    const parsed = JSON.parse(s.innerText);
                    const items = Array.isArray(parsed) ? parsed : [parsed];
                    for (const item of items) {
                        const graph = item['@graph'] ? item['@graph'] : [item];
                        for (const g of graph) {
                            if (g['@type'] === 'Product' && g.offers) {
                                const off = Array.isArray(g.offers) ? g.offers[0] : g.offers;
                                const p = off.price || off.lowPrice;
                                if (p) return { price: p, source: "json_ld", is_out_of_stock: false };
                            }
                        }
                    }
                } catch(e) {}
            }

            // 7. OpenGraph Meta tag fallback
            const metaPrice = document.querySelector('meta[property="product:price:amount"], meta[property="og:price:amount"]');
            if (metaPrice && metaPrice.content) {
                return { price: metaPrice.content, source: "meta_tag", is_out_of_stock: false };
            }

            return { price: null, source: "none", is_out_of_stock: isOutOfStock };
        }""")

        return eval_result

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


def parse_clean_number(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()

    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-zA-Z0-9#]+;", " ", s)

    for curr in ["جنيه مصري", "جنيه", "ج.م.", "ج.م", "جم", "EGP", "egp", "LE", "L.E.", "le", "l.e.", "E£"]:
        s = s.replace(curr, " ")

    arabic_map = {"٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9","،":","}
    for ar, en in arabic_map.items():
        s = s.replace(ar, en)
    s = s.strip()

    # 1. European format (28.999,00 -> 28999.00)
    m_euro = re.search(r"\b(\d{1,3}(?:\.\d{3})+),(\d{1,2})\b", s)
    if m_euro:
        val = float(m_euro.group(1).replace(".", "") + "." + m_euro.group(2))
        return val if val > 0 else None

    # 2. Dot-thousands (28.999 -> 28999)
    m_dot = re.search(r"\b(\d{1,3})\.(\d{3})\b(?!\.\d)", s)
    if m_dot:
        val = float(m_dot.group(1) + m_dot.group(2))
        return val if val > 0 else None

    # 3. Comma-thousands (28,999 or 1,499.00)
    m_comma = re.search(r"\b(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?\b", s)
    if m_comma:
        int_part = m_comma.group(1).replace(",", "")
        dec_part = "." + m_comma.group(2) if m_comma.group(2) else ""
        val = float(int_part + dec_part)
        return val if val > 0 else None

    # 4. Standard digits
    m_plain = re.search(r"\b\d+(?:\.\d+)?\b", s)
    if m_plain:
        val = float(m_plain.group(0))
        return val if val > 0 else None

    return None
