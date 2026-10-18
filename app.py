from flask import Flask, request, jsonify
import threading
import requests
import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


def slugify_title(title):
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


app = Flask(__name__)

SONARR_URL = os.getenv("SONARR_URL", "").rstrip("/")
SONARR_API = os.getenv("SONARR_API", "")
SONARR_HEADERS = {"X-Api-Key": SONARR_API} if SONARR_API else {}
SONARR_LINK = os.getenv("SONARR_LINK", "").rstrip("/")

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
base_ntfy_url = os.getenv("NTFY_URL", "").rstrip("/")
NTFY_URL = f"{base_ntfy_url}/{NTFY_TOPIC}" if base_ntfy_url and NTFY_TOPIC else ""
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()

BUFFER_TIMEOUT = int(os.getenv("BUFFER_TIMEOUT", "600"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))
QUIET_START = time(1, 0)
QUIET_END = time(8, 30)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

season_buffer = {}
timers = {}
buffer_lock = threading.Lock()

# Fingerprint -> timestamp. Prevents duplicate sends when a Timer callback
# races the webhook path for the same (series, season, episodes, full_season).
notified_complete = {}  # dict[tuple, float]
COMPLETE_DEDUP_TTL = int(os.getenv("COMPLETE_DEDUP_TTL", "86400"))

# Per-episode dedup for "last episode of the season" notifications.
notified_last_episode = {}  # dict[str, float]
LAST_EP_DEDUP_TTL = int(os.getenv("LAST_EP_DEDUP_TTL", "300"))


def now_local():
    return datetime.now(TIMEZONE)


def in_quiet_window():
    current = now_local().time()
    return QUIET_START <= current < QUIET_END


def seconds_until_0830():
    now = now_local()
    target = now.replace(hour=8, minute=30, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0, (target - now).total_seconds())


def get_series(series_id):
    if not series_id or not SONARR_URL:
        return None

    try:
        resp = requests.get(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers=SONARR_HEADERS,
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"API error loading series {series_id}: {e}")
        return None


def get_season_total_eps(series_id, season_num, series_data=None):
    data = series_data or get_series(series_id)
    if not data:
        return 0

    season_data = next(
        (s for s in data.get("seasons", []) if s.get("seasonNumber") == season_num),
        {},
    )

    total = (
        season_data.get("statistics", {}).get("episodeCount")
        or season_data.get("episodeCount")
        or season_data.get("totalEpisodeCount")
        or 0
    )

    try:
        return int(total)
    except (TypeError, ValueError):
        return 0


def get_last_released_episode(series_id):
    """Return the latest released standard episode according to Sonarr.

    The /episode endpoint provides airDateUtc. Episodes without an air date or
    whose air date is in the future are ignored. Specials (season 0) are also
    ignored, because they should not make a regular-season episode "the last".
    """
    if not series_id or not SONARR_URL:
        return None

    try:
        resp = requests.get(
            f"{SONARR_URL}/api/v3/episode",
            params={"seriesId": series_id},
            headers=SONARR_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        episodes = resp.json()
    except Exception as e:
        print(f"API error loading episodes for series {series_id}: {e}")
        return None

    now_utc = datetime.now(ZoneInfo("UTC"))
    released = []

    for episode in episodes:
        season_num = episode.get("seasonNumber")
        episode_num = episode.get("episodeNumber")
        air_date_utc = episode.get("airDateUtc")

        if not isinstance(season_num, int) or season_num <= 0:
            continue
        if not isinstance(episode_num, int) or episode_num <= 0:
            continue
        if not air_date_utc:
            continue

        try:
            air_dt = datetime.fromisoformat(air_date_utc.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue

        if air_dt <= now_utc:
            released.append((air_dt, season_num, episode_num))

    if not released:
        return None

    # Air date is primary. Season/episode makes ties deterministic.
    _, season_num, episode_num = max(released, key=lambda x: (x[0], x[1], x[2]))
    return season_num, episode_num


def event_contains_episode(events, season_num, episode_num):
    for event in events:
        for ep in event.get("episodes", []):
            if ep.get("seasonNumber") != season_num:
                continue

            if ep.get("episodeNumber") == episode_num:
                return True

            episode_numbers = ep.get("episodeNumbers", [])
            if isinstance(episode_numbers, list) and episode_num in episode_numbers:
                return True

    return False


def is_last_released_episode(series_id, events):
    last_episode = get_last_released_episode(series_id)
    if not last_episode:
        return False

    season_num, episode_num = last_episode
    return event_contains_episode(events, season_num, episode_num)


def extract_episode_numbers(events):
    nums = set()

    for event in events:
        for ep in event.get("episodes", []):
            ep_num = ep.get("episodeNumber")
            if isinstance(ep_num, int) and 1 <= ep_num <= 999:
                nums.add(ep_num)

            ep_nums = ep.get("episodeNumbers", [])
            if isinstance(ep_nums, list):
                for n in ep_nums:
                    if isinstance(n, int) and 1 <= n <= 999:
                        nums.add(n)

    return sorted(nums)


def load_events_for_key(key):
    return list(season_buffer.get(key, []))


def save_events_for_key(key, events):
    season_buffer[key] = list(events)


def clear_state_for_key(key):
    season_buffer.pop(key, None)
    timer = timers.pop(key, None)
    if timer:
        try:
            timer.cancel()
        except Exception:
            pass


def send_ntfy_curl_style(title, message, click_url, poster_url=None, tags="tv"):
    if not NTFY_URL:
        print("ntfy URL not configured")
        return False

    headers = {
        "Title": title,
        "Click": click_url,
        "Tags": tags,
        "Content-Type": "text/plain",
    }

    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    if poster_url:
        headers["Attach"] = poster_url