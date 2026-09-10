"""Count attempted wire requests, including failed reads and protocol frames."""

from collections import Counter, deque
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from urllib.parse import urlsplit


class ApiBudgetUnavailable(RuntimeError):
    """The next ordinary action would spend capacity reserved for bounded exit."""

    def __init__(self, message, *, values=None):
        super().__init__(message)
        self.diagnostic_values = values or {}


class ApiBudget:
    """One session's rolling minute, not evidence of other IP/L1 consumers."""

    LIMITS = {"rest": 24000, "ws": 200, "tx": 40}
    WEIGHTS = {"nextNonce": 6, "accountInactiveOrders": 100, "apikeys": 150,
               "trades": 600, "recentTrades": 600,
               **dict.fromkeys(("account", "accountLimits", "assetDetails", "positionFunding",
                                "orderBooks", "orderBookDetails", "accountActiveOrders", "fundings"), 300)}

    def __init__(self, clock):
        self.clock = clock
        self._events = deque()
        self._used = Counter()
        self.peaks = Counter()
        self.counts = Counter()
        self.deferrals = 0
        self.account_read_deferrals = 0
        self._last = 0

    def _expire(self):
        now = self.clock()
        if not isfinite(now) or now < self._last:
            raise RuntimeError("API accounting clock unavailable")
        self._last = now
        while self._events and self._events[0][0] <= now - 60:
            _, bucket, weight = self._events.popleft()
            self._used[bucket] -= weight
        return now

    def observe(self, transport, target):
        now = self._expire()
        if transport == "rest":
            # Never retain URL query strings, headers, bodies or auth material.
            endpoint = urlsplit(target).path.rsplit("/", 1)[-1]
            bucket = "tx" if endpoint in {"sendTx", "sendTxBatch"} else "rest"
            if bucket == "rest" and endpoint not in self.WEIGHTS:
                raise RuntimeError("unclassified API accounting endpoint")
            weight = 1 if bucket == "tx" else self.WEIGHTS[endpoint]
            name = "rest:" + endpoint
        elif transport == "ws" and type(target) is int and target in {0, 1, 2, 8, 9, 10}:
            bucket, weight, name = "ws", 1, "ws:" + str(target)
        else:
            raise RuntimeError("unsupported API accounting transport")
        self._events.append((now, bucket, weight))
        self._used[bucket] += weight
        self.peaks[bucket] = max(self.peaks[bucket], self._used[bucket])
        self.counts[name] += 1

    def available(self, *, normal, reserve):
        self._expire()
        if set(normal) != set(self.LIMITS) or set(reserve) != set(self.LIMITS):
            raise ValueError("complete API cost bounds required")
        if any(type(v) is not int or v < 0 for v in (*normal.values(), *reserve.values())):
            raise ValueError("nonnegative integral API costs required")
        return all(self._used[key] + normal[key] + reserve[key] <= limit
                   for key, limit in self.LIMITS.items())

    def scheduled_live_available(self, normal):
        """Check a healthy 3-IOC exit (30s) and final proof (10s) after this action.

        REST immediate 6006 = 8 cash reads*300 + 4 trade reads*600 + 9 first
        history reads*100 + market metadata 300 + nonce 6. Add 33 extra history
        polls (at least 0.5s apart) and five 8s metadata refreshes.
        Include one forced funding refresh (600), one exact public funding round (300), and one
        coherent-read retry (1000, including terminal history) immediately. WS reserves 52 proof frames,
        five retry frames and ten control frames; TX reserves two cancels and
        three IOC sends. This is not a repeated-race/429 or network-success
        guarantee; other IP/L1 consumers are outside this meter's scope.
        """
        if not self.available(normal=normal, reserve={"rest": 8806, "ws": 67, "tx": 5}):
            return False
        now = Fraction(self._last)
        expiries = deque((Fraction(at) + 60 - now, weight)
                         for at, bucket, weight in self._events if bucket == "rest")
        # Exact rational boundaries prevent crediting a just-unexpired request.
        points = {Fraction(step, 2) for step in range(34)} | {8, 16, 24, 32, 40}
        points.update(at for at, _ in expiries if at <= 40)
        used, prefix = self._used["rest"], 8806
        for elapsed in sorted(points):
            # Before this boundary the preceding prefix still applies. Expiry
            # happens before a coincident history/metadata step takes effect.
            if used + normal["rest"] + prefix > self.LIMITS["rest"]:
                return False
            while expiries and expiries[0][0] <= elapsed:
                used -= expiries.popleft()[1]
            prefix = (6006 + 100 * min(33, int(2 * elapsed))
                      + 900 * min(5, 1 + int(elapsed / 8)) + 900 + 1000)
            if used + normal["rest"] + prefix > self.LIMITS["rest"]:
                return False
        return True

    def require_normal(self, normal):
        """Call before an ordinary action, never as a post-send confirmation gate."""
        if not self.scheduled_live_available(normal):
            raise ApiBudgetUnavailable("API capacity reserved for bounded exit", values={
                **{f"api_used_{key}": Decimal(self._used[key]) for key in self.LIMITS},
                **{f"api_next_{key}": Decimal(normal[key]) for key in self.LIMITS}})

    def snapshot(self):
        self._expire()
        return {"used": dict(self._used), "peaks": dict(self.peaks),
                "attempts": dict(self.counts), "scope": "owned_python_transports",
                "deferrals": self.deferrals, "account_read_deferrals": self.account_read_deferrals}
