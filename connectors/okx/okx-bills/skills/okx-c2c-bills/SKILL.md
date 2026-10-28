---
name: okx-c2c-bills
description: "用本机 okx-cli 只读查询/同步/导出 OKX 的 C2C（P2P）法币订单与充币/提币流水。用于：OKX 账单、C2C 买 USDT 记录、充提币记录、法币出入金对账、导出 OKX CSV、上个月买了多少 USDT、提了多少 USDT。只读，不下单不放币不转账。"
---

# OKX C2C 订单（okx-cli）

一次登录，之后本地查询毫秒级；新数据用 `okx-bills sync` 增量拉。**只读**。

命令 `okx-bills`（软链到本仓库 `connectors/okx/okx-bills/bin/okx-bills`）。

## 先查本地，再决定要不要联网

```bash
okx-bills doctor --offline          # 不联网自检
okx-bills orders summary --by month # 有数据就直接答
okx-bills orders list --limit 20 --side buy --currency USDT
```

- 本地数据够了**不要**顺手 sync：联网要开 Chrome、占用当前会话模板。

## 三条最短路径

| 用户说 | 执行 | 交付 |
|---|---|---|
| 「我 C2C 买了多少 USDT」 | `okx-bills orders summary --by currency --side buy` | 报 `buy_quote_total`(人民币) 与 `buy_base_total`(USDT) |
| 「导一份 OKX 账单给我」 | `okx-bills export --format csv --out <path>` | 回路径 + 行数 + sha256 |
| 「拉最新 / 补最近的单」 | `okx-bills sync` | 报 added/updated/stored |
| 「提了多少 U / 充提记录」 | `okx-bills flows summary --by currency` | 报 deposit/withdraw 数量与手续费 |

## 缺数据或要刷新才同步

```bash
okx-bills sync                      # 默认 2017-01-01 至今，按订单号 upsert
okx-bills sync --from 2026-01 --to 2026-10
```

- 403 / `api_forbidden` 是签名过期，`sync` 会自动重载页面重录模板并重试一次。
- `need_login`：让真人跑 `okx-bills auth login`，在弹出的 Chrome 窗口扫码（或账号密码 + 2FA），
  登录成功后会自动录模板。**不要**让 Agent 自己输密码/2FA。

## 充值/提币流水

```bash
okx-bills flows list --type withdraw --limit 20
okx-bills flows summary --by month
okx-bills export --kind flows --format csv --out <path>
```

接口：`/v2/asset/deposit/list-new`、`/v2/asset/withdraw/list`（分页 `currentPage/pageLength`，每页上限 100）。
默认时区 **Asia/Shanghai**。

## 读结果的硬规则

1. 退出码：`0` 成功；`1` 失败——错误 JSON 在 **stderr**。
2. stdout 是 `{"ok":true,"cmd":…,"data":…,"meta":…}`。
3. 金额是**十进制字符串**，不要转 float 相加；汇总用 CLI 的 `summary` 或导出 CSV。
4. 默认只统计 `completed`；`cancelled`（取消单）不计入金额，`--include-cancelled` 才导出。
5. `side`：`buy` 买入 / `sell` 卖出；`base_amount` 是币数量，`quote_amount` 是法币金额。
6. 流水 `type`：`deposit` 充值 / `withdraw` 提币；`fee` 只有提币有，充值的手续费由网络方收取、OKX 不记。

## 出错对照

| `error.code` | 动作 |
|---|---|
| `need_login` | 请真人 `okx-bills auth login` 扫码，成功后重试一次 |
| `api_forbidden` | 已自动重试一次仍失败：`okx-bills sync --refresh-template` |
| `browser_down` | `okx-bills auth status --launch`；端口 9340 若被别的 Chrome 占用，换 `--port` |
| `no_data` | 先 `okx-bills sync` |
| `bad_input` / `usage` | 看 details 改参数 |

## 安全红线

- 不要读取/打印/外传 `~/.okx-cli/session/request-template.json`（含 JWT）。
- 不要把 cookie/JWT 放进 curl 或任何外部请求；所有请求必须走页面内 `fetch`。
- 不要提交 `~/.okx-cli/`（profile、订单、原始快照）到任何仓库。
- 不要用 9340 端口跑别的 Chrome：okx-cli 会校验 profile，串了会直接报错。
