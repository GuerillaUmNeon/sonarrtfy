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
completed_seasons = set()
buffer_lock = threading.Lock()
notified_complete = {}  # dict[str, float]
COMPLETE_DEDUP_TTL = int(os.getenv("COMPLETE_DEDUP_TTL", "86400"))

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

    try:
        resp = requests.post(
            NTFY_URL,
            headers=headers,
            data=message.encode("utf-8"),
            timeout=10,
        )
        print(f"ntfy: {resp.status_code} | {title[:60]}")

        if resp.status_code != 200:
            try:
                print(f"ntfy response body: {resp.text}")
            except Exception:
                pass

        return resp.status_code == 200
    except Exception as e:
        print(f"ntfy error: {e}")
        return False


def find_season_poster(series, season_num):
    root_folder = series.get("path")
    if not root_folder:
        return None

    candidates = [
        f"season-{season_num}.jpg",
        f"season-{season_num}.png",
        f"season{season_num}.jpg",
        f"season{season_num}.png",
        f"season-{season_num:02d}.jpg",
        f"season-{season_num:02d}.png",
        f"season{season_num:02d}.jpg",
        f"season{season_num:02d}.png",
        os.path.join(f"Season {season_num}", "season.jpg"),
        os.path.join(f"Season {season_num}", "season.png"),
        os.path.join(f"Season {season_num:02d}", "season.jpg"),
        os.path.join(f"Season {season_num:02d}", "season.png"),
    ]

    for rel_path in candidates:
        full_path = os.path.join(root_folder, rel_path)
        if os.path.exists(full_path):
            return full_path

    return None


def build_notification(events, key, is_full_season=False, total_eps=0):
    series = events[0].get("series", {})
    series_title = series.get("title", "Unknown")
    season_num = int(key.split(":")[1])

    ep_list = extract_episode_numbers(events)
    if not ep_list:
        raise ValueError(f"No episode numbers found for {key}")

    if is_full_season and total_eps > 0:
        title = f"{series_title} - Season {season_num:02d} Complete"
        message = f"Episodes 01-{total_eps:02d}"
    else:
        title = f"{series_title} - Season {season_num:02d} Downloaded"
        message = "Episode " + " + ".join(f"{num:02d}" for num in ep_list)

    poster_url = None
    local_season_poster = find_season_poster(series, season_num)
    if local_season_poster:
        print(f"Local season poster found but unavailable to ntfy Attach: {local_season_poster}")

    for img_type in ["poster", "banner", "fanart"]:
        img = next(
            (i for i in series.get("images", []) if i.get("coverType") == img_type),
            None,
        )
        if img:
            poster_url = img.get("remoteUrl") or img.get("url")
            break

    slug = series.get("titleSlug") or slugify_title(series_title)
    click_url = f"{SONARR_LINK}/series/{slug}" if SONARR_LINK else ""

    return title, message, click_url, poster_url


def flush_season(key, events_override=None, is_full_season=False, total_eps=0):
    try:
        with buffer_lock:
            events = list(events_override) if events_override is not None else load_events_for_key(key)

            if not events:
                clear_state_for_key(key)
                print(f"Empty buffer for {key}")
                return

            clear_state_for_key(key)

        series = events[0].get("series", {})
        series_id = series.get("id")
        season_num = int(key.split(":")[1])
        complete_key = f"{series_id}:{season_num}"

        is_complete = is_full_season and total_eps > 0

        if is_complete:
            now = datetime.now(TIMEZONE).timestamp()
            prev = notified_complete.get(complete_key)

            # If we notified recently (within TTL), skip
            if prev is not None and (now - prev) < COMPLETE_DEDUP_TTL:
                print(f"Skipping duplicate 'Complete' notification for {complete_key} (within TTL)")
                return

        title, message, click_url, poster_url = build_notification(
            events,
            key,
            is_full_season=is_full_season,
            total_eps=total_eps,
        )

        ok = send_ntfy_curl_style(title, message, click_url, poster_url, "tv")

        if ok and is_complete:
            notified_complete[complete_key] = datetime.now(TIMEZONE).timestamp()

    except Exception as e:
        print(f"flush_season({key}) error: {e}")
        import traceback
        traceback.print_exc()


def schedule_flush(key, delay_seconds):
    old_timer = timers.pop(key, None)
    if old_timer:
        try:
            old_timer.cancel()
        except Exception:
            pass

    timer = threading.Timer(delay_seconds, flush_season, [key])
    timer.daemon = True
    timers[key] = timer
    timer.start()


def cleanup_expired_dedup():
    now = datetime.now(TIMEZONE).timestamp()
    with buffer_lock:
        expired = [k for k, ts in notified_complete.items() if (now - ts) >= COMPLETE_DEDUP_TTL]
        for k in expired:
            notified_complete.pop(k, None)
    if expired:
        print(f"Cleaned up {len(expired)} expired complete-notification entries")

@app.route("/sonarr-webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"error": "No JSON"}), 400

        series = payload.get("series", {})
        series_id = series.get("id")
        if not series_id:
            return jsonify({"error": "No series ID"}), 400

        episodes = payload.get("episodes", [])
        season = episodes[0].get("seasonNumber", 0) if episodes else 0
        if not isinstance(season, int) or season < 0:
            return jsonify({"error": "Invalid season number"}), 400

        key = f"{series_id}:{season}"
        event_type = payload.get("eventType", "")

        flush_now = False
        flush_events = None
        full_season = False
        last_released = False
        total_eps = 0
        ep_list = []
        scheduled_for_0830 = False

        with buffer_lock:
            existing_events = load_events_for_key(key)
            existing_events.append(payload)

            ep_list = extract_episode_numbers(existing_events)
            series_data = get_series(series_id)
            total_eps = get_season_total_eps(series_id, season, series_data)

            expected_full_list = list(range(1, total_eps + 1)) if total_eps > 0 else []
            full_season = total_eps > 0 and ep_list == expected_full_list

            # If this payload/buffer includes Sonarr's latest released episode,
            # send immediately. This applies even during the 01:00-08:30 window.
            last_released = is_last_released_episode(series_id, existing_events)

            print(
                f"Buffered {key}: episodes={len(ep_list)} total_eps={total_eps} "
                f"full_season={full_season} last_released={last_released}"
            )

            if full_season or last_released:
                old_timer = timers.pop(key, None)
                if old_timer:
                    try:
                        old_timer.cancel()
                    except Exception:
                        pass

                flush_now = True
                flush_events = list(existing_events)
                clear_state_for_key(key)

                if full_season:
                    completed_seasons.add(key)
            else:
                save_events_for_key(key, existing_events)

                if in_quiet_window():
                    delay_seconds = seconds_until_0830()
                    scheduled_for_0830 = True
                    print(f"Quiet window: scheduling {key} for 08:30 Amsterdam time")
                else:
                    delay_seconds = BUFFER_TIMEOUT
                    print(f"Scheduling {key} in {BUFFER_TIMEOUT} seconds")

                schedule_flush(key, delay_seconds)

        if flush_now:
            flush_season(
                key,
                events_override=flush_events,
                is_full_season=full_season,
                total_eps=total_eps,
            )

        return jsonify(
            {
                "status": "ok",
                "key": key,
                "buffered": len(ep_list),
                "total_eps": total_eps,
                "event_type": event_type,
                "flush_now": flush_now,
                "full_season": full_season,
                "last_released_episode": last_released,
                "scheduled_for_0830": scheduled_for_0830,
            }
        ), 200

    except Exception as e:
        print(f"Webhook ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    with buffer_lock:
        active_buffers = len(season_buffer)
        active_timers = len(timers)

    return jsonify(
        {
            "status": "ok",
            "buffers": active_buffers,
            "timers": active_timers,
            "sonarr_api_loaded": bool(SONARR_API),
            "ntfy_token_loaded": bool(NTFY_TOKEN),
            "timezone": str(TIMEZONE),
        }
    )


if __name__ == "__main__":
    print(f"SONARR_URL={SONARR_URL}")
    print(f"SONARR_API loaded={bool(SONARR_API)}")
    print(f"NTFY_URL={NTFY_URL}")
    print(f"NTFY_TOKEN loaded={bool(NTFY_TOKEN)}")
    print(f"TIMEZONE={TIMEZONE}")

    success = send_ntfy_curl_style(
        title="Sonarr Season Webhook started",
        message="Application is up and listening for Sonarr webhook events.",
        click_url=f"http://{HOST}:{PORT}",
        tags="tv,system",
    )
    print("Startup notification sent" if success else "Failed to send startup notification")


    def periodic_cleanup():
        while True:
            time.sleep(3600)  # every hour
            try:
                cleanup_expired_dedup()
            except Exception:
                import traceback
                traceback.print_exc()


    cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
    cleanup_thread.start()

    app.run(host=HOST, port=PORT, debug=False)
