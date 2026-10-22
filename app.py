from flask import Flask, request, jsonify
import threading
import requests
import os
import re
import time
import logging
import traceback
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
)
log = logging.getLogger("sonarrtfy")


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
QUIET_START = dtime(1, 0)
QUIET_END = dtime(8, 30)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

# Sonarr event types:
# - "Download": file imported or upgraded
# - "Grab": release sent to download client
# - "Rename": files renamed
# We only want actual imported/upgraded files.
ALLOWED_EVENT_TYPES = {"Download"}

season_buffer = {}
timers = {}
completed_seasons = set()
buffer_lock = threading.RLock()

# One dedup store for every notification type.
notified_episodes = {}
EPISODE_DEDUP_TTL = int(os.getenv("EPISODE_DEDUP_TTL", "86400"))


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
        log.warning("API error loading series %s: %s", series_id, e)
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
        log.warning("API error loading episodes for series %s: %s", series_id, e)
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
    """Caller must hold buffer_lock."""
    season_buffer.pop(key, None)

    timer = timers.pop(key, None)

    if timer:
        try:
            timer.cancel()
        except Exception:
            pass


def should_notify_episodes(series_id, season_num, ep_list):
    """Return only episode numbers that have not been notified within the TTL."""
    if not series_id:
        return ep_list

    now_ts = datetime.now(TIMEZONE).timestamp()
    fresh_episodes = []

    for ep_num in ep_list:
        dedup_key = f"{series_id}:{season_num}:{ep_num}"
        previous = notified_episodes.get(dedup_key)

        if previous is None or (now_ts - previous) >= EPISODE_DEDUP_TTL:
            fresh_episodes.append(ep_num)

    return fresh_episodes


def mark_episodes_notified(series_id, season_num, ep_list):
    if not series_id:
        return

    now_ts = datetime.now(TIMEZONE).timestamp()

    for ep_num in ep_list:
        dedup_key = f"{series_id}:{season_num}:{ep_num}"
        notified_episodes[dedup_key] = now_ts


def send_ntfy_curl_style(title, message, click_url, poster_url=None, tags="tv"):
    if not NTFY_URL:
        log.warning("ntfy URL not configured")
        return False

    headers = {
        "Title": title,
        "Tags": tags,
        "Content-Type": "text/plain",
    }

    if click_url:
        headers["Click"] = click_url

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

        log.info("ntfy: %s | %s", resp.status_code, title[:60])

        if resp.status_code != 200:
            log.warning("ntfy response body: %s", resp.text[:300])

        return resp.status_code == 200
    except Exception as e:
        log.error("ntfy error: %s", e)
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
        log.info(
            "Local season poster found but unavailable to ntfy Attach: %s",
            local_season_poster,
        )

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


def flush_season(
    key,
    events_override=None,
    is_full_season=False,
    total_eps=0,
    flush_reason="buffer",
):
    try:
        with buffer_lock:
            events = (
                list(events_override)
                if events_override is not None
                else load_events_for_key(key)
            )

            if not events:
                clear_state_for_key(key)
                log.info("Empty buffer for %s", key)
                return

            clear_state_for_key(key)

        series = events[0].get("series", {})
        series_id = series.get("id")
        season_num = int(key.split(":")[1])
        ep_list = extract_episode_numbers(events)

        if not ep_list:
            log.warning("No episode numbers found for %s; skipping notification", key)
            return

        fresh_episodes = should_notify_episodes(series_id, season_num, ep_list)

        if not fresh_episodes:
            log.info(
                "Skipping duplicate notification for %s; episodes already notified: %s",
                key,
                ep_list,
            )
            return

        title, message, click_url, poster_url = build_notification(
            events,
            key,
            is_full_season=is_full_season,
            total_eps=total_eps,
        )

        ok = send_ntfy_curl_style(title, message, click_url, poster_url, "tv")

        if ok:
            mark_episodes_notified(series_id, season_num, ep_list)
            log.info(
                "Notified %s: reason=%s episodes=%s",
                key,
                flush_reason,
                ep_list,
            )
        else:
            log.warning("Failed to send notification for %s", key)

    except Exception as e:
        log.error("flush_season(%s) error: %s", key, e)
        log.error(traceback.format_exc())


def schedule_flush(key, delay_seconds):
    old_timer = timers.pop(key, None)

    if old_timer:
        try:
            old_timer.cancel()
        except Exception:
            pass

    timer = threading.Timer(
        delay_seconds,
        flush_season,
        [key],
        kwargs={"flush_reason": "buffer"},
    )
    timer.daemon = True
    timers[key] = timer
    timer.start()


def cleanup_expired_episode_dedup():
    now = datetime.now(TIMEZONE).timestamp()

    with buffer_lock:
        expired = [
            k
            for k, ts in notified_episodes.items()
            if (now - ts) >= EPISODE_DEDUP_TTL
        ]

        for k in expired:
            notified_episodes.pop(k, None)

    if expired:
        log.info("Cleaned up %d expired episode dedup entries", len(expired))


@app.route("/sonarr-webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(silent=True)

        if not payload:
            return jsonify({"error": "No JSON"}), 400

        event_type = payload.get("eventType", "")

        if event_type not in ALLOWED_EVENT_TYPES:
            log.info("Ignoring Sonarr event type: %s", event_type)
            return jsonify(
                {
                    "status": "ignored",
                    "event_type": event_type,
                    "reason": "event type not enabled",
                }
            ), 200

        series = payload.get("series", {})
        series_id = series.get("id")

        if not series_id:
            return jsonify({"error": "No series ID"}), 400

        episodes = payload.get("episodes", [])
        season = episodes[0].get("seasonNumber", 0) if episodes else 0

        if not isinstance(season, int) or season < 0:
            return jsonify({"error": "Invalid season number"}), 400

        key = f"{series_id}:{season}"

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

            if not ep_list:
                log.warning("Webhook contained no usable episode numbers for %s", key)
                return jsonify(
                    {
                        "status": "ignored",
                        "key": key,
                        "reason": "no episode numbers",
                    }
                ), 200

            series_data = get_series(series_id)
            total_eps = get_season_total_eps(series_id, season, series_data)

            expected_full_list = (
                list(range(1, total_eps + 1)) if total_eps > 0 else []
            )
            full_season = total_eps > 0 and ep_list == expected_full_list

            # If this payload/buffer includes Sonarr's latest released episode,
            # send immediately, even during the quiet window.
            last_released = is_last_released_episode(series_id, existing_events)

            log.info(
                "Buffered %s: episodes=%s total_eps=%s full_season=%s "
                "last_released=%s event_type=%s",
                key,
                ep_list,
                total_eps,
                full_season,
                last_released,
                event_type,
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
                    log.info(
                        "Quiet window: scheduling %s for 08:30 Amsterdam time",
                        key,
                    )
                else:
                    delay_seconds = BUFFER_TIMEOUT
                    log.info("Scheduling %s in %s seconds", key, BUFFER_TIMEOUT)

                schedule_flush(key, delay_seconds)

        if flush_now:
            reason = "full_season" if full_season else "last_episode"

            flush_season(
                key,
                events_override=flush_events,
                is_full_season=full_season,
                total_eps=total_eps,
                flush_reason=reason,
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
        log.error("Webhook ERROR: %s", e)
        log.error(traceback.format_exc())
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
            "episode_dedup_ttl": EPISODE_DEDUP_TTL,
            "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
        }
    )


if __name__ == "__main__":
    log.info("SONARR_URL=%s", SONARR_URL)
    log.info("SONARR_API loaded=%s", bool(SONARR_API))
    log.info("NTFY_URL=%s", NTFY_URL)
    log.info("NTFY_TOKEN loaded=%s", bool(NTFY_TOKEN))
    log.info("TIMEZONE=%s", TIMEZONE)
    log.info("Allowed Sonarr event types=%s", sorted(ALLOWED_EVENT_TYPES))
    log.info("Episode dedup TTL=%s seconds", EPISODE_DEDUP_TTL)

    success = send_ntfy_curl_style(
        title="Sonarr Season Webhook started",
        message="Application is up and listening for Sonarr webhook events.",
        click_url=f"http://{HOST}:{PORT}",
        tags="tv,system",
    )

    log.info(
        "Startup notification sent" if success else "Failed to send startup notification"
    )

    def periodic_cleanup():
        while True:
            time.sleep(3600)

            try:
                cleanup_expired_episode_dedup()
            except Exception:
                log.error(traceback.format_exc())

    cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
    cleanup_thread.start()

    app.run(host=HOST, port=PORT, debug=False)