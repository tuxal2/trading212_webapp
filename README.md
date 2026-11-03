# trading212-webapi

Unofficial Python API for the Trading212 web app (`app.trading212.com`),
reconstructed from browser captures of the web client (WC4 v8.54.0).
**Login, demo/live, budget, orders, history, taxes, profit/loss.**

Numbers in the CLI output use the format `1.234,56`.

## Installation

```bash
pip install -e .            # or: pip install requests
```

## Credentials

The code contains no credentials. They are taken from, in this order:

1. parameters (`--username`, `--password`, `--otp` or `Config.load(username=...)`)
2. environment: `T212_USERNAME`, `T212_PASSWORD`, `T212_OTP`, `T212_TURNSTILE_TOKEN`,
   `T212_ACCOUNT`, `T212_CONFIRM_ORDERS`, `T212_CONFIG`
3. config file `~/.config/trading212/config.toml` (template: `config.example.toml`)
4. interactive prompt (password via `getpass`, 2FA code via prompt)

```bash
mkdir -p ~/.config/trading212
cp config.example.toml ~/.config/trading212/config.toml
chmod 600 ~/.config/trading212/config.toml
```

## First login (important)

The password login is protected by a Cloudflare captcha. Without a captcha token the
server answers `InvalidCredentials`, even if the password is correct. So log in once
through the browser:

```bash
pip install -e ".[browser]"            # playwright + playwright-stealth
playwright install chromium            # only needed if Google Chrome is not installed
python -m trading212 browser-login     # a window opens, log in normally incl. 2FA
python -m trading212 budget            # from now on without a browser (session / token login)
```

The browser runs in stealth mode (playwright-stealth, no automation flags, prefers the
installed Google Chrome, persistent profile in `~/.config/trading212/browser-profile`).
Options: `--browser-channel chrome|msedge|chromium`, `--browser-profile <path>|none`.

Without a window (e.g. on a server) the form is filled automatically. Username and
password come from `-u/-p`, `$T212_USERNAME/$T212_PASSWORD`, the config or a prompt;
the 2FA code from `--otp`, `$T212_OTP` or a prompt:

```bash
python -m trading212 browser-login --headless -u me@example.com --otp 123456
```

If Cloudflare shows an interactive challenge instead of passing silently, the
headless login stops with an error – then run `browser-login` with a window.

`browser-login` is only needed again once the stored token expires.

## CLI

```bash
python -m trading212 login                 # login incl. 2FA, stores the session
python -m trading212 budget                # budget overview
python -m trading212 budget --json
python -m trading212 accounts              # LIVE/DEMO accounts
python -m trading212 summary               # raw data: cash, orders, positions
python -m trading212 logout                # delete the local session

python -m trading212 search intel          # search instruments → INTC_US_EQ
python -m trading212 price INTC            # last price
python -m trading212 orders                # open orders

# Orders: preview + validation by Trading212, then sent immediately
# (confirmation only with --confirm or [orders] confirm = true)
python -m trading212 order buy INTC 2 --limit 60            # day limit order (default: --day)
python -m trading212 order buy INTC 2 --limit 60 --gtc      # good till cancelled
python -m trading212 order sell INTC 2 --stop 55            # stop order
python -m trading212 order sell INTC 2 --stop 55 --limit 54.5   # stop-limit
python -m trading212 order buy INTC 3                       # market order
python -m trading212 order buy INTC 500 --value             # buy for 500 USD
python -m trading212 order buy INTC 2 --limit 60 --dry-run  # preview + validation only
python -m trading212 order buy INTC 2 --limit 60 --exchange xetra  # Xetra listing (INLd_EQ, EUR)
python -m trading212 order sell SIE 5 --limit 280 --exchange xetra --gtc
python -m trading212 price Intel --exchange xetra
python -m trading212 order buy INTC_US_EQ 2 --limit 60      # exact ticker – always used as given
python -m trading212 order buy INTC 2 --limit 60 --confirm  # with confirmation [y/N]
python -m trading212 order sell INTC 60 --limit 63          # sell, proceeds to the USD wallet
python -m trading212 order sell INTC 60 --limit 63 --settle EUR   # proceeds in EUR (with FX fee)
python -m trading212 cancel 51234567890                     # cancel an order (waits until done)
python -m trading212 cancel 51234567890 51234567891         # several
python -m trading212 cancel --symbol INTC --side buy        # all Intel buy orders
python -m trading212 cancel --symbol SIE --exchange xetra   # all Siemens orders on Xetra
python -m trading212 cancel --all                           # all open orders
python -m trading212 cancel 51234567890 --no-wait           # do not wait for completion
python -m trading212 -c ./my.toml -u me@example.com budget

# Demo (practice money) / live – options can go before or after the command
python -m trading212 --demo budget
python -m trading212 order buy INTC 2 --limit 60 --demo
python -m trading212 --live orders

# Positions, history, details
python -m trading212 positions                        # open positions with unrealised P/L
python -m trading212 history                          # orders (default), newest first
python -m trading212 history orders --details -n 20   # with fill price and P/L per sale
python -m trading212 history orders --symbol MSFT     # one instrument only
python -m trading212 history dividends | transactions | interest | all
python -m trading212 history all --since 2026-10-01 -n 0   # everything since a date (0 = no limit)
python -m trading212 details 99319640949              # all details of an order
python -m trading212 details /dividends/200000001
python -m trading212 details 99319640949 --pdf confirmation.pdf   # trade confirmation

# Taxes, profit/loss
python -m trading212 taxes                            # exemption order, loss pots, result
python -m trading212 tax-transactions --year 2026 --type EQUITY_SOLD
python -m trading212 gains --year 2026                # realised P/L per sale in EUR + unrealised
python -m trading212 gains --year 2026 --currency USD --symbol MSFT
```

## Python

```python
from trading212 import Config, Trading212Client

cfg = Config.load("~/.config/trading212/config.toml")
with Trading212Client(cfg) as t212:
    t212.ensure_login(otp_callback=lambda ctx: input("2FA code: "))
    b = t212.get_budget()
    print(b.available, b.cash.currency)        # free to invest
    for w in b.wallets:
        print(w.currency, w.free_for_stocks)

    # day limit order: 2 Intel at 60 USD
    inst = t212.resolve_ticker("INTC")                    # INTC_US_EQ, currency USD
    order = t212.build_order(inst.ticker, 2, side="BUY", order_type="LIMIT",
                             currency=inst.currency, limit_price=60, time_validity="DAY")
    print(t212.preview_order(order).total)                # cost preview
    t212.validate_order(order)                            # validation without placing
    placed = t212.place_order(order)                      # sends it (real money!)
    t212.cancel_order(placed.order_id, wait=True)         # None = cancellation complete
    t212.cancel_orders(ticker="INTC_US_EQ", side="BUY")   # several at once
    # shortcuts: buy_limit / sell_limit / buy_market / sell_market / place_value_order

    # by symbol with exchange selection
    t212.order("INTC", 2, limit_price=60)                         # NASDAQ, USD
    t212.order("Intel", 2, limit_price=55, exchange="xetra")      # Xetra, EUR
    t212.order("SIE", 5, side="SELL", limit_price=280, exchange="xetra",
               time_validity="GOOD_TILL_CANCEL")
```

**Demo/live:** the default is `[client] account = "live"` (or `T212_ACCOUNT=demo`).
With `--demo` the client switches like the web app via
`POST https://demo.services.trading212.com/rest/v4/login/session` `{"accountId": <demo account>}`
and then works entirely against the demo server (separate cookie `TRADING212_SESSION_DEMO`).
Every output shows `[LIVE (real money)]` or `[DEMO (practice money)]`, orders additionally
`BUY [LIVE]`/`[DEMO]`. In Python: `t212.ensure_login(account="demo")` or
`t212.switch_account("DEMO")`, `t212.is_demo`.

**History & taxes in Python:**

```python
for e in t212.iter_history("orders", since="2026-10-01"):
    print(e.date, e.label, e.ticker, e.quantity, e.amount, e.currency)
d = t212.get_history_details("99319640949")          # or get_history_details(e)
print(d.fill_price, d.return_amount, d.gain("EUR"), d.taxes)

o = t212.get_tax_overview()
print(o.exemption_remaining, o.stock_loss_pot, o.total_return)
for g in t212.realized_gains(year=2026):
    print(g.date, g.ticker, g.gain["EUR"])
for p in t212.get_positions():
    print(p.ticker, p.ppl, p.ppl_percent)
```

For German tax purposes the EUR amount is what counts. `gains` computes proceeds + cost
basis in EUR per sale from Trading212's tax details; for USD positions the P/L in USD (as
shown in the app) differs because of the exchange rate.

The tax transaction search is sent with an empty body `{}` like the web app (the server
rejects filters with `MessageNotReadable`); year, type and ticker are filtered locally.
Plain dates are interpreted in local time (tax year = calendar year).

**Settlement currency:** an order's `currencyCode` is the wallet that pays / receives the
proceeds – not the quote currency. Limit and stop prices are always in the listing's quote
currency (INTC_US_EQ: USD). The default is the listing's trading currency (US exchanges → USD,
Xetra → EUR), so no FX fee (0.15 %) applies. The web app suggests EUR by default instead.
To change it: `--settle EUR`, `[orders] settlement_currency = "EUR"`, or in Python
`t212.order(..., currency="EUR")`.

**Cancelling:** Trading212 cancels asynchronously – right after the `DELETE` the order is
still listed with status `CANCELLING`. `cancel_order(..., wait=True)` (CLI: default) polls
until it has disappeared.

**Instrument selection:** for a symbol, name or ISIN only stocks/ETFs (`*_EQ`) are chosen,
never CFDs. With several listings (Intel: `INTC_US_EQ` NASDAQ/USD, `INLd_EQ` Xetra/EUR) the
US listing in USD wins – configurable via `[orders] preferred_market/preferred_currency` or
`--exchange` (`us`, `nasdaq`, `nyse`, `xetra`, `lse`, `paris`, …). If an explicitly requested
exchange has no listing, the command aborts and lists the available ones. An exact ticker is
always used as given.

As in the web client, sell orders are sent with a negative quantity;
`build_order(side="SELL")` takes care of that. Invalid orders (insufficient funds, market
closed, wrong quantity precision …) are rejected up front by
`/rest/public/v2/equity/order/validate` and never sent.

## Login flow

`ensure_login()` tries, in order:

1. **stored session** (`session.json`, mode 600) → `GET /rest/validate-session`
2. **token login** with `loginToken` → `POST /rest/v4/login/token`
3. **password login** → `POST /rest/v5/login`; on `TwoFactorAuthenticationRequired`
   (HTTP 403) the request is repeated with `twoFactorAuth.authenticationCode`.

Then it switches to DEMO if requested. The session file contains cookies, `loginToken`,
the account ids per sub-system and a fixed device id (`dUUID`), but **no password**. The
fixed device id makes "remember this device" work.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/rest/v5/login` | login (username/password/2FA) |
| POST | `/rest/v4/login/token` | login with loginToken |
| GET | `/rest/validate-session` | check session |
| POST | `{demo\|live}.services…/rest/v4/login/session` `{"accountId"}` | switch LIVE ↔ DEMO |
| GET | `/rest/v1/accounts` | accounts |
| GET | `/rest/v1/customers/accounts/summary` | net worth |
| POST | `/rest/v1/equity/multi-accounts/summary?targetCurrency=EUR` | cash, orders, positions |
| GET | `/rest/trading/v1/account/wallets` | balance per currency |
| POST | `/rest/v1/equity/order/instruments/settings` | limits per instrument |
| POST | `/rest/v2/public/added-costs` | cost preview |
| POST | `/rest/public/v2/equity/order/validate` | validate order |
| POST | `/rest/public/v2/equity/order` | quantity order |
| DELETE | `/rest/public/v2/equity/order/{id}` | cancel order (asynchronous, status `CANCELLING`) |
| GET | `/rest/v1/equity/order-details?orderId=` | quantity/value/costs of an order |
| POST | `/rest/v1/equity/value-order` (+`/review`, `/validate`) | value order |
| DELETE | `/rest/v1/equity/value-order/{id}` | cancel value order |
| POST | `/instrumentarium/v1/prices/last-trade` | last prices |
| GET | `/rest/instrument-search/v3/config` + Algolia | instrument search |
| GET | `/rest/history/v2/{all\|orders\|dividends\|transactions\|interest}?limit&olderThan&instrumentCode` | history |
| GET | `/rest/history/v5/{type}/{id}` | details of an entry |
| GET | `/rest/taxes/v2/overview` | tax overview |
| GET | `/rest/taxes/v1/pot-summary/pot/{STOCK_LOSS_POT\|LOSS_POT\|WITHHOLDING_TAX_POT}` | loss pots |
| GET | `/rest/customer-taxes/v1/customers/exemption/nv-status` | NV certificate |
| POST | `/rest/reports/v1/income-taxes/tax-transactions/search?limit&idCursor&dateTimeCursor` | tax transactions |
| GET | `/rest/reports/v1/income-taxes/tax-transactions/EQUITY/{taxType}/{incomeId}` | tax details |

Base URL: `https://live.services.trading212.com` (demo: `https://demo.services.trading212.com`).
Required headers: `X-Trader-Client: application=WC4,version=8.54.0,dUUID=<uuid>[,accountId=<id>]`,
`X-Trader-Device-Model`, after login `X-Trader-Target-Type: EQUITY`, for login requests
`X-Trader-Login-Ver: v2`.

## Known limitations

- **Cloudflare Turnstile:** the web client sends the header `X-Trader-TST` (captcha token)
  with the password login. Without it the server may reject the login. It can be set via
  `turnstile_token`; otherwise log in once via `browser-login` and continue with the
  token/session login.
- The capture contained an additional login header `demo: <hash>` of unknown origin. It can
  be set via `[client.extra_login_headers]` if needed.
- Cloudflare may detect and block Python clients by their TLS fingerprint.
- Not verified against a live capture: tax search pagination (`idCursor`/`dateTimeCursor`)
  and the PDF download of trade confirmations.
- `tests/history_fixtures.json` is based on captured responses, anonymized: all ids are
  replaced, money amounts and prices are scaled by a random factor, tax id and e-mail
  removed.
- Unofficial API: changes to the web app can break it at any time. Trading212 also offers an
  official public API with an API key (account settings → API), which is more stable for
  automation.
