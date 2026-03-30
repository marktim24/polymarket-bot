"""
config.py — Центральная конфигурация Polymarket Copy-Trading Bot.
Все параметры считываются из переменных окружения (.env файла),
что позволяет менять настройки без правки кода.
"""

import os
from dotenv import load_dotenv

# Загружаем .env файл, если он существует
load_dotenv()

# ============================================================
# НАСТРОЙКИ КОШЕЛЬКА И API POLYMARKET
# ============================================================

# Приватный ключ Polygon-кошелька (hex, без 0x)
WALLET_PRIVATE_KEY: str = os.getenv("WALLET_PRIVATE_KEY", "")

# Публичный адрес кошелька (0x...)
WALLET_ADDRESS: str = os.getenv("WALLET_ADDRESS", "")

# Учётные данные CLOB API (получить: polymarket.com → Profile → API Keys)
CLOB_API_KEY: str = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET: str = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE: str = os.getenv("CLOB_API_PASSPHRASE", "")

# ============================================================
# НАСТРОЙКИ TELEGRAM
# ============================================================

# Токен бота (@BotFather → /newbot)
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ID вашего чата или группы (получить через @userinfobot)
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# ТРЕЙДЕРЫ ДЛЯ КОПИРОВАНИЯ
# ============================================================

TRADERS: list[dict] = [
    {
        "name": "lebronjames23",
        "address": "0xa1b3fa26d16c11b222f6785851981c2f560b0329",
        "strategy": "спорт + киберспорт",
        "win_rate": 0.80,
        "sharpe": 0.68,
        # Типичный диапазон цен входа этого трейдера (для справки)
        "entry_range": (0.08, 0.48),
    },
    {
        "name": "sayber",
        "address": "0x96b41aac95788f717d0566210cda48e8e686c2f1",
        "strategy": "политика + спорт + крипто",
        "win_rate": 0.88,
        "sharpe": 4.13,
        "entry_range": (0.50, 0.70),
    },
]

# ============================================================
# ПАРАМЕТРЫ РИСК-МЕНЕДЖМЕНТА
# ============================================================

# Максимальный размер одной позиции в USDC
MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "10.0"))

# Нижняя граница цены входа (не торговать дешевле 5¢)
MIN_ENTRY_PRICE: float = float(os.getenv("MIN_ENTRY_PRICE", "0.05"))

# Верхняя граница цены входа (не торговать дороже 70¢)
MAX_ENTRY_PRICE: float = float(os.getenv("MAX_ENTRY_PRICE", "0.70"))

# Коэффициент копирования (0.5 = 50% от объёма оригинала)
COPY_RATIO: float = float(os.getenv("COPY_RATIO", "0.5"))

# Минимальный размер для исполнения в USDC (меньше — пропускать)
MIN_COPY_SIZE_USD: float = float(os.getenv("MIN_COPY_SIZE_USD", "1.0"))

# Максимальное количество одновременно открытых позиций
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "20"))

# Интервал опроса активности трейдеров (секунды)
POLL_INTERVAL_SEC: int = int(os.getenv("POLL_INTERVAL_SEC", "30"))

# Стоп-лосс: закрыть позицию если текущая цена < входная * STOP_LOSS_PERCENT
# Значение 0.80 означает выход при падении цены на 20%
STOP_LOSS_PERCENT: float = float(os.getenv("STOP_LOSS_PERCENT", "0.80"))

# Интервал проверки стоп-лоссов (секунды)
STOP_LOSS_CHECK_INTERVAL_SEC: int = 60

# ============================================================
# ОБЩИЕ НАСТРОЙКИ БОТА
# ============================================================

# DRY_RUN=True → бот только логирует сделки, не исполняет реальные ордера
# ОБЯЗАТЕЛЬНО протестируйте в этом режиме перед включением реальной торговли!
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

# Уровень логирования: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Путь к файлу лога
LOG_FILE: str = "logs/bot.log"

# Интервал отправки статус-отчёта в Telegram (часы)
STATUS_INTERVAL_HOURS: int = 6

# Максимум строк лога для ротации файла (5 MB)
LOG_MAX_BYTES: int = 5 * 1024 * 1024
LOG_BACKUP_COUNT: int = 3

# ============================================================
# СЕТЕВЫЕ НАСТРОЙКИ
# ============================================================

# RPC-узел Polygon (для проверки баланса через web3.py)
POLYGON_RPC: str = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

# Базовые URL Polymarket API
CLOB_HOST: str = "https://clob.polymarket.com"
DATA_API_HOST: str = "https://data-api.polymarket.com"
GAMMA_API_HOST: str = "https://gamma-api.polymarket.com"

# Таймаут HTTP запросов (секунды)
HTTP_TIMEOUT: int = 15

# Chain ID сети Polygon Mainnet
CHAIN_ID: int = 137

# Адрес USDC контракта на Polygon
USDC_CONTRACT: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ============================================================
# НАСТРОЙКИ ВЕБ-ДАШБОРДА
# ============================================================

# Порт Flask-дашборда
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "5000"))

# Хост (0.0.0.0 = доступен из локальной сети)
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")


# ============================================================
# ВАЛИДАЦИЯ КОНФИГУРАЦИИ
# ============================================================

def validate_config() -> list[str]:
    """
    Проверяет обязательные настройки и возвращает список предупреждений.
    Не выбрасывает исключение — бот может работать в dry-run без ключей.
    """
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
