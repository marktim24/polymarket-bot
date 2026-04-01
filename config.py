"""
config.py — Центральная конфигурация Polymarket Copy-Trading Bot.
Версия 2.0 — сигнальная система с расширенным риск-менеджментом.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# НАСТРОЙКИ КОШЕЛЬКА И API POLYMARKET
# ============================================================

WALLET_PRIVATE_KEY: str = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADDRESS: str = os.getenv("WALLET_ADDRESS", "")

CLOB_API_KEY: str = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET: str = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE: str = os.getenv("CLOB_API_PASSPHRASE", "")

# ============================================================
# НАСТРОЙКИ TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# ТРЕЙДЕРЫ ДЛЯ МОНИТОРИНГА
# COPY  — основной источник сигналов, позиции копируются
# WATCH — только подтверждение (confluence), не копируется отдельно
# ============================================================

TRADERS: list[dict] = [
    {
        "name": "lebronjames23",
        "address": "0xa1b3fa26d16c11b222f6785851981c2f560b0329",
        "role": "COPY",
        "strategy": "спорт + киберспорт",
        "win_rate": 0.80,
        "sharpe": 0.68,
        "entry_range": (0.08, 0.48),
    },
    {
        "name": "sayber",
        "address": "0x96b41aac95788f717d0566210cda48e8e686c2f1",
        "role": "COPY",
        "strategy": "политика + спорт + крипто",
        "win_rate": 0.88,
        "sharpe": 4.13,
        "entry_range": (0.50, 0.70),
    },
    {
        "name": "zeto82",
        "address": "0xe2cafb64726ca135a74fcebf9af0ce07d4daa64",
        "role": "COPY",
        "strategy": "whitelist trader",
        "win_rate": 0.0,
        "sharpe": 0.0,
        "entry_range": (0.20, 0.55),
    },
    {
        "name": "DenariusX",
        "address": "0x9ce7de90312489a01af6d4e8661b196c873f7cfa",
        "role": "COPY",
        "strategy": "whitelist trader",
        "win_rate": 0.0,
        "sharpe": 0.0,
        "entry_range": (0.20, 0.55),
    },
    {
        "name": "akirauva",
        "address": "0xbea27864852ba214557d12203ecb459ca5b79214",
        "role": "COPY",
        "strategy": "whitelist trader",
        "win_rate": 0.0,
        "sharpe": 0.0,
        "entry_range": (0.20, 0.55),
    },
]

# Быстрый доступ: имя трейдера → роль
TRADER_ROLES: dict[str, str] = {t["name"]: t["role"] for t in TRADERS}

# ============================================================
# SIGNAL-ONLY MODE
# ============================================================

# MODE = "SIGNAL_ONLY" → только вывод сигналов, нулевое исполнение
# MODE = "LIVE"        → полный режим с реальными ордерами
MODE: str = os.getenv("MODE", "SIGNAL_ONLY")

# Только сделки этих трейдеров проходят в SIGNAL_ONLY режиме
WHITELIST_TRADERS: list[str] = [
    name.strip()
    for name in os.getenv("WHITELIST_TRADERS", "zeto82,DenariusX,akirauva").split(",")
    if name.strip()
]

# Максимум активных сигналов одновременно
MAX_SIGNALS: int = int(os.getenv("MAX_SIGNALS", "3"))

# Минимальное время до резолюции рынка для прохождения фильтра (часы)
MIN_TIME_TO_RESOLUTION_HOURS: float = float(
    os.getenv("MIN_TIME_TO_RESOLUTION_HOURS", "72.0")
)

# ============================================================
# КЛАССИФИКАЦИЯ СИГНАЛОВ
# ============================================================

# HIGH сигнал: оба трейдера COPY + подтверждение
SIGNAL_HIGH_MIN_PRICE: float = 0.20
SIGNAL_HIGH_MAX_PRICE: float = 0.55

# MEDIUM сигнал: один COPY трейдер
SIGNAL_MEDIUM_MIN_PRICE: float = 0.20
SIGNAL_MEDIUM_MAX_PRICE: float = 0.45

# Окно времени для confluence (часы) — сколько держать буфер предыдущих сделок
SIGNAL_CONFLUENCE_WINDOW_HOURS: float = 1.0

# Минимальная глубина стакана для прохождения фильтра ликвидности (USD)
MIN_LIQUIDITY_USD: float = float(os.getenv("MIN_LIQUIDITY_USD", "500.0"))

# Минимальное время до резолюции рынка (часы)
MIN_MARKET_RESOLUTION_HOURS: float = float(os.getenv("MIN_MARKET_RESOLUTION_HOURS", "72.0"))

# ============================================================
# ФИЛЬТРЫ СДЕЛОК
# ============================================================

# Диапазон допустимых цен входа (обновлён с 0.05–0.70 на 0.20–0.55)
MIN_ENTRY_PRICE: float = float(os.getenv("MIN_ENTRY_PRICE", "0.20"))
MAX_ENTRY_PRICE: float = float(os.getenv("MAX_ENTRY_PRICE", "0.55"))

# Максимальный возраст сигнала — сделки старше этого игнорируются
MAX_SIGNAL_AGE_HOURS: float = float(os.getenv("MAX_SIGNAL_AGE_HOURS", "12.0"))

# Максимальное движение цены с момента сделки трейдера
# Если цена уже сдвинулась на >10% — слишком поздно входить
MAX_PRICE_MOVEMENT_PCT: float = float(os.getenv("MAX_PRICE_MOVEMENT_PCT", "0.10"))

# Лимит записей активности при опросе API (был 5, теперь 20)
ACTIVITY_FETCH_LIMIT: int = int(os.getenv("ACTIVITY_FETCH_LIMIT", "20"))

# Интервал опроса API в секундах
POLL_INTERVAL_SEC: int = int(os.getenv("POLL_INTERVAL_SEC", "30"))

# ============================================================
# РАЗМЕР ПОЗИЦИЙ ПО ТИПУ СИГНАЛА
# ============================================================

BASE_POSITION_USD: float = float(os.getenv("BASE_POSITION_USD", "2.0"))
MEDIUM_POSITION_USD: float = float(os.getenv("MEDIUM_POSITION_USD", "3.0"))
HIGH_POSITION_USD: float = float(os.getenv("HIGH_POSITION_USD", "5.0"))

# Устаревшие параметры — сохранены для обратной совместимости
MAX_POSITION_USD: float = HIGH_POSITION_USD
COPY_RATIO: float = float(os.getenv("COPY_RATIO", "0.5"))
MIN_COPY_SIZE_USD: float = float(os.getenv("MIN_COPY_SIZE_USD", "1.0"))

# ============================================================
# ЛИМИТЫ ПОРТФЕЛЯ
# ============================================================

# Максимум одновременных открытых позиций (снижено с 20 до 3)
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

# Максимальная суммарная экспозиция в USDC
MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "20.0"))

# ============================================================
# ПАРАМЕТРЫ РИСКА
# ============================================================

# Максимальный убыток на одну сделку (USD)
MAX_LOSS_PER_TRADE_USD: float = float(os.getenv("MAX_LOSS_PER_TRADE_USD", "2.0"))

# Дневной лимит потерь (USD) — после превышения торговля останавливается
DAILY_LOSS_LIMIT_USD: float = float(os.getenv("DAILY_LOSS_LIMIT_USD", "6.0"))

# Максимум подряд убыточных сделок — после этого пауза до следующего дня
MAX_CONSECUTIVE_LOSSES: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))

# Порог просадки для уменьшения размера позиций на 50%
DRAWDOWN_REDUCE_THRESHOLD: float = float(os.getenv("DRAWDOWN_REDUCE_THRESHOLD", "0.20"))

# Стоп-лосс: закрыть если цена упала ниже entry * STOP_LOSS_PERCENT
STOP_LOSS_PERCENT: float = float(os.getenv("STOP_LOSS_PERCENT", "0.80"))

# Интервал проверки стоп-лосса и выходов (секунды)
STOP_LOSS_CHECK_INTERVAL_SEC: int = 60

# ============================================================
# ЛОГИКА ВЫХОДА
# ============================================================

# Тейк-профит уровень 1: при росте +20% закрыть 50% позиции
TAKE_PROFIT_1_PCT: float = float(os.getenv("TAKE_PROFIT_1_PCT", "0.20"))
TAKE_PROFIT_1_CLOSE_RATIO: float = 0.50

# Тейк-профит уровень 2: при росте +40% закрыть ещё 25%
TAKE_PROFIT_2_PCT: float = float(os.getenv("TAKE_PROFIT_2_PCT", "0.40"))
TAKE_PROFIT_2_CLOSE_RATIO: float = 0.25

# Временной стоп: выход если цена не двигалась 24 часа
TIME_STOP_NO_MOVEMENT_HOURS: float = float(os.getenv("TIME_STOP_NO_MOVEMENT_HOURS", "24.0"))

# Максимальное время удержания позиции (часы)
MAX_HOLD_HOURS: float = float(os.getenv("MAX_HOLD_HOURS", "72.0"))

# ============================================================
# ОБЩИЕ НАСТРОЙКИ БОТА
# ============================================================

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = "logs/bot.log"
LOG_MAX_BYTES: int = 5 * 1024 * 1024
LOG_BACKUP_COUNT: int = 3
STATUS_INTERVAL_HOURS: int = 6

# ============================================================
# СЕТЕВЫЕ НАСТРОЙКИ
# ============================================================

POLYGON_RPC: str = os.getenv("POLYGON_RPC", "https://rpc.ankr.com/polygon")
CLOB_HOST: str = "https://clob.polymarket.com"
DATA_API_HOST: str = "https://data-api.polymarket.com"
GAMMA_API_HOST: str = "https://gamma-api.polymarket.com"
HTTP_TIMEOUT: int = 15
CHAIN_ID: int = 137
USDC_CONTRACT: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ============================================================
# НАСТРОЙКИ ДАШБОРДА
# ============================================================

DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")


# ============================================================
# ВАЛИДАЦИЯ
# ============================================================

def validate_config() -> list[str]:
    warnings = []
    if not WALLET_PRIVATE_KEY:
        warnings.append("WALLET_PRIVATE_KEY не задан — реальная торговля невозможна")
    if not WALLET_ADDRESS:
        warnings.append("WALLET_ADDRESS не задан")
    if not DRY_RUN and (not CLOB_API_KEY or not CLOB_API_SECRET):
        warnings.append("CLOB_API_KEY/SECRET не заданы — реальные ордера не будут работать")
    if not TELEGRAM_BOT_TOKEN:
        warnings.append("TELEGRAM_BOT_TOKEN не задан — уведомления отключены")
    if not TELEGRAM_CHAT_ID:
        warnings.append("TELEGRAM_CHAT_ID не задан — уведомления отключены")
    return warnings
