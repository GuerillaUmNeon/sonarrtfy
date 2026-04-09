from flask import Flask, request, jsonify
import json
import threading
import requests
import os
import re
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

SONARR_LINK = os.getenv("SONARR_LINK", "")

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
base_ntfy_url = os.getenv("NTFY_URL", "").rstrip("/")
NTFY_URL = f"{base_ntfy_url}/{NTFY_TOPIC}" if base_ntfy_url and NTFY_TOPIC else ""

BUFFER_TIMEOUT = int(os.getenv("BUFFER_TIMEOUT", "600"))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

season_buffer = {}
timers = {}
buffer_lock = threading.Lock()


def get_season_total_eps(series_id, season_num):
    if not series_id or not SONARR_URL:
        return 0

    url = f"{SONARR_URL}/api/v3/series/{series_id}"

    try:
        resp = requests.get(url, headers=SONARR_HEADERS, timeout=5)
        resp.raise_for_status()
        data = resp.json()

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

        return int(total)

    except Exception as e:
        print(f"API fallback for {series_id} S{season_num}: {e}")
        return 0


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
        print("❌ ntfy URL not configured")
        return False

    headers = {
        "Title": title,
        "Click": click_url,
        "Tags": tags,
        "Content-Type": "text/plain",
    }

    if poster_url:
        headers["Attach"] = poster_url

    try:
        resp = requests.post(
            NTFY_URL,
            headers=headers,
            data=message.encode("utf-8"),
            timeout=10,
        )
        print(f"✅ ntfy: {resp.status_code} | {title[:60]}")
        return resp.status_code == 200
    except Exception as e:
        print(f"❌ ntfy: {e}")
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
        print(
            f"DEBUG local season poster found but not usable via Attach header: {local_season_poster}"
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
    click_url = f"{SONARR_LINK}/series/{slug}"

    return title, message, click_url, poster_url


def flush_season(key, events_override=None, is_full_season=False, total_eps=0):
    try:
        with buffer_lock:
            events = list(events_override) if events_override is not None else load_events_for_key(key)

            if not events:
                clear_state_for_key(key)
                print(f"❌ Empty buffer for {key}")
                return

            clear_state_for_key(key)

        title, message, click_url, poster_url = build_notification(
            events,
            key,
            is_full_season=is_full_season,
            total_eps=total_eps,
        )

        send_ntfy_curl_style(title, message, click_url, poster_url, "tv")

    except Exception as e:
        print(f"❌ flush_season({key}) error: {e}")
        import traceback
        traceback.print_exc()


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
        key = f"{series_id}:{season}"
        event_type = payload.get("eventType", "")

        flush_now = False
        flush_events = None
        total_eps = 0
        ep_list = []

        with buffer_lock:
            existing_events = load_events_for_key(key)
            existing_events.append(payload)

            ep_list = extract_episode_numbers(existing_events)
            total_eps = get_season_total_eps(series_id, season)

            expected_full_list = list(range(1, total_eps + 1)) if total_eps > 0 else []
            is_full_season = total_eps > 0 and ep_list == expected_full_list

            print(f"⏳ Debounced {key}: buffered={len(ep_list)} total_eps={total_eps}")

            if is_full_season:
                flush_now = True
                flush_events = list(existing_events)
                clear_state_for_key(key)
            else:
                old_timer = timers.pop(key, None)
                if old_timer:
                    try:
                        old_timer.cancel()
                    except Exception:
                        pass

                save_events_for_key(key, existing_events)

                t = threading.Timer(BUFFER_TIMEOUT, flush_season, [key])
                t.daemon = True
                timers[key] = t
                t.start()

        if flush_now:
            flush_season(
                key,
                flush_events,
                is_full_season=True,
                total_eps=total_eps,
            )

        return jsonify({
            "status": "ok",
            "key": key,
            "buffered": len(ep_list),
            "total_eps": total_eps,
            "event_type": event_type,
            "flush_now": flush_now,
        }), 200

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

    return jsonify({
        "status": "ok",
        "buffers": active_buffers,
        "timers": active_timers,
        "sonarr_api_loaded": bool(SONARR_API),
    })


if __name__ == "__main__":
    print(f"SONARR_URL={SONARR_URL}")
    print(f"SONARR_API loaded={bool(SONARR_API)}")

    app.run(host=HOST, port=PORT, debug=False)

    # Startup notification to ntfy (runs just after app starts)
    send_ntfy_curl_style(
        title="🚀 Sonarr Season Webhook started",
        message="Application is up and listening for Sonarr webhook events.",
        click_url="http://{}:{}".format(HOST, PORT),
        tags="tv,system",
    )