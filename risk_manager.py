"""
risk_manager.py — Управление рисками и стоп-лосс.

Отвечает за:
1. Фильтрацию входящих сделок по параметрам риска
2. Хранение и управление открытыми позициями
3. Фоновый поток для проверки стоп-лоссов
4. Расчёт PnL и статистики сессии
"""

import threading
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Callable

import requests

import config
from monitor import TradeActivity

logger = logging.getLogger(__name__)


# ============================================================
# ПРИЧИНЫ ОТКАЗА (для уведомлений)
# ============================================================

class SkipReason:
    PRICE_TOO_LOW = "цена ниже MIN_ENTRY_PRICE ({:.3f} < {:.3f})"
    PRICE_TOO_HIGH = "цена выше MAX_ENTRY_PRICE ({:.3f} > {:.3f})"
    SIZE_TOO_SMALL = "размер после масштабирования меньше MIN_COPY_SIZE_USD (${:.2f} < ${:.2f})"
    MAX_POSITIONS = "достигнут лимит открытых позиций ({}/{})"
    MARKET_INACTIVE = "рынок закрыт или неактивен ({})"
    NOT_BUY = "не BUY операция ({})"
    MISSING_TOKEN = "нет token_id в сделке"
    DUPLICATE = "дублирующаяся сделка"


# ============================================================
# СТРУКТУРА ОТКРЫТОЙ ПОЗИЦИИ
# ============================================================

@dataclass
class OpenPosition:
    """Открытая позиция, скопированная с оригинального трейдера."""

    # Идентификатор ордера от Polymarket (или "dry_run_XXX")
    order_id: str

    # Идентификатор токена/рынка
    token_id: str

    # Имя трейдера-источника
    trader_name: str

    # Цена входа (0.0 – 1.0)
    entry_price: float

    # Вложенная сумма в USDC
    size_usd: float

    # Количество акций/контрактов
    shares: float

    # Время открытия позиции
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Текущая рыночная цена (обновляется при каждой проверке)
    current_price: float = 0.0

    # Нереализованный PnL в USDC
    unrealized_pnl: float = 0.0

    # Статус: open / closed / stop_loss
    status: str = "open"

    # Время закрытия (если закрыта)
    closed_at: Optional[datetime] = None

    # Реализованный PnL при закрытии
    realized_pnl: float = 0.0

    # Описание рынка (для читаемости)
    market_slug: str = ""

    def update_pnl(self, current_price: float):
        """Обновляет текущую цену и рассчитывает нереализованный PnL."""
        self.current_price = current_price
        # PnL = (текущая - входная) / входная * вложено
        if self.entry_price > 0:
            self.unrealized_pnl = (
                (current_price - self.entry_price) / self.entry_price
            ) * self.size_usd

    def is_stop_loss_triggered(self) -> bool:
        """Возвращает True если текущая цена упала ниже стоп-лосса."""
        if self.current_price <= 0:
            return False
        threshold = self.entry_price * config.STOP_LOSS_PERCENT
        return self.current_price < threshold

    def to_dict(self) -> dict:
        """Сериализует позицию в словарь (для дашборда и JSON)."""
        return {
            "order_id": self.order_id,
            "token_id": self.token_id,
            "trader_name": self.trader_name,
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
        }


# ============================================================
# МЕНЕДЖЕР РИСКОВ
# ============================================================

class RiskManager:
    """
    Центральный менеджер рисков.

    Хранит реестр открытых позиций, проверяет каждую сделку
    на соответствие параметрам риска, запускает фоновый поток
    мониторинга стоп-лоссов.
    """

    def __init__(self, on_stop_loss: Optional[Callable] = None):
        """
        Args:
            on_stop_loss: callback(position: OpenPosition) вызывается
                          когда нужно закрыть позицию по стоп-лоссу.
                          Обычно это executor.close_position().
        """
        self._lock = threading.Lock()

        # Реестр открытых позиций: order_id → OpenPosition
        self._positions: dict[str, OpenPosition] = {}

        # История закрытых позиций (для статистики)
        self._closed_positions: list[OpenPosition] = []

        # Callback для закрытия позиций (передаётся из executor.py)
        self._on_stop_loss = on_stop_loss

        # Статистика сессии
        self.total_copied: int = 0
        self.total_skipped: int = 0
        self.session_realized_pnl: float = 0.0

        # Фоновый поток стоп-лосса
        self._stop_event = threading.Event()
        self._sl_thread: Optional[threading.Thread] = None

        # HTTP сессия для получения текущих цен
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ----------------------------------------------------------
    # ФИЛЬТРАЦИЯ СДЕЛОК
    # ----------------------------------------------------------

    def validate_trade(
        self, activity: TradeActivity, market_checker=None
    ) -> tuple[bool, str]:
        """
        Проверяет сделку на соответствие всем параметрам риска.

        Returns:
            (True, "") — сделка прошла фильтрацию
            (False, reason) — сделка отклонена, reason — причина
        """
        # 1. Проверка типа операции
        if not activity.is_valid_buy():
            return False, SkipReason.NOT_BUY.format(activity.action)

        # 2. Наличие token_id
        if not activity.token_id:
            return False, SkipReason.MISSING_TOKEN

        # 3. Диапазон цены входа
        if activity.price < config.MIN_ENTRY_PRICE:
            return False, SkipReason.PRICE_TOO_LOW.format(
                activity.price, config.MIN_ENTRY_PRICE
            )
        if activity.price > config.MAX_ENTRY_PRICE:
            return False, SkipReason.PRICE_TOO_HIGH.format(
                activity.price, config.MAX_ENTRY_PRICE
            )

        # 4. Расчёт размера копируемой сделки
        copy_size = self.calculate_copy_size(activity.size_usd)
        if copy_size < config.MIN_COPY_SIZE_USD:
            return False, SkipReason.SIZE_TOO_SMALL.format(
                copy_size, config.MIN_COPY_SIZE_USD
            )

        # 5. Лимит открытых позиций
        with self._lock:
            open_count = len(self._positions)
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, SkipReason.MAX_POSITIONS.format(
                open_count, config.MAX_OPEN_POSITIONS
            )

        # 6. Проверка активности рынка (если передан market_checker)
        if market_checker is not None:
            if not market_checker.is_market_active(activity.token_id):
                return False, SkipReason.MARKET_INACTIVE.format(
                    activity.token_id[:16]
                )

        return True, ""

    def calculate_copy_size(self, original_size_usd: float) -> float:
        """
        Рассчитывает размер копируемой позиции в USDC:
        min(original * COPY_RATIO, MAX_POSITION_USD)
        """
        raw = original_size_usd * config.COPY_RATIO
        return min(raw, config.MAX_POSITION_USD)

    def calculate_shares(self, size_usd: float, price: float) -> float:
        """Рассчитывает количество контрактов из суммы USD и цены."""
        if price <= 0:
            return 0.0
        return size_usd / price

    # ----------------------------------------------------------
    # УПРАВЛЕНИЕ ПОЗИЦИЯМИ
    # ----------------------------------------------------------

    def register_position(self, position: OpenPosition):
        """Добавляет новую открытую позицию в реестр."""
        with self._lock:
            self._positions[position.order_id] = position
            self.total_copied += 1
        logger.info(
            "✅ Позиция зарегистрирована: %s | трейдер=%s | цена=%.3f | $%.2f",
            position.order_id[:16],
            position.trader_name,
            position.entry_price,
            position.size_usd,
        )

    def close_position(self, order_id: str, realized_pnl: float = 0.0, reason: str = "manual"):
        """Закрывает позицию и переносит её в историю."""
        with self._lock:
            position = self._positions.pop(order_id, None)
        if position is None:
            logger.warning("Попытка закрыть несуществующую позицию: %s", order_id)
            return

        position.status = reason  # "stop_loss" / "manual" / "expired"
        position.closed_at = datetime.now(timezone.utc)
        position.realized_pnl = realized_pnl
        self.session_realized_pnl += realized_pnl

        with self._lock:
            self._closed_positions.append(position)

        logger.info(
            "📤 Позиция закрыта [%s]: %s | PnL=$%.2f",
            reason, order_id[:16], realized_pnl
        )

    def get_open_positions(self) -> list[OpenPosition]:
        """Возвращает список всех открытых позиций (снимок)."""
        with self._lock:
            return list(self._positions.values())

    def get_closed_positions(self) -> list[OpenPosition]:
        """Возвращает историю закрытых позиций."""
        with self._lock:
            return list(self._closed_positions)

    def get_total_exposure(self) -> float:
        """Суммарная вложенная сумма в открытых позициях."""
        with self._lock:
            return sum(p.size_usd for p in self._positions.values())

    def get_total_unrealized_pnl(self) -> float:
        """Суммарный нереализованный PnL по всем открытым позициям."""
        with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values())

    # ----------------------------------------------------------
    # ОБНОВЛЕНИЕ ЦЕН И СТОП-ЛОСС
    # ----------------------------------------------------------

    def _fetch_current_price(self, token_id: str) -> Optional[float]:
        """
        Получает текущую best-ask цену токена через CLOB API.
        Возвращает None при ошибке.
        """
        try:
            url = f"{config.CLOB_HOST}/price?token_id={token_id}&side=sell"
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                price = data.get("price")
                if price is not None:
                    return float(price)
        except Exception as e:
            logger.debug("Не удалось получить цену для %s: %s", token_id[:12], e)
        return None

    def _stop_loss_loop(self):
        """
        Фоновый поток: проверяет стоп-лосс для каждой открытой позиции
        каждые STOP_LOSS_CHECK_INTERVAL_SEC секунд.
        """
        logger.info("🛡️ Поток стоп-лосса запущен (интервал %d сек)", config.STOP_LOSS_CHECK_INTERVAL_SEC)

        while not self._stop_event.is_set():
            positions = self.get_open_positions()

            for pos in positions:
                if self._stop_event.is_set():
                    break

                try:
                    current_price = self._fetch_current_price(pos.token_id)
                    if current_price is None:
                        continue

                    pos.update_pnl(current_price)

                    if pos.is_stop_loss_triggered():
                        logger.warning(
                            "🚨 СТОП-ЛОСС для %s: цена %.3f < порог %.3f (вход %.3f)",
                            pos.order_id[:16],
                            current_price,
                            pos.entry_price * config.STOP_LOSS_PERCENT,
                            pos.entry_price,
                        )
                        # Вызываем callback закрытия (executor.close_position)
                        if self._on_stop_loss:
                            try:
                                self._on_stop_loss(pos)
                            except Exception as cb_err:
                                logger.error(
                                    "Ошибка callback стоп-лосса для %s: %s",
                                    pos.order_id[:16], cb_err
                                )

                except Exception as e:
                    logger.error(
                        "Ошибка проверки стоп-лосса для позиции %s: %s",
                        pos.order_id[:16], e,
                        exc_info=True
                    )

            self._stop_event.wait(timeout=config.STOP_LOSS_CHECK_INTERVAL_SEC)

    def start_stop_loss_monitor(self):
        """Запускает фоновый поток мониторинга стоп-лоссов."""
        self._stop_event.clear()
        self._sl_thread = threading.Thread(
            target=self._stop_loss_loop,
            name="StopLossThread",
            daemon=True,
        )
        self._sl_thread.start()

    def stop_stop_loss_monitor(self):
        """Останавливает фоновый поток стоп-лоссов."""
        self._stop_event.set()
        if self._sl_thread:
            self._sl_thread.join(timeout=10)
        logger.info("Поток стоп-лосса остановлен")

    def set_stop_loss_callback(self, callback: Callable):
        """Задаёт callback для закрытия позиций при срабатывании стоп-лосса."""
        self._on_stop_loss = callback

    # ----------------------------------------------------------
    # СТАТИСТИКА И ОТЧЁТНОСТЬ
    # ----------------------------------------------------------

    def get_session_stats(self) -> dict:
        """Возвращает сводную статистику текущей сессии."""
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
        }

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """Возвращает последние N скопированных сделок (открытые + закрытые)."""
        all_positions = []

        with self._lock:
            all_positions.extend(list(self._positions.values()))
            all_positions.extend(self._closed_positions)

        # Сортируем по времени открытия (новейшие первыми)
        all_positions.sort(key=lambda p: p.opened_at, reverse=True)

        return [p.to_dict() for p in all_positions[:limit]]
