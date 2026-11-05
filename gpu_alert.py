#!/usr/bin/env python3
"""
GPU deal watcher for bazaraki.com and vendora.cy (Cyprus), plus eBay through
its official Browse API once you have stored developer keys (--setup-ebay).

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


def fetch(url, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,el;q=0.8"})
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
    """Return list of GB numbers mentioned like '12gb', '16 gb', '12g'."""
    return [int(n) for n in re.findall(r"\b(\d{1,2})\s*(?:gb|g)\b", norm)]


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
    if item.get("condition"):
        notes.append(str(item["condition"]))
    when = str(item.get("itemCreationDate") or "")[:16].replace("T", " ")
    who = seller.get("username", "")
    if who and score is not None:
        who += f" ({score} ratings, {seller.get('feedbackPercentage', '?')}%)"
    return {
        "site": EBAY_SITE_NAMES.get(marketplace, marketplace.lower()),
        "id": "eb:" + str(item.get("legacyItemId") or item.get("itemId")),
        "title": item.get("title", ""),
        "price": round(total, 2) if total is not None else None,
        "url": item.get("itemWebUrl", ""),
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


# ------------------------------------------------------------------------ matching
def compile_config(cfg):
    cfg["_exclude"] = [re.compile(p, re.I) for p in cfg.get("exclude_title_patterns", [])]
    for g in cfg["gpus"]:
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

    vrams = vram_in_title(norm)
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
    ceiling = min(float(cfg["budget_eur"]), float(best["max_eur"]) * float(listing.get("ceiling_factor", 1.0)))
    listing["ceiling"] = ceiling
    if price > ceiling:
        return best, "above", [f"{price:.0f} > ceiling {ceiling:.0f}"]
    if price < ceiling * cfg.get("scam_ratio", 0.4):
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
    listings = scrape_bazaraki(src.get("bazaraki_pages", 2)) + \
        scrape_vendora(src.get("vendora_pages", 2), src.get("vendora_searches", ["rtx", "radeon"]))
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

    state = load_json(STATE_PATH, {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    deals = []
    rows = []
    items = [(l, None) for l in listings] + watched
    for lst, watch in items:
        if watch is None:
            gpu, verdict, notes = classify(lst, cfg)
        else:
            gpu, verdict, notes = classify_watch(lst, watch, cfg)
            if verdict == "skip":
                continue   # unrelated search hit, do not even remember it
        if verdict == "deal" and lst.get("extra_notes"):
            notes = notes + lst["extra_notes"]
        if list_all and gpu and verdict != "skip":
            rows.append((lst, gpu, verdict, notes))
        prev = state.get(lst["id"])
        if verdict == "deal":
            kind = None
            if prev is None:
                kind = "new"
            elif not prev.get("alerted") and prev.get("price") is not None and lst["price"] < prev["price"]:
                kind = "drop"
            if kind:
                deals.append((format_deal(lst, gpu, notes, kind), lst, gpu))
        if not dry_run:
            entry = prev or {"first_seen": now, "title": lst["title"][:100], "site": lst["site"]}
            entry["price"] = lst["price"]
            entry["last_seen"] = now
            if verdict == "deal":
                entry["alerted"] = True
            state[lst["id"]] = entry
    if ran_ebay and not dry_run:
        meta = state.setdefault("_meta", {})
        meta["ebay_last"] = time.time()
        if ebay_err and meta.get("ebay_warned", "") < (dt.datetime.now() - dt.timedelta(hours=24)).isoformat():
            notify_telegram(cfg, f"GPU Alert Bot warning: the eBay search failed ({html.escape(ebay_err[:200])}). "
                                 "Check the keys with: python gpu_alert.py --setup-ebay")
            meta["ebay_warned"] = now
    if not dry_run:
        cutoff = (dt.datetime.now() - dt.timedelta(days=90)).isoformat()
        state = {k: v for k, v in state.items() if v.get("last_seen", now) >= cutoff}
        save_json(STATE_PATH, state)

    nb = sum(1 for l in listings if l["site"] == "bazaraki")
    nv = sum(1 for l in listings if l["site"] == "vendora")
    ebay_txt = f", {ne} ebay" if ran_ebay else ""
    log(f"scanned {len(listings)} GPU listings ({nb} bazaraki, {nv} vendora{ebay_txt}) + {len(watched)} keyword-search results "
        f"-> {len(deals)} new deal(s)")
    if (nb == 0 or nv == 0) and not dry_run:
        # a site returned nothing: blocked, down or changed layout. Warn on Telegram at most once a day.
        meta = state.setdefault("_meta", {})
        last = meta.get("empty_warned", "")
        if last < (dt.datetime.now() - dt.timedelta(hours=24)).isoformat():
            where = os.environ.get("GITHUB_ACTIONS") and "GitHub cloud" or "PC"
            notify_telegram(cfg, f"GPU Alert Bot warning ({where}): "
                                 f"{'bazaraki' if nb == 0 else 'vendora'} returned 0 listings. "
                                 "The site may be blocking this runner or changed its page layout.")
            meta["empty_warned"] = now
            save_json(STATE_PATH, state)

    if list_all:
        rows.sort(key=lambda r: (r[0]["price"] or 0) / max(r[0].get("ceiling") or 1, 1))
        print(f"\n{'site':9} {'price':>7} {'ceiling':>8}  {'model':22} title")
        for lst, gpu, verdict, notes in rows:
            flag = "DEAL " if verdict == "deal" else "     "
            print(f"{lst['site']:9} {lst['price'] or 0:7.0f} {lst.get('ceiling') or 0:8.0f} {flag}{gpu['name']:22} "
                  f"{lst['title'][:70]}  {lst['url']}")
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
