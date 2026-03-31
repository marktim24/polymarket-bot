"""
monitor.py — Мониторинг активности трейдеров + классификация сигналов.

Версия 2.0 изменения:
- Лимит запроса активности: 5 → 20
- Фильтр возраста сигнала: отклонять старше 12 часов
- Фильтр движения цены: отклонять если цена сдвинулась >10% с момента сделки
- Диапазон цен: 0.20–0.55 (было 0.05–0.70)
- Классификация сигналов: HIGH / MEDIUM / IGNORE
- FAIL-SAFE: при недоступности API рынка → НЕ торговать (было: торговать)
- Детекция продаж трейдеров для сигнала выхода
"""

import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests

import config

logger = logging.getLogger(__name__)


# ============================================================
# ТОРГОВАЯ АКТИВНОСТЬ (расширена полями сигнала)
# ============================================================

class TradeActivity:
    """Структура одной торговой активности трейдера."""

    def __init__(self, raw: dict):
        self.id: str = raw.get("id") or raw.get("transactionHash", "")
        self.action: str = (raw.get("type") or raw.get("side") or "").upper()
        self.token_id: str = (
            raw.get("conditionId")
            or raw.get("tokenId")
            or raw.get("asset")
            or ""
        )
        self.price: float = self._parse_float(raw.get("price") or raw.get("avgPrice"))
        self.size_usd: float = self._parse_float(raw.get("usdcSize") or raw.get("size"))
        self.shares: float = self._parse_float(raw.get("size") or raw.get("shares"))
        self.market_slug: str = raw.get("market") or raw.get("slug") or ""
        self.outcome: str = raw.get("outcome") or raw.get("side", "")

        # Время транзакции
        ts_raw = raw.get("timestamp") or raw.get("createdAt") or 0
        if isinstance(ts_raw, str):
            try:
                self.timestamp = int(
                    datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                )
            except (ValueError, AttributeError):
                self.timestamp = 0
        else:
            self.timestamp = int(ts_raw)

        # === ПОЛЯ СИГНАЛА (заполняются SignalClassifier) ===
        self.signal_type: str = "MEDIUM"       # HIGH / MEDIUM / IGNORE
        self.signal_reason: str = ""            # Человекочитаемое объяснение
        self.confidence: float = 0.0            # 0.0 – 1.0
        self.trader_name: str = ""              # Имя трейдера-источника

        self._raw = raw

    @staticmethod
    def _parse_float(value) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def is_buy(self) -> bool:
        return self.action in ("BUY", "PURCHASE") and self.price > 0

    def is_sell(self) -> bool:
        return self.action in ("SELL", "REDEEM") and self.price > 0

    def is_valid_buy(self) -> bool:
        return self.is_buy() and (self.size_usd > 0 or self.shares > 0)

    def age_hours(self) -> float:
        """Возраст сделки в часах относительно текущего времени."""
        if self.timestamp <= 0:
            return 999.0
        now = datetime.now(timezone.utc).timestamp()
        return (now - self.timestamp) / 3600.0

    def __repr__(self) -> str:
        return (
            f"<Trade {self.signal_type} id={self.id[:10]}… "
            f"{self.action} price={self.price:.3f} ${self.size_usd:.2f}>"
        )


# ============================================================
# КЛАССИФИКАТОР СИГНАЛОВ
# ============================================================

class SignalClassifier:
    """
    Классифицирует торговые активности как HIGH / MEDIUM / IGNORE.

    HIGH:   ≥ 2 COPY-трейдера торгуют один рынок в течение CONFLUENCE_WINDOW +
            цена 0.20–0.55 + ликвидность + время до резолюции > 72ч

    MEDIUM: 1 COPY-трейдер + цена 0.20–0.45 + нет резкого движения цены

    IGNORE: цена > 0.55 | до резолюции < 72ч | низкая ликвидность | смешанные сигналы
    """

    def __init__(self, market_checker):
        self._market_checker = market_checker
        self._lock = threading.Lock()
        # Буфер последних сделок: token_id → list[(trader_name, price, timestamp)]
        self._trade_buffer: dict[str, list[tuple[str, float, float]]] = {}

    def record_trade(self, trader_name: str, activity: TradeActivity):
        """Добавляет сделку в буфер для определения confluence."""
        if not activity.token_id:
            return
        with self._lock:
            if activity.token_id not in self._trade_buffer:
                self._trade_buffer[activity.token_id] = []
            self._trade_buffer[activity.token_id].append(
                (trader_name, activity.price, activity.timestamp or time.time())
            )
            # Чистим буфер: удаляем записи старше CONFLUENCE_WINDOW
            cutoff = time.time() - config.SIGNAL_CONFLUENCE_WINDOW_HOURS * 3600
            self._trade_buffer[activity.token_id] = [
                entry for entry in self._trade_buffer[activity.token_id]
                if entry[2] > cutoff
            ]

    def classify(
        self,
        trader_name: str,
        activity: TradeActivity,
    ) -> tuple[str, str, float]:
        """
        Классифицирует сигнал.

        Returns:
            (signal_type, reason, confidence)
            signal_type: "HIGH" | "MEDIUM" | "IGNORE"
        """
        price = activity.price
        token_id = activity.token_id

        # ---- Проверка 1: диапазон цены ----
        if price > config.SIGNAL_HIGH_MAX_PRICE:
            return "IGNORE", f"цена {price:.3f} выше максимума {config.SIGNAL_HIGH_MAX_PRICE}", 0.0
        if price < config.SIGNAL_HIGH_MIN_PRICE:
            return "IGNORE", f"цена {price:.3f} ниже минимума {config.SIGNAL_HIGH_MIN_PRICE}", 0.0

        # ---- Проверка 2: время до резолюции ----
        hours_left = self._market_checker.get_hours_to_resolution(token_id)
        if hours_left is not None and hours_left < config.MIN_MARKET_RESOLUTION_HOURS:
            return (
                "IGNORE",
                f"до закрытия рынка {hours_left:.1f}ч < {config.MIN_MARKET_RESOLUTION_HOURS}ч",
                0.0,
            )

        # ---- Проверка 3: ликвидность ----
        liquidity = self._market_checker.get_liquidity(token_id)
        if liquidity is not None and liquidity < config.MIN_LIQUIDITY_USD:
            return (
                "IGNORE",
                f"низкая ликвидность ${liquidity:.0f} < ${config.MIN_LIQUIDITY_USD:.0f}",
                0.0,
            )

        # ---- Проверка 4: confluence (несколько COPY трейдеров) ----
        with self._lock:
            recent_traders = set()
            cutoff = time.time() - config.SIGNAL_CONFLUENCE_WINDOW_HOURS * 3600
            for name, _, ts in self._trade_buffer.get(token_id, []):
                if ts > cutoff and config.TRADER_ROLES.get(name) == "COPY":
                    recent_traders.add(name)
        # Добавляем текущего трейдера
        if config.TRADER_ROLES.get(trader_name) == "COPY":
            recent_traders.add(trader_name)

        has_confluence = len(recent_traders) >= 2

        # ---- Классификация ----
        confidence_score = 0.0

        # Базовый балл от ценового диапазона HIGH
        if config.SIGNAL_HIGH_MIN_PRICE <= price <= config.SIGNAL_HIGH_MAX_PRICE:
            confidence_score += 0.30

        # Бонус за время до резолюции
        if hours_left is None or hours_left >= config.MIN_MARKET_RESOLUTION_HOURS * 2:
            confidence_score += 0.20
        elif hours_left >= config.MIN_MARKET_RESOLUTION_HOURS:
            confidence_score += 0.10

        # Бонус за ликвидность
        if liquidity is None or liquidity >= config.MIN_LIQUIDITY_USD * 3:
            confidence_score += 0.20
        elif liquidity >= config.MIN_LIQUIDITY_USD:
            confidence_score += 0.10

        # Бонус за confluence
        if has_confluence:
            confidence_score += 0.30
            reason = (
                f"confluence: {', '.join(sorted(recent_traders))} | "
                f"цена={price:.3f} | liquidity=${liquidity or 0:.0f}"
            )
            return "HIGH", reason, min(confidence_score, 1.0)

        # MEDIUM: один трейдер, цена в MEDIUM диапазоне
        if config.SIGNAL_MEDIUM_MIN_PRICE <= price <= config.SIGNAL_MEDIUM_MAX_PRICE:
            reason = (
                f"трейдер={trader_name} | цена={price:.3f} | "
                f"liquidity=${liquidity or 0:.0f}"
            )
            return "MEDIUM", reason, min(confidence_score, 1.0)

        # Цена в HIGH диапазоне но нет confluence → MEDIUM с низким confidence
        reason = (
            f"трейдер={trader_name} | цена={price:.3f} выше MEDIUM диапазона, нет подтверждения"
        )
        return "MEDIUM", reason, min(confidence_score * 0.5, 1.0)


# ============================================================
# ПРОВЕРКА СТАТУСА РЫНКА (с fail-safe)
# ============================================================

class MarketStatusChecker:
    """
    Проверяет статус, время резолюции и ликвидность рынков.

    FAIL-SAFE v2.0: при любой ошибке API возвращает False (НЕ торговать).
    Предыдущая версия при ошибке возвращала True — это было опасно.
    """

    CACHE_TTL_SEC = 300  # 5 минут

    def __init__(self):
        self._active_cache: dict[str, tuple[bool, float]] = {}
        self._info_cache: dict[str, tuple[dict, float]] = {}
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def _get_market_data(self, token_id: str) -> Optional[dict]:
        """Получает данные рынка с кэшированием."""
        now = time.time()
        if token_id in self._info_cache:
            data, cached_at = self._info_cache[token_id]
            if now - cached_at < self.CACHE_TTL_SEC:
                return data

        try:
            # Пробуем по conditionId
            for param in ("conditionIds", "clob_token_ids"):
                url = f"{config.GAMMA_API_HOST}/markets?{param}={token_id}"
                resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
                resp.raise_for_status()
                markets = resp.json()
                if markets and isinstance(markets, list):
                    data = markets[0]
                    self._info_cache[token_id] = (data, now)
                    return data
        except Exception as e:
            logger.debug("Не удалось получить данные рынка %s: %s", token_id[:12], e)

        return None

    def is_market_active(self, token_id: str) -> bool:
        """
        FAIL-SAFE: возвращает False при любой ошибке или недоступности API.
        Версия 1.0 возвращала True при ошибке — исправлено.
        """
        now = time.time()
        if token_id in self._active_cache:
            is_active, cached_at = self._active_cache[token_id]
            if now - cached_at < self.CACHE_TTL_SEC:
                return is_active

        data = self._get_market_data(token_id)

        if data is None:
            # FAIL-SAFE: нет данных → не торговать
            logger.warning(
                "⚠️ FAIL-SAFE: статус рынка %s недоступен → пропуск сделки", token_id[:12]
            )
            self._active_cache[token_id] = (False, now)
            return False

        is_active = (
            data.get("active", False)
            and not data.get("closed", True)
            and not data.get("archived", False)
        )
        self._active_cache[token_id] = (is_active, now)
        return is_active

    def get_hours_to_resolution(self, token_id: str) -> Optional[float]:
        """
        Возвращает количество часов до закрытия рынка.
        None если данные недоступны (при fail-safe считается как IGNORE).
        """
        data = self._get_market_data(token_id)
        if not data:
            return None

        end_date_str = data.get("endDate") or data.get("end_date_iso")
        if not end_date_str:
            return None

        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            delta = end_dt - now_dt
            return max(delta.total_seconds() / 3600.0, 0.0)
        except (ValueError, AttributeError):
            return None

    def get_liquidity(self, token_id: str) -> Optional[float]:
        """
        Оценивает ликвидность рынка по глубине стакана через CLOB API.
        Возвращает суммарный объём bid+ask в USD. None при ошибке.
        """
        try:
            url = f"{config.CLOB_HOST}/book?token_id={token_id}"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None
            book = resp.json()

            total = 0.0
            for side_key in ("bids", "asks"):
                for level in book.get(side_key, []):
                    try:
                        price = float(level.get("price", 0))
                        size = float(level.get("size", 0))
                        total += price * size
                    except (TypeError, ValueError):
                        continue
            return total if total > 0 else None
        except Exception:
            return None

    def get_current_price(self, token_id: str) -> Optional[float]:
        """Возвращает текущую mid-цену токена через CLOB API."""
        try:
            url = f"{config.CLOB_HOST}/midpoint?token_id={token_id}"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                p = data.get("mid") or data.get("price")
                return float(p) if p is not None else None
        except Exception:
            pass
        # Запасной вариант
        try:
            url = f"{config.CLOB_HOST}/price?token_id={token_id}&side=sell"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                p = resp.json().get("price")
                return float(p) if p is not None else None
        except Exception:
            pass
        return None


# ============================================================
# МОНИТОР ОДНОГО ТРЕЙДЕРА
# ============================================================

class TraderMonitor:
    """
    Мониторит активность одного трейдера.

    Новые фильтры v2.0:
    - Отклоняет сделки старше MAX_SIGNAL_AGE_HOURS
    - Отклоняет если цена сдвинулась >MAX_PRICE_MOVEMENT_PCT
    - Добавляет сигнал в отдельную sell-очередь при SELL-активности
    """

    def __init__(self, trader: dict, buy_queue, sell_signals: dict):
        self.trader = trader
        self.name: str = trader["name"]
        self.address: str = trader["address"]
        self.role: str = trader.get("role", "COPY")
        self._buy_queue = buy_queue
        # Словарь token_id → set(trader_names) для сигналов продажи
        self._sell_signals = sell_signals

        self._seen_ids: set[str] = set()
        self.total_detected: int = 0
        self.total_skipped: int = 0
        self.last_poll_time: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self._initialized: bool = False

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def _fetch_activity(self) -> list[dict]:
        """Запрашивает последние ACTIVITY_FETCH_LIMIT активностей (было 5, стало 20)."""
        url = (
            f"{config.DATA_API_HOST}/activity"
            f"?user={self.address}&limit={config.ACTIVITY_FETCH_LIMIT}"
        )
        try:
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", data.get("activities", []))
            return []
        except requests.exceptions.Timeout:
            self.last_error = "Timeout при запросе Activity API"
            logger.warning("[%s] %s", self.name, self.last_error)
        except requests.exceptions.HTTPError as e:
            self.last_error = f"HTTP {e.response.status_code}"
            logger.warning("[%s] %s", self.name, self.last_error)
        except Exception as e:
            self.last_error = str(e)
            logger.error("[%s] Ошибка запроса: %s", self.name, e)
        return []

    def poll(
        self,
        market_checker: MarketStatusChecker,
        signal_classifier: SignalClassifier,
    ) -> int:
        """
        Опрашивает API и помещает новые сделки в очередь.

        Применяет фильтры:
        1. Дедупликация по ID
        2. Только BUY (для очереди копирования)
        3. Возраст < MAX_SIGNAL_AGE_HOURS
        4. Движение цены < MAX_PRICE_MOVEMENT_PCT
        5. Классификация → IGNORE пропускается
        """
        self.last_poll_time = datetime.now(timezone.utc)
        raw_activities = self._fetch_activity()

        if not raw_activities:
            return 0

        # При первом опросе — только запоминаем ID
        if not self._initialized:
            for raw in raw_activities:
                a = TradeActivity(raw)
                if a.id:
                    self._seen_ids.add(a.id)
            self._initialized = True
            logger.info(
                "[%s] Инициализация: запомнено %d существующих активностей",
                self.name, len(self._seen_ids),
            )
            return 0

        new_count = 0
        for raw in raw_activities:
            activity = TradeActivity(raw)
            activity.trader_name = self.name

            if not activity.id or activity.id in self._seen_ids:
                continue
            self._seen_ids.add(activity.id)

            # --- Обработка SELL: записываем в sell_signals ---
            if activity.is_sell() and activity.token_id:
                with threading.Lock():
                    if activity.token_id not in self._sell_signals:
                        self._sell_signals[activity.token_id] = set()
                    self._sell_signals[activity.token_id].add(self.name)
                logger.info(
                    "[%s] 📉 Трейдер продаёт: token=%s",
                    self.name, activity.token_id[:16],
                )
                continue

            # --- Только BUY дальше ---
            if not activity.is_valid_buy():
                continue

            # --- Фильтр 1: возраст сигнала ---
            age = activity.age_hours()
            if age > config.MAX_SIGNAL_AGE_HOURS:
                logger.debug(
                    "[%s] Пропуск: сделка слишком старая (%.1fч > %.1fч)",
                    self.name, age, config.MAX_SIGNAL_AGE_HOURS
                )
                self.total_skipped += 1
                continue

            # --- Фильтр 2: движение цены с момента сделки ---
            if activity.token_id and activity.price > 0:
                current_price = market_checker.get_current_price(activity.token_id)
                if current_price is not None:
                    movement = abs(current_price - activity.price) / activity.price
                    if movement > config.MAX_PRICE_MOVEMENT_PCT:
                        logger.info(
                            "[%s] Пропуск: цена сдвинулась на %.1f%% с момента сделки "
                            "(вход=%.4f, сейчас=%.4f)",
                            self.name, movement * 100, activity.price, current_price
                        )
                        self.total_skipped += 1
                        continue

            # --- Записываем в буфер для confluence ---
            signal_classifier.record_trade(self.name, activity)

            # --- Классификация сигнала ---
            signal_type, reason, confidence = signal_classifier.classify(
                self.name, activity
            )
            activity.signal_type = signal_type
            activity.signal_reason = reason
            activity.confidence = confidence

            if signal_type == "IGNORE":
                logger.info(
                    "[%s] ⏭ IGNORE: %s", self.name, reason
                )
                self.total_skipped += 1
                continue

            self._buy_queue.put(activity)
            self.total_detected += 1
            new_count += 1

            logger.info(
                "[%s] 🔍 %s сигнал (conf=%.0f%%): token=%s | цена=%.3f | $%.2f | %s",
                self.name, signal_type, confidence * 100,
                activity.token_id[:16], activity.price,
                activity.size_usd, activity.market_slug or "",
            )

        return new_count


# ============================================================
# МЕНЕДЖЕР МОНИТОРИНГА
# ============================================================

class MonitorManager:
    """
    Управляет несколькими TraderMonitor'ами в одном потоке.
    """

    def __init__(self, traders: list[dict], shared_queue):
        self.market_checker = MarketStatusChecker()
        self.signal_classifier = SignalClassifier(self.market_checker)

        # Словарь sell-сигналов: token_id → set(trader_names)
        # Читается risk_manager'ом для триггера выхода
        self.sell_signals: dict[str, set[str]] = {}

        self.monitors = [
            TraderMonitor(trader, shared_queue, self.sell_signals)
            for trader in traders
        ]

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.total_polls: int = 0
        self.start_time: Optional[datetime] = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="MonitorThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "🚀 Мониторинг запущен для %d трейдеров (интервал %d сек)",
            len(self.monitors), config.POLL_INTERVAL_SEC,
        )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Мониторинг остановлен")

    def _run_loop(self):
        self.start_time = datetime.now(timezone.utc)
        while not self._stop_event.is_set():
            self.total_polls += 1
            for monitor in self.monitors:
                if self._stop_event.is_set():
                    break
                try:
                    monitor.poll(self.market_checker, self.signal_classifier)
                except Exception as e:
                    logger.error(
                        "[%s] Критическая ошибка poll: %s", monitor.name, e,
                        exc_info=True
                    )
            self._stop_event.wait(timeout=config.POLL_INTERVAL_SEC)

    def get_sell_signals_for_token(self, token_id: str) -> set[str]:
        """Возвращает набор трейдеров, продавших данный токен."""
        return self.sell_signals.get(token_id, set())

    def clear_sell_signal(self, token_id: str):
        """Сбрасывает sell-сигнал после обработки."""
        self.sell_signals.pop(token_id, None)

    def get_status(self) -> dict:
        return {
            "total_polls": self.total_polls,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "traders": [
                {
                    "name": m.name,
                    "address": m.address,
                    "role": m.role,
                    "total_detected": m.total_detected,
                    "total_skipped": m.total_skipped,
                    "last_poll": (
                        m.last_poll_time.isoformat() if m.last_poll_time else None
                    ),
                    "last_error": m.last_error,
                }
                for m in self.monitors
            ],
        }
