# Forward Monitor

Лёгкий асинхронный мост между каналами Discord и чатами Telegram. Бот следит за указанными каналами, фильтрует сообщения и
отправляет их в Telegram, аккуратно очищая Markdown, разворачивая вложения и добавляя ссылку «Открыть в Discord».

## Содержание
- [Возможности](#возможности)
- [Установка](#установка)
- [Быстрый старт](#быстрый-старт)
- [Глобальные настройки](#глобальные-настройки)
- [Сеть и rate limits](#сеть-и-rate-limits)
- [Runtime-параметры](#runtime-параметры)
- [Принцип работы](#принцип-работы)
- [Советы по эксплуатации](#советы-по-эксплуатации)
- [Разработка и CI](#разработка-и-ci)

## Возможности
- Очистка, нарезка и повторная отправка длинных сообщений с сохранением HTML-разметки.
- Разворачивание ссылок на вложения и embed'ы в отдельные сообщения.
- Настраиваемые «фишки» (chips), заголовки, подвал и find/replace-замены для каждого канала.
- Фильтрация по белым/чёрным спискам ключевых слов, отправителей и типов сообщений.
- Индивидуальные профили форматирования Telegram: parse mode, отключение превью, лимиты длины, стиль списка вложений.
- Гибкие rate limit'ы для Discord и Telegram, включая джиттер и максимальную параллельность.
- Поддержка пулов прокси, раздельных настроек для Discord и Telegram и рандомизации user-agent'ов.
- Персистентное состояние (`state_file`), настраиваемые интервалы опроса и случайные задержки между сообщениями.

## Установка
1. Создайте виртуальное окружение и активируйте его.
2. Установите проект в режиме разработки.
3. Скопируйте пример конфигурации и запустите бота.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.yml config.yml
python -m forward_monitor --config config.yml
```

## Быстрый старт
`config.yml` разбит на несколько секций. Для начала работы достаточно указать токены и пары каналов:

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
    - discord: 234567890123456789  # fallback чат будет использован автоматически
```

Параметр `token_type` подбирает нужный формат авторизации: `user` — пользовательский токен, `bot` — токен бота без префикса,
`bearer` — OAuth, `auto` (по умолчанию) определяет вариант автоматически.

## Глобальные настройки
Секция `forward.defaults` задаёт поведение по умолчанию. Любой параметр можно переопределить внутри конкретного канала
(`forward.channels[n]`).

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

`allowed_types` и `blocked_types` поддерживают значения `text`, `attachment`, `image`, `video`, `audio`, `file`, `document`, `other`.

Любой канал может уточнить нужный раздел:

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

## Сеть и rate limits
Бот умеет работать через прокси и самостоятельно следит за здоровьем каждого endpoint'а. Настройки user-agent'ов и сетевых пулов
вынесены в секцию `network`:

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

Для Discord и Telegram можно настроить частоту запросов, параллельность, случайный джиттер и паузы на восстановление:

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

## Runtime-параметры
Секция `runtime` управляет частотой опроса и задержками между отправками. Здесь же задаётся путь к файлу состояния, чтобы бот
восстанавливался после перезапуска:

```yaml
runtime:
  poll_every: 300  # секунды между запросами Discord
  state_file: "monitor_state.json"
  delays:
    min: 0.6
    max: 2.5
```

## Принцип работы
1. Discord-API опрашивается с учётом `runtime.poll_every` (по умолчанию 5 минут).
2. Для каждого нового сообщения применяются фильтры и текстовые замены.
3. Сообщение отправляется в Telegram, затем последовательно досылаются вложения с небольшими случайными задержками.
4. Состояние обработки хранится в JSON-файле (`state_file`), чтобы бот продолжил с нужного сообщения после перезапуска.

## Советы по эксплуатации
- Используйте отдельного технического пользователя Discord с правами только на чтение целевых каналов.
- Запускайте монитор внутри `systemd`, `supervisord` или Docker для авто‑рестарта.
- Храните `config.yml` и `state_file` вне репозитория, если в нём есть секреты.
- Бот не обрабатывает закреплённые сообщения — только новые публикации.

## Разработка и CI
- Установите инструменты разработки перед первым запуском:

  ```bash
  pip install -U ruff mypy pytest pytest-asyncio aresponses pre-commit
  pre-commit install
  ```

- Полный прогон статических анализаторов и тестов:

  ```bash
  make ci
  ```

Команда последовательно выполняет `ruff`, `mypy` и `pytest`. Ошибки `ruff` обычно указывают на форматирование или импорты,
предупреждения `mypy` — на несоответствие аннотаций типов, а отчёты `pytest` помогают найти дефекты логики. Перед публикацией
изменений убедитесь, что `make ci` завершается без ошибок.
