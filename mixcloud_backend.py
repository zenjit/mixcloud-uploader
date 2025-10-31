import os
import csv
import logging
import requests
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

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
            self.token = self.run_oauth_flow()
            with open(self.token_file, "w", encoding="utf-8") as f:
                f.write(self.token)
            logging.info(f"Saved new token to {self.token_file}")
        return self.token

    def run_oauth_flow(self):
        auth_url = (
            f"https://www.mixcloud.com/oauth/authorize?"
            f"client_id={self.client_id}&redirect_uri={self.redirect_uri}&response_type=code"
        )
        logging.info(f"Opening browser for Mixcloud authorization...")
        webbrowser.open(auth_url)

        code_holder = {}

        class OAuthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = urlparse(self.path).query
                params = parse_qs(query)
                if "code" in params:
                    code_holder["code"] = params["code"][0]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Authorization complete! You can close this window.")
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing code parameter.")

            def log_message(self, format, *args):
                return

        server_address = ('', 8080)
        httpd = HTTPServer(server_address, OAuthHandler)
        logging.info(f"Waiting for OAuth callback on {self.redirect_uri} ...")
        while "code" not in code_holder:
            httpd.handle_request()

        code = code_holder["code"]
        logging.info(f"Got code: {code}, exchanging for access_token...")

        token_url = "https://www.mixcloud.com/oauth/access_token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
            "grant_type": "authorization_code"
        }
        resp = requests.post(token_url, data=data)
        if resp.status_code != 200:
            logging.error(f"Failed to get access_token: {resp.status_code} {resp.text}")
            raise RuntimeError("OAuth token request failed")
        access_token = resp.json().get("access_token")
        if not access_token:
            raise RuntimeError("No access_token returned by Mixcloud")
        logging.info("OAuth flow completed successfully!")
        return access_token


# -----------------------
# Mixcloud Uploader
# -----------------------
class MixcloudUploader:
    def __init__(self, auth, shows_folder, metadata_file):
        self.auth = auth
        self.shows_folder = shows_folder
        self.metadata_file = metadata_file
        self.metadata = self.load_metadata()

    def load_metadata(self):
        metadata = {}
        if not os.path.exists(self.metadata_file):
            logging.warning(f"Metadata file not found: {self.metadata_file}")
            return metadata

        with open(self.metadata_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                show_name = row.get("show", "").strip()
                if show_name:
                    bio = row.get("bio", "").strip()
                    host = row.get("host", "").strip()
                    tags_csv = row.get("tags", "")
                    tags_list = [t.strip() for t in tags_csv.split(";") if t.strip()]
                    metadata[show_name] = {"bio": bio, "tags": tags_list, "host": host}
        logging.info(f"Loaded metadata for {len(metadata)} shows")
        return metadata

    def find_best_match(self, name):
        """Loose match for show name from CSV"""
        from difflib import get_close_matches
        candidates = list(self.metadata.keys())
        matches = get_close_matches(name.lower(), [c.lower() for c in candidates], n=1, cutoff=0.6)
        if not matches:
            return {}
        for key in candidates:
            if key.lower() == matches[0]:
                return self.metadata[key]
        return {}

    def find_image(self, show_name):
        """Try to find an image file in ./images/ matching the show name"""
        img_folder = Path("images")
        if not img_folder.exists():
            return None
        base_name = show_name.lower().replace(" ", "_")
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            candidate = img_folder / f"{base_name}{ext}"
            if candidate.exists():
                return str(candidate)
        return None

    def upload(self, mp3_path, title=None, host=None, tags=None, date_str=None):
        token = self.auth.get_token()
        url = f"https://api.mixcloud.com/upload/?access_token={token}"

        files = {"mp3": open(mp3_path, "rb")}

        show_name = title or os.path.basename(mp3_path).replace(".mp3", "")
        meta = self.find_best_match(show_name)
        bio = meta.get("bio", "").strip()
        csv_host = meta.get("host", "").strip()
        csv_tags = meta.get("tags", [])
        image_path = self.find_image(show_name)

        final_host = host or csv_host
        final_tags = tags or csv_tags[:5]

        description_parts = []
        if bio:
            description_parts.append(bio)
        if date_str:
            description_parts.append(
                f"Tracklist: http://dublab.cat/shows/{show_name.lower().replace(' ', '-')}/{date_str}"
            )
        description = "\n\n".join(description_parts) or "Uploaded via Mixcloud Uploader"

        show_name = f"{show_name} {date_str} w/ {final_host}"
        data = {"name": show_name, "description": description}
        for i, tag in enumerate(final_tags[:5]):
            data[f"tags-{i}-tag"] = tag

        # Include image if found
        if image_path:
            try:
                files["picture"] = open(image_path, "rb")
                logging.info(f"✅ Using image {image_path}")
            except Exception as e:
                logging.warning(f"⚠️ Could not open image file '{image_path}': {e}")
        else:
            logging.info("🖼️ No image found for this show")

        logging.info(f"Uploading '{show_name}' with tags {final_tags} and host '{final_host}'")

        try:
            resp = requests.post(url, files=files, data=data)
        finally:
            for f in files.values():
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
# FastAPI App for Railway
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
def shows_metadata():
    uploader = MixcloudUploader(auth, ".", "shows.csv")
    return uploader.metadata

@app.post("/upload")
async def upload_to_mixcloud(
    file: UploadFile,
    title: str = Form(...),
    host: str = Form(""),
    tags: str = Form(""),
    day: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
):
    os.makedirs("uploads", exist_ok=True)
    temp_path = Path("uploads") / file.filename
    with open(temp_path, "wb") as f:
        f.write(await file.read())

    date_str = f"{day}-{month}-{year}".strip("-")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    uploader = MixcloudUploader(auth, ".", "shows.csv")
    success = uploader.upload(str(temp_path), title, host, tag_list, date_str)

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

    uvicorn.run("mixcloud_backend:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
else:
    auth = MixcloudAuth(
        client_id=os.getenv("MIXCLOUD_CLIENT_ID"),
        client_secret=os.getenv("MIXCLOUD_CLIENT_SECRET"),
        redirect_uri=os.getenv("REDIRECT_URI", "http://localhost:8080/callback"),
    )
