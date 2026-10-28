# pfin — one ledger from Alipay, WeChat Pay, bank cards and shopping orders

[简体中文](README.zh-CN.md)

One payment shows up several times on your phone: a row in the WeChat Pay bill, a row in the bank app, a push
notification, a Taobao order. **pfin keeps every record as evidence and merges them into the real events, so each
yuan is counted once**, then sorts the events into your personal, business and investing books by rules you write.

- **Local-first.** Everything lives in one private folder on your Mac. Nothing is uploaded anywhere.
- **Read-only.** It reads bills and notices. No command can pay, transfer, cancel or change a setting.
- **Missing stays missing.** A number it cannot get is never filled with 0; anything that does not reconcile is listed for you.
- **Built for AI agents.** JSON output, plain-JSON rules, stable exit codes. Your agent collects and reconciles; you decide.

```console
$ python3 -m pfin demo
6 records -> 4 events

01-05 20:15  purchase    129.00  某数码旗舰店    <- alipay-transaction + taobao-order
01-08 12:30  p2p         200.00  张三        <- alipay-transaction
01-09 09:00  internal    300.00  花呗        <- alipay-transaction
01-14 12:00  purchase    420.00  某酒店       <- ctrip-naquhua-bill + ctrip-order
```

The Alipay payment and the Taobao order are one purchase. The Ctrip hotel order and the 拿去花 (Ctrip credit) bill two
days later are one purchase. The Huabei repayment is money moving between your own accounts, so it is not spending.
The transfer to 张三 waits for you to say which book it belongs to.

## Quick start

Needs Python 3.11+ (standard library only). macOS for the connectors and collectors; the merge engine runs anywhere.

```bash
git clone https://github.com/risingjerry/china-finance-agent-cli.git
cd china-finance-agent-cli
python3 -m pfin demo                      # made-up data in a temp folder; touches nothing real
```

With your own data (private folder: `PFIN_HOME`, default `~/Library/Application Support/personal-finance`):

```bash
RAW=~/Library/Application\ Support/personal-finance/raw
python3 -m connectors.alipay login                         # scan a QR code (Alipay connector: Node 22 + Chrome)
python3 -m connectors.alipay fetch --sync --out "$RAW/alipay"
python3 -m connectors.taobao fetch --out "$RAW/taobao"     # log in to taobao.com in your browser first
python3 -m pfin import
python3 -m pfin resolve --from 2026-09-01 --to 2026-09-30
python3 -m pfin coverage                                   # which bill covers which days, and the gaps
```

Once it works, list the connectors in `config.json` → `"sync"` and `python3 -m pfin sync --install` keeps it fresh ([`docs/RULES.md`](docs/RULES.md)).

Exported files work too: drop the WeChat Pay bill (`.xlsx`, needs `openpyxl`) into `raw/wechat/`, then `python3 -m pfin import`.

## What it reads

| Kind | Sources |
|---|---|
| Payment bills | WeChat Pay bill export, Alipay official bill export (several accounts), Alipay merchant balance (open platform API) |
| Banks and credit | Bank SMS on the Mac, bank / credit card / Huabei / Meituan Yuefu lists read from the phone screen |
| Shopping and travel | Taobao, JD (+ Baitiao), Ctrip (+ 拿去花), Meituan Yuefu, Dianping, Xianyu (Goofish), Didi, 12306 |
| Crypto exchanges | OKX (C2C + API), Binance, Bitget — investing book only |

Each platform is a **connector**: a small read-only CLI under `connectors/` with the same five verbs.

| Command | Does |
|---|---|
| `python3 -m connectors.<name> manifest` | what the connector is (JSON) |
| `python3 -m connectors.<name> status [--online]` | is the login there, and still accepted? |
| `python3 -m connectors.<name> login` | log in once; you scan the QR code or type the captcha |
| `python3 -m connectors.<name> fetch --out DIR` | raw JSON + `<name>-rows-<ts>.jsonl` in one shared row format |
| `python3 -m connectors.<name> keepalive` | one light read so the session stays warm |

Browser-login connectors read cookies from your own Chromium browser (Chrome, Edge, Brave, Arc, Dia; pick one with
`PFIN_BROWSER=chrome`) through a read-only copy, in memory only. API-key connectors look keys up by name in the
environment, the macOS Keychain (service `pfin`) or a secret-manager command set in `~/.config/pfin/secrets.json`. Writing a new connector: [`docs/CONNECTORS.md`](docs/CONNECTORS.md).

## Framework commands

| Command | Does |
|---|---|
| `pfin demo` | merge a month of made-up records in a throwaway folder |
| `pfin import` / `pfin rebuild` | read new files under `raw/` into the evidence store (idempotent) / rebuild it from scratch |
| `pfin resolve --from --to` | merge records into events, total each book, list counterparties waiting for a decision |
| `pfin coverage` | per account: which bill covers which days |
| `pfin sources` | every dataset, its role, and why a dataset is left out of the merge |
| `pfin invest` | investing book: deposits, withdrawals, futures results, funding fees, earn |
| `pfin sync [--install]` | one full round; `--install` adds the **one** launchd job that runs it every 10 minutes |
| `pfin connectors` / `status` / `login` | every connector, its schedule and login state |

Run them as `python3 -m pfin <command>` from the clone.

## How it works

1. **Records** are kept exactly as the source gave them (`raw/` is never modified) and deduplicated by source id.
2. **Four merge rounds**: payment channels (WeChat, Alipay) create events → bank and credit lines attach as funding →
   notices and SMS attach as signals → orders attach as details. A higher-priority bill owns its own date range.
3. **Rules** put each event in a book: per-transaction overrides > ordered rules > account default > personal.
   Loan repayments are split into principal (internal) and interest.

Details: [`docs/ONTOLOGY.md`](docs/ONTOLOGY.md) (the model and its known data traps), [`docs/RULES.md`](docs/RULES.md)
(config, rules, overrides), [`docs/SOURCES.md`](docs/SOURCES.md) (every source).

## Use with Claude Code, Codex and other agents

The repo ships its agent manual: [`AGENTS.md`](AGENTS.md) (also loaded by Claude Code through `CLAUDE.md`) and a portable
skill at [`skills/pfin/SKILL.md`](skills/pfin/SKILL.md).

```bash
npx skills add risingjerry/china-finance-agent-cli      # or copy skills/pfin to ~/.claude/skills/
```

Then ask:

- "How much did I spend last month, by book? List what still needs my decision."
- "Pull my JD orders and tell me which ones have no matching payment."
- "Which days in September have no bank coverage?"

## Limits

- Connectors and collectors are macOS-only (Keychain, launchd, the Messages database, Chromium cookie decryption).
- Platforms change their pages. A connector that hits a risk-control signature stops and tells you; it never forges one.
- WeChat chat notices need a separate local WeChat reader that is not part of this repo (`pfin.collectors.wechat_*`).
  The WeChat Pay bill export works without it.
- Money is in CNY; the investing book lists coins by quantity and does not price them.

## FAQ

**How do I merge Alipay and WeChat Pay bills without double counting?**
Put both exports (or connector outputs) under `raw/` and run `pfin resolve`. Rows that describe the same money are
merged into one event; `events_seen_in_2plus_sources` tells you how many were matched.

**Can I export my Taobao / JD / Ctrip order history as JSON?**
Yes: `python3 -m connectors.taobao fetch --out ./out` (same for `jd`, `ctrip` ...) writes the raw pages and one JSONL row per order.

**Does it need my password or upload my data?**
No. Logins are QR codes or captchas you complete yourself. Cookies read from your browser stay in memory; the Alipay, OKX, Didi and 12306 tools keep their own login in private (0700/0600) folders in your home directory. No data leaves the machine except the requests to the platform itself.

**Is it safe to let an AI agent run it?**
Every command is read-only and outputs JSON. Secrets are never printed, logged or put in command-line arguments.

## Contributing

Bug reports and new connectors are welcome: [`CONTRIBUTING.md`](CONTRIBUTING.md). Security issues: [`SECURITY.md`](SECURITY.md).

Platform names and logos belong to their owners; this project is not affiliated with any of them.

If pfin saved you an evening of spreadsheet work, a star helps others find it.

## License

[MIT](LICENSE)
