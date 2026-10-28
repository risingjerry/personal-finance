# Agent guide

pfin merges a person's payment records (Alipay, WeChat Pay, bank SMS, credit lines, shopping orders, exchanges) into
events counted once and books them by the owner's rules. Python 3.11+, standard library only; two connectors
(`connectors/alipay`, `connectors/okx/okx-bills`) are Node 22 programs.

## Run

```bash
python3 -m pfin demo                         # made-up data, safe anywhere
python3 -m pfin <command> --help             # import, resolve, coverage, sources, invest, sync, connectors, status, login
python3 -m connectors.<name> <verb>          # manifest | status | login | fetch --out DIR | keepalive
```

Private data lives in `PFIN_HOME` (default `~/Library/Application Support/personal-finance`): `raw/` (never modified),
`evidence.sqlite`, `config.json`, `rules.json`, `overrides.json`. Reports go to `PFIN_HOME/recon/`.

## Test

```bash
python3 -m unittest discover -s tests                                   # framework
for d in connectors/*/tests; do python3 -m unittest discover -s "$d" -t .; done
(cd connectors/alipay && npm test) ; (cd connectors/okx/okx-bills && npm test)
```

Tests use temporary folders only. Never point them at a real `PFIN_HOME`.

## Rules for agents

- Read-only. Never add a code path that pays, transfers, cancels, confirms, withdraws or changes platform settings.
- Never forge or bypass risk-control signatures (h5st, mtgsig, a_bogus, wsgsig, rdsToken, captchas). Stop and report.
- Logins (QR codes, captchas) are done by the person. Start `login`, then let them finish it.
- Secrets (cookies, tokens, API keys) never go to stdout, logs, files, command-line arguments or error messages.
  API keys are read by name (`connectors/kit/secrets.py`).
- Amounts are `Decimal` / decimal strings, never float. Times are converted to the report time zone, raw times kept.
- A missing number is reported as missing, never as 0.
- `raw/` is append-only evidence: add new files, never edit or delete existing ones.
- The framework talks to connectors only through subprocesses and the files they write (`pfin/connect.py`).
- Answering "how much did I spend": run `pfin resolve --from --to` and read `books`; items in `confirm_book_for`
  need the owner's decision, so ask rather than guess.

## Layout

```
pfin/            framework: sources/ (file -> records), resolve.py (records -> events), rules.py (books), sync.py
connectors/      one read-only CLI per platform; kit/ is shared (runner, cookies, http, rows, secrets)
docs/            ONTOLOGY.md (model), RULES.md (config + rules), SOURCES.md, CONNECTORS.md (connector contract)
skills/pfin/     portable Agent Skill
tests/           framework tests
```
