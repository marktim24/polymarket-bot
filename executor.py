"""
executor.py — Исполнение ордеров v2.0.

Изменения:
- Размер позиции определяется signal_type (HIGH=$5, MEDIUM=$3, BASE=$2)
- Поддержка частичного закрытия (take-profit)
- Новый формат Telegram-уведомлений (SIGNAL карточка)
- Callbacks для всех типов выходов: stop-loss, take-profit, time-stop, trader-exit
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Callable

import config
from monitor import TradeActivity
from risk_manager import RiskManager, OpenPosition

logger = logging.getLogger(__name__)


def build_clob_client():
    """Создаёт аутентифицированный ClobClient."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=config.CLOB_API_KEY,
            api_secret=config.CLOB_API_SECRET,
            api_passphrase=config.CLOB_API_PASSPHRASE,
        )
        return ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.WALLET_PRIVATE_KEY,
            creds=creds,
            signature_type=0,
        )
    except ImportError:
        logger.error("py-clob-client не установлен")
        return None
    except Exception as e:
        logger.error("Не удалось инициализировать ClobClient: %s", e)
        return None


class OrderExecutor:
    """
    Исполнитель ордеров v2.0.

    Поддерживает:
    - Размер позиции по сигналу
    - Полное и частичное закрытие (TP)
    - Все типы выходов с уведомлениями
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        on_trade_executed: Optional[Callable] = None,
        on_trade_skipped: Optional[Callable] = None,
        on_trade_failed: Optional[Callable] = None,
        on_stop_loss_closed: Optional[Callable] = None,
        on_take_profit_closed: Optional[Callable] = None,
        on_time_stop_closed: Optional[Callable] = None,
        on_trader_exit_closed: Optional[Callable] = None,
    ):
        self.risk_manager = risk_manager
        self.on_trade_executed = on_trade_executed
        self.on_trade_skipped = on_trade_skipped
        self.on_trade_failed = on_trade_failed
        self.on_stop_loss_closed = on_stop_loss_closed
        self.on_take_profit_closed = on_take_profit_closed
        self.on_time_stop_closed = on_time_stop_closed
        self.on_trader_exit_closed = on_trader_exit_closed

        self._client = None
        self._client_initialized = False
        self._dry_run_counter = 0

        # Регистрируем все callbacks в risk_manager
        self.risk_manager._on_stop_loss = self._handle_stop_loss
        self.risk_manager._on_take_profit = self._handle_take_profit
        self.risk_manager._on_time_stop = self._handle_time_stop
        self.risk_manager._on_trader_exit = self._handle_trader_exit

    def _ensure_client(self) -> bool:
        if self._client_initialized:
            return self._client is not None
        self._client_initialized = True
        if config.DRY_RUN:
            return False
        if not config.WALLET_PRIVATE_KEY or not config.CLOB_API_KEY:
            logger.error("Нет ключей для реальной торговли")
            return False
        self._client = build_clob_client()
        return self._client is not None

    # ----------------------------------------------------------
    # ИСПОЛНЕНИЕ СДЕЛКИ
    # ----------------------------------------------------------

    def execute_trade(
        self, activity: TradeActivity, skip_reason: str = ""
    ) -> Optional[OpenPosition]:
        """Основной метод: валидирует и исполняет сделку."""
        trader_name = getattr(activity, "trader_name", "unknown")

        if skip_reason:
            logger.info("⏭ [%s] Пропуск: %s", trader_name, skip_reason)
            self.risk_manager.total_skipped += 1
            if self.on_trade_skipped:
                self.on_trade_skipped(activity, skip_reason)
            return None

        # Размер определяется signal_type
        position_size_usd = self.risk_manager.calculate_position_size(
            activity.signal_type
        )
        shares = self.risk_manager.calculate_shares(position_size_usd, activity.price)

        logger.info(
            "📋 [%s][%s] Ордер: token=%s | цена=%.4f | $%.2f | %.4f акций | conf=%.0f%%",
            trader_name, activity.signal_type,
            activity.token_id[:16], activity.price,
            position_size_usd, shares, activity.confidence * 100,
        )

        if config.DRY_RUN:
            return self._execute_dry_run(activity, position_size_usd, shares, trader_name)
        return self._execute_real(activity, position_size_usd, shares, trader_name)

    def _execute_dry_run(
        self, activity: TradeActivity, size_usd: float, shares: float, trader_name: str
    ) -> OpenPosition:
        self._dry_run_counter += 1
        order_id = f"dry_run_{self._dry_run_counter:04d}_{int(time.time())}"

        logger.info(
            "🔒 [DRY-RUN][%s] BUY %s | цена=%.4f | $%.2f | %.4f акций",
            activity.signal_type, order_id, activity.price, size_usd, shares,
        )

        position = OpenPosition(
            order_id=order_id,
            token_id=activity.token_id,
            trader_name=trader_name,
            entry_price=activity.price,
            size_usd=size_usd,
            shares=shares,
            signal_type=activity.signal_type,
            market_slug=activity.market_slug,
        )
        position.last_price_for_movement = activity.price

        self.risk_manager.register_position(position)
        if self.on_trade_executed:
            self.on_trade_executed(position, activity)
        return position

    def _execute_real(
        self, activity: TradeActivity, size_usd: float, shares: float, trader_name: str
    ) -> Optional[OpenPosition]:
        if not self._ensure_client():
            err = "CLOB клиент недоступен"
            if self.on_trade_failed:
                self.on_trade_failed(activity, err)
            return None
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = OrderArgs(
                token_id=activity.token_id,
                price=round(activity.price, 4),
                size=round(shares, 4),
                side=BUY,
            )
            signed = self._client.create_order(order_args)
            response = self._client.post_order(signed, OrderType.FOK)

            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID") or response.get("order_id")
            if not order_id:
                order_id = f"real_{int(time.time())}"

            logger.info("✅ [%s] Ордер исполнен: %s | $%.2f", trader_name, order_id, size_usd)

            position = OpenPosition(
                order_id=order_id,
                token_id=activity.token_id,
                trader_name=trader_name,
                entry_price=activity.price,
                size_usd=size_usd,
                shares=shares,
                signal_type=activity.signal_type,
                market_slug=activity.market_slug,
            )
            position.last_price_for_movement = activity.price

            self.risk_manager.register_position(position)
            if self.on_trade_executed:
                self.on_trade_executed(position, activity)
            return position

        except Exception as e:
            logger.error("❌ [%s] Ошибка ордера: %s", trader_name, e, exc_info=True)
            if self.on_trade_failed:
                self.on_trade_failed(activity, str(e))
            return None

    # ----------------------------------------------------------
    # ЗАКРЫТИЕ ПОЗИЦИЙ
    # ----------------------------------------------------------

    def close_position(self, position: OpenPosition, reason: str = "manual") -> bool:
        """Полное закрытие позиции."""
        if config.DRY_RUN:
            realized_pnl = position.unrealized_pnl
            self.risk_manager.close_position(position.order_id, realized_pnl, reason)
            logger.info("🔒 [DRY-RUN] Закрыто [%s]: %s | PnL=$%.2f", reason, position.order_id[:16], realized_pnl)
            return True

        if not self._ensure_client():
            return False
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            order_args = OrderArgs(
                token_id=position.token_id,
                price=round(max(position.current_price, 0.01), 4),
                size=round(position.shares, 4),
                side=SELL,
            )
            signed = self._client.create_order(order_args)
            self._client.post_order(signed, OrderType.FOK)

            sell_proceeds = position.shares * position.current_price
            realized_pnl = sell_proceeds - position.size_usd
            self.risk_manager.close_position(position.order_id, realized_pnl, reason)
            return True
        except Exception as e:
            logger.error("❌ Ошибка закрытия %s: %s", position.order_id[:16], e)
            return False

    def partial_close_position(
        self, position: OpenPosition, close_ratio: float, reason: str
    ) -> bool:
        """Частичное закрытие (для тейк-профитов)."""
        close_usd = self.risk_manager.partial_close_position(
            position.order_id, close_ratio, reason
        )
        if close_usd <= 0:
            return False

        close_shares = position.shares * close_ratio

        if config.DRY_RUN:
            realized_pnl = close_shares * (position.current_price - position.entry_price)
            self.risk_manager.session_realized_pnl += realized_pnl
            logger.info(
                "🔒 [DRY-RUN] Частичное закрытие [%s]: %.0f%% | PnL=$%.2f",
                reason, close_ratio * 100, realized_pnl,
            )
            return True

        if not self._ensure_client():
            return False
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL
            order_args = OrderArgs(
                token_id=position.token_id,
                price=round(max(position.current_price, 0.01), 4),
                size=round(close_shares, 4),
                side=SELL,
            )
            signed = self._client.create_order(order_args)
            self._client.post_order(signed, OrderType.FOK)
            return True
        except Exception as e:
            logger.error("❌ Ошибка частичного закрытия %s: %s", position.order_id[:16], e)
            return False

    # ----------------------------------------------------------
    # CALLBACKS ОТ RISK MANAGER
    # ----------------------------------------------------------

    def _handle_stop_loss(self, position: OpenPosition):
        self.close_position(position, reason="stop_loss")
        if self.on_stop_loss_closed:
            self.on_stop_loss_closed(position)

    def _handle_take_profit(self, position: OpenPosition, ratio: float, tp_name: str):
        self.partial_close_position(position, ratio, reason=tp_name)
        if self.on_take_profit_closed:
            self.on_take_profit_closed(position, tp_name)

    def _handle_time_stop(self, position: OpenPosition, reason: str):
        self.close_position(position, reason=f"time_stop_{reason}")
        if self.on_time_stop_closed:
            self.on_time_stop_closed(position, reason)

    def _handle_trader_exit(self, position: OpenPosition):
        self.close_position(position, reason="trader_exit")
        if self.on_trader_exit_closed:
            self.on_trader_exit_closed(position)

    # ----------------------------------------------------------
    # HEALTH CHECK
    # ----------------------------------------------------------

    def health_check(self) -> tuple[bool, str]:
        if config.DRY_RUN:
            return True, "DRY-RUN режим — проверка API пропущена"
        if not config.CLOB_API_KEY:
            return False, "CLOB_API_KEY не задан"
        try:
            import requests as req
            resp = req.get(f"{config.CLOB_HOST}/time", timeout=config.HTTP_TIMEOUT)
            if resp.status_code == 200:
                return True, f"CLOB API доступен"
            return False, f"CLOB вернул {resp.status_code}"
        except Exception as e:
            return False, f"CLOB недоступен: {e}"
