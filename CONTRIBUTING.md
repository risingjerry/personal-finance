# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Setup

```bash
git clone https://github.com/risingjerry/personal-finance.git && cd personal-finance
python3 -m pfin demo
python3 -m unittest discover -s tests
for d in connectors/*/tests; do python3 -m unittest discover -s "$d" -t .; done
```

No dependencies beyond Python 3.11+ (and Node 22 for `connectors/alipay`, `connectors/okx/okx-bills`).

## Rules

- **No real data in the repository.** Fixtures are made up: fake names, `13800000000`-style phones, zero-padded ids.
  Never commit a captured page, cookie, token, HAR file or screenshot from a real account.
- Read-only: a connector may only call read endpoints. No pay / transfer / cancel / confirm paths.
- Never forge or bypass a platform's risk-control signature or captcha; stop with exit code 4 instead.
- Money is `Decimal`, never float.
- Tests must run offline, without a browser, Keychain or network.

## A new connector

Follow [`docs/CONNECTORS.md`](docs/CONNECTORS.md): one package under `connectors/<name>/` that declares a `Connector`
(five verbs from `connectors/kit/runner.py`) and writes rows in the shared format. If the framework should merge its
rows, add a normalizer in `pfin/sources/connectors.py`.
