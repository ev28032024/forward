# forward-monitor

Лёгкий мост между каналами Discord и чатами Telegram. Бот читает выбранные каналы, фильтрует сообщения и отправляет их в Telegram, сохраняя базовое форматирование и ссылки на вложения.

## Возможности

- пересылка текста, вложений и embed'ов с очисткой Markdown;
- гибкие фильтры по отправителям, ключевым словам и типам сообщений;
- настройки форматирования и rate limit'ов для каждого канала.

## Установка и запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.yml config.yml
python -m forward_monitor --config config.yml
```

## Минимальная конфигурация

```yaml
telegram:
  token: "TELEGRAM_BOT_TOKEN"
  chat: "@fallback_channel"

discord:
  token: "DISCORD_USER_TOKEN"
  token_type: auto

forward:
  channels:
    - discord: 123456789012345678
      telegram: "-1001234567890"
```

Дополнительные параметры смотрите в `config.example.yml`.
