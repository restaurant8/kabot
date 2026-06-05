# kabot

`kabot` 是面向 Dujiao-Next 的 Telegram Bot 项目。它把数字商品售卖流程搬进 Telegram：商品浏览、下单、支付、交付、订单查询、钱包、礼品卡、会员、分销、运营群发和管理员通知都在一个服务内完成。

项目使用 Python 标准库实现，默认不需要先安装第三方依赖。

## 已实现功能

- Telegram 长轮询和 webhook 两种运行模式
- `/start`、主菜单、帮助、客服、深链参数
- Telegram 命令菜单、站点公告读取、补货通知订阅
- 商品分类、商品列表、商品详情、SKU 选择、库存和促销展示
- 注册用户登录、Telegram 一键登录、游客邮箱 + 查询密码
- 订单预览、优惠码、人工交付表单采集、创建订单
- 支付渠道选择、二维码/跳转链接展示、支付捕获和刷新
- 订单列表、订单详情、取消待支付订单、交付内容展示
- 钱包余额/交易记录/礼品卡兑换入口，支持可配置接口路径
- 会员资料展示
- 分销开通、看板、佣金、提现申请、推广链接和点击上报
- 管理员状态页、测试通知、HTML/图片/文件群发、按用户群发统计
- 外部事件 webhook，用于订单支付、人工交付、库存预警等管理员通知
- SQLite 本地状态、回调按钮短 ID、幂等事件、防重复处理

## 快速开始

```powershell
cd D:\kabot
Copy-Item .env.example .env
notepad .env
python -m kabot init-db
python -m kabot run --mode polling
```

至少需要配置：

- `TELEGRAM_BOT_TOKEN`
- `DUJIAO_BASE_URL`
- `SHOP_BASE_URL`
- `ADMIN_CHAT_IDS`
- `PAYMENT_CHANNELS`

## webhook 模式

```powershell
python -m kabot set-webhook
python -m kabot run --mode webhook
```

Webhook 服务默认提供：

- `POST /telegram/webhook`：Telegram 更新入口
- `POST /kabot/events`：外部业务事件入口
- `GET /healthz`：健康检查

`/kabot/events` 需要请求头：

```text
X-Kabot-Secret: <KABOT_EVENT_SECRET>
```

事件体示例：

```json
{
  "event": "order_paid_success",
  "order_no": "DN202602110001",
  "amount": "99.00",
  "currency": "CNY",
  "customer_label": "user@example.com",
  "items_summary": "Example product x1"
}
```

## 常用命令

```powershell
python -m kabot status
python -m kabot set-commands
python -m kabot broadcast --text "<b>活动通知</b>\n今晚 8 点开始。"
python -m kabot broadcast --text "新品图" --photo-url "https://example.com/new.png"
python -m kabot notify-test
python -m kabot delete-webhook
```

## Dujiao API 说明

官方前台 API 文档覆盖了商品、订单、支付、Telegram 登录和分销接口。钱包接口在公开文档中没有完整列出，所以 `kabot` 将钱包相关路径做成 `DUJIAO_ENDPOINTS` 可配置项。默认路径如下：

```json
{
  "wallet_profile": "/wallet",
  "wallet_transactions": "/wallet/transactions",
  "wallet_redeem_gift_card": "/wallet/gift-cards/redeem"
}
```

如果你的 Dujiao-Next 版本路径不同，在 `.env` 中覆盖：

```text
DUJIAO_ENDPOINTS={"wallet_profile":"/me/wallet","wallet_transactions":"/me/wallet/transactions"}
```

## 安全建议

- 不要把 `.env` 提交到仓库
- `KABOT_EVENT_SECRET` 和 `WEBHOOK_SECRET_TOKEN` 使用高强度随机值
- 卡密/交付内容只通过订单详情获取并只发给订单所属 Telegram 用户
- Telegram 登录要求 Dujiao 后台配置的 Bot Token 与 `TELEGRAM_LOGIN_BOT_TOKEN` 一致
