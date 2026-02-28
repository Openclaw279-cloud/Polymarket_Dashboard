# Polymarket Automated Trading Starter

This repository includes a **safe-by-default starter bot** for building an AI-assisted automated strategy on Polymarket.

## What this gives you

- Pulls market metadata from Polymarket's public Gamma API.
- Pulls order book snapshots from Polymarket's CLOB API.
- Supports two autonomous modes:
  - **Copy trading mode** (default): mirrors a target wallet's public trades.
  - **Strategy mode** (optional): momentum + spread heuristic.
- Applies risk controls (max position size, dry-run by default).
- Can place **autonomous live orders** when wallet/API credentials are provided.

> ⚠️ The bot defaults to `DRY_RUN=true` so it will **not** place real orders unless you explicitly disable dry-run and provide valid credentials.

## Quick start

1. Create and activate a Python environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy and edit env file:

```bash
cp .env.example .env
```

4. Run in dry-run mode:

```bash
python bot/automated_strategy.py
```

## Your requested copy-trading wallet

By default, `.env.example` is pre-configured to copy this wallet:

- `0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d`

Set `COPY_TRADING_ENABLED=true` (default) and the bot will attempt to mirror each newly seen trade from that address.

## Copy-trading behavior

- Source of truth: `data-api.polymarket.com/trades?user=<wallet>`.
- The bot polls recent trades each loop, deduplicates using tx hash + asset + side + timestamp, and places matching BUY/SELL orders.
- Copied notional is calculated as:
  - `copied_size_tokens * copied_price * COPY_SIZE_MULTIPLIER`
  - then capped by `MAX_ORDER_USDC`.

### Important caveats for "every trade"

Your request was to take **every trade** from the target account. In practice, exact 1:1 replication may fail when:

- your account has insufficient balance,
- your account cannot SELL because it does not hold that outcome token,
- market microstructure changed before your order hits,
- the source account uses a special account architecture and your signer/funder settings differ.

The bot will still attempt each detected trade, but execution depends on account state and exchange constraints.

## Enabling autonomous live trades

Set these values in `.env`, then set `DRY_RUN=false`:

- `POLY_PRIVATE_KEY` - EVM private key used for signing orders.
- `POLY_CHAIN_ID` - Polygon mainnet is `137`.
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` - optional if already created.
- `POLY_FUNDER_ADDRESS` and `POLY_SIGNATURE_TYPE` - optional for proxy/smart-wallet or custodial funder setups.

If API creds are not provided, the bot will attempt to derive/create them from your signer key.

## Phantom wallet + custodial Polymarket address

If your Polymarket position is tied to a **custodial/funder address** but you usually click-sign in Phantom:

- This is workable, but you need the correct signer model.
- The bot can run with **separate signer + funder**:
  - `POLY_PRIVATE_KEY` = programmatic signer key (the key the bot can access),
  - `POLY_FUNDER_ADDRESS` = your custodial/proxy address,
  - `POLY_SIGNATURE_TYPE` = signature mode expected by your account setup.

Important constraint:

- Browser wallets (including Phantom) do **not** provide a stable headless API for unattended server-side signing.
- True autonomous trading requires a key the bot controls directly (or an institutional signer/HSM setup).
- If your only signing path is "click approve in Phantom", it cannot be fully autonomous.

## Strategy mode (optional)

Set `STRATEGY_ENABLED=true` to also run the momentum strategy:

1. Fetch active markets and filter to a keyword/topic universe.
2. Read each market's best bid/ask and recent midpoint history.
3. Compute momentum minus spread penalty.
4. Trigger BUY when score exceeds threshold.

## Important notes

- Review Polymarket API docs and Terms before live deployment.
- Keep secret keys out of source control.
- Start with paper trading + tiny position sizes.
- Monitor logs and include kill switches.
