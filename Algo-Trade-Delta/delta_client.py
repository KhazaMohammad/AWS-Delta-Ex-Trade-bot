"""
Delta Exchange API client.

Wraps the Delta Exchange India REST API with:
  * HMAC-SHA256 authentication (api-key / timestamp / signature headers)
  * Retries with exponential backoff
  * Rate-limit handling
  * Comprehensive error handling and logging

Endpoints used:
  * GET  /v2/account            - account information
  * GET  /v2/wallet/balance     - wallet balance
  * GET  /v2/positions/margined - open positions
  * GET  /v2/history/{symbol}   - order history
  * GET  /v2/candles/{symbol}   - OHLCV candle data (2-hour timeframe)
  * POST /v2/orders             - place market orders
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

import requests

from config import (
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_BASE_URL,
)
from logger import get_logger

logger = get_logger("delta_client")


class DeltaClientError(Exception):
    """Base exception for Delta Client errors."""


class DeltaAuthError(DeltaClientError):
    """Authentication / credential errors."""


class DeltaAPIError(DeltaClientError):
    """Non-2xx or unsuccessful API responses."""


class DeltaClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        dry_run: bool = True,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or DELTA_API_KEY
        self.api_secret = api_secret or DELTA_API_SECRET
        self.base_url = (base_url or DELTA_BASE_URL).rstrip("/")
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.timeout = timeout

        if not dry_run and (not self.api_key or not self.api_secret):
            raise DeltaAuthError(
                "DELTA_API_KEY and DELTA_API_SECRET are required for live trading."
            )

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "algo-trade-delta/1.0"})

    # ------------------------------------------------------------------ #
    # Authentication helpers
    # ------------------------------------------------------------------ #
    def _generate_signature(self, secret: str, message: str) -> str:
        return hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + body
        signature = self._generate_signature(self.api_secret, signature_data)
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "User-Agent": "algo-trade-delta/1.0",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Core request with retry + exponential backoff
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body_str = ""
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))
        query_string = ""
        if params:
            # Build a deterministic query string for signature (sorted keys)
            import urllib.parse
            query_string = urllib.parse.urlencode(sorted(params.items()))
            if query_string:
                path = f"{path}?{query_string}"

        # In dry run mode, intercept order placement without requiring live API keys
        if self.dry_run and method == "POST" and path.endswith("/v2/orders"):
            logger.info(
                "[DRY_RUN] Would place %s order %s %s",
                (json_body or {}).get("side"), (json_body or {}).get("symbol"), (json_body or {}).get("size")
            )
            return {
                "success": True,
                "result": {
                    "id": f"dry_run_{int(time.time()*1000)}",
                    "symbol": (json_body or {}).get("symbol"),
                    "side": (json_body or {}).get("side"),
                    "size": (json_body or {}).get("size"),
                    "price": (json_body or {}).get("price"),
                    "state": "filled",
                    "filled_size": (json_body or {}).get("size"),
                    "avg_fill_price": (json_body or {}).get("price"),
                },
            }

        headers: dict[str, str] = {}
        if authenticated:
            if not self.api_key or not self.api_secret:
                if self.dry_run:
                    # Provide empty dummy response for authenticated GETs in dry_run mode if keys omitted
                    if method == "GET":
                        return {"success": True, "result": []}
                raise DeltaAuthError("API credentials not configured")
            headers = self._auth_headers(method, path, query_string, body_str)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, headers=headers, params=params,
                    data=body_str if json_body is not None else None,
                    timeout=self.timeout,
                )

                # Rate-limit (429) or transient server error (5xx) -> backoff
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                    logger.warning(
                        "API %s on %s %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, method, path, attempt, self.max_retries, wait,
                    )
                    time.sleep(wait)
                    last_exc = DeltaAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue

                if resp.status_code == 401 or resp.status_code == 403:
                    raise DeltaAuthError(f"Authentication failed: {resp.text[:200]}")

                try:
                    data = resp.json()
                except ValueError:
                    raise DeltaAPIError(
                        f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
                    )

                if not data.get("success", True):
                    err = data.get("error", data)
                    # Treat rate-limit messages from the body as retryable
                    msg = str(err).lower()
                    if "rate" in msg or "limit" in msg or "too many" in msg:
                        wait = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                        logger.warning("API rate-limited (attempt %d/%d), retrying in %.1fs",
                                       attempt, self.max_retries, wait)
                        time.sleep(wait)
                        last_exc = DeltaAPIError(str(err))
                        continue
                    raise DeltaAPIError(f"API error: {err}")

                logger.debug("API %s %s -> success", method, path)
                return data

            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                    logger.warning(
                        "Network error on %s %s (attempt %d/%d): %s, retrying in %.1fs",
                        method, path, attempt, self.max_retries, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                raise DeltaAPIError(f"Network error after {self.max_retries} attempts: {exc}") from exc
            except DeltaAuthError:
                raise
            except DeltaAPIError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                    logger.warning(
                        "Unexpected error on %s %s (attempt %d/%d): %s, retrying in %.1fs",
                        method, path, attempt, self.max_retries, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                raise DeltaAPIError(f"Unexpected error after {self.max_retries} attempts: {exc}") from exc

        raise DeltaAPIError(f"Request failed after {self.max_retries} attempts: {last_exc}")

    # ------------------------------------------------------------------ #
    # Public API methods
    # ------------------------------------------------------------------ #
    def get_account(self) -> dict[str, Any]:
        """GET /v2/account — account information."""
        return self._request("GET", "/v2/account", authenticated=True)

    def get_wallet_balance(self) -> dict[str, Any]:
        """GET /v2/wallet/balance — wallet balance."""
        return self._request("GET", "/v2/wallet/balance", authenticated=True)

    def get_positions(self) -> list[dict[str, Any]]:
        """GET /v2/positions/margined — open positions."""
        data = self._request("GET", "/v2/positions/margined", authenticated=True)
        return data.get("result", []) if isinstance(data, dict) else []

    def get_order_history(self, symbol: str) -> list[dict[str, Any]]:
        """GET /v2/history/{symbol} — order history for a symbol."""
        data = self._request("GET", f"/v2/history/{symbol}", authenticated=True)
        return data.get("result", []) if isinstance(data, dict) else []

    def get_candles(
        self,
        symbol: str,
        resolution: str = "120",   # 120 minutes = 2 hours
        limit: int = 200,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        GET /v2/candles/{symbol} — OHLCV candle data.

        resolution : candle size in minutes (120 = 2-hour candles)
        limit      : max number of candles
        start/end  : unix timestamps (optional window)
        """
        params: dict[str, Any] = {"resolution": resolution, "limit": limit}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end

        data = self._request("GET", f"/v2/candles/{symbol}", params=params, authenticated=True)
        result = data.get("result", []) if isinstance(data, dict) else []
        # Normalise each candle to a dict with consistent keys
        candles: list[dict[str, Any]] = []
        for c in result:
            if isinstance(c, dict):
                candles.append(c)
            elif isinstance(c, (list, tuple)) and len(c) >= 6:
                # Some exchanges return [time, open, high, low, close, volume]
                candles.append({
                    "time": c[0], "open": c[1], "high": c[2], "low": c[3],
                    "close": c[4], "volume": c[5] if len(c) > 5 else 0,
                })
        return candles

    def place_market_order(
        self,
        symbol: str,
        side: str,        # 'buy' or 'sell'
        size: float,
        price: Optional[float] = None,
        product_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        POST /v2/orders — place a market order.

        side: 'buy' to open/long, 'sell' to sell/short (Delta Exchange convention)
        size: order size in contracts/lots
        """
        order_body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "size": size,
            "type": "market",
        }
        if price is not None:
            order_body["price"] = price
        if product_id is not None:
            order_body["product_id"] = product_id

        data = self._request("POST", "/v2/orders", json_body=order_body, authenticated=True)
        return data.get("result", data) if isinstance(data, dict) else data

    def close_position(self, symbol: str) -> dict[str, Any]:
        """
        Close an open position via market order. Delta Exchange supports
        reducing a position by sending an opposing order of equal size.
        Falls back to fetching the position first to determine exact size.
        """
        positions = self.get_positions()
        target = None
        for pos in positions:
            psym = pos.get("product_symbol") or pos.get("symbol")
            if psym == symbol:
                target = pos
                break

        if target is None:
            logger.warning("No open position found for %s — nothing to close.", symbol)
            return {"success": True, "result": {"skipped": True, "reason": "no_open_position"}}

        size = abs(float(target.get("size", 0)))
        side = target.get("side", "").lower()
        # Opposing side to close
        close_side = "sell" if side in ("buy", "long") else "buy"

        logger.info(
            "Closing %s position for %s: size=%s side=%s", side, symbol, size, close_side
        )
        return self.place_market_order(symbol, close_side, size)
