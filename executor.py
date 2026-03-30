"""
executor.py — Исполнение торговых ордеров через py-clob-client.

Отвечает за:
1. Инициализацию и аутентификацию CLOB-клиента Polymarket
2. Размещение рыночных ордеров (BUY)
3. Закрытие позиций через SELL ордер
4. Dry-run режим (логирование без реального исполнения)
5. Обработку ошибок исполнения с уведомлениями
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Callable

import config
from monitor import TradeActivity
from risk_manager import RiskManager, OpenPosition

logger = logging.getLogger(__name__)


# ============================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ИНИЦИАЛИЗАЦИИ CLOB КЛИЕНТА
# ============================================================

def build_clob_client():
    """
    Создаёт и возвращает аутентифицированный ClobClient.
    Импорт вынесен в функцию, чтобы бот мог запуститься
    даже без установленного py-clob-client (dry-run режим).
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=config.CLOB_API_KEY,
            api_secret=config.CLOB_API_SECRET,
            api_passphrase=config.CLOB_API_PASSPHRASE,
        )
        client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.WALLET_PRIVATE_KEY,
            creds=creds,
            signature_type=0,  # EOA подпись
        )
        return client
    except ImportError:
        logger.error(
            "py-clob-client не установлен. "
            "Выполните: pip install py-clob-client"
        )
        return None
    except Exception as e:
        logger.error("Не удалось инициализировать ClobClient: %s", e)
        return None


# ============================================================
# ОСНОВНОЙ ИСПОЛНИТЕЛЬ ОРДЕРОВ
# ============================================================

class OrderExecutor:
    """
    Размещает ордера на Polymarket CLOB.

    В dry-run режиме (config.DRY_RUN=True) только логирует
    намерение разместить ордер, не выполняя реальных транзакций.
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        on_trade_executed: Optional[Callable] = None,
        on_trade_failed: Optional[Callable] = None,
        on_stop_loss_closed: Optional[Callable] = None,
    ):
        """
        Args:
            risk_manager: экземпляр RiskManager для регистрации позиций
            on_trade_executed: callback(position) при успешном исполнении
            on_trade_failed: callback(activity, error) при ошибке
            on_stop_loss_closed: callback(position) при закрытии по стоп-лоссу
        """
        self.risk_manager = risk_manager
        self.on_trade_executed = on_trade_executed
        self.on_trade_failed = on_trade_failed
        self.on_stop_loss_closed = on_stop_loss_closed

        # Инициализируем CLOB клиент (None в dry-run или при ошибке)
        self._client = None
        self._client_initialized = False

        # Счётчик для генерации dry-run ID
        self._dry_run_counter = 0

        # Регистрируем себя как обработчик стоп-лоссов
        self.risk_manager.set_stop_loss_callback(self._handle_stop_loss)

    def _ensure_client(self) -> bool:
        """
        Лениво инициализирует CLOB клиент при первом реальном ордере.
        Возвращает True если клиент готов к работе.
        """
        if self._client_initialized:
            return self._client is not None

        self._client_initialized = True

        if config.DRY_RUN:
            logger.info("🔒 DRY-RUN режим: реальные ордера отключены")
            return False

        if not config.WALLET_PRIVATE_KEY or not config.CLOB_API_KEY:
            logger.error(
                "Не заданы WALLET_PRIVATE_KEY или CLOB_API_KEY — "
                "реальная торговля невозможна"
            )
            return False

        self._client = build_clob_client()
        if self._client:
            logger.info("✅ CLOB клиент инициализирован")
        return self._client is not None

    # ----------------------------------------------------------
    # ИСПОЛНЕНИЕ СДЕЛКИ
    # ----------------------------------------------------------

    def execute_trade(
        self,
        activity: TradeActivity,
        skip_reason: str = "",
    ) -> Optional[OpenPosition]:
        """
        Основной метод исполнения: проверяет риски и размещает ордер.

        Args:
            activity: торговая активность из monitor.py
            skip_reason: если задан, сделка уже отфильтрована — пропускаем

        Returns:
            OpenPosition если ордер размещён, None — если пропущен/ошибка
        """
        trader_name = getattr(activity, "trader_name", "unknown")

        # Если причина пропуска уже задана снаружи
        if skip_reason:
            logger.info(
                "⏭ [%s] Пропуск сделки %s: %s",
                trader_name, activity.id[:12], skip_reason
            )
            self.risk_manager.total_skipped += 1
            if self.on_trade_failed:
                self.on_trade_failed(activity, skip_reason)
            return None

        # Рассчитываем параметры ордера
        copy_size_usd = self.risk_manager.calculate_copy_size(activity.size_usd)
        shares = self.risk_manager.calculate_shares(copy_size_usd, activity.price)

        logger.info(
            "📋 [%s] Подготовка ордера: token=%s | цена=%.4f | "
            "оригинал=$%.2f | копия=$%.2f | акций=%.4f",
            trader_name,
            activity.token_id[:16],
            activity.price,
            activity.size_usd,
            copy_size_usd,
            shares,
        )

        # Dry-run режим
        if config.DRY_RUN:
            return self._execute_dry_run(activity, copy_size_usd, shares, trader_name)

        # Реальное исполнение
        return self._execute_real(activity, copy_size_usd, shares, trader_name)

    def _execute_dry_run(
        self,
        activity: TradeActivity,
        copy_size_usd: float,
        shares: float,
        trader_name: str,
    ) -> OpenPosition:
        """Симулирует исполнение ордера в dry-run режиме."""
        self._dry_run_counter += 1
        fake_order_id = f"dry_run_{self._dry_run_counter:04d}_{int(time.time())}"

        logger.info(
            "🔒 [DRY-RUN] Симуляция BUY: %s | token=%s | "
            "цена=%.4f | $%.2f | %.4f акций",
            fake_order_id,
            activity.token_id[:16],
            activity.price,
            copy_size_usd,
            shares,
        )

        position = OpenPosition(
            order_id=fake_order_id,
            token_id=activity.token_id,
            trader_name=trader_name,
            entry_price=activity.price,
            size_usd=copy_size_usd,
            shares=shares,
            market_slug=activity.market_slug,
        )

        self.risk_manager.register_position(position)

        if self.on_trade_executed:
            self.on_trade_executed(position)

        return position

    def _execute_real(
        self,
        activity: TradeActivity,
        copy_size_usd: float,
        shares: float,
        trader_name: str,
    ) -> Optional[OpenPosition]:
        """Размещает реальный ордер через py-clob-client."""
        if not self._ensure_client():
            error_msg = "CLOB клиент недоступен"
            logger.error("❌ [%s] %s", trader_name, error_msg)
            self.risk_manager.total_skipped += 1
            if self.on_trade_failed:
                self.on_trade_failed(activity, error_msg)
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Формируем параметры ордера
            order_args = OrderArgs(
                token_id=activity.token_id,
                price=round(activity.price, 4),
                size=round(shares, 4),
                side=BUY,
            )

            # Используем MARKET ордер для надёжного исполнения
            # FOK (Fill or Kill) — немедленно исполняется или отменяется
            signed_order = self._client.create_order(order_args)
            response = self._client.post_order(
                signed_order,
                OrderType.FOK,
            )

            logger.debug("Ответ CLOB: %s", response)

            # Проверяем успешность
            order_id = None
            if isinstance(response, dict):
                order_id = (
                    response.get("orderID")
                    or response.get("order_id")
                    or response.get("id")
                )
                status = response.get("status", "unknown")
                if status in ("matched", "filled", "success"):
                    pass  # OK
                elif status == "unmatched":
                    logger.warning(
                        "[%s] Ордер не исполнен (unmatched): %s",
                        trader_name, order_id
                    )
            elif hasattr(response, "order_id"):
                order_id = response.order_id

            if not order_id:
                order_id = f"real_{int(time.time())}"

            logger.info(
                "✅ [%s] Ордер исполнен: %s | цена=%.4f | $%.2f",
                trader_name, order_id, activity.price, copy_size_usd
            )

            position = OpenPosition(
                order_id=order_id,
                token_id=activity.token_id,
                trader_name=trader_name,
                entry_price=activity.price,
                size_usd=copy_size_usd,
                shares=shares,
                market_slug=activity.market_slug,
            )

            self.risk_manager.register_position(position)

            if self.on_trade_executed:
                self.on_trade_executed(position)

            return position

        except Exception as e:
            error_msg = str(e)
            logger.error(
                "❌ [%s] Ошибка исполнения ордера: %s",
                trader_name, error_msg,
                exc_info=True
            )
            self.risk_manager.total_skipped += 1
            if self.on_trade_failed:
                self.on_trade_failed(activity, error_msg)
            return None

    # ----------------------------------------------------------
    # ЗАКРЫТИЕ ПОЗИЦИИ (СТОП-ЛОСС / РУЧНОЕ)
    # ----------------------------------------------------------

    def close_position(
        self, position: OpenPosition, reason: str = "manual"
    ) -> bool:
        """
        Закрывает открытую позицию SELL ордером.

        Args:
            position: позиция для закрытия
            reason: причина закрытия ("stop_loss" / "manual")

        Returns:
            True если позиция успешно закрыта
        """
        logger.info(
            "📤 Закрытие позиции [%s]: %s | текущая цена=%.4f | вход=%.4f",
            reason, position.order_id[:16],
            position.current_price, position.entry_price
        )

        if config.DRY_RUN:
            # В dry-run просто регистрируем закрытие
            realized_pnl = position.unrealized_pnl
            self.risk_manager.close_position(
                position.order_id, realized_pnl, reason
            )
            logger.info(
                "🔒 [DRY-RUN] Позиция закрыта: %s | PnL=$%.2f",
                position.order_id[:16], realized_pnl
            )
            return True

        # Реальное закрытие
        if not self._ensure_client():
            logger.error("Не могу закрыть позицию — клиент недоступен")
            return False

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            # Продаём столько акций, сколько купили
            order_args = OrderArgs(
                token_id=position.token_id,
                price=round(position.current_price, 4),
                size=round(position.shares, 4),
                side=SELL,
            )

            signed_order = self._client.create_order(order_args)
            response = self._client.post_order(signed_order, OrderType.FOK)

            # Рассчитываем реализованный PnL
            sell_proceeds = position.shares * position.current_price
            realized_pnl = sell_proceeds - position.size_usd

            self.risk_manager.close_position(position.order_id, realized_pnl, reason)

            logger.info(
                "✅ Позиция закрыта [%s]: PnL=$%.2f | response=%s",
                reason, realized_pnl, str(response)[:80]
            )
            return True

        except Exception as e:
            logger.error(
                "❌ Ошибка закрытия позиции %s: %s",
                position.order_id[:16], e,
                exc_info=True
            )
            return False

    def _handle_stop_loss(self, position: OpenPosition):
        """
        Callback вызывается из RiskManager когда срабатывает стоп-лосс.
        """
        success = self.close_position(position, reason="stop_loss")

        if success and self.on_stop_loss_closed:
            self.on_stop_loss_closed(position)

    # ----------------------------------------------------------
    # ПРОВЕРКА ПОДКЛЮЧЕНИЯ
    # ----------------------------------------------------------

    def health_check(self) -> tuple[bool, str]:
        """
        Проверяет доступность CLOB API и корректность ключей.

        Returns:
            (True, "OK") или (False, "описание ошибки")
        """
        if config.DRY_RUN:
            return True, "DRY-RUN режим — проверка API пропущена"

        if not config.CLOB_API_KEY:
            return False, "CLOB_API_KEY не задан"

        try:
            import requests as req
            resp = req.get(
                f"{config.CLOB_HOST}/time",
                timeout=config.HTTP_TIMEOUT
            )
            if resp.status_code == 200:
                return True, f"CLOB API доступен (время: {resp.json()})"
            return False, f"CLOB API вернул статус {resp.status_code}"
        except Exception as e:
            return False, f"CLOB API недоступен: {e}"
