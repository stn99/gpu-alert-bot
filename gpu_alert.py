#!/usr/bin/env python3
"""
GPU deal watcher for bazaraki.com and vendora.cy (Cyprus).

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

Standard library only - no pip installs needed.
"""
import argparse
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
    out = []
    for page in range(1, pages + 1):
        h = fetch(BAZARAKI_GPU_URL.format(page=page))
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


# ------------------------------------------------------------------------ matching
def compile_config(cfg):
    cfg["_exclude"] = [re.compile(p, re.I) for p in cfg.get("exclude_title_patterns", [])]
    for g in cfg["gpus"]:
        g["_re"] = re.compile(g["pattern"], re.I)
    return cfg


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
    ceiling = min(float(cfg["budget_eur"]), float(best["max_eur"]))
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
    head = f"[{tag}] {gpu['name']} - EUR {listing['price']:.0f} (ceiling {listing['ceiling']:.0f}) on {listing['site']}"
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
            notify_toast(f"{gpu['name']} - EUR {listing['price']:.0f} ({listing['site']})",
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
    state = load_json(STATE_PATH, {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    deals = []
    rows = []
    for lst in listings:
        gpu, verdict, notes = classify(lst, cfg)
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
    if not dry_run:
        cutoff = (dt.datetime.now() - dt.timedelta(days=90)).isoformat()
        state = {k: v for k, v in state.items() if v.get("last_seen", now) >= cutoff}
        save_json(STATE_PATH, state)

    nb = sum(1 for l in listings if l["site"] == "bazaraki")
    nv = sum(1 for l in listings if l["site"] == "vendora")
    log(f"scanned {len(listings)} listings ({nb} bazaraki, {nv} vendora) -> {len(deals)} new deal(s)")
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
        rows.sort(key=lambda r: (r[0]["price"] or 0) / max(r[0].get("ceiling", 1), 1))
        print(f"\n{'site':9} {'price':>7} {'ceiling':>8}  {'model':22} title")
        for lst, gpu, verdict, notes in rows:
            flag = "DEAL " if verdict == "deal" else "     "
            print(f"{lst['site']:9} {lst['price'] or 0:7.0f} {lst.get('ceiling', 0):8.0f} {flag}{gpu['name']:22} "
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
    args = ap.parse_args()

    if args.setup_telegram is not None:
        setup_telegram(args.setup_telegram)
        return

    raw = load_json(CONFIG_PATH, None)
    if not raw:
        sys.exit("config.json missing or invalid")
    raw.update(load_json(LOCAL_CONFIG_PATH, {}) or {})   # secrets / personal overrides
    cfg = compile_config(raw)

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
