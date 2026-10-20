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

