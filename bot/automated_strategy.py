"""Polymarket automated trading starter.

Safe defaults:
- DRY_RUN enabled by default.
- Conservative filtering and risk controls.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:  # pragma: no cover - optional in dry-run only mode
    ClobClient = None
    ApiCreds = None
    OrderArgs = None
    BUY = "BUY"
    SELL = "SELL"

load_dotenv()

GAMMA_BASE = os.getenv("GAMMA_BASE", "https://gamma-api.polymarket.com")
CLOB_BASE = os.getenv("CLOB_BASE", "https://clob.polymarket.com")
DATA_BASE = os.getenv("DATA_BASE", "https://data-api.polymarket.com")


@dataclass
class Settings:
    query: str = os.getenv("MARKET_QUERY", "bitcoin")
    max_markets: int = int(os.getenv("MAX_MARKETS", "20"))
    lookback_points: int = int(os.getenv("LOOKBACK_POINTS", "5"))
    min_score: float = float(os.getenv("MIN_SCORE", "0.015"))
    spread_penalty: float = float(os.getenv("SPREAD_PENALTY", "1.0"))
    bankroll_usdc: float = float(os.getenv("BANKROLL_USDC", "150"))
    risk_fraction: float = float(os.getenv("RISK_FRACTION", "0.01"))
    max_order_usdc: float = float(os.getenv("MAX_ORDER_USDC", "25"))
    max_daily_trades: int = int(os.getenv("MAX_DAILY_TRADES", "10"))
    loop_seconds: int = int(os.getenv("LOOP_SECONDS", "60"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # Optional strategy/copy mode toggles
    strategy_enabled: bool = os.getenv("STRATEGY_ENABLED", "false").lower() == "true"
    copy_trading_enabled: bool = os.getenv("COPY_TRADING_ENABLED", "true").lower() == "true"
    copy_trader_address: str = os.getenv(
        "COPY_TRADER_ADDRESS", "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d"
    )
    copy_trade_limit: int = int(os.getenv("COPY_TRADE_LIMIT", "20"))
    copy_size_multiplier: float = float(os.getenv("COPY_SIZE_MULTIPLIER", "1.0"))

    chain_id: int = int(os.getenv("POLY_CHAIN_ID", "137"))
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    signature_type: str = os.getenv("POLY_SIGNATURE_TYPE", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")


class LiveOrderExecutor:
    def __init__(self, settings: Settings) -> None:
        if ClobClient is None or OrderArgs is None or ApiCreds is None:
            raise RuntimeError(
                "py-clob-client is required for live trading. Install dependencies from requirements.txt"
            )

        if not settings.private_key:
            raise RuntimeError(
                "Missing POLY_PRIVATE_KEY. Keep DRY_RUN=true until wallet credentials are configured."
            )

        signature_type = int(settings.signature_type) if settings.signature_type else None
        funder = settings.funder_address or None
        self.client = ClobClient(
            host=CLOB_BASE,
            chain_id=settings.chain_id,
            key=settings.private_key,
            signature_type=signature_type,
            funder=funder,
        )

        if settings.api_key and settings.api_secret and settings.api_passphrase:
            creds = ApiCreds(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                api_passphrase=settings.api_passphrase,
            )
        else:
            creds = self.client.create_or_derive_api_creds()
            print("Generated/derived API creds for this wallet (store them securely).")

        self.client.set_api_creds(creds)

    def place_order(self, token_id: str, side: str, price: float, size_usdc: float) -> dict[str, Any]:
        if price <= 0:
            raise RuntimeError("Cannot place order with non-positive price.")

        size_tokens = round(size_usdc / price, 4)
        order_args = OrderArgs(token_id=token_id, price=price, size=size_tokens, side=side)
        signed_order = self.client.create_order(order_args)
        response = self.client.post_order(signed_order)
        return response if isinstance(response, dict) else {"response": str(response)}


class PolymarketClient:
    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.session = requests.Session()

    def get_markets(self, query: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
        }
        res = self.session.get(f"{GAMMA_BASE}/markets", params=params, timeout=self.timeout)
        res.raise_for_status()
        rows = res.json()
        if query:
            q = query.lower()
            rows = [m for m in rows if q in (m.get("question") or "").lower()]
        return rows[:limit]

    def get_book(self, token_id: str) -> dict[str, Any]:
        res = self.session.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json()

    def get_user_trades(self, user: str, limit: int) -> list[dict[str, Any]]:
        res = self.session.get(
            f"{DATA_BASE}/trades",
            params={"user": user, "limit": limit},
            timeout=self.timeout,
        )
        res.raise_for_status()
        rows = res.json()
        return rows if isinstance(rows, list) else []


def best_prices(book: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return best_bid, best_ask


def midpoint(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2.0


def compute_score(mid_history: list[float], spread: float, spread_penalty: float) -> float:
    if len(mid_history) < 2:
        return -999.0
    first = mid_history[0]
    last = mid_history[-1]
    if first <= 0:
        return -999.0
    momentum = (last - first) / first
    return momentum - (spread * spread_penalty)


def intended_order_size(settings: Settings) -> float:
    raw = settings.bankroll_usdc * settings.risk_fraction
    return min(raw, settings.max_order_usdc)


def maybe_execute_order(
    *,
    label: str,
    token_id: str,
    side: str,
    price: float,
    size_usdc: float,
    dry_run: bool,
    executor: LiveOrderExecutor | None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "token_id": token_id,
        "side": side,
        "price": round(price, 4),
        "size_usdc": round(size_usdc, 2),
    }
    if extra:
        payload.update(extra)

    if dry_run:
        print("[DRY RUN] order", json.dumps(payload))
        return

    if executor is None:
        raise RuntimeError("Live executor not initialized. Configure wallet/API credentials.")

    response = executor.place_order(token_id=token_id, side=side, price=price, size_usdc=size_usdc)
    print("[LIVE] order", json.dumps(payload), "response", json.dumps(response))


def run_strategy_once(
    client: PolymarketClient,
    settings: Settings,
    mid_cache: dict[str, list[float]],
    executor: LiveOrderExecutor | None,
) -> int:
    trades = 0
    markets = client.get_markets(settings.query, settings.max_markets)

    for market in markets:
        raw_ids = market.get("clobTokenIds")
        if not raw_ids:
            continue

        try:
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        except json.JSONDecodeError:
            continue
        if not token_ids:
            continue

        token_id = str(token_ids[0])
        book = client.get_book(token_id)
        bid, ask = best_prices(book)
        mid = midpoint(bid, ask)

        if mid is None or bid is None or ask is None:
            continue

        spread = max(0.0, ask - bid)
        key = str(market.get("conditionId") or token_id)
        history = mid_cache.setdefault(key, [])
        history.append(mid)
        if len(history) > settings.lookback_points:
            history.pop(0)

        score = compute_score(history, spread, settings.spread_penalty)
        print(f"[STRATEGY] market={market.get('question')} bid={bid:.4f} ask={ask:.4f} score={score:.5f}")

        if score >= settings.min_score and trades < settings.max_daily_trades:
            size = intended_order_size(settings)
            maybe_execute_order(
                label="strategy",
                token_id=token_id,
                side=BUY,
                price=ask,
                size_usdc=size,
                dry_run=settings.dry_run,
                executor=executor,
                extra={
                    "market": market.get("question"),
                    "condition_id": market.get("conditionId"),
                },
            )
            trades += 1

    return trades


def copy_trade_once(
    client: PolymarketClient,
    settings: Settings,
    seen_trade_ids: set[str],
    executor: LiveOrderExecutor | None,
) -> int:
    if not settings.copy_trader_address:
        return 0

    copied = 0
    rows = client.get_user_trades(settings.copy_trader_address, settings.copy_trade_limit)

    for trade in sorted(rows, key=lambda x: x.get("timestamp", 0)):
        tx_hash = str(trade.get("transactionHash") or "")
        asset = str(trade.get("asset") or "")
        side = str(trade.get("side") or "").upper()
        trade_id = f"{tx_hash}:{asset}:{side}:{trade.get('timestamp')}"

        if not tx_hash or not asset or side not in {BUY, SELL}:
            continue
        if trade_id in seen_trade_ids:
            continue

        try:
            price = float(trade.get("price") or 0)
            size_tokens = float(trade.get("size") or 0)
        except (TypeError, ValueError):
            continue

        notional_usdc = size_tokens * price * settings.copy_size_multiplier
        size_usdc = min(notional_usdc, settings.max_order_usdc)
        if size_usdc <= 0:
            continue

        maybe_execute_order(
            label="copy_trade",
            token_id=asset,
            side=side,
            price=price,
            size_usdc=size_usdc,
            dry_run=settings.dry_run,
            executor=executor,
            extra={
                "copied_wallet": settings.copy_trader_address,
                "copied_tx_hash": tx_hash,
                "copied_size_tokens": size_tokens,
            },
        )
        seen_trade_ids.add(trade_id)
        copied += 1

    return copied


def main() -> None:
    settings = Settings()
    client = PolymarketClient()
    executor = None if settings.dry_run else LiveOrderExecutor(settings)
    mid_cache: dict[str, list[float]] = {}
    seen_trade_ids: set[str] = set()

    print("Starting Polymarket bot", settings)
    daily_trade_count = 0

    while True:
        try:
            placed = 0
            if settings.strategy_enabled:
                placed += run_strategy_once(client, settings, mid_cache, executor)
            if settings.copy_trading_enabled:
                placed += copy_trade_once(client, settings, seen_trade_ids, executor)

            daily_trade_count += placed
            print(f"loop complete placed={placed} daily_total={daily_trade_count}")
        except Exception as exc:
            print("loop error", exc)

        time.sleep(settings.loop_seconds)


if __name__ == "__main__":
    main()
