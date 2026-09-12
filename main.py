import time
import random
from fastapi import FastAPI, Query, Body
from curl_cffi import requests
from bs4 import BeautifulSoup
import urllib.parse
import json
import re

app = FastAPI(title="Competitor Price Engine", version="3.1.0")

@app.get("/health")
def health():
    return {"status": "ok", "port": 5009}

@app.api_route("/get-price", methods=["GET", "POST"])
def get_price(url: str = Query(None), payload: dict = Body(None)):
    target_url = url or (payload.get("url") if payload else None)
    if not target_url:
        return {"status": "error", "message": "Missing 'url' parameter"}

    cleaned_url = clean_tracking_params(target_url)

    # Introduce a polite human-like delay (1.5 to 3.2 seconds) to avoid HTTP 429 Rate Limits
    time.sleep(random.uniform(1.5, 3.2))

    try:
        session = requests.Session(impersonate="chrome124")
        domain = urllib.parse.urlparse(cleaned_url).netloc
        
        resp = session.get(
            cleaned_url,
            headers={
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
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
