# Migration Impact Assessment: Binance USDⓈ-M Futures to Bybit V5 USDT Linear Perpetuals

**Document Version:** 1.0.0  
**Date:** 2026-11-03  
**Target Architecture:** Bybit V5 API (`category=linear`), USDT-settled Perpetual Futures, Demo Trading Only (`https://api-demo.bybit.com`), Price Action Strategy  
**Security Posture:** Fail-Closed, Zero Real-Money Order Exposure  

---

## 1. Executive Summary

The existing codebase (`TRADING WITH CLOUDI`) is tightly coupled to Binance USDⓈ-M Futures REST and WebSocket APIs. No Bybit V5 integration, abstraction layer, or client adapter currently exists. Migrating to Bybit V5 Demo Trading is a **breaking architectural transition** across the entire pipeline:
1. Environment isolation & configuration
2. Authentication & request signing
3. REST API endpoints & data payloads
4. WebSocket streaming & subscription protocol
5. Order execution, client order IDs, and position mode
6. Stop-Loss / Take-Profit (SL/TP) mechanics and conditional order lifecycle
7. Position tracking, reconciliation, and trade journaling

Direct URL replacement or partial patching is strictly prohibited due to fundamental protocol and payload differences. A clean migration requires implementing a dedicated Bybit V5 client module and updating all downstream consumers while establishing a rigorous fail-closed guard against live trading endpoints.

---

## 2. Environment & Endpoint Isolation Matrix

| Component | Current (Binance USDⓈ-M) | Target (Bybit V5 Linear Demo) | Live Disaster Risk if Misconfigured | Fail-Closed Requirement |
|---|---|---|---|---|
| **REST Base URL** | `https://demo-fapi.binance.com` (hardcoded in `trade_config.py:1` / conditional in `config.py:12`) | `https://api-demo.bybit.com` | If routed to `https://api.bybit.com`, orders execute with live funds | Reject any URL other than `https://api-demo.bybit.com`. Abort startup immediately if `api.bybit.com` is detected |
| **Market Data REST** | `https://fapi.binance.com` (hardcoded bypass in `binance_client.py:17` for `/futures/data/*`) | `https://api-demo.bybit.com` (or public `https://api.bybit.com` for read-only klines if permitted, but strictly demo for order actions) | Live query data leakage / IP rate-limiting | Strict separation of authenticated order clients from public read-only market data clients |
| **WebSocket Base URL** | `wss://demo-fstream.binance.com` / `wss://fstream.binance.com` (`config.py:18`) | `wss://stream-demo.bybit.com/v5/public/linear` | Connection failure, mismatched symbols | WebSocket client must enforce `stream-demo.bybit.com` |
| **Private WebSocket** | None (polling REST via `DemoClient`) | `wss://stream-demo.bybit.com/v5/private` | Authentication leaks | Dedicated authenticated private stream for real-time order/position updates |

---

## 3. Authentication & Request Signing Specification

### Current Implementation (Binance)
- **Header:** `X-MBX-APIKEY: <API_KEY>`
- **Signing:** HMAC-SHA256 signature passed as query parameter:
  $$\text{signature} = \text{HMAC-SHA256}(\text{secret}, \text{queryString})$$
- Timestamp in milliseconds (`timestamp`) and `recvWindow` included in query parameters.

### Target Implementation (Bybit V5)
Bybit V5 mandates custom headers for authentication:
- `X-BAPI-API-KEY`: Bybit API Key
- `X-BAPI-TIMESTAMP`: Millisecond Unix timestamp ($\pm 1000\text{ ms}$ synchronization)
- `X-BAPI-RECV-WINDOW`: Recv window (recommended `5000` to `10000`)
- `X-BAPI-SIGN`: HMAC-SHA256 hexadecimal signature calculated over:
  - **GET requests:** `timestamp + api_key + recv_window + queryString`
  - **POST requests:** `timestamp + api_key + recv_window + jsonPayloadBody`
- `X-BAPI-SIGN-TYPE`: `"2"`

```python
# Canonical Bybit V5 Signature Generation
import hmac, hashlib, time, json

def generate_bybit_v5_headers(api_key: str, api_secret: str, params_or_body: str = "", recv_window: str = "5000"):
    ts = str(int(time.time() * 1000))
    sign_payload = f"{ts}{api_key}{recv_window}{params_or_body}"
    signature = hmac.new(api_secret.encode("utf-8"), sign_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }
```

---

## 4. REST API Endpoint Mapping

| Functional Operation | Current Binance Endpoint | Target Bybit V5 Endpoint | Parameters & Differences |
|---|---|---|---|
| **Server Time Sync** | `GET /fapi/v1/time` | `GET /v5/market/time` | Bybit response: `result.timeSecond` / `result.timeNano` |
| **Exchange Info & Filters** | `GET /fapi/v1/exchangeInfo` | `GET /v5/market/instruments-info?category=linear` | Tick size, step size, min qty in `priceFilter`, `lotSizeFilter` |
| **Candlestick Klines** | `GET /fapi/v1/klines` | `GET /v5/market/kline` | Params: `category=linear`, `symbol`, `interval` (`15`, `60`, `240`, `D`), `start`, `end`, `limit` |
| **Order Book Depth** | `GET /fapi/v1/depth` | `GET /v5/market/orderbook` | Params: `category=linear`, `symbol`, `limit=5` or `50` |
| **24h Ticker & Volume** | `GET /fapi/v1/ticker/24hr` | `GET /v5/market/tickers?category=linear` | Volume field is `turnover24h` (quote volume USDT) |
| **Open Interest** | `GET /fapi/v1/openInterest` | `GET /v5/market/open-interest?category=linear&symbol=...&intervalTime=...` | Specific interval parameter required |
| **Funding Rate & Mark Price**| `GET /fapi/v1/premiumIndex` | `GET /v5/market/tickers?category=linear` | Returns `markPrice`, `indexPrice`, `fundingRate`, `nextFundingTime` directly |
| **Funding History** | `GET /fapi/v1/fundingRate` | `GET /v5/market/funding/history?category=linear` | Historical settlement records |
| **Account Balance** | `GET /fapi/v2/balance` | `GET /v5/account/wallet-balance?accountType=UNIFIED` | Query `result.list[0].totalEquity`, `totalAvailableBalance` |
| **Position Query** | `GET /fapi/v2/positionRisk` | `GET /v5/position/list?category=linear&settleCoin=USDT` | Returns `size`, `side`, `avgPrice`, `unrealisedPnl`, `stopLoss`, `takeProfit` |
| **Set Margin & Leverage** | `POST /fapi/v1/marginType`<br>`POST /fapi/v1/leverage` | `POST /v5/position/set-leverage`<br>`POST /v5/position/switch-isolated` | Body: `{"category": "linear", "symbol": sym, "buyLeverage": "5", "sellLeverage": "5", "isIsolated": true}` |
| **Market Order Execution**| `POST /fapi/v1/order` | `POST /v5/order/create` | Body: `{"category": "linear", "symbol": sym, "side": "Buy"|"Sell", "orderType": "Market", "qty": "...", "orderLinkId": "..."}` |
| **Protective SL/TP** | Conditional `POST /fapi/v1/algoOrder` | Native `POST /v5/position/trading-stop` OR embedded in `POST /v5/order/create` | Bybit natively attaches SL/TP to position (`stopLoss`, `takeProfit`, `tpslMode="Full"`) |
| **Cancel Conditional Orders**| `DELETE /fapi/v1/algoOpenOrders`<br>`DELETE /fapi/v1/allOpenOrders` | `POST /v5/order/cancel-all` | Body: `{"category": "linear", "symbol": sym}` |
| **Trade Execution History**| `GET /fapi/v1/userTrades` | `GET /v5/execution/list?category=linear` | Params: `symbol`, `startTime`, `limit` |

---

## 5. Architectural & Structural Impacts

### 5.1. Protective SL/TP Lifecycle Advantage
- **Current Binance Defect:** Binance separates primary market orders and conditional stop-loss orders (`algoOrder` / `order`). When a position closes, secondary conditional orders must be manually cancelled via `cancel_conditionals(sym)`. In the existing code (`demo_client.py:212-217`), cancellation errors are swallowed, causing orphaned stop orders that execute later as unexpected naked positions.
- **Bybit V5 Solution:** Bybit V5 allows setting position-tied Stop-Loss and Take-Profit via `POST /v5/position/trading-stop` or directly during order submission (`stopLoss`, `takeProfit`, `tpslMode="Full"`). When the position closes (via SL, TP, or manual exit), Bybit **automatically voids** the associated protective triggers. This completely eliminates orphaned protective order risk.

### 5.2. Client Order ID & Idempotency
- **Binance:** Uses `newClientOrderId` (max 36 chars) or `clientAlgoId`.
- **Bybit V5:** Uses `orderLinkId` (custom string up to 36 characters, letters, numbers, hyphens, and underscores).
- **Rule:** Every order must provide a deterministic `orderLinkId` formatted as `demo_{engine}_{symbol}_{timestamp_ms}`. If network connection times out, querying `GET /v5/order/realtime?category=linear&orderLinkId=...` provides guaranteed idempotency without duplicate executions.

### 5.3. WebSocket Streaming Discrepancies
- **Binance Multiplexing:** `GET wss://fstream.binance.com/stream?streams=btcusdt@kline_15m/ethusdt@kline_15m`
- **Bybit V5 Topics:** JSON subscription over persistent WebSocket connection:
  ```json
  {
    "op": "subscribe",
    "args": [
      "kline.15.BTCUSDT",
      "kline.60.BTCUSDT",
      "orderbook.1.BTCUSDT"
    ]
  }
  ```
- **Keep-Alive:** Bybit V5 requires sending `{"op": "ping"}` every 20 seconds, expecting `{"op": "pong"}` response. Failure to ping results in connection closure after 30 seconds.

---

## 6. Codebase File Impact Inventory

The table below lists every file in `E:\TRADING WITH CLOUDI` requiring refactoring or replacement during the Bybit V5 migration:

| File Path | Impact Level | Primary Changes Required |
|---|---|---|
| `app/config.py` | Critical (P0) | Replace `BINANCE_FAPI_BASE` and `BINANCE_WS_BASE` with Bybit V5 demo endpoints (`https://api-demo.bybit.com`, `wss://stream-demo.bybit.com/v5/public/linear`). Add fail-closed assertion. |
| `app/trade_config.py` | Critical (P0) | Update `DEMO_BASE = "https://api-demo.bybit.com"`. Adjust interval and volume threshold specifications. |
| `app/binance_client.py` | Critical (P0) | Replace/rename with `app/bybit_client.py` implementing Bybit V5 headers, signature, error parsing, and rate limit handling. Remove production URL bypass (`/futures/data/`). |
| `app/demo_client.py` | Critical (P0) | Rewrite order creation, position queries, margin/leverage setting, and SL/TP management using Bybit V5 contract. Utilize `orderLinkId` and `trading-stop`. |
| `app/trade_engine.py` | Critical (P0) | Migrate order execution (`/v5/order/create`), exchange info unpacking, position query parsing (`/v5/position/list`), and reconciliation (`/v5/execution/list`). |
| `app/ai_engine.py` | Critical (P0) | Enforce SL validation, integrate Bybit V5 execution calls, add circuit-breaker checks (daily loss and balance), prevent concurrent symbol collisions with Rule Engine. |
| `app/collector.py` | High (P1) | Rewrite WebSocket subscription payload to Bybit V5 topics (`kline.{interval}.{symbol}`). Rewrite kline REST backfill parser to match Bybit V5 kline response. |
| `app/symbols.py` | High (P1) | Parse `GET /v5/market/instruments-info?category=linear` and `GET /v5/market/tickers?category=linear` to compute eligible universe. |
| `app/extra.py` | High (P1) | Replace Binance `/futures/data/*` calls with Bybit V5 open interest, funding rate history, and long/short ratio endpoints. |
| `app_ui.py` | Medium (P1) | Update live position queries, mark price queries, and balance retrieval to Bybit V5 client. Fix daily PnL calculation to use `closed_ms`. |
| `run_unified.py` | High (P0) | Eliminate misleading "DRY RUN ONLY" print messages. Enforce single-instance lock across all sub-scripts. Ensure fail-closed checks before starting background threads. |

---

## 7. Migration Readiness Decision Gate

**Status: BLOCKED.**  
Migration implementation must not proceed until:
1. P0 safety audit findings are formally reviewed and approved.
2. Verified Bybit V5 Demo API credentials (`BYBIT_DEMO_API_KEY`, `BYBIT_DEMO_API_SECRET`) are provisioned and tested in isolation.
3. Isolated unit tests and mock integration tests for `bybit_client.py` are created and verified.
4. The migration plan is executed in an isolated branch with zero modifications to master during testing.
