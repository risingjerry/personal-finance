---
name: pfin
description: "Personal finance from records you already have: merge Alipay, WeChat Pay, bank SMS, Huabei / credit lines and Taobao / JD / Ctrip / Didi orders into one ledger where each payment counts once, then report spending per book (personal, business, investing). Use for: how much did I spend, monthly spending by book, reconcile Alipay and WeChat bills, find orders without a payment, export Taobao / JD / Ctrip order history, check which days a bill does not cover, crypto exchange deposits and withdrawals. Read-only: never pays or transfers."
---

# pfin

Repository: https://github.com/risingjerry/personal-finance (run commands from the clone).

## First look

```bash
python3 -m pfin demo          # see what it does with made-up data
python3 -m pfin connectors    # which connectors exist, their schedule and login state
python3 -m pfin coverage      # what data is already there, per account and day range
```

## Answer money questions

| The person asks | Run | Report |
|---|---|---|
| "How much did I spend in September?" | `python3 -m pfin resolve --from 2026-09-01 --to 2026-09-30` | `books.<book>.purchase_out` per book; list `confirm_book_for` and ask which book each belongs to |
| "Is my data up to date?" | `python3 -m pfin coverage` | last day per account; name the gaps |
| "Pull my latest JD orders" | `python3 -m connectors.jd fetch --out "$PFIN_HOME/raw/jd"` then `python3 -m pfin import` | rows written; on exit code 3 ask the person to log in |
| "Crypto deposits and results" | `python3 -m pfin invest` | per exchange and coin, by quantity |

## Exit codes (connectors)

`0` ok · `3` needs login → run `python3 -m connectors.<name> login` and let the person scan / type the captcha ·
`4` blocked by risk control → stop, tell the person, never retry in a loop · `6` network · `70` internal.

## Never

- pay, transfer, cancel, confirm or change anything on a platform; bypass a captcha or risk signature;
- print, log or pass secrets on the command line;
- fill a missing amount with 0 or decide which book an unknown counterparty belongs to without asking.
