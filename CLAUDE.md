# CLAUDE.md

Internal tool for **dublab.cat** (Barcelona community radio) to upload recorded radio shows to Mixcloud. A host or staff member picks the show's `.mp3`. The app works out which show it is from the filename, fills in metadata from the radio's API (host names, description, tags, picture), and posts it all to Mixcloud. The recordings belong to the radio and its hosts but are publicly listenable on Mixcloud.

This file is the shared, version-controlled memory for the project. Claude's local auto-memory (`~/.claude/projects/...`) does **not** sync between machines, so anything worth keeping across locations goes here (see "Knowledge log" at the bottom).

## Running locally

```bash
python3 -m venv .venv                      # once
.venv/bin/pip install -r requirements.txt  # once / when deps change
source .venv/bin/activate
uvicorn mixcloud_backend:app --reload --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000. Always run it from the repo root: `index.html`, `shows.json`, `token.txt`, `uploads/` and the log are all resolved relative to the working directory.

- Homebrew's `uvicorn` runs in its own isolated Python and can't see project deps. Activate `.venv` or call `.venv/bin/uvicorn`.
- `python mixcloud_backend.py` also works. It regenerates `shows.json` on startup and serves without `--reload`.
- `path/to/venv/` is a leftover venv created by copy-pasting a docs example literally. It isn't used and can be deleted.
- There are no tests, linter, or build step.

## Deployment

Deployed to **Railway** (commit 114d589). The `Procfile` runs `uvicorn mixcloud_backend:app --host 0.0.0.0 --port $PORT`. Railway's filesystem is ephemeral, so `shows.json` is regenerated lazily on the first request after a deploy, and the Mixcloud token comes from the `MIXCLOUD_TOKEN` Railway variable (see Mixcloud auth). The frontend uses `window.location.origin` as the backend URL, so frontend and backend must be served from the same origin.

## Architecture

Two files hold all the logic:

- `mixcloud_backend.py`: FastAPI app, radio-API fetcher, Mixcloud uploader.
- `index.html`: single-page frontend (vanilla JS, inline CSS, decorative sparkle canvas). Served by `GET /`.

### Data flow

```
radio API (paginated)  ──generate_shows_json()──▶  shows.json  (normalized cache)
                                                     │
                     GET /shows_metadata ◀───────────┤
                            │                        │
   index.html: parse filename → prefill form         │
                            │                        │
                     POST /upload ──▶ MixcloudUploader.upload()
                                        ├─ find_best_match(title) in shows.json
                                        ├─ download picture from show_meta.picture
                                        └─ POST https://api.mixcloud.com/upload/
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Serves `index.html` |
| GET | `/shows_metadata` | Returns `shows.json`, generating it first if missing |
| POST | `/refresh_shows` | Re-fetches from the radio API and rewrites `shows.json` |
| POST | `/upload` | Multipart: `file`, `title` (required), `host`, `tags` (comma-separated), `description`, `day`, `month`, `year`. Returns `{"success": bool}` |

### The normalized show schema (the contract)

`shows.json` is a list of these objects. **Everything downstream (frontend prefill, tag suggestions, backend matching, title and description building) depends only on this shape, never on the raw API response.**

```json
{
  "id": 171,
  "name": "Ameba",                          // matched against the filename; used in the Mixcloud title
  "host": "Mario Fano & Héctor Pascual",    // string, "" if none
  "tags": ["Electronica", "Barcelona"],     // list[str]; Mixcloud accepts max 5
  "description": "…",                        // whitespace-collapsed by clean_text()
  "picture": "https://…/ameba.jpg",          // URL or null; downloaded and sent as cover art
  "slug": "ameba"                            // used to build the tracklist URL
}
```

### Current source API (Dublab)

- Base: `https://api.dublab.cat/api`. Endpoint: `GET /profiles/?page=N`. No auth.
- Django REST Framework–style pagination: `{"count": int, "results": [...]}`. Page size is inferred from the length of page 1.
- `fetch_all_profiles()` fetches page 1, then pages 2..N in parallel (10 threads). A failed page is logged and skipped.
- `extract_current_profiles()` keeps only `is_current == true` profiles and maps them to the schema above. Raw fields used: `id, name, host, tags, description, picture, slug, is_current`.

### Filename conventions (in `index.html`, file-input `change` handler)

- The expected filename is `<Show Name> YY.MM.DD.mp3`. The separator can be `.`, `-`, `_` or a space. Example: `Ameba 25.03.14.mp3`.
- The regex `(\d{1,2})[.\-_ ](\d{1,2})[.\-_ ](\d{2})` reads group 1 as **year**, group 2 as month and group 3 as day. The year field accepts 2 digits (20–30).
- The date is stripped out of the filename, and the rest, NFC-normalized (macOS gives NFD filenames, see commit 3eeacfd), becomes the title.
- **Frontend match:** the first `shows.json` name that is a case-insensitive *substring* of the title. On a match it prefills host, description and tags. With no match it shows a warning and the button reads "Upload to Mixcloud anyway".

### Upload logic (`MixcloudUploader.upload`)

- **Backend match:** `difflib.get_close_matches(title, names, cutoff=0.6)`. This differs from the frontend's substring match, so the two can disagree. When the backend finds nothing, the Mixcloud title falls back to the raw form title, and there's no picture and no slug.
- Form values override show metadata: `host or meta.host`, `tags or meta.tags[:5]`, `description or meta.description`.
- **Mixcloud title:** `"{show name} {DD.MM.YY} w/ {host}"`.
- **Tracklist link** appended to the description:
  - Show `b-sides`: `http://dublab.cat/b-sides/{host-slugified}` (`&` removed, whitespace collapsed to `-`).
  - Any other show with a date: `http://dublab.cat/shows/{slug}/{DD-MM-YY}`.
- Always sends `hide_stats=true`. Tags go as `tags-{i}-tag` form fields (max 5).
- The picture is streamed from the API URL and sent as `picture` with a hard-coded `image/jpeg` content type.
- The MP3 is first written to `uploads/<original filename>` and deleted after the upload.

### Mixcloud auth

- `MixcloudAuth.get_token()` reads a pre-obtained OAuth access token from the `MIXCLOUD_TOKEN` env var (set in Railway → service → Variables), falling back to `token.txt` for local dev. `client_id`, `client_secret` and `redirect_uri` (env `MIXCLOUD_CLIENT_ID`, `MIXCLOUD_CLIENT_SECRET`, `REDIRECT_URI`) are stored but **never used**. There is no OAuth flow in the app.
- The token is sent as an `access_token` query parameter.
- On HTTP 401/403 from Mixcloud, the app logs the error and clears the in-memory token (it no longer deletes `token.txt`). To renew: get a token through Mixcloud's OAuth flow (developer app at mixcloud.com/developers), then update `MIXCLOUD_TOKEN` in Railway (that redeploys the service) or `token.txt` locally.

## Adapting to a new radio API (planned)

The intended seam is the **normalized show schema**. To switch sources:

1. Rewrite `get_total_pages` / `fetch_page` / `fetch_all_profiles` for the new API's URL, auth and pagination, and change `BASE_URL`.
2. Rewrite `extract_current_profiles` to map the new fields onto `id, name, host, tags, description, picture, slug`, and to work out which shows are "current".
3. Keep the output shape identical. Then `index.html`, `find_best_match` and `upload` need no changes.
4. Watch the parts that hard-code Dublab assumptions outside the fetcher:
   - The tracklist URLs (`dublab.cat/shows/{slug}/{date}` and the `b-sides` special case) in `upload()`.
   - `host` being a single string (a new API might return a list of hosts and need joining).
   - `tags` being a list of strings (a new API might return objects).
   - The picture's `image/jpeg` content type.
5. Delete `shows.json` (or call `POST /refresh_shows`) after deploying so the cache is rebuilt.

When the new API definition arrives, record its endpoint, auth and field mapping under "Knowledge log" below.

## Known issues / gotchas

- `upload_to_mixcloud` is `async def` but calls blocking `requests` code, so a large upload blocks the event loop for every other request.
- `uploads/<file.filename>` uses the client-supplied filename unsanitized (path traversal possible). It also collides if two people upload the same filename at once.
- With an empty date, `date_str_for_title` becomes `".."`, because `.strip("-")` doesn't strip dots. With an empty host, the title ends in `" w/"`.
- `MixcloudUploader` is re-created on every upload, re-reading `shows.json` from disk each time.
- The radio API calls have no timeout. Only `requests.HTTPError` is caught per page, not connection errors.
- CORS is wide open (`*`), and there's no auth on any endpoint. Anyone who can reach the server can upload to the radio's Mixcloud account or trigger refreshes.

## Security notes

- The production token lives in Railway's `MIXCLOUD_TOKEN` variable. `token.txt` is for local dev only and must not be committed. Never print or log the token.
- History: `token.txt` was committed and pushed to GitHub (`zenjit/mixcloud-uploader`) at commits 66c251c, aa9a412 and 7e839d1, so any token from before the move to the env var counts as leaked.
- Don't commit `shows.json`, `mixcloud_backend.log`, `uploads/` or `.venv/`. They're generated locally.

## Conventions

- Keep it simple: a single backend module plus a single HTML file, no frameworks or build tooling on the frontend.
- Commit messages are short, lowercase and imperative-ish ("fixed date", "added hide_stats").
- Logging goes to stdout and to `mixcloud_backend.log` (named after the module), with emoji status markers (✅ ❌ 🔑).
- Show names, hosts and descriptions are multilingual (Catalan, Spanish, English) with accents. Keep `encoding="utf-8"` / `ensure_ascii=False` and NFC normalization.

## Knowledge log

Append dated entries here for decisions, discoveries, credentials *locations* (never values), and API details learned while working, so they're available from any machine.

- 2026-10-13: Token moved from the committed `token.txt` to the `MIXCLOUD_TOKEN` env var (file kept as a local fallback); a 401/403 no longer deletes the file.
- 2026-10-13: CLAUDE.md created from a full code review. Local dev uses `.venv` because Homebrew's uvicorn is isolated. 62 current shows in the Dublab API at this date.
