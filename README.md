# GPU Alert Bot (bazaraki.com + vendora.cy + eBay)

Watches the newest GPU listings on both Cypriot marketplaces and alerts you when a
card with **12 GB VRAM or more** shows up at or below what it is worth second-hand
(and never above your **600 EUR** budget).

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
* `ambiguous_vram: true` marks models that also exist with less VRAM (3060 8/12 GB,
  3080 10/12 GB, 5060 Ti 8/16 GB, 9060 XT 8/16 GB, A770 8/16 GB). If the title
  names a smaller size the ad is skipped; if it names no size you get the alert
  with a "confirm VRAM" note. `require_vram: true` (RTX 2060) skips it unless
  "12GB" is written.
* `exclude_title_patterns` - regexes that drop whole PCs, laptops, fans,
  wanted-ads etc. Add your own.
* `great_deal_ratio` / `scam_ratio` - below 80 percent of the ceiling the alert is
  tagged GREAT DEAL; below 40 percent it is tagged as a likely scam.

## Notes

* Shops on bazaraki re-post the same stock daily under new ad IDs; those are
  mostly new cards above your ceilings, so they rarely alert.
* Only titles are parsed. A seller who writes just "graphics card" will be missed.
* Keep `poll_minutes` at 5 or more; the bot fetches about 7 pages per scan.

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
* The price compared with the ceiling is item + shipping to Cyprus. For sellers
  outside the EU (UK, US, China...) `non_eu_import_vat_pct` (19) is added on top.
* On eBay a "card" under `scam_ratio` x its ceiling (40%) is skipped instead of alerted
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
