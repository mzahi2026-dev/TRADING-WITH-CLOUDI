import hashlib
import hmac
import os
import time
from decimal import Decimal
from urllib.parse import urlencode

import requests

from . import config  # noqa: F401  (.env load hoy)
from . import trade_config as tc
from .trade_log import log


class DemoError(Exception):
    def __init__(self, code, msg, status=0):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.status = status


def dstr(x):
    return format(Decimal(str(x)), "f")


class DemoClient:
    def __init__(self):
        self.key = os.getenv("DEMO_API_KEY", "").strip()
        self.secret = os.getenv("DEMO_API_SECRET", "").strip()
        if not self.key or not self.secret:
            raise RuntimeError(".env e DEMO_API_KEY / DEMO_API_SECRET nai")
        if not tc.DEMO_BASE.startswith("https://demo-"):
            raise RuntimeError("DEMO_BASE demo endpoint na. Thamlam.")
        self.s = requests.Session()
        self.s.headers["X-MBX-APIKEY"] = self.key
        self.offset = 0
        self.sync_time()

    def sync_time(self):
        r = requests.get(tc.DEMO_BASE + "/fapi/v1/time", timeout=10).json()
        self.offset = int(r["serverTime"]) - int(time.time() * 1000)

    def _req(self, method, path, params=None, signed=True):
        last = None
        for attempt in range(4):
            p = dict(params or {})
            if signed:
                p["timestamp"] = int(time.time() * 1000) + self.offset
                p["recvWindow"] = 10000
            q = urlencode(p)
            if signed:
                q += "&signature=" + hmac.new(self.secret.encode(), q.encode(),
                                              hashlib.sha256).hexdigest()
            try:
                r = self.s.request(method, f"{tc.DEMO_BASE}{path}?{q}", timeout=15)
            except requests.RequestException as e:
                last = e
                # Duplicate recovery: if an order timed out, check if it was accepted
                cid = p.get("newClientOrderId")
                if method == "POST" and path == "/fapi/v1/order" and cid:
                    recovered = self.check_order_exists(p.get("symbol"), cid)
                    if recovered:
                        log.info(f"Recovered order {cid} for {p.get('symbol')} after network timeout: {e}")
                        return recovered
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = None
            if r.status_code in (429, 418):
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            code = data.get("code") if isinstance(data, dict) else None
            if r.status_code >= 400 or (isinstance(code, int) and code < 0):
                if code == -1021:
                    self.sync_time()
                    continue
                # If duplicate order error occurs on retry, recover the original order
                cid = p.get("newClientOrderId")
                if method == "POST" and path == "/fapi/v1/order" and cid:
                    recovered = self.check_order_exists(p.get("symbol"), cid)
                    if recovered:
                        log.info(f"Recovered order {cid} for {p.get('symbol')} on duplicate response: {code}")
                        return recovered
                msg = data.get("msg") if isinstance(data, dict) else r.text[:200]
                raise DemoError(code, msg, r.status_code)
            return data
        raise RuntimeError(f"Demo API failed: {path} {last}")

    def query_order(self, symbol, order_id=None, orig_client_order_id=None):
        p = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = order_id
        if orig_client_order_id is not None:
            p["origClientOrderId"] = orig_client_order_id
        return self._req("GET", "/fapi/v1/order", p)

    def check_order_exists(self, symbol, client_order_id):
        try:
            return self.query_order(symbol, orig_client_order_id=client_order_id)
        except DemoError as e:
            if e.code == -2013:  # Order does not exist
                return None
            log.warning(f"check_order_exists {symbol} {client_order_id}: {e}")
            return None
        except Exception as e:
            log.warning(f"check_order_exists error {symbol} {client_order_id}: {e}")
            return None

    # ---------------- account
    def balance(self):
        try:
            rows = self._req("GET", "/fapi/v2/balance")
        except DemoError:
            rows = self._req("GET", "/fapi/v3/balance")
        for r in rows:
            if r.get("asset") == "USDT":
                return float(r.get("availableBalance", 0)), float(r.get("balance", 0))
        return 0.0, 0.0

    def dual_mode(self):
        return bool(self._req("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition"))

    def set_one_way(self):
        return self._req("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})

    def positions(self):
        try:
            return self._req("GET", "/fapi/v2/positionRisk")
        except DemoError:
            return self._req("GET", "/fapi/v3/positionRisk")

    def user_trades(self, symbol, start_ms):
        return self._req("GET", "/fapi/v1/userTrades",
                         {"symbol": symbol, "startTime": int(start_ms), "limit": 1000})

    def open_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("GET", "/fapi/v1/openOrders", p)

    def algo_open_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        try:
            return self._req("GET", "/fapi/v1/algoOpenOrders", p)
        except DemoError as e:
            if e.status in (404, 405):
                return []
            raise

    # ---------------- orders
    def set_margin(self, symbol):
        try:
            self._req("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except DemoError as e:
            if e.code != -4046:      # "No need to change margin type"
                raise

    def set_leverage(self, symbol, lev):
        return self._req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

    def market(self, symbol, side, qty, reduce=False, client_order_id=None):
        if not client_order_id:
            client_order_id = f"m_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        p = {"symbol": symbol, "side": side, "type": "MARKET",
             "quantity": dstr(qty), "newOrderRespType": "RESULT",
             "newClientOrderId": client_order_id}
        if reduce:
            p["reduceOnly"] = "true"
        return self._req("POST", "/fapi/v1/order", p)

    def conditional(self, symbol, side, otype, trigger, client_algo_id=None):
        """SL / TP. Notun Algo endpoint age, na hole purono endpoint."""
        if not client_algo_id:
            client_algo_id = f"a_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": otype,
             "triggerPrice": dstr(trigger), "closePosition": "true", "workingType": "MARK_PRICE",
             "clientAlgoId": client_algo_id}
        try:
            return self._req("POST", "/fapi/v1/algoOrder", p)
        except DemoError as e:
            if e.status not in (404, 405) and e.code is not None:
                raise
            log.warning(f"algoOrder endpoint nai ({e}), purono /order try korchi")
            q = {"symbol": symbol, "side": side, "type": otype, "stopPrice": dstr(trigger),
                 "closePosition": "true", "workingType": "MARK_PRICE",
                 "newClientOrderId": client_algo_id}
            return self._req("POST", "/fapi/v1/order", q)

    def cancel_conditionals(self, symbol):
        for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
            try:
                self._req("DELETE", path, {"symbol": symbol})
            except DemoError as e:
                log.info(f"cancel {path} {symbol}: {e}")