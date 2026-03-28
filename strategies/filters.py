"""Filters — long-shot bias correction (Taleb) and correlation filter (Simons)."""

import re

import config


def extract_keywords(question: str) -> list[str]:
    """Extract meaningful keywords from a market question for correlation comparison."""
    stop_words = {
        "will", "the", "be", "is", "at", "in", "on", "by", "to", "of", "a", "an",
        "and", "or", "for", "with", "this", "that", "from", "has", "have", "was",
        "are", "been", "being", "do", "does", "did", "before", "after", "above",
        "below", "between", "during", "than", "more", "less", "over", "under",
        "yes", "no", "not", "what", "when", "where", "who", "how", "which",
    }
    words = re.findall(r"[a-zA-Z0-9]+", question.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


def longshot_bias_correction(market_price: float, raw_edge: float) -> float:
    """Taleb long-shot bias: markets systematically overprice low-probability events.

    For markets priced 5%-20%, add +8% to estimated edge because
    true probability is likely lower than market implies.
    """
    if config.LONGSHOT_LOW <= market_price <= config.LONGSHOT_HIGH:
        return raw_edge + config.LONGSHOT_CORRECTION
    return raw_edge


def correlation_score(keywords_a: list[str], keywords_b: list[str]) -> float:
    """Jaccard similarity of keyword sets between two markets."""
    set_a = set(keywords_a)
    set_b = set(keywords_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def filter_correlated(candidates: list[dict], max_corr: float = None) -> list[dict]:
    """Remove highly correlated trades, keeping the highest-EV one from each cluster.

    Each candidate dict must have 'keywords' (list[str]) and 'ev' (float).
    Returns filtered list with no pair exceeding the correlation threshold.
    """
    if max_corr is None:
        max_corr = config.CORRELATION_THRESHOLD

    # Sort by EV descending — greedy selection
    sorted_candidates = sorted(candidates, key=lambda c: c.get("ev", 0), reverse=True)
    selected = []

    for candidate in sorted_candidates:
        correlated = False
        for existing in selected:
            score = correlation_score(
                candidate.get("keywords", []),
                existing.get("keywords", []),
            )
            if score > max_corr:
                correlated = True
                break
        if not correlated:
            selected.append(candidate)

    return selected
