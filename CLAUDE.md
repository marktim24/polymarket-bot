# Инструкция для Claude — Polymarket Copy-Trading Bot

Этот файл описывает проект и твою роль в работе с ним на этом сервере.
Читай его при каждом новом сеансе работы с проектом.

---

## Что это за проект

Автоматический копи-трейдинг бот для платформы Polymarket (polymarket.com) —
предсказательного рынка на базе блокчейна Polygon.

Бот отслеживает сделки двух опытных трейдеров и автоматически копирует их
с заданным коэффициентом и ограничениями риска. Владелец — Mark
(marktimoshevich@gmail.com).

---

## Архитектура проекта

```
polymarket-copy-bot/
├── bot.py           # Точка входа. Запускать: python bot.py
├── config.py        # ВСЕ настройки. Параметры берутся из .env файла
├── monitor.py       # Опрашивает Polymarket API каждые 30 сек
├── executor.py      # Размещает ордера через py-clob-client
├── risk_manager.py  # Фильтрует сделки, следит за стоп-лоссом
├── dashboard.py     # Flask дашборд на порту 5000
├── requirements.txt # Зависимости Python
├── .env             # СЕКРЕТЫ (не в git). Создать из .env.example
├── .env.example     # Шаблон для .env
└── logs/bot.log     # Лог файл (создаётся автоматически)
```

### Как компоненты взаимодействуют

```
MonitorManager
  └─ TraderMonitor × 2  →  Queue[TradeActivity]
                                    ↓
                            PolymarketCopyBot._process_queue()
                                    ↓
                            RiskManager.validate_trade()
                                    ↓ (если прошла фильтрацию)
                            OrderExecutor.execute_trade()
                                    ↓
                            RiskManager.register_position()
                                    ↓
                            TelegramNotifier.notify_trade_copied()

RiskManager._stop_loss_loop()  ←── фоновый поток, каждые 60 сек
  └─ если цена упала на 20% → OrderExecutor.close_position()
```

---

## Отслеживаемые трейдеры

| Имя | Адрес кошелька | Стратегия | Win Rate |
|---|---|---|---|
| lebronjames23 | 0xa1b3fa26d16c11b222f6785851981c2f560b0329 | Спорт + Киберспорт | 80% |
| sayber | 0x96b41aac95788f717d0566210cda48e8e686c2f1 | Политика + Спорт + Крипто | 88% |

API для мониторинга: `https://data-api.polymarket.com/activity?user={адрес}&limit=5`

---

## Параметры риск-менеджмента (из config.py)

| Параметр | Значение | Смысл |
|---|---|---|
| MAX_POSITION_USD | 10.0 | Максимум $10 на одну сделку |
| MIN_ENTRY_PRICE | 0.05 | Не входить дешевле 5 центов |
| MAX_ENTRY_PRICE | 0.70 | Не входить дороже 70 центов |
| COPY_RATIO | 0.5 | Копировать 50% от объёма оригинала |
| MIN_COPY_SIZE_USD | 1.0 | Минимум $1 для исполнения |
| MAX_OPEN_POSITIONS | 20 | Лимит одновременных позиций |
| STOP_LOSS_PERCENT | 0.80 | Стоп-лосс при падении цены на 20% |
| POLL_INTERVAL_SEC | 30 | Опрос каждые 30 секунд |

Все параметры можно менять через `.env` файл без правки кода.

---

## Режимы работы

**DRY_RUN=true** (по умолчанию) — бот логирует сделки, но НЕ размещает реальные ордера.
Используй для тестирования и мониторинга.

**DRY_RUN=false** — реальная торговля. Требует заполненных CLOB API ключей и
приватного ключа кошелька в `.env`.

---

## Как запустить

```powershell
# Установить зависимости (один раз)
pip install -r requirements.txt

# Создать .env из шаблона
copy .env.example .env
notepad .env

# Тест без реальных сделок
python bot.py

# Дашборд доступен по адресу http://localhost:5000
```

---

## Как запустить в фоне на Windows (чтобы работал без открытого окна)

```powershell
# Вариант 1 — pythonw (без консоли)
pythonw bot.py

# Вариант 2 — через PowerShell в фоне
Start-Process python -ArgumentList "bot.py" -WindowStyle Hidden -RedirectStandardOutput "logs\stdout.log"

# Проверить что работает
Get-Process python

# Остановить
Get-Process python | Stop-Process
```

---

## Частые задачи — как тебе помочь

### Изменить параметры риска
Отредактируй `.env` файл — найди нужную переменную и измени значение.
Перезапусти бота после изменений.

### Добавить нового трейдера для копирования
В `config.py` найди список `TRADERS` и добавь словарь по образцу существующих.
Нужны: `name`, `address` (Polygon кошелёк), `strategy`, `win_rate`, `sharpe`, `entry_range`.

### Посмотреть логи
```powershell
# Последние 50 строк
Get-Content logs\bot.log -Tail 50

# В реальном времени
Get-Content logs\bot.log -Wait -Tail 20
```

### Проверить открытые позиции
Открой дашборд: http://localhost:5000
Или запроси JSON: http://localhost:5000/api/positions

### Бот не запускается — модуль не найден
```powershell
pip install -r requirements.txt
```

### Обновить код с GitHub
```powershell
git pull origin main
```

---

## Структура .env файла (что должно быть заполнено)

```
WALLET_PRIVATE_KEY=...   # приватный ключ Polygon кошелька (без 0x)
WALLET_ADDRESS=0x...     # публичный адрес кошелька
CLOB_API_KEY=...         # ключи от polymarket.com → Profile → API Keys
CLOB_API_SECRET=...
CLOB_API_PASSPHRASE=...
TELEGRAM_BOT_TOKEN=...   # токен от @BotFather
TELEGRAM_CHAT_ID=...     # chat id от @userinfobot
DRY_RUN=true             # сначала всегда true!
```

---

## Что НЕ делать

- Не коммить `.env` файл в git — там приватный ключ
- Не ставить `DRY_RUN=false` пока не протестировано в dry-run
- Не менять адреса трейдеров в `config.py` без понимания последствий
- Не останавливать бота резко (kill -9) — используй Ctrl+C или Stop-Process для корректного завершения

---

## Владелец проекта

Mark — marktimoshevich@gmail.com

Если что-то непонятно в коде или нужно внести изменения — читай
соответствующий файл целиком перед правкой, все комментарии на русском языке.
