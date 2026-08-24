"""
Instagram Stories & Reels Scraper — Streamlit app.

Combines the features from insta_scraper.ipynb into a single deployable app:
  - Sidebar login (with 2FA support) using instaloader, session cached to disk.
  - Target account input via CSV upload and/or manual entry.
  - "Stories" tab: download current stories for the selected accounts.
  - "Reels" tab: download the latest reel, or all reels since a given date,
    for the selected accounts (correctly handling pinned posts).

Run with:
    streamlit run app.py
"""

import random
import time
from datetime import datetime, timezone
from pathlib import Path

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
# Main app
# ----------------------------------------------------------------------------

st.title("📸 Instagram Stories & Reels Scraper")

if not st.session_state.logged_in:
    st.info("Log in with your Instagram account in the sidebar to get started.")
    st.stop()

tab_stories, tab_reels = st.tabs(["Stories", "Reels"])

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
