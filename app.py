"""
Instagram Stories & Reels Scraper — Streamlit app.

Combines the features from insta_scraper.ipynb into a single deployable app:
  - Sidebar login (with 2FA support) using instaloader, session cached to disk.
  - Target account input via CSV upload and/or manual entry.
  - "Stories" tab: download current stories for the selected accounts.
  - "Reels" tab: download the latest reel, or all reels since a given date,
    for the selected accounts (correctly handling pinned posts).
  - "On-Screen Text" tab: OCR downloaded story/reel videos to extract any
    text overlays baked into the video frames (e.g. text stickers), and
    search across extracted text.

Run with:
    streamlit run app.py
"""

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import instaloader
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Instagram Scraper", page_icon="📸", layout="wide")


# ----------------------------------------------------------------------------
# Session state
# ----------------------------------------------------------------------------

def init_session_state():
    defaults = {
        "logged_in": False,
        "loader": None,
        "username": "",
        "awaiting_2fa": False,
        "login_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_session_state()


# ----------------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------------

def attempt_login(username: str, password: str):
    st.session_state.login_error = None

    if not username:
        st.session_state.login_error = "Please enter a username."
        return

    loader = instaloader.Instaloader(save_metadata=False, quiet=True)

    try:
        loader.load_session_from_file(username)
        st.session_state.loader = loader
        st.session_state.username = username
        st.session_state.logged_in = True
        return
    except FileNotFoundError:
        pass

    if not password:
        st.session_state.login_error = "Please enter a password."
        return

    try:
        loader.login(username, password)
        loader.save_session_to_file()
        st.session_state.loader = loader
        st.session_state.username = username
        st.session_state.logged_in = True
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        st.session_state.loader = loader
        st.session_state.username = username
        st.session_state.awaiting_2fa = True
    except instaloader.exceptions.BadCredentialsException:
        st.session_state.login_error = "Invalid username or password."
    except instaloader.exceptions.ConnectionException as e:
        st.session_state.login_error = f"Connection error: {e}"
    except instaloader.exceptions.LoginException as e:
        st.session_state.login_error = (
            f"Instagram rejected the login attempt ({e}). This often happens when logging in "
            "from a cloud/datacenter IP address (e.g. Streamlit Community Cloud) that Instagram "
            "flags as suspicious. Try again in a few minutes, log in from a residential IP first "
            "to clear any checkpoint, or run this app on a host with a residential/non-datacenter "
            "IP."
        )


def submit_two_factor_code(code: str):
    loader = st.session_state.loader
    if not code:
        st.session_state.login_error = "Please enter the 2FA code."
        return
    try:
        loader.two_factor_login(code)
        loader.save_session_to_file()
        st.session_state.logged_in = True
        st.session_state.awaiting_2fa = False
        st.session_state.login_error = None
    except instaloader.exceptions.BadCredentialsException:
        st.session_state.login_error = "Invalid or expired 2FA code, try again."
    except instaloader.exceptions.LoginException as e:
        st.session_state.login_error = f"Instagram rejected the 2FA attempt: {e}"


def log_out():
    st.session_state.logged_in = False
    st.session_state.loader = None
    st.session_state.username = ""
    st.session_state.awaiting_2fa = False
    st.session_state.login_error = None


with st.sidebar:
    st.header("Instagram Login")

    if st.session_state.logged_in:
        st.success(f"Logged in as **{st.session_state.username}**")
        if st.button("Log out"):
            log_out()
            st.rerun()
    elif st.session_state.awaiting_2fa:
        st.info("Two-factor authentication required.")
        code = st.text_input("2FA code", key="tfa_code")
        if st.button("Submit code"):
            submit_two_factor_code(code)
            st.rerun()
        if st.session_state.login_error:
            st.error(st.session_state.login_error)
    else:
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Log in"):
            with st.spinner("Logging in..."):
                attempt_login(username, password)
            st.rerun()
        if st.session_state.login_error:
            st.error(st.session_state.login_error)


# ----------------------------------------------------------------------------
# Target accounts input (shared by both tabs)
# ----------------------------------------------------------------------------

def get_target_accounts(key_prefix: str) -> list[str]:
    uploaded = st.file_uploader(
        "Upload a CSV with a 'username' column",
        type=["csv"],
        key=f"{key_prefix}_csv",
    )
    manual = st.text_area(
        "Or enter usernames (one per line, or comma-separated)",
        key=f"{key_prefix}_manual",
        placeholder="chellemakesfood\nwoah.my",
    )

    accounts: list[str] = []

    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded)
        except Exception as e:
            st.error(f"Could not read CSV: {e}")
            df = None
        if df is not None:
            if "username" not in df.columns:
                st.error(f"CSV has no 'username' column. Found columns: {list(df.columns)}")
            else:
                accounts.extend(df["username"].dropna().astype(str).str.strip().tolist())

    if manual:
        for line in manual.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                accounts.append(line)

    # De-duplicate while preserving order.
    seen = set()
    result = []
    for account in accounts:
        if account and account not in seen:
            seen.add(account)
            result.append(account)

    return result


# ----------------------------------------------------------------------------
# Profile resolution
# ----------------------------------------------------------------------------

def resolve_profile(loader: instaloader.Instaloader, username: str) -> instaloader.Profile:
    """
    Resolve a Profile from a username.

    Instagram's `web_profile_info` endpoint (used by `Profile.from_username`) currently
    400s for some business/creator accounts with an Instagram-side error like:
      "Asset asset://laser.provider/ig_business_category_subvertical has been deleted.
       You cannot use this schema"
    This is a known, unresolved bug on Instagram's end (see instaloader issues/PRs
    around web_profile_info), not something instaloader or this app can prevent outright.

    As a workaround, when that happens we fall back to Instagram's "top search" endpoint
    (instaloader.TopSearchResults), which uses a different response schema and isn't
    affected, to resolve the username to a Profile.
    """
    try:
        return instaloader.Profile.from_username(loader.context, username)
    except instaloader.exceptions.QueryReturnedBadRequestException:
        pass

    results = instaloader.TopSearchResults(loader.context, username)
    for profile in results.get_profiles():
        if profile.username.lower() == username.lower():
            return profile

    raise instaloader.exceptions.ProfileNotExistsException(
        f"Profile {username} does not exist (or Instagram's search couldn't resolve it)."
    )


# ----------------------------------------------------------------------------
# Stories feature
# ----------------------------------------------------------------------------

def download_stories(loader: instaloader.Instaloader, target_accounts: list[str], log) -> int:
    loader.dirname_pattern = "stories/{target}"
    loader.filename_pattern = "{date_utc}_{shortcode}"
    loader.download_videos = True
    loader.download_video_thumbnails = False

    profiles = []
    for name in target_accounts:
        try:
            profiles.append(resolve_profile(loader, name))
        except instaloader.exceptions.ProfileNotExistsException as e:
            log(f"[{name}] {e}")

    if not profiles:
        log("No valid profiles found.")
        return 0

    user_ids = [p.userid for p in profiles]

    downloaded = 0
    for story in loader.get_stories(userids=user_ids):
        for item in story.get_items():
            loader.download_storyitem(item, target=story.owner_username)
            downloaded += 1
        log(f"[{story.owner_username}] Downloaded story item(s).")

    log(f"Done downloading stories. {downloaded} item(s) downloaded.")
    return downloaded


# ----------------------------------------------------------------------------
# Reels feature
# ----------------------------------------------------------------------------

def get_reels(loader: instaloader.Instaloader, target: str, since: datetime = None,
              only_latest: bool = True, lookahead: int = 6):
    """
    Fetch reels from a target profile, correctly handling pinned posts.
    - Pulls up to `lookahead` posts from the top (covers up to 3 possible pinned + buffer)
    - Sorts by actual date_utc rather than trusting feed order
    - since: only include reels posted on/after this UTC datetime (optional)
    - only_latest: if True, return just the single most recent reel by date
    """
    profile = resolve_profile(loader, target)

    candidates = []
    for i, post in enumerate(profile.get_posts()):
        if i >= lookahead and not since:
            break

        if not post.is_video:
            continue
        if getattr(post, "product_type", None) not in (None, "clips", "reels"):
            continue

        post_date = post.date_utc.replace(tzinfo=timezone.utc)

        if since and post_date < since and i >= lookahead:
            break

        if since and post_date < since:
            continue

        candidates.append(post)

    candidates.sort(key=lambda p: p.date_utc, reverse=True)

    if only_latest:
        return candidates[:1]
    return candidates


def download_reels_for_account(loader: instaloader.Instaloader, target: str, log,
                                since: datetime = None, only_latest: bool = True,
                                delay_range=(15, 30)) -> int:
    try:
        reels = get_reels(loader, target, since=since, only_latest=only_latest)
    except instaloader.exceptions.ProfileNotExistsException as e:
        log(f"[{target}] {e}")
        return 0
    except instaloader.exceptions.ConnectionException as e:
        log(f"[{target}] Connection error, skipping: {e}")
        return 0

    if not reels:
        log(f"[{target}] No matching reels found.")
        return 0

    downloaded = 0
    for post in reels:
        log(f"[{target}] Downloading reel {post.shortcode} posted {post.date_utc} UTC")
        try:
            loader.download_post(post, target=target)
            downloaded += 1
        except Exception as e:
            log(f"[{target}] Failed to download {post.shortcode}: {e}")
        time.sleep(random.uniform(*delay_range))

    log(f"[{target}] Done. Downloaded {downloaded} reel(s).")
    return downloaded


def download_reels(loader: instaloader.Instaloader, target_accounts: list[str], log,
                    since: datetime = None, only_latest: bool = True,
                    delay_range=(15, 30), account_delay_range=(30, 60)) -> int:
    loader.dirname_pattern = "reels/{target}"
    loader.filename_pattern = "{date_utc}_{shortcode}"
    loader.download_videos = True
    loader.download_video_thumbnails = False
    loader.download_pictures = False
    loader.post_metadata_txt_pattern = "{caption}"

    total_downloaded = 0
    for i, target in enumerate(target_accounts):
        count = download_reels_for_account(
            loader, target, log, since=since, only_latest=only_latest, delay_range=delay_range
        )
        total_downloaded += count

        if i < len(target_accounts) - 1:
            pause = random.uniform(*account_delay_range)
            log(f"Pausing {pause:.0f}s before next account...")
            time.sleep(pause)

    log(f"\nAll done. Downloaded {total_downloaded} reel(s) across {len(target_accounts)} account(s).")
    return total_downloaded


# ----------------------------------------------------------------------------
# Shared UI helpers
# ----------------------------------------------------------------------------

def make_logger(placeholder):
    lines: list[str] = []

    def log(message: str):
        lines.append(message)
        placeholder.text("\n".join(lines))

    return log


def list_downloaded_files(root: str):
    root_path = Path(root)
    if not root_path.exists():
        return []
    return sorted(str(p) for p in root_path.rglob("*") if p.is_file())


# ----------------------------------------------------------------------------
# On-screen text extraction (OCR)
# ----------------------------------------------------------------------------
#
# Instagram doesn't expose text overlays/stickers added on top of a Story or
# Reel as structured metadata — that text is baked directly into the video's
# pixels when the story/reel is created. The only way to recover it is OCR:
# sample frames from the downloaded video and run them through an OCR engine.

OCR_INDEX_PATH = Path("ocr_text_index.json")


def get_ocr_reader():
    """Lazily create (and cache) the EasyOCR reader. Loading the model is slow
    the first time (downloads model weights), so it's cached in session_state."""
    if "ocr_reader" not in st.session_state:
        import easyocr
        st.session_state.ocr_reader = easyocr.Reader(["en"])
    return st.session_state.ocr_reader


def extract_frames(video_path: Path, interval_sec: float = 1.0):
    """Yield (timestamp_seconds, frame) tuples sampled every `interval_sec` seconds."""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(int(fps * interval_sec), 1)

    frames = []
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % frame_interval == 0:
            frames.append((idx / fps, frame))
        idx += 1
    cap.release()
    return frames


def extract_text_from_video(reader, video_path: Path, interval_sec: float = 1.0,
                             min_confidence: float = 0.5) -> dict:
    """Sample frames from a video and OCR each one, de-duplicating repeated text
    (e.g. text that's on screen for several sampled frames in a row)."""
    detections = []
    seen_texts = set()

    for timestamp, frame in extract_frames(video_path, interval_sec=interval_sec):
        for _, text, confidence in reader.readtext(frame):
            text = text.strip()
            if not text or confidence < min_confidence:
                continue
            key = text.lower()
            if key in seen_texts:
                continue
            seen_texts.add(key)
            detections.append({
                "timestamp": round(timestamp, 2),
                "text": text,
                "confidence": round(float(confidence), 3),
            })

    return {
        "video_path": str(video_path),
        "detections": detections,
        "full_text": " ".join(d["text"] for d in detections),
    }


def load_ocr_index() -> list[dict]:
    if OCR_INDEX_PATH.exists():
        return json.loads(OCR_INDEX_PATH.read_text(encoding="utf-8"))
    return []


def save_ocr_index(index: list[dict]):
    OCR_INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def extract_text_from_new_videos(reader, root_dirs: list[str], interval_sec: float, log) -> list[dict]:
    index = load_ocr_index()
    indexed_paths = {entry["video_path"] for entry in index}

    video_files = []
    for root in root_dirs:
        root_path = Path(root)
        if root_path.exists():
            video_files.extend(sorted(root_path.rglob("*.mp4")))

    new_count = 0
    for video_path in video_files:
        if str(video_path) in indexed_paths:
            continue
        log(f"Extracting on-screen text from {video_path}...")
        try:
            result = extract_text_from_video(reader, video_path, interval_sec=interval_sec)
            index.append(result)
            new_count += 1
        except Exception as e:
            log(f"Failed to process {video_path}: {e}")

    save_ocr_index(index)
    log(f"Processed {new_count} new video(s). Index now has {len(index)} entries.")
    return index


def search_ocr_text(keywords: list[str], match_all: bool = False, case_sensitive: bool = False) -> list[dict]:
    index = load_ocr_index()
    results = []

    for entry in index:
        text = entry["full_text"] if case_sensitive else entry["full_text"].lower()
        search_terms = keywords if case_sensitive else [k.lower() for k in keywords]

        matched_terms = [term for term in search_terms if term in text]
        is_match = (len(matched_terms) == len(search_terms)) if match_all else bool(matched_terms)
        if not is_match:
            continue

        matching_detections = [
            d for d in entry["detections"]
            if any(term in (d["text"] if case_sensitive else d["text"].lower()) for term in matched_terms)
        ]
        results.append({
            "video_path": entry["video_path"],
            "matched_keywords": matched_terms,
            "matching_detections": matching_detections,
        })

    return results


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

st.title("📸 Instagram Stories & Reels Scraper")

if not st.session_state.logged_in:
    st.info("Log in with your Instagram account in the sidebar to get started.")
    st.stop()

tab_stories, tab_reels, tab_ocr = st.tabs(["Stories", "Reels", "On-Screen Text"])

with tab_stories:
    st.subheader("Download Stories")
    target_accounts = get_target_accounts("stories")

    if target_accounts:
        st.write(f"**{len(target_accounts)} target account(s):** {', '.join(target_accounts)}")

    if st.button("Download Stories", disabled=not target_accounts, key="download_stories_btn"):
        log_placeholder = st.empty()
        log = make_logger(log_placeholder)
        with st.spinner("Downloading stories..."):
            download_stories(st.session_state.loader, target_accounts, log)

        files = list_downloaded_files("stories")
        if files:
            with st.expander(f"Downloaded files ({len(files)})"):
                for f in files:
                    st.text(f)

with tab_reels:
    st.subheader("Download Reels")
    target_accounts = get_target_accounts("reels")

    if target_accounts:
        st.write(f"**{len(target_accounts)} target account(s):** {', '.join(target_accounts)}")

    mode = st.radio("Mode", ["Latest reel only", "All reels since a date"], key="reels_mode")
    only_latest = mode == "Latest reel only"

    since = None
    if not only_latest:
        since_date = st.date_input("Only include reels posted on/after", key="reels_since")
        since = datetime.combine(since_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    col1, col2 = st.columns(2)
    with col1:
        delay_min, delay_max = st.slider(
            "Delay between downloads (seconds)", 1, 120, (15, 30), key="reels_delay"
        )
    with col2:
        account_delay_min, account_delay_max = st.slider(
            "Delay between accounts (seconds)", 1, 180, (30, 60), key="reels_account_delay"
        )

    if st.button("Download Reels", disabled=not target_accounts, key="download_reels_btn"):
        log_placeholder = st.empty()
        log = make_logger(log_placeholder)
        with st.spinner("Downloading reels..."):
            download_reels(
                st.session_state.loader,
                target_accounts,
                log,
                since=since,
                only_latest=only_latest,
                delay_range=(delay_min, delay_max),
                account_delay_range=(account_delay_min, account_delay_max),
            )

        files = list_downloaded_files("reels")
        if files:
            with st.expander(f"Downloaded files ({len(files)})"):
                for f in files:
                    st.text(f)

with tab_ocr:
    st.subheader("Extract On-Screen Text (OCR)")
    st.caption(
        "Instagram doesn't expose text stickers/overlays as metadata — they're baked into "
        "the video pixels. This scans downloaded story/reel videos with OCR (EasyOCR) to pull "
        "out visible on-screen text. Accuracy varies with font, animation, and background "
        "contrast, and it will not be as reliable as a real caption."
    )

    sources = st.multiselect(
        "Folders to scan", ["stories", "reels"], default=["stories", "reels"], key="ocr_sources"
    )
    interval = st.slider(
        "Sample every N seconds", 0.5, 5.0, 1.0, step=0.5, key="ocr_interval",
        help="Smaller values catch more text but take longer to process.",
    )

    if st.button("Extract Text", disabled=not sources, key="ocr_extract_btn"):
        with st.spinner("Loading OCR model (first run downloads model weights, this can take a while)..."):
            reader = get_ocr_reader()

        log_placeholder = st.empty()
        log = make_logger(log_placeholder)
        with st.spinner("Extracting on-screen text..."):
            extract_text_from_new_videos(reader, sources, interval, log)

    st.divider()
    st.subheader("Search extracted text")

    query = st.text_input("Keywords (comma-separated)", key="ocr_search_query")
    match_all = st.checkbox("Match all keywords", key="ocr_match_all")

    if st.button("Search", key="ocr_search_btn"):
        keywords = [k.strip() for k in query.split(",") if k.strip()]
        if not keywords:
            st.warning("Enter at least one keyword.")
        else:
            results = search_ocr_text(keywords, match_all=match_all)
            if not results:
                st.info(f"No matches found for {keywords}.")
            else:
                st.write(f"Found {len(results)} matching video(s):")
                for r in results:
                    with st.expander(r["video_path"]):
                        st.write(f"Matched: {', '.join(r['matched_keywords'])}")
                        for d in r["matching_detections"]:
                            st.text(f"[{d['timestamp']}s] {d['text']} (confidence {d['confidence']})")
