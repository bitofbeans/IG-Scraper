import json
from pathlib import Path

from scraper.instagram_posts_scraper import InstagramHtmlScraper

# This path parses only browser-rendered HTML.  It does not make direct calls
# to Pixnoy's unstable JSON API.
scraper = InstagramHtmlScraper(headless=True)
res = scraper.scrape(username="vlonelyandfriends")

output_path = Path(__file__).with_name("scrape.json")
output_path.write_text(json.dumps(res, indent=4, default=str))

print(f"Saved scrape output to {output_path}")
