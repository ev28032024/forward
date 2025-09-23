# Forward Monitor

Лёгкий асинхронный мост между каналами Discord и чатами Telegram. Бот отслеживает новые сообщения,
очищает их, при необходимости нарезает на несколько частей и отправляет в указанные чаты Telegram.

## Возможности
- Аккуратное форматирование: чипы, заголовки, подвал и find/replace-замены без дублирующих строк.
- Распаковка вложений и embed-блоков в компактные подписи с сохранением HTML-разметки.
- Гибкие фильтры по ключевым словам, отправителям и типам сообщений.
- Настраиваемые Telegram-профили (parse mode, отключение превью, лимиты длины, стиль списка вложений).
- Индивидуальные rate limit'ы для Discord и Telegram, поддержка пулов прокси и пользовательских user-agent'ов.
- Персистентное состояние (`state_file`), регулируемые интервалы опроса и случайные задержки между отправками.

## Установка
1. Создайте и активируйте виртуальное окружение.
2. Установите зависимости и проект в режиме разработки.
3. Скопируйте пример конфигурации и заполните обязательные поля.
4. Запустите бота, передав путь к конфигурации.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.yml config.yml
python -m forward_monitor --config config.yml
```

## Быстрый старт
Минимально необходимый `config.yml` должен содержать токены и хотя бы одну пару каналов:

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

`token_type` у Discord подбирается автоматически (`auto`), но при необходимости можно указать `user`,
`bot` или `bearer`.

## Структура config.yml
Все секции независимы и имеют разумные значения по умолчанию. Пустые и дублирующиеся элементы
автоматически исключаются — достаточно перечислить только действительно нужные параметры.

### Telegram
```yaml
telegram:
  token: "…"           # обязательный токен бота
  chat: "…"            # fallback-чат на случай отсутствия telegram ID у канала
  formatting:
    parse_mode: HTML    # HTML или MarkdownV2
    disable_preview: true
    attachments: minimal
```

### Discord
```yaml
discord:
  token: "…"           # пользовательский или бот-аккаунт
  token_type: auto      # auto, user, bot, bearer
  rate_limit:
    per_second: 3.5
    per_minute: 60
    concurrency: 4
    jitter_min_ms: 40
    jitter_max_ms: 160
    cooldown_seconds: 30
```

### Forward
`forward.defaults` задаёт базовые фильтры, оформление и форматирование для всех каналов. Любое
значение можно переопределить внутри конкретного элемента `forward.channels`.

```yaml
forward:
  defaults:
    filters:
      whitelist: ["release", "update"]
      blocked_senders: ["spam-bot"]
      allowed_types: [text, image]
    text:
      chips: ["🔥"]
      headers: ["📢 Новости"]
      footers: ["Forward Bot"]
      replacements:
        "GM": "Good morning"
        "[скрыть]": ""
    formatting:
      attachments: compact
  channels:
    - discord: 123
      telegram: "-100555"
      name: "#announcements"
      filters:
        blacklist: ["maintenance"]
      text:
        headers: ["📦 Обновления"]
        replacements:
          - find: "LFG"
            replace: "Let's go"
      formatting:
        disable_preview: false
```

Дополнительно можно использовать массивы `allowed_senders`, `blocked_types`, `chips`, `headers` и
`footers`. При подготовке сообщения пустые элементы и дубликаты удаляются автоматически.

### Network
```yaml
network:
  user_agents:
    mobile_ratio: 0.35
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

### Runtime
```yaml
runtime:
  poll_every: 300            # секунды между запросами к Discord
  state_file: "monitor_state.json"
  delays:
    min: 0.6
    max: 2.5
```

## Принцип работы
1. Discord опрашивается с периодом `runtime.poll_every` (по умолчанию 5 минут).
2. Сообщения проходят фильтры, замены и очистку Markdown.
3. Telegram получает основное сообщение и вложения отдельными компактными блоками.
4. Состояние доставки сохраняется в `state_file`, чтобы бот мог продолжить после перезапуска.

## Советы по эксплуатации
- Используйте отдельный Discord-аккаунт с правами только на чтение нужных каналов.
- Запускайте бота под присмотром `systemd`, `supervisord` или Docker для авто‑рестарта.
- Держите `config.yml` и `state_file` вне репозитория, если внутри есть приватные данные.
- Закреплённые сообщения в Discord игнорируются — учитываются только новые публикации.

## Разработка
Установите инструменты разработки и активируйте git-hook'и:

```bash
pip install -U ruff mypy pytest pytest-asyncio aresponses pre-commit
pre-commit install
pre-commit install --hook-type pre-push
```

## CI и тестирование

Для локальной проверки перед коммитом запустите полную цепочку:

```bash
make ci
```

Команда последовательно выполняет `ruff`, `mypy` и `pytest`. Если какой-либо этап завершился ошибкой:

- `ruff` сообщает о синтаксических, импортных и стилистических нарушениях.
- `mypy` проверяет статические типы и ожидаемые контракты.
- `pytest` выявляет регрессы и ошибки исполнения.

Исправьте причину сбоя, повторно запустите `make ci` и убедитесь, что все проверки зелёные. Политика проекта — **никаких merge, пока CI не проходит успешно**. Хук `pre-push` автоматически выполняет `pytest -q`, чтобы не отправить нестабильные изменения.

Для полного прогона статических анализаторов и тестов используйте ту же команду `make ci` — она
запускает `ruff`, `mypy` и `pytest`. Перед публикацией убедитесь, что все проверки проходят успешно.
