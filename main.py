from fastapi import FastAPI, Query, Body
from curl_cffi import requests
from bs4 import BeautifulSoup
import urllib.parse
import json
import re

app = FastAPI(title="Competitor Price Engine", version="4.2.0")

@app.get("/health")
def health():
    return {"status": "ok", "port": 5009}

@app.api_route("/get-price", methods=["GET", "POST"])
def get_price(url: str = Query(None), payload: dict = Body(None)):
    target_url = url or (payload.get("url") if payload else None)
    if not target_url:
        return {"status": "error", "message": "Missing 'url' parameter"}

    cleaned_url = clean_tracking_params(target_url)

    try:
        # Use curl_cffi impersonating Chrome 124 to bypass TLS fingerprint blocks on Vercel/Cloudflare
        session = requests.Session(impersonate="chrome124")
        domain = urllib.parse.urlparse(cleaned_url).netloc
        
        resp = session.get(
            cleaned_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Referer": f"https://{domain}/"
            },
            timeout=15,
            allow_redirects=True
        )

        code = resp.status_code
        if code in [404, 410]:
            return {"status": "invalid_link", "code": code, "url": cleaned_url}

        if code in [403, 429, 503]:
            return {"status": "blocked", "code": code, "url": cleaned_url}

        if code != 200:
            return {"status": "error", "code": code, "url": cleaned_url}

        html = resp.text

        # Check for Vercel checkpoint or out of stock
        if "Vercel Security Checkpoint" in html:
            return {"status": "blocked", "message": "Vercel Checkpoint", "url": cleaned_url}

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


def is_out_of_stock_page(html: str) -> bool:
    return bool(re.search(r"\b(out of stock|sold out|temporarily unavailable|notify me|currently unavailable|item unavailable|غير متوفر|نفدت الكمية|نفذت الكمية|غير متاح|مباع بالكامل)\b", html, re.I))


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

    # 1. WooCommerce Variations (Dubai Phone)
    var_form = soup.select_one("form.variations_form")
    if var_form and var_form.get("data-product_variations"):
        try:
            variations = json.loads(var_form["data-product_variations"])
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

    # 2. Schema.org JSON-LD
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

    # 3. Meta Tags
    for prop in ["product:price:amount", "og:price:amount", "price"]:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            p = parse_clean_number(tag["content"])
            if p and p > 0: return p

    # 4. Global BDI search
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
