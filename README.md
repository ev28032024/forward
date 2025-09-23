# forward-monitor


Лёгкий мост между каналами Discord и чатами Telegram. Бот читает выбранные каналы, фильтрует сообщения и отправляет их в Telegram, сохраняя базовое форматирование и ссылки на вложения.
=======
Лёгкий асинхронный мост из Discord в Telegram. Приложение следит за указанными каналами Discord и пересылает новые сообщения в чаты/каналы Telegram, аккуратно очищает Markdown, подтягивает текст из embed'ов, добавляет ссылку «Открыть в Discord», а вложения оформляет компактным списком ссылок. 【F:src/forward_monitor/formatter.py†L69-L136】

## ✨ Возможности

- Очистка, нарезка и повторная отправка длинных сообщений с сохранением HTML‑разметки, а также разворачивание ссылок на вложения и embed'ы в отдельные сообщения. 【F:src/forward_monitor/formatter.py†L69-L136】
- Настраиваемые «фишки» (chips), заголовки и подвал, а также find/replace-замены для каждого канала или глобально. 【F:src/forward_monitor/config.py†L302-L343】
- Фильтрация по белым/чёрным спискам ключевых слов, отправителей и типов сообщений, чтобы пересылать только релевантный контент. 【F:src/forward_monitor/config.py†L232-L286】
- Индивидуальные профили форматирования Telegram: parse mode, отключение превью, лимиты длины, стиль списка вложений. 【F:src/forward_monitor/config.py†L161-L229】
- Гибкая настройка rate limit'ов для Discord и Telegram, включая джиттер и максимальную параллельность. 【F:src/forward_monitor/config.py†L75-L108】【F:src/forward_monitor/config.py†L650-L673】
- Пулы прокси с проверкой доступности, отдельные настройки для Discord и Telegram и рандомизация user-agent'ов с заданной долей мобильных клиентов. 【F:src/forward_monitor/config.py†L110-L158】【F:src/forward_monitor/config.py†L582-L613】
- Персистентное состояние (`state_file`), настраиваемые интервалы опроса и случайные задержки между сообщениями. 【F:src/forward_monitor/config.py†L406-L411】【F:src/forward_monitor/config.py†L553-L579】

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
=======
## ⚙️ Минимальная конфигурация

`config.yml` разбит на несколько секций. Для старта достаточно токенов и списка каналов:

```yaml
telegram:
  token: "TELEGRAM_BOT_TOKEN"
  chat: "@fallback_channel"

discord:
  token: "DISCORD_USER_TOKEN"
  token_type: auto  # auto, user, bot или bearer

forward:
  channels:
    - discord: 123456789012345678
      telegram: "-1001234567890"
      name: "#announcements"
    - discord: 234567890123456789  # fallback chat будет использован автоматически
```

Поле `token_type` подбирает нужный формат авторизации: `user` — пользовательский токен, `bot` — токен бота без префикса, `bearer` — OAuth, `auto` (значение по умолчанию) пытается распознать вариант автоматически. 【F:src/forward_monitor/config.py†L493-L505】【F:src/forward_monitor/config.py†L847-L855】

## 🧱 Глобальные настройки и переопределения

Секция `forward.defaults` задаёт поведение по умолчанию. Любой параметр можно уточнить внутри конкретного канала (`forward.channels[n]`).

```yaml
forward:
  defaults:
    filters:
      whitelist: ["release", "update"]
      blocked_senders: ["spam-bot"]
      allowed_types: [text, image]
    text:
      chips: ["🔥"]
      headers: ["📢 Новости проекта"]
      footers: ["— Forward Bot"]
      replacements:
        "GM": "Good morning"
        "[скрыть]": ""  # пустая строка удаляет найденный фрагмент
    formatting:
      attachments: compact
```

> `allowed_types` и `blocked_types` поддерживают значения `text`, `attachment`, `image`, `video`, `audio`, `file`, `document`, `other`. 【F:src/forward_monitor/config.py†L676-L695】

Любой канал может переопределить нужный раздел:

```yaml
forward:
  channels:
    - discord: 123
      telegram: "-100555"
      text:
        headers: ["📦 Обновления"]
        replacements:
          - find: "LFG"
            replace: "Let's go"
      filters:
        blacklist: ["maintenance"]
      formatting:
        disable_preview: false
```

## 🌐 Сеть и rate limits

Бот умеет работать через прокси и самостоятельно следит за здоровьем каждого endpoint'а. Настройки user-agent'ов и сетевых пулов вынесены в секцию `network`:

```yaml
network:
  user_agents:
    mobile_ratio: 0.35  # доля мобильных UA при опросе Discord
  proxies:
    pool:
      - "http://proxy.example:8080"
    telegram:
      pool:
        - "socks5://tg-proxy.example:9050"
    healthcheck:
      url: "https://www.google.com/generate_204"
      timeout: 5
      cooldown: 180
```

Отдельно настраиваются лимиты запросов. Для Discord и Telegram можно указать частоту, параллельность, случайный джиттер и паузы на восстановление:

```yaml
discord:
  rate_limit:
    per_second: 3.5
    per_minute: 60
    concurrency: 4
    jitter_min_ms: 40
    jitter_max_ms: 160
    cooldown_seconds: 30

telegram:
  rate_limit:
    per_second: 0.9
    per_minute: 25
    concurrency: 1
```

## 🕒 Runtime-параметры

Секция `runtime` управляет частотой опроса и задержками между отправками. Здесь же можно задать путь к файлу состояния, чтобы бот восстанавливался после перезапуска:

```yaml
runtime:
  poll_every: 300  # секунды между запросами Discord
  state_file: "monitor_state.json"
  delays:
    min: 0.6
    max: 2.5
```

## 🔁 Как это работает

1. Discord-API опрашивается с учётом `runtime.poll_every` (по умолчанию 5 минут).
2. Для каждого нового сообщения применяются фильтры и текстовые замены.
3. Сообщение отправляется в Telegram, затем последовательно досылаются вложения с небольшими случайными задержками, чтобы не упираться в лимиты Telegram.
4. Состояние обработки хранится в JSON-файле (`state_file`), чтобы бот продолжил с нужного сообщения после перезапуска.

## 🧰 Полезные флаги

- `runtime.state_file` — путь к файлу состояния (по умолчанию `monitor_state.json` рядом с конфигом).
- `runtime.delays.min` / `runtime.delays.max` — границы случайной паузы между сообщениями и медиа (в секундах).
- `runtime.poll_every` — интервал опроса Discord.
- `--log-level DEBUG` — выводит причины, по которым сообщение было отфильтровано.

## ☑️ Рекомендации

- Используйте отдельного технического пользователя Discord с правами только на чтение целевых каналов.
- Запускайте монитор внутри `systemd`, `supervisord` или Docker для авто‑рестарта.
- Храните `config.yml` и `state_file` вне репозитория, если в нём есть секреты.

Проект избавлен от поддержки закреплённых сообщений — монитор обрабатывает только новые публикации.

## 🧪 CI и разработка

- Установите инструменты разработки один раз перед работой:

  ```bash
  pip install -U ruff mypy pytest pytest-asyncio aresponses pre-commit
  pre-commit install
  ```

- Полный прогон статических анализаторов и тестов запускается командой:

  ```bash
  make ci
  ```

  Команда последовательно выполняет `ruff`, `mypy` и `pytest`. Ошибки линтера (`ruff`) обычно означают проблемы с импортами, форматированием или линейной длиной. Предупреждения `mypy` указывают на несоответствие аннотаций типов — исправляйте код, а не добавляйте `# type: ignore`. Трассировки `pytest` помогают найти реальные дефекты в логике.

- Политика репозитория: **никаких merge/push, пока `make ci` не завершится успешно**. Прежде чем отправлять изменения, убедитесь, что локальный прогон зелёный.