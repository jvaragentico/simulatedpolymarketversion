"""Read a current BTC 5m market and Polymarket's live opening reference.

No wallet access or orders. If the opening reference cannot be validated, fail
closed instead of substituting a spot quote or an estimated strike.
"""

from datetime import datetime, timezone
import json
import math
import re
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode

from polymarket_bot.feeds import GAMMA, get_json, market_from_gamma


OPEN_PRICE_API = "https://polymarket.com/api/crypto/crypto-price"
RESOLUTION_SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def market_json(url, start, clock=time.time):
    """Respect 429 backoff only while this market has a safe startup window."""
    for attempt in range(3):
        try:
            return get_json(url)
        except HTTPError as error:
            if error.code != 429:
                raise
            header = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(header) if header else 10.0
            except ValueError:
                try:
                    delay = parsedate_to_datetime(header).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    delay = 10.0
            delay = max(5.0, delay)
            if attempt == 2 or clock() + delay >= start + 110:
                raise ValueError("Polymarket is rate limiting market checks beyond this safe startup window") from error
            print(f"Polymarket rate limit; waiting {math.ceil(delay)} seconds before one retry...",
                  file=sys.stderr, flush=True)
            time.sleep(delay)


def startup_delay(now, after_slug=None):
    if after_slug and not re.fullmatch(r"btc-updown-5m-\d+", after_slug):
        raise ValueError("Invalid previous BTC market")
    start = int(now) // 300 * 300
    elapsed = now - start
    previous_start = int(after_slug.rsplit("-", 1)[1]) if after_slug else None
    if previous_start is not None and start <= previous_start:
        return previous_start + 330 - now
    return (30 - elapsed) if elapsed < 30 else (330 - elapsed if elapsed > 100 else 0)


def current_market(after_slug=None, clock=time.time):
    now = clock()
    # Give the opening reference time to appear, and leave at least 150 seconds
    # for startup, warmup, trading and cancellation. Wait for the next interval
    # when the current one is already too old or was already attempted.
    delay = startup_delay(now, after_slug)
    if delay > 0:
        print(f"Waiting {math.ceil(delay)} seconds for a fresh BTC market...", file=sys.stderr, flush=True)
        time.sleep(delay)
    now = clock()
    start = int(now) // 300 * 300
    slug = f"btc-updown-5m-{start}"
    raw = market_json(f"{GAMMA}/markets/slug/{slug}", start, clock)
    if raw.get("slug") != slug or raw.get("resolutionSource") != RESOLUTION_SOURCE:
        raise ValueError("Market identity or TWAP resolution source changed")
    config = raw.get("cryptoMarketConfig") or {}
    if config.get("asset") != "btc" or config.get("duration") != "5m" or config.get("twapEnabled") is not True or config.get("twapLookbackSeconds") != 60:
        raise ValueError("BTC 5m TWAP configuration is missing or changed")
    if not 30 <= now - start < 110:
        raise ValueError("Market is not in the safe startup window; run again")
    event_start = datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    event_end = datetime.fromtimestamp(start + 300, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if raw.get("eventStartTime") != event_start or raw.get("endDate") != event_end:
        raise ValueError("Gamma market interval does not match the requested price interval")
    url = OPEN_PRICE_API + "?" + urlencode(dict(symbol="BTC", eventStartTime=event_start,
                                               variant="fiveminute", endDate=event_end))
    # The public opening reference can lag the market listing. Wait briefly for
    # two consistent observations; never substitute a local spot quote.
    observations = []
    while clock() < start + 110:
        reference = market_json(url, start, clock)
        price, stamp = reference.get("openPrice"), reference.get("timestamp")
        valid = (not isinstance(price, bool) and isinstance(price, (int, float)) and
                 math.isfinite(price) and price > 0 and not isinstance(stamp, bool) and
                 isinstance(stamp, (int, float)) and
                 start * 1000 <= stamp <= (clock() + 5) * 1000)
        if valid:
            observations.append(price)
            if len(observations) >= 2 and abs(observations[-2] - observations[-1]) <= 0.005:
                break
        else:
            observations.clear()
        time.sleep(2 if not valid else 0.25)
    else:
        raise ValueError("Live Polymarket opening reference is unavailable or unstable")
    market = market_from_gamma(raw, observations[1])
    if market.slug != slug or clock() >= market.end - 190:
        raise ValueError("Market changed or is too close to expiry")
    return dict(slug=slug, strike=observations[1], conditionId=raw["conditionId"],
                source="Polymarket live crypto-price openPrice, BTC 5m TWAP market",
                observedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 2:
            raise ValueError("Expected at most one previous market slug")
        print(json.dumps(current_market(sys.argv[1] if len(sys.argv) == 2 else None)), flush=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"No live BTC market was approved: {error}", file=sys.stderr)
        sys.exit(1)

