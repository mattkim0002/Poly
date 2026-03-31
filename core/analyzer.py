"""Claude AI probability estimation for market outcomes."""

import json
import re

import anthropic

import config
from core.news import get_news_context
from utils.logger import log

_anthropic_client: anthropic.Anthropic | None = None

SYSTEM_PROMPT = """You are a calibrated probability forecaster trading on Polymarket with real money. Your job is to ONLY recommend trades where you have a genuine informational edge.

Rules:
- Be well-calibrated: events you say are 70% likely should happen ~70% of the time
- PAY CLOSE ATTENTION to the recent news headlines — they reflect the current situation
- If you don't have enough information to disagree with the market price, say confidence "low"
- If the market is basically a coin flip or you're guessing, say confidence "low"
- Only say confidence "high" if the news or evidence CLEARLY points one direction
- Consider the current date when relevant
- BE HONEST: if you don't know, admit it with "low" confidence. Bad trades lose real money.

IMPORTANT: You must respond with ONLY a JSON object. No explanation before or after. No markdown.
Example: {"probability": 0.35, "confidence": "medium", "reasoning": "Recent news shows sanctions tightening"}

probability = your estimate the FIRST outcome (Yes) is correct, from 0.01 to 0.99
confidence = high (strong evidence), medium (some evidence), or low (guessing/uncertain)
reasoning = one sentence explaining WHY you disagree with market price, or "no edge" if you don't"""


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
        # Retry up to 3 times on overloaded errors
        response = None
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=config.CLAUDE_MODEL,
                    max_tokens=config.CLAUDE_MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                break
            except anthropic.APIStatusError as e:
                if e.status_code == 529 and attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
                    continue
                raise

        if not response:
            return None

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
