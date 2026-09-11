"""Count attempted wire requests, including failed reads and protocol frames."""

from collections import Counter, deque
from decimal import Decimal, localcontext
from fractions import Fraction
from math import isfinite
from urllib.parse import urlsplit


class ApiBudgetUnavailable(RuntimeError):
    """The next ordinary action would spend capacity reserved for bounded exit."""

    def __init__(self, message, *, values=None, operation="normal", blocker=None):
        super().__init__(message)
        self.diagnostic_values = values or {}
        self.operation = operation
        self.blocking_bucket = self.blocking_offset_seconds = None
        self.projected_usage = self.limit = None
        if blocker is not None:
            self.blocking_bucket, offset, self.projected_usage, self.limit = blocker
            # Checkpoints are half seconds or differences of binary clock values;
            # their rational offsets have finite, exactly representable decimals.
            with localcontext() as context:
                context.prec = len(str(offset.numerator)) + offset.denominator.bit_length() + 2
                self.blocking_offset_seconds = Decimal(offset.numerator) / Decimal(offset.denominator)

    @property
    def budget_diagnostic(self):
        return {"operation": self.operation, "blocking_bucket": self.blocking_bucket,
                "blocking_offset_seconds": (None if self.blocking_offset_seconds is None
                                            else str(self.blocking_offset_seconds)),
                "projected_usage": self.projected_usage, "limit": self.limit}


class ApiBudget:
    """One session's rolling minute, not evidence of other IP/L1 consumers."""

    LIMITS = {"rest": 24000, "ws": 200, "tx": 40}
    WEIGHTS = {"nextNonce": 6, "accountInactiveOrders": 100, "apikeys": 150,
               "trades": 600, "recentTrades": 600,
               **dict.fromkeys(("account", "accountLimits", "assetDetails", "positionFunding",
                                "orderBooks", "orderBookDetails", "accountActiveOrders", "fundings"), 300)}

    def __init__(self, clock, *, work_phase=lambda: "unclassified"):
        self.clock = clock
        self.work_phase = work_phase
        self.rest_weight_by_phase = Counter()
        self._events = deque()
        self._used = Counter()
        self.peaks = Counter()
        self.counts = Counter()
        self.deferrals = 0
        self.optional_waits = 0
        self.account_read_deferrals = 0
        self.account_profile = None
        self._backpressure_exits = deque(maxlen=64)
        self._account_read_exits = deque(maxlen=64)
        self._admission_denials = deque(maxlen=64)
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
        if bucket == "rest":
            phase = self.work_phase()
            self.rest_weight_by_phase[phase if phase in {"startup", "normal", "exit"}
                                      else "unclassified"] += weight

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
        return self._scheduled_live_blocker(normal) is None

    def _scheduled_live_blocker(self, normal):
        reserve = {"rest": 8806, "ws": 67, "tx": 5}
        if not self.available(normal=normal, reserve=reserve):
            for bucket, limit in self.LIMITS.items():
                projected = self._used[bucket] + normal[bucket] + reserve[bucket]
                if projected > limit:
                    return bucket, Fraction(0), projected, limit
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
                return "rest", elapsed, used + normal["rest"] + prefix, self.LIMITS["rest"]
            while expiries and expiries[0][0] <= elapsed:
                used -= expiries.popleft()[1]
            prefix = (6006 + 100 * min(33, int(2 * elapsed))
                      + 900 * min(5, 1 + int(elapsed / 8)) + 900 + 1000)
            if used + normal["rest"] + prefix > self.LIMITS["rest"]:
                return "rest", elapsed, used + normal["rest"] + prefix, self.LIMITS["rest"]
        return None

    def require_normal(self, normal, *, operation="normal"):
        """Call before an ordinary action, never as a post-send confirmation gate."""
        if (type(operation) is not str or not operation or len(operation) > 64
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in operation)):
            raise ValueError("API operation must be a bounded local code label")
        blocker = self._scheduled_live_blocker(normal)
        if blocker is not None:
            error = ApiBudgetUnavailable("API capacity reserved for bounded exit", values={
                **{f"api_used_{key}": Decimal(self._used[key]) for key in self.LIMITS},
                **{f"api_next_{key}": Decimal(normal[key]) for key in self.LIMITS}},
                operation=operation, blocker=blocker)
            self._admission_denials.append({"observed_monotonic": self._last,
                                           **error.budget_diagnostic})
            raise error

    def record_backpressure_exit(self, *, phase, exit_id, error):
        """Keep bounded, sanitized attribution; account-read races are separate."""
        now = self._expire()
        self.deferrals += 1
        self._backpressure_exits.append({
            "number": self.deferrals, "observed_monotonic": now,
            "reason": "local_api_budget", "phase": phase, "exit_id": exit_id,
            **error.budget_diagnostic,
            "used": {key: int(error.diagnostic_values.get(f"api_used_{key}", self._used[key]))
                     for key in self.LIMITS},
            "next": {key: int(error.diagnostic_values[f"api_next_{key}"])
                     for key in self.LIMITS if f"api_next_{key}" in error.diagnostic_values}})
        return sum(row["observed_monotonic"] >= now - 600 for row in self._backpressure_exits)

    def require_flat_read(self, normal, *, operation="flat_read"):
        """Admit read-only recovery after proven cleanup, preserving a final audit.

        A fresh cash audit has at most two attempts: each cash300 + fees900
        (including one public funding round) + settlement300 + trades600 +
        terminal history100. Each attempt has five WS frames; keep five more
        for control traffic. Caller must prove flat/empty and forbid mutations.
        """
        if (type(operation) is not str or not operation or len(operation) > 64
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in operation)):
            raise ValueError("API operation must be a bounded local code label")
        reserve = {"rest": 4400, "ws": 15, "tx": 0}
        allowed = self.available(normal=normal, reserve=reserve)
        if normal["tx"] != 0:
            raise ValueError("flat proof admission cannot include mutations")
        if not allowed:
            blocker = next((key, Fraction(0), self._used[key] + normal[key] + reserve[key], limit)
                           for key, limit in self.LIMITS.items()
                           if self._used[key] + normal[key] + reserve[key] > limit)
            error = ApiBudgetUnavailable("API capacity reserved for final flat proof", values={
                **{f"api_used_{key}": Decimal(self._used[key]) for key in self.LIMITS},
                **{f"api_next_{key}": Decimal(normal[key]) for key in self.LIMITS}},
                operation=operation, blocker=blocker)
            self._admission_denials.append({"observed_monotonic": self._last,
                                           **error.budget_diagnostic})
            raise error

    def record_account_read_exit(self, *, phase, exit_id, error):
        """Attribute exhausted account proofs without retaining exception payloads."""
        from .lighter_runtime import AccountReadRace, _AccountCashRace, UnattributedCashflow

        if type(error) not in {AccountReadRace, _AccountCashRace, UnattributedCashflow}:
            raise ValueError("account read race required")
        if (phase not in {"syncing_orders", "authorizing_quotes", "reconciling_quotes"}
                or type(exit_id) is not str or not exit_id.startswith("exit-")
                or not 1 <= len(exit_id[5:]) <= 20
                or any(character not in "0123456789" for character in exit_id[5:])):
            raise ValueError("bounded account read exit labels required")
        # These exact messages belong to our account-proof implementation.
        # Unknown messages remain useful as a class without becoming log text.
        reasons = {
            "exact terminal order proof unavailable": "terminal_order_history",
            "terminal fills not reflected in account ledger": "terminal_fill_history",
            "account changed during stream/REST bracket": "stream_rest_bracket",
            "account trade count and history disagree": "trade_counter_history",
            "account history exceeds activity counter": "history_ahead_of_counter",
            "active order fills not reflected in account history": "active_fill_history",
            "account fills and position disagree": "fill_position",
            "new fill exceeds observed fee terms": "fill_fee_terms",
            "unattributed account cashflow or equity mismatch": "cash_equity",
        }
        message = error.args[0] if error.args and type(error.args[0]) is str else None
        self.account_read_deferrals += 1
        self._account_read_exits.append({
            "number": self.account_read_deferrals, "observed_monotonic": self._expire(),
            "reason": "account_cash_conflict" if isinstance(error, _AccountCashRace) else "account_read_race",
            "subreason": reasons.get(message, "unclassified_account_read_race"),
            "phase": phase, "exit_id": exit_id,
        })

    def snapshot(self):
        self._expire()
        return {"used": dict(self._used), "peaks": dict(self.peaks), "limits": dict(self.LIMITS),
                "rest_weight_by_phase": dict(self.rest_weight_by_phase),
                "account_profile": self.account_profile,
                "attempts": dict(self.counts), "scope": "owned_python_transports",
                "deferrals": self.deferrals, "optional_waits": self.optional_waits,
                "account_read_deferrals": self.account_read_deferrals,
                "backpressure_window_seconds": 600,
                "recent_admission_denials": list(self._admission_denials),
                "recent_account_read_exits": list(self._account_read_exits),
                "recent_backpressure_exits": list(self._backpressure_exits)}
