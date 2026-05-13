import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta  # NEW

import requests
from dotenv import load_dotenv

# Load .env next to this file (robust against IDE working dir)
load_dotenv(Path(__file__).with_name(".env"))

# --- toggle from .env
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

def pick(name: str, default: str = "") -> str:
    """
    Prefer TEST_<name> when TEST_MODE=true and it's present; else use <name>.
    Examples:
      - MENTION_ROLE_ID -> TEST_MENTION_ROLE_ID
      - MENTION_TEXT    -> TEST_MENTION_TEXT
      - STATE_FILE      -> TEST_STATE_FILE
      - DISCORD_WEBHOOK_URL -> TEST_DISCORD_WEBHOOK_URL
      - CHANNEL_ID/HANDLE -> TEST_CHANNEL_ID/TEST_CHANNEL_HANDLE
    """
    if TEST_MODE:
        tv = os.getenv(f"TEST_{name}")
        if tv not in (None, ""):
            return tv
    return os.getenv(name, default)

API_KEY = (os.getenv("YOUTUBE_API_KEY") or "").strip()

# Use TEST_* when TEST_MODE=true
WEBHOOK_URL = (pick("DISCORD_WEBHOOK_URL") or "").strip()
CHANNEL_ID_ENV = (pick("CHANNEL_ID", "") or "").strip()
CHANNEL_HANDLE = (pick("CHANNEL_HANDLE", "") or "").strip().lstrip("@")
STATE_FILE = pick("STATE_FILE", "posted_videos.json")  # honors TEST_STATE_FILE

# Filters
SHORT_MAX_SECONDS = int(os.getenv("SHORT_MAX_SECONDS", "180"))  # exclude videos <= this length
INCLUDE_LIVE = os.getenv("INCLUDE_LIVE", "false").lower() == "true"

# Skip posting if video is older than this many minutes (but still record it to state)
MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", "60"))

def require_env():
    missing = []
    if not API_KEY:
        missing.append("YOUTUBE_API_KEY")

    # In test mode, require test webhook + test channel identifiers
    if TEST_MODE:
        if not os.getenv("TEST_DISCORD_WEBHOOK_URL"):
            missing.append("TEST_DISCORD_WEBHOOK_URL")
        if not (os.getenv("TEST_CHANNEL_ID") or os.getenv("TEST_CHANNEL_HANDLE")):
            missing.append("TEST_CHANNEL_ID or TEST_CHANNEL_HANDLE")
    else:
        if not os.getenv("DISCORD_WEBHOOK_URL"):
            missing.append("DISCORD_WEBHOOK_URL")
        if not (os.getenv("CHANNEL_ID") or os.getenv("CHANNEL_HANDLE")):
            missing.append("CHANNEL_ID or CHANNEL_HANDLE")

    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

def iso8601_duration_to_seconds(s: str) -> int:
    # e.g. 'PT1H2M3S'
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, m_, s_ = m.groups()
    h = int(h) if h else 0
    m_ = int(m_) if m_ else 0
    s_ = int(s_) if s_ else 0
    return h * 3600 + m_ * 60 + s_

def parse_rfc3339(ts: str):
    """Parse YouTube RFC3339/ISO timestamps like '2025-01-28T18:23:45Z' to aware UTC dt."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_state(id_set: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(id_set)), f)

def resolve_channel_id() -> str:
    """Use CHANNEL_ID if provided; else resolve from handle via channels.list(forHandle=...)."""
    if CHANNEL_ID_ENV.startswith("UC") and len(CHANNEL_ID_ENV) >= 20:
        return CHANNEL_ID_ENV

    # Try channels.list with forHandle (best for @handles)
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "id", "forHandle": CHANNEL_HANDLE, "key": API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if items:
        return items[0]["id"]

    # Fallback: search the handle text (avoid in loops; search.list is 100 units)
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "q": CHANNEL_HANDLE, "type": "channel", "maxResults": 1, "key": API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise RuntimeError("Could not resolve channel by handle.")
    return items[0]["id"]["channelId"]

def get_uploads_playlist_id(channel_id: str) -> str:
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "contentDetails", "id": channel_id, "key": API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise RuntimeError("Channel not found.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def fetch_playlist_items(playlist_id: str, max_results: int = 25):
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key": API_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

def fetch_video_details(video_ids: list[str]) -> dict:
    """Return {videoId: details} including duration, live status."""
    out = {}
    if not video_ids:
        return out
    url = "https://www.googleapis.com/youtube/v3/videos"
    # batch up to 50 per call
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        params = {"part": "contentDetails,snippet,liveStreamingDetails", "id": ",".join(chunk), "key": API_KEY}
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        for it in r.json().get("items", []):
            vid = it["id"]
            duration_iso = it["contentDetails"].get("duration", "PT0S")
            duration_s = iso8601_duration_to_seconds(duration_iso)
            live_details = it.get("liveStreamingDetails", {})
            is_live_related = bool(live_details)  # upcoming/live/ended metadata exists
            out[vid] = {
                "duration_s": duration_s,
                "is_live_related": is_live_related,
                "title": it["snippet"].get("title", ""),
                "description": it["snippet"].get("description", ""),
                "channelTitle": it["snippet"].get("channelTitle", "YouTube Channel"),
                "publishedAt": it["snippet"].get("publishedAt"),
                "thumbnail": (
                        it["snippet"].get("thumbnails", {}).get("high", {})
                        or it["snippet"].get("thumbnails", {}).get("medium", {})
                        or {}
                ).get("url"),
            }
    return out

def post_to_discord_embed(video):
    # Mention config (TEST_MENTION_ROLE_ID/TEST_MENTION_TEXT honored automatically)
    role_id = (pick("MENTION_ROLE_ID", "") or "").strip()
    mention_text = (pick("MENTION_TEXT", "A new video is live!") or "").strip()

    # Build the embed (no announcement text inside the embed)
    embed = {
        "title": video["title"],
        "url": video["url"],
        "description": f"New upload from {video['channelTitle']}",
        "thumbnail": {"url": video["thumbnail"]} if video.get("thumbnail") else None,
        "timestamp": video["publishedAt"],
    }
    embed = {k: v for k, v in embed.items() if v is not None}

    # Mention the role in the message body (not the embed)
    content = None
    allowed = {"parse": []}
    if role_id:
        content = f"<@&{role_id}> {mention_text}"
        allowed["roles"] = [role_id]  # allow only this role to be pinged

    payload = {"content": content, "embeds": [embed], "allowed_mentions": allowed}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()

def main():
    require_env()
    posted = load_state()

    channel_id = resolve_channel_id()
    uploads_id = get_uploads_playlist_id(channel_id)

    # Fetch recent uploads (newest-first in API response)
    upload_items = fetch_playlist_items(uploads_id, max_results=25)

    # Collect video metadata from uploads list
    recent = []
    for it in upload_items:
        snip = it.get("snippet", {}) or {}
        cd = it.get("contentDetails", {}) or {}
        vid = cd.get("videoId")
        if not vid:
            continue
        recent.append({
            "videoId": vid,
            "title": snip.get("title") or "",
            "publishedAt": snip.get("publishedAt"),
            "thumbnail": (snip.get("thumbnails", {}).get("high", {}) or snip.get("thumbnails", {}).get("medium", {})).get("url"),
            "channelTitle": snip.get("channelTitle") or "YouTube Channel",
        })

    # Sort oldest → newest so we post in order
    recent.sort(key=lambda x: x.get("publishedAt") or "")

    # Always fetch details so we can apply duration + live + age filters
    details_map = fetch_video_details([r["videoId"] for r in recent])

    for item in recent:
        vid = item["videoId"]
        if vid in posted:
            continue

        det = details_map.get(vid, {})
        duration_s = det.get("duration_s", 0)
        is_live_related = det.get("is_live_related", False)
        published_at = det.get("publishedAt") or item.get("publishedAt")
        pub_dt = parse_rfc3339(published_at)

        # 1) Skip Shorts by duration (<= SHORT_MAX_SECONDS)
        if duration_s <= SHORT_MAX_SECONDS:
            posted.add(vid)   # still record it so we don't see it again
            continue

        # 2) Optionally skip livestream-related videos entirely
        if not INCLUDE_LIVE and is_live_related:
            posted.add(vid)
            continue

        # 3) NEW: Skip if older than MAX_AGE_MINUTES, but record to state
        if pub_dt is not None:
            if datetime.now(timezone.utc) - pub_dt > timedelta(minutes=MAX_AGE_MINUTES):
                # Too old → do not post, just mark as handled
                posted.add(vid)
                # Optional: uncomment for log line
                # print(f"Skipped old video (> {MAX_AGE_MINUTES}m): {det.get('title') or item.get('title')} ({vid})")
                continue

        video_url = f"https://www.youtube.com/watch?v={vid}"
        payload = {
            "id": vid,
            "title": det.get("title") or item["title"],
            "url": video_url,
            "thumbnail": det.get("thumbnail") or item.get("thumbnail"),
            "channelTitle": det.get("channelTitle") or item.get("channelTitle"),
            "publishedAt": det.get("publishedAt") or item.get("publishedAt"),
        }

        try:
            post_to_discord_embed(payload)
            posted.add(vid)
            print(f"Posted: {payload['title']}")
        except requests.HTTPError as e:
            print(f"Failed to post {vid}: {e}")

    save_state(posted)

if __name__ == "__main__":
    main()
