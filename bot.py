"""
bot.py — Главный модуль Polymarket Copy-Trading Bot v2.0.

Изменения v2.0:
- Новый формат Telegram SIGNAL-карточек (HIGH/MEDIUM/IGNORE)
- Callbacks для take-profit, time-stop, trader-exit
- Передача MonitorManager в RiskManager для sell-сигналов
- Статус включает дневные лимиты и trading_halted
"""

import sys
import signal
import json
import logging
import logging.handlers
import threading
import queue
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from monitor import MonitorManager, TradeActivity
from risk_manager import RiskManager, OpenPosition
from executor import OrderExecutor


# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================

def setup_logging():
    """
    Настраивает двойное логирование:
    - В консоль (stdout) с цветным форматом
    - В logs/bot.log с ротацией по размеру
    """
    import os
    os.makedirs("logs", exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Формат сообщений
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Обработчик консоли
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Обработчик файла с ротацией
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)  # В файл пишем всё включая DEBUG

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Подавляем лишние логи от сторонних библиотек
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


# ============================================================
# TELEGRAM УВЕДОМЛЕНИЯ
# ============================================================

class TelegramNotifier:
    """
    Отправляет уведомления в Telegram через Bot API.
    При ошибках — только логирует, не прерывает работу бота.
    """

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._enabled = bool(token and chat_id)
        self._session = requests.Session()
        self._base_url = f"https://api.telegram.org/bot{token}"

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправляет текстовое сообщение. Возвращает True при успехе."""
        if not self._enabled:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logging.getLogger(__name__).warning(
                    "Telegram API ошибка: %s — %s", resp.status_code, resp.text[:200]
                )
                return False
            return True
        except Exception as e:
            logging.getLogger(__name__).warning("Telegram send error: %s", e)
            return False

    def health_check(self) -> tuple[bool, str]:
        """Проверяет доступность Telegram Bot API."""
        if not self._enabled:
            return False, "Telegram не настроен (нет токена или chat_id)"
        try:
            resp = self._session.get(
                f"{self._base_url}/getMe",
                timeout=10,
            )
            if resp.ok:
                bot_info = resp.json().get("result", {})
                return True, f"@{bot_info.get('username', 'unknown')}"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    # ---- Шаблоны сообщений ----

    def notify_signal(
        self,
        position: OpenPosition,
        activity: TradeActivity,
    ):
        """
        📡 SIGNAL карточка — основной формат уведомления v2.0.

        SIGNAL:
        Type:          HIGH / MEDIUM
        Trader:        lebronjames23
        Market:        ...
        Side:          BUY
        Entry Price:   0.3500
        Position Size: $5.00
        Confidence:    85%
        Reason:        confluence: lebronjames23, sayber | цена=0.35
        """
        mode_tag = "🔒 DRY-RUN" if config.DRY_RUN else "💰 РЕАЛ"

        signal_emoji = {"HIGH": "🔥", "MEDIUM": "📊"}.get(position.signal_type, "📋")

        text = (
            f"{signal_emoji} <b>SIGNAL: {position.signal_type}</b> | {mode_tag}\n\n"
            f"<b>Type:</b>          {position.signal_type}\n"
            f"<b>Trader:</b>        {position.trader_name}\n"
            f"<b>Market:</b>        <code>{position.market_slug or position.token_id[:20]}</code>\n"
            f"<b>Side:</b>          BUY\n"
            f"<b>Entry Price:</b>   {position.entry_price:.4f}\n"
            f"<b>Position Size:</b> ${position.size_usd:.2f}\n"
            f"<b>Confidence:</b>    {activity.confidence * 100:.0f}%\n"
            f"<b>Reason:</b>        {activity.signal_reason or '—'}\n\n"
            f"🆔 <code>{position.order_id}</code> | "
            f"🕒 {position.opened_at.strftime('%H:%M UTC')}"
        )
        self.send(text)

    def notify_trade_skipped(self, activity: TradeActivity, reason: str, trader_name: str):
        """⏭ Пропущенная сделка — краткое уведомление."""
        signal_type = getattr(activity, "signal_type", "?")
        text = (
            f"⏭ <b>Пропуск [{signal_type}]</b>\n\n"
            f"👤 <code>{trader_name}</code> | цена={activity.price:.3f}\n"
            f"❓ {reason}"
        )
        self.send(text)

    def notify_trade_error(self, activity: TradeActivity, error: str, trader_name: str):
        """❌ Ошибка исполнения."""
        text = (
            f"❌ <b>Ошибка исполнения</b>\n\n"
            f"👤 <code>{trader_name}</code>\n"
            f"⚠️ {error}"
        )
        self.send(text)

    def notify_stop_loss(self, position: OpenPosition):
        """🚨 Стоп-лосс."""
        text = (
            f"🚨 <b>СТОП-ЛОСС [{position.signal_type}]</b>\n\n"
            f"👤 {position.trader_name} | "
            f"<code>{position.market_slug or position.token_id[:16]}</code>\n"
            f"📉 Вход: {position.entry_price:.4f} → {position.current_price:.4f}\n"
            f"💸 PnL: <b>${position.unrealized_pnl:.2f}</b>\n"
            f"🆔 <code>{position.order_id}</code>"
        )
        self.send(text)

    def notify_take_profit(self, position: OpenPosition, tp_name: str):
        """💰 Тейк-профит."""
        tp_pct = {"tp1": "20%", "tp2": "40%"}.get(tp_name, "?")
        close_pct = {"tp1": "50%", "tp2": "25%"}.get(tp_name, "?")
        text = (
            f"💰 <b>ТЕЙК-ПРОФИТ {tp_name.upper()} (+{tp_pct})</b>\n\n"
            f"👤 {position.trader_name} | "
            f"<code>{position.market_slug or position.token_id[:16]}</code>\n"
            f"📈 Вход: {position.entry_price:.4f} → {position.current_price:.4f}\n"
            f"✂️ Закрыто {close_pct} позиции\n"
            f"🆔 <code>{position.order_id}</code>"
        )
        self.send(text)

    def notify_time_stop(self, position: OpenPosition, reason: str):
        """⏰ Временной стоп."""
        reason_text = {
            "no_movement": f"нет движения {config.TIME_STOP_NO_MOVEMENT_HOURS:.0f}ч",
            "max_hold":    f"макс. удержание {config.MAX_HOLD_HOURS:.0f}ч",
        }.get(reason, reason)
        text = (
            f"⏰ <b>ВРЕМЕННОЙ СТОП</b>\n\n"
            f"👤 {position.trader_name} | "
            f"<code>{position.market_slug or position.token_id[:16]}</code>\n"
            f"⚠️ Причина: {reason_text}\n"
            f"💰 Держали: {position.hours_held():.1f}ч | "
            f"PnL: ${position.unrealized_pnl:.2f}\n"
            f"🆔 <code>{position.order_id}</code>"
        )
        self.send(text)

    def notify_trader_exit(self, position: OpenPosition):
        """🔄 Трейдер-источник продал позицию."""
        text = (
            f"🔄 <b>ТРЕЙДЕР ВЫШЕЛ → закрываем</b>\n\n"
            f"👤 {position.trader_name} продал "
            f"<code>{position.market_slug or position.token_id[:16]}</code>\n"
            f"💰 PnL: ${position.unrealized_pnl:.2f}\n"
            f"🆔 <code>{position.order_id}</code>"
        )
        self.send(text)

    def notify_status(self, stats: dict, monitor_status: dict):
        """📊 Периодический статус-отчёт v2.0 с дневными лимитами."""
        mode_tag = "🔒 DRY-RUN" if config.DRY_RUN else "💰 РЕАЛЬНЫЙ"
        halt_tag = "🛑 ТОРГОВЛЯ ОСТАНОВЛЕНА" if stats.get("trading_halted") else "✅ Активен"
        traders_list = "\n".join(
            f"  • {t['name']} [{t.get('role','?')}]: "
            f"обнаружено={t['total_detected']} пропущено={t.get('total_skipped',0)}"
            for t in monitor_status.get("traders", [])
        )
        text = (
            f"📊 <b>Статус бота</b> | {mode_tag}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"🤖 Статус: {halt_tag}\n\n"
            f"📈 <b>Сессия:</b>\n"
            f"  • Скопировано: {stats['total_copied']} | Пропущено: {stats['total_skipped']}\n"
            f"  • Открыто: {stats['open_positions']} | Закрыто: {stats['closed_positions']}\n\n"
            f"💰 <b>PnL:</b>\n"
            f"  • Нереализованный: ${stats['unrealized_pnl']:.2f}\n"
            f"  • Реализованный:   ${stats['realized_pnl']:.2f}\n"
            f"  • Итого:           <b>${stats['total_pnl']:.2f}</b>\n\n"
            f"🛡️ <b>Дневные лимиты:</b>\n"
            f"  • Потери сегодня: ${abs(stats.get('daily_loss', 0)):.2f} / ${config.DAILY_LOSS_LIMIT_USD:.2f}\n"
            f"  • Серия убытков:  {stats.get('daily_consecutive_losses', 0)} / {config.MAX_CONSECUTIVE_LOSSES}\n\n"
            f"👀 <b>Трейдеры:</b>\n{traders_list}\n\n"
            f"🔄 Опросов: {monitor_status['total_polls']}"
        )
        self.send(text)

    def notify_bot_start(self, wallet_balance: float, warnings: list[str]):
        """🚀 Уведомление о запуске бота."""
        mode_tag = "🔒 DRY-RUN (без реальных сделок)" if config.DRY_RUN else "💰 РЕАЛЬНАЯ ТОРГОВЛЯ"
        traders_list = "\n".join(
            f"  • {t['name']} ({t['address'][:10]}…)"
            for t in config.TRADERS
        )
        warn_text = ""
        if warnings:
            warn_text = "\n\n⚠️ <b>Предупреждения:</b>\n" + "\n".join(
                f"  • {w}" for w in warnings
            )

        text = (
            f"🚀 <b>Polymarket Copy-Bot запущен!</b>\n\n"
            f"🔧 Режим: {mode_tag}\n"
            f"💳 Баланс: ${wallet_balance:.2f} USDC\n\n"
            f"👀 Отслеживаю:\n{traders_list}\n\n"
            f"⚙️ Параметры:\n"
            f"  • MAX_POSITION: ${config.MAX_POSITION_USD}\n"
            f"  • COPY_RATIO: {config.COPY_RATIO * 100:.0f}%\n"
            f"  • STOP_LOSS: {(1 - config.STOP_LOSS_PERCENT) * 100:.0f}%\n"
            f"  • MAX_POSITIONS: {config.MAX_OPEN_POSITIONS}"
            f"{warn_text}"
        )
        self.send(text)

    def notify_bot_stop(self, stats: dict):
        """🛑 Уведомление об остановке бота."""
        text = (
            f"🛑 <b>Бот остановлен</b>\n\n"
            f"📊 Итоги сессии:\n"
            f"  • Скопировано: {stats['total_copied']}\n"
            f"  • PnL: ${stats['total_pnl']:.2f}"
        )
        self.send(text)


# ============================================================
# HEALTH CHECK
# ============================================================

def health_check(notifier: TelegramNotifier, executor: OrderExecutor) -> float:
    """
    Проверяет все компоненты системы при старте.
    Возвращает баланс USDC кошелька (0.0 при ошибке).
    """
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("HEALTH CHECK")
    logger.info("=" * 60)

    all_ok = True

    # 1. Проверка Polymarket Data API
    try:
        resp = requests.get(
            f"{config.DATA_API_HOST}/activity"
            f"?user={config.TRADERS[0]['address']}&limit=1",
            timeout=config.HTTP_TIMEOUT,
        )
        if resp.status_code in (200, 404):
            logger.info("✅ Polymarket Data API: доступен")
        else:
            logger.warning("⚠️ Polymarket Data API: статус %d", resp.status_code)
            all_ok = False
    except Exception as e:
        logger.error("❌ Polymarket Data API недоступен: %s", e)
        all_ok = False

    # 2. Проверка CLOB API
    clob_ok, clob_msg = executor.health_check()
    if clob_ok:
        logger.info("✅ CLOB API: %s", clob_msg)
    else:
        logger.warning("⚠️ CLOB API: %s", clob_msg)

    # 3. Проверка Telegram
    tg_ok, tg_msg = notifier.health_check()
    if tg_ok:
        logger.info("✅ Telegram Bot: %s", tg_msg)
    else:
        logger.warning("⚠️ Telegram: %s", tg_msg)

    # 4. Баланс USDC через web3.py
    wallet_balance = 0.0
    if config.WALLET_ADDRESS:
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC))
            if w3.is_connected():
                # ABI только для функции balanceOf
                usdc_abi = [
                    {
                        "constant": True,
                        "inputs": [{"name": "_owner", "type": "address"}],
                        "name": "balanceOf",
                        "outputs": [{"name": "balance", "type": "uint256"}],
                        "type": "function",
                    },
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "decimals",
                        "outputs": [{"name": "", "type": "uint8"}],
                        "type": "function",
                    },
                ]
                usdc = w3.eth.contract(
                    address=Web3.to_checksum_address(config.USDC_CONTRACT),
                    abi=usdc_abi,
                )
                decimals = usdc.functions.decimals().call()
                raw_balance = usdc.functions.balanceOf(
                    Web3.to_checksum_address(config.WALLET_ADDRESS)
                ).call()
                wallet_balance = raw_balance / (10 ** decimals)
                logger.info(
                    "✅ Polygon RPC: подключён | Баланс USDC: $%.2f", wallet_balance
                )
            else:
                logger.warning("⚠️ Polygon RPC: нет подключения к %s", config.POLYGON_RPC)
        except ImportError:
            logger.warning("⚠️ web3.py не установлен — баланс не проверен")
        except Exception as e:
            logger.warning("⚠️ Ошибка получения баланса: %s", e)
    else:
        logger.warning("⚠️ WALLET_ADDRESS не задан — баланс не проверен")

    # 5. Проверка конфигурации
    config_warnings = config.validate_config()
    for w in config_warnings:
        logger.warning("⚠️ Config: %s", w)

    # Итог
    mode = "DRY-RUN 🔒" if config.DRY_RUN else "РЕАЛЬНАЯ ТОРГОВЛЯ 💰"
    logger.info("=" * 60)
    logger.info("Режим: %s | Баланс: $%.2f USDC", mode, wallet_balance)
    logger.info("Трейдеров для мониторинга: %d", len(config.TRADERS))
    logger.info("=" * 60)

    return wallet_balance


# ============================================================
# ГЛАВНЫЙ КЛАСС БОТА
# ============================================================

class PolymarketCopyBot:
    """
    Главный класс бота. Объединяет все компоненты и управляет их жизненным циклом.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Очередь для передачи новых сделок из монитора в обработчик
        self._trade_queue: queue.Queue = queue.Queue(maxsize=100)

        # Создаём компоненты
        self.notifier = TelegramNotifier(
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
        )

        self.risk_manager = RiskManager()

        self.executor = OrderExecutor(
            risk_manager=self.risk_manager,
            on_trade_executed=self._on_trade_executed,
            on_trade_skipped=self._on_trade_skipped,
            on_trade_failed=self._on_trade_failed,
            on_stop_loss_closed=self._on_stop_loss_closed,
            on_take_profit_closed=self._on_take_profit_closed,
            on_time_stop_closed=self._on_time_stop_closed,
            on_trader_exit_closed=self._on_trader_exit_closed,
        )

        self.monitor_manager = MonitorManager(
            traders=config.TRADERS,
            shared_queue=self._trade_queue,
        )
        # Передаём монитор в risk_manager для отслеживания sell-сигналов
        self.risk_manager._monitor_manager = self.monitor_manager

        # Поток обработки очереди сделок
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Поток периодического статуса
        self._status_thread: Optional[threading.Thread] = None

        # Флаг запуска
        self._running = False

        # ---- SIGNAL_ONLY: реестр активных сигналов ----
        # signal_id → dict с данными сигнала
        self._active_signals: dict[str, dict] = {}
        self._signal_lock = threading.Lock()

    # ----------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------

    def start(self):
        """Запускает все компоненты бота."""
        self.logger.info("🚀 Запуск Polymarket Copy-Trading Bot...")

        # Health check
        wallet_balance = health_check(self.notifier, self.executor)
        config_warnings = config.validate_config()

        # Уведомление о запуске
        self.notifier.notify_bot_start(wallet_balance, config_warnings)

        # Запускаем мониторинг трейдеров
        self.monitor_manager.start()

        # В SIGNAL_ONLY режиме стоп-лосс и executor НЕ запускаются
        if config.MODE != "SIGNAL_ONLY":
            self.risk_manager.start_stop_loss_monitor()
        else:
            self.logger.info("🔕 SIGNAL_ONLY: стоп-лосс и исполнение отключены")

        # Запускаем воркер очереди сделок
        self._stop_event.clear()
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._process_queue,
            name="TradeWorker",
            daemon=True,
        )
        self._worker_thread.start()

        # Запускаем поток периодического статуса
        self._status_thread = threading.Thread(
            target=self._status_loop,
            name="StatusThread",
            daemon=True,
        )
        self._status_thread.start()

        self.logger.info("✅ Бот успешно запущен. Нажмите Ctrl+C для остановки.")

    def stop(self):
        """Корректно останавливает все компоненты."""
        if not self._running:
            return

        self.logger.info("🛑 Остановка бота...")
        self._running = False
        self._stop_event.set()

        # Останавливаем компоненты
        self.monitor_manager.stop()
        if config.MODE != "SIGNAL_ONLY":
            self.risk_manager.stop_stop_loss_monitor()

        # Ждём завершения потоков
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        if self._status_thread:
            self._status_thread.join(timeout=5)

        # Финальная статистика
        stats = self.risk_manager.get_session_stats()
        self.notifier.notify_bot_stop(stats)
        self.logger.info(
            "Итоги сессии: скопировано=%d | PnL=$%.2f",
            stats["total_copied"], stats["total_pnl"]
        )

    # ----------------------------------------------------------
    # ОБРАБОТКА ОЧЕРЕДИ СДЕЛОК
    # ----------------------------------------------------------

    def _process_queue(self):
        """
        Основной воркер: читает новые сделки из очереди
        и запускает валидацию + исполнение.
        """
        self.logger.info("👷 Воркер очереди сделок запущен")

        while not self._stop_event.is_set():
            try:
                # Ждём новую сделку с таймаутом (чтобы проверять _stop_event)
                activity: TradeActivity = self._trade_queue.get(timeout=1.0)
                self._handle_activity(activity)
                self._trade_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(
                    "Критическая ошибка в воркере очереди: %s", e, exc_info=True
                )

    def _handle_activity(self, activity: TradeActivity):
        """
        Роутер: направляет активность в нужный режим обработки.
        SIGNAL_ONLY → _handle_signal_only()
        LIVE        → валидация + исполнение (оригинальная логика)
        """
        if config.MODE == "SIGNAL_ONLY":
            self._handle_signal_only(activity)
            return

        # ---- Оригинальная LIVE логика ----
        trader_name = getattr(activity, "trader_name", "unknown")

        self.logger.debug(
            "Обработка активности: trader=%s id=%s action=%s price=%.4f",
            trader_name, activity.id[:12], activity.action, activity.price
        )

        # Валидация через риск-менеджер
        is_valid, reason = self.risk_manager.validate_trade(
            activity,
            market_checker=self.monitor_manager.market_checker,
        )

        if not is_valid:
            # Логируем пропуск
            self.logger.info(
                "⏭ [%s] Пропуск: %s", trader_name, reason
            )
            self.risk_manager.total_skipped += 1
            # Уведомляем в Telegram о пропуске
            self.notifier.notify_trade_skipped(activity, reason, trader_name)
            return

        # Исполняем ордер
        self.executor.execute_trade(activity)

    # ----------------------------------------------------------
    # SIGNAL_ONLY ЛОГИКА
    # ----------------------------------------------------------

    def _handle_signal_only(self, activity: TradeActivity):
        """
        Обрабатывает сделку в режиме SIGNAL_ONLY.
        Никакого исполнения — только фильтрация и вывод сигнала.

        Фильтры (в порядке проверки):
        1. Трейдер в WHITELIST_TRADERS
        2. Цена в диапазоне MIN_ENTRY_PRICE – MAX_ENTRY_PRICE
        3. Время до резолюции >= MIN_TIME_TO_RESOLUTION_HOURS
        4. Возраст сделки <= 12 часов
        5. Не превышен MAX_SIGNALS
        """
        trader_name = getattr(activity, "trader_name", "unknown")

        # 1. Whitelist
        if trader_name not in config.WHITELIST_TRADERS:
            self.logger.info(
                "IGNORE | trader=%s не в WHITELIST_TRADERS", trader_name
            )
            return

        # 2. Цена
        if activity.price < config.MIN_ENTRY_PRICE:
            self.logger.info(
                "IGNORE | price=%.4f < MIN=%.4f | trader=%s",
                activity.price, config.MIN_ENTRY_PRICE, trader_name,
            )
            return
        if activity.price > config.MAX_ENTRY_PRICE:
            self.logger.info(
                "IGNORE | price=%.4f > MAX=%.4f | trader=%s",
                activity.price, config.MAX_ENTRY_PRICE, trader_name,
            )
            return

        # 3. Время до резолюции
        hours_left: Optional[float] = None
        if activity.token_id:
            hours_left = self.monitor_manager.market_checker.get_hours_to_resolution(
                activity.token_id
            )
        if hours_left is not None and hours_left < config.MIN_TIME_TO_RESOLUTION_HOURS:
            self.logger.info(
                "IGNORE | time_to_resolution=%.1fh < %.0fh | trader=%s",
                hours_left, config.MIN_TIME_TO_RESOLUTION_HOURS, trader_name,
            )
            return

        # 4. Возраст сделки (резервная проверка — monitor уже фильтрует)
        age = activity.age_hours()
        if age > 12.0:
            self.logger.info(
                "IGNORE | сделка слишком старая (%.1fч) | trader=%s", age, trader_name
            )
            return

        # 5. Лимит активных сигналов
        with self._signal_lock:
            if len(self._active_signals) >= config.MAX_SIGNALS:
                self.logger.info(
                    "IGNORE | MAX_SIGNALS=%d достигнут | trader=%s",
                    config.MAX_SIGNALS, trader_name,
                )
                return
            signal_id = activity.id or f"{trader_name}_{int(time.time())}"
            self._active_signals[signal_id] = {
                "market": activity.market_slug or activity.token_id[:20],
                "side": activity.outcome or "YES",
                "price": activity.price,
                "trader": trader_name,
                "token_id": activity.token_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        # Выводим сигнал
        reason = "price in range + whitelisted trader"
        if hours_left is not None:
            reason += f" + {hours_left:.0f}h to resolution"

        self._output_signal(activity, reason, hours_left)

    def _output_signal(
        self,
        activity: TradeActivity,
        reason: str,
        hours_left: Optional[float] = None,
    ):
        """
        Выводит сигнал в двух форматах:
        1. JSON в консоль и лог
        2. Telegram-сообщение
        """
        trader_name = getattr(activity, "trader_name", "unknown")

        signal = {
            "market": activity.market_slug or activity.token_id[:20],
            "side": activity.outcome or "YES",
            "price": round(activity.price, 4),
            "trader": trader_name,
            "decision": "BUY_CANDIDATE",
            "reason": reason,
        }

        # Консоль + лог
        signal_json = json.dumps(signal, ensure_ascii=False, indent=2)
        print(signal_json)
        self.logger.info("BUY_CANDIDATE | %s", json.dumps(signal, ensure_ascii=False))

        # Telegram
        hours_str = f"{hours_left:.0f}h" if hours_left is not None else "unknown"
        text = (
            f"🟢 <b>BUY_CANDIDATE</b>\n\n"
            f"Market: <code>{signal['market']}</code>\n"
            f"Side: {signal['side']}\n"
            f"Price: {signal['price']}\n"
            f"Trader: {trader_name}\n"
            f"Time to resolution: {hours_str}\n"
            f"Reason: {reason}"
        )
        self.notifier.send(text)

    # ----------------------------------------------------------
    # CALLBACKS ОТ EXECUTOR
    # ----------------------------------------------------------

    def _on_trade_executed(self, position: OpenPosition, activity: TradeActivity):
        """✅ Сделка исполнена — отправляем SIGNAL карточку."""
        self.notifier.notify_signal(position, activity)

    def _on_trade_skipped(self, activity: TradeActivity, reason: str):
        """⏭ Сделка пропущена."""
        trader_name = getattr(activity, "trader_name", "unknown")
        self.notifier.notify_trade_skipped(activity, reason, trader_name)

    def _on_trade_failed(self, activity: TradeActivity, error: str):
        """❌ Ошибка исполнения."""
        trader_name = getattr(activity, "trader_name", "unknown")
        self.notifier.notify_trade_error(activity, error, trader_name)

    def _on_stop_loss_closed(self, position: OpenPosition):
        """🚨 Стоп-лосс."""
        self.notifier.notify_stop_loss(position)

    def _on_take_profit_closed(self, position: OpenPosition, tp_name: str):
        """💰 Тейк-профит."""
        self.notifier.notify_take_profit(position, tp_name)

    def _on_time_stop_closed(self, position: OpenPosition, reason: str):
        """⏰ Временной стоп."""
        self.notifier.notify_time_stop(position, reason)

    def _on_trader_exit_closed(self, position: OpenPosition):
        """🔄 Трейдер-источник вышел."""
        self.notifier.notify_trader_exit(position)

    # ----------------------------------------------------------
    # ПЕРИОДИЧЕСКИЙ СТАТУС
    # ----------------------------------------------------------

    def _status_loop(self):
        """Отправляет статус-отчёт каждые STATUS_INTERVAL_HOURS часов."""
        interval_sec = config.STATUS_INTERVAL_HOURS * 3600
        self.logger.info(
            "📊 Статус-поток запущен (каждые %d ч)", config.STATUS_INTERVAL_HOURS
        )

        while not self._stop_event.is_set():
            # Ждём интервал
            self._stop_event.wait(timeout=interval_sec)
            if self._stop_event.is_set():
                break

            try:
                stats = self.risk_manager.get_session_stats()
                monitor_status = self.monitor_manager.get_status()
                self.notifier.notify_status(stats, monitor_status)
                self.logger.info(
                    "📊 Статус отправлен: позиций=%d | PnL=$%.2f",
                    stats["open_positions"], stats["total_pnl"]
                )
            except Exception as e:
                self.logger.error("Ошибка отправки статуса: %s", e)

    # ----------------------------------------------------------
    # ДАННЫЕ ДЛЯ ДАШБОРДА
    # ----------------------------------------------------------

    def get_dashboard_data(self) -> dict:
        """Возвращает все данные для Flask-дашборда."""
        stats = self.risk_manager.get_session_stats()
        monitor_status = self.monitor_manager.get_status()
        open_positions = [p.to_dict() for p in self.risk_manager.get_open_positions()]
        recent_trades = self.risk_manager.get_recent_trades(limit=10)

        return {
            "running": self._running,
            "dry_run": config.DRY_RUN,
            "stats": stats,
            "monitor": monitor_status,
            "open_positions": open_positions,
            "recent_trades": recent_trades,
            "config": {
                "max_position_usd": config.MAX_POSITION_USD,
                "copy_ratio": config.COPY_RATIO,
                "stop_loss_percent": config.STOP_LOSS_PERCENT,
                "max_open_positions": config.MAX_OPEN_POSITIONS,
                "poll_interval_sec": config.POLL_INTERVAL_SEC,
            },
        }


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    """Точка входа. Создаёт и запускает бота."""
    # Настраиваем логирование до всего остального
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Polymarket Copy-Trading Bot v1.0")
    logger.info("=" * 60)

    # Создаём экземпляр бота
    bot = PolymarketCopyBot()

    # Обработчик сигналов завершения (Ctrl+C, SIGTERM)
    def signal_handler(sig, frame):
        logger.info("\nПолучен сигнал завершения...")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Запускаем бота
    bot.start()

    # Главный поток ждёт сигнала завершения
    try:
        while bot._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
