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
Dublab v2 API (skip/take)  ──generate_shows_json()──▶  shows.json  (normalized cache)
                                                     │
                     GET /shows_metadata ◀───────────┤
                            │                        │
   index.html: parse filename → prefill form         │
                            │                        │
                     POST /upload ──▶ MixcloudUploader.upload()
                                        ├─ lookup_show(title): cache match → GET /profiles/slug/{slug}
                                        │    (no match → GET /profiles?isCurrent=true&take=1&search=)
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

### Source API (Dublab v2)

Base `https://v2.dublab.cat/api/v1`, NestJS backend, no auth for reads. The OpenAPI spec is at `https://v2.dublab.cat/api/docs-json` (Swagger UI at `/api/docs`). Switched from the old `https://api.dublab.cat/api` (DRF, `?page=N`) on 2026-10-13.

| Call | Function | Use |
|---|---|---|
| `GET /profiles?isCurrent=true&skip=N&take=100` | `fetch_current_profiles()` | Builds `shows.json`. Returns a **plain JSON array** (no count/envelope); paging stops at a short page. `GET /profiles/count?isCurrent=true` → `{"count": n}` exists but isn't used. |
| `GET /profiles/slug/{slug}` | `fetch_profile_by_slug()` | At upload, re-fetches the matched show so metadata is fresh. 404 (`NotFoundException`) → `None`. Also returns a `shows` array (past episodes, each with `mixcloudUrl`), which is ignored. |
| `GET /profiles?isCurrent=true&take=1&search=…` | `search_current_profile()` | Upload fallback when the cached difflib match fails. |

- Raw profile fields: `id, name, host (str), tags (list[str]), isCurrent, slug, description, bio, picture, links, createdAt, updatedAt` (+ `_count` on lists and `shows` on the slug call). `normalize_profile()` maps them onto the schema above.
- **Pictures are relative paths.** All of them are served from `https://v2.dublab.cat/uploads/profiles/…`. `resolve_picture_url()` maps:
  - `profiles/x.png` → `https://v2.dublab.cat/uploads/profiles/x.png` (new uploads).
  - `photos/profiles/x.jpg` → `https://v2.dublab.cat/uploads/profiles/x.jpg` (legacy path still returned by the API; the `photos/` prefix is dropped). Example: Lucii On Air → `https://v2.dublab.cat/uploads/profiles/luciionair.jpg`.
  - An empty `picture` (`""`) becomes `null`, and the upload goes out with no cover.
- Image types include jpeg, png and webp. The upload forwards the image server's `Content-Type`.
- `b-sides` exists (slug `b-sides`) but has `isCurrent: false`, so it is not in `shows.json` and is not found by the search fallback. The b-sides tracklist special case therefore can't trigger right now. Left as-is on purpose for now (user decision, 2026-10-13). The API also has a separate b-sides resource (`CreateBsideDto` in the spec), which may be the proper source for b-sides.

### Filename conventions (in `index.html`, file-input `change` handler)

- The expected filename is `<Show Name> YY.MM.DD.mp3`. The separator can be `.`, `-`, `_` or a space. Example: `Ameba 25.03.14.mp3`.
- The regex `(\d{1,2})[.\-_ ](\d{1,2})[.\-_ ](\d{2})` reads group 1 as **year**, group 2 as month and group 3 as day. The year field accepts 2 digits (20–30).
- The date is stripped out of the filename, and the rest, NFC-normalized (macOS gives NFD filenames, see commit 3eeacfd), becomes the title.
- **Frontend match:** the first `shows.json` name that is a case-insensitive *substring* of the title. On a match it prefills host, description and tags. With no match it shows a warning and the button reads "Upload to Mixcloud anyway".

### Upload logic (`MixcloudUploader.upload`)

- **Backend match** (`lookup_show`): `difflib.get_close_matches(title, names, cutoff=0.6)` against `shows.json`, then a fresh copy via the slug endpoint. With no cache match, it tries the API search. If the API is unreachable, it uses the cached entry. The difflib match differs from the frontend's substring match, so the two can disagree. When nothing matches, the Mixcloud title falls back to the raw form title, and there's no picture and no slug.
- Form values override show metadata: `host or meta.host`, `tags or meta.tags[:5]`, `description or meta.description`.
- **Mixcloud title:** `"{show name} {DD.MM.YY} w/ {host}"`.
- **Tracklist link** appended to the description:
  - Show `b-sides`: `http://dublab.cat/b-sides/{host-slugified}` (`&` removed, whitespace collapsed to `-`).
  - Any other show with a date: `http://dublab.cat/shows/{slug}/{DD-MM-YY}`.
- Always sends `hide_stats=true`. Tags go as `tags-{i}-tag` form fields (max 5).
- The picture is streamed from its URL and sent as `picture` with the image server's `Content-Type`. A failed download is logged and the upload continues without a cover.
- The MP3 is first written to `uploads/<original filename>` and deleted after the upload.

### Mixcloud auth

- `MixcloudAuth.get_token()` reads a pre-obtained OAuth access token from the `MIXCLOUD_TOKEN` env var (set in Railway → service → Variables), falling back to `token.txt` for local dev. `client_id`, `client_secret` and `redirect_uri` (env `MIXCLOUD_CLIENT_ID`, `MIXCLOUD_CLIENT_SECRET`, `REDIRECT_URI`) are stored but **never used**. There is no OAuth flow in the app.
- The token is sent as an `access_token` query parameter.
- On HTTP 401/403 from Mixcloud, the app logs the error and clears the in-memory token (it no longer deletes `token.txt`). To renew: get a token through Mixcloud's OAuth flow (developer app at mixcloud.com/developers), then update `MIXCLOUD_TOKEN` in Railway (that redeploys the service) or `token.txt` locally.

## Changing the source API again

The seam is the **normalized show schema**. Rewrite the fetch functions and `normalize_profile()` / `resolve_picture_url()`, keep the output shape, and `index.html`, matching and `upload()` need no changes. Things hard-coded outside the fetcher: the tracklist URLs and the `b-sides` special case in `upload()`. After deploying, call `POST /refresh_shows` (or delete `shows.json`) so the cache is rebuilt.

## Known issues / gotchas

- `upload_to_mixcloud` is `async def` but calls blocking `requests` code, so a large upload blocks the event loop for every other request.
- `uploads/<file.filename>` uses the client-supplied filename unsanitized (path traversal possible). It also collides if two people upload the same filename at once.
- With an empty date, `date_str_for_title` becomes `".."`, because `.strip("-")` doesn't strip dots. With an empty host, the title ends in `" w/"`.
- `MixcloudUploader` is re-created on every upload, re-reading `shows.json` from disk each time.
- `/refresh_shows` and the startup fetch raise if the API is down. Upload-time lookups fall back to the cache.
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

- 2026-10-13: All profile photos moved to `https://v2.dublab.cat/uploads/profiles/`; the old `api.dublab.cat/media/` server is no longer used for pictures. Verified all 46 current pictures return 200.
- 2026-10-13: Confirmed the tracklist URL `http://dublab.cat/shows/{slug}/{DD-MM-YY}` is still valid on the new site. b-sides (not current in v2) deliberately left unhandled for now.
- 2026-10-13: Migrated to the Dublab v2 API (list + slug + search endpoints, relative picture paths). 49 current shows. Old shows missing from v2 as current include b-sides.
- 2026-10-13: Token moved from the committed `token.txt` to the `MIXCLOUD_TOKEN` env var (file kept as a local fallback); a 401/403 no longer deletes the file.
- 2026-10-13: CLAUDE.md created from a full code review. Local dev uses `.venv` because Homebrew's uvicorn is isolated. 62 current shows in the old Dublab API at this date.
