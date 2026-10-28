# okx-bills

把「OKX C2C（P2P）法币订单」和「充币/提币流水」变成一套可被 Agent 随时调用的**只读**本地 CLI：
一次登录，之后按需增量同步，随时导出 CSV。

零第三方依赖，只用 Node 22 内置能力（`fetch`、`WebSocket`）。

> 只读承诺：只调用三个查询接口——`/v4/c2c/order/getOrderList`（C2C 订单）、
> `/v2/asset/deposit/list-new`（充币）、`/v2/asset/withdraw/list`（提币）。
> 代码里没有下单、付款、放币、取消、申诉、提币任何调用路径。

## 为什么是这样实现的（重要）

OKX 的 C2C 订单接口 `/v4/c2c/order/getOrderList` **不认裸 cookie**：
请求必须带浏览器里生成的 `Authorization`(JWT) + `X-Request-Timestamp` +
`X-Client-Signature`(P1363) + `Devid` 等一整套头，缺一个就 `403`。

所以同步分两步：

1. 打开真实的 OKX 页面（`/zh-hans/p2p/orders-completed`），让**页面自己**发一次
   订单请求，我们把这次请求的头录下来（模板）；
2. 用这份头模板，在**页面上下文里** `fetch()` 我们自己的分页/时间窗查询。

实测结论：
- 签名**不绑定 query**：换 `pageIndex` / 时间范围照样 200（已验证）。
- 签名会**过期**：403 时自动重载页面重新录一次模板并重试一次。

## 安装

```bash
ln -sf "$PWD/bin/okx-bills" ~/.local/bin/okx-bills   # 在 connectors/okx/okx-bills 下执行
okx-bills doctor      # 自检
```

## 快速开始

```bash
okx-bills auth login                  # 弹出可见 Chrome 窗口，扫码/密码登录，登录后自动录模板
okx-bills sync                        # 拉全量（默认 2017-01-01 至今），按订单号增量入库
okx-bills orders summary --by month   # 按月汇总
okx-bills export --format csv --out ~/Desktop/okx-c2c.csv
```

会话过期时 `sync` 会报 `need_login`，重跑 `okx-bills auth login` 即可（OKX 网页会话通常能活很久）。

## 命令

| 命令 | 说明 |
|---|---|
| `okx-bills auth login [--timeout 300]` | 打开登录窗口并等待登录；登录后立即录制请求模板 |
| `okx-bills auth status [--launch]` | 会话状态：`ok` / `need_login` / `browser_down` + 模板状态 |
| `okx-bills sync [--from] [--to] [--order-type all\|completed\|pending] [--page-size 100] [--refresh-template] [--only all\|c2c\|flows]` | 增量同步 C2C 订单 + 充提流水（按 id upsert） |
| `okx-bills orders list [--from] [--to] [--side buy\|sell] [--status completed\|cancelled] [--currency USDT] [--q 关键词] [--limit N] [--offset N]` | 查询本地订单 |
| `okx-bills orders summary [--by month\|side\|currency\|status\|counterparty\|method] [同上过滤]` | 聚合（金额精确十进制字符串，不经过 float） |
| `okx-bills flows list \| summary [--type deposit\|withdraw] [--by month\|type\|currency\|network\|status] [过滤]` | 查询充值/提币流水 |
| `okx-bills export --kind c2c\|flows --format csv\|json\|ndjson [--tz Asia/Shanghai] [过滤] [--out FILE\|-]` | 导出；C2C 默认只含已完成订单，`--include-cancelled` 可含取消单 |
| `okx-bills doctor [--offline]` | 环境/数据/会话自检 |

全局参数：`--home DIR`（默认 `~/.okx-cli`）、`--port N`（默认 **9340**）、`--pretty`、`--quiet`。

## 数据

| 位置 | 内容 |
|---|---|
| `~/.okx-cli/profile` | 专用 Chrome profile（会话 cookie 在这里，不导出、不复制） |
| `~/.okx-cli/session/request-template.json` | 录到的请求头模板（含 JWT，0600，20 分钟有效期内复用） |
| `~/.okx-cli/data/orders.ndjson` | 归一化 C2C 订单库（按订单号 upsert） |
| `~/.okx-cli/data/flows.ndjson` | 归一化充币/提币流水（按记录 id upsert） |
| `~/.okx-cli/data/raw/orders-*.json` | 每次同步的原始 API 快照（留证） |
| `~/.okx-cli/data/meta.json` | 上次同步时间/窗口/计数 |
| `~/.okx-cli/exports/` | `okx-bills export --out` 的默认落点（示例） |

## CSV 列

**C2C 订单**：订单号、公开单号、创建时间、完成时间、方向、币种、数量、法币、法币金额、单价、
结算汇率、状态、付款状态、对手方、收款方式、订单标签、付款时间。

- 时间默认按**本机时区**格式化（`YYYY-MM-DD HH:mm:ss`），`--tz Asia/Shanghai` 可切换；
  ndjson/json 里始终是 UTC ISO。
- 金额是字符串原样输出，不做 float 运算。

**充值/提币流水**（`--kind flows`）：类型（充值/提币）、时间、币种、数量、网络、地址、
交易哈希、手续费、手续费币种、状态、内部转账、ID。默认北京时间。

> 默认时区统一为 **Asia/Shanghai**（跟支付宝账单对得上）；要改成 UTC 或本机时间用 `--tz`。

## 测试

```bash
node --test tests/*.test.js
```

## 已知边界

- 覆盖 C2C/P2P 订单 + 充币/提币流水；现货成交、资金划转（account transfer）不在其中。
- 订单查询的时间窗由参数决定，服务端无 6 个月限制（一次就能拿全历史）。
- `pageSize` 上限实测 100；总单数少时一页拿完。
