# Eurostar Snap watcher

Checks Eurostar Snap every 5 minutes for London → Paris, Brussels, Amsterdam,
Lille and Rotterdam on 31 Oct – 2 Nov 2026 (1 adult, single). It:

- logs every check to `data/prices.csv`: Snap price and regular fare per route and date
- keeps `data/latest.md` up to date, a table of the most recent check you can open on GitHub
- emails you when a Snap fare **appears** or **gets cheaper**
- emails you once if Eurostar starts blocking it

It calls the same API that snap.eurostar.com uses for its calendar
(`site-api.eurostar.com/gateway`, query `cheapestFaresSearch` with Snap's
`LASTMIN` fare family). That's 10 small requests per run. No scraping, no login.

## Setup (about 10 minutes)

### 1. Make a Gmail app password

Alerts are sent from a Gmail account. You can use your own or make a throwaway one.

1. The account needs 2-Step Verification on: <https://myaccount.google.com/security>
2. Go to <https://myaccount.google.com/apppasswords>, name it "Snap watcher", click **Create**.
3. Copy the 16-character password. You only see it once.

### 2. Create the GitHub repo

1. Go to <https://github.com/new>, name it something like `snap-watcher`.
2. Make it **Public**. Public repos get unlimited free Actions minutes; private
   ones get 2,000 a month, and every-5-minutes uses about 8,600. Nothing
   private goes in the repo: your email and password live in secrets (step 3).
   The only thing visible is the price log.
3. Upload these files, keeping the folder structure:
   `watcher.py`, `config.json`, `README.md`, `.github/workflows/watch.yml`.
   The easiest way is to drag the unzipped folder onto the repo's **"uploading an existing file"** link.
   If `.github` doesn't show up (hidden folder on Mac: press Cmd+Shift+. in Finder),
   create the file on GitHub instead: **Add file → Create new file**, type
   `.github/workflows/watch.yml` as the name and paste the contents.

### 3. Add the secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add three:

| Name | Value |
|---|---|
| `SMTP_USER` | the Gmail address sending the alerts |
| `SMTP_PASSWORD` | the 16-character app password |
| `ALERT_TO` | `colm@colm.one` |

### 4. Allow the workflow to push

Repo → **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → **Save**. This lets it commit the price log.

### 5. Run it once by hand

Repo → **Actions** tab → **Snap watcher** → **Run workflow**. After about 30 seconds:

- the run should go green, and its log shows the price table
- `data/latest.md` and `data/prices.csv` appear in the repo
- if any of your dates already have Snap fares, you get an email straight away

From then on it runs every 5 minutes on its own.

## If the first run says "blocked" or "challenge"

Eurostar uses AWS bot protection. It let plain requests through when this was
built, but GitHub's servers might get treated differently. If it's blocked,
you'll see it in the run log, and after 3 failed runs in a row you get one email.
Options then:

- **Run it on your own machine** (home internet is much less likely to be blocked):
  `python3 watcher.py` works anywhere with Python 3.9+. Put it on a loop with
  `while true; do python3 watcher.py; sleep 300; done` while the laptop is awake,
  or use a Raspberry Pi or cheap VPS.
- Ask Claude to switch it to a headless browser (Playwright), which gets past
  most bot checks but is slower.

## Changing what it watches

Edit `config.json` on GitHub (pencil icon):

- `dates`: list of `YYYY-MM-DD`. Snap only sells up to 14 days ahead, so dates
  further out will show "sold out" until they enter that window.
- `destinations`: remove the ones you don't care about. UIC codes for other
  stations are on the Snap site's network requests, or ask Claude.
- `alert_on_price_drop`: set `false` if you only want "newly available" emails.

Once all your dates have passed, the script does nothing. Turn it off at
**Actions → Snap watcher → ⋯ → Disable workflow**.

## Notes

- GitHub's 5-minute schedule is best effort; gaps of 10–20 minutes happen at busy times.
- Prices are per person, single, in GBP, the cheapest Snap fare for that day.
  Snap picks your actual train within the time slot you choose when booking.
- Test your email any time: run `python watcher.py --test-email` locally with
  the three variables set, or just trigger a manual run.
