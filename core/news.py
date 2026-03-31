"""Live news fetcher — gets current headlines for market topics.

Uses Google News RSS (free, no API key needed) to fetch recent headlines
so Claude can make decisions based on current events.
"""

import re
import xml.etree.ElementTree as ET

import httpx

from utils.logger import log


def fetch_headlines(query: str, max_results: int = 5) -> list[str]:
    """Fetch recent news headlines from Google News RSS.

    Args:
        query: Search query (e.g. "Bitcoin price", "Iran sanctions")
        max_results: Max headlines to return

    Returns:
        List of headline strings
    """
    try:
        # Clean query for URL
        clean_query = re.sub(r'[^\w\s]', '', query)[:80]
        url = f"https://news.google.com/rss/search?q={clean_query}&hl=en-US&gl=US&ceid=US:en"

        resp = httpx.get(url, timeout=5, follow_redirects=True)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        headlines = []
        for item in root.findall('.//item')[:max_results]:
            title = item.find('title')
            if title is not None and title.text:
                # Clean HTML entities and extra whitespace
                headline = title.text.strip()
                headline = re.sub(r'\s+', ' ', headline)
                headlines.append(headline)

        return headlines

    except Exception as e:
        log.debug("News fetch failed for '%s': %s", query[:30], e)
        return []


def get_news_context(question: str) -> str:
    """Extract key terms from a market question and fetch relevant news.

    Returns a formatted string of headlines, or empty string if none found.
    """
    # Extract the core topic from the question
    # Remove common prediction market phrasing
    clean = question.lower()
    for phrase in ["will ", "by ", "before ", "on ", "in ", "the ", "a ",
                   "march", "april", "may", "june", "july", "august",
                   "september", "october", "november", "december",
                   "2025", "2026", "2027"]:
        clean = clean.replace(phrase, " ")

    # Take the most meaningful words
    words = [w for w in clean.split() if len(w) > 3][:5]
    search_query = " ".join(words)

    if not search_query.strip():
        return ""

    headlines = fetch_headlines(search_query)
    if not headlines:
        return ""

    formatted = "\n".join(f"- {h}" for h in headlines)
    return f"\nRecent news headlines:\n{formatted}"
