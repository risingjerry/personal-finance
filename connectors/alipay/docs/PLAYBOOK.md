# PLAYBOOK — Agent 接入契约（alipay-cli v1）

给会调用 `alipay` 的 Agent 看的操作手册。只讲「怎么调、怎么判断、出错怎么办」。

## 0. 黄金规则

1. **先看退出码，再读 stdout。** `0` = 成功；`1` = 失败，错误在 **stderr** 的 JSON 里。
2. **stdout 永远是一个 JSON 对象**（唯一例外：`export --out -` 直接流式输出数据）。
3. **不要重试限流。** 收到 `rate_limited` 就把冷却时间原样报给用户，等够了再单独跑一次。
4. **写操作单飞。** `sync` / `import` 自带锁；不要并发调用，也不要自己循环调用。
5. **只读数据。** 本 CLI 不可能发起支付/转账/退款——不需要在提示词里额外防护。
6. **别把 cookie 贴进上下文。** 需要登录时让人去扫码，不要读 `~/.alipay-cli/vault/`。

## 1. 三十秒判断：这事该不该调 CLI

| 用户意图 | 做法 |
|---|---|
| 「我去年花了多少」「某商户/某月明细」「导出一份账单」 | 直接 `bills summary` / `bills list` / `export`（纯本地，0 网络） |
| 「同步一下最新账单」「把 2024 年补上」 | `sync --from Y --to Y`（会联网、会占用浏览器） |
| 「我下载了账单文件，导进去」 | `import <file>`（不联网，最安全） |
| 「登录过期了」 | 让真人在弹出的 Chrome 里扫码：`auth login` |
| 「环境有问题/查不了」 | `doctor`，把 `checks` 里 `fail`/`warn` 项原样贴出来 |

## 2. 推荐调用顺序

```bash
alipay doctor --offline        # 1. 本地自检（不联网、不碰浏览器，~0.1s）
alipay bills summary --by month --from 2025   # 2. 先看库里有没有数据
alipay sync --from 2025 --to 2025             # 3. 缺数据才同步（按需、单次）
```

`doctor --offline` 里 `warn` 不代表坏了（例如「还没同步过」就是 warn）。只有 `fail` 才是硬问题。

## 3. 命令与返回结构（虚构样例）

### 3.1 `alipay bills summary --by month`

```json
{"ok":true,"cmd":"bills.summary","data":{
  "by":"year",
  "groups":[{"key":"2025","count":12,"income":"1200.00","expense":"1000.00","neutral":"300.00",
             "net":"200.00","refunds_in":"0.00","refunds_out":"100.00",
             "income_net":"1200.00","expense_net":"900.00",
             "first_at":"2025-01-03T12:00:00+08:00","last_at":"2025-12-20T12:00:00+08:00"}],
  "groupCount":1,
  "totals":{"matched":12,"income":"1200.00","expense":"1000.00","neutral":"300.00",
            "net":"200.00","refunds_in":"0.00","refunds_out":"100.00",
            "income_net":"1200.00","expense_net":"900.00"}},
 "meta":{"version":"1.0.0","home":"/Users/…/.alipay-cli","elapsedMs":4,"storeRecords":12,…}}
```

**怎么读金额**：全部是十进制字符串，不要再转 float 相加（会丢分）。
- `expense` = 支出毛额；`expense_net` = 支出 − 成功退款（与支付宝页脚一致）
- `net` = `income − expense`；`neutral` = 不计收支（退款、还款、冻结）的金额，不参与 `net`
- 需要精确加总时用 `--by` 分组后的条目，或直接 `export --format csv` 交给别的工具

### 3.2 `alipay bills list`

```json
{"ok":true,"cmd":"bills.list","data":{
  "total":2,"returned":2,"offset":0,"limit":50,
  "records":[{"trade_no":"…","created_at":"2025-07-28T…+08:00","amount":"100.00",
              "direction":"out","counterparty":"示例商户","item":"示例商品","status":"交易成功",
              "year":2025,"month":"2025-07","raw":["…17 列…"]}]},
 "meta":{"storeRecords":2,…}}
```

- 默认按 `created_at` **倒序**，`--order asc` 反转；`--limit 0` = 全部
- `--q` 模糊匹配 对方/商品/备注/类型/来源/交易号/商户单号
- `--min/--max` 单位是元，按 BigInt 分精确比较
- 要分页：`total` 是匹配总数，`returned` 是本页条数

### 3.3 `alipay export`

```json
{"ok":true,"cmd":"export","data":{
  "file":"/tmp/x/bills.csv","format":"csv","rows":12,"bytes":4096,
  "sha256":"<文件的 SHA-256>","columns":["trade_no","merchant_order_no",…]}}
```

`--out -` 时不带信封，直接把 csv/json/ndjson 写到 stdout（适合管道）。
文件写出权限 0600。

### 3.4 `alipay sync`

```json
{"ok":true,"cmd":"sync","data":{
  "status":"complete","ok":true,
  "years":[{"year":"2024","years":[2024],"status":"complete","rows":10,"added":10,"updated":0,
            "range":{"begin":"2024-01-01","end":"2025-01-01"},"raw":"data/raw/2024-….zip",
            "exportMs":1000,"issues":[]}],
  "skipped":[],"failed":[],"pending":[],"added":10,"updated":0,"records":12,
  "rateLimited":false,"elapsedMs":10000}}
```

判定顺序：
1. `data.status`：`complete` / `nothing_to_do` / `partial` / `rate_limited` / `failed`
2. `data.pending`：本次没做成的年份（下次 `sync` 会自动续传，不用 `--force`）
3. `data.failed`：失败项及 `code` / `detail`（坏包之类，值得人看一眼）

`status` 为 `partial` / `rate_limited` 时退出码是 1，但已完成年份**已经落盘**，不要重跑全量。

### 3.5 `alipay auth status`

```json
{"ok":true,"cmd":"auth.status","data":{
  "session":"ok","reason":"session markers present",
  "browser":{"port":9333,"up":true,"launched":false},
  "vault":{"file":"/Users/…/vault/cookies.json","count":2,"savedAt":null},
  "injectedFromVault":false,"checkedAt":"2022-01-01T00:00:00.000Z"}}
```

`session` 的四个取值与动作：

| session | 含义 | Agent 动作 |
|---|---|---|
| `ok` | 可以读/可以同步 | 继续 |
| `need_login` | 被跳登录页（网页会话空闲 15–30 分钟就过期，属正常） | 让真人跑 `alipay auth login` 扫码，成功后再 `alipay sync` |
| `security_challenge` | 需要短信/身份校验 | 让真人在 Chrome 窗口完成，然后重试一次 |
| `browser_down` | 9333 没浏览器（默认不自动启动） | `alipay auth status --launch` 或直接 `sync` |

## 4. 出错处理矩阵

| stderr `error.code` | 是不是 Agent 能自己修的 | 动作 |
|---|---|---|
| `usage` / `bad_input` | 是 | 读 `error.details`，改参数重试一次 |
| `no_data`（库里为空） | 是 | `alipay import <已有文件>` 或 `sync` |
| `locked` | 是（等） | 等 `details.ageMs`/对方进程结束；>2h 的陈旧锁会自动回收 |
| `rate_limited` | **否** | 报告 `details.excerpt` + 建议冷却时间（≥ 上次退避），**不要立刻重试** |
| `need_login` | 否（要真人扫码） | 请用户跑 `alipay auth login` |
| `security_challenge` | 否（要真人校验） | 请用户在 Chrome 窗口完成校验 |
| `browser_down` | 部分 | 先 `auth status --launch`；仍是 down 就看 `details.hint` |
| `bad_zip` / `bad_csv` | 否 | 报给用户，附 `details`；原始文件在 `data/raw/` |
| `internal` | 否 | 带 `error.message`（`ALIPAY_CLI_DEBUG=1` 有 stack）报告 |

限流退避是 60s → 120s → 300s。**在同一轮对话里不要连续调用 `sync` 超过 2 次**；
真的还缺数据，就把 `pending` 列表和 `meta.rateLimit` 一起报给用户，等下一个时间窗。

## 5. MCP 接入（可选）

```bash
alipay mcp        # stdio，newline-delimited JSON-RPC 2.0
```

工具：`bills_list`、`bills_summary`、`bills_status`（全部只读、不联网）。
适合让 Agent 直接把「查账」当工具用，而不是拼命令行。

## 6. 性能与限制

- 本地查询和导出不联网，耗时随本地记录条数和文件大小增长。
- `auth status` 需要浏览器导航，耗时取决于页面响应。
- `sync` 的耗时取决于平台生成账单的速度和限流状态；优先按年分段并保留断点。

## 7. 不要做的事

- ❌ 不要 `while true; do alipay sync; done` —— 会把账号打进「系统繁忙」冷却。
- ❌ 不要用 `curl` 重放 cookie —— 支付宝会判定为异常，返回登录/安全页。
- ❌ 不要在 `--out -` 的同时期望 JSON 信封。
- ❌ 不要把 `vault/cookies.json` 的内容打印、外传或提交。
- ❌ 不要以为 `bills summary` 的 `expense` 是「净支出」——净支出看 `expense_net`。
