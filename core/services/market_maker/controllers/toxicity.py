from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from .base import (
    MarketFeatureSnapshotLike,
    QuoteControllerContext,
    QuoteControllerDecision,
    SideQuoteAdjustment,
)


_ZERO = Decimal("0")
_THREE = Decimal("3")
_SQRT_FIVE = Decimal("5").sqrt()
_MODES = frozenset({"fixed", "shadow", "active"})


def _finite_decimal(value: object, *, nonnegative: bool = False) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and (not nonnegative or value >= 0)
    )


@dataclass
class _SideBlockState:
    blocked: bool = False
    adverse_streak: int = 0
    resume_streak: int = 0
    blocked_since: float | None = None


class ToxicityAwareEntryQuoteController:
    def __init__(
        self,
        *,
        mode: str,
        min_signal_ticks: Decimal,
        widen_start_ticks: Decimal,
        max_extra_spread_ticks: int,
        block_threshold_ticks: Decimal,
        resume_threshold_ticks: Decimal,
        block_confirmations: int,
        resume_confirmations: int,
        min_block_seconds: int,
        use_markout_feedback: bool = False,
        markout_horizon_seconds: int = 5,
        markout_min_samples: int = 8,
    ) -> None:
        if mode not in _MODES:
            raise ValueError("mode must be fixed, shadow, or active")
        for name, value in (
            ("min_signal_ticks", min_signal_ticks),
            ("widen_start_ticks", widen_start_ticks),
            ("block_threshold_ticks", block_threshold_ticks),
            ("resume_threshold_ticks", resume_threshold_ticks),
        ):
            if not _finite_decimal(value, nonnegative=True):
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        for name, value in (
            ("block_confirmations", block_confirmations),
            ("resume_confirmations", resume_confirmations),
            ("min_block_seconds", min_block_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if block_confirmations > 3:
            raise ValueError(
                "block_confirmations cannot exceed the three directional signals"
            )
        if type(max_extra_spread_ticks) is not int or max_extra_spread_ticks < 0:
            raise ValueError("max_extra_spread_ticks must be an integer >= 0")
        if type(use_markout_feedback) is not bool:
            raise ValueError("use_markout_feedback must be a boolean")
        if markout_horizon_seconds not in {5, 15}:
            raise ValueError("markout_horizon_seconds must be 5 or 15")
        if type(markout_min_samples) is not int or markout_min_samples <= 0:
            raise ValueError("markout_min_samples must be a positive integer")
        if block_threshold_ticks > 0 and not (
            _ZERO <= resume_threshold_ticks
            < widen_start_ticks
            < block_threshold_ticks
        ):
            raise ValueError(
                "blocking requires 0 <= resume < widen_start < block"
            )
        if (
            mode == "active"
            and max_extra_spread_ticks == 0
            and block_threshold_ticks == 0
        ):
            raise ValueError("active mode requires widening or blocking")
        if mode == "active" and use_markout_feedback:
            raise ValueError("active mode cannot use markout feedback in v1")

        self.mode = mode
        self.min_signal_ticks = min_signal_ticks
        self.widen_start_ticks = widen_start_ticks
        self.max_extra_spread_ticks = max_extra_spread_ticks
        self.block_threshold_ticks = block_threshold_ticks
        self.resume_threshold_ticks = resume_threshold_ticks
        self.block_confirmations = block_confirmations
        self.resume_confirmations = resume_confirmations
        self.min_block_seconds = min_block_seconds
        self.use_markout_feedback = use_markout_feedback
        self.markout_horizon_seconds = markout_horizon_seconds
        self.markout_min_samples = markout_min_samples
        self._decision_id = 0
        self._bid_state = _SideBlockState()
        self._ask_state = _SideBlockState()

    @classmethod
    def from_config(cls, config: object) -> "ToxicityAwareEntryQuoteController":
        return cls(
            mode=getattr(config, "quote_controller_mode"),
            min_signal_ticks=getattr(config, "toxicity_min_signal_ticks"),
            widen_start_ticks=getattr(config, "toxicity_widen_start_ticks"),
            max_extra_spread_ticks=getattr(
                config, "toxicity_max_extra_spread_ticks"
            ),
            block_threshold_ticks=getattr(
                config, "toxicity_block_threshold_ticks"
            ),
            resume_threshold_ticks=getattr(
                config, "toxicity_resume_threshold_ticks"
            ),
            block_confirmations=getattr(config, "toxicity_block_confirmations"),
            resume_confirmations=getattr(
                config, "toxicity_resume_confirmations"
            ),
            min_block_seconds=getattr(config, "toxicity_min_block_seconds"),
            use_markout_feedback=getattr(
                config, "toxicity_use_markout_feedback"
            ),
            markout_horizon_seconds=getattr(
                config, "toxicity_markout_horizon_seconds"
            ),
            markout_min_samples=getattr(
                config, "toxicity_markout_min_samples"
            ),
        )

    def evaluate(self, context: QuoteControllerContext) -> QuoteControllerDecision:
        self._decision_id += 1
        if (
            isinstance(context.now_monotonic, bool)
            or not isinstance(context.now_monotonic, (int, float))
            or not math.isfinite(context.now_monotonic)
        ):
            return self._unready(context.features, "invalid_time")

        feature_error = self._feature_error(context.features)
        if feature_error is not None:
            self._bid_state.adverse_streak = 0
            self._bid_state.resume_streak = 0
            self._ask_state.adverse_streak = 0
            self._ask_state.resume_streak = 0
            return self._unready(context.features, feature_error)

        features = context.features
        assert features is not None
        m5 = features.return_5s_ticks
        m15 = features.return_15s_ticks / _THREE
        microprice = features.microprice_shift_ticks
        rms = features.rms_1s_move_15s_ticks
        assert m5 is not None and microprice is not None and rms is not None

        directional_pressure = sorted((m5, m15, microprice))[1]
        volatility_buffer = rms * _SQRT_FIVE
        buy_score = max(volatility_buffer, max(_ZERO, -directional_pressure))
        ask_score = max(volatility_buffer, max(_ZERO, directional_pressure))
        buy_score = self._with_markout_penalty(
            buy_score, "buy", context, features
        )
        ask_score = self._with_markout_penalty(
            ask_score, "sell", context, features
        )
        buy_confirmations = sum(
            value < -self.min_signal_ticks for value in (m5, m15, microprice)
        )
        ask_confirmations = sum(
            value > self.min_signal_ticks for value in (m5, m15, microprice)
        )

        bid = self._adjust_side(
            self._bid_state,
            buy_score,
            buy_confirmations,
            float(context.now_monotonic),
        )
        ask = self._adjust_side(
            self._ask_state,
            ask_score,
            ask_confirmations,
            float(context.now_monotonic),
        )
        return QuoteControllerDecision(
            mode=self.mode,
            controller="toxicity_v1",
            ready=True,
            reason="toxicity_ready",
            decision_id=self._decision_id,
            bid=bid,
            ask=ask,
            features=features,
        )

    @staticmethod
    def _feature_error(features: MarketFeatureSnapshotLike | None) -> str | None:
        if features is None:
            return "features_missing"
        health = getattr(features, "health", None)
        health_value = getattr(health, "value", health)
        if health_value != "ready":
            if health_value in {"warming", "stale"}:
                return f"features_{health_value}"
            return "features_invalid"
        required = (
            getattr(features, "return_5s_ticks", None),
            getattr(features, "return_15s_ticks", None),
            getattr(features, "rms_1s_move_15s_ticks", None),
            getattr(features, "microprice_shift_ticks", None),
        )
        if not all(_finite_decimal(value) for value in required):
            return "features_invalid"
        if required[2] < 0:
            return "features_invalid"
        return None

    def _with_markout_penalty(
        self,
        score: Decimal,
        side: str,
        context: QuoteControllerContext,
        features: MarketFeatureSnapshotLike,
    ) -> Decimal:
        if not self.use_markout_feedback:
            return score
        feedback = context.entry_markout_feedback
        if not isinstance(feedback, dict):
            return score
        side_feedback = feedback.get(side)
        if not isinstance(side_feedback, dict):
            return score
        stats = side_feedback.get(f"{self.markout_horizon_seconds}s")
        if not isinstance(stats, dict):
            return score
        count = stats.get("count")
        markout_bps = stats.get("ewma_bps")
        mid = getattr(features, "mid", None)
        tick = getattr(context.metadata, "price_tick", None)
        if (
            type(count) is not int
            or count < self.markout_min_samples
            or not _finite_decimal(markout_bps)
            or not _finite_decimal(mid)
            or mid <= 0
            or not _finite_decimal(tick)
            or tick <= 0
            or markout_bps >= 0
        ):
            return score
        penalty_ticks = -markout_bps / Decimal("10000") * mid / tick
        return max(score, penalty_ticks)

    def _unready(
        self,
        features: MarketFeatureSnapshotLike | None,
        reason: str,
    ) -> QuoteControllerDecision:
        return QuoteControllerDecision(
            mode=self.mode,
            controller="toxicity_v1",
            ready=False,
            reason=reason,
            decision_id=self._decision_id,
            bid=SideQuoteAdjustment(
                blocked=self._bid_state.blocked,
                reason=reason,
            ),
            ask=SideQuoteAdjustment(
                blocked=self._ask_state.blocked,
                reason=reason,
            ),
            features=features,
        )

    def _adjust_side(
        self,
        state: _SideBlockState,
        score: Decimal,
        directional_confirmations: int,
        now_monotonic: float,
    ) -> SideQuoteAdjustment:
        extra_ticks = 0
        if score > self.widen_start_ticks:
            extra_ticks = int(
                (score - self.widen_start_ticks).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            extra_ticks = min(extra_ticks, self.max_extra_spread_ticks)

        reason = "toxicity_widened" if extra_ticks else "none"
        if self.block_threshold_ticks == 0:
            state.blocked = False
            state.adverse_streak = 0
            state.resume_streak = 0
            state.blocked_since = None
        elif not state.blocked:
            adverse = (
                score >= self.block_threshold_ticks
                and directional_confirmations >= self.block_confirmations
            )
            state.adverse_streak = state.adverse_streak + 1 if adverse else 0
            if state.adverse_streak >= self.block_confirmations:
                state.blocked = True
                state.blocked_since = now_monotonic
                state.resume_streak = 0
                reason = "toxicity_blocked"
        else:
            held_long_enough = (
                state.blocked_since is not None
                and now_monotonic - state.blocked_since >= self.min_block_seconds
            )
            resume = held_long_enough and score < self.resume_threshold_ticks
            state.resume_streak = state.resume_streak + 1 if resume else 0
            if state.resume_streak >= self.resume_confirmations:
                state.blocked = False
                state.adverse_streak = 0
                state.resume_streak = 0
                state.blocked_since = None
                reason = "toxicity_resumed"
            else:
                reason = "toxicity_blocked"

        return SideQuoteAdjustment(
            extra_spread_ticks=extra_ticks,
            blocked=state.blocked,
            toxicity_score_ticks=score,
            directional_confirmations=directional_confirmations,
            reason=reason,
        )
