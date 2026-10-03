# BTC Polymarket paper simulator

This is a separate, **paper-only** version. It starts with **$50 virtual cash**, records public Polymarket BTC Up/Down 5-minute order books and trades plus the BTC 60-second TWAP feed, and simulates maker orders. It compares two order caps, **$1 and $5**, on the same recording. It uses no wallet, private key, account endpoint, order submission, or cancellation endpoint. Running it cannot place a real bet.

## Start on Windows PowerShell

```powershell
git clone https://github.com/jvaragentico/simulatedpolymarketversion.git
cd simulatedpolymarketversion
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\scripts\start-paper-btc.ps1 -Markets 1
```

If the repository is already on your computer, open PowerShell in that directory and begin at the `py -3 -m venv .venv` step.

The simulator waits for a fresh BTC 5-minute market, records about 90 seconds of public data, and waits up to five more minutes for an official result. It writes detailed recordings and `runs\paper-summary.json`. To record more markets, use `-Markets 20`. Stop with `Ctrl+C`; this only stops data collection. To check pending resolutions and refresh the report later:

```powershell
.\scripts\start-paper-btc.ps1 -Review
```

Each new run records a fresh market. The two caps are replayed against the same book and trade data. The simulator follows the market's advertised minimum share size; when it is 5 shares, a $1 order can be simulated only at prices of 20 cents or below. The $5 comparison is included to show how this affects opportunity count. The virtual cash rolls forward only after an identity-matched finalized market result is available. Unresolved or incomplete feed recordings are not scored as wins.

The program measures clock skew from Polymarket's public server and applies that offset to captured receive times. It polls current public order-book snapshots and records public trade and TWAP updates. A capture with missing feeds or book snapshots arriving more than five seconds late appears under `excluded` and is never scored. REST snapshots between polls can miss short-lived price changes, so simulated fills remain estimates.

Public data can be delayed or time out. If capture stops, use `-Review` to inspect `excluded` and retry on a later fresh market. Failed captures do not change the virtual bankroll. No live order is ever sent.

## What the results mean

`virtual_balance` is modeled cash after finalized market payouts. `simulated_fills` are estimates based on public depth, queue position, observed aggressive trades, and latency. They are **not** exchange-confirmed fills. Public feeds can miss events; actual queue position, order acceptance, fees, cancellations, and real execution can differ. The probability model is an untrained baseline. A favorable paper result, especially from a few markets, does not establish a profitable strategy or justify refilling a live wallet. Gather a meaningful out-of-sample set of resolved markets and compare fills, trade frequency, and net results before making any live decision.

No live bot launcher or wallet code is included in this repository.

