# Job Finder (email digest)

Daily 9 AM IST email with: (1) MNC off-campus and walk-in drives, (2) fresher jobs matching your skills,
0-1 yr experience, salary up to 4.5 LPA (if listed), in your preferred cities. You open the link and apply.

## Setup
1. Push this folder to a GitHub repo.
2. Gmail: turn on 2-Step Verification, then create an App Password (myaccount.google.com/apppasswords).
   Repo Settings > Secrets > Actions: `GMAIL_USER` (your gmail), `GMAIL_APP_PASSWORD`.
3. Jobs: free key at developer.adzuna.com -> secrets `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`.
4. Actions tab > "Daily job digest" > Run workflow.

## Tune
`config.json`: cities, skills, max salary, MNC list, trusted domains, drive_feeds (RSS URLs).
Feeds that fail are skipped; replace dead URLs with working ones.
