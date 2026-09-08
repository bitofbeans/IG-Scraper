"""HTML-first scraper for the public Pixnoy profile and post pages.

This module deliberately does not call Pixnoy's JSON endpoints.  It lets the
site's own profile page render its post grid, parses the resulting DOM, and
then opens each post page to read the media links rendered in its slideshow.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .utils.utils import BASE_URL, BrowserSession


class InstagramHtmlScraper:
    """Collect every rendered profile post and its post-page image URLs.

    ``headless=False`` is useful for the first run if Cloudflare asks for a
    browser check.  The scraper never sends a request to ``/api/*`` itself;
    all collection is from the HTML currently rendered in the browser.
    """

    def __init__(
        self,
        wait_seconds: float = 3,
        scroll_pause_seconds: float = 0.5,
        max_stalled_rounds: int = 4,
        headless: bool = True,
    ) -> None:
        self.wait_seconds = wait_seconds
        self.scroll_pause_seconds = scroll_pause_seconds
        self.max_stalled_rounds = max_stalled_rounds
        self.headless = headless
        self.driver = None

    # -- pure HTML parsing -------------------------------------------------

    @staticmethod
    def _text(node: Optional[Tag]) -> Optional[str]:
        if node is None:
            return None
        value = " ".join(node.stripped_strings).strip()
        return value or None

    @staticmethod
    def _url(value: Optional[str], base_url: str = BASE_URL) -> Optional[str]:
        if not value:
            return None
        return urljoin(base_url, value)

    @staticmethod
    def _count(node: Optional[Tag]) -> Optional[int]:
        if node is None:
            return None
        raw = (node.get("title") or node.get_text(" ", strip=True)).replace(",", "").strip()
        match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([KMBkmb]?)", raw)
        if not match:
            return None
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(match.group(2).lower(), 1)
        return int(float(match.group(1)) * multiplier)

    @classmethod
    def parse_profile_html(cls, html: str, requested_username: str) -> Dict[str, Any]:
        """Extract metadata available directly in a rendered profile page."""
        soup = BeautifulSoup(html, "html.parser")
        profile = soup.select_one(".profile")
        username = cls._text(profile.select_one(".username h2") if profile else None)
        avatar = profile.select_one(".ava img") if profile else None
        return {
            "username": (username or f"@{requested_username}").lstrip("@"),
            "userid": (soup.select_one('input[name="userid"]') or {}).get("value"),
            "full_name": cls._text(profile.select_one("h1.fullname") if profile else None),
            "biography": cls._text(profile.select_one(".info > .sum") if profile else None),
            "followers": cls._count(profile.select_one(".item_followers .num") if profile else None),
            "following": cls._count(profile.select_one(".item_following .num") if profile else None),
            # The viewer does not put an authoritative count in its profile
            # HTML.  It is set after all unique cards have been rendered.
            "posts_count": None,
            "profile_picture": cls._url(avatar.get("data-src") or avatar.get("src")) if avatar else None,
        }

    @classmethod
    def parse_profile_posts_html(cls, html: str) -> List[Dict[str, Any]]:
        """Read unique cards from the rendered profile grid, in display order."""
        soup = BeautifulSoup(html, "html.parser")
        posts: List[Dict[str, Any]] = []
        seen = set()
        for item in soup.select(".posts .items > .item"):
            post_link = item.select_one("a.cover_link[href*='/post/']")
            if not post_link:
                continue
            post_url = cls._url(post_link.get("href"))
            match = re.search(r"/post/([^/?#]+)/?", post_url or "")
            shortcode = match.group(1) if match else post_url
            if not shortcode or shortcode in seen:
                continue
            seen.add(shortcode)
            image = post_link.select_one("img")
            classes = " ".join((item.select_one(".corner span") or {}).get("class", []))
            is_video = "icon_video" in classes or "icon_tv" in classes
            posts.append({
                "shortcode": shortcode,
                "post_url": post_url,
                "caption": cls._text(item.select_one(".meta .sum")) or (image.get("alt") if image else None),
                "media_type": "video" if is_video else ("carousel" if "icon_multi" in classes else "image"),
                "is_video": is_video,
                "thumbnail": cls._url((image.get("data-src") or image.get("src")) if image else None),
                "image_urls": [],
            })
        return posts

    @classmethod
    def parse_post_html(cls, html: str) -> Dict[str, Any]:
        """Return post-page fields, especially the ordered, full-size photos."""
        soup = BeautifulSoup(html, "html.parser")
        image_urls: List[str] = []
        for slide in soup.select(".post .slide-item"):
            image = slide.select_one(".entry-body img")
            link = slide.select_one(".entry-body > a[href]")
            # A slideshow image is a photo even when it is lazy-loaded.  The
            # enclosing anchor holds the full resolution CDN link; image src is
            # merely Pixnoy's proxy/thumbnail URL.
            if image and link:
                media_url = cls._url(link.get("href"))
                if media_url and media_url not in image_urls:
                    image_urls.append(media_url)

        if not image_urls:
            og_image = soup.select_one('meta[property="og:image"]')
            fallback = cls._url(og_image.get("content")) if og_image else None
            if fallback:
                image_urls.append(fallback)

        return {
            "caption": cls._text(soup.select_one(".post .sum_full")),
            "published": cls._text(soup.select_one(".post .userinfo .time .txt")),
            "like_count": cls._count(soup.select_one(".post .count_item_like .num")),
            "comment_count": cls._count(soup.select_one(".post .count_item_comment .num")),
            "image_urls": image_urls,
        }

    # -- browser orchestration --------------------------------------------

    def _page_source(self) -> str:
        try:
            return self.driver.get_page_source()
        except Exception:
            return self.driver.page_source

    def _navigate(self, url: str) -> None:
        cdp = getattr(self.driver, "cdp", None)
        if cdp is not None:
            try:
                cdp.get(url)
                return
            except Exception:
                pass
        self.driver.get(url)

    def _js(self, script: str) -> Any:
        """Evaluate JavaScript through CDP when SeleniumBase is in CDP mode.

        SeleniumBase intentionally detaches the regular WebDriver HTTP channel
        after ``activate_cdp_mode()``.  Using ``execute_script`` in that mode
        therefore looks like a page that never loads even though the DOM is
        present.  CDP evaluation keeps the HTML workflow on that live DOM.
        """
        cdp = getattr(self.driver, "cdp", None)
        if cdp is not None:
            try:
                cdp_script = script.strip()
                if cdp_script.startswith("return "):
                    cdp_script = cdp_script[7:].strip()
                elif "return " in cdp_script:
                    # Put closing braces on a new line so sb_cdp's return-stripping
                    # regex on exp_list[-1] does not corrupt the last return statement.
                    cdp_script = f"(() => {{\n{cdp_script}\n}})()"
                return cdp.evaluate(cdp_script)
            except Exception:
                pass
        try:
            return self.driver.execute_script(script)
        except Exception:
            return None

    def _wait_for(self, javascript: str, description: str) -> None:
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            try:
                if self._js(javascript):
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"Timed out waiting for {description}")

    def _post_count(self) -> int:
        return int(self._js(
            "return document.querySelectorAll('.posts .items > .item').length"
        ) or 0)

    def load_all_profile_posts(self) -> int:
        """Incrementally scroll and paginate until the profile grid has no more pages."""
        self._wait_for(
            "return document.querySelectorAll('.posts .items > .item').length > 0",
            "profile posts",
        )
        previous_count = self._post_count()
        print(f"Initial posts loaded: {previous_count}")
        stalled_rounds = 0
        scrolls_without_growth = 0

        while True:
            # 1. Check if Pixnoy still has more posts.
            # Pixnoy removes `.posts .more` from the DOM when has_next is false.
            has_more = self._js(
                "return Boolean(document.querySelector('.posts .more'))"
            )
            if not has_more:
                time.sleep(1)
                has_more = self._js(
                    "return Boolean(document.querySelector('.posts .more'))"
                )
                if not has_more:
                    previous_count = self._post_count()
                    print(f"All posts loaded: {previous_count} total posts.")
                    return previous_count

            # 2. Scroll incrementally down the page
            self._js(
                "window.scrollBy({top: Math.max(480, Math.floor(window.innerHeight * 2)), behavior: 'smooth'});"
            )
            time.sleep(self.scroll_pause_seconds)

            # 3. Check if new posts were loaded by the incremental scroll
            current_count = self._post_count()
            if current_count > previous_count:
                print(f"Loaded {current_count} posts...")
                previous_count = current_count
                stalled_rounds = 0
                scrolls_without_growth = 0
                continue

            scrolls_without_growth += 1

            # 4. Check if we've reached the bottom of the current scrollable page
            at_bottom = self._js(
                "return (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 150)"
            )

            # If at the bottom or scrolled several times without growth,
            # Pixnoy might have paused autoload (every 10 batches) or need a click
            if at_bottom or scrolls_without_growth >= 3:
                # Scroll button into view and click 'View more' if available
                self._js("""
                    const button = document.querySelector('.posts .more_btn');
                    if (button) {
                        button.scrollIntoView({block: 'center', behavior: 'smooth'});
                        button.click();
                    }
                """)

                # Wait for new posts to arrive or for .more to disappear
                deadline = time.monotonic() + self.wait_seconds
                while time.monotonic() < deadline:
                    time.sleep(0.3)
                    current_count = self._post_count()
                    if current_count > previous_count:
                        print(f"Loaded {current_count} posts...")
                        previous_count = current_count
                        stalled_rounds = 0
                        scrolls_without_growth = 0
                        break

                    if not self._js("return Boolean(document.querySelector('.posts .more'))"):
                        time.sleep(1)
                        current_count = self._post_count()
                        print(f"Reached end of posts: {current_count} total posts.")
                        return current_count
                else:
                    stalled_rounds += 1
                    print(
                        f"Stalled round {stalled_rounds}/{self.max_stalled_rounds} at {previous_count} posts..."
                    )
                    if stalled_rounds >= self.max_stalled_rounds:
                        print(
                            f"Profile grid stopped growing at {previous_count} posts after {self.max_stalled_rounds} retries. Proceeding with loaded posts."
                        )
                        return previous_count

    def scrape(self, username: str) -> Dict[str, Any]:
        """Create the HTML-only JSON-ready result for one public profile."""
        username = username.strip().lstrip("@")
        session = BrowserSession(username, headless=self.headless)
        try:
            session.launch()
            if not session.challenge_cleared:
                raise RuntimeError("Cloudflare challenge was not cleared; no profile HTML is available")
            self.driver = session.driver
            self.load_all_profile_posts()
            profile_html = self._page_source()
            profile = self.parse_profile_html(profile_html, username)
            posts = self.parse_profile_posts_html(profile_html)
            profile["posts_count"] = len(posts)

            for index, post in enumerate(posts, start=1):
                print(f"Reading post {index}/{len(posts)}: {post['shortcode']}")
                try:
                    self._navigate(post["post_url"])
                    self._wait_for("return Boolean(document.querySelector('.post'))", "post page")
                    details = self.parse_post_html(self._page_source())
                    for key, value in details.items():
                        if value is not None and (key != "caption" or value):
                            post[key] = value
                except Exception as exc:
                    # Keep the profile-card record so a single transient CDN/page
                    # failure cannot silently remove an otherwise discovered show.
                    post["media_error"] = f"{type(exc).__name__}: {exc}"

            return {
                "profile": profile,
                "account_status": "public",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "posts": posts,
            }
        finally:
            if session.driver is not None:
                try:
                    session.driver.quit()
                except Exception:
                    pass
            self.driver = None
