import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.core import Engine, Market, Order
from polymarket_bot.live_market import startup_delay
from polymarket_bot.paper import official_winner, paper_config, settle_once, summary
from polymarket_bot.replay import replay


MARKET = Market("btc-updown-5m-1000", "up-token", "down-token", 84000, 1000, 1300)


def gamma(**changes):
    raw = dict(slug=MARKET.slug, conditionId="0xabc", endDate="1970-01-01T00:21:40Z",
               outcomes='["Up","Down"]', clobTokenIds='["up-token","down-token"]',
               closed=True, umaResolutionStatus="resolved", outcomePrices='["1","0"]')
    raw.update(changes)
    return raw


class PaperTests(unittest.TestCase):
    def test_too_late_for_full_capture_waits_for_next_market(self):
        self.assertEqual(startup_delay(1200 + 101), 229)
        self.assertEqual(startup_delay(1200 + 30), 0)

    def test_finalized_outcome_requires_matching_identity(self):
        self.assertEqual(official_winner(gamma(), MARKET, "0xabc"), "up-token")
        self.assertEqual(official_winner(gamma(outcomePrices='["0","1"]'), MARKET, "0xabc"), "down-token")
        self.assertIsNone(official_winner(gamma(umaResolutionStatus="proposed"), MARKET, "0xabc"))
        self.assertIsNone(official_winner(gamma(outcomePrices='["0.5","0.5"]'), MARKET, "0xabc"))
        for changed in (dict(conditionId="0xdef"), dict(slug="another"),
                        dict(clobTokenIds='["other","down-token"]')):
            with self.assertRaises(ValueError):
                official_winner(gamma(**changed), MARKET, "0xabc")

    def test_paper_stop_cancels_simulated_quote(self):
        engine = Engine(MARKET, paper_config(50, 1))
        engine.orders.append(Order("Up", engine.ticks["Up"], 5, 1001, 1000))
        engine.ingest(dict(kind="session_stop", ts=1002))
        self.assertEqual(engine.orders, [])
        self.assertEqual(engine.halted, "paper_session_finished")

    def test_only_finalized_recording_moves_virtual_cash(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            run = root / "paper-1000-example"
            run.mkdir()
            meta = dict(kind="meta", schema=1, source="live_public", spot_feed="chainlink_twap",
                        book_mode="rest_snapshots", market=asdict(MARKET),
                        config=asdict(paper_config(50, 1)))
            (run / "events.jsonl").write_text(json.dumps(meta) + "\n" +
                json.dumps(dict(kind="session_stop", ts=1100)) + "\n", encoding="utf-8")
            (run / "report.json").write_text(json.dumps(dict(feed_errors=[])), encoding="utf-8")
            (run / "identity.json").write_text(json.dumps(dict(slug=MARKET.slug,
                conditionId="0xabc")), encoding="utf-8")
            self.assertIn(run.name, summary(root)["variants"]["one_dollar"]["pending"])
            self.assertFalse(settle_once(run, lambda url: gamma(), lambda: 1299))
            self.assertTrue(settle_once(run, lambda url: gamma(), lambda: 1301))
            self.assertTrue(replay(run / "events.jsonl")["portfolio"]["resolved"])
            result = summary(root)
            self.assertEqual(Decimal(result["variants"]["one_dollar"]["virtual_balance"]), Decimal("50"))
            self.assertEqual(result["variants"]["five_dollar"]["settled_markets"], 1)
            (run / "report.json").write_text(json.dumps(dict(feed_errors=[],
                max_snapshot_delay_seconds=6)), encoding="utf-8")
            self.assertEqual(summary(root)["variants"]["one_dollar"]["excluded"], [run.name])

    def test_feed_gap_is_not_scored(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            run = Path(tmp) / "paper-gap"
            run.mkdir()
            (run / "identity.json").write_text("{}", encoding="utf-8")
            self.assertEqual(summary(tmp)["variants"]["one_dollar"]["excluded"], ["paper-gap"])


if __name__ == "__main__":
    unittest.main()

