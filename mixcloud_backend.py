import os
import json
import logging
import requests
from pathlib import Path
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import uvicorn
import re
from difflib import get_close_matches

# -----------------------
# Logging setup
# -----------------------
log_file = os.path.splitext(os.path.basename(__file__))[0] + ".log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# -----------------------
# Mixcloud Auth
# -----------------------
class MixcloudAuth:
    def __init__(self, client_id, client_secret, redirect_uri, token_file="token.txt"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.token = None

    def get_token(self):
        if self.token:
            return self.token
        env_token = os.getenv("MIXCLOUD_TOKEN", "").strip()
        if env_token:
            self.token = env_token
            return self.token
        if os.path.exists(self.token_file):
            with open(self.token_file, "r", encoding="utf-8") as f:
                self.token = f.read().strip()
        if not self.token:
            raise RuntimeError("No Mixcloud token found. Please authenticate first.")
        return self.token

# -----------------------
# Dublab API fetching
# -----------------------
API_BASE_URL = "https://v2.dublab.cat/api/v1"
# Profile pictures come back as relative paths. New uploads ("profiles/...") are served
# by the v2 server; legacy ones ("photos/...") are still only served by the old server.
PICTURE_BASE_URL = "https://v2.dublab.cat/uploads/"
LEGACY_PICTURE_BASE_URL = "https://api.dublab.cat/media/"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
SHOWS_JSON_FILE = Path("shows.json")

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()

def resolve_picture_url(path):
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    path = path.lstrip("/")
    base = LEGACY_PICTURE_BASE_URL if path.startswith("photos/") else PICTURE_BASE_URL
    return base + path

def fetch_current_profiles():
    profiles = []
    skip = 0
    while True:
        resp = requests.get(
            f"{API_BASE_URL}/profiles",
            params={"isCurrent": "true", "skip": skip, "take": PAGE_SIZE},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        page = resp.json()
        profiles.extend(page)
        if len(page) < PAGE_SIZE:
            return profiles
        skip += PAGE_SIZE

def fetch_profile_by_slug(slug):
    resp = requests.get(f"{API_BASE_URL}/profiles/slug/{slug}", timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()

def search_current_profile(query):
    resp = requests.get(
        f"{API_BASE_URL}/profiles",
        params={"isCurrent": "true", "take": 1, "search": query},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None

def normalize_profile(profile):
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "host": profile.get("host") or "",
        "tags": profile.get("tags") or [],
        "description": clean_text(profile.get("description")),
        "picture": resolve_picture_url(profile.get("picture")),
        "slug": profile.get("slug") or ""
    }

def generate_shows_json():
    logging.info("Fetching profiles from Dublab API...")
    profiles = fetch_current_profiles()
    current_profiles = [normalize_profile(p) for p in profiles if p.get("isCurrent")]
    with open(SHOWS_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(current_profiles, f, ensure_ascii=False, indent=2)
    logging.info(f"✅ Generated shows.json with {len(current_profiles)} shows")
    return current_profiles

# -----------------------
# Mixcloud Uploader
# -----------------------
class MixcloudUploader:
    def __init__(self, auth):
        self.auth = auth
        self.metadata = self.load_metadata()

    def load_metadata(self):
        if not SHOWS_JSON_FILE.exists():
            logging.warning("shows.json not found, generating...")
            return generate_shows_json()
        with open(SHOWS_JSON_FILE, encoding="utf-8") as f:
            return json.load(f)

    def find_best_match(self, query):
        candidates = [show["name"] for show in self.metadata]
        matches = get_close_matches(query.lower(), [c.lower() for c in candidates], n=1, cutoff=0.6)
        if matches:
            for show in self.metadata:
                if show["name"].lower() == matches[0]:
                    return show
        return None

    def lookup_show(self, title):
        """Match the title against the cached shows, then refresh that profile from the
        API by slug. Falls back to the API search when the cache has no match."""
        cached = self.find_best_match(title)
        try:
            if cached:
                fresh = fetch_profile_by_slug(cached["slug"]) if cached.get("slug") else None
                return normalize_profile(fresh) if fresh else cached
            found = search_current_profile(title)
            if found:
                logging.info(f"Matched '{title}' to '{found.get('name')}' via API search")
                return normalize_profile(found)
        except requests.RequestException as e:
            logging.warning(f"Dublab API lookup failed for '{title}', using cached data: {e}")
            return cached or {}
        logging.warning(f"No show found for '{title}'")
        return {}

    def upload(self, mp3_path, title=None, host=None, tags=None, description=None, date_str=None, date_str_for_title=None):
        token = self.auth.get_token()
        url = f"https://api.mixcloud.com/upload/?access_token={token}"

        files = {"mp3": open(mp3_path, "rb")}

        show_meta = self.lookup_show(title)
        final_host = host or show_meta.get("host", "")
        final_tags = tags or show_meta.get("tags", [])[:5]
        final_description = description or show_meta.get("description", "")

        if show_meta.get('name') == "b-sides":
            host_clean = re.sub(r'-+', '-', re.sub(r'\s+', '-', final_host.replace('&', '').strip()))
            final_description += f"\n\nTracklist: http://dublab.cat/b-sides/{host_clean}"
        elif date_str:
            final_description += f"\n\nTracklist: http://dublab.cat/shows/{show_meta.get('slug','')}/{date_str}"

        data = {
            "name": f"{show_meta.get('name', title)} {date_str_for_title or ''} w/ {final_host}".strip(),
            "description": final_description or "Uploaded via Mixcloud Uploader",
            "hide_stats": "true"
        }

        for i, tag in enumerate(final_tags[:5]):
            data[f"tags-{i}-tag"] = tag

        picture_url = show_meta.get("picture")
        if picture_url:
            pic_resp = requests.get(picture_url, stream=True, timeout=REQUEST_TIMEOUT)
            if pic_resp.status_code == 200:
                content_type = pic_resp.headers.get("Content-Type", "image/jpeg")
                files["picture"] = (Path(picture_url).name, pic_resp.raw, content_type)
            else:
                logging.warning(f"Could not download picture {picture_url}: {pic_resp.status_code}")

        logging.info(f"Uploading '{data['name']}' with tags {final_tags} and host '{final_host}'")

        try:
            resp = requests.post(url, files=files, data=data)
        finally:
            for f in files.values():
                if hasattr(f, "close"):
                    f.close()

        if resp.status_code == 200:
            logging.info("✅ Upload successful")
            return True
        elif resp.status_code in (401, 403):
            logging.error(f"🔑 Access token rejected by Mixcloud ({resp.status_code}): {resp.text} — renew MIXCLOUD_TOKEN")
            self.auth.token = None
            return False
        else:
            logging.error(f"❌ Upload failed: {resp.status_code} {resp.text}")
            return False

# -----------------------
# FastAPI App
# -----------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def serve_frontend():
    return FileResponse("index.html")

@app.get("/shows_metadata")
def shows_metadata_endpoint():
    if not SHOWS_JSON_FILE.exists():
        generate_shows_json()
    with open(SHOWS_JSON_FILE, encoding="utf-8") as f:
        return json.load(f)

@app.post("/refresh_shows")
def refresh_shows_endpoint():
    return generate_shows_json()

@app.post("/upload")
async def upload_to_mixcloud(
    file: UploadFile,
    title: str = Form(...),
    host: str = Form(""),
    tags: str = Form(""),
    description: str = Form(""),
    day: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
):
    os.makedirs("uploads", exist_ok=True)
    temp_path = Path("uploads") / file.filename
    with open(temp_path, "wb") as f:
        f.write(await file.read())

    date_str = f"{day}-{month}-{year}".strip("-")
    date_str_for_title = f"{day}.{month}.{year}".strip("-")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    uploader = MixcloudUploader(auth)
    success = uploader.upload(str(temp_path), title, host, tag_list, description, date_str, date_str_for_title)

    try:
        os.remove(temp_path)
    except Exception as e:
        logging.warning(f"Could not delete temp file: {e}")

    return JSONResponse({"success": success})

# -----------------------
# App startup
# -----------------------
if __name__ == "__main__":
    auth = MixcloudAuth(
        client_id=os.getenv("MIXCLOUD_CLIENT_ID"),
        client_secret=os.getenv("MIXCLOUD_CLIENT_SECRET"),
        redirect_uri=os.getenv("REDIRECT_URI", "http://localhost:8080/callback"),
    )
    generate_shows_json()  # Generate on startup
    uvicorn.run("mixcloud_backend:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
else:
    auth = MixcloudAuth(
        client_id=os.getenv("MIXCLOUD_CLIENT_ID"),
        client_secret=os.getenv("MIXCLOUD_CLIENT_SECRET"),
        redirect_uri=os.getenv("REDIRECT_URI", "http://localhost:8080/callback"),
    )
