from fastapi import FastAPI, Query, Body
from curl_cffi import requests
from bs4 import BeautifulSoup
import urllib.parse
import json
import re

app = FastAPI(title="Competitor Price Scraper Service", version="1.0.0")

@app.get("/health")
def health_check():
    return {"status": "ok", "port": 5009}

@app.api_route("/get-price", methods=["GET", "POST"])
def get_price(
    url: str = Query(None),
    payload: dict = Body(None)
):
    target_url = url or (payload.get("url") if payload else None)
    if not target_url:
        return {"status": "error", "message": "Missing 'url' parameter"}

    # Strip marketing trackers (gclid, gbraid) while keeping variants/attributes
    cleaned_url = clean_tracking_params(target_url)

    try:
        # Impersonate Chrome 124 browser TLS/JA3 handshake to bypass Cloudflare
        resp = requests.get(
            cleaned_url,
            impersonate="chrome124",
            headers={
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                "Upgrade-Insecure-Requests": "1"
            },
            timeout=15,
            allow_redirects=True
        )
    except Exception as e:
        return {"status": "invalid_link", "message": str(e), "url": cleaned_url}

    if resp.status_code in [404, 410]:
        return {"status": "invalid_link", "code": resp.status_code, "url": cleaned_url}

    if resp.status_code == 403:
        return {"status": "blocked", "code": 403, "url": cleaned_url}

    if resp.status_code != 200:
        return {"status": "error", "code": resp.status_code, "url": cleaned_url}

    html = resp.text

    # Check for soft 404 / search redirect pages
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.DOTALL)
    title = title_match.group(1).lower() if title_match else ""
    if any(k in title for k in ["404", "not found", "search results for", "لا توجد نتائج"]):
        return {"status": "invalid_link", "message": "Page redirected to 404/search"}

    # Check for out of stock status
    is_out_of_stock = bool(re.search(r"\b(out of stock|غير متوفر|نفذت الكمية|غير متاح|مباع بالكامل)\b", html, re.I))

    # Extract price using prioritized parsers
    price = extract_price(cleaned_url, html)

    if price and price > 0:
        return {
            "status": "success",
            "price": price,
            "currency": "EGP",
            "url": cleaned_url
        }

    if is_out_of_stock:
        return {"status": "out_of_stock", "price": None, "url": cleaned_url}

    return {"status": "attention_needed", "price": None, "url": cleaned_url}


def clean_tracking_params(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if not parsed.query:
        return raw_url
    query_dict = urllib.parse.parse_qs(parsed.query)
    # Keep only e-commerce attributes & Shopify variants
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
        # Match 'SAVE ... EGP [PRICE]'
        m_save = re.search(r"SAVE\s+(?:\d+%\s+|EGP\s*[\d,.]+\.?\s+)+EGP\s*([\d,.]+)", html, re.I)
        if m_save:
            p = parse_clean_number(m_save.group(1))
            if p: return p

        # Match 'EGP [PRICE]' before checkout tags
        m_egp = re.search(r"EGP\s*([\d,.]+)\.?\s*(?:Easy Payment Plans|Inclusive of VAT|Standard Delivery)", html, re.I)
        if m_egp:
            p = parse_clean_number(m_egp.group(1))
            if p: return p

    # 2. Dubai Phone & WooCommerce Variations (attribute_pa_colors)
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

            # Fallback to first in-stock variation
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
                # Handle @graph wrapper
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

    # 5. Scoped WooCommerce Summary (ignores ValU/installment banners)
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
    if raw is None:
        return None
    s = str(raw).strip()

    # Remove tags and entities
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-zA-Z0-9#]+;", " ", s)

    # Strip currency labels
    for curr in ["جنيه مصري", "جنيه", "ج.م.", "ج.م", "جم", "EGP", "egp", "LE", "L.E.", "le", "l.e.", "E£"]:
        s = s.replace(curr, " ")

    # Map Arabic numerals
    arabic_map = {"٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9","،":","}
    for ar, en in arabic_map.items():
        s = s.replace(ar, en)
    s = s.strip()

    # 1. European format: 28.999,00 -> 28999.00
    m_euro = re.search(r"\b(\d{1,3}(?:\.\d{3})+),(\d{1,2})\b", s)
    if m_euro:
        val = float(m_euro.group(1).replace(".", "") + "." + m_euro.group(2))
        return val if val > 0 else None

    # 2. Dot-thousands: 28.999 -> 28999
    m_dot = re.search(r"\b(\d{1,3})\.(\d{3})\b(?!\.\d)", s)
    if m_dot:
        val = float(m_dot.group(1) + m_dot.group(2))
        return val if val > 0 else None

    # 3. Comma-thousands: 28,999 or 28,999.00
    m_comma = re.search(r"\b(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?\b", s)
    if m_comma:
        int_part = m_comma.group(1).replace(",", "")
        dec_part = "." + m_comma.group(2) if m_comma.group(2) else ""
        val = float(int_part + dec_part)
        return val if val > 0 else None

    # 4. Standard integers/floats
    m_plain = re.search(r"\b\d+(?:\.\d+)?\b", s)
    if m_plain:
        val = float(m_plain.group(0))
        return val if val > 0 else None

    return None
