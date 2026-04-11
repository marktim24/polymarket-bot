"""
discovery.py — Автоматическое обнаружение и оценка кошельков-кандидатов.

Архитектура:
  1. CandidateWallet  — данные одного кандидата
  2. CandidatePool    — список кандидатов, персистентность, логика продвижения
  3. CandidateMonitor — мониторинг кандидатов (dry-run симуляция)

Жизненный цикл кандидата:
  "evaluating" → (критерии выполнены) → "promoted"
                → (срок истёк или отклонён) → "rejected"

Файл состояния: logs/candidate_wallets.json
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


# ============================================================
# МОДЕЛЬ КАНДИДАТА
# ============================================================

@dataclass
class SimulatedTrade:
    """Одна симулированная сделка кандидата."""
    token_id: str
    entry_price: float
    entry_usd: float
    entry_ts: float          # unix timestamp
    exit_price: float = 0.0
    exit_ts: float = 0.0
    realized_pnl: float = 0.0
    status: str = "open"     # "open" | "closed"

    def close(self, exit_price: float):
        if self.entry_price > 0:
            self.realized_pnl = (exit_price - self.entry_price) / self.entry_price * self.entry_usd
        self.exit_price = exit_price
        self.exit_ts = time.time()
        self.status = "closed"

    def is_win(self) -> bool:
        return self.realized_pnl > 0


@dataclass
class CandidateWallet:
    """Кандидат на добавление в постоянный список трейдеров."""

    address: str
    name: str
    discovered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "scanner"   # "scanner" | "manual"

    # --- Статистика оценки ---
    trades_seen: int = 0         # всего замечено BUY-транзакций
    simulated_trades: list = field(default_factory=list)  # list[dict]

    # --- Итоговые метрики ---
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0

    # --- Статус ---
    status: str = "evaluating"   # "evaluating" | "promoted" | "rejected"
    promoted_at: Optional[str] = None
    rejection_reason: str = ""

    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return self.wins / closed if closed > 0 else 0.0

    def days_since_discovery(self) -> float:
        try:
            dt = datetime.fromisoformat(self.discovered_at.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        except Exception:
            return 0.0

    def closed_trades(self) -> int:
        return self.wins + self.losses

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateWallet":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def check_promotion(self) -> tuple[bool, str]:
        """
        Проверяет критерии продвижения.

        Returns:
            (should_promote, reason)
        """
        closed = self.closed_trades()
        if closed < config.DISCOVERY_MIN_TRADES:
            return False, f"мало сделок: {closed}/{config.DISCOVERY_MIN_TRADES}"

        if self.realized_pnl <= config.DISCOVERY_MIN_PNL_USD:
            return False, f"PnL {self.realized_pnl:.2f} ≤ {config.DISCOVERY_MIN_PNL_USD:.2f}"

        wr = self.win_rate()
        if wr < config.DISCOVERY_MIN_WIN_RATE:
            return False, f"win_rate {wr:.0%} < {config.DISCOVERY_MIN_WIN_RATE:.0%}"

        return True, (
            f"closed={closed} win_rate={wr:.0%} pnl={self.realized_pnl:.2f}"
        )

    def check_rejection(self) -> tuple[bool, str]:
        """Проверяет, нужно ли отклонить кандидата (срок истёк)."""
        if self.days_since_discovery() > config.DISCOVERY_MAX_EVAL_DAYS:
            return True, f"срок оценки истёк ({config.DISCOVERY_MAX_EVAL_DAYS} дней)"
        return False, ""


# ============================================================
# ПУЛ КАНДИДАТОВ
# ============================================================

class CandidatePool:
    """
    Хранит кандидатов, проверяет критерии продвижения,
    сохраняет состояние в JSON-файл.
    """

    def __init__(self, state_file: str = ""):
        self._state_file = state_file or config.DISCOVERY_STATE_FILE
        self._lock = threading.Lock()
        self._candidates: dict[str, CandidateWallet] = {}
        os.makedirs("logs", exist_ok=True)
        self._load()

    # ----------------------------------------------------------
    # ПЕРСИСТЕНТНОСТЬ
    # ----------------------------------------------------------

    def _load(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("candidates", []):
                try:
                    c = CandidateWallet.from_dict(item)
                    self._candidates[c.address.lower()] = c
                except Exception as e:
                    logger.warning("Не удалось загрузить кандидата: %s", e)
            logger.info(
                "💡 Загружено %d кандидатов из %s",
                len(self._candidates), self._state_file,
            )
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Ошибка загрузки состояния discovery: %s", e)

    def _save(self):
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"candidates": [c.to_dict() for c in self._candidates.values()]},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning("Ошибка сохранения состояния discovery: %s", e)

    # ----------------------------------------------------------
    # УПРАВЛЕНИЕ КАНДИДАТАМИ
    # ----------------------------------------------------------

    def add_candidate(self, address: str, name: str = "", source: str = "scanner") -> CandidateWallet:
        """
        Добавляет нового кандидата в пул. Если уже существует — возвращает существующего.
        Адрес нормализуется в нижний регистр.
        """
        key = address.lower()

        # Не добавляем если уже в постоянном списке
        permanent_addresses = {t["address"].lower() for t in config.TRADERS}
        if key in permanent_addresses:
            raise ValueError(f"Адрес {address} уже в постоянном списке трейдеров")

        with self._lock:
            if key in self._candidates:
                return self._candidates[key]

            candidate = CandidateWallet(
                address=address,
                name=name or f"candidate_{key[:8]}",
                source=source,
            )
            self._candidates[key] = candidate
            self._save()
            logger.info(
                "💡 Новый кандидат: %s (%s) [источник: %s]",
                candidate.name, address[:12], source,
            )
            return candidate

    def record_trade(
        self,
        address: str,
        token_id: str,
        entry_price: float,
        entry_usd: float,
    ) -> Optional[SimulatedTrade]:
        """Записывает новую симулированную сделку для кандидата."""
        key = address.lower()
        with self._lock:
            c = self._candidates.get(key)
            if c is None or c.status != "evaluating":
                return None

            trade = SimulatedTrade(
                token_id=token_id,
                entry_price=entry_price,
                entry_usd=entry_usd,
                entry_ts=time.time(),
            )
            c.trades_seen += 1
            c.simulated_trades.append(asdict(trade))
            self._save()
            logger.info(
                "💡 [%s] Симулированная сделка #%d: token=%s price=%.4f $%.2f",
                c.name, c.trades_seen, token_id[:16], entry_price, entry_usd,
            )
            return trade

    def close_trade(
        self,
        address: str,
        token_id: str,
        exit_price: float,
    ):
        """Закрывает открытую симулированную сделку для кандидата."""
        key = address.lower()
        with self._lock:
            c = self._candidates.get(key)
            if c is None:
                return

            # Ищем последнюю открытую сделку по token_id
            for trade_dict in reversed(c.simulated_trades):
                if trade_dict.get("token_id") == token_id and trade_dict.get("status") == "open":
                    trade = SimulatedTrade(**trade_dict)
                    trade.close(exit_price)
                    # Обновляем dict
                    trade_dict.update(asdict(trade))
                    # Обновляем статистику
                    c.realized_pnl += trade.realized_pnl
                    if trade.is_win():
                        c.wins += 1
                    else:
                        c.losses += 1
                    logger.info(
                        "💡 [%s] Закрыта сделка: token=%s pnl=%.2f | "
                        "wins=%d losses=%d total_pnl=%.2f",
                        c.name, token_id[:16], trade.realized_pnl,
                        c.wins, c.losses, c.realized_pnl,
                    )
                    break

            self._save()
            self._check_and_promote(c)

    def _check_and_promote(self, c: CandidateWallet):
        """Проверяет и применяет продвижение или отклонение (вызывается под блокировкой)."""
        if c.status != "evaluating":
            return

        # Проверяем отклонение
        reject, reason = c.check_rejection()
        if reject:
            c.status = "rejected"
            c.rejection_reason = reason
            self._save()
            logger.info("❌ Кандидат %s отклонён: %s", c.name, reason)
            return

        # Проверяем продвижение
        promote, reason = c.check_promotion()
        if promote:
            c.status = "promoted"
            c.promoted_at = datetime.now(timezone.utc).isoformat()
            self._save()
            logger.info(
                "✅ Кандидат %s ПРОДВИНУТ в постоянный список! %s", c.name, reason
            )
            self._add_to_permanent(c)

    def _add_to_permanent(self, c: CandidateWallet):
        """Добавляет продвинутого кандидата в config.TRADERS в памяти."""
        new_trader = {
            "name": c.name,
            "address": c.address,
            "role": "COPY",
            "strategy": f"auto-discovered | wr={c.win_rate():.0%} pnl={c.realized_pnl:.2f}",
            "win_rate": c.win_rate(),
            "sharpe": 0.0,
            "entry_range": (0.05, 0.95),
            "overrides": {
                "MIN_ENTRY_PRICE": 0.05,
                "MAX_ENTRY_PRICE": 0.95,
                "MIN_TRADER_SIZE_USD": 1.0,
                "MAX_COPY_DELAY_HOURS": 2.0,
                "MAX_PRICE_RATIO_VS_ENTRY": 3.0,
                "MIN_MARKET_VOLUME_USD": 500.0,
                "SKIP_CRYPTO_MICRO": False,
                "SKIP_CATEGORIES": [],
                "HIGH_POSITION_USD": 2.0,
                "MEDIUM_POSITION_USD": 2.0,
                "BASE_POSITION_USD": 1.0,
                "STOP_LOSS_PERCENT": 0.70,
                "TAKE_PROFIT_1_PCT": 0.30,
                "TAKE_PROFIT_2_PCT": 0.70,
                "TAKE_PROFIT_1_CLOSE_RATIO": 0.50,
                "TAKE_PROFIT_2_CLOSE_RATIO": 0.25,
                "TIME_STOP_NO_MOVEMENT_HOURS": 48.0,
                "MAX_HOLD_HOURS": 120.0,
                "MIN_TRADER_EXIT_SIZE_USD": 1.0,
            },
        }
        # Проверяем, нет ли уже этого адреса
        existing = {t["address"].lower() for t in config.TRADERS}
        if c.address.lower() not in existing:
            config.TRADERS.append(new_trader)
            config.TRADER_ROLES[c.name] = "COPY"
            config._TRADER_BY_NAME[c.name] = new_trader
            logger.info(
                "🚀 Трейдер %s добавлен в активный список копирования.", c.name
            )

    # ----------------------------------------------------------
    # ЗАПРОСЫ СОСТОЯНИЯ
    # ----------------------------------------------------------

    def get_evaluating(self) -> list[CandidateWallet]:
        """Возвращает кандидатов в стадии оценки."""
        with self._lock:
            return [c for c in self._candidates.values() if c.status == "evaluating"]

    def get_all(self) -> list[CandidateWallet]:
        with self._lock:
            return list(self._candidates.values())

    def get_by_address(self, address: str) -> Optional[CandidateWallet]:
        with self._lock:
            return self._candidates.get(address.lower())

    def run_periodic_check(self):
        """Периодически проверяет истечение срока оценки кандидатов."""
        with self._lock:
            for c in list(self._candidates.values()):
                if c.status == "evaluating":
                    reject, reason = c.check_rejection()
                    if reject:
                        c.status = "rejected"
                        c.rejection_reason = reason
                        logger.info("❌ Кандидат %s отклонён: %s", c.name, reason)
            self._save()

    def get_summary(self) -> dict:
        """Краткая сводка для дашборда/логов."""
        with self._lock:
            evaluating = sum(1 for c in self._candidates.values() if c.status == "evaluating")
            promoted = sum(1 for c in self._candidates.values() if c.status == "promoted")
            rejected = sum(1 for c in self._candidates.values() if c.status == "rejected")
        return {
            "total": len(self._candidates),
            "evaluating": evaluating,
            "promoted": promoted,
            "rejected": rejected,
        }


# ============================================================
# МОНИТОР КАНДИДАТОВ
# ============================================================

class CandidateMonitor:
    """
    Опрашивает активность кандидатов и передаёт данные в CandidatePool.

    Не использует сигнальную классификацию — просто симулирует все BUY
    в диапазоне 0.01–0.95 для честной оценки.
    """

    def __init__(self, pool: CandidatePool):
        self._pool = pool
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._seen_ids: dict[str, set[str]] = {}  # address → seen tx hashes
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="CandidateMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("🔎 Монитор кандидатов запущен")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run_loop(self):
        while not self._stop_event.is_set():
            candidates = self._pool.get_evaluating()
            for c in candidates:
                if self._stop_event.is_set():
                    break
                try:
                    self._poll_candidate(c)
                except Exception as e:
                    logger.error(
                        "[candidate/%s] Ошибка poll: %s", c.name, e, exc_info=True
                    )
            # Периодически проверяем истечение срока
            self._pool.run_periodic_check()
            self._stop_event.wait(timeout=config.POLL_INTERVAL_SEC)

    def _poll_candidate(self, c: CandidateWallet):
        """Опрашивает активность одного кандидата."""
        url = (
            f"{config.DATA_API_HOST}/activity"
            f"?user={c.address}&limit={config.ACTIVITY_FETCH_LIMIT}"
        )
        try:
            resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            raw_activities = resp.json()
            if not isinstance(raw_activities, list):
                return
        except Exception as e:
            logger.debug("[candidate/%s] Ошибка запроса: %s", c.name, e)
            return

        key = c.address.lower()
        if key not in self._seen_ids:
            # Инициализация: запоминаем текущие ID, не обрабатываем
            self._seen_ids[key] = {
                a.get("transactionHash") or a.get("id") or ""
                for a in raw_activities
                if (a.get("transactionHash") or a.get("id"))
            }
            return

        seen = self._seen_ids[key]
        for raw in raw_activities:
            tx = raw.get("transactionHash") or raw.get("id") or ""
            if not tx or tx in seen:
                continue
            seen.add(tx)

            side = (raw.get("side") or "").upper()
            price = float(raw.get("price") or 0)
            usd = float(raw.get("usdcSize") or raw.get("size") or 0)
            token_id = raw.get("conditionId") or raw.get("asset") or ""

            if side == "BUY" and 0.005 < price < 0.999 and usd > 0.10 and token_id:
                self._pool.record_trade(c.address, token_id, price, usd)

            elif side == "SELL" and token_id:
                self._pool.close_trade(c.address, token_id, price)
