# GPU Alert Bot (bazaraki.com + vendora.cy)

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
