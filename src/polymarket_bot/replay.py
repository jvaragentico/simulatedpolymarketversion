"""Replay public observations in receive order; no wallet or exchange client."""

import json
from pathlib import Path

from .core import Config, Engine, Market


def replay(path, config=None):
    with Path(path).open(encoding="utf-8") as recording:
        meta = json.loads(next(recording))
        if (meta.get("kind") != "meta" or meta.get("schema") != 1 or
                meta.get("source") != "live_public" or
                meta.get("spot_feed") != "chainlink_twap"):
            raise ValueError("Expected a public BTC TWAP paper recording")
        market = Market(**meta["market"])
        if not market.slug.startswith("btc-updown-5m-"):
            raise ValueError("Only BTC 5-minute paper recordings are supported")
        engine = Engine(market, config or Config(**meta["config"]))
        for number, line in enumerate(recording, 2):
            if line.strip():
                try:
                    engine.ingest(json.loads(line))
                except (ValueError, KeyError, TypeError) as error:
                    raise ValueError(f"Invalid event on line {number}: {error}") from error
    report = engine.report()
    report["source"] = "live_public_paper"
    return report

