"""Causal strategy, conservative execution, and exact decimal inventory accounting."""

from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import math


ZERO = Decimal("0")
ONE = Decimal("1")


def D(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite numeric input")
    return number


@dataclass
class Config:
    capital: float = 1000
    order_dollars: float = 23.59
    max_entry_price: float = 1.0
    max_market_spend: float = 250
    max_net_shares: float = 100
    max_loss: float = 50
    maker_edge: float = .025
    taker_edge: float = .07
    allow_taker: bool = False
    pair_edge: float = .01
    uncertainty_buffer: float = .01
    slippage_buffer: float = .005
    order_latency: float = .25
    cancel_latency: float = .25
    taker_delay: float = .25
    quote_lifetime: float = 3
    decision_interval: float = .25
    max_feed_age: float = 5
    warmup_seconds: float = 30
    model_window: float = 120
    volatility_floor: float = .00005
    momentum_weight: float = .15

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == "allow_taker":
                if not isinstance(value, bool):
                    raise ValueError("allow_taker must be boolean")
                continue
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid config: {key}")
        for key in ("capital", "order_dollars", "max_market_spend", "max_net_shares",
                    "max_loss", "quote_lifetime", "decision_interval", "max_feed_age",
                    "warmup_seconds", "model_window", "volatility_floor"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.model_window < self.warmup_seconds:
            raise ValueError("model_window must cover warmup_seconds")
        if self.max_market_spend > self.capital:
            raise ValueError("max_market_spend cannot exceed capital")
        if not 0 < self.max_entry_price <= 1:
            raise ValueError("max_entry_price must be in (0, 1]")


@dataclass
class Market:
    slug: str
    up_token: str
    down_token: str
    strike: float
    start: float
    end: float
    tick: str = "0.01"
    min_size: str = "5"
    fee_rate: str = "0.07"
    fee_exponent: float = 1
    fee_taker_only: bool = True

    def __post_init__(self):
        if (not self.slug or not self.up_token or not self.down_token
                or self.up_token == self.down_token or not isinstance(self.fee_taker_only, bool)):
            raise ValueError("Invalid binary market identity")
        for value in (self.start, self.end, self.strike, self.fee_exponent):
            D(value)
        if (self.strike <= 0 or self.end <= self.start or D(self.tick) <= 0
                or D(self.tick) >= 1 or D(self.min_size) <= 0
                or D(self.fee_rate) < 0 or self.fee_exponent <= 0):
            raise ValueError("Invalid market parameters")

    def outcome(self, token):
        if token == self.up_token:
            return "Up"
        if token == self.down_token:
            return "Down"
        raise ValueError("Token does not belong to this market")

    def fee(self, price, size, maker=False):
        if maker and self.fee_taker_only:
            return ZERO
        curve = D(float(price * (ONE - price)) ** self.fee_exponent)
        return (size * D(self.fee_rate) * curve).quantize(
            Decimal("0.00001"), rounding=ROUND_HALF_UP)


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    received: float = -math.inf
    source: float = -math.inf
    initialized: bool = False

    def update(self, event):
        if event["kind"] == "book":
            self.bids = self.levels(event["bids"])
            self.asks = self.levels(event["asks"])
            self.initialized = True
        elif self.initialized:
            price, size = D(event["price"]), D(event["size"])
            if not ZERO < price < ONE or size < ZERO:
                raise ValueError("Invalid book delta")
            if event["side"] not in ("BUY", "SELL"):
                raise ValueError("Invalid book side")
            levels = self.bids if event["side"] == "BUY" else self.asks
            if size:
                levels[price] = size
            else:
                levels.pop(price, None)
        else:
            return
        self.received = event["ts"]
        self.source = event.get("source_ts", event["ts"])

    @staticmethod
    def levels(rows):
        result = {}
        for price, size in rows:
            price, size = D(price), D(size)
            if not ZERO < price < ONE or size < ZERO:
                raise ValueError("Invalid order book level")
            if size:
                result[price] = size
        return result

    @property
    def bid(self):
        return max(self.bids, default=ZERO)

    @property
    def ask(self):
        return min(self.asks, default=ONE)

    def fresh(self, now, age):
        return (self.initialized and self.bids and self.asks and self.bid < self.ask
                and 0 <= now - self.received <= age
                and -.5 <= now - self.source <= age)


@dataclass
class Lot:
    size: Decimal
    cost: Decimal  # all-in unit cost, including any taker fee


class Portfolio:
    def __init__(self, capital):
        self.initial = D(capital)
        self.cash = self.initial
        self.spent = ZERO
        self.fees = ZERO
        self.lots = {"Up": deque(), "Down": deque()}
        self.pairs = []  # (quantity, combined all-in cost per share)
        self.fills = []
        self.settled = False
        self.realized = None

    def residual(self, side):
        return sum((lot.size for lot in self.lots[side]), ZERO)

    @property
    def paired(self):
        return sum((size for size, _ in self.pairs), ZERO)

    @property
    def net(self):
        return self.residual("Up") - self.residual("Down")

    def buy(self, side, size, price, fee, ts, mode):
        if self.settled or side not in self.lots or size <= 0 or not ZERO < price < ONE or fee < 0:
            raise ValueError("Invalid fill")
        total = size * price + fee
        if total > self.cash:
            raise ValueError("Insufficient simulated cash")
        self.cash -= total
        self.spent += total
        self.fees += fee
        self.lots[side].append(Lot(size, total / size))
        self.fills.append(dict(ts=ts, outcome=side, shares=str(size), price=str(price),
                               fee=str(fee), mode=mode))
        # FIFO matching: don't cherry-pick cheap lots or hide losing pairs.
        while self.lots["Up"] and self.lots["Down"]:
            up, down = self.lots["Up"][0], self.lots["Down"][0]
            quantity = min(up.size, down.size)
            self.pairs.append((quantity, up.cost + down.cost))
            up.size -= quantity
            down.size -= quantity
            for outcome in ("Up", "Down"):
                if self.lots[outcome] and not self.lots[outcome][0].size:
                    self.lots[outcome].popleft()

    def value(self, books):
        # Complete sets have a guaranteed terminal $1 payout, but no cash is recycled.
        return (self.cash + self.paired + self.residual("Up") * books["Up"].bid
                + self.residual("Down") * books["Down"].bid)

    def settle(self, winner):
        if self.settled or winner not in self.lots:
            raise ValueError("Invalid or duplicate resolution")
        self.cash += self.paired + self.residual(winner)
        self.realized = self.cash - self.initial
        self.settled = True

    def report(self, books):
        quantity = self.paired
        paired_cost = sum((size * cost for size, cost in self.pairs), ZERO)
        residual = abs(self.net)
        total_shares = 2 * quantity + residual
        fill_values = [D(f["shares"]) * D(f["price"]) for f in self.fills]
        return dict(
            cash=str(self.cash), total_cost=str(self.spent), fees=str(self.fees),
            fills=len(self.fills), average_trade_dollars=str(sum(fill_values, ZERO) / len(fill_values)) if fill_values else None,
            paired_shares=str(quantity), paired_cost=str(paired_cost),
            average_pair_cost=str(paired_cost / quantity) if quantity else None,
            paired_terminal_pnl=str(quantity - paired_cost),
            profitable_paired_shares=str(sum((size for size, cost in self.pairs if cost < ONE), ZERO)),
            residual_up=str(self.residual("Up")), residual_down=str(self.residual("Down")),
            inventory_imbalance=float(residual / total_shares) if total_shares else 0,
            conservative_mark_pnl=str((self.cash if self.settled else self.value(books)) - self.initial),
            realized_pnl=str(self.realized) if self.settled else None,
            resolved=self.settled,
        )


class ProbabilityModel:
    """Untrained baseline log-price diffusion with bounded momentum drift."""

    def __init__(self, config):
        self.config = config
        self.samples = deque()
        self.source_ts = -math.inf

    def update(self, ts, price, source_ts):
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid spot price")
        if not math.isfinite(source_ts):
            raise ValueError("Invalid spot timestamp")
        if source_ts <= self.source_ts:
            return
        # One observation per second keeps bursty messages from dominating volatility.
        if self.samples and int(ts) == int(self.samples[-1][0]):
            self.samples[-1] = (ts, math.log(price))
        else:
            self.samples.append((ts, math.log(price)))
        self.source_ts = source_ts
        while self.samples and ts - self.samples[0][0] > self.config.model_window:
            self.samples.popleft()

    def probability(self, now, market):
        c = self.config
        if (len(self.samples) < 3 or self.samples[-1][0] - self.samples[0][0] < c.warmup_seconds
                or not 0 <= now - self.samples[-1][0] <= c.max_feed_age
                or not -.5 <= now - self.source_ts <= c.max_feed_age):
            return None
        increments = [(b[1] - a[1], b[0] - a[0])
                      for a, b in zip(self.samples, list(self.samples)[1:]) if b[0] > a[0]]
        duration = sum(dt for _, dt in increments)
        drift = sum(r for r, _ in increments) / duration
        variance = sum((r - drift * dt) ** 2 for r, dt in increments) / duration
        sigma = max(c.volatility_floor, math.sqrt(variance))
        remaining = max(0, market.end - now)
        bounded_drift = max(-sigma / 4, min(sigma / 4, drift)) * c.momentum_weight
        denominator = sigma * math.sqrt(max(remaining, .1))
        z = (self.samples[-1][1] - math.log(market.strike) + bounded_drift * remaining) / denominator
        return max(.01, min(.99, .5 * (1 + math.erf(z / math.sqrt(2)))))


@dataclass
class Order:
    side: str
    price: Decimal
    remaining: Decimal
    ready: float
    created: float
    mode: str = "maker"
    active: bool = False
    queue_ahead: Decimal = ZERO
    cancel_at: float | None = None


class Engine:
    def __init__(self, market, config=None):
        self.market = market
        self.config = config or Config()
        self.portfolio = Portfolio(self.config.capital)
        self.books = {"Up": Book(), "Down": Book()}
        self.model = ProbabilityModel(self.config)
        self.orders = []
        self.now = -math.inf
        self.last_decision = -math.inf
        self.halted = None
        self.q = None
        self.actions = []
        self.events = 0
        self.trade_ids = set()
        self.spot_source = None
        self.ticks = {"Up": D(market.tick), "Down": D(market.tick)}

    def log(self, action, **details):
        self.actions.append(dict(ts=self.now, action=action, **details))

    def cancel(self, immediate=False):
        for order in self.orders:
            if order.cancel_at is None:
                order.cancel_at = self.now + (0 if immediate else self.config.cancel_latency)
                self.log("cancel_requested", side=order.side)
        if immediate:
            self.orders.clear()

    def ingest(self, event):
        ts = float(event["ts"])
        if not math.isfinite(ts) or ts < self.now:
            raise ValueError("Events must be ordered by receive time; replay will not sort them")
        self.now = ts
        self.events += 1
        kind = event["kind"]
        if kind not in {"spot", "book", "delta", "tick", "resolution", "disconnect", "feed_reset", "trade", "clock", "session_stop"}:
            raise ValueError(f"Unknown event kind: {kind}")
        # Apply the observation first: delayed orders use the book available at arrival.
        if kind == "spot":
            self.model.update(ts, float(event["price"]), float(event.get("source_ts", ts)))
            self.spot_source = event.get("feed", "unspecified")
        elif kind in ("book", "delta"):
            self.books[self.market.outcome(event["token"])].update(event)
        elif kind == "tick":
            side = self.market.outcome(event["token"])
            tick = D(event["tick"])
            if not ZERO < tick < ONE:
                raise ValueError("Invalid tick")
            self.ticks[side] = tick
            for order in self.orders:
                if order.side == side and order.cancel_at is None:
                    order.cancel_at = self.now + self.config.cancel_latency
                    self.log("cancel_requested", side=side, reason="tick_changed")
        elif kind == "resolution":
            if ts < self.market.end:
                raise ValueError("Resolution before market end")
            if not self.portfolio.settled:
                self.portfolio.settle(self.market.outcome(event["winner"]))
                self.cancel(immediate=True)
                self.log("settled", winner=event["winner"])
        elif kind == "session_stop":
            self.halted = "paper_session_finished"
            self.cancel(immediate=True)
        elif kind in ("disconnect", "feed_reset"):
            if kind == "disconnect":
                self.halted = "feed_disconnected"
            self.cancel(immediate=True)
            for book in self.books.values():
                book.initialized = False
                book.bids.clear()
                book.asks.clear()
        self.q = self.model.probability(ts, self.market)
        healthy = (self.q is not None and all(b.fresh(ts, self.config.max_feed_age) for b in self.books.values()))
        # Expiry takes effect even without new book messages. Existing quotes can fill
        # during cancellation latency; stale feeds never authorize new fills.
        if not healthy or ts >= self.market.end - 2:
            self.cancel()
        self.advance(healthy)
        if kind == "trade" and healthy and not self.portfolio.settled and ts < self.market.end:
            self.trade(event)
        if not self.portfolio.settled:
            guaranteed = self.portfolio.cash + self.portfolio.paired
            if self.portfolio.initial - guaranteed >= D(self.config.max_loss):
                self.halted = "worst_case_loss_limit"
                self.cancel()
        if (healthy and not self.halted and not self.portfolio.settled
                and self.market.start <= ts < self.market.end - 2
                and ts - self.last_decision >= self.config.decision_interval):
            self.last_decision = ts
            self.decide()

    def advance(self, healthy):
        for order in list(self.orders):
            if order.cancel_at is not None and self.now >= order.cancel_at:
                self.orders.remove(order)
                continue
            if self.now >= self.market.end:
                self.orders.remove(order)
                continue
            if self.now - order.created >= self.config.quote_lifetime and order.cancel_at is None:
                order.cancel_at = self.now + self.config.cancel_latency
            if order.active or self.now < order.ready:
                continue
            if not healthy or self.halted:
                self.orders.remove(order)
                continue
            book = self.books[order.side]
            if order.mode == "taker":
                self.execute_taker(order)
                self.orders.remove(order)
            elif order.price >= book.ask:
                self.orders.remove(order)
                self.log("post_only_rejected", side=order.side)
            else:
                order.active = True
                order.queue_ahead = book.bids.get(order.price, ZERO)
                self.log("quote_active", side=order.side, price=str(order.price), queue=str(order.queue_ahead))

    def trade(self, event):
        side = self.market.outcome(event["token"])
        price, volume = D(event["price"]), D(event["size"])
        if not ZERO < price < ONE or volume <= ZERO:
            raise ValueError("Invalid trade")
        if event["side"] not in ("BUY", "SELL"):
            raise ValueError("Invalid aggressor side")
        identity = event.get("id")
        if identity:
            key = (event["token"], identity)
            if key in self.trade_ids:
                return
            self.trade_ids.add(key)
        if event["side"] != "SELL":
            return
        source = float(event.get("source_ts", self.now))
        if not -.5 <= self.now - source <= self.config.max_feed_age:
            return
        # Exact-price aggressive sells only. Never fill on a quote touch or a
        # book-size reduction; cancellations ahead do not improve queue position.
        for order in self.orders:
            if (not order.active or order.side != side or order.price != price
                    or order.mode != "maker" or source < order.ready):
                continue
            consumed = min(volume, order.queue_ahead)
            order.queue_ahead -= consumed
            volume -= consumed
            quantity = min(volume, order.remaining)
            if quantity > 0:
                self.fill(order, quantity, price)
                volume -= quantity
        self.orders = [order for order in self.orders if order.remaining > 0]

    def fill(self, order, quantity, price):
        fee = self.market.fee(price, quantity, order.mode == "maker")
        self.portfolio.buy(order.side, quantity, price, fee, self.now, order.mode)
        order.remaining -= quantity
        self.log("fill", side=order.side, shares=str(quantity), price=str(price), mode=order.mode)

    def execute_taker(self, order):
        book = self.books[order.side]
        fair = D(self.q if order.side == "Up" else 1 - self.q)
        # IOC: walk currently displayed asks, bounded by limit and remaining edge.
        for price in sorted(book.asks):
            if price > order.price or order.remaining <= 0:
                break
            all_in = price + self.market.fee(price, ONE) + D(self.config.slippage_buffer)
            if fair - all_in < D(self.config.taker_edge + self.config.uncertainty_buffer):
                break
            quantity = min(order.remaining, book.asks[price])
            if quantity > 0:
                self.fill(order, quantity, price)
                book.asks[price] -= quantity
        book.asks = {p: s for p, s in book.asks.items() if s > 0}

    def decide(self):
        c, p = self.config, self.portfolio
        target = D((2 * self.q - 1) * c.max_net_shares)
        for order in self.orders:
            fair = D(self.q if order.side == "Up" else 1 - self.q)
            opposite = "Down" if order.side == "Up" else "Up"
            paired_edge = bool(p.lots[opposite] and p.lots[opposite][0].cost + order.price
                               + self.market.fee(order.price, ONE, order.mode == "maker")
                               < ONE - D(c.pair_edge + c.uncertainty_buffer))
            sign = ONE if order.side == "Up" else -ONE
            threshold = c.maker_edge if order.mode == "maker" else c.taker_edge
            valid_edge = fair - order.price - self.market.fee(order.price, ONE, order.mode == "maker")
            if (not paired_edge and (valid_edge < D(threshold + c.uncertainty_buffer)
                                     or sign * (target - p.net) <= 0)) and order.cancel_at is None:
                order.cancel_at = self.now + c.cancel_latency
                self.log("cancel_requested", side=order.side, reason="signal_changed")
        for side in ("Up", "Down"):
            tick = self.ticks[side]
            if any(o.side == side for o in self.orders):
                continue
            sign = ONE if side == "Up" else -ONE
            effective_net = p.net + sum((o.remaining if o.side == "Up" else -o.remaining for o in self.orders), ZERO)
            need = sign * (target - effective_net)
            book = self.books[side]
            fair = D(self.q if side == "Up" else 1 - self.q)
            price = min(book.bid, book.ask - tick)
            price = (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            if price <= 0:
                continue
            opposite = "Down" if side == "Up" else "Up"
            hedge_size = p.residual(opposite)
            pair_good = bool(p.lots[opposite] and
                             p.lots[opposite][0].cost + price + self.market.fee(price, ONE, True)
                             < ONE - D(c.pair_edge + c.uncertainty_buffer))
            maker_good = fair - price - self.market.fee(price, ONE, True) >= D(c.maker_edge + c.uncertainty_buffer)
            mode = "maker"
            if c.allow_taker and need > 0:
                ask_cost = book.ask + self.market.fee(book.ask, ONE) + D(c.slippage_buffer)
                if fair - ask_cost >= D(c.taker_edge + c.uncertainty_buffer):
                    price = min(ONE - tick, book.ask + D(c.slippage_buffer))
                    price = (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
                    mode = "taker"
            if price > D(c.max_entry_price):
                continue
            if mode == "maker" and not ((maker_good and need > 0) or pair_good):
                continue
            quantity = max(need if maker_good or mode == "taker" else ZERO,
                           hedge_size if pair_good and mode == "maker" else ZERO)
            # Reserve all open/pending orders, including orders awaiting cancellation.
            reserve = sum((o.remaining * o.price + self.market.fee(o.price, o.remaining, o.mode == "maker")
                           for o in self.orders), ZERO)
            # Use the requested order amount when the signal qualifies. The
            # exchange minimum and the remaining market risk caps still apply.
            cap = min(D(c.order_dollars), p.cash - reserve,
                      D(c.max_market_spend) - p.spent - reserve,
                      D(c.max_loss) - (p.initial - p.cash - p.paired) - reserve)
            unit_cost = price + self.market.fee(price, ONE, mode == "maker")
            budget = cap
            room = D(c.max_net_shares) - sign * p.net - sum(
                (o.remaining for o in self.orders if o.side == side), ZERO)
            quantity = min(quantity, max(ZERO, room), max(ZERO, budget) / unit_cost)
            quantity = quantity.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            if quantity < D(self.market.min_size):
                continue
            delay = c.order_latency + (c.taker_delay if mode == "taker" else 0)
            self.orders.append(Order(side, price, quantity, self.now + delay, self.now, mode))
            self.log("order_created", side=side, shares=str(quantity), price=str(price), mode=mode,
                     probability_up=self.q, target_net=str(target))

    def report(self):
        return dict(market=asdict(self.market), config=asdict(self.config),
                    paper_only=True, events=self.events, halted=self.halted,
                    probability_up=self.q, open_orders=len(self.orders),
                    spot_source=self.spot_source,
                    tick_sizes={side: str(tick) for side, tick in self.ticks.items()},
                    portfolio=self.portfolio.report(self.books),
                    limitations=["Untrained probability model; performance is unproven.",
                                 "Verify the selected price stream and strike against the market's resolution rules.",
                                 "Queue is inferred from public L2 data; no rebates credited.",
                                 "Paired payoff is terminal value, not merged or available cash."],
                    fills=self.portfolio.fills, actions=self.actions)

