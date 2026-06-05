# 外部业务事件

`kabot` 可以作为 Dujiao-Next 外围扩展运行。站点、支付网关、库存任务或人工交付任务可以把事件推送到 Bot，由 Bot 通知管理员和相关 Telegram 用户。

请求：

```http
POST /kabot/events
Content-Type: application/json
X-Kabot-Secret: <KABOT_EVENT_SECRET>
```

推荐事件类型：

- `order_created`
- `order_paid_success`
- `order_fulfilled`
- `fulfillment_ready`
- `wallet_recharged`
- `stock_low`
- `stock_empty`
- `stock_restocked`
- `stock_available`
- `payment_failed`

示例：

```json
{
  "event": "order_paid_success",
  "event_id": "pay_202602110001",
  "order_no": "DN202602110001",
  "status": "paid",
  "amount": "99.00",
  "currency": "CNY",
  "customer_label": "user@example.com",
  "items_summary": "Example product x1",
  "message": "Payment callback confirmed"
}
```

事件幂等：

- 如果提供 `event_id`，Bot 使用它去重。
- 如果没有 `event_id`，Bot 会用 `event + order_no + status + updated_at` 组合去重。
