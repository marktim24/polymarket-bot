"""
monitor.py — Мониторинг активности отслеживаемых трейдеров.

Каждые POLL_INTERVAL_SEC секунд опрашивает Polymarket Data API,
обнаруживает новые сделки и передаёт их в очередь для обработки.
Дедупликация по ID транзакции гарантирует, что каждая сделка
копируется только один раз.
"""

import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
import requests

import config

logger = logging.getLogger(__name__)


class TradeActivity:
    """Структура данных одной торговой активности трейдера."""

    def __init__(self, raw: dict):
        # Уникальный идентификатор транзакции/активности
        self.id: str = raw.get("id") or raw.get("transactionHash", "")

        # Тип операции: BUY / SELL / REDEEM
        self.action: str = (raw.get("type") or raw.get("side") or "").upper()

        # Адрес токена (conditionId или tokenId)
        self.token_id: str = (
            raw.get("conditionId")
            or raw.get("tokenId")
            or raw.get("asset")
            or ""
        )

        # Цена входа (0.0 – 1.0)
        self.price: float = self._parse_float(
            raw.get("price") or raw.get("avgPrice")
        )

        # Размер позиции в USDC
        self.size_usd: float = self._parse_float(
            raw.get("usdcSize") or raw.get("size")
        )

        # Количество контрактов (shares)
        self.shares: float = self._parse_float(
            raw.get("size") or raw.get("shares")
        )

        # Название рынка (опционально, для читаемости логов)
        self.market_slug: str = raw.get("market") or raw.get("slug") or ""
        self.outcome: str = raw.get("outcome") or raw.get("side", "")

        # Время транзакции (Unix timestamp)
        ts_raw = raw.get("timestamp") or raw.get("createdAt") or 0
        if isinstance(ts_raw, str):
            try:
                self.timestamp = int(
                    datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")
                    ).timestamp()
                )
            except (ValueError, AttributeError):
                self.timestamp = 0
        else:
            self.timestamp = int(ts_raw)

        # Оригинальный словарь — для отладки
        self._raw = raw

    @staticmethod
    def _parse_float(value) -> float:
        """Безопасное преобразование значения к float."""
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def is_valid_buy(self) -> bool:
        """Возвращает True если активность является покупкой с ненулевым размером."""
        return (
            self.action in ("BUY", "PURCHASE")
            and self.price > 0
            and (self.size_usd > 0 or self.shares > 0)
        )

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.id[:12]}… {self.action} "
            f"price={self.price:.3f} size=${self.size_usd:.2f}>"
        )


class TraderMonitor:
    """
    Мониторит активность одного трейдера.

    Хранит множество уже виденных ID транзакций, чтобы не дублировать сделки.
    При обнаружении новых сделок помещает их в shared_queue (threading.Queue).
    """

    def __init__(self, trader: dict, shared_queue):
        self.trader = trader
        self.name: str = trader["name"]
        self.address: str = trader["address"]
        self.queue = shared_queue

        # Множество уже обработанных ID транзакций
        self._seen_ids: set[str] = set()

        # Статистика за сессию
        self.total_detected: int = 0
        self.last_poll_time: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._initialized: bool = False

        # HTTP сессия с повтором при ошибках
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def _fetch_activity(self) -> list[dict]:
        """
        Запрашивает последние 5 активностей трейдера через Data API.
        Возвращает список словарей или пустой список при ошибке.
        """
        url = (
            f"{config.DATA_API_HOST}/activity"
            f"?user={self.address}&limit=5"
        )
        try:
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            # API может вернуть список напрямую или обёрнутый в {"data": [...]}
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", data.get("activities", []))
            return []
        except requests.exceptions.Timeout:
            self.last_error = "Timeout при запросе Activity API"
            logger.warning("[%s] %s", self.name, self.last_error)
        except requests.exceptions.HTTPError as e:
            self.last_error = f"HTTP {e.response.status_code} от Activity API"
            logger.warning("[%s] %s", self.name, self.last_error)
        except Exception as e:
            self.last_error = str(e)
            logger.error("[%s] Ошибка запроса активности: %s", self.name, e)
        return []

    def poll(self) -> int:
        """
        Опрашивает API и помещает новые BUY-сделки в очередь.
        Возвращает количество новых сделок.
        """
        self.last_poll_time = datetime.now(timezone.utc)
        raw_activities = self._fetch_activity()

        if not raw_activities:
            return 0

        # При первом опросе только запоминаем ID — не копируем старые сделки
        if not self._initialized:
            for raw in raw_activities:
                activity = TradeActivity(raw)
                if activity.id:
                    self._seen_ids.add(activity.id)
            self._initialized = True
            logger.info(
                "[%s] Инициализация: запомнено %d существующих активностей",
                self.name,
                len(self._seen_ids),
            )
            return 0

        new_count = 0
        for raw in raw_activities:
            activity = TradeActivity(raw)

            # Пропускаем уже виденные транзакции
            if not activity.id or activity.id in self._seen_ids:
                continue

            self._seen_ids.add(activity.id)

            # Пропускаем не-покупки
            if not activity.is_valid_buy():
                logger.debug(
                    "[%s] Пропуск не-BUY активности: %s action=%s",
                    self.name, activity.id[:12], activity.action
                )
                continue

            # Добавляем метаданные трейдера в активность
            raw["_trader_name"] = self.name
            raw["_trader_address"] = self.address
            activity.trader_name = self.name

            self.queue.put(activity)
            self.total_detected += 1
            new_count += 1

            logger.info(
                "[%s] 🔍 Новая сделка: %s | цена=%.3f | размер=$%.2f | рынок=%s",
                self.name,
                activity.id[:16],
                activity.price,
                activity.size_usd,
                activity.market_slug or activity.token_id[:12],
            )

        return new_count


class MarketStatusChecker:
    """
    Проверяет статус рынка через Gamma API.
    Кэширует результаты на 5 минут, чтобы не спамить запросами.
    """

    CACHE_TTL_SEC = 300  # 5 минут

    def __init__(self):
        self._cache: dict[str, tuple[bool, float]] = {}  # token_id → (is_active, ts)
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def is_market_active(self, token_id: str) -> bool:
        """
        Возвращает True если рынок открыт и принимает ордера.
        При ошибке запроса считает рынок активным (не блокируем сделку).
        """
        now = time.time()
        if token_id in self._cache:
            is_active, cached_at = self._cache[token_id]
            if now - cached_at < self.CACHE_TTL_SEC:
                return is_active

        try:
            # Пробуем найти рынок по conditionId
            url = f"{config.GAMMA_API_HOST}/markets?conditionIds={token_id}"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            markets = resp.json()

            if not markets:
                # Пробуем по clob_token_ids
                url2 = f"{config.GAMMA_API_HOST}/markets?clob_token_ids={token_id}"
                resp2 = self._session.get(url2, timeout=config.HTTP_TIMEOUT)
                resp2.raise_for_status()
                markets = resp2.json()

            if markets and isinstance(markets, list) and len(markets) > 0:
                market = markets[0]
                # active=True и closed=False означает открытый рынок
                is_active = (
                    market.get("active", True)
                    and not market.get("closed", False)
                    and not market.get("archived", False)
                )
            else:
                # Рынок не найден — считаем активным, не блокируем
                is_active = True

            self._cache[token_id] = (is_active, now)
            return is_active

        except Exception as e:
            logger.warning("Не удалось проверить статус рынка %s: %s", token_id[:12], e)
            return True  # При ошибке — не блокируем


class MonitorManager:
    """
    Управляет несколькими TraderMonitor'ами в одном потоке.

    Запускает периодический опрос всех трейдеров и обрабатывает
    межпотоковую коммуникацию через shared_queue.
    """

    def __init__(self, traders: list[dict], shared_queue):
        self.monitors = [
            TraderMonitor(trader, shared_queue)
            for trader in traders
        ]
        self.market_checker = MarketStatusChecker()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Статистика
        self.total_polls: int = 0
        self.start_time: Optional[datetime] = None

    def start(self):
        """Запускает мониторинг в фоновом потоке."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="MonitorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "🚀 Мониторинг запущен для %d трейдеров (интервал %d сек)",
            len(self.monitors),
            config.POLL_INTERVAL_SEC,
        )

    def stop(self):
        """Останавливает мониторинг."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Мониторинг остановлен")

    def _run_loop(self):
        """Основной цикл опроса всех трейдеров."""
        self.start_time = datetime.now(timezone.utc)

        while not self._stop_event.is_set():
            self.total_polls += 1
            for monitor in self.monitors:
                if self._stop_event.is_set():
                    break
                try:
                    new_trades = monitor.poll()
                    if new_trades > 0:
                        logger.debug(
                            "[%s] Обнаружено %d новых сделок", monitor.name, new_trades
                        )
                except Exception as e:
                    logger.error(
                        "[%s] Критическая ошибка в poll(): %s", monitor.name, e,
                        exc_info=True
                    )

            # Ждём следующего цикла, с возможностью прерывания
            self._stop_event.wait(timeout=config.POLL_INTERVAL_SEC)

    def get_status(self) -> dict:
        """Возвращает статус всех мониторов для дашборда."""
        return {
            "total_polls": self.total_polls,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "traders": [
                {
                    "name": m.name,
                    "address": m.address,
                    "total_detected": m.total_detected,
                    "last_poll": (
                        m.last_poll_time.isoformat() if m.last_poll_time else None
                    ),
                    "last_error": m.last_error,
                }
                for m in self.monitors
            ],
        }
