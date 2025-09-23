# Forward Monitor

Forward Monitor — асинхронный мост между каналами Discord и чатами Telegram. Он деликатно очищает и форматирует сообщения, пересылает вложения и поддерживает тонкую настройку поведения для каждого канала.

## Основное
- **Форматирование.** Чипы, заголовки, подвал, автоматические замены текста, аккуратная работа с Markdown и HTML.
- **Вложения.** Текстовые сводки для файлов и embed-блоков, сохранение ссылок и описаний.
- **Фильтры.** Гибкие белые и чёрные списки по словам, авторам и типам контента.
- **Скорость и устойчивость.** Раздельные лимиты отправки, поддержка прокси-пулов, сохранение состояния между перезапусками.

## Требования
- Python 3.11+
- Аккаунт или бот Discord с действующим токеном
- Бот Telegram с токеном BotFather

## Установка
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.yml config.yml
python -m forward_monitor --config config.yml
```

## Конфигурация
Настройка выполняется YAML-файлом. Подробные значения и комментарии приведены в [`config.example.yml`](config.example.yml). Ниже кратко описаны ключевые блоки.

### telegram
- `token` *(обязательно)* — токен бота Telegram.
- `chat` *(обязательно)* — fallback-канал или чат.
- `rate_limit.*` — скорость (`per_second`, `per_minute`), параллелизм (`concurrency`), случайные задержки (`jitter_min_ms`, `jitter_max_ms`) и `cooldown_seconds`. Значение `null` включает дефолты API.
- `formatting` — профиль разметки: `parse_mode`, `disable_link_preview`, `max_length`, `ellipsis`, `attachments_style`.

### discord
- `token` *(обязательно)* — пользовательский, бот или bearer-токен.
- `token_type` — `auto` (по умолчанию) выбирает схему авторизации, можно задать `user`, `bot` или `bearer`.
- `rate_limit.*` — ограничения запросов к API Discord: скорость, параллельные отправки, случайные задержки и `cooldown_seconds`.

### forward
- `defaults.filters` — списки `whitelist`/`blacklist`, `allowed_senders`/`blocked_senders`, `allowed_types`/`blocked_types` (`text`, `attachment`, `image`, `video`, `audio`, `file`, `document`, `other`).
- `defaults.text` — декоративные элементы (чипы, заголовки, подвал) и правила find/replace.
- `defaults.formatting` — глобальное переопределение профиля Telegram.
- `channels[]` — пары Discord → Telegram. Можно задавать `name` и собственные `filters`, `text`, `formatting`; параметры дополняют `defaults`.

### network
- `user_agents.desktop`/`mobile` — переопределение встроенных списков user-agent'ов.
- `mobile_ratio` — доля запросов, имитирующих мобильных клиентов.
- `proxies.pool` — общий пул прокси, опциональные `username`, `password`, `rotate_url`.
- `proxies.discord`/`telegram` — отдельные настройки для каждого сервиса.
- `proxies.healthcheck` — URL проверки, таймаут и интервал восстановления пула.

### runtime
- `poll_every` — интервал опроса Discord.
- `state_file` — файл прогресса для восстановления после перезапуска.
- `max_messages`, `max_fetch_seconds` — лимиты количества сообщений и ожидания API.
- `delays.min`/`delays.max` — случайные паузы перед отправкой в Telegram.

## Минимальный пример
```yaml
telegram:
  token: "TELEGRAM_BOT_TOKEN"
  chat: "@fallback_channel"

discord:
  token: "DISCORD_USER_TOKEN"

forward:
  channels:
    - discord: 123456789012345678
      telegram: "-1001234567890"
```

## Рабочий цикл
1. Discord опрашивается с шагом `runtime.poll_every`.
2. Сообщения проходят фильтры, замены и очистку Markdown и HTML.
3. В Telegram уходит основное сообщение и компактные сводки вложений.
4. Прогресс записывается в `state_file`, чтобы продолжить после рестарта.

## Разработка и проверки
```bash
pip install -U ruff mypy pytest pytest-asyncio aresponses pre-commit
pre-commit install
pre-commit install --hook-type pre-push
make ci
```

- `ruff` — форматирование и статический анализ.
- `mypy` — проверка аннотаций типов.
- `pytest` — регрессионные тесты.

Все коммиты и merge-запросы должны проходить `make ci`.
