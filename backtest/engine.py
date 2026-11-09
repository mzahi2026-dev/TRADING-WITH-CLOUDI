"""
High-Fidelity Quantitative Order Execution and Backtest Simulator.

Supports:
- Phase 1: Baseline Static Stop Loss & Take Profit (1:2 R:R)
- Phase 2: Scaled Take Profit & Static Breakeven
- Phase 3: Scaled Take Profit (50% at 1.5 * ATR) + Intraday EMA20 Trailing Stop
- Phase 4 (Final Precision):
  * Limit Pullback Entry Model: 0.02% Maker Fee on entry fills
  * 0.05% Taker Fee on market exits
  * Upgraded TP1 Target: 2.0 * ATR (closing 50% of position)
  * Candle CLOSE Trailing Stop on Runner: only exits when 15m candle CLOSES across EMA20
  * Catastrophic capital protection against flash crashes
- Strict position sizing ($15 USDT risk, $200 USDT max notional cap)
- 0.02% slippage model on every fill
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import numpy as np
import pandas as pd


@dataclass
class EngineConfig:
    risk_per_trade_usdt: float = 15.0
    max_notional_usdt: float = 200.0
    maker_fee_pct: float = 0.0002             # 0.02% maker fee on entry limit fills
    taker_fee_pct: float = 0.0005             # 0.05% taker fee on market exits
    slippage_pct: float = 0.0002              # 0.02% slippage
    initial_capital_usdt: float = 1000.0
    mode: str = "phase4"                      # 'phase1', 'phase2', 'phase3', 'phase4'
    tp1_close_pct: float = 0.50               # 50% closed at TP1
    trail_buffer_atr_mult: float = 0.2        # Trailing buffer


@dataclass
class Trade:
    symbol: str
    direction: str          # 'LONG' or 'SHORT'
    pattern: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float       # Final or weighted exit price
    qty: float
    notional: float
    risk_amount: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    total_fee: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    exit_reason: str        # 'RUNNER_CLOSE_EMA20', 'RUNNER_CATASTROPHIC_SL', 'SL_FULL', 'SL', 'TP'
    duration_bars: int


class BacktestEngine:
    """
    Simulates realistic market order execution with maker/taker fees, slippage,
    scaled profit taking, and candle-close EMA20 trailing stops.
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()

    def run_symbol(self, df: pd.DataFrame) -> List[Trade]:
        """
        Run backtest on a single symbol DataFrame containing signals and market bars.
        """
        trades: List[Trade] = []
        n = len(df)
        if n < 2:
            return trades

        symbol = df["symbol"].iloc[0] if "symbol" in df.columns else "UNKNOWN"

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        timestamps = df["timestamp"].values
        ema20 = df["ema20"].values
        atr14 = df["atr14"].values

        signals = df["signal"].values
        patterns = df["pattern"].values
        sl_dists = df["sl_dist"].values

        has_tp1 = "tp1_dist" in df.columns
        tp1_dists = df["tp1_dist"].values if has_tp1 else sl_dists
        tp2_dists = df["tp2_dist"].values if "tp2_dist" in df.columns else df["tp_dist"].values

        cfg = self.config
        maker_fee = cfg.maker_fee_pct
        taker_fee = cfg.taker_fee_pct
        slip_pct = cfg.slippage_pct
        risk_usdt = cfg.risk_per_trade_usdt
        max_notional = cfg.max_notional_usdt
        mode = cfg.mode

        in_pos = False
        pos_dir = 0
        entry_idx = 0
        entry_price = 0.0
        sl_price = 0.0
        tp1_price = 0.0
        tp2_price = 0.0
        qty_total = 0.0
        qty_rem = 0.0
        pattern = ""
        trade_risk = 0.0

        # Scaled TP and Trailing Stop tracking
        tp1_hit = False
        tp1_gross = 0.0
        tp1_fees = 0.0
        tp1_fill_p = 0.0

        for i in range(n - 1):
            if in_pos:
                bar_o = opens[i]
                bar_h = highs[i]
                bar_l = lows[i]
                bar_c = closes[i]
                current_ema20 = ema20[i]
                current_atr = atr14[i]

                trade_closed = False
                total_gross = 0.0
                total_exit_fee = 0.0
                exit_price_final = 0.0
                exit_reason = ""

                # --- MODE: PHASE 1 (Static 1:2 R:R Baseline) ---
                if mode == "phase1":
                    if pos_dir == 1:
                        sl_hit = bar_l <= sl_price
                        tp_hit = bar_h >= tp2_price
                        if sl_hit and tp_hit:
                            trade_closed = True
                            fill_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                            exit_price_final = fill_raw * (1.0 - slip_pct)
                            exit_reason = "SL"
                        elif sl_hit:
                            trade_closed = True
                            fill_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                            exit_price_final = fill_raw * (1.0 - slip_pct)
                            exit_reason = "SL"
                        elif tp_hit:
                            trade_closed = True
                            fill_raw = max(bar_o, tp2_price) if bar_o >= tp2_price else tp2_price
                            exit_price_final = fill_raw * (1.0 - slip_pct)
                            exit_reason = "TP"

                        if trade_closed:
                            total_gross = (exit_price_final - entry_price) * qty_total
                            total_exit_fee = exit_price_final * qty_total * taker_fee

                    else: # SHORT
                        sl_hit = bar_h >= sl_price
                        tp_hit = bar_l <= tp2_price
                        if sl_hit and tp_hit:
                            trade_closed = True
                            fill_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                            exit_price_final = fill_raw * (1.0 + slip_pct)
                            exit_reason = "SL"
                        elif sl_hit:
                            trade_closed = True
                            fill_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                            exit_price_final = fill_raw * (1.0 + slip_pct)
                            exit_reason = "SL"
                        elif tp_hit:
                            trade_closed = True
                            fill_raw = min(bar_o, tp2_price) if bar_o <= tp2_price else tp2_price
                            exit_price_final = fill_raw * (1.0 + slip_pct)
                            exit_reason = "TP"

                        if trade_closed:
                            total_gross = (entry_price - exit_price_final) * qty_total
                            total_exit_fee = exit_price_final * qty_total * taker_fee

                # --- MODE: PHASE 2 (Scaled TP + Static Breakeven) ---
                elif mode == "phase2":
                    if pos_dir == 1:
                        if not tp1_hit:
                            sl_hit = bar_l <= sl_price
                            tp1_hit_bar = bar_h >= tp1_price
                            if sl_hit and tp1_hit_bar:
                                trade_closed = True
                                fill_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                                exit_price_final = fill_raw * (1.0 - slip_pct)
                                total_gross = (exit_price_final - entry_price) * qty_total
                                total_exit_fee = exit_price_final * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif sl_hit:
                                trade_closed = True
                                fill_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                                exit_price_final = fill_raw * (1.0 - slip_pct)
                                total_gross = (exit_price_final - entry_price) * qty_total
                                total_exit_fee = exit_price_final * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif tp1_hit_bar:
                                tp1_hit = True
                                fill_tp1_raw = max(bar_o, tp1_price) if bar_o >= tp1_price else tp1_price
                                tp1_fill_p = fill_tp1_raw * (1.0 - slip_pct)
                                qty1 = qty_total * cfg.tp1_close_pct
                                qty_rem = qty_total - qty1
                                tp1_gross = (tp1_fill_p - entry_price) * qty1
                                tp1_fees = tp1_fill_p * qty1 * taker_fee
                                sl_price = entry_price
                                if bar_h >= tp2_price:
                                    trade_closed = True
                                    fill_tp2_raw = max(bar_o, tp2_price) if bar_o >= tp2_price else tp2_price
                                    tp2_fill_p = fill_tp2_raw * (1.0 - slip_pct)
                                    tp2_gross = (tp2_fill_p - entry_price) * qty_rem
                                    tp2_fees = tp2_fill_p * qty_rem * taker_fee
                                    total_gross = tp1_gross + tp2_gross
                                    total_exit_fee = tp1_fees + tp2_fees
                                    exit_price_final = (tp1_fill_p * qty1 + tp2_fill_p * qty_rem) / qty_total
                                    exit_reason = "TP1_AND_TP2"
                                elif bar_l <= sl_price:
                                    trade_closed = True
                                    fill_be_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                                    be_fill_p = fill_be_raw * (1.0 - slip_pct)
                                    be_gross = (be_fill_p - entry_price) * qty_rem
                                    be_fees = be_fill_p * qty_rem * taker_fee
                                    total_gross = tp1_gross + be_gross
                                    total_exit_fee = tp1_fees + be_fees
                                    exit_price_final = (tp1_fill_p * qty1 + be_fill_p * qty_rem) / qty_total
                                    exit_reason = "TP1_THEN_BE"
                        else:
                            if bar_h >= tp2_price:
                                trade_closed = True
                                fill_tp2_raw = max(bar_o, tp2_price) if bar_o >= tp2_price else tp2_price
                                tp2_fill_p = fill_tp2_raw * (1.0 - slip_pct)
                                tp2_gross = (tp2_fill_p - entry_price) * qty_rem
                                tp2_fees = tp2_fill_p * qty_rem * taker_fee
                                total_gross = tp1_gross + tp2_gross
                                total_exit_fee = tp1_fees + tp2_fees
                                qty1 = qty_total - qty_rem
                                exit_price_final = (tp1_fill_p * qty1 + tp2_fill_p * qty_rem) / qty_total
                                exit_reason = "TP2"
                            elif bar_l <= sl_price:
                                trade_closed = True
                                fill_be_raw = min(bar_o, sl_price) if bar_o <= sl_price else sl_price
                                be_fill_p = fill_be_raw * (1.0 - slip_pct)
                                be_gross = (be_fill_p - entry_price) * qty_rem
                                be_fees = be_fill_p * qty_rem * taker_fee
                                total_gross = tp1_gross + be_gross
                                total_exit_fee = tp1_fees + be_fees
                                qty1 = qty_total - qty_rem
                                exit_price_final = (tp1_fill_p * qty1 + be_fill_p * qty_rem) / qty_total
                                exit_reason = "BE"

                    else: # SHORT
                        if not tp1_hit:
                            sl_hit = bar_h >= sl_price
                            tp1_hit_bar = bar_l <= tp1_price
                            if sl_hit and tp1_hit_bar:
                                trade_closed = True
                                fill_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                                exit_price_final = fill_raw * (1.0 + slip_pct)
                                total_gross = (entry_price - exit_price_final) * qty_total
                                total_exit_fee = exit_price_final * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif sl_hit:
                                trade_closed = True
                                fill_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                                exit_price_final = fill_raw * (1.0 + slip_pct)
                                total_gross = (entry_price - exit_price_final) * qty_total
                                total_exit_fee = exit_price_final * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif tp1_hit_bar:
                                tp1_hit = True
                                fill_tp1_raw = min(bar_o, tp1_price) if bar_o <= tp1_price else tp1_price
                                tp1_fill_p = fill_tp1_raw * (1.0 + slip_pct)
                                qty1 = qty_total * cfg.tp1_close_pct
                                qty_rem = qty_total - qty1
                                tp1_gross = (entry_price - tp1_fill_p) * qty1
                                tp1_fees = tp1_fill_p * qty1 * taker_fee
                                sl_price = entry_price
                                if bar_l <= tp2_price:
                                    trade_closed = True
                                    fill_tp2_raw = min(bar_o, tp2_price) if bar_o <= tp2_price else tp2_price
                                    tp2_fill_p = fill_tp2_raw * (1.0 + slip_pct)
                                    tp2_gross = (entry_price - tp2_fill_p) * qty_rem
                                    tp2_fees = tp2_fill_p * qty_rem * taker_fee
                                    total_gross = tp1_gross + tp2_gross
                                    total_exit_fee = tp1_fees + tp2_fees
                                    exit_price_final = (tp1_fill_p * qty1 + tp2_fill_p * qty_rem) / qty_total
                                    exit_reason = "TP1_AND_TP2"
                                elif bar_h >= sl_price:
                                    trade_closed = True
                                    fill_be_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                                    be_fill_p = fill_be_raw * (1.0 + slip_pct)
                                    be_gross = (entry_price - be_fill_p) * qty_rem
                                    be_fees = be_fill_p * qty_rem * taker_fee
                                    total_gross = tp1_gross + be_gross
                                    total_exit_fee = tp1_fees + be_fees
                                    exit_price_final = (tp1_fill_p * qty1 + be_fill_p * qty_rem) / qty_total
                                    exit_reason = "TP1_THEN_BE"
                        else:
                            if bar_l <= tp2_price:
                                trade_closed = True
                                fill_tp2_raw = min(bar_o, tp2_price) if bar_o <= tp2_price else tp2_price
                                tp2_fill_p = fill_tp2_raw * (1.0 + slip_pct)
                                tp2_gross = (entry_price - tp2_fill_p) * qty_rem
                                tp2_fees = tp2_fill_p * qty_rem * taker_fee
                                total_gross = tp1_gross + tp2_gross
                                total_exit_fee = tp1_fees + tp2_fees
                                qty1 = qty_total - qty_rem
                                exit_price_final = (tp1_fill_p * qty1 + tp2_fill_p * qty_rem) / qty_total
                                exit_reason = "TP2"
                            elif bar_h >= sl_price:
                                trade_closed = True
                                fill_be_raw = max(bar_o, sl_price) if bar_o >= sl_price else sl_price
                                be_fill_p = fill_be_raw * (1.0 + slip_pct)
                                be_gross = (entry_price - be_fill_p) * qty_rem
                                be_fees = be_fill_p * qty_rem * taker_fee
                                total_gross = tp1_gross + be_gross
                                total_exit_fee = tp1_fees + be_fees
                                qty1 = qty_total - qty_rem
                                exit_price_final = (tp1_fill_p * qty1 + be_fill_p * qty_rem) / qty_total
                                exit_reason = "BE"

                # --- MODE: PHASE 4 (Final Precision: Limit Maker Fee + Candle CLOSE EMA20 Trailing Stop) ---
                else:
                    if pos_dir == 1: # LONG
                        if not tp1_hit:
                            sl_hit = bar_l <= sl_price
                            tp1_hit_bar = bar_h >= tp1_price

                            if sl_hit and tp1_hit_bar:
                                trade_closed = True
                                fill_sl = min(bar_o, sl_price) * (1.0 - slip_pct)
                                exit_price_final = fill_sl
                                total_gross = (fill_sl - entry_price) * qty_total
                                total_exit_fee = fill_sl * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif sl_hit:
                                trade_closed = True
                                fill_sl = min(bar_o, sl_price) * (1.0 - slip_pct)
                                exit_price_final = fill_sl
                                total_gross = (fill_sl - entry_price) * qty_total
                                total_exit_fee = fill_sl * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif tp1_hit_bar:
                                # TP1 Hit!
                                tp1_hit = True
                                fill_tp1_raw = max(bar_o, tp1_price) if bar_o >= tp1_price else tp1_price
                                tp1_fill_p = fill_tp1_raw * (1.0 - slip_pct)
                                qty1 = qty_total * cfg.tp1_close_pct
                                qty_rem = qty_total - qty1

                                tp1_gross = (tp1_fill_p - entry_price) * qty1
                                tp1_fees = tp1_fill_p * qty1 * taker_fee

                                # Set catastrophic protective stop for runner
                                sl_price = entry_price - 0.2 * current_atr

                        else: # Runner is active: 15m Candle CLOSE trailing stop below EMA20
                            if bar_c < current_ema20:
                                # Candle closed below EMA20 -> exit runner
                                trade_closed = True
                                fill_close = bar_c * (1.0 - slip_pct)
                                qty1 = qty_total - qty_rem
                                runner_gross = (fill_close - entry_price) * qty_rem
                                runner_fee = fill_close * qty_rem * taker_fee
                                total_gross = tp1_gross + runner_gross
                                total_exit_fee = tp1_fees + runner_fee
                                exit_price_final = (tp1_fill_p * qty1 + fill_close * qty_rem) / qty_total
                                exit_reason = "RUNNER_CLOSE_EMA20"
                            elif bar_l <= sl_price:
                                # Catastrophic protective stop
                                trade_closed = True
                                fill_sl = min(bar_o, sl_price) * (1.0 - slip_pct)
                                qty1 = qty_total - qty_rem
                                runner_gross = (fill_sl - entry_price) * qty_rem
                                runner_fee = fill_sl * qty_rem * taker_fee
                                total_gross = tp1_gross + runner_gross
                                total_exit_fee = tp1_fees + runner_fee
                                exit_price_final = (tp1_fill_p * qty1 + fill_sl * qty_rem) / qty_total
                                exit_reason = "RUNNER_CATASTROPHIC_SL"

                    else: # SHORT
                        if not tp1_hit:
                            sl_hit = bar_h >= sl_price
                            tp1_hit_bar = bar_l <= tp1_price

                            if sl_hit and tp1_hit_bar:
                                trade_closed = True
                                fill_sl = max(bar_o, sl_price) * (1.0 + slip_pct)
                                exit_price_final = fill_sl
                                total_gross = (entry_price - fill_sl) * qty_total
                                total_exit_fee = fill_sl * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif sl_hit:
                                trade_closed = True
                                fill_sl = max(bar_o, sl_price) * (1.0 + slip_pct)
                                exit_price_final = fill_sl
                                total_gross = (entry_price - fill_sl) * qty_total
                                total_exit_fee = fill_sl * qty_total * taker_fee
                                exit_reason = "SL_FULL"
                            elif tp1_hit_bar:
                                tp1_hit = True
                                fill_tp1_raw = min(bar_o, tp1_price) if bar_o <= tp1_price else tp1_price
                                tp1_fill_p = fill_tp1_raw * (1.0 + slip_pct)
                                qty1 = qty_total * cfg.tp1_close_pct
                                qty_rem = qty_total - qty1

                                tp1_gross = (entry_price - tp1_fill_p) * qty1
                                tp1_fees = tp1_fill_p * qty1 * taker_fee

                                sl_price = entry_price + 0.2 * current_atr

                        else: # Runner active for SHORT
                            if bar_c > current_ema20:
                                # Candle closed above EMA20 -> exit runner
                                trade_closed = True
                                fill_close = bar_c * (1.0 + slip_pct)
                                qty1 = qty_total - qty_rem
                                runner_gross = (entry_price - fill_close) * qty_rem
                                runner_fee = fill_close * qty_rem * taker_fee
                                total_gross = tp1_gross + runner_gross
                                total_exit_fee = tp1_fees + runner_fee
                                exit_price_final = (tp1_fill_p * qty1 + fill_close * qty_rem) / qty_total
                                exit_reason = "RUNNER_CLOSE_EMA20"
                            elif bar_h >= sl_price:
                                trade_closed = True
                                fill_sl = max(bar_o, sl_price) * (1.0 + slip_pct)
                                qty1 = qty_total - qty_rem
                                runner_gross = (entry_price - fill_sl) * qty_rem
                                runner_fee = fill_sl * qty_rem * taker_fee
                                total_gross = tp1_gross + runner_gross
                                total_exit_fee = tp1_fees + runner_fee
                                exit_price_final = (tp1_fill_p * qty1 + fill_sl * qty_rem) / qty_total
                                exit_reason = "RUNNER_CATASTROPHIC_SL"

                if trade_closed:
                    # In Phase 4, entry fee uses maker fee (0.02%); in earlier phases uses taker fee (0.05%)
                    entry_fee_rate = maker_fee if mode == "phase4" else taker_fee
                    entry_fee = entry_price * qty_total * entry_fee_rate
                    total_fee = entry_fee + total_exit_fee
                    net_pnl = total_gross - total_fee
                    notional = entry_price * qty_total
                    ret_pct = (net_pnl / notional) * 100.0 if notional > 0 else 0.0
                    r_mult = net_pnl / trade_risk if trade_risk > 0 else 0.0

                    trades.append(
                        Trade(
                            symbol=symbol,
                            direction="LONG" if pos_dir == 1 else "SHORT",
                            pattern=pattern,
                            entry_time=int(timestamps[entry_idx]),
                            exit_time=int(timestamps[i]),
                            entry_price=entry_price,
                            exit_price=exit_price_final,
                            qty=qty_total,
                            notional=notional,
                            risk_amount=trade_risk,
                            gross_pnl=total_gross,
                            entry_fee=entry_fee,
                            exit_fee=total_exit_fee,
                            total_fee=total_fee,
                            net_pnl=net_pnl,
                            return_pct=ret_pct,
                            r_multiple=r_mult,
                            exit_reason=exit_reason,
                            duration_bars=i - entry_idx,
                        )
                    )
                    in_pos = False

            # 2. Entry signal execution on bar i+1 open
            if not in_pos and signals[i] != 0:
                pos_dir = signals[i]
                pattern = patterns[i]
                sl_d = sl_dists[i]
                tp1_d = tp1_dists[i]
                tp2_d = tp2_dists[i]
                next_o = opens[i + 1]

                if pos_dir == 1:
                    entry_price = next_o * (1.0 + slip_pct)
                    sl_price = entry_price - sl_d
                    tp1_price = entry_price + tp1_d
                    tp2_price = entry_price + tp2_d
                else:
                    entry_price = next_o * (1.0 - slip_pct)
                    sl_price = entry_price + sl_d
                    tp1_price = entry_price - tp1_d
                    tp2_price = entry_price - tp2_d

                calc_qty = risk_usdt / sl_d
                if calc_qty * entry_price > max_notional:
                    calc_qty = max_notional / entry_price

                qty_total = calc_qty
                qty_rem = calc_qty
                trade_risk = min(risk_usdt, qty_total * sl_d)
                entry_idx = i + 1

                tp1_hit = False
                tp1_gross = 0.0
                tp1_fees = 0.0
                tp1_fill_p = 0.0
                in_pos = True

        return trades

    def compute_metrics(self, trades: List[Trade]) -> Dict[str, Any]:
        """
        Compute institutional performance statistics from a list of completed trades.
        """
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "net_pnl": 0.0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_usdt": 0.0,
                "expectancy_usdt": 0.0,
                "expectancy_r": 0.0,
                "avg_win_usdt": 0.0,
                "avg_loss_usdt": 0.0,
                "realized_rr": 0.0,
                "total_fees": 0.0,
                "wins": 0,
                "losses": 0,
            }

        tdf = pd.DataFrame([t.__dict__ for t in trades]).sort_values("exit_time").reset_index(drop=True)

        wins = tdf[tdf["net_pnl"] > 0]
        losses = tdf[tdf["net_pnl"] <= 0]

        total_trades = len(tdf)
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = (n_wins / total_trades) * 100.0

        gross_profit = wins["net_pnl"].sum() if n_wins > 0 else 0.0
        gross_loss = abs(losses["net_pnl"].sum()) if n_losses > 0 else 1e-9
        profit_factor = gross_profit / gross_loss
        net_pnl = tdf["net_pnl"].sum()
        total_fees = tdf["total_fee"].sum()

        avg_win = wins["net_pnl"].mean() if n_wins > 0 else 0.0
        avg_loss = abs(losses["net_pnl"].mean()) if n_losses > 0 else 0.0
        realized_rr = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        expectancy_usdt = net_pnl / total_trades
        expectancy_r = tdf["r_multiple"].mean()

        initial_cap = self.config.initial_capital_usdt
        tdf["cum_pnl"] = tdf["net_pnl"].cumsum()
        equity_curve = initial_cap + tdf["cum_pnl"]
        peak = equity_curve.cummax()
        dd_usdt_series = peak - equity_curve
        dd_pct_series = (dd_usdt_series / peak) * 100.0

        max_dd_usdt = dd_usdt_series.max()
        max_dd_pct = dd_pct_series.max()

        return {
            "total_trades": total_trades,
            "wins": n_wins,
            "losses": n_losses,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "net_pnl": net_pnl,
            "total_fees": total_fees,
            "max_drawdown_pct": max_dd_pct,
            "max_drawdown_usdt": max_dd_usdt,
            "expectancy_usdt": expectancy_usdt,
            "expectancy_r": expectancy_r,
            "avg_win_usdt": avg_win,
            "avg_loss_usdt": avg_loss,
            "realized_rr": realized_rr,
        }
