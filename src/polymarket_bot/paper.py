"""Public-feed BTC paper sessions. No keys, account API, or order submission."""

import argparse
import asyncio
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from .core import Config, Market
from .feeds import GAMMA, array, epoch, get_json, market_from_gamma, shadow
from .live_market import current_market
from .replay import replay


STARTING_BALANCE = Decimal("50")
CAPS = {"one_dollar": Decimal("1"), "five_dollar": Decimal("5")}


def public_clock_offset():
    """Measure host skew against the public Gamma Date header; no account API."""
    for _ in range(3):
        before = time.time()
        start = int(before) // 300 * 300
        request = Request(f"{GAMMA}/markets/slug/btc-updown-5m-{start}",
                          headers={"Cache-Control": "no-cache", "User-Agent": "polymarket-shadow-bot/0.1"})
        with urlopen(request, timeout=10) as response:
            server_date = response.headers.get("Date")
        after = time.time()
        if server_date and after - before <= 3:
            offset = parsedate_to_datetime(server_date).timestamp() - (before + after) / 2
            if abs(offset) <= 60:
                return offset
            raise ValueError("Computer and Polymarket clocks differ by more than a minute")
    raise ValueError("Could not verify public server time")


def paper_config(balance, cap):
    """Match the live maker settings while limiting simulated market exposure."""
    balance, cap = Decimal(str(balance)), Decimal(str(cap))
    budget = min(balance, cap)
    if budget < Decimal("0.01"):
        raise ValueError("Virtual bankroll is below the minimum paper budget")
    return Config(capital=float(balance), order_dollars=float(budget),
                  max_market_spend=float(budget), max_loss=float(budget),
                  max_net_shares=20, allow_taker=False, quote_lifetime=8)


def official_winner(raw, market, expected_condition):
    """Accept only an identity-matched, finalized binary Gamma result."""
    if (raw.get("slug") != market.slug or
            str(raw.get("conditionId", "")).lower() != expected_condition.lower() or
            abs(epoch(raw["endDate"]) - market.end) > 1):
        raise ValueError("Resolved market identity changed")
    outcomes, tokens = array(raw["outcomes"]), array(raw["clobTokenIds"])
    if (outcomes != ["Up", "Down"] or
            tokens != [market.up_token, market.down_token]):
        raise ValueError("Resolved market tokens changed")
    if raw.get("closed") is not True or raw.get("umaResolutionStatus") != "resolved":
        return None
    prices = array(raw.get("outcomePrices", []))
    if len(prices) != 2:
        return None
    try:
        prices = [Decimal(str(value)) for value in prices]
    except Exception as error:
        raise ValueError("Invalid resolved outcome prices") from error
    if prices == [Decimal(1), Decimal(0)]:
        return market.up_token
    if prices == [Decimal(0), Decimal(1)]:
        return market.down_token
    return None


def recording_identity(directory):
    identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    with (directory / "events.jsonl").open(encoding="utf-8") as stream:
        meta = json.loads(next(stream))
    if (meta.get("source") != "live_public" or meta.get("spot_feed") != "chainlink_twap" or
            meta.get("book_mode") != "rest_snapshots" or
            meta.get("market", {}).get("slug") != identity.get("slug") or
            identity.get("conditionId") is None):
        raise ValueError("Paper recording identity is invalid")
    capture = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if capture.get("feed_errors"):
        raise ValueError("Paper recording has feed gaps and cannot be scored")
    if capture.get("max_snapshot_delay_seconds", 0) > 5:
        raise ValueError("Paper book snapshots arrived more than five seconds late")
    return Market(**meta["market"]), identity


def append_event(directory, event):
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        stream.flush()


def settle_once(directory, fetch=get_json, now=time.time):
    market, identity = recording_identity(directory)
    if (directory / "settlement.json").exists():
        sidecar = json.loads((directory / "settlement.json").read_text(encoding="utf-8"))
        if sidecar.get("winner") not in (market.up_token, market.down_token):
            raise ValueError("Invalid existing paper settlement")
        return True
    if now() < market.end:
        return False
    raw = fetch(f"{GAMMA}/markets/slug/{quote(market.slug, safe='')}")
    winner = official_winner(raw, market, identity["conditionId"])
    if winner is None:
        return False
    append_event(directory, dict(kind="resolution", ts=max(now(), market.end), winner=winner,
                                 source="gamma_resolved_outcome"))
    (directory / "settlement.json").write_text(json.dumps(dict(winner=winner,
        observed_at=now(), source="gamma_resolved_outcome"), indent=2) + "\n", encoding="utf-8")
    return True


def review(root, fetch=get_json, now=time.time):
    """Refresh unsettled runs without submitting an order or using a wallet."""
    root = Path(root)
    for directory in sorted(root.glob("paper-*")) if root.exists() else []:
        if not directory.is_dir() or not (directory / "identity.json").exists():
            continue
        try:
            settle_once(directory, fetch, now)
        except (OSError, ValueError, KeyError, TimeoutError):
            # Preserve the recording; unresolved results are never counted as wins.
            continue
    return summary(root)


def summary(root):
    root = Path(root)
    directories = [directory for directory in sorted(root.glob("paper-*"))
                   if directory.is_dir() and (directory / "identity.json").exists()] if root.exists() else []
    variants = {}
    for name, cap in CAPS.items():
        balance = STARTING_BALANCE
        fills = settled = markets_with_fills = wins = losses = 0
        pending = []
        excluded = []
        blocked = False
        for directory in directories:
            if blocked or balance < Decimal("0.01"):
                pending.append(directory.name)
                continue
            try:
                recording_identity(directory)
                report = replay(directory / "events.jsonl", paper_config(balance, cap))
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                excluded.append(directory.name)
                continue
            portfolio = report["portfolio"]
            if not portfolio["resolved"]:
                pending.append(directory.name)
                blocked = True
                continue
            pnl = Decimal(portfolio["realized_pnl"])
            balance += pnl
            settled += 1
            fills += portfolio["fills"]
            markets_with_fills += portfolio["fills"] > 0
            wins += pnl > 0
            losses += pnl < 0
        variants[name] = dict(order_cap=str(cap), starting_balance=str(STARTING_BALANCE),
                              settled_markets=settled, markets_with_fills=markets_with_fills,
                              simulated_fills=fills, wins=wins, losses=losses,
                              realized_pnl=str(balance - STARTING_BALANCE),
                              virtual_balance=str(balance), pending=pending, excluded=excluded)
    result = dict(source="live Polymarket public books, trades, BTC TWAP and finalized Gamma outcomes",
                  execution="paper only; queue and latency estimates, never exchange-confirmed fills",
                  variants=variants, generated_at=time.time())
    root.mkdir(parents=True, exist_ok=True)
    (root / "paper-summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def record_one(root, after_slug=None, clock=time.time):
    """Record one fresh market for the same 90-second entry window as live mode."""
    info = current_market(after_slug, clock)
    raw = get_json(f"{GAMMA}/markets/slug/{quote(info['slug'], safe='')}")
    market = market_from_gamma(raw, info["strike"])
    if raw.get("conditionId") != info["conditionId"]:
        raise ValueError("Market condition changed after the opening-reference check")
    directory = Path(root) / f"paper-{int(market.start)}-{uuid4().hex[:8]}"
    print(f"Recording public books, trades and BTC TWAP: {market.slug}", flush=True)
    try:
        asyncio.run(shadow(market, paper_config(STARTING_BALANCE, 1), "BTC-USD", 90,
                           directory, spot_feed="chainlink_twap", clock=clock, capture_only=True))
    finally:
        if (directory / "events.jsonl").exists():
            (directory / "identity.json").write_text(json.dumps(dict(slug=market.slug,
                conditionId=info["conditionId"], official_strike=info["strike"],
                entry_seconds=90, source=info["source"]), indent=2) + "\n", encoding="utf-8")
            append_event(directory, dict(kind="session_stop", ts=clock()))
    return directory


def run_markets(root, count, resolution_wait=300, max_new_log_mb=1024, clock=time.time):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    previous = None
    baseline = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    for _ in range(count):
        current = summary(root)
        if current["variants"]["one_dollar"]["pending"]:
            print("An earlier paper market is unresolved. Run review later; no new market was started.")
            break
        if Decimal(current["variants"]["one_dollar"]["virtual_balance"]) < Decimal("0.01"):
            print("The $50 paper bankroll is exhausted; no new market was started.")
            break
        directory = record_one(root, previous, clock)
        previous = json.loads((directory / "identity.json").read_text(encoding="utf-8"))["slug"]
        market, _ = recording_identity(directory)
        deadline = max(clock(), market.end) + resolution_wait
        while clock() < deadline:
            try:
                if settle_once(directory, now=clock):
                    break
            except (OSError, ValueError, KeyError, TimeoutError):
                pass
            time.sleep(min(10, max(0, deadline - clock())))
        result = summary(root)
        print(json.dumps(result["variants"], indent=2), flush=True)
        size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) - baseline
        if size >= max_new_log_mb * 1024 * 1024:
            print("Paper recording size limit reached; stopped after this market.")
            break
        if directory.name in result["variants"]["one_dollar"]["pending"]:
            print("Official resolution is still pending. Run review later.")
            break
    return summary(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Public-data BTC paper simulator; never uses a wallet")
    parser.add_argument("command", choices=["run", "review"])
    parser.add_argument("--runs", default="runs")
    parser.add_argument("--markets", type=int, default=1)
    parser.add_argument("--resolution-wait", type=int, default=300)
    parser.add_argument("--max-new-log-mb", type=int, default=1024)
    args = parser.parse_args(argv)
    if not 1 <= args.markets <= 100 or not 0 <= args.resolution_wait <= 3600 or not 50 <= args.max_new_log_mb <= 4096:
        parser.error("Invalid paper run limits")
    try:
        offset = public_clock_offset()
        clock = lambda: time.time() + offset
        print(f"Public server clock offset applied: {offset:+.1f} seconds.", flush=True)
        result = review(args.runs, now=clock) if args.command == "review" else run_markets(
            args.runs, args.markets, args.resolution_wait, args.max_new_log_mb, clock)
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        print("Paper capture stopped. No real orders were submitted.")
        raise SystemExit(130)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        try:
            summary(args.runs)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        print(f"Paper run stopped: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

