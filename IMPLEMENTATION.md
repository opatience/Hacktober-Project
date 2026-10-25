# Backend implementation and live setup

Completed 2026-10-25 for the selected Flask hackathon scope. App: http://127.0.0.1:5050 .

## Live resources

- Snowflake database/schema: `HACKATHON_DB.APP`, with all 11 tables from the spec.
- Roles: `HACKATHON_OWNER`, `HACKATHON_API`, `HACKATHON_INGEST`.
- Warehouses: `HACKATHON_APP_WH`, `HACKATHON_SEARCH_WH`; XSMALL with idle suspension.
- Cortex Search service: `HACKATHON_DB.APP.EVENT_SEARCH`.
- Cortex Agent: `HACKATHON_DB.APP.EVENT_ASSISTANT`.
- Extraction model: `llama3.1-8b`; the account's existing cross-region policy was `ANY_REGION` and was not changed.

## Implemented behavior

The app stores users, preferences, follows, club mappings, posts, canonical events, event-source evidence, and tags in Snowflake. Users sign up and log in with email and password (`USERS.PASSWORD_HASH`, werkzeug hash). Every followed account gets its own club automatically, so all followed accounts can publish calendar events.

The source cache contains 45 actual public Instagram posts for Purdue Hackers, CS Undergraduate Student Board, Build Purdue, and Hello World Purdue. Four exact post images were read with macOS Vision OCR. A real Cortex extraction of those four posts committed five source-validated occurrences. Past occurrences are removed from the upcoming feed at read time. Source mode remains attached to posts; a cached capture is not described as a fresh scrape.

Feed requests use saved interests, follows, soft natural-language date/price preferences, Cortex Search, and current SQL facts. Explicit free-only, followed-only, exclusion, and date controls remain strict. Unknown admission prices stay unknown. Date-only cards expose no invented clock time.

`POST /v1/events/ask` invokes the stored Cortex Agent. Its structured citations are intersected with the currently eligible feed; the public answer is composed from current stored facts and source links. Agent failure falls back with a warning. A failed extraction on unchanged source content preserves previously supported event facts.

## Verified against the live account

- Provisioning reported 11/11 base tables and both Cortex resources created.
- Profile, tag catalog, follow creation, preference saving, followed-account list, event feed, and newsletter requests returned successful responses.
- Following two real accounts returned 12 stored newsletter posts.
- Semantic event retrieval returned four upcoming events with `backend=snowflake` and `search_degraded=false`.
- The Agent endpoint returned `agent_used=true`, four validated citations, and no warnings.
- Python compilation and `git diff --check` passed. No full test suite was added or run.

## Run and handoff

From this directory, run `.venv/bin/python app.py`. The local `.env` already selects Snowflake and port 5050. Connection material is local and ignored by Git.

`python snowflake/apply.py --help` documents setup. `python scripts/import_cached.py --rules` reloads source-backed caption/OCR extraction; omit `--rules` to use the configured Cortex model. `scripts/ocr.swift` can repeat OCR for supplied local image paths.

The implementation uses one process with serialized writes and an explicit refresh action. Scheduled workers, durable pagination, email delivery, and production hosting remain deferred. Frontend design work was stopped when the user assigned it to another agent. The `/v1` route contracts in `app.py` are the frontend integration surface.

Changes remain uncommitted. Deployment records are in the system temporary directory as `snowflask-setup.jsonl`.
