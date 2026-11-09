# 🧪 Trading Cloudi — Strategy Backtesting Sandbox

**Purpose:** Isolated research and backtesting framework for Velotrade 1H+15m price action rules.  
**Constraint:** Strict sandbox mode. The agent working in this directory must **NOT** modify or delete any files outside `backtest/` (such as `app/`, `run_unified.py`, `app_ui.py`, etc.).

---

## 📂 Data Directory Structure

All historical data has been pre-loaded locally inside `backtest/data/`:

```text
backtest/data/
├── BTCUSDT/
│   ├── 15m/ohlcv_365d.csv
│   ├── 1h/ohlcv_365d.csv
│   └── 1d/ohlcv_365d.csv
├── ETHUSDT/ (15m, 1h, 1d)
├── SOLUSDT/ (15m, 1h, 1d)
├── XRPUSDT/ (15m, 1h, 1d)
├── DOGEUSDT/ (15m, 1h, 1d)
├── BNBUSDT/ (15m, 1h, 1d)
├── ADAUSDT/ (15m, 1h, 1d)
├── AVAXUSDT/ (15m, 1h, 1d)
├── LINKUSDT/ (15m, 1h, 1d)
└── DOTUSDT/ (15m, 1h, 1d)
```

### CSV Schema:
`timestamp` (ms epoch), `open`, `high`, `low`, `close`, `volume`, `turnover`

---

## 🎯 Target Backtest Deliverables

1. `backtest/data_loader.py` — Load and resample OHLCV data from `backtest/data/`.
2. `backtest/strategies.py` — Implement Velotrade 1H macro trend + 15m entry price action rules.
3. `backtest/engine.py` — Simulation engine with 1.5*ATR stop loss, 3.0*ATR take profit, and fee model.
4. `backtest/run_backtest.py` — Runner script producing summary metrics.
5. `backtest/REPORT.md` — Final validation report against acceptance criteria:
   - Win Rate >= 50%
   - Profit Factor >= 1.6
   - Max Drawdown <= 12%
   - Trades >= 150
