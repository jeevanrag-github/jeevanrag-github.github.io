#!/usr/bin/env python3
"""LinkedIn feed scanner — login via Google and collect engagement recommendations."""

import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SESSION_DIR = Path("/workspace/.linkedin_session")
SESSION_FILE = SESSION_DIR / "state.json"
OUTPUT_FILE = Path("/workspace/feed_results.json")
SCREENSHOT_DIR = Path("/workspace/login_screenshots")

EMAIL = os.environ.get("LINKEDIN_EMAIL", "")
PASSWORD = os.environ.get("GOOGLE_PASSORD", "")
AUTH_METHOD = os.environ.get("LINKEDIN_AUTH_METHOD", "")
TOTP_SECRET = os.environ.get("GOOGLE_TOTP_SECRET", "")

TFA_POLL_SECONDS = 5
TFA_MAX_WAIT_SECONDS = 90  # 2SV disabled; short wait if Google shows unexpected challenge

SKIP_KEYWORDS = re.compile(
    r"\b(hiring now|we're hiring|apply now|job opening|limited time offer|"
    r"click link in bio|follow for more|engagement bait|giveaway|"
    r"repost if you agree|tag someone who)\b",
    re.I,
)
PREFER_KEYWORDS = re.compile(
    r"\b(ai|artificial intelligence|machine learning|llm|gpt|software|engineering|"
    r"developer|data science|cloud|leadership|career|tech|startup|product|"
    r"architecture|python|javascript|typescript|devops|kubernetes|api)\b",
    re.I,
)
POLITICAL = re.compile(
    r"\b(election|political party|democrat|republican|left wing|right wing|"
    r"immigration ban|culture war)\b",
    re.I,
)
CHALLENGE_HINTS = (
    "challenge",
    "signin/v2/challenge",
    "totp",
    "verify",
    "captcha",
    "recaptcha",
    "2-step",
    "two-step",
    "phone number",
    "tap yes",
    "check your",
)

STORAGE_STATE_FILE = Path("/workspace/linkedin_storage_state.json")


def is_on_feed(page) -> bool:
    url = page.url.lower()
    return "linkedin.com/feed" in url and "login" not in url and "checkpoint" not in url


def save_state(context):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(SESSION_FILE))
    context.storage_state(path=str(STORAGE_STATE_FILE))


def screenshot(page, name: str):
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"))
    except Exception:
        pass


def has_li_at(context) -> bool:
    cookies = context.cookies("https://www.linkedin.com")
    return any(c["name"] == "li_at" and c["value"] for c in cookies)


def page_looks_like_challenge(page) -> bool:
    url = page.url.lower()
    if any(h in url for h in CHALLENGE_HINTS):
        return True
    try:
        body = page.inner_text("body")[:4000].lower()
    except Exception:
        return False
    return any(h in body for h in CHALLENGE_HINTS)


def try_totp(page) -> bool:
    if not TOTP_SECRET:
        return False
    try:
        import pyotp

        code = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
        field = page.locator('input[type="tel"], input[name="totpPin"], input[aria-label*="code" i]').first
        field.wait_for(state="visible", timeout=8000)
        field.fill(code)
        page.locator("#totpNext, button:has-text('Next')").first.click()
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def extract_2fa_tap_info(page) -> dict:
    """Extract Google 2FA details (tap-number or tap-yes prompts)."""
    info: dict = {"tap_number": None, "devices": [], "prompt_type": "number"}
    try:
        body = page.inner_text("body")[:8000]
    except Exception:
        return info

    if re.search(r"tap\s+yes|sent a notification|check your .{0,40}(phone|device)", body, re.I):
        info["prompt_type"] = "yes"
        info["tap_number"] = "YES"

    for pattern in (
        r"tap\s+(?:number\s+)?(\d{1,3})\b",
        r"number\s+(\d{1,3})\s+on\s+your",
        r"choose\s+(\d{1,3})\b",
        r"(\d{2,3})\s+on\s+your\s+(?:phone|device)",
    ):
        match = re.search(pattern, body, re.I)
        if match:
            info["tap_number"] = match.group(1)
            info["prompt_type"] = "number"
            break

    for device in re.findall(
        r"(Galaxy [A-Za-z0-9+ ]+|iPhone \d+[A-Za-z+ ]*|Pixel \d+[A-Za-z+ ]*|"
        r"iPad(?: Pro)?[A-Za-z0-9+ ]*)",
        body,
        re.I,
    ):
        cleaned = " ".join(device.split())
        if cleaned and cleaned not in info["devices"]:
            info["devices"].append(cleaned)

    return info


def emit_tfa_alert(tfa_info: dict) -> None:
    """Print 2FA alert immediately — must be first visible output during 2FA."""
    num = tfa_info.get("tap_number") or "???"
    devices = tfa_info.get("devices") or []
    device_str = ", ".join(devices) if devices else "your registered phone"
    if tfa_info.get("prompt_type") == "yes" or num == "YES":
        line = f"TAP YES — approve on {device_str} NOW. You have 5 minutes."
    else:
        line = f"TAP NUMBER: {num} — approve on {device_str} NOW. You have 5 minutes."
    print(line, flush=True)
    print(f"TFA_ALERT|tap={num}|devices={device_str}|type={tfa_info.get('prompt_type', 'number')}", flush=True)
    sys.stdout.flush()


def _active_google_page(page, google_page):
    try:
        if google_page and not google_page.is_closed():
            return google_page
    except Exception:
        pass
    return page


def _google_popup_closed(google_page, page) -> bool:
    if google_page is page:
        return False
    try:
        return google_page.is_closed()
    except Exception:
        return True


def _try_finish_login(page, context) -> dict | None:
    """Return success dict if LinkedIn session is established."""
    if has_li_at(context):
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        if is_on_feed(page) or has_li_at(context):
            save_state(context)
            return {"status": "success", "reason": "Google sign-in completed (li_at cookie)"}

    try:
        if is_on_feed(page):
            save_state(context)
            return {"status": "success", "reason": "Google sign-in completed (feed URL)"}
    except Exception:
        pass

    return None


def _recover_after_popup_close(page, context) -> dict | None:
    """After Google popup closes post-2FA, OAuth may finish on the LinkedIn opener."""
    for attempt in range(6):
        result = _try_finish_login(page, context)
        if result:
            return result
        try:
            page.wait_for_timeout(3000)
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        print(f"Waiting for LinkedIn session after Google popup closed ({attempt + 1}/6)...", flush=True)
    return None


def wait_for_auth_completion(page, google_page, context) -> dict:
    """Poll up to 5 minutes while user approves phone 2FA on their device."""
    deadline = time.time() + TFA_MAX_WAIT_SECONDS
    elapsed = 0
    challenge_seen = False
    tfa_alert_sent = False
    tfa_info_cached: dict = {}
    popup_closed_seen = False

    while time.time() < deadline:
        elapsed = TFA_MAX_WAIT_SECONDS - int(deadline - time.time())
        active_google = _active_google_page(page, google_page)

        if _google_popup_closed(google_page, page):
            popup_closed_seen = True
            challenge_seen = True
            print("Google popup closed — checking if 2FA was approved...", flush=True)
            recovered = _recover_after_popup_close(page, context)
            if recovered:
                return recovered

        for active in {page, active_google}:
            try:
                result = _try_finish_login(page, context)
                if result:
                    return result
                if "linkedin.com/feed" in active.url and is_on_feed(active):
                    save_state(context)
                    return {"status": "success", "reason": "Google sign-in completed (feed URL)"}
                if "linkedin.com" in active.url and "login" not in active.url and "checkpoint" not in active.url:
                    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                    result = _try_finish_login(page, context)
                    if result:
                        return result
            except Exception:
                pass

        on_challenge = page_looks_like_challenge(active_google) or page_looks_like_challenge(page)
        if on_challenge:
            challenge_seen = True
            if try_totp(active_google):
                continue

            tfa_info = extract_2fa_tap_info(active_google)
            if not tfa_info.get("tap_number"):
                tfa_info = extract_2fa_tap_info(page)

            if tfa_info.get("tap_number") and not tfa_alert_sent:
                emit_tfa_alert(tfa_info)
                tfa_alert_sent = True
                tfa_info_cached = tfa_info
                screenshot(active_google, "2fa_alert")
            elif tfa_alert_sent and elapsed > 0 and elapsed % 30 == 0:
                emit_tfa_alert(tfa_info_cached)
                screenshot(active_google, f"2fa_wait_{elapsed}s")
            elif not tfa_alert_sent:
                screenshot(active_google, f"2fa_wait_{elapsed}s")
                print(
                    f"TAP YES — approve on your phone NOW. Waiting ({elapsed}s / {TFA_MAX_WAIT_SECONDS}s)",
                    flush=True,
                )
        elif challenge_seen and not popup_closed_seen:
            # 2FA screen gone but popup still open — user may have approved; keep polling
            result = _try_finish_login(page, context)
            if result:
                return result
        elif not challenge_seen and elapsed > 15 and not popup_closed_seen:
            break

        page.wait_for_timeout(TFA_POLL_SECONDS * 1000)

    if popup_closed_seen:
        recovered = _recover_after_popup_close(page, context)
        if recovered:
            return recovered

    if challenge_seen:
        screenshot(active_google if not _google_popup_closed(google_page, page) else page, "2fa_blocked_final")
        return {
            "status": "2fa_blocked",
            "reason": f"Google 2FA still pending after {TFA_MAX_WAIT_SECONDS}s — approve on phone during the run",
        }

    result = _try_finish_login(page, context)
    if result:
        return result

    if "checkpoint" in page.url or "challenge" in page.url:
        return {"status": "2fa_blocked", "reason": "LinkedIn checkpoint/challenge page"}

    return {"status": "failed", "reason": f"Could not reach feed; final URL: {page.url}"}


def click_google_signin(page):
    """Return (google_page, clicked). Prefer role-based button (works in non-headless)."""
    google_page = page

    strategies = [
        lambda: page.get_by_role("button", name=re.compile(r"Sign in with Google|Continue with Google", re.I)).first,
        lambda: page.locator('iframe[title*="Sign in with Google"]').last,
        lambda: page.locator('div[role="button"]:has-text("Continue with Google")').last,
        lambda: page.locator('div[role="button"]:has-text("Sign in with Google")').last,
    ]

    for get_locator in strategies:
        loc = get_locator()
        try:
            loc.wait_for(state="visible", timeout=8000)
            with page.expect_popup(timeout=25000) as popup_info:
                loc.click(force=True, timeout=10000)
            google_page = popup_info.value
            google_page.wait_for_load_state("domcontentloaded", timeout=30000)
            return google_page, True
        except Exception:
            continue

    return google_page, False


def fill_google_credentials(google_page):
    if "accounts.google.com" not in google_page.url:
        return

    email_input = google_page.locator('input[type="email"], input[name="identifier"]').first
    try:
        email_input.wait_for(state="visible", timeout=15000)
        email_input.click()
        email_input.fill(EMAIL)
        google_page.locator("#identifierNext, button:has-text('Next')").first.click()
        google_page.wait_for_timeout(2500)
        screenshot(google_page, "01_email_entered")
    except PlaywrightTimeout:
        pass

    password_input = google_page.locator('input[type="password"], input[name="Passwd"]').first
    password_input.wait_for(state="visible", timeout=20000)
    password_input.click()
    password_input.fill(PASSWORD)
    google_page.locator("#passwordNext, button:has-text('Next')").first.click()
    google_page.wait_for_timeout(3000)
    screenshot(google_page, "02_password_submitted")


def login_via_google(page, context) -> dict:
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    if is_on_feed(page) or (has_li_at(context) and "login" not in page.url):
        return {"status": "success", "reason": "already logged in"}

    google_page, clicked = click_google_signin(page)
    if not clicked:
        screenshot(page, "google_button_not_found")
        return {"status": "failed", "reason": "Google sign-in button not found"}

    fill_google_credentials(google_page)
    return wait_for_auth_completion(page, google_page, context)


def extract_posts(page) -> list[dict]:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    posts = []
    seen = set()

    for _ in range(12):
        cards = page.locator(
            'div.feed-shared-update-v2, article.main-feed-activity, '
            'div[data-urn*="activity"], li.profile-creator-shared-feed-update__container'
        ).all()

        for card in cards:
            try:
                text = card.inner_text(timeout=2000)
            except Exception:
                continue
            if not text or len(text) < 40:
                continue
            key = text[:120]
            if key in seen:
                continue
            seen.add(key)

            author = "Unknown"
            for sel in (
                ".update-components-actor__name span",
                ".feed-shared-actor__name",
                "span.feed-shared-actor__title",
                "a.app-aware-link.update-components-actor__name",
            ):
                loc = card.locator(sel).first
                if loc.count():
                    author = loc.inner_text(timeout=1000).strip().split("\n")[0]
                    if author:
                        break

            snippet = " ".join(text.split())[:280]
            posts.append({"author": author, "snippet": snippet, "full_text": text[:1500]})
            if len(posts) >= 15:
                break

        if len(posts) >= 15:
            break
        page.evaluate("window.scrollBy(0, window.innerHeight * 1.2)")
        page.wait_for_timeout(1800)

    return posts


def classify_post(post: dict) -> dict:
    text = post["full_text"]
    snippet = post["snippet"]

    if SKIP_KEYWORDS.search(text):
        return {**post, "action": "skip", "draft": "", "priority": 0, "topic": "spam/promo"}
    if POLITICAL.search(text):
        return {**post, "action": "skip", "draft": "", "priority": 0, "topic": "political"}

    is_relevant = bool(PREFER_KEYWORDS.search(text))
    has_question = "?" in text
    is_thoughtful = len(text) > 200 and not text.lower().startswith("i'm excited to announce")

    if not is_relevant and not has_question:
        return {**post, "action": "skip", "draft": "", "priority": 1, "topic": "off-topic"}

    priority = 3 + (3 if is_relevant else 0) + (2 if has_question else 0) + (1 if is_thoughtful else 0)
    action = "comment" if (has_question or (is_relevant and is_thoughtful)) else "like"
    draft = draft_comment(text, post["author"]) if action == "comment" else ""
    topic_match = PREFER_KEYWORDS.search(text)
    topic = topic_match.group(0) if topic_match else "general"

    return {**post, "action": action, "draft": draft, "priority": priority, "topic": topic}


def draft_comment(text: str, author: str) -> str:
    first_name = author.split()[0] if author and author != "Unknown" else "there"
    lower = text.lower()

    if any(k in lower for k in ("ai", "llm", "machine learning")):
        return (
            f"Great perspective, {first_name}. The pace of change in AI is forcing teams to rethink "
            f"how they ship and measure value — curious what you've seen work best in practice."
        )
    if any(k in lower for k in ("leadership", "manager", "career")):
        return (
            f"Appreciate you sharing this, {first_name}. These lessons resonate — "
            f"the best leaders I know combine clarity with empathy when stakes are high."
        )
    if "?" in text:
        return (
            f"Thoughtful question, {first_name}. In my experience it depends on context, "
            f"but starting with small experiments and tight feedback loops usually surfaces the right path."
        )
    if any(k in lower for k in ("software", "engineering", "developer")):
        return (
            f"Solid take, {first_name}. Building reliable systems at scale is as much about culture "
            f"and communication as it is about tooling — thanks for putting this out there."
        )
    return (
        f"Thanks for sharing, {first_name}. Really valuable insight — "
        f"always good to see practical experience backed thinking on the feed."
    )


def launch_browser(playwright):
    launch_args = {
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }
    for channel in ("chrome", "chromium", None):
        try:
            kwargs = dict(launch_args)
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch(**kwargs)
        except Exception:
            continue
    return playwright.chromium.launch(headless=True, args=launch_args["args"])


def try_existing_session(context, page) -> dict | None:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    if is_on_feed(page):
        save_state(context)
        return {"status": "success", "reason": "Restored session (saved storage state)"}
    return None


def main():
    result = {
        "login": {"status": "pending", "reason": ""},
        "posts": [],
        "recommendations": [],
        "top3": [],
    }

    if not EMAIL or not PASSWORD or AUTH_METHOD != "google":
        result["login"] = {"status": "failed", "reason": "Missing or invalid secrets"}
        print(json.dumps(result, indent=2))
        sys.exit(1)

    with sync_playwright() as p:
        browser = launch_browser(p)
        context_args = {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if SESSION_FILE.exists():
            context_args["storage_state"] = str(SESSION_FILE)
        elif STORAGE_STATE_FILE.exists():
            context_args["storage_state"] = str(STORAGE_STATE_FILE)

        context = browser.new_context(**context_args)
        page = context.new_page()

        try:
            restored = try_existing_session(context, page)
            if restored:
                result["login"] = restored
            else:
                result["login"] = login_via_google(page, context)

            if result["login"]["status"] != "success":
                print(json.dumps(result, indent=2))
                OUTPUT_FILE.write_text(json.dumps(result, indent=2))
                browser.close()
                sys.exit(0)

            raw_posts = extract_posts(page)
            classified = [classify_post(p) for p in raw_posts]
            engage = [c for c in classified if c["action"] != "skip"][:10]
            engage.sort(key=lambda x: x["priority"], reverse=True)

            result["posts"] = [{"author": p["author"], "snippet": p["snippet"][:200]} for p in raw_posts[:10]]
            result["recommendations"] = [
                {
                    "author": r["author"],
                    "topic_snippet": r["snippet"][:120],
                    "action": r["action"],
                    "draft_reply": r["draft"] if r["action"] == "comment" else "",
                    "priority": r["priority"],
                }
                for r in engage
            ]
            result["top3"] = result["recommendations"][:3]
            save_state(context)
        except Exception as e:
            result["login"] = {"status": "failed", "reason": str(e)}
        finally:
            browser.close()

    OUTPUT_FILE.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
