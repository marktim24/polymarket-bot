"""
risk_manager.py — Расширенный риск-менеджмент v2.0.

Новое в v2.0:
- Дневной лимит потерь $6 (остановка торговли при превышении)
- Стоп после 2 подряд убыточных сделок
- Снижение размера позиций на 50% при просадке >20%
- Тейк-профит: +20% → закрыть 50%, +40% → закрыть 25%
- Временной стоп: нет движения 24ч → выход | максимум держать 72ч
- Выход если трейдер-источник продаёт тот же токен
- Размер позиции по типу сигнала: BASE/$2, MEDIUM/$3, HIGH/$5
"""

import threading
import time
import logging
from datetime import datetime, timezone, date, timedelta
from dataclasses import dataclass, field
from typing import Optional, Callable

import requests

import config
from monitor import TradeActivity

logger = logging.getLogger(__name__)


# ============================================================
# ПРИЧИНЫ ОТКАЗА
# ============================================================

class SkipReason:
    PRICE_TOO_LOW    = "цена ниже MIN_ENTRY_PRICE ({:.3f} < {:.3f})"
    PRICE_TOO_HIGH   = "цена выше MAX_ENTRY_PRICE ({:.3f} > {:.3f})"
    SIZE_TOO_SMALL   = "размер позиции ${:.2f} меньше минимума ${:.2f}"
    MAX_POSITIONS    = "достигнут лимит позиций ({}/{})"
    MAX_EXPOSURE     = "достигнут лимит экспозиции (${:.2f} > ${:.2f})"
    MARKET_INACTIVE  = "рынок закрыт или статус неизвестен (fail-safe)"
    NOT_BUY          = "не BUY операция ({})"
    MISSING_TOKEN    = "нет token_id"
    SIGNAL_IGNORE    = "сигнал классифицирован как IGNORE: {}"
    DAILY_LOSS       = "превышен дневной лимит потерь (${:.2f} из ${:.2f})"
    CONSECUTIVE_LOSS = "остановка: {} подряд убыточных сделок"
    TRADING_HALTED   = "торговля приостановлена до следующего дня"


# ============================================================
# ОТКРЫТАЯ ПОЗИЦИЯ (расширена полями для TP и time-stop)
# ============================================================

@dataclass
class OpenPosition:
    order_id: str
    token_id: str
    trader_name: str
    entry_price: float
    size_usd: float
    shares: float

    # Тип сигнала при открытии
    signal_type: str = "MEDIUM"

    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    status: str = "open"
    closed_at: Optional[datetime] = None
    realized_pnl: float = 0.0
    market_slug: str = ""

    # Тейк-профит флаги
    tp1_triggered: bool = False   # +20% TP уже сработал
    tp2_triggered: bool = False   # +40% TP уже сработал

    # Для time-stop: последнее время значимого движения цены
    last_significant_price_change: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_price_for_movement: float = 0.0

    def update_pnl(self, current_price: float):
        """Обновляет PnL и отслеживает движение цены для time-stop."""
        # Порог "значимого движения" — 1%
        if self.last_price_for_movement > 0:
            movement = abs(current_price - self.last_price_for_movement) / self.last_price_for_movement
            if movement >= 0.01:
                self.last_significant_price_change = datetime.now(timezone.utc)
                self.last_price_for_movement = current_price
        else:
            self.last_price_for_movement = current_price

        self.current_price = current_price
        if self.entry_price > 0:
            self.unrealized_pnl = (
                (current_price - self.entry_price) / self.entry_price
            ) * self.size_usd

    def pnl_pct(self) -> float:
        """Процентное изменение цены от входа."""
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    def is_stop_loss_triggered(self) -> bool:
        if self.current_price <= 0:
            return False
        return self.current_price < self.entry_price * config.STOP_LOSS_PERCENT

    def is_tp1_due(self) -> bool:
        """TP1: цена выросла на +20% и TP1 ещё не срабатывал."""
        return not self.tp1_triggered and self.pnl_pct() >= config.TAKE_PROFIT_1_PCT

    def is_tp2_due(self) -> bool:
        """TP2: цена выросла на +40% и TP2 ещё не срабатывал."""
        return not self.tp2_triggered and self.pnl_pct() >= config.TAKE_PROFIT_2_PCT

    def is_time_stop_no_movement(self) -> bool:
        """Нет значимого движения цены более TIME_STOP_NO_MOVEMENT_HOURS."""
        elapsed = (
            datetime.now(timezone.utc) - self.last_significant_price_change
        ).total_seconds() / 3600.0
        return elapsed >= config.TIME_STOP_NO_MOVEMENT_HOURS

    def is_max_hold_exceeded(self) -> bool:
        """Позиция удерживается дольше MAX_HOLD_HOURS."""
        elapsed = (
            datetime.now(timezone.utc) - self.opened_at
        ).total_seconds() / 3600.0
        return elapsed >= config.MAX_HOLD_HOURS

    def hours_held(self) -> float:
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 3600.0

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "token_id": self.token_id,
            "trader_name": self.trader_name,
            "signal_type": self.signal_type,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "size_usd": self.size_usd,
            "shares": self.shares,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "status": self.status,
            "market_slug": self.market_slug,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "tp1_triggered": self.tp1_triggered,
            "tp2_triggered": self.tp2_triggered,
            "hours_held": round(self.hours_held(), 1),
        }


# ============================================================
# ДНЕВНАЯ СТАТИСТИКА
# ============================================================

@dataclass
class DailyStats:
    """Статистика текущего торгового дня."""
    date: date = field(default_factory=date.today)
    realized_loss: float = 0.0    # Только убытки (отрицательное значение)
    realized_profit: float = 0.0  # Только прибыль
    trades_count: int = 0
    consecutive_losses: int = 0   # Серия подряд убыточных сделок
    trading_halted: bool = False   # Пауза из-за лимитов

    def record_close(self, pnl: float):
        """Записывает результат закрытой сделки."""
        self.trades_count += 1
        if pnl < 0:
            self.realized_loss += pnl  # отрицательное число
            self.consecutive_losses += 1
        else:
            self.realized_profit += pnl
            self.consecutive_losses = 0  # Сбрасываем серию при прибыли

        # Проверяем лимиты
        if abs(self.realized_loss) >= config.DAILY_LOSS_LIMIT_USD:
            self.trading_halted = True
            logger.warning(
                "🛑 Дневной лимит потерь достигнут: $%.2f. Торговля остановлена до следующего дня.",
                abs(self.realized_loss),
            )

        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            self.trading_halted = True
            logger.warning(
                "🛑 %d подряд убыточных сделок. Торговля остановлена до следующего дня.",
                self.consecutive_losses,
            )

    def is_today(self) -> bool:
        return self.date == date.today()

    def total_pnl(self) -> float:
        return self.realized_profit + self.realized_loss


# ============================================================
# МЕНЕДЖЕР РИСКОВ
# ============================================================

class RiskManager:
    """
    Центральный менеджер рисков v2.0.

    Управляет позициями, фильтрует входящие сделки, отслеживает
    дневные лимиты, серии убытков, просадку, тейк-профиты и временные стопы.
    """

    def __init__(
        self,
        on_stop_loss: Optional[Callable] = None,
        on_take_profit: Optional[Callable] = None,
        on_time_stop: Optional[Callable] = None,
        on_trader_exit: Optional[Callable] = None,
        monitor_manager=None,
    ):
        self._lock = threading.Lock()
        self._positions: dict[str, OpenPosition] = {}
        self._closed_positions: list[OpenPosition] = []

        # Callbacks
        self._on_stop_loss = on_stop_loss
        self._on_take_profit = on_take_profit
        self._on_time_stop = on_time_stop
        self._on_trader_exit = on_trader_exit

        # Ссылка на MonitorManager для sell-сигналов
        self._monitor_manager = monitor_manager

        # Статистика сессии
        self.total_copied: int = 0
        self.total_skipped: int = 0
        self.session_realized_pnl: float = 0.0

        # Дневная статистика (пересоздаётся каждый день)
        self._daily: DailyStats = DailyStats()

        # Начальный баланс сессии (для расчёта просадки)
        self._session_start_balance: float = 0.0

        # HTTP сессия для цен
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Поток мониторинга выходов
        self._stop_event = threading.Event()
        self._exit_thread: Optional[threading.Thread] = None

    def set_session_balance(self, balance: float):
        """Устанавливает начальный баланс для расчёта просадки."""
        self._session_start_balance = balance

    # ----------------------------------------------------------
    # ВАЛИДАЦИЯ ВХОДЯЩИХ СДЕЛОК
    # ----------------------------------------------------------

    def _ensure_daily_stats(self):
        """Сбрасывает дневную статистику если наступил новый день."""
        if not self._daily.is_today():
            logger.info(
                "📅 Новый день. Сброс дневной статистики. "
                "Предыдущий день: PnL=$%.2f, сделок=%d",
                self._daily.total_pnl(), self._daily.trades_count,
            )
            self._daily = DailyStats()

    def validate_trade(
        self, activity: TradeActivity, market_checker=None
    ) -> tuple[bool, str]:
        """
        Проверяет сделку на соответствие всем параметрам риска.

        Returns: (True, "") или (False, причина_отказа)
        """
        self._ensure_daily_stats()

        # 1. Торговля не приостановлена
        if self._daily.trading_halted:
            return False, SkipReason.TRADING_HALTED

        # 2. Тип операции
        if not activity.is_valid_buy():
            return False, SkipReason.NOT_BUY.format(activity.action)

        # 3. token_id
        if not activity.token_id:
            return False, SkipReason.MISSING_TOKEN

        # 4. Сигнал не IGNORE
        if activity.signal_type == "IGNORE":
            return False, SkipReason.SIGNAL_IGNORE.format(activity.signal_reason)

        # 5. Диапазон цены
        if activity.price < config.MIN_ENTRY_PRICE:
            return False, SkipReason.PRICE_TOO_LOW.format(
                activity.price, config.MIN_ENTRY_PRICE
            )
        if activity.price > config.MAX_ENTRY_PRICE:
            return False, SkipReason.PRICE_TOO_HIGH.format(
                activity.price, config.MAX_ENTRY_PRICE
            )

        # 6. Размер позиции после расчёта
        position_size = self.calculate_position_size(activity.signal_type)
        if position_size < config.MIN_COPY_SIZE_USD:
            return False, SkipReason.SIZE_TOO_SMALL.format(
                position_size, config.MIN_COPY_SIZE_USD
            )

        # 7. Лимит открытых позиций
        with self._lock:
            open_count = len(self._positions)
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, SkipReason.MAX_POSITIONS.format(
                open_count, config.MAX_OPEN_POSITIONS
            )

        # 8. Лимит суммарной экспозиции
        current_exposure = self.get_total_exposure()
        if current_exposure + position_size > config.MAX_TOTAL_EXPOSURE_USD:
            return False, SkipReason.MAX_EXPOSURE.format(
                current_exposure + position_size, config.MAX_TOTAL_EXPOSURE_USD
            )

        # 9. Дневной лимит потерь
        if abs(self._daily.realized_loss) >= config.DAILY_LOSS_LIMIT_USD:
            return False, SkipReason.DAILY_LOSS.format(
                abs(self._daily.realized_loss), config.DAILY_LOSS_LIMIT_USD
            )

        # 10. Серия убыточных сделок
        if self._daily.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return False, SkipReason.CONSECUTIVE_LOSS.format(
                self._daily.consecutive_losses
            )

        # 11. Статус рынка (FAIL-SAFE: None/False → не торговать)
        if market_checker is not None:
            if not market_checker.is_market_active(activity.token_id):
                return False, SkipReason.MARKET_INACTIVE

        return True, ""

    def calculate_position_size(self, signal_type: str) -> float:
        """
        Рассчитывает размер позиции по типу сигнала.
        При просадке >20% размер снижается вдвое.
        """
        base = {
            "HIGH":   config.HIGH_POSITION_USD,
            "MEDIUM": config.MEDIUM_POSITION_USD,
        }.get(signal_type, config.BASE_POSITION_USD)

        # Проверка просадки
        if self._session_start_balance > 0:
            current_equity = (
                self._session_start_balance
                + self.session_realized_pnl
                + self.get_total_unrealized_pnl()
            )
            drawdown = (self._session_start_balance - current_equity) / self._session_start_balance
            if drawdown >= config.DRAWDOWN_REDUCE_THRESHOLD:
                logger.warning(
                    "⚠️ Просадка %.1f%% >= %.0f%% → размер позиции снижен на 50%%",
                    drawdown * 100, config.DRAWDOWN_REDUCE_THRESHOLD * 100,
                )
                base *= 0.5

        return min(base, config.MAX_POSITION_USD)

    def calculate_shares(self, size_usd: float, price: float) -> float:
        if price <= 0:
            return 0.0
        return size_usd / price

    # ----------------------------------------------------------
    # УПРАВЛЕНИЕ ПОЗИЦИЯМИ
    # ----------------------------------------------------------

    def register_position(self, position: OpenPosition):
        with self._lock:
            self._positions[position.order_id] = position
            self.total_copied += 1
        logger.info(
            "✅ Позиция открыта [%s]: %s | трейдер=%s | цена=%.3f | $%.2f",
            position.signal_type, position.order_id[:16],
            position.trader_name, position.entry_price, position.size_usd,
        )

    def close_position(
        self, order_id: str, realized_pnl: float = 0.0, reason: str = "manual"
    ):
        with self._lock:
            position = self._positions.pop(order_id, None)
        if position is None:
            return

        position.status = reason
        position.closed_at = datetime.now(timezone.utc)
        position.realized_pnl = realized_pnl
        self.session_realized_pnl += realized_pnl

        # Обновляем дневную статистику
        self._ensure_daily_stats()
        self._daily.record_close(realized_pnl)

        with self._lock:
            self._closed_positions.append(position)

        logger.info(
            "📤 Позиция закрыта [%s]: %s | PnL=$%.2f | держали=%.1fч",
            reason, order_id[:16], realized_pnl, position.hours_held(),
        )

    def partial_close_position(
        self, order_id: str, close_ratio: float, reason: str
    ) -> float:
        """
        Частичное закрытие позиции (для тейк-профитов).
        Уменьшает shares и size_usd позиции на close_ratio.
        Возвращает USD сумму для закрытия.
        """
        with self._lock:
            pos = self._positions.get(order_id)
        if not pos:
            return 0.0

        close_usd = pos.size_usd * close_ratio
        close_shares = pos.shares * close_ratio

        # Обновляем оставшуюся позицию
        with self._lock:
            pos.size_usd -= close_usd
            pos.shares -= close_shares

        logger.info(
            "📊 Частичное закрытие [%s]: %s | %.0f%% | $%.2f",
            reason, order_id[:16], close_ratio * 100, close_usd,
        )
        return close_usd

    def get_open_positions(self) -> list[OpenPosition]:
        with self._lock:
            return list(self._positions.values())

    def get_closed_positions(self) -> list[OpenPosition]:
        with self._lock:
            return list(self._closed_positions)

    def get_total_exposure(self) -> float:
        with self._lock:
            return sum(p.size_usd for p in self._positions.values())

    def get_total_unrealized_pnl(self) -> float:
        with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values())

    # ----------------------------------------------------------
    # ПОЛУЧЕНИЕ ТЕКУЩИХ ЦЕН
    # ----------------------------------------------------------

    def _fetch_current_price(self, token_id: str) -> Optional[float]:
        try:
            url = f"{config.CLOB_HOST}/midpoint?token_id={token_id}"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                p = resp.json().get("mid") or resp.json().get("price")
                if p is not None:
                    return float(p)
        except Exception:
            pass
        try:
            url = f"{config.CLOB_HOST}/price?token_id={token_id}&side=sell"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                p = resp.json().get("price")
                if p is not None:
                    return float(p)
        except Exception as e:
            logger.debug("Не удалось получить цену для %s: %s", token_id[:12], e)
        return None

    # ----------------------------------------------------------
    # ПОТОК МОНИТОРИНГА ВЫХОДОВ (стоп-лосс + TP + time-stop + trader exit)
    # ----------------------------------------------------------

    def _exit_check_loop(self):
        """
        Фоновый поток: каждые STOP_LOSS_CHECK_INTERVAL_SEC секунд проверяет
        все условия выхода из открытых позиций.
        """
        logger.info(
            "🛡️ Поток выходов запущен (интервал %d сек)",
            config.STOP_LOSS_CHECK_INTERVAL_SEC,
        )

        while not self._stop_event.is_set():
            positions = self.get_open_positions()

            for pos in positions:
                if self._stop_event.is_set():
                    break
                try:
                    self._check_exits_for_position(pos)
                except Exception as e:
                    logger.error(
                        "Ошибка проверки выходов для %s: %s",
                        pos.order_id[:16], e, exc_info=True,
                    )

            self._stop_event.wait(timeout=config.STOP_LOSS_CHECK_INTERVAL_SEC)

    def _check_exits_for_position(self, pos: OpenPosition):
        """Проверяет все условия выхода для одной позиции."""

        # --- 0. Проверка sell-сигнала от трейдера-источника ---
        if self._monitor_manager:
            sellers = self._monitor_manager.get_sell_signals_for_token(pos.token_id)
            if pos.trader_name in sellers:
                logger.info(
                    "🔄 Трейдер %s продал %s → закрываем позицию",
                    pos.trader_name, pos.order_id[:16],
                )
                self._monitor_manager.clear_sell_signal(pos.token_id)
                if self._on_trader_exit:
                    self._on_trader_exit(pos)
                return  # Не проверяем остальные условия

        # --- Получаем текущую цену ---
        current_price = self._fetch_current_price(pos.token_id)
        if current_price is None:
            return  # Нет данных — не трогаем

        pos.update_pnl(current_price)

        # --- 1. Стоп-лосс ---
        if pos.is_stop_loss_triggered():
            logger.warning(
                "🚨 СТОП-ЛОСС %s: %.4f < %.4f (вход=%.4f)",
                pos.order_id[:16], current_price,
                pos.entry_price * config.STOP_LOSS_PERCENT,
                pos.entry_price,
            )
            if self._on_stop_loss:
                self._on_stop_loss(pos)
            return

        # --- 2. Тейк-профит 2 (+40% → закрыть 25%) ---
        if pos.is_tp2_due():
            logger.info(
                "💰 TP2 (+40%%) %s: цена=%.4f | закрываем 25%%",
                pos.order_id[:16], current_price,
            )
            pos.tp2_triggered = True
            if self._on_take_profit:
                self._on_take_profit(pos, config.TAKE_PROFIT_2_CLOSE_RATIO, "tp2")
            return

        # --- 3. Тейк-профит 1 (+20% → закрыть 50%) ---
        if pos.is_tp1_due():
            logger.info(
                "💰 TP1 (+20%%) %s: цена=%.4f | закрываем 50%%",
                pos.order_id[:16], current_price,
            )
            pos.tp1_triggered = True
            if self._on_take_profit:
                self._on_take_profit(pos, config.TAKE_PROFIT_1_CLOSE_RATIO, "tp1")
            # Не return — продолжаем держать остаток

        # --- 4. Временной стоп: нет движения 24ч ---
        if pos.is_time_stop_no_movement():
            logger.info(
                "⏰ TIME-STOP (нет движения %.0fч) %s",
                config.TIME_STOP_NO_MOVEMENT_HOURS, pos.order_id[:16],
            )
            if self._on_time_stop:
                self._on_time_stop(pos, "no_movement")
            return

        # --- 5. Временной стоп: максимальное время удержания ---
        if pos.is_max_hold_exceeded():
            logger.info(
                "⏰ TIME-STOP (макс. удержание %.0fч) %s",
                config.MAX_HOLD_HOURS, pos.order_id[:16],
            )
            if self._on_time_stop:
                self._on_time_stop(pos, "max_hold")
            return

    def start_stop_loss_monitor(self):
        self._stop_event.clear()
        self._exit_thread = threading.Thread(
            target=self._exit_check_loop,
            name="ExitCheckThread",
            daemon=True,
        )
        self._exit_thread.start()

    def stop_stop_loss_monitor(self):
        self._stop_event.set()
        if self._exit_thread:
            self._exit_thread.join(timeout=10)
        logger.info("Поток выходов остановлен")

    def set_stop_loss_callback(self, callback: Callable):
        self._on_stop_loss = callback

    # ----------------------------------------------------------
    # СТАТИСТИКА
    # ----------------------------------------------------------

    def get_session_stats(self) -> dict:
        self._ensure_daily_stats()
        open_positions = self.get_open_positions()
        return {
            "total_copied": self.total_copied,
            "total_skipped": self.total_skipped,
            "open_positions": len(open_positions),
            "closed_positions": len(self._closed_positions),
            "total_exposure_usd": self.get_total_exposure(),
            "unrealized_pnl": self.get_total_unrealized_pnl(),
            "realized_pnl": self.session_realized_pnl,
            "total_pnl": self.session_realized_pnl + self.get_total_unrealized_pnl(),
            "daily_loss": self._daily.realized_loss,
            "daily_consecutive_losses": self._daily.consecutive_losses,
            "trading_halted": self._daily.trading_halted,
        }

    def get_daily_stats(self) -> DailyStats:
        self._ensure_daily_stats()
        return self._daily

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        all_positions = []
        with self._lock:
            all_positions.extend(list(self._positions.values()))
            all_positions.extend(self._closed_positions)
        all_positions.sort(key=lambda p: p.opened_at, reverse=True)
        return [p.to_dict() for p in all_positions[:limit]]
