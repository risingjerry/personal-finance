# alipay-cli

把「支付宝个人交易记录（账单）」变成一套可被 Agent 随时调用的**只读**本地 CLI。

一次扫码登录，之后所有查询都在本地完成（离线、毫秒级）；需要新数据时按年增量同步，
导出原始 zip 留档。零第三方依赖，只用 Node 22 内置能力（`fetch`、`WebSocket`、`zlib`）。

> 只读承诺：只允许 GET `record/*.htm` 与 POST `record/download.resource`（账单导出）。
> 代码里没有任何支付、转账、退款、提现、关闭订单的调用路径。

## 商户账单（同一个账号的「商家」那一面）

商户账单走**官方开放平台 API**（`openapi.alipay.com`，RSA2 签名，无限流），
和个人侧（网页 + 浏览器会话）是两个数据源、两个库，但同属一个 `alipay` 二进制。

```bash
alipay merchant status                  # 条数、覆盖、配置
alipay merchant sync --from 2026-05-01 --to 2026-10-27 --kind both --concurrency 6
alipay merchant list --from 2026-10-01 --to 2026-10-26 --kind funds
alipay merchant summary --by month
alipay merchant balance                 # 实时余额
alipay merchant query --kind sell --from 2026-10-20 --to 2026-10-23
alipay merchant trade --trade-no 2026...    # 单笔 / 加 --refund-no 查退款
```

| | 个人账单 | 商户账单 |
|---|---|---|
| 来源 | 网页交易记录（浏览器会话） | `alipay.data.*.bill.*` 官方 API |
| 凭据 | cookie 快照 | `app_id` + 应用私钥（`~/.alipay-cli/config.json` 的 `private_key_file` 或 `private_key_command`） |
| 本地库 | `~/.alipay-cli/data/` | `~/.alipay-cli/merchant/` |
| 限流 | 导出配额 | 无 |
| 全量归档 | 10 年 3–5 分钟 | 6 个月 **约 5 秒**（并发 6） |

商户侧的账单按「整月优先、日级回退」拉取，逐文件核对支付宝自带的
`#收入合计 / #支出合计`，对不上就拒收该文件；原始 zip 留在 `merchant/raw/`。
`merchant sync` 的 `--to` 是排他日期且最晚到明天（T+1 账单）。

## 安装与卸载

```bash
scripts/install.sh               # 建软链 ~/.local/bin/alipay + 自检（不联网）
scripts/uninstall.sh             # 移除软链；数据默认保留，--purge 才删 ~/.alipay-cli
```

`cli.json` 描述全部对外契约（命令、数据目录、只读承诺），方便被统一的 CLI 管理器接管。

## 环境要求

- Node.js **>= 22**（用到全局 `WebSocket` 与 `TextDecoder('gb18030')`）
- Google Chrome（默认 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，
  可用 `ALIPAY_CLI_CHROME` 覆盖）
- 独立 Chrome profile：`~/.alipay-cli/profile`（不会碰你日常用的 Chrome）

## 安装

```bash
cd connectors/alipay
ln -sf "$PWD/bin/alipay" ~/.local/bin/alipay   # 或者直接 ./bin/alipay
```

无需 `npm install`（没有依赖）。`npm test` 用 `node --test`。

## 快速开始

```bash
alipay auth login            # 开一个离屏 Chrome，扫码，成功后把 cookie 快照存进 vault
alipay doctor                # 自检：node / Chrome / profile / vault / 数据 / 会话
alipay sync --from 2017 --to 2026   # 首次全量（按年导出，45s 节流，可断点续传）
alipay bills summary --by month --from 2025-01 --to 2025-12
alipay bills list --direction out --min 100 --limit 20
alipay export --format csv --from 2025 --out ~/Desktop/2025.csv
```

### 登录能撑多久

支付宝网页会话很短：**空闲约 15–30 分钟就失效**，同一浏览器没有任何操作也会退回登录页。

所以默认工作流就是**要同步时扫一次码**：

```bash
alipay auth login && alipay sync     # 扫码 → 立刻导出，几秒钟的事
```

已有的本地数据查询、汇总、导出**完全不需要登录**，随时可用。
如果哪天想让它白天一直活着，可以装可选的保活 agent（每 5 分钟 ping 一次记录页）：

```bash
alipay keepalive install --interval 300
alipay keepalive status
alipay keepalive uninstall
```

它只能对冲「空闲过期」，对冲不了合盖睡眠/重启；会话真被吊销时仍然要重新扫码。

已经手工下载过账单的话，可以完全离线入库：

```bash
alipay import ~/Downloads/alipay_record_2025.zip
alipay import ~/.alipay-cli/data/raw        # 目录会递归取 .zip / .csv
```

`import` 是幂等的（同字节文件复用 `data/raw` 里的证据，不重复复制）；完整覆盖某年的文件
会把该年记为 `complete`，当年（未过完）的文件记为 `imported`——下次 `sync` 仍会刷新当年。
同一目录里 `2017.zip` 与 `2017.csv` 并存时只取 zip，不会重复解析。

## 命令

| 命令 | 说明 |
|---|---|
| `alipay auth login [--timeout 300]` | 开离屏 Chrome，等扫码，成功落 cookie 快照 |
| `alipay auth status [--launch]` | 会话状态：`ok` / `need_login` / `security_challenge` / `browser_down` |
| `alipay keepalive install\|status\|uninstall [--interval 600]` | 会话保活的 launchd agent（避免 ~30 分钟空闲失效） |
| `alipay doctor [--offline]` | 环境 + 数据自检；有 `fail` 项时退出码 1 |
| `alipay sync [--from Y] [--to Y] [--force] [--throttle 45] [--span-years 1] [--max-retries 2]` | 按年导出并入库，可续传 |
| `alipay import [file.zip\|file.csv\|dir...] [--year YYYY]` | 解析已有导出入库，不联网 |
| `alipay bills list [--from] [--to] [--direction in\|out\|none] [--q TEXT] [--min N] [--max N] [--limit 50] [--offset 0] [--order asc\|desc]` | 查询本地账单 |
| `alipay bills summary [--by year\|month\|counterparty\|type\|status\|source] [--from] [--to] [--direction] [--top N]` | 聚合（金额精确十进制） |
| `alipay export --format json\|csv\|ndjson [filters] [--out FILE\|-]` | 导出切片；`--out -` 直接流式输出 |
| `alipay mcp` | 可选：stdio MCP server（`bills_list` / `bills_summary` / `bills_status`） |

全局参数：`--home DIR`（默认 `~/.alipay-cli`，也可用 `ALIPAY_CLI_HOME`）、
`--port N`（默认 9333）、`--pretty`、`--quiet`。

日期参数接受 `YYYY`、`YYYY-MM`、`YYYY-MM-DD`（按 `+08:00` 解释，含首含尾）。

## 输出契约

- 成功：stdout 一个 JSON 对象 `{"ok":true,"cmd":"...","data":{...},"meta":{...}}`，退出码 0
- 失败：stderr 一个 JSON 对象 `{"ok":false,"error":{"code":"...","message":"...","details":{...}}}`，退出码 1
- 进度/诊断：stderr 的 `[alipay] ...` 行（`--quiet` 静默）
- 唯一例外：`export --out -` 直接输出原始数据（csv/json/ndjson），便于管道

## 数据与目录

```
~/.alipay-cli/
  profile/                  Chrome user-data-dir（0700，独立于日常 Chrome）
  vault/cookies.json        cookie 快照（0600，绝不进 git / 日志）
  data/raw/<label>.zip      每次导出的原始证据
  data/raw/<label>.csv      解压后的 GB18030 CSV（便于人工核对）
  data/bills.ndjson         规范化后的全量账单（一行一笔，trade_no 主键）
  data/meta.json            每年完成状态、行数、区间、sha256、限流信息
  lock                      单飞锁（open(O_EXCL) + pid 存活检测）
```

一条记录：

```json
{
  "id": "2025102022000000000000000000_3005000000000000000",
  "trade_no": "2025102022000000000000000000_3005000000000000000",
  "merchant_order_no": "T200P0000000000000000",
  "created_at": "2025-10-21T12:10:05+08:00",
  "paid_at": null,
  "modified_at": "2025-10-21T12:10:05+08:00",
  "source": "淘宝",
  "type": "支付宝担保交易",
  "counterparty": "t***0",
  "item": "退款-示例商品",
  "amount": "23.00",
  "direction": "none",
  "status": "退款成功",
  "fee": "0.00",
  "refund": "0.00",
  "note": "",
  "fund_state": "已收入",
  "year": 2025,
  "month": "2025-10",
  "raw": ["...17 列原值（已去 padding）..."]
}
```

- **金额一律十进制字符串**，内部用 BigInt 分做算术，任何环节都不经过 float。
- `direction`：`out`=支出 · `in`=收入 · `none`=不计收支（退款/还款/冻结等）。
- `trade_no` 原样保留：退款行可能是 `..._3000000000000000001` 或 `...*T200P4000000000000000001`，
  不是纯数字，因此解析器**不按数字过滤行**。
- 去重主键 `trade_no`；跨区间重复时取 `modified_at` 较新者。

## 两个容易踩的语义

1. **导出区间的 `endDate` 是「当天 00:00」排他**：
   `Y-01-01..(Y+1)-01-01` 的导出不含 `(Y+1)-01-01` 当天的记录。所以本 CLI 的年份窗口是
   `Y-01-01 .. (Y+1)-01-01`（当年则是 `今天+1`），正好不重不漏。

2. **页脚合计是「金额 − 成功退款」后的净值**。
   例如：支出金额合计 `1000.00`，成功退款 `100.00`，页脚写 `900.00`。
   因此 `bills summary` 同时给毛额与净额：
   `expense` / `income`（毛额）、`refunds_out` / `refunds_in`、`expense_net` / `income_net`。
   `income_net` / `expense_net` 与支付宝自己的页脚**逐分对得上**。

## 同步策略与限流

- 默认**按年**一个导出窗口；`--span-years 2` 可合并成两年一次（更省请求、但单次生成更慢）。
- 两次导出之间默认 `--throttle 45` 秒（跨进程生效，记录在 `meta.json.lastAttemptAt`）。
- 命中「系统繁忙」→ `rate_limited`：指数退避 60s → 120s → 300s，最多 `--max-retries 2` 次；
  该状态实测落在 `record/download.htm` 跳转的
  `error.htm?functionCode=FUN.ALL&causeCode=ERROR.SYSTEM_BUSY`（页面文案「抱歉，系统繁忙，请稍候再试。」），
  CLI 会从 URL 和文案两处识别；
  可完成的年份先落盘，失败年份留在 `pending`，下次 `sync` 自动续传。
- 已完成的年份默认跳过（`--force` 重导）。导出 0 行的年份记为 `empty`，不会反复重试。
- 单飞锁保证同一时刻只有一个写操作（`sync` / `import`）。

## 失败码

| code | 含义 | 处理 |
|---|---|---|
| `need_login` | 会话失效，被跳到登录页 | 人扫码：`alipay auth login` |
| `security_challenge` | 需要短信/身份校验 | 人在 Chrome 窗口里完成校验后重试 |
| `rate_limited` | 支付宝「系统繁忙」 | 等冷却（≥ 上次退避值）再试，别刷 |
| `browser_down` | 9333 上没有 Chrome | `alipay auth login` 或 `alipay sync` 会自动拉起 |
| `locked` | 另一个进程在写 | 等它结束；陈旧锁会自动回收 |
| `bad_zip` / `bad_csv` | 下载物损坏 | 重试用例；原始文件留在 `data/raw/` |
| `empty` | 该区间没有交易 | 正常，记为 `empty` |

## 验证范围

已在真实账号上验证：会话复用、扫码登录、按年导出（`download.htm` + 页内 `fetch download.resource`）、
导入官方导出并与支付宝页脚净值逐分一致。同步引擎另有离线测试（成功/限流退避/断点续传/坏包/跨年窗口，见 `tests/sync.test.js`）。

未实现：`standard.htm` 的 HTML 列表增量通道（`pageNum` 需 POST + `rdsToken`），只用于会话探测。

## 排错

```bash
alipay doctor --offline          # 只看本地，不碰浏览器
alipay auth status --launch      # 起浏览器并做一次会话探测
ALIPAY_CLI_DEBUG=1 alipay sync --from 2024 --to 2024   # 异常时带 stack
```

- 9333 被别的 Chrome 占用时，CLI 会用 `chrome://version` 验证 profile，
  不是 `~/.alipay-cli/profile` 就拒绝操作（绝不往别人的浏览器里注入 cookie）。
- 想换端口：`--port 9444` 或 `ALIPAY_CLI_CDP_PORT=9444`。
- `browser_down` 但 Chrome 明明开着：多半是端口/`--home` 不一致，`--port` 对齐即可。
