"""Instagram HTML Scraper.

Scrapes public Instagram profiles via pixnoy.com's rendered HTML pages.
"""

__version__ = "1.0.0"

from .html_scraper import InstagramHtmlScraper

__all__ = [
    "InstagramHtmlScraper",
    "__version__",
]
