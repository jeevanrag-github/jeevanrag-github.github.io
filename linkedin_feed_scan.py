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

EMAIL = os.environ.get("LINKEDIN_EMAIL", "")
PASSWORD = os.environ.get("GOOGLE_PASSORD", "")
AUTH_METHOD = os.environ.get("LINKEDIN_AUTH_METHOD", "")

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


def save_state(context):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(SESSION_FILE))


def has_li_at(context) -> bool:
    cookies = context.cookies("https://www.linkedin.com")
    return any(c["name"] == "li_at" and c["value"] for c in cookies)


def login_via_google(page, context) -> dict:
    """Return {status, reason}."""
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    if "feed" in page.url:
        return {"status": "success", "reason": "already logged in"}

    google_page = page
    clicked = False

    # Google Identity Services button is rendered inside a visible iframe overlay
    iframe = page.locator('iframe[title*="Sign in with Google"]').last
    try:
        iframe.wait_for(state="attached", timeout=10000)
        with page.expect_popup(timeout=20000) as popup_info:
            iframe.click(force=True, timeout=10000)
        google_page = popup_info.value
        clicked = True
    except Exception:
        btn = page.locator('div[role="button"]:has-text("Continue with Google")').last
        try:
            with page.expect_popup(timeout=20000) as popup_info:
                btn.click(force=True, timeout=10000)
            google_page = popup_info.value
            clicked = True
        except Exception:
            pass

    if not clicked:
        return {"status": "failed", "reason": "Google sign-in button not found"}

    google_page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Google account chooser or email entry
    if "accounts.google.com" in google_page.url:
        email_input = google_page.locator('input[type="email"], input[name="identifier"]').first
        try:
            email_input.wait_for(state="visible", timeout=15000)
            email_input.fill(EMAIL)
            google_page.locator("#identifierNext, button:has-text('Next')").first.click()
            google_page.wait_for_timeout(2500)
        except PlaywrightTimeout:
            pass

        password_input = google_page.locator('input[type="password"], input[name="Passwd"]').first
        try:
            password_input.wait_for(state="visible", timeout=15000)
            password_input.fill(PASSWORD)
            google_page.locator("#passwordNext, button:has-text('Next')").first.click()
            google_page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            pass

    # 2FA / challenge detection
    page.wait_for_timeout(3000)
    check_page = google_page if google_page != page else page
    url = check_page.url
    try:
        body = check_page.inner_text("body")[:3000].lower()
    except Exception:
        body = page.inner_text("body")[:3000].lower()

    if any(
        x in url or x in body
        for x in [
            "challenge",
            "signin/v2/challenge",
            "totp",
            "verify",
            "captcha",
            "recaptcha",
            "2-step",
            "two-step",
            "phone number",
        ]
    ):
        if "feed" not in url:
            return {"status": "2fa_blocked", "reason": "Google 2FA/CAPTCHA or verification required"}

    # Wait for LinkedIn redirect (popup may close and set cookies on main page)
    for _ in range(30):
        if has_li_at(context):
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            save_state(context)
            return {"status": "success", "reason": "Google sign-in completed"}
        if "linkedin.com/feed" in page.url:
            save_state(context)
            return {"status": "success", "reason": "Google sign-in completed"}
        for p in [page, google_page]:
            try:
                if "linkedin.com" in p.url and "login" not in p.url and "checkpoint" not in p.url:
                    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                    if has_li_at(context):
                        save_state(context)
                        return {"status": "success", "reason": "Redirected to LinkedIn"}
            except Exception:
                pass
        page.wait_for_timeout(2000)

    if "checkpoint" in page.url or "challenge" in page.url:
        return {"status": "2fa_blocked", "reason": "LinkedIn checkpoint/challenge page"}

    if has_li_at(context):
        save_state(context)
        return {"status": "success", "reason": "li_at cookie present after sign-in attempt"}

    return {"status": "failed", "reason": f"Could not reach feed; final URL: {page.url}"}


def extract_posts(page) -> list[dict]:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    posts = []
    seen = set()

    for scroll in range(12):
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
            for sel in [
                ".update-components-actor__name span",
                ".feed-shared-actor__name",
                "span.feed-shared-actor__title",
                "a.app-aware-link.update-components-actor__name",
            ]:
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

    priority = 3
    if is_relevant:
        priority += 3
    if has_question:
        priority += 2
    if is_thoughtful:
        priority += 1

    action = "comment" if (has_question or (is_relevant and is_thoughtful)) else "like"
    draft = ""
    if action == "comment":
        draft = draft_comment(text, post["author"])

    topic_match = PREFER_KEYWORDS.search(text)
    topic = topic_match.group(0) if topic_match else "general"

    return {
        **post,
        "action": action,
        "draft": draft,
        "priority": priority,
        "topic": topic,
    }


def draft_comment(text: str, author: str) -> str:
    first_name = author.split()[0] if author and author != "Unknown" else "there"
    lower = text.lower()

    if "ai" in lower or "llm" in lower or "machine learning" in lower:
        return (
            f"Great perspective, {first_name}. The pace of change in AI is forcing teams to rethink "
            f"how they ship and measure value — curious what you've seen work best in practice."
        )
    if "leadership" in lower or "manager" in lower or "career" in lower:
        return (
            f"Appreciate you sharing this, {first_name}. These lessons resonate — "
            f"the best leaders I know combine clarity with empathy when stakes are high."
        )
    if "?" in text:
        return (
            f"Thoughtful question, {first_name}. In my experience it depends on context, "
            f"but starting with small experiments and tight feedback loops usually surfaces the right path."
        )
    if "software" in lower or "engineering" in lower or "developer" in lower:
        return (
            f"Solid take, {first_name}. Building reliable systems at scale is as much about culture "
            f"and communication as it is about tooling — thanks for putting this out there."
        )
    return (
        f"Thanks for sharing, {first_name}. Really valuable insight — "
        f"always good to see practical experience backed thinking on the feed."
    )


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
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
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

        context = browser.new_context(**context_args)
        page = context.new_page()

        try:
            if has_li_at(context):
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
                if "feed" in page.url or has_li_at(context):
                    result["login"] = {"status": "success", "reason": "Restored session (li_at cookie)"}
                else:
                    result["login"] = login_via_google(page, context)
            else:
                result["login"] = login_via_google(page, context)

            if result["login"]["status"] != "success":
                print(json.dumps(result, indent=2))
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
