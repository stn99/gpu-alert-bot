# GPU Alert Bot (bazaraki.com + vendora.cy + eBay + Cypriot shops)

Watches the newest GPU listings on both Cypriot marketplaces and alerts you when a
card with **12 GB VRAM or more** shows up at or below what it is worth second-hand
(and never above your **600 EUR** budget). It also watches **complete desktop PCs**
and alerts when one is clearly cheaper than its parts bought separately (see
"Ready PCs" below).

## Files

| File | Purpose |
|---|---|
| `gpu_alert.py` | the bot (Python 3, standard library only) |
| `config.json` | budget, per-card price ceilings, exclusions, Telegram settings |
| `toast.ps1` | shows the Windows notification (click it to open the listing) |
| `install_task.cmd` | schedules a scan every 5 minutes via Task Scheduler |
| `uninstall_task.cmd` | removes that scheduled task |
| `seen.json` | listings already seen (so you get alerted once per ad) |
| `alerts.log` / `scan.log` | history of alerts / of scans and errors |

## Quick start

```bat
cd %USERPROFILE%\gpu-alert-bot
python gpu_alert.py --list          :: market snapshot: every recognised GPU vs. its ceiling
python gpu_alert.py --test-notify   :: check that the Windows toast appears
python gpu_alert.py                 :: one real scan (alerts on anything currently matching)
install_task.cmd                    :: then run automatically every 5 minutes
```

The first real scan alerts on everything that matches right now; after that only
new listings (or price drops) trigger alerts.

## Phone alerts via Telegram (optional, recommended)

1. In Telegram talk to **@BotFather**, send `/newbot`, copy the token.
2. Open your new bot in Telegram, press Start and send any message (e.g. "hi").
3. Run `python gpu_alert.py --setup-telegram <TOKEN>` - it finds your chat id,
   saves both to config.json and sends a test message to your phone.

## Tuning

* `budget_eur` - hard cap for every card.
* `gpus[].max_eur` - what that model is worth to you used. The shipped numbers are
  Nov-2026 estimates taken from eBay sold-price trackers minus roughly 10 percent;
  check them against `--list` output every few weeks because the used market moves.
  Keep the real value even when it is above `budget_eur`: the ceiling stays at the
  budget, and the value is what "suspiciously cheap" is measured against. The cards
  above 600 (3090, 4070 Ti Super and up, 5070 Ti and up, 7900 XTX, 9070 XT) are set
  about 10 percent under the cheapest real used offers on eBay delivered to Cyprus
  on 2026-11-12.
* `ambiguous_vram: true` marks models that also exist with less VRAM (3060 8/12 GB,
  3080 10/12 GB, 5060 Ti 8/16 GB, 9060 XT 8/16 GB, A770 8/16 GB). If the title
  names a smaller size the ad is skipped; if it names no size you get the alert
  with a "confirm VRAM" note. `require_vram: true` (RTX 2060) skips it unless
  "12GB" is written.
* `exclude_title_patterns` - regexes that drop whole PCs, laptops, fans,
  wanted-ads etc. Add your own.
* `great_deal_ratio` / `scam_ratio` - below 80 percent of the ceiling the alert is
  tagged GREAT DEAL; below 40 percent of the card's used value (`max_eur`, not capped
  by the budget) it is tagged as a likely scam. An RTX 4090 is worth about 2,700, so
  any 4090 inside a 600 budget is flagged.

## Notes

* Shops on bazaraki re-post the same stock daily under new ad IDs; those are
  mostly new cards above your ceilings, so they rarely alert.
* For graphics cards only titles are parsed. A seller who writes just "graphics card"
  will be missed. (For ready PCs the description is read too, see below.)
* Keep `poll_minutes` at 5 or more; the bot fetches about 20 pages per scan, plus 4
  for ready PCs every 10 minutes.

## Running in the cloud (PC can be off)

The repository ships with `.github/workflows/scan.yml`, which runs the same scan
on GitHub Actions every 5 minutes. Setup once:

```bat
gh auth login
gh repo create gpu-alert-bot --public --source . --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh workflow run "GPU scan"
```

* The Telegram token lives only in GitHub's encrypted secrets, never in the code.
* `seen.json` is carried between runs with the Actions cache.
* `keepalive.yml` makes an empty commit twice a month so GitHub does not switch
  the schedule off after 60 days of inactivity.
* GitHub's scheduler is often 5 to 15 minutes late at busy times.
* If a site blocks GitHub's servers the bot sends a Telegram warning (max once a
  day); then fall back to the local scheduled task.
* Run `uninstall_task.cmd` locally once the cloud version works, or you will get
  every alert twice.

Secrets for the local copy live in `config.local.json` (git-ignored); `--setup-telegram`
writes them there.

**Tested 2026-11-04: both sites answer GitHub's runners with HTTP 403 (bot
protection on datacenter IP ranges), so the cloud workflows are disabled. The bot
has to run from a home internet connection: this PC, or any always-on device at
home such as a Raspberry Pi or an old laptop.**

## Keyword watches (anything, not just GPUs)

`keyword_watches` in `config.json` runs site-wide keyword searches on both sites
and alerts on any title matching `pattern`, with an optional `max_eur` (null = any
price). The first one watches for a DF64 coffee grinder:

```json
{ "name": "DF64 coffee grinder",
  "searches": ["df64", "df 64", "turin grinder", "coffee grinder"],
  "pattern": "\bdf\s*64|\bturin\b|g\s*-?\s*iota",
  "exclude": ["\bwtb\b", "looking for"],
  "max_eur": null }
```

## eBay (official API, needs free developer keys)

eBay's User Agreement forbids scraping the website, so the bot only talks to
eBay through the official **Browse API**, which is free and allowed.

1. Join the eBay Developers Program at developer.ebay.com with your eBay account
   (approval can take a day or two).
2. Create an application and open its **Production** keyset.
3. In the "marketplace account deletion" step choose the exemption: the bot does
   not store any eBay user data. Until this is done the production keys stay disabled.
4. Run `python gpu_alert.py --setup-ebay` and paste the App ID (Client ID) and
   Cert ID (Client Secret). It checks them with eBay, saves them to the git-ignored
   `config.local.json` and runs a test search. From then on every scheduled scan
   includes eBay.

How eBay listings are judged (`ebay` section of `config.json`):

* Only Buy It Now listings that ship to Cyprus (`deliver_to`), newest first, on the
  `marketplaces` you list (default eBay.de). Auctions and "for parts" items are skipped.
  One marketplace is enough: with delivery to Cyprus the API searches the listings of
  every eBay site (tested 2026-11-09: all 11 European marketplaces returned the same
  items and totals). The marketplace only sets the currency and the link's domain.
* The price compared with the ceiling is item + shipping to Cyprus. For sellers
  outside the EU (UK, US, China...) `non_eu_import_vat_pct` (19) is added on top.
* On eBay a "card" under `scam_ratio` x its used value (40% of `max_eur`) is skipped instead of alerted
  (`skip_suspiciously_cheap`): in practice those are coolers, empty boxes, "ohne Core &
  VRAM" donor boards or fakes. Set it to `false` to get them with a warning instead.
  German part words (Kühler, Halter, ohne Core, für Teile...) are in the eBay
  `exclude_title_patterns`.
* On every site a title is skipped when it names only another maker (a 2004
  "GeForce 6800" is not an RX 6800), gives the memory in MB, or states less VRAM than
  the model has (a "16GB" listing is not an RTX 4090). "Dual/Triple Fan" cards are not
  treated as loose fans.
* `ceiling_factor` (default 1.0) multiplies `gpus[].max_eur` for eBay only. The local
  ceilings are about 10 percent under eBay sold prices because local deals have no
  buyer protection; eBay does, so 1.1 is a reasonable value if you want more eBay alerts.
* `every_minutes` (10) limits how often eBay is asked; that is about 300 of the
  5,000 free API calls per day.
* A keyword watch can search eBay too by adding `"ebay": true` (optionally with its own
  `"ebay_searches"`). The DF64 watch has it off, because at "any price" every new
  DF64 sold by a shop would alert.

## Cypriot computer shops

The `shops` section of `config.json` adds the new (and a few refurbished) cards of
four Cypriot shops. They are read every `every_minutes` (30) in background threads,
so the 5-minute marketplace scans are not slowed down:

| Shop | How it is read |
|---|---|
| Armenius | the shop's public WooCommerce Store API (JSON), main + refurbished GPU categories, 5 s between requests |
| Bionic | the product data its graphics-card page embeds as JSON |
| Custom PC | NVIDIA / AMD / Intel graphics-card category pages, 5 s apart (its robots.txt crawl-delay) |
| ComputerG | graphics-card category pages, newest first, 10 s apart (its robots.txt crawl-delay) |

* Only pages each shop's robots.txt allows are used (no search, sorting or
  items-per-page URLs), and the requests say they come from `gpu-alert-bot`.
* Only cards you can order now count. ComputerG drop-ships most cards; the alert
  shows its delivery time ("Ships in 7-10 days").
* Prices are shop prices including VAT, picked up in store or with the shop's own
  delivery, compared with the same `gpus[].max_eur` ceilings. In Nov 2026 Cypriot shop
  prices are far above used values (RTX 5060 Ti 16GB 930-995 EUR, RX 9070 XT
  1,080-1,210 EUR), so shop alerts will only come with real sales (Black Friday).
  Raise `shops.ceiling_factor` if a new card with warranty is worth more to you.
* A shop that returns 0 cards triggers the same once-a-day Telegram warning as the
  marketplaces (layout change or blocked).
* needy.shop is not included: its terms forbid scraping, and its agent policy only
  allows automated access through its UCP catalog API with a published agent
  profile.

## Ready PCs (complete desktops)

With RAM and graphics cards as expensive as they are, a complete PC is often cheaper
than its parts. The `pcs` section of `config.json` watches for that:

1. The bot reads which **graphics card, processor and RAM** an advert names.
2. Each part is priced at what it costs **used when bought on its own** (tables in
   `pcs`: `gpu_eur`, `other_gpus`, `cpu_eur`, `ram_eur_per_gb`), plus `rest_eur` for the
   board, PSU, case and SSD.
3. You get an alert when the PC costs at most `max_price_ratio` x that sum. It is set to
   1.05: **anything up to 5% above the price of its parts bought one by one**. The alert
   says how the price compares ("14% cheaper as a ready PC", "4% dearer than its parts,
   but ready built"), and GREAT DEAL marks PCs at least 16% cheaper than their parts.
   Stricter: 1.0 = no dearer than the parts, 0.85 = only PCs at least 15% cheaper.
   There is no price cap (`budget_eur: null`).

The alert shows the sum and every part it counted, for example
`RTX 3060 12GB 350 + i5-11600 95 + 16GB DDR4 67 + board, PSU, case, SSD 170`.

Where it looks:

| Source | How |
|---|---|
| bazaraki, vendora | the newest adverts of their desktop-PC sections, every `every_minutes` (10) |
| the graphics-card feeds | whole PCs that turn up there are now valued instead of ignored |
| Armenius, Bionic, Custom PC, ComputerG | their complete-PC categories, every `shops_every_minutes` (180), same polite pace as the cards |

Which PCs count:

* a PC with a card from your 12 GB+ list (`gpus[]`), or
* a strong base to put your own card into: a processor worth `min_cpu_eur` (60, a
  Ryzen 5 3600 or better) or more with at least `min_ram_gb` (16) of RAM. A smaller card
  in it still adds its value.
* Laptops, all-in-ones, Macs, mini and office PCs, servers and part bundles are left
  out (`pcs.exclude_title_patterns`). Brand-name boxes (HP, Dell, Lenovo, Acer...)
  get only `rest_eur.oem` for the rest: their boards, PSUs and cases are proprietary.

How it stays careful:

* A part the advert does not name counts as **0**, so a vague advert never looks cheap.
* If the VRAM is not stated, the cheaper variant is assumed (an "RTX 3060" counts as
  the 8 GB one). Intel 12th-14th gen RAM counts as DDR4 unless the advert says DDR5;
  the alert tells you how much more the parts are worth if it is DDR5.
* A bare number is only a graphics card after a word like RTX/GTX/RX: "Dell 3060" is
  an OptiPlex.
* On bazaraki many sellers write just "Gaming PC". For adverts above
  `details_min_price` whose title leaves parts open, the seller's description is read
  once (at most `detail_fetches_per_scan` adverts per scan) and remembered in
  `seen.json`. The alert says when parts came from the description.
* Under `scam_ratio` x the parts sum a shop item is skipped (not a complete PC) and a
  marketplace advert is tagged SUSPICIOUSLY CHEAP.

The part prices are **market prices** (used Buy It Now offers on eBay Germany, lower
quartile, measured 2026-11-12), not the stricter `gpus[].max_eur` ceilings. Measured
that day: DDR5 about 13.5 EUR/GB (32 GB = 430), DDR4 about 4.2 EUR/GB. Refresh the
tables every few weeks. To see more PCs raise `max_price_ratio` or lower `min_cpu_eur`;
a PC seen earlier that qualifies only after the change alerts once.
`python gpu_alert.py --list` shows every valued PC with its ceiling.

## Second buyer: a VAT-registered business

The `business` section watches the same shops for a friend's family business that
needs a card too and can reclaim the 19% VAT on a shop invoice (only legitimate when
the card really is for the business). Instead of the used-value ceilings it uses
**shop price cuts**:

* any card from `gpus[]` (same title checks, 12 GB+) whose shop price falls
  `min_drop_pct` (10%) or more below the first price the bot saw for that product,
* with the price without VAT (`price / 1.19`) at or under `budget_eur` (600),
* is sent as `[BUSINESS PRICE DROP]`, showing the price with and without VAT.
  Each cut alerts once, and again only after a further 3% drop.

The reference price is stored per product in `seen.json` (`first_price`), so cuts
are measured from when the bot started watching the shops (2026-11-09). Marketplace
and eBay listings are not checked for the business: private sellers give no VAT invoice.
