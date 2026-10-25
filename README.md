# InstaLetter

Follow Instagram accounts to collect their recent posts into a newsletter and
find upcoming Purdue club events. Save interests, topics to avoid, and optional
date, admission, and followed-account filters. The event list and calendar link
back to the original posts.

## Start locally

Run these commands from this project folder. If `.venv` and its dependencies
already exist, skip the first two commands. Keep an existing `.env` file.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
PORT=5050 .venv/bin/python app.py
```

Open [InstaLetter on localhost:5050](http://localhost:5050). Follow a club account,
then choose **Refresh posts**. The app starts with no fabricated posts or events.
Refresh can return original public posts from a previous saved Instagram capture
when Instagram is unavailable; the response includes a warning in that case.

The supplied `.env.example` selects local SQLite storage. This stores follows,
preferences, and ingested content in `.local/feed.sqlite3`. Starting the local
app does **not** mean Snowflake, Cortex Search, or model inference is connected.
With local storage, caption rules extract only supported dates that include a
year. A post without sufficient event evidence still appears in the newsletter.

Sign up at `/signup` with an email and password (hashed into `USERS.PASSWORD_HASH`).
Set a stable random `FLASK_SECRET_KEY` in `.env` so login sessions survive restarts.
An account provisioned before login was added needs `snowflake/007_password_login.sql`.

## Connect the Snowflake account

The reviewed setup and account configuration are described in
[snowflake/README.md](snowflake/README.md). The setup runner opens a browser for
Snowflake authentication; no password needs to be pasted into the app or chat.

```bash
# Show the selected setup files without connecting or executing SQL.
.venv/bin/python snowflake/apply.py --seed-tags --seed-clubs

# After reviewing the files, create the storage resources and curated mappings.
.venv/bin/python snowflake/apply.py --apply --seed-tags --seed-clubs --status
```

After provisioning, change `STORAGE_BACKEND=snowflake` in `.env`. Configure
`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, authentication, API/ingest roles, database,
schema, and warehouse. For Cortex extraction, set `AI_BACKEND=cortex` and choose
an enabled `SNOWFLAKE_EXTRACT_MODEL`. These values are read only by the server.
Private keys and credentials stay outside version control.

Search and Agent objects are optional separate resources. Review
`snowflake/004_search_agent.sql` before enabling them:

```bash
.venv/bin/python snowflake/apply.py --apply --include-search --status
```

This starts billable Search indexing and serving. Object creation does not prove
index readiness, model access, or an end-to-end result. The running app can use
Cortex Search to rank candidates, then checks current event facts and hard
filters again. When Search fails, it keeps the same filters and uses follows,
tags, and event text, with a visible warning.

## Instagram ingestion and event evidence

`SCRAPER=instaloader` requests public Instagram posts. `SCRAPER=apify` uses a
configured `APIFY_TOKEN`. `SCRAPER=cached` reads previously captured posts from
`data/instagram_posts.json`. Instagram access can fail or be restricted; refresh
errors are shown explicitly. `IG_USERNAME` and `IG_SESSIONFILE` can configure an
existing local Instaloader session.

Event extraction currently reads captions only. It does not perform image OCR.
A source must support the event date and year; missing time, location, or cost
stays unknown. Free food is not treated as free admission. Every followed account gets its own club automatically and can publish
calendar events. Edited or repeated posts are persisted
through the shared serialized writer.

Saved text and search phrases affect ranking. The explicit **Free events only**,
**Only accounts I follow**, and **Look ahead** controls are hard filters.
Excluded topics also override a follow or relevance score.

## Current scope

The app includes email/password accounts with persistent follows/preferences, on-demand ingestion,
newsletter posts, evidence-checked caption events, deterministic ranking,
Snowflake/local storage adapters, and an optional Cortex Search integration.

The `/v1/events/ask` endpoint currently formats eligible event results and source
citations deterministically. It does **not** call the provisioned Cortex Agent.
Real identity-provider login, Cortex Agent request integration, image OCR,
background ingestion workers, cursor pagination, and scheduled email delivery
remain outside this implementation. Run one backend process so the application
write and refresh locks cover all requests.

No account setup, model validation, or acceptance result is claimed by this
README. Consult the current run output and Snowflake query history for the
actual environment state.

## Main files

| File | Purpose |
| --- | --- |
| `app.py` | Flask routes, email/password login, refresh orchestration |
| `storage.py` / `snowflake_client.py` | Serialized persistence and scoped Snowflake access |
| `feed.py` | Hard event filters, preference preview, and ranking |
| `scraper.py` / `ai.py` / `dates.py` | Public-post ingestion and validated caption extraction |
| `templates/index.html` / `static/app.*` | Preferences, follows, events, newsletter, and calendar UI |
