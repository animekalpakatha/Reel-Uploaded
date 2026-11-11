import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "posts.json"
VIDEOS = ROOT / "videos"
RELEASE_TAG = "videos"
GRAPH = "https://graph.facebook.com/v23.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v23.0"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def log(msg):
    print(f"{datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC  {msg}", flush=True)


def load_queue():
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save_queue(q):
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def release_assets():
    out = subprocess.run(["gh", "release", "view", RELEASE_TAG, "--json", "assets", "-q", ".assets[].name"],
                         check=True, capture_output=True, text=True).stdout
    return set(out.split())


def download(name):
    VIDEOS.mkdir(exist_ok=True)
    path = VIDEOS / name
    if not path.exists():
        subprocess.run(["gh", "release", "download", RELEASE_TAG, "-p", name, "-D", str(VIDEOS)], check=True)
    return path


def yt_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(json.loads(os.environ["YT_TOKEN"]), YT_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def post_youtube(video, meta):
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": "1",
        },
        "status": {"privacyStatus": meta.get("privacy", "public")},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt_service().videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"], resp.get("status", {}).get("privacyStatus", "?")


def check(r):
    if r.status_code >= 400:
        raise RuntimeError(f"Instagram {r.status_code}: {r.text[:500]}")
    return r.json()


def post_instagram(video, meta):
    ig_id, token = os.environ["IG_USER_ID"], os.environ["IG_TOKEN"]
    c = check(requests.post(f"{GRAPH}/{ig_id}/media", data={
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": meta["caption"],
        "share_to_feed": "true",
        "access_token": token,
    }, timeout=60))
    cid = c["id"]
    with open(video, "rb") as f:
        check(requests.post(f"{RUPLOAD}/{cid}", headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(video.stat().st_size),
        }, data=f, timeout=1800))
    for _ in range(90):
        s = check(requests.get(f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": token}, timeout=60))
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {s}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")
    p = check(requests.post(f"{GRAPH}/{ig_id}/media_publish", data={"creation_id": cid, "access_token": token}, timeout=60))
    return p["id"]


def run_check():
    ok = True
    try:
        yt_service()
        log("CHECK YouTube login: OK")
    except Exception as e:
        ok = False
        log(f"CHECK YouTube login: FAILED - {e}")
    try:
        r = check(requests.get(f"{GRAPH}/{os.environ['IG_USER_ID']}",
                               params={"fields": "username", "access_token": os.environ["IG_TOKEN"]}, timeout=60))
        log(f"CHECK Instagram: OK (@{r.get('username')})")
    except Exception as e:
        ok = False
        log(f"CHECK Instagram: FAILED - {e}")
    try:
        assets = release_assets()
    except Exception as e:
        assets = set()
        ok = False
        log(f"CHECK release '{RELEASE_TAG}': FAILED - {e}")
    for i in load_queue()["queue"]:
        if i.get("done"):
            continue
        found = i["file"] in assets
        ok = ok and found
        log(f"CHECK video: {i['file']} - {'found' if found else 'MISSING in release'}")
    log("CHECK result: " + ("ALL OK" if ok else "PROBLEMS FOUND"))
    return 0 if ok else 1


def main():
    if "--check" in sys.argv:
        return run_check()

    q = load_queue()
    item = next((i for i in q["queue"] if not i.get("done")), None)
    if not item:
        log("Queue empty - nothing to post.")
        return 0
    video = download(item["file"])

    ok = True
    if not item.get("youtube_id"):
        try:
            vid, privacy = post_youtube(video, item["youtube"])
            item["youtube_id"] = vid
            save_queue(q)
            log(f"YouTube OK: {item['file']} -> https://youtube.com/shorts/{vid} ({privacy})")
        except Exception as e:
            ok = False
            log(f"YouTube FAILED: {item['file']}: {e}")
    if not item.get("instagram_id"):
        try:
            item["instagram_id"] = post_instagram(video, item["instagram"])
            save_queue(q)
            log(f"Instagram OK: {item['file']} -> media {item['instagram_id']}")
        except Exception as e:
            ok = False
            log(f"Instagram FAILED: {item['file']}: {e}")

    if ok:
        item["done"] = True
        item["posted_at"] = f"{datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC"
        save_queue(q)
        log(f"Done: {item['file']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
