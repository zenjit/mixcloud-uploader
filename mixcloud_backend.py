import os
import json
import logging
import requests
from pathlib import Path
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import uvicorn
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        if os.path.exists(self.token_file):
            with open(self.token_file, "r", encoding="utf-8") as f:
                self.token = f.read().strip()
        if not self.token:
            raise RuntimeError("No Mixcloud token found. Please authenticate first.")
        return self.token

# -----------------------
# Dublab API fetching
# -----------------------
BASE_URL = "https://api.dublab.cat/api"
SHOWS_JSON_FILE = Path("shows.json")

def clean_text(text):
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()

def get_total_pages():
    url = f"{BASE_URL}/profiles/?page=1"
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()
    count = data["count"]
    per_page = len(data["results"])
    total_pages = (count + per_page - 1) // per_page
    return total_pages, data["results"]

def fetch_page(page):
    url = f"{BASE_URL}/profiles/?page={page}"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json().get("results", [])

def fetch_all_profiles(max_workers=10):
    total_pages, first_page_results = get_total_pages()
    all_profiles = first_page_results
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_page, page): page for page in range(2, total_pages + 1)}
        for future in as_completed(futures):
            try:
                all_profiles.extend(future.result())
            except requests.HTTPError as e:
                logging.warning(f"Failed to fetch page {futures[future]}: {e}")
    return all_profiles

def extract_current_profiles(profiles):
    current_profiles = [p for p in profiles if p.get("is_current")]
    extracted = []
    for profile in current_profiles:
        extracted.append({
            "id": profile.get("id"),
            "name": profile.get("name"),
            "host": profile.get("host") or "",
            "tags": profile.get("tags") or [],
            "description": clean_text(profile.get("description")),
            "picture": profile.get("picture"),
            "slug": profile.get("slug") or ""
        })
    return extracted

def generate_shows_json():
    logging.info("Fetching profiles from Dublab API...")
    profiles = fetch_all_profiles()
    current_profiles = extract_current_profiles(profiles)
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

    def upload(self, mp3_path, title=None, host=None, tags=None, description=None, date_str=None, date_str_for_title=None):
        token = self.auth.get_token()
        url = f"https://api.mixcloud.com/upload/?access_token={token}"

        files = {"mp3": open(mp3_path, "rb")}

        show_meta = self.find_best_match(title) or {}
        final_host = host or show_meta.get("host", "")
        final_tags = tags or show_meta.get("tags", [])[:5]
        final_description = description or show_meta.get("description", "")

        if date_str:
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
            pic_resp = requests.get(picture_url, stream=True)
            if pic_resp.status_code == 200:
                files["picture"] = (Path(picture_url).name, pic_resp.raw, "image/jpeg")

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
            logging.warning("🔑 Access token invalid — deleting token file")
            try:
                os.remove(self.auth.token_file)
            except OSError as e:
                logging.error(f"Failed to remove token file: {e}")
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
