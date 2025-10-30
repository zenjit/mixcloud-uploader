import os
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from tempfile import NamedTemporaryFile
import json
import csv
from fastapi.responses import JSONResponse

from mixcloud_backend import MixcloudAuth, MixcloudUploader

# Load config
CONFIG_FILE = "config.json"
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

CLIENT_ID = config["client_id"]
CLIENT_SECRET = config["client_secret"]
REDIRECT_URI = config["redirect_uri"]
TOKEN_FILE = config.get("token_file", "token.txt")
SHOWS_FOLDER = config.get("shows_folder", "shows")
METADATA_FILE = config.get("metadata_file", "shows.csv")

auth = MixcloudAuth(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, TOKEN_FILE)
uploader = MixcloudUploader(auth, SHOWS_FOLDER, METADATA_FILE)

app = FastAPI(title="Mixcloud Uploader API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/shows_metadata")
def shows_metadata():
    data = {}
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                show_name = row.get("show", "").strip()
                if show_name:
                    tags = [t.strip() for t in row.get("tags", "").split(";") if t.strip()]
                    host = row.get("host", "").strip()
                    data[show_name] = {"tags": tags, "host": host}
    return JSONResponse(data)

@app.post("/upload")
async def upload_show(
    file: UploadFile,
    title: str = Form(...),
    tags: str = Form(""),
    host: str = Form(""),
    day: str = Form(""),
    month: str = Form(""),
    year: str = Form("")
):
    # Save temporary MP3
    with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    # Build date string
    date_str = None
    if day and month and year:
        date_str = f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    # Prepare picture if exists
    picture_path = os.path.join(SHOWS_FOLDER, title, "picture.jpg")
    uploader.upload_picture_override = picture_path if os.path.exists(picture_path) else None

    try:
        success = uploader.upload(
            tmp_path,
            title=title,
            host=host,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            date_str=date_str
        )
        return {"success": success}
    finally:
        # Cleanup
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        for attr in ["upload_name_override", "upload_description_override", "upload_picture_override"]:
            setattr(uploader, attr, None)
