#!/usr/bin/env python3
"""
GPU deal watcher for bazaraki.com and vendora.cy (Cyprus), plus eBay through
its official Browse API once you have stored developer keys (--setup-ebay), plus
the Cypriot computer shops Armenius, Bionic, Custom PC and ComputerG.

It also watches complete desktop PCs on the marketplaces and in the shops (config
"pcs") and alerts when one costs clearly less than its parts bought separately.

Scans the newest GPU listings on both sites, recognises the card model from the
title, checks the VRAM requirement and compares the price against a per-model
ceiling (never above your overall budget).  New matches are pushed as a Windows
toast and, if configured, a Telegram message.  Already-seen listings are
remembered in seen.json so you are only alerted once per listing (or again if
its price drops under the ceiling).

Usage:
  python gpu_alert.py                one scan, alert on new deals
  python gpu_alert.py --loop         scan forever every config["poll_minutes"]
  python gpu_alert.py --list         print every recognised GPU listing with its
                                     price vs. ceiling (handy to tune config.json)
  python gpu_alert.py --dry-run      show what would be alerted, change nothing
  python gpu_alert.py --test-notify  send a test toast/Telegram message
  python gpu_alert.py --setup-telegram <TOKEN>   link your Telegram bot (auto-detects chat id)
  python gpu_alert.py --setup-ebay   store your eBay developer keys, check them, run a test search

Standard library only - no pip installs needed.
"""
import argparse
import base64
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOCAL_CONFIG_PATH = os.path.join(BASE, "config.local.json")   # secrets, git-ignored
STATE_PATH = os.path.join(BASE, "seen.json")
ALERT_LOG = os.path.join(BASE, "alerts.log")
SCAN_LOG = os.path.join(BASE, "scan.log")
TOAST_PS1 = os.path.join(BASE, "toast.ps1")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

BAZARAKI_GPU_URL = "https://www.bazaraki.com/computers-and-games/hardware/gpu/?ordering=newest&page={page}"
VENDORA_PARTS_URL = "https://vendora.cy/browse/qrzg1v/components-accessories.html?sort=recent&page={page}"
VENDORA_SEARCH_URL = "https://vendora.cy/browse/7vwxzr/computers-tablets-peripherals.html?sort=recent&q={q}"
# site-wide keyword searches, used by the "keyword_watches" in config.json
BAZARAKI_SEARCH_URL = "https://www.bazaraki.com/search/?q={q}&ordering=newest"
VENDORA_ANY_SEARCH_URL = "https://vendora.cy/items?q={q}&sort=recent"


# --------------------------------------------------------------------------- utils
def log(msg, path=SCAN_LOG):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        print(line)
    except (OSError, ValueError):
        pass  # pythonw has no console
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch(url, retries=2, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en,el;q=0.8"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    log(f"FETCH FAILED {url}: {last}")
    return ""


def parse_price(text):
    """'€1.028,91' -> 1028.91 ; '€ 250' -> 250.0 ; '€220' -> 220.0 ; junk -> None"""
    if not text:
        return None
    s = html.unescape(text).replace("\xa0", " ").strip()
    s = re.sub(r"[€\sa-zA-Z]", "", s)
    if not re.search(r"\d", s):
        return None
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, tail = s.rsplit(",", 1)
        s = head + "." + tail if len(tail) == 2 else s.replace(",", "")
    elif "." in s:
        head, tail = s.rsplit(".", 1)
        if len(tail) == 3:  # 1.028 -> thousands separator
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def normalise_title(title):
    t = html.unescape(title).lower()
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"\b(rtx|gtx|rx|arc)(\d)", r"\1 \2", t)               # rtx4070 -> rtx 4070
    t = re.sub(r"(\d)(xtx|xt|tis|ti|gre|super|gb|g)\b", r"\1 \2", t)  # 9060xt -> 9060 xt
    t = re.sub(r"\btis\b", "ti super", t)
    t = re.sub(r"\s+", " ", t)
    return t


def vram_in_title(norm):
    """Return list of GB numbers mentioned like '12gb', '16 gb', '8192 mb', or else '12g'.
    A bare 'g' is only used when no 'gb' is written: part numbers end in it too
    (Sapphire 11350-03-20G is an 8GB card)."""
    sizes = [int(n) for n in re.findall(r"\b(\d{1,2})\s*gb\b", norm)]
    sizes += [round(int(n) / 1024) for n in re.findall(r"\b(\d{4,5})\s*mb\b", norm)]   # 8192 MB
    return sizes or [int(n) for n in re.findall(r"\b(\d{1,2})\s*g\b", norm)]


# Words naming a GPU maker (matched on the normalised title). A title that names only
# another maker is not a match: a 2004 "GeForce 6800" is not an RX 6800.
BRAND_WORDS = {
    "NVIDIA": re.compile(r"\b(nvidia|geforce|rtx|gtx|quadro|tesla)\b"),
    "AMD": re.compile(r"\b(amd|radeon|rx|ati)\b"),
    "Intel": re.compile(r"\b(intel|arc)\b"),
}
# memory given in MB means a card from the 2000s that shares a model number (GeForce 6800 128MB)
OLD_CARD_MEMORY = re.compile(r"\b(32|64|128|256|512|768)\s*mb?\b")


def gpu_brand(gpu):
    if gpu.get("brand"):
        return gpu["brand"]
    name = gpu["name"].lower()
    return "NVIDIA" if name.startswith(("rtx", "gtx")) else "AMD" if name.startswith("rx") else \
        "Intel" if name.startswith("arc") else None


# ------------------------------------------------------------------------ scrapers
def scrape_bazaraki(pages):
    return scrape_bazaraki_urls([BAZARAKI_GPU_URL.format(page=p) for p in range(1, pages + 1)])


def scrape_bazaraki_urls(urls):
    out = []
    for url in urls:
        h = fetch(url)
        if not h:
            continue
        chunks = h.split('data-marker="listing-advert_card-advert:')[1:]
        for ch in chunks:
            m = re.match(r"(\d+)", ch)
            if not m:
                continue
            ad_id = m.group(1)
            href = re.search(r'href="(/adv/[^"]+)"', ch)
            name = re.search(r'itemProp="name" content="([^"]*)"', ch)
            price = re.search(r'itemProp="price" content="([^"]*)"', ch)
            when = re.search(r'm-0 text-sm">([^<]*)', ch)
            alt = re.search(r'<img src="[^"]*" alt="([^"]*)" class="h-full', ch)
            seller = re.search(r'alt="([^"]*)" class="max-h-10', ch)
            location = ""
            if alt and ", " in alt.group(1):
                location = html.unescape(alt.group(1).rsplit(", ", 1)[1])
            out.append({
                "site": "bazaraki",
                "id": "bz:" + ad_id,
                "title": html.unescape(name.group(1)) if name else "",
                "price": parse_price(price.group(1)) if price else None,
                "url": "https://www.bazaraki.com" + href.group(1) if href else "",
                "when": html.unescape(when.group(1)).strip() if when else "",
                "location": location,
                "seller": html.unescape(seller.group(1)) if seller else "",
            })
        time.sleep(1.5)
    return out


def scrape_vendora(pages, searches):
    urls = [VENDORA_PARTS_URL.format(page=p) for p in range(1, pages + 1)]
    urls += [VENDORA_SEARCH_URL.format(q=urllib.parse.quote(q)) for q in searches]
    return scrape_vendora_urls(urls)


def scrape_vendora_urls(urls):
    out, seen = [], set()
    for url in urls:
        h = fetch(url)
        if not h:
            continue
        chunks = h.split('class="card vCard card-product"')[1:]
        for ch in chunks:
            href = re.search(r'href="(https://vendora\.cy/items/[^"]+)"', ch)
            vid = re.search(r'data-id="([a-z0-9]+)"', ch)
            title = re.search(r'<span class="body-m">([^<]*)', ch)
            price = re.search(r'<span class="label-l[^"]*">\s*([^<]*?)\s*<', ch)
            if not vid or vid.group(1) in seen:
                continue
            seen.add(vid.group(1))
            out.append({
                "site": "vendora",
                "id": "vd:" + vid.group(1),
                "title": html.unescape(title.group(1)).strip() if title else "",
                "price": parse_price(price.group(1)) if price else None,
                "url": href.group(1) if href else "",
                "when": "",
                "location": "",
                "seller": "",
            })
        time.sleep(1.5)
    return out


# ---------------------------------------------------------------------------- eBay
# Official Browse API (developer.ebay.com). eBay's User Agreement forbids scraping
# the website, so eBay is only ever queried through this API with your own keys.
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_TOKEN_PATH = os.path.join(BASE, "ebay_token.json")   # cached 2-hour app token, git-ignored
EBAY_GPU_CATEGORY = "27386"                                # Graphics/Video Cards (same id on ebay.de)
EBAY_DEFAULT_GPU_SEARCHES = [
    "(2060, 3060, 3080, 3090, 4070, 4080, 4090, 5060, 5070, 5080, 5090)",
    "(6700, 6750, 6800, 6900, 6950, 7600, 7700, 7800, 7900, 9060, 9070, a770, b580)",
]
EBAY_SITE_NAMES = {"EBAY_DE": "ebay.de", "EBAY_AT": "ebay.at", "EBAY_IT": "ebay.it", "EBAY_FR": "ebay.fr",
                   "EBAY_ES": "ebay.es", "EBAY_NL": "ebay.nl", "EBAY_BE": "ebay.be", "EBAY_IE": "ebay.ie"}
EBAY_CONDITIONS = {"1000": "New", "1500": "New (other)", "1750": "New with defects",
                   "2000": "Certified refurbished", "2010": "Refurbished - excellent",
                   "2020": "Refurbished - very good", "2030": "Refurbished - good",
                   "2500": "Seller refurbished", "3000": "Used", "7000": "For parts"}
EU_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
                "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


class EbayError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"HTTP {status}: {msg}" if status else msg)
        self.status = status


def ebay_request(url, headers, data=None):
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise EbayError(e.code, e.read().decode("utf-8", "replace")[:300]) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise EbayError(0, str(e)) from None


def ebay_creds(cfg):
    """Environment variables win, then "ebay_keys" in config.local.json."""
    keys = cfg.get("ebay_keys") or {}
    cid = os.environ.get("EBAY_CLIENT_ID") or keys.get("client_id", "")
    secret = os.environ.get("EBAY_CLIENT_SECRET") or keys.get("client_secret", "")
    return cid.strip(), secret.strip()


def ebay_token(cfg, force=False):
    """Application token (client credentials grant), cached until shortly before it expires."""
    cid, secret = ebay_creds(cfg)
    if not cid or not secret:
        raise EbayError(0, "no eBay keys configured (run: python gpu_alert.py --setup-ebay)")
    cached = load_json(EBAY_TOKEN_PATH, {}) or {}
    if not force and cached.get("client_id") == cid and cached.get("expires_at", 0) > time.time() + 300:
        return cached["access_token"]
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": EBAY_SCOPE}).encode()
    data = ebay_request(EBAY_TOKEN_URL, {"Authorization": "Basic " + basic,
                                         "Content-Type": "application/x-www-form-urlencoded"}, body)
    token = data.get("access_token")
    if not token:
        raise EbayError(0, "eBay token response had no access_token")
    try:
        save_json(EBAY_TOKEN_PATH, {"client_id": cid, "access_token": token,
                                    "expires_at": time.time() + int(data.get("expires_in") or 7200)})
    except OSError:
        pass
    return token


def ebay_search(cfg, token, marketplace, q, category=None, filters=(), limit=200):
    ecfg = cfg.get("ebay") or {}
    params = {"q": q, "sort": "newlyListed", "limit": str(limit)}
    if category:
        params["category_ids"] = str(category)
    if filters:
        params["filter"] = ",".join(filters)
    url = EBAY_SEARCH_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    country = str(ecfg.get("deliver_to", "CY")).upper()
    headers = {"Authorization": "Bearer " + token,
               "X-EBAY-C-MARKETPLACE-ID": marketplace,
               # buyer location, so shipping costs are estimated for delivery to Cyprus
               "X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(f"country={country}", safe=""),
               "Accept": "application/json"}
    return ebay_request(url, headers).get("itemSummaries") or []


def _eur(amount):
    amount = amount or {}
    if amount.get("currency") != "EUR":
        return None
    try:
        return float(amount.get("value"))
    except (TypeError, ValueError):
        return None


def ebay_item_to_listing(item, marketplace, cfg):
    """Browse API item summary -> the bot's listing dict. 'price' is what it costs delivered to you."""
    ecfg = cfg.get("ebay") or {}
    value = _eur(item.get("price"))
    ship = None
    for opt in item.get("shippingOptions") or []:
        cost = _eur(opt.get("shippingCost"))
        if cost is not None:
            ship = cost if ship is None else min(ship, cost)
    loc = item.get("itemLocation") or {}
    country = str(loc.get("country") or "").upper()
    seller = item.get("seller") or {}
    score = seller.get("feedbackScore")
    notes, total = [], None
    if value is not None:
        total = value + (ship or 0)
        if ship is None:
            notes.append("shipping cost not shown - check it in the listing")
        elif ship == 0:
            notes.append(f"item {value:.0f}, free shipping")
        else:
            notes.append(f"item {value:.0f} + shipping {ship:.0f}")
        vat = float(ecfg.get("non_eu_import_vat_pct", 19) or 0)
        if country and country not in EU_COUNTRIES and vat:
            total *= 1 + vat / 100
            notes.append(f"ships from {country}: {vat:.0f}% import VAT already added to the price, customs fee possible")
    if item.get("itemGroupHref") or item.get("itemGroupType"):
        notes.append("listing has several variants - the price may be for a different card")
    if isinstance(score, int) and score < int(ecfg.get("warn_feedback_below", 10)):
        notes.append(f"seller has only {score} ratings - pay through eBay checkout only")
    # items listed on other eBay sites carry their condition in that language (Usato, Occasion...)
    condition = EBAY_CONDITIONS.get(str(item.get("conditionId") or ""), item.get("condition"))
    if condition:
        notes.append(str(condition))
    when = str(item.get("itemCreationDate") or "")[:16].replace("T", " ")
    who = seller.get("username", "")
    if who and score is not None:
        who += f" ({score} ratings, {seller.get('feedbackPercentage', '?')}%)"
    return {
        "site": EBAY_SITE_NAMES.get(marketplace, marketplace.lower()),
        "id": "eb:" + str(item.get("legacyItemId") or item.get("itemId")),
        "title": item.get("title", ""),
        "price": round(total, 2) if total is not None else None,
        "url": item.get("itemWebUrl", "").split("?", 1)[0],   # drop eBay's long tracking query
        "skip_if_suspiciously_cheap": bool(ecfg.get("skip_suspiciously_cheap", True)),
        "when": f"listed {when} UTC" if when else "",
        "location": ", ".join(x for x in [loc.get("city"), country] if x),
        "seller": who,
        "ceiling_factor": float(ecfg.get("ceiling_factor", 1.0) or 1.0),
        "extra_notes": notes,
    }


def ebay_due(cfg, force=False):
    """True when eBay keys exist and every_minutes have passed since the last eBay search."""
    ecfg = cfg.get("ebay") or {}
    if not ecfg.get("enabled", True) or not all(ebay_creds(cfg)):
        return False
    if force:
        return True
    last = (load_json(STATE_PATH, {}) or {}).get("_meta", {}).get("ebay_last", 0)
    return time.time() - float(last or 0) >= 60 * float(ecfg.get("every_minutes", 10)) - 30


def ebay_scan(cfg):
    """GPU searches plus keyword watches marked "ebay": true.
    Returns ([(listing, watch or None)], error text or None)."""
    ecfg = cfg.get("ebay") or {}
    budget = float(cfg.get("budget_eur", 600))
    jobs = [(q, ecfg.get("gpu_category", EBAY_GPU_CATEGORY), budget, None)
            for q in ecfg.get("gpu_searches") or EBAY_DEFAULT_GPU_SEARCHES]
    for w in cfg.get("keyword_watches", []):
        terms = w.get("ebay_searches") or w.get("searches") or []
        if w.get("ebay") and terms:
            q = terms[0] if len(terms) == 1 else "(" + ", ".join(terms) + ")"
            jobs.append((q, w.get("ebay_category"), w.get("max_eur"), w))
    excl = [re.compile(p, re.I) for p in ecfg.get("exclude_title_patterns", [])]
    country = str(ecfg.get("deliver_to", "CY")).upper()
    out, seen = [], set()
    try:
        token = ebay_token(cfg)
        for mp in ecfg.get("marketplaces") or ["EBAY_DE"]:
            for q, category, max_price, watch in jobs:
                filters = ["buyingOptions:{FIXED_PRICE}", f"deliveryCountry:{country}"]
                if max_price:
                    filters += [f"price:[..{float(max_price):g}]", "priceCurrency:EUR"]
                try:
                    items = ebay_search(cfg, token, mp, q, category, filters)
                except EbayError as e:
                    if e.status != 401:
                        raise
                    token = ebay_token(cfg, force=True)   # token expired early: renew once
                    items = ebay_search(cfg, token, mp, q, category, filters)
                for it in items:
                    if "AUCTION" in (it.get("buyingOptions") or []):
                        continue   # auction price is not final
                    if str(it.get("conditionId")) == "7000":
                        continue   # for parts / not working
                    if any(rx.search(it.get("title", "")) for rx in excl):
                        continue
                    lst = ebay_item_to_listing(it, mp, cfg)
                    key = (lst["id"], watch["name"] if watch else "")
                    if key not in seen:
                        seen.add(key)
                        out.append((lst, watch))
                time.sleep(0.5)
    except EbayError as e:
        log(f"EBAY FAILED: {e}")
        return out, str(e)
    return out, None


def setup_ebay(cfg):
    """Ask for the eBay production keys, check them with eBay, store them in config.local.json."""
    print("In developer.ebay.com open Application Keys and use the Production keyset:\n"
          "App ID = Client ID, Cert ID = Client Secret.")
    cid = os.environ.get("EBAY_CLIENT_ID") or input("App ID (Client ID): ")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not secret:
        try:
            import getpass
            secret = getpass.getpass("Cert ID (Client Secret, hidden while you paste): ")
        except Exception:  # noqa: BLE001
            secret = input("Cert ID (Client Secret): ")
    cid, secret = (cid or "").strip(), (secret or "").strip()
    if not cid or not secret:
        sys.exit("Both keys are needed.")
    if "-SBX-" in cid.upper():
        sys.exit("That is a Sandbox key (it contains SBX). Use the Production keyset instead.")
    cfg = dict(cfg, ebay_keys={"client_id": cid, "client_secret": secret})
    try:
        token = ebay_token(cfg, force=True)
    except EbayError as e:
        sys.exit(f"eBay did not accept these keys: {e}\nIf the keyset is shown as disabled, finish the "
                 "'marketplace account deletion' exemption in the developer portal, then retry.")
    local = load_json(LOCAL_CONFIG_PATH, {}) or {}
    local["ebay_keys"] = cfg["ebay_keys"]
    try:
        save_json(LOCAL_CONFIG_PATH, local)
    except OSError as e:
        sys.exit(f"could not write config.local.json: {e}")
    ecfg = cfg.get("ebay") or {}
    mp = (ecfg.get("marketplaces") or ["EBAY_DE"])[0]
    country = str(ecfg.get("deliver_to", "CY")).upper()
    try:
        items = ebay_search(cfg, token, mp, "rtx 4070", ecfg.get("gpu_category", EBAY_GPU_CATEGORY),
                            ["buyingOptions:{FIXED_PRICE}", f"deliveryCountry:{country}"], limit=5)
    except EbayError as e:
        sys.exit(f"Keys saved to config.local.json, but the test search failed: {e}")
    print(f"eBay keys work and are saved in config.local.json. Test search on {mp} for 'rtx 4070' "
          f"shipping to {country}:")
    for it in items:
        lst = ebay_item_to_listing(it, mp, cfg)
        print(f"  EUR {lst['price'] or 0:7.0f}  {lst['title'][:70]}  ({lst['location']})")
    if not items:
        print("  (no results - unusual, but the keys themselves are fine)")
    print("The scheduled scans will now include eBay automatically.")


# --------------------------------------------------------------------- retail shops
# Cypriot computer shops (new cards, a few refurbished). Each one is read only through
# pages its robots.txt allows (no search / sort / items-per-page URLs), at its crawl-delay,
# with a user agent that says what this is. needy.shop is deliberately missing: its terms
# forbid scraping and its agent policy (needy.shop/agents.md) only allows automated access
# through its UCP catalog API with a published, identifiable agent profile.
SHOP_UA = "gpu-alert-bot/1.0 (personal GPU price alert; read-only)"
ARMENIUS_API = ("https://armenius.com.cy/wp-json/wc/store/v1/products"
                "?category=gpu-graphics-cards,refurbished-gpu-graphics-cards&per_page=100&page={page}")
BIONIC_URL = "https://bionic.com.cy/en/products/c/graphics-card?page={page}&per_page=48"
CUSTOMPC_BASE = "https://www.custompc.com.cy/"
CUSTOMPC_GPU_PATHS = [f"components/graphics-cards/{brand}-graphics-cards/" for brand in ("nvidia", "amd", "intel")]
COMPUTERG_URL = "https://www.computerg.shop/shop/category/graphicscards-gpus-118"
# the same shops' complete desktop PCs (config "pcs"), read with the same parsers
ARMENIUS_PC_API = ("https://armenius.com.cy/wp-json/wc/store/v1/products"   # 50 a page: 100 PCs time out
                   "?category=desktop-pc,custom-pc,refurbished-desktop-pc&per_page=50&page={page}")
BIONIC_PC_URL = "https://bionic.com.cy/en/products/c/computers?page={page}&per_page=48"
CUSTOMPC_PC_PATHS = ["pc-systems/gaming-series/", "pc-systems/creator-series/", "pc-systems/exclusive-series/",
                     "pc-systems/next-day-desktops/"]
COMPUTERG_PC_URL = "https://www.computerg.shop/shop/category/pcs-workstations-110"


def _shop_item(site, sid, title, price, url, in_stock, notes):
    return {"site": site, "id": sid, "title": title, "price": price, "url": url, "in_stock": in_stock,
            "extra_notes": notes, "when": "", "location": "", "seller": ""}


def shop_armenius(api=ARMENIUS_API, per_page=100):
    """The shop's own WooCommerce Store API (public JSON), 100 cards per request, 5 s apart."""
    out = []
    for page in range(1, 1000 // per_page + 1):   # at most 1,000 products
        if page > 1:
            time.sleep(5)
        raw = fetch(api.format(page=page), ua=SHOP_UA)
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            items = []
        if not isinstance(items, list):
            break
        for p in items:
            pr = p.get("prices") or {}
            try:
                scale = 10 ** int(pr.get("currency_minor_unit", 2))
                price = int(pr["price"]) / scale if pr.get("currency_code", "EUR") == "EUR" else None
                regular = int(pr.get("regular_price") or 0) / scale
            except (KeyError, TypeError, ValueError):
                price, regular = None, 0
            notes = []
            if any("refurb" in str(c.get("slug")) for c in p.get("categories") or []):
                notes.append("refurbished")
            if price and regular > price:
                notes.append(f"on sale, was {regular:.0f}")
            stock = (p.get("stock_availability") or {}).get("text")
            if stock:
                notes.append(html.unescape(stock))
            out.append(_shop_item("armenius", f"am:{p.get('id')}", html.unescape(p.get("name") or ""), price,
                                  p.get("permalink") or "", bool(p.get("is_in_stock") and p.get("is_purchasable")),
                                  notes))
        if len(items) < per_page:
            break
    return out


def shop_bionic(url=BIONIC_URL):
    """The product list the category page embeds as JSON (prices include VAT)."""
    out = []
    for page in range(1, 6):
        if page > 1:
            time.sleep(3)
        h = fetch(url.format(page=page), ua=SHOP_UA)
        m = re.search(r'<script type="application/json" class="js-react-on-rails-component" '
                      r'data-component-name="App"[^>]*>(.*?)</script>', h, re.S)
        try:
            data = json.loads(m.group(1)) if m else {}
        except ValueError:
            data = {}
        for p in data.get("productsList") or []:
            en = next((t for t in p.get("translations") or [] if t.get("locale") == "en"), {})
            price = p.get("total_price_value")
            notes = []
            if p.get("onSale"):
                notes.append("on sale")
            if p.get("clearance"):
                notes.append("clearance")
            if isinstance(p.get("totalStock"), int):
                notes.append(f"{p['totalStock']} in stock")
            slug = en.get("slug") or ""
            out.append(_shop_item("bionic", f"bi:{p.get('id')}", en.get("title") or "",
                                  float(price) if isinstance(price, (int, float)) else None,
                                  f"https://bionic.com.cy/en/products/{slug}" if slug else "",
                                  bool(p.get("inStock")), notes))
        if page >= int((data.get("productParameters") or {}).get("totalPages") or 1):
            break
    return out


def shop_custompc(paths=None):
    """NVIDIA / AMD / Intel category pages, 5 s apart (the parent category also holds accessories)."""
    out, first = [], True
    for path in paths or CUSTOMPC_GPU_PATHS:
        page, last = 1, 1
        tail = re.escape(path.rstrip("/").rsplit("/", 1)[-1])
        while page <= min(last, 30):
            if not first:
                time.sleep(5)
            first = False
            h = fetch(CUSTOMPC_BASE + path + (f"page-{page}/" if page > 1 else ""), ua=SHOP_UA)
            last = max([last] + [int(n) for n in re.findall(tail + r'/page-(\d+)/', h)])
            for ch in h.split('class="ty-grid-list__item ')[1:]:
                pid = re.search(r'name="product_data\[(\d+)\]\[product_id\]"', ch)
                a = re.search(r'href="([^"]+)" class="product-title" title="([^"]*)"', ch)
                if not pid or not a:
                    continue
                # "1,388<sup>60</sup>" = 1388.60 incl. VAT
                pr = re.search(r'id="sec_discounted_price_\d+" class="ty-price-num">([^<]*)(?:<sup>(\d+)</sup>)?', ch)
                whole = re.sub(r"\D", "", pr.group(1)) if pr else ""
                price = float(whole + "." + (pr.group(2) or "0")) if whole else None
                stock = re.search(r'class="et-grid-stock (et-[a-z-]+)"', ch)
                qty = re.search(r'<span>(\d+)&nbsp;item', ch)
                out.append(_shop_item("custompc", f"cp:{pid.group(1)}", html.unescape(a.group(2)), price, a.group(1),
                                      bool(stock and stock.group(1) == "et-in-stock"),
                                      [f"{qty.group(1)} in stock"] if qty else []))
            page += 1
    return out


def shop_computerg(url=COMPUTERG_URL, pages=40):
    """Category pages, newest first, 10 s apart. Most cards ship from a supplier in some days."""
    out = []
    for page in range(1, pages + 1):
        if page > 1:
            time.sleep(10)
        h = fetch(url + (f"/page/{page}" if page > 1 else ""), ua=SHOP_UA)
        for ch in h.split('<div class="sf-card ')[1:]:
            a = re.search(r'class="sf-card-name" href="(/shop/[^"]*?-(\d+))">([^<]*)</a>', ch)
            if not a:
                continue
            pr = re.search(r'class="sf-price-now">(.*?)</span>', ch, re.S)
            digits = re.sub(r"[^\d.]", "", html.unescape(pr.group(1))) if pr else ""   # "1 181.99 €"
            try:
                price = float(digits) if digits else None
            except ValueError:
                price = None
            notes = []
            for rx in (r'class="sf-card-grade[^"]*">(.*?)</span>', r'class="sf-card-ship[^"]*">(.*?)</span>'):
                m = re.search(rx, ch, re.S)
                if m:
                    notes.append(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip().capitalize())
            out.append(_shop_item("computerg", f"cg:{a.group(2)}", html.unescape(a.group(3)).strip(), price,
                                  "https://www.computerg.shop" + a.group(1), "sf-addform" in ch, notes))
        shown = re.search(r'<b>\d+</b>\s*(?:[–-]|&ndash;)\s*(\d+)\s*of\s*<b>(\d+)</b>', h)
        if not shown or int(shown.group(1)) >= int(shown.group(2)):
            break
    return out


SHOPS = {"armenius": shop_armenius, "bionic": shop_bionic, "custompc": shop_custompc, "computerg": shop_computerg}
SHOP_PCS = {"armenius": lambda: shop_armenius(ARMENIUS_PC_API, 50), "bionic": lambda: shop_bionic(BIONIC_PC_URL),
            "custompc": lambda: shop_custompc(CUSTOMPC_PC_PATHS),
            "computerg": lambda: shop_computerg(COMPUTERG_PC_URL, pages=15)}


def _shop_job(name, with_pcs):
    """One shop, one thread: its graphics cards, then (when due) its complete PCs, at the shop's own pace."""
    items = SHOPS[name]()
    if with_pcs:
        time.sleep(10)
        try:
            pcs = SHOP_PCS[name]()
        except Exception as e:  # noqa: BLE001
            log(f"SHOP PCS FAILED {name}: {e!r}")
            pcs = []
        for p in pcs:
            p["kind"] = "pc"
        items = items + pcs
    return items


def shops_due(cfg, force=False):
    """True when at least one shop is switched on and every_minutes have passed since the last read."""
    scfg = cfg.get("shops") or {}
    if not scfg.get("enabled", True) or not any(scfg.get(n, True) for n in SHOPS):
        return False
    if force:
        return True
    last = (load_json(STATE_PATH, {}) or {}).get("_meta", {}).get("shops_last", 0)
    return time.time() - float(last or 0) >= 60 * float(scfg.get("every_minutes", 30)) - 30


def start_shops(cfg, with_pcs=False):
    """Read every enabled shop in its own thread (each keeps its own pace), while the rest of the scan runs."""
    scfg = cfg.get("shops") or {}
    names = [n for n in SHOPS if scfg.get(n, True)]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(names))
    jobs = {n: pool.submit(_shop_job, n, with_pcs) for n in names}
    pool.shutdown(wait=False)
    return jobs


def collect_shops(jobs, cfg):
    """-> (listings you can order now, {shop: cards read in total}, {shop: complete PCs read})"""
    scfg = cfg.get("shops") or {}
    factor = float(scfg.get("ceiling_factor", 1.0) or 1.0)
    out, counts, pc_counts, seen = [], {}, {}, set()
    for name, job in jobs.items():
        try:
            items = job.result(timeout=1500)
        except Exception as e:  # noqa: BLE001
            log(f"SHOP FAILED {name}: {e!r}")
            items = []
        pc_counts[name] = sum(1 for lst in items if lst.get("kind") == "pc")
        counts[name] = len(items) - pc_counts[name]
        for lst in items:
            if not lst.pop("in_stock") or lst["id"] in seen:
                continue
            seen.add(lst["id"])
            lst["ceiling_factor"] = factor
            # a "card" at a shop under scam_ratio x its used value is an accessory or another product
            lst["skip_if_suspiciously_cheap"] = True
            out.append(lst)
    return out, counts, pc_counts


def business_drop(lst, gpu, verdict, prev, cfg):
    """Second buyer: a VAT-registered business that reclaims the VAT on shop invoices. Alert when a shop
    cuts a recognised card's price by min_drop_pct or more from the first price the bot saw, and the price
    without VAT fits the business budget. Returns an alert tuple for send_alerts or None."""
    bcfg = cfg.get("business") or {}
    if not bcfg.get("enabled") or not gpu or verdict == "skip" or not lst.get("price") or not prev:
        return None
    ref = prev.get("first_price") or prev.get("price")
    if not ref:
        return None
    drop = 1 - lst["price"] / ref
    vat = float(bcfg.get("vat_pct", 19))
    net = lst["price"] / (1 + vat / 100)
    if drop * 100 < float(bcfg.get("min_drop_pct", 10)) or net > float(bcfg.get("budget_eur", 600)):
        return None
    if prev.get("biz_alerted") and lst["price"] > prev["biz_alerted"] * 0.97:
        return None   # already alerted for this cut and it has not dropped meaningfully further
    who = bcfg.get("name") or "the business"
    head = (f"[BUSINESS PRICE DROP] {gpu['name']} - EUR {net:.0f} without VAT ({lst['price']:.0f} with VAT), "
            f"down {drop * 100:.0f}% from {ref:.0f} on {lst['site']}")
    notes = [f"for {who}: invoice in the business name so it can reclaim the {vat:g}% VAT"] + \
        list(lst.get("extra_notes") or [])
    text = "\n".join([head, lst["title"], "Notes: " + "; ".join(notes), lst["url"]])
    return text, lst, dict(gpu, name="Business: " + gpu["name"])


# ----------------------------------------------------------------------- ready PCs
# Complete desktop PCs (config "pcs"). The parts an advert names (graphics card, processor,
# RAM) are priced at what they cost used when bought one by one, plus a flat amount for the
# board, PSU, case and SSD. The PC is a deal when it is clearly cheaper than that sum.
BAZARAKI_PC_URL = "https://www.bazaraki.com/computers-and-games/desktops-and-pcs/?ordering=newest&page={page}"
VENDORA_PC_URL = "https://vendora.cy/browse/2vdx1l/desktops.html?sort=recent&page={page}"

PC_WORDS = re.compile(r"\bpcs?\b|desktop|\btower\b|computer|υπολογιστ|workstation|gaming (rig|setup|build|system)", re.I)
# a bare model number only counts as a graphics card right after one of these words:
# "Dell 3060 micro pc" is an OptiPlex and "1920 x 1080" a resolution
PC_GPU_PREFIX = re.compile(r"(?:rtx|gtx|geforce|nvidia|rx|radeon|arc|gpu|vga|graphics)\s*$")
PC_INTEL = re.compile(r"\bi([3579])\s*(\d{4,5})([a-z]{0,2})\b")
PC_ULTRA = re.compile(r"\bultra\s*([3579])\s*(\d{3})([a-z]{0,2})\b")
PC_RYZEN = re.compile(r"\b(?:ryzen\s*([3579])|r([3579]))\s*(?:pro\s*)?(\d{4})\s*(x3d|xt|x|ge|g|f|u|hs|hx|h)?\b")
PC_RYZEN_BARE = re.compile(r"(?<!rx )\b([1-9]\d{3})(x3d|x)\b")
PC_MOBILE_SUFFIX = {"h", "hx", "hk", "hs", "hq", "u", "y", "p", "m", "mq", "qm", "v"}
PC_RAM_SIZES = {4, 6, 8, 12, 16, 20, 24, 32, 48, 64, 96}
PC_NOT_RAM = r"(?!\s*(?:ssd|hdd|nvme|m\.?2|sata|storage|emmc|usb|vram|gddr|σκληρ|δ[ίι]σκ))"
PC_REST_LABEL = "board, PSU, case, SSD"


def pcs_due(cfg, key="pcs_last", every="every_minutes", default=10, force=False):
    """True when the ready-PC search is switched on and its interval has passed."""
    pcfg = cfg.get("pcs") or {}
    if not pcfg.get("enabled"):
        return False
    if force:
        return True
    last = (load_json(STATE_PATH, {}) or {}).get("_meta", {}).get(key, 0)
    return time.time() - float(last or 0) >= 60 * float(pcfg.get(every, default)) - 30


def scrape_pcs(cfg):
    """Newest adverts of the desktop-PC sections of bazaraki and vendora."""
    pcfg = cfg.get("pcs") or {}
    out = scrape_bazaraki_urls([BAZARAKI_PC_URL.format(page=p)
                                for p in range(1, int(pcfg.get("bazaraki_pages", 2)) + 1)])
    out += scrape_vendora_urls([VENDORA_PC_URL.format(page=p)
                                for p in range(1, int(pcfg.get("vendora_pages", 2)) + 1)])
    for lst in out:
        lst["kind"] = "pc"
    return out


def bazaraki_description(url):
    """The seller's own text of one bazaraki advert: the specs are often only written there."""
    h = fetch(url, retries=0)
    # the advert's own text sits right before its "askwire" field (other "description"s are page texts)
    marker = '\\"description\\":\\"'
    end = h.find('\\",\\"askwire\\"')
    start = h.rfind(marker, 0, end) if end > 0 else -1
    text = h[start + len(marker):end] if start >= 0 else ""
    for _ in range(2):   # the page carries it as JSON inside a JavaScript string
        try:
            text = json.loads('"' + text + '"')
        except ValueError:
            break
    if not text:
        m = re.search(r'<meta name="description" content="([^"]*)"', h)
        text = m.group(1) if m else ""
    text = re.sub(r"<[^>]+>|\\[nrt]", " ", html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()[:1500]


def pc_find_gpu(norm, cfg):
    """The graphics card an advert names -> (name, EUR, note or None, text without the card), or None."""
    pcfg = cfg["pcs"]
    found = []
    for g in cfg["gpus"] + pcfg.get("other_gpus", []):
        for m in g["_re"].finditer(norm):
            if not re.search(r"[a-z]", m.group(0)) and not PC_GPU_PREFIX.search(norm[:m.start()]):
                continue
            found.append((m.end() - m.start(), g, m))
    if not found:
        return None
    longest = max(f[0] for f in found)
    first = min(f[2].start() for f in found if f[0] == longest)
    cands = [f for f in found if f[0] == longest and f[2].start() == first]
    m = cands[0][2]
    after = re.match(r"\s*(\d{1,2})\s*gb?\b(?!\s*(?:of\s*)?(?:ddr|ram|memory|μν))", norm[m.end():])
    if after and int(after.group(1)) > max(c[1]["vram_gb"] for c in cands):
        after = None   # more than this card can have: that number is the PC's RAM
    stated = int(after.group(1)) if after else None

    def eur(g):
        return float(pcfg.get("gpu_eur", {}).get(g["name"], g.get("eur", g.get("max_eur", 0))))

    note = None
    if stated is not None and any(c[1]["vram_gb"] == stated for c in cands):
        gpu = next(c[1] for c in cands if c[1]["vram_gb"] == stated)
    else:
        gpu = min((c[1] for c in cands), key=eur)   # the cheaper variant when the advert does not say
        if len(cands) > 1 or gpu.get("ambiguous_vram"):
            if gpu.get("ambiguous_vram"):   # the big variant is the only one known: do not count on it
                return (gpu["name"] + "?", 0.0, f"VRAM not stated - could be the small {gpu['name'].rsplit(' ', 1)[0]}",
                        norm[:m.start()] + " " + norm[m.end():])
            note = f"VRAM not stated - {gpu['name']} assumed"
    end = m.end() + (after.end() if after else 0)
    return gpu["name"], eur(gpu), note, norm[:m.start()] + " " + norm[end:]


def pc_find_cpu(norm, cfg):
    """The processor an advert names -> dict(label, key, eur, era, ram, mobile), or None.
    key = family used for pricing: i5-12 (Core i5, 12th gen), r7-5 (Ryzen 7 5000), x3d-7, u7-2 (Core Ultra 7 200)."""
    prices = cfg["pcs"].get("cpu_eur", {})
    m = PC_ULTRA.search(norm)
    if m:
        tier, num, suf = m.group(1), m.group(2), m.group(3)
        mobile = num[0] != "2" or suf in PC_MOBILE_SUFFIX
        key = f"u{tier}-{num[0]}"
        return {"label": f"Core Ultra {tier} {num}{suf.upper()}", "key": key, "eur": float(prices.get(key, 0)),
                "era": "top", "ram": "ddr5", "mobile": mobile}
    m = PC_INTEL.search(norm)
    if m:
        tier, num, suf = m.group(1), int(m.group(2)), m.group(3)
        gen = num // 1000
        mobile = suf in PC_MOBILE_SUFFIX
        # 4-digit numbers starting with 1 are laptop chips (i5-1235U); without a suffix it is no CPU at all
        if 2 <= gen <= 14 or (gen == 1 and mobile):
            key = f"i{tier}-{gen}"
            era, ram = ("old", "ddr3") if gen <= 5 else ("old", "ddr4") if gen <= 7 else \
                ("mid", "ddr4") if gen <= 11 else ("new", "ddr4")
            return {"label": f"i{tier}-{num}{suf.upper()}", "key": key, "eur": float(prices.get(key, 0)),
                    "era": era, "ram": ram, "mobile": mobile}
    m = PC_RYZEN.search(norm)
    bare = None if m else PC_RYZEN_BARE.search(norm)
    if m or bare:
        if m:
            tier, num, suf = m.group(1) or m.group(2), int(m.group(3)), m.group(4) or ""
        else:
            num, suf = int(bare.group(1)), bare.group(2)
            tier = "9" if num % 1000 >= 900 else "7" if num % 1000 >= 700 else "5" if num % 1000 >= 500 else "3"
        series = num // 1000
        mobile = suf in PC_MOBILE_SUFFIX or series == 6
        key = f"x3d-{series}" if suf == "x3d" else f"r{tier}-{series}"
        era, ram = ("mid", "ddr4") if series <= 5 else ("top", "ddr5")
        return {"label": f"Ryzen {tier} {num}{suf.upper()}", "key": key, "eur": float(prices.get(key, 0)),
                "era": era, "ram": ram, "mobile": mobile}
    return None


def pc_find_ram(text):
    """RAM of a PC advert (text = normalised, graphics card already cut out) -> (GB or None, 'ddr4'.. or None, guessed)."""
    typ = re.search(r"\bddr\s*([345])\b", text)
    typ = "ddr" + typ.group(1) if typ else None
    kit = re.search(r"\b([1248])\s*x\s*(\d{1,2})\s*gb?\b" + PC_NOT_RAM, text)
    if kit and int(kit.group(1)) * int(kit.group(2)) in PC_RAM_SIZES | {128}:
        return int(kit.group(1)) * int(kit.group(2)), typ, False
    m = re.search(r"\b(\d{1,3})\s*gb?\s*(?:of\s*)?(?:ddr\s*\d|ram|memory|μν[ήη]μη)", text) or \
        re.search(r"\b(?:ram|memory|μν[ήη]μη|ddr\s*\d)\s*:?\s*(\d{1,3})\s*gb?\b", text)
    if m and int(m.group(1)) in PC_RAM_SIZES | {128, 192, 256}:
        return int(m.group(1)), typ, False
    loose = {int(n) for n in re.findall(r"\b(\d{1,3})\s*gb\b" + PC_NOT_RAM, text)} & PC_RAM_SIZES
    if len(loose) == 1:
        return loose.pop(), typ, True
    return None, typ, False


def pc_parts(norm, cfg):
    """What one piece of advert text says about the PC: {'gpu': ..., 'cpu': ..., 'ram': ...} (missing keys = not said)."""
    parts = {}
    rest = norm
    gpu = pc_find_gpu(norm, cfg)
    if gpu:
        parts["gpu"] = gpu[:3]
        rest = gpu[3]
    cpu = pc_find_cpu(norm, cfg)
    if cpu:
        parts["cpu"] = cpu
    gb, typ, guessed = pc_find_ram(rest)
    if gb:
        parts["ram"] = (gb, typ, guessed)
    elif typ:
        parts["ram_type"] = typ
    return parts


def pc_read(listing, cfg):
    """Parts of a PC advert: the title first, the seller's description only for what the title leaves open."""
    parts = pc_parts(normalise_title(listing["title"]), cfg)
    if listing.get("desc"):
        extra = pc_parts(normalise_title(listing["desc"]), cfg)
        added = [k for k in ("gpu", "cpu", "ram", "ram_type") if k not in parts and k in extra]
        for k in added:
            parts[k] = extra[k]
        if added:
            parts["from_desc"] = True
    return parts


def pc_in_card_feed(listing, cfg):
    """A whole PC among the marketplace graphics-card results: says PC/desktop AND names a processor or RAM.
    (A card advert that merely mentions "PC" must not be valued as a computer; eBay and shop feeds hold cards only.)"""
    if listing["site"] not in ("bazaraki", "vendora") or not PC_WORDS.search(listing["title"]):
        return False
    parts = pc_parts(normalise_title(listing["title"]), cfg)
    return "cpu" in parts or "ram" in parts


def pc_wants_details(listing, cfg):
    """A bazaraki PC advert worth one extra request: not excluded, not cheap junk, title leaves parts open."""
    pcfg = cfg["pcs"]
    if any(rx.search(listing["title"]) for rx in pcfg["_exclude"]):
        return False
    if (listing.get("price") or 0) < float(pcfg.get("details_min_price", 150)):
        return False
    parts = pc_parts(normalise_title(listing["title"]), cfg)
    return not all(k in parts for k in ("gpu", "cpu", "ram"))


def classify_pc(listing, cfg):
    """A complete PC -> (name for the alert, verdict, notes). verdict in {'deal','above','skip'}.
    Sets listing['ceiling'] = the most this PC may cost to count as a deal."""
    pcfg = cfg["pcs"]
    for rx in pcfg["_exclude"]:
        if rx.search(listing["title"]):
            return None, "skip", [f"not a desktop PC: /{rx.pattern}/"]
    parts = pc_read(listing, cfg)
    cpu, gpu, ram = parts.get("cpu"), parts.get("gpu"), parts.get("ram")
    if cpu and cpu["mobile"]:
        return None, "skip", ["laptop or mini-PC processor"]
    price = listing["price"]
    if price is None or price <= 0:
        return None, "skip", ["no price"]

    lines, notes, value = [], [], 0.0
    gpu_eur = gpu[1] if gpu else 0.0
    if gpu:
        value += gpu_eur
        lines.append(f"{gpu[0]} {gpu_eur:.0f}")
        if gpu[2]:
            notes.append(gpu[2])
    cpu_eur = cpu["eur"] if cpu else 0.0
    if cpu:
        value += cpu_eur
        lines.append(f"{cpu['label']} {cpu_eur:.0f}")
    ram_gb = ram[0] if ram else 0
    rates = pcfg.get("ram_eur_per_gb", {})
    if ram:
        typ = ram[1] or parts.get("ram_type") or (cpu["ram"] if cpu else None)
        rate = float(rates.get(typ, min(rates.values()) if rates else 0))
        value += ram_gb * rate
        lines.append(f"{ram_gb}GB {typ.upper() if typ else 'RAM'} {ram_gb * rate:.0f}")
        if not ram[1] and not parts.get("ram_type"):
            if cpu and cpu["era"] == "new" and "ddr5" in rates:
                more = ram_gb * (float(rates["ddr5"]) - rate)
                notes.append(f"RAM type not stated - DDR4 assumed; with DDR5 the parts are worth {more:.0f} more")
            elif not cpu:
                notes.append("RAM type not stated - priced as the cheapest type")
        if ram[2]:
            notes.append(f"RAM size read from '{ram_gb}GB' without the word RAM - check it")
    rest_by = pcfg.get("rest_eur", {})
    rest = float(rest_by.get(cpu["era"] if cpu else "unknown", 0))
    if pcfg.get("_oem") and pcfg["_oem"].search(listing["title"]):
        # brand-name office and gaming boxes: own board, PSU and case formats, little use or resale value
        rest = min(rest, float(rest_by.get("oem", rest)))
    value += rest
    lines.append(f"{PC_REST_LABEL} {rest:.0f}")
    if not cpu:
        notes.append("processor not recognised - not counted")
    if not gpu:
        notes.append("no graphics card named - not counted")
    if parts.get("from_desc"):
        notes.append("some parts were read from the seller's description, not the title")

    name = "PC " + " + ".join(x for x in [gpu[0] if gpu else None, cpu["label"] if cpu else None] if x)
    pseudo = {"name": name.strip()}
    listing["pc_value"] = value
    listing["pc_parts"] = " + ".join(lines)
    # worth an alert: a card from the 12GB+ watch list, or a strong base to put your own card into
    watched_card = bool(gpu) and gpu_eur > 0 and any(g["name"] == gpu[0] for g in cfg["gpus"])
    strong_base = cpu_eur >= float(pcfg.get("min_cpu_eur", 90)) and ram_gb >= int(pcfg.get("min_ram_gb", 16))
    if not watched_card and not strong_base:
        return pseudo, "skip", ["no 12GB+ graphics card and no strong processor + RAM recognised"]

    ceiling = value * float(pcfg.get("max_price_ratio", 0.85))
    if pcfg.get("budget_eur"):
        ceiling = min(ceiling, float(pcfg["budget_eur"]))
    listing["ceiling"] = ceiling
    if price > ceiling:
        return pseudo, "above", [f"{price:.0f} > ceiling {ceiling:.0f} (parts {value:.0f})"]
    diff = round((price / value - 1) * 100)   # max_price_ratio above 1 also lets through PCs a little over their parts
    head = [f"the parts bought used one by one cost about {value:.0f}: {listing['pc_parts']}",
            f"{-diff}% cheaper as a ready PC" if diff < 0 else
            "costs the same as its parts, but ready built" if diff == 0 else
            f"{diff}% dearer than its parts, but ready built"]
    if price < value * cfg.get("scam_ratio", 0.4):
        if listing.get("skip_if_suspiciously_cheap"):
            return pseudo, "skip", [f"{price:.0f} is under {cfg.get('scam_ratio', 0.4):g} x the parts ({value:.0f}) - "
                                    "not a complete PC"]
        head.append("SUSPICIOUSLY CHEAP - likely scam, never pay in advance")
    elif price <= ceiling * cfg.get("great_deal_ratio", 0.8):
        head.append("GREAT DEAL")
    return pseudo, "deal", head + notes


# ------------------------------------------------------------------------ matching
def compile_config(cfg):
    cfg["_exclude"] = [re.compile(p, re.I) for p in cfg.get("exclude_title_patterns", [])]
    for g in cfg["gpus"]:
        g["_re"] = re.compile(g["pattern"], re.I)
    pcfg = cfg.get("pcs") or {}
    pcfg["_exclude"] = [re.compile(p, re.I) for p in pcfg.get("exclude_title_patterns", [])]
    pcfg["_oem"] = re.compile(pcfg["oem_title_pattern"], re.I) if pcfg.get("oem_title_pattern") else None
    for g in pcfg.get("other_gpus", []):
        g["_re"] = re.compile(g["pattern"], re.I)
    for w in cfg.get("keyword_watches", []):
        w["_re"] = re.compile(w["pattern"], re.I)
        w["_exclude"] = [re.compile(p, re.I) for p in w.get("exclude", [])]
    return cfg


def classify_watch(listing, watch, cfg):
    """Keyword watch: alert on any title matching the pattern, optional max_eur."""
    title = listing["title"]
    for rx in watch["_exclude"]:
        if rx.search(title):
            return None, "skip", [f"excluded by /{rx.pattern}/"]
    if not watch["_re"].search(normalise_title(title)):
        return None, "skip", ["pattern not in title"]
    price = listing["price"]
    max_eur = watch.get("max_eur")
    listing["ceiling"] = float(max_eur) if max_eur else None
    notes = []
    if price is None or price <= 0:
        notes.append("no price shown - ask the seller")
    elif max_eur and price > float(max_eur):
        return watch, "above", [f"{price:.0f} > ceiling {max_eur}"]
    return watch, "deal", notes


def classify(listing, cfg):
    """Return (gpu, verdict, notes).  verdict in {'deal','above','skip'}"""
    title = listing["title"]
    notes = []
    for rx in cfg["_exclude"]:
        if rx.search(title):
            return None, "skip", [f"excluded by /{rx.pattern}/"]
    norm = normalise_title(title)
    best, best_len = None, 0
    for g in cfg["gpus"]:
        m = g["_re"].search(norm)
        if m and (m.end() - m.start()) > best_len:
            best, best_len = g, m.end() - m.start()
    if not best:
        return None, "skip", ["no known GPU in title"]

    brand = gpu_brand(best)
    if brand:
        others = [b for b, rx in BRAND_WORDS.items() if b != brand and rx.search(norm)]
        if others and not BRAND_WORDS[brand].search(norm):
            return best, "skip", [f"title is for an {others[0]} card, not a {best['name']}"]
    vrams = vram_in_title(norm)
    if OLD_CARD_MEMORY.search(norm) and not vrams:
        return best, "skip", ["memory given in MB - an old card with the same number"]
    if not best.get("ambiguous_vram") and vrams and max(vrams) < best["vram_gb"]:
        return best, "skip", [f"title says {max(vrams)}GB but a {best['name']} has {best['vram_gb']}GB "
                              "- another card or a part"]
    if best.get("ambiguous_vram"):
        if any(v >= 12 for v in vrams):
            pass
        elif vrams:
            return best, "skip", [f"smaller-VRAM variant ({max(vrams)}GB)"]
        elif best.get("require_vram"):
            return best, "skip", ["VRAM not stated and this model is usually the small variant"]
        else:
            notes.append(f"VRAM not stated - confirm it is the {best['vram_gb']}GB version")

    price = listing["price"]
    if price is None or price <= 0:
        return best, "skip", ["no price"]
    # ceiling_factor > 1 lets eBay (which has buyer protection) go a bit above the local ceilings
    worth = float(best["max_eur"]) * float(listing.get("ceiling_factor", 1.0))   # may be above the budget
    ceiling = min(float(cfg["budget_eur"]), worth)
    listing["ceiling"] = ceiling
    if price > ceiling:
        return best, "above", [f"{price:.0f} > ceiling {ceiling:.0f}"]
    # "too cheap" is measured against what the card is worth, not against the budget cap:
    # a 4090 for 266 is a tenth of its value, not 44% of a 600 budget
    if price < worth * cfg.get("scam_ratio", 0.4):
        if listing.get("skip_if_suspiciously_cheap"):
            # on eBay a "card" this cheap is a cooler, a box, a photo or a fake, not a deal
            return best, "skip", [f"{price:.0f} is under {cfg.get('scam_ratio', 0.4):g} x its used value "
                                  f"({worth:.0f}) - a part, box or fake"]
        notes.append("SUSPICIOUSLY CHEAP - likely scam, never pay in advance")
    elif price <= ceiling * cfg.get("great_deal_ratio", 0.8):
        notes.append("GREAT DEAL")
    return best, "deal", notes


# --------------------------------------------------------------------- notifying
def notify_toast(title, body, url=""):
    if os.name != "nt" or not os.path.exists(TOAST_PS1):
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", TOAST_PS1,
             "-Title", title, "-Body", body, "-Url", url],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        log(f"toast failed: {e}")


def telegram_creds(cfg):
    """Environment variables win (used on GitHub Actions), then config.local.json / config.json."""
    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id", ""))
    return token, chat


def notify_telegram(cfg, text):
    token, chat = telegram_creds(cfg)
    if not token or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        log(f"telegram failed: {e}")


def setup_telegram(token):
    """Discover the chat id for this bot token, store both in config.json, send a test."""
    token = (token or "").strip()
    if not token:
        token = input("Paste the bot token from @BotFather: ").strip()
    if not re.fullmatch(r"\d+:[\w-]+", token):
        sys.exit("That does not look like a Telegram bot token (expected 123456789:AbCdEf...).")
    raw = fetch(f"https://api.telegram.org/bot{token}/getUpdates", retries=0)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if not data.get("ok"):
        sys.exit("Telegram rejected the token (or no internet). Check it in @BotFather and retry.")
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
        if msg and "chat" in msg:
            chats[msg["chat"]["id"]] = msg["chat"]
    if not chats:
        sys.exit("No message found yet. In Telegram open your bot, press Start, send 'hi', then rerun this.")
    chat_id, chat = list(chats.items())[-1]
    who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
    local = load_json(LOCAL_CONFIG_PATH, {}) or {}
    local["telegram"] = {"bot_token": token, "chat_id": str(chat_id)}
    try:
        save_json(LOCAL_CONFIG_PATH, local)
    except OSError as e:
        sys.exit(f"could not write config.local.json: {e}")
    notify_telegram({"telegram": {"bot_token": token, "chat_id": chat_id}},
                    "GPU Alert Bot is connected. You will get a message here for every matching GPU listing.")
    print(f"Telegram configured for chat {chat_id} ({who}); saved to config.local.json. A test message was sent.")


def format_deal(listing, gpu, notes, kind):
    tag = "PRICE DROP" if kind == "drop" else "NEW"
    extra = " | ".join(x for x in [listing.get("when"), listing.get("location"), listing.get("seller")] if x)
    price_txt = f"EUR {listing['price']:.0f}" if listing.get("price") else "no price"
    ceil_txt = f" (ceiling {listing['ceiling']:.0f})" if listing.get("ceiling") else ""
    head = f"[{tag}] {gpu['name']} - {price_txt}{ceil_txt} on {listing['site']}"
    lines = [head, listing["title"]]
    if extra:
        lines.append(extra)
    if notes:
        lines.append("Notes: " + "; ".join(notes))
    lines.append(listing["url"])
    return "\n".join(lines)


def send_alerts(deals, cfg, dry_run=False):
    if not deals:
        return
    for text, listing, gpu in deals:
        if dry_run:
            print("WOULD ALERT: " + text.replace("\n", " | "))
        else:
            log(text.replace("\n", " | "), ALERT_LOG)
    if dry_run:
        return
    toast_on = cfg.get("windows_toast", True)
    if toast_on and len(deals) > 3:
        notify_toast("GPU deals", f"{len(deals)} matching GPU listings found. Details in alerts.log",
                     deals[0][1]["url"])
    for text, listing, gpu in deals[:12]:
        if toast_on and len(deals) <= 3:
            price_txt = f"EUR {listing['price']:.0f}" if listing.get("price") else "no price"
            notify_toast(f"{gpu['name']} - {price_txt} ({listing['site']})",
                         listing["title"][:120], listing["url"])
        safe = html.escape(text.rsplit("\n", 1)[0])
        notify_telegram(cfg, f"{safe}\n<a href=\"{listing['url']}\">open listing</a>")
    if len(deals) > 12:
        notify_telegram(cfg, f"...and {len(deals) - 12} more, see alerts.log")


# -------------------------------------------------------------------------- state
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- scan
def scan(cfg, dry_run=False, list_all=False):
    src = cfg.get("sources", {})
    force = dry_run or list_all
    pcs_on = bool((cfg.get("pcs") or {}).get("enabled"))
    # the shops are read in background threads while the marketplaces are scraped
    shop_pcs = pcs_on and cfg["pcs"].get("shops", True) and \
        pcs_due(cfg, "pcs_shops_last", "shops_every_minutes", 180, force)
    shop_jobs = start_shops(cfg, with_pcs=shop_pcs) if shops_due(cfg, force=force) else None
    listings = scrape_bazaraki(src.get("bazaraki_pages", 2)) + \
        scrape_vendora(src.get("vendora_pages", 2), src.get("vendora_searches", ["rtx", "radeon"]))
    # complete desktop PCs have their own sections on both marketplaces
    ran_pcs = pcs_due(cfg, force=force)
    pc_listings = scrape_pcs(cfg) if ran_pcs else []
    pc_ids = {l["id"] for l in pc_listings}
    listings = [l for l in listings if l["id"] not in pc_ids] + pc_listings
    # keyword watches: site-wide searches, classified by their own pattern
    gpu_ids = {l["id"] for l in listings}
    watched = []
    for w in cfg.get("keyword_watches", []):
        terms = [urllib.parse.quote(q) for q in w.get("searches", [])]
        found = scrape_bazaraki_urls([BAZARAKI_SEARCH_URL.format(q=q) for q in terms]) + \
            scrape_vendora_urls([VENDORA_ANY_SEARCH_URL.format(q=q) for q in terms])
        seen_here = set()
        for l in found:
            if l["id"] in gpu_ids or l["id"] in seen_here:
                continue
            seen_here.add(l["id"])
            watched.append((l, w))
    # eBay through its official Browse API (only when keys are configured)
    ran_ebay, ebay_err, ne = False, None, 0
    if ebay_due(cfg, force=dry_run or list_all):
        ran_ebay = True
        found, ebay_err = ebay_scan(cfg)
        for lst, w in found:
            if w is None:
                listings.append(lst)
                ne += 1
            else:
                watched.append((lst, w))
    shop_counts, shop_pc_counts, ns = {}, {}, 0
    if shop_jobs is not None:
        found, shop_counts, shop_pc_counts = collect_shops(shop_jobs, cfg)
        listings += found
        ns = len(found)

    state = load_json(STATE_PATH, {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    deals = []
    rows = []
    items = [(l, None) for l in listings] + watched
    details_left = int((cfg.get("pcs") or {}).get("detail_fetches_per_scan", 6))
    for lst, watch in items:
        prev = state.get(lst["id"])
        is_pc = False
        if watch is None:
            if lst.get("kind") == "pc":
                is_pc = True
            else:
                gpu, verdict, notes = classify(lst, cfg)
                # a whole PC in the graphics-card feeds (skipped there as "not a card")
                is_pc = pcs_on and verdict == "skip" and gpu is None and pc_in_card_feed(lst, cfg)
            if is_pc:
                if prev and "desc" in prev:
                    lst["desc"] = prev["desc"]
                elif lst["site"] == "bazaraki" and details_left > 0 and pc_wants_details(lst, cfg):
                    # the specs are often only in the seller's text: read it once per advert
                    details_left -= 1
                    lst["desc"] = bazaraki_description(lst["url"])
                    time.sleep(1.5)
                gpu, verdict, notes = classify_pc(lst, cfg)
        else:
            gpu, verdict, notes = classify_watch(lst, watch, cfg)
            if verdict == "skip":
                continue   # unrelated search hit, do not even remember it
        if verdict == "deal" and lst.get("extra_notes"):
            notes = notes + lst["extra_notes"]
        if list_all and gpu and verdict != "skip":
            rows.append((lst, gpu, verdict, notes))
        if verdict == "deal":
            kind = None
            if prev is None:
                kind = "new"
            elif not prev.get("alerted") and prev.get("price") is not None and lst["price"] < prev["price"]:
                kind = "drop"
            elif not prev.get("alerted") and is_pc:
                # seen before, but a deal only now: first read as a PC, the seller's description
                # was read, or the pcs settings were loosened
                kind = "new"
            if kind:
                deals.append((format_deal(lst, gpu, notes, kind), lst, gpu))
        is_shop = watch is None and lst["site"] in SHOPS
        biz = business_drop(lst, gpu, verdict, prev, cfg) if is_shop and not is_pc else None
        if biz:
            deals.append(biz)
        if not dry_run:
            entry = prev or {"first_seen": now, "title": lst["title"][:100], "site": lst["site"]}
            if is_shop and entry.get("first_price") is None:
                entry["first_price"] = entry.get("price") or lst["price"]   # reference for price-drop alerts
            entry["price"] = lst["price"]
            entry["last_seen"] = now
            if is_pc:
                entry["pc"] = True
                if "desc" in lst:
                    entry["desc"] = lst["desc"]
            if verdict == "deal":
                entry["alerted"] = True
            if biz:
                entry["biz_alerted"] = lst["price"]
            state[lst["id"]] = entry
    if ran_ebay and not dry_run:
        meta = state.setdefault("_meta", {})
        meta["ebay_last"] = time.time()
        if ebay_err and meta.get("ebay_warned", "") < (dt.datetime.now() - dt.timedelta(hours=24)).isoformat():
            notify_telegram(cfg, f"GPU Alert Bot warning: the eBay search failed ({html.escape(ebay_err[:200])}). "
                                 "Check the keys with: python gpu_alert.py --setup-ebay")
            meta["ebay_warned"] = now
    if shop_jobs is not None and not dry_run:
        state.setdefault("_meta", {})["shops_last"] = time.time()
        if shop_pcs:
            state["_meta"]["pcs_shops_last"] = time.time()
    if ran_pcs and not dry_run:
        state.setdefault("_meta", {})["pcs_last"] = time.time()
    if not dry_run:
        cutoff = (dt.datetime.now() - dt.timedelta(days=90)).isoformat()
        state = {k: v for k, v in state.items() if v.get("last_seen", now) >= cutoff}
        save_json(STATE_PATH, state)

    npc = sum(1 for l in listings if l.get("kind") == "pc")
    nb = sum(1 for l in listings if l["site"] == "bazaraki" and l.get("kind") != "pc")
    nv = sum(1 for l in listings if l["site"] == "vendora" and l.get("kind") != "pc")
    ns -= sum(1 for l in listings if l.get("kind") == "pc" and l["site"] in SHOPS)
    ebay_txt = f", {ne} ebay" if ran_ebay else ""
    shop_txt = f", {ns} in stock at shops" if shop_jobs is not None else ""
    pc_txt = f" + {npc} ready PCs" if npc else ""
    log(f"scanned {len(listings) - npc} GPU listings ({nb} bazaraki, {nv} vendora{ebay_txt}{shop_txt}){pc_txt} + "
        f"{len(watched)} keyword-search results -> {len(deals)} new deal(s)")
    if shop_counts:
        log("shops (cards read): " + ", ".join(f"{k} {v}" for k, v in shop_counts.items()))
    if shop_pcs and any(shop_pc_counts.values()):
        log("shops (complete PCs read): " + ", ".join(f"{k} {v}" for k, v in shop_pc_counts.items()))
    empty = [s for s, n in [("bazaraki", nb), ("vendora", nv)] + list(shop_counts.items()) if n == 0]
    if empty and not dry_run:
        # a site returned nothing: blocked, down or changed layout. Warn on Telegram at most once a day.
        meta = state.setdefault("_meta", {})
        last = meta.get("empty_warned", "")
        if last < (dt.datetime.now() - dt.timedelta(hours=24)).isoformat():
            where = os.environ.get("GITHUB_ACTIONS") and "GitHub cloud" or "PC"
            notify_telegram(cfg, f"GPU Alert Bot warning ({where}): "
                                 f"{', '.join(empty)} returned 0 listings. "
                                 "The site may be blocking this runner or changed its page layout.")
            meta["empty_warned"] = now
            save_json(STATE_PATH, state)

    if list_all:
        rows.sort(key=lambda r: (r[0]["price"] or 0) / max(r[0].get("ceiling") or 1, 1))
        print(f"\n{'site':9} {'price':>7} {'ceiling':>8}  {'model':34} title")
        for lst, gpu, verdict, notes in rows:
            flag = "DEAL " if verdict == "deal" else "     "
            print(f"{lst['site']:9} {lst['price'] or 0:7.0f} {lst.get('ceiling') or 0:8.0f} {flag}{gpu['name'][:34]:34} "
                  f"{lst['title'][:70]}  {lst['url']}")
            if lst.get("pc_parts"):   # a ready PC: what its ceiling is made of
                print(f"{'':32}parts {lst['pc_value']:.0f} = {lst['pc_parts']}")
    send_alerts(deals, cfg, dry_run=dry_run)
    return deals


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="keep scanning every poll_minutes")
    ap.add_argument("--list", action="store_true", help="print all recognised GPU listings vs ceilings")
    ap.add_argument("--dry-run", action="store_true", help="do not save state or notify")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--setup-telegram", nargs="?", const="", metavar="BOT_TOKEN",
                    help="store the Telegram bot token, auto-detect your chat id, send a test")
    ap.add_argument("--setup-ebay", action="store_true",
                    help="store your eBay developer keys (official Browse API), check them, run a test search")
    args = ap.parse_args()

    if args.setup_telegram is not None:
        setup_telegram(args.setup_telegram)
        return

    raw = load_json(CONFIG_PATH, None)
    if not raw:
        sys.exit("config.json missing or invalid")
    raw.update(load_json(LOCAL_CONFIG_PATH, {}) or {})   # secrets / personal overrides
    cfg = compile_config(raw)

    if args.setup_ebay:
        setup_ebay(cfg)
        return

    if args.test_notify:
        notify_toast("GPU Alert Bot", "Test notification - toasts are working.",
                     "https://www.bazaraki.com/computers-and-games/hardware/gpu/")
        notify_telegram(cfg, "GPU Alert Bot: test message - Telegram is working.")
        print("test notifications sent (Telegram only if configured in config.json)")
        return

    if args.loop:
        while True:
            try:
                scan(cfg, dry_run=args.dry_run, list_all=args.list)
            except Exception as e:  # noqa: BLE001
                log(f"scan error: {e!r}")
            time.sleep(max(2, int(cfg.get("poll_minutes", 10))) * 60)
    else:
        scan(cfg, dry_run=args.dry_run, list_all=args.list)


if __name__ == "__main__":
    main()
