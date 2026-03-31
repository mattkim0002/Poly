"""Claude AI probability estimation for market outcomes."""

import json
import re

import anthropic

import config
from core.news import get_news_context
from utils.logger import log

_anthropic_client: anthropic.Anthropic | None = None

SYSTEM_PROMPT = """You are a calibrated probability forecaster. Estimate the true probability of prediction market outcomes.

Rules:
- Be well-calibrated: events you say are 70% likely should happen ~70% of the time
- Consider base rates, current evidence, and historical precedent
- PAY CLOSE ATTENTION to the recent news headlines provided — they reflect the current situation
- Account for your uncertainty
- Consider the current date when relevant

IMPORTANT: You must respond with ONLY a JSON object. No explanation before or after. No markdown.
Example: {"probability": 0.35, "confidence": "medium", "reasoning": "Historical base rate is low"}

probability = your estimate the FIRST outcome (Yes) is correct, from 0.01 to 0.99
confidence = high, medium, or low
reasoning = one sentence max"""


def get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def estimate_probability(
    question: str,
    outcomes: list[str],
    current_prices: list[float],
) -> dict | None:
    """Ask Claude to estimate the true probability of a market outcome.

    Returns:
        {"probability": float, "confidence": str, "reasoning": str}
        or None on failure
    """
    client = get_client()

    # Build the user prompt
    price_info = ", ".join(
        f"{outcome}: {price:.0%}" for outcome, price in zip(outcomes, current_prices)
    )

    # Fetch live news for context
    news_context = get_news_context(question)
    if news_context:
        log.info("Got live news for '%s'", question[:40])

    user_prompt = f"""Market question: "{question}"
Current market prices: {price_info}
Today's date: {_today()}
{news_context}

What is the TRUE probability that "{outcomes[0]}" is the correct outcome? Use the news headlines above (if any) plus your knowledge to make a well-calibrated estimate."""

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = response.content[0].text.strip()
        result = _parse_response(text)

        if result:
            log.info(
                "Claude estimate for '%s': prob=%.2f conf=%s",
                question[:60], result["probability"], result["confidence"],
            )
        return result

    except anthropic.RateLimitError:
        log.warning("Claude rate limited, skipping this market")
        return None
    except Exception as e:
        log.error("Claude estimation failed: %s", e)
        return None


def _parse_response(text: str) -> dict | None:
    """Parse Claude's JSON response, handling markdown code blocks."""
    # Strip markdown code fences if present
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Failed to parse Claude response as JSON: %s", text[:200])
        return None

    prob = data.get("probability")
    if prob is None or not isinstance(prob, (int, float)):
        log.warning("Missing or invalid probability in Claude response")
        return None

    prob = max(0.01, min(0.99, float(prob)))

    return {
        "probability": prob,
        "confidence": data.get("confidence", "low"),
        "reasoning": data.get("reasoning", ""),
    }


def _today() -> str:
    from datetime import date
    return date.today().isoformat()
