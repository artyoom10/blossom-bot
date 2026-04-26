# blossom-bot

## Security env

Client API endpoints require Telegram WebApp signed init data in
`X-Telegram-Init-Data`.

Webhook requests require the Telegram secret token header. Set one of:

- `BLOSSOM_WEBHOOK_SECRET`
- `TELEGRAM_WEBHOOK_SECRET`
- `WEBHOOK_SECRET_TOKEN`

Use the same value when configuring Telegram webhook `secret_token`, for example:

```text
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<PUBLIC_URL>/webhook&secret_token=<SECRET>
```
