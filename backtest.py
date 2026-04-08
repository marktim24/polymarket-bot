"""
backtest.py — Бэктест копи-трейдинга по историческим сделкам трейдеров.

Загружает последние N сделок каждого трейдера из Polymarket API,
применяет текущие фильтры из config.py и моделирует результат.

Для каждой BUY-сделки, прошедшей фильтры:
  - Определяет размер позиции по типу сигнала (MEDIUM/HIGH)
  - Получает текущую цену через CLOB API
  - Если рынок resolved (цена → 0 или 1): P&L = (outcome - entry) * shares
  - Если рынок active: P&L = (current_price - entry) * shares
  - Применяет стоп-лосс и тейк-профит

Запуск: python backtest.py
"""

import time
import sys
import json
from datetime import datetime, timezone
from typing import Optional, Union

import requests

import config


# ────────────────────────────────────────────────────────────
# HTTP-сессия
# ────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update({"Accept": "application/json"})

RATE_LIMIT_DELAY = 0.3  # задержка между запросами (сек)


def api_get(url: str, retries: int = 2) -> Optional[Union[dict, list]]:
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            if attempt < retries:
                time.sleep(1)
    return None


# ────────────────────────────────────────────────────────────
# Загрузка сделок трейдеров
# ────────────────────────────────────────────────────────────

def fetch_trader_activities(address: str, limit: int = 100) -> list[dict]:
    """Загружает последние `limit` активностей трейдера."""
    url = f"{config.DATA_API_HOST}/activity?user={address}&limit={limit}"
    data = api_get(url)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data", data.get("activities", []))
    return []


# ────────────────────────────────────────────────────────────
# Получение текущей/финальной цены
# ────────────────────────────────────────────────────────────

_price_cache: dict[str, Optional[float]] = {}
_market_cache: dict[str, Optional[dict]] = {}


def get_market_info(condition_id: str) -> Optional[dict]:
    """Получает данные рынка из Gamma API (с кэшем)."""
    if condition_id in _market_cache:
        return _market_cache[condition_id]

    time.sleep(RATE_LIMIT_DELAY)
    url = f"{config.GAMMA_API_HOST}/markets?conditionIds={condition_id}"
    data = api_get(url)
    result = None
    if isinstance(data, list) and data:
        result = data[0]
    _market_cache[condition_id] = result
    return result


_event_cache: dict[str, Optional[list]] = {}


def get_event_markets(event_slug: str) -> list[dict]:
    """Получает все рынки события по eventSlug (с кэшем)."""
    if event_slug in _event_cache:
        return _event_cache[event_slug] or []

    time.sleep(RATE_LIMIT_DELAY)
    url = f"{config.GAMMA_API_HOST}/events?slug={event_slug}"
    data = api_get(url)
    markets = []
    if isinstance(data, list) and data:
        markets = data[0].get("markets", [])
    _event_cache[event_slug] = markets
    return markets


def find_market_by_title(event_slug: str, title: str) -> Optional[dict]:
    """Ищет конкретный рынок по title внутри события."""
    markets = get_event_markets(event_slug)
    for m in markets:
        q = m.get("question", "")
        if q == title or title in q or q in title:
            return m
    # Фоллбэк: первый рынок с matching slug
    return markets[0] if markets else None


def get_current_price(token_id: str) -> Optional[float]:
    """Получает текущую mid-цену токена через CLOB API (с кэшем)."""
    if token_id in _price_cache:
        return _price_cache[token_id]

    time.sleep(RATE_LIMIT_DELAY)
    url = f"{config.CLOB_HOST}/midpoint?token_id={token_id}"
    data = api_get(url)
    price = None
    if data:
        mid = data.get("mid") or data.get("price")
        if mid is not None:
            price = float(mid)
    _price_cache[token_id] = price
    return price


def get_resolved_price(
    condition_id: str, outcome_index: int,
    event_slug: str = "", title: str = "", outcome: str = "",
) -> Optional[float]:
    """
    Если рынок закрыт, возвращает финальную цену (0.0 или 1.0).
    Если открыт — None.
    Ищет через eventSlug → title matching → outcomePrices.
    """
    market = None

    # Способ 1: по event_slug + title (самый надёжный)
    if event_slug and title:
        market = find_market_by_title(event_slug, title)

    # Способ 2: по conditionId
    if not market and condition_id:
        market = get_market_info(condition_id)

    if not market:
        return None

    is_closed = market.get("closed", False)
    if not is_closed:
        return None

    outcome_prices = market.get("outcomePrices")
    if outcome_prices:
        try:
            # outcomePrices может быть JSON-строкой '["0", "1"]' или уже list
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            if isinstance(outcome_prices, list):
                prices = [float(p) for p in outcome_prices]
                if outcome_index < len(prices):
                    return prices[outcome_index]
                # Фоллбэк по имени outcome
                if outcome.lower() in ("yes", "over") and len(prices) >= 1:
                    return prices[0]
                if outcome.lower() in ("no", "under") and len(prices) >= 2:
                    return prices[1]
        except (ValueError, IndexError, json.JSONDecodeError):
            pass

    return None


# ────────────────────────────────────────────────────────────
# Фильтры (повторяют логику бота)
# ────────────────────────────────────────────────────────────

def passes_filters(trade: dict) -> tuple[bool, str]:
    """
    Проверяет сделку по фильтрам из config.py.
    Returns: (passed, reason)
    """
    trade_type = (trade.get("type") or trade.get("side") or "").upper()
    if trade_type not in ("TRADE", "BUY", "PURCHASE"):
        return False, f"не BUY (type={trade_type})"

    side = (trade.get("side") or "").upper()
    if side == "SELL":
        return False, "SELL-сделка"

    price = 0.0
    try:
        price = float(trade.get("price", 0))
    except (TypeError, ValueError):
        return False, "невалидная цена"

    if price <= 0:
        return False, "цена = 0"

    # Диапазон цен
    if price < config.MIN_ENTRY_PRICE:
        return False, f"цена {price:.3f} < {config.MIN_ENTRY_PRICE}"
    if price > config.MAX_ENTRY_PRICE:
        return False, f"цена {price:.3f} > {config.MAX_ENTRY_PRICE}"

    # Размер сделки
    usdc_size = 0.0
    try:
        usdc_size = float(trade.get("usdcSize", 0))
    except (TypeError, ValueError):
        pass
    if usdc_size <= 0:
        return False, "usdcSize = 0"

    return True, "OK"


def classify_signal(price: float) -> str:
    """Упрощённая классификация по цене (без confluence в бэктесте)."""
    if config.SIGNAL_HIGH_MIN_PRICE <= price <= config.SIGNAL_HIGH_MAX_PRICE:
        return "HIGH"
    if config.SIGNAL_MEDIUM_MIN_PRICE <= price <= config.SIGNAL_MEDIUM_MAX_PRICE:
        return "MEDIUM"
    return "MEDIUM"


def get_position_size(signal_type: str) -> float:
    """Размер позиции по типу сигнала."""
    if signal_type == "HIGH":
        return config.HIGH_POSITION_USD
    return config.MEDIUM_POSITION_USD


# ────────────────────────────────────────────────────────────
# Симуляция P&L одной сделки
# ────────────────────────────────────────────────────────────

def simulate_trade(trade: dict, position_usd: float) -> dict:
    """
    Симулирует результат одной BUY-сделки.

    Returns dict с полями:
      entry_price, current_price, shares, pnl, pnl_pct,
      status (resolved_win / resolved_loss / open_profit / open_loss / stop_loss / take_profit),
      market, outcome
    """
    entry_price = float(trade["price"])
    condition_id = trade.get("conditionId", "")
    outcome_index = trade.get("outcomeIndex", 0)
    token_id = trade.get("asset", "")
    title = trade.get("title", "???")
    outcome = trade.get("outcome", "")
    event_slug = trade.get("eventSlug", "")

    shares = position_usd / entry_price

    # 1) Проверяем resolved
    resolved = get_resolved_price(
        condition_id, outcome_index,
        event_slug=event_slug, title=title, outcome=outcome,
    )
    if resolved is not None:
        exit_price = resolved
        pnl = (exit_price - entry_price) * shares
        pnl_pct = (exit_price - entry_price) / entry_price
        status = "resolved_win" if pnl >= 0 else "resolved_loss"
        return {
            "entry_price": entry_price,
            "exit_price": exit_price,
            "shares": shares,
            "position_usd": position_usd,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": status,
            "market": title,
            "outcome": outcome,
        }

    # 2) Открытый рынок — берём текущую цену
    current = get_current_price(token_id) if token_id else None
    if current is None:
        # Не удалось получить цену — считаем позицию нейтральной
        return {
            "entry_price": entry_price,
            "exit_price": entry_price,
            "shares": shares,
            "position_usd": position_usd,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "status": "no_price_data",
            "market": title,
            "outcome": outcome,
        }

    # 3) Применяем стоп-лосс
    stop_price = entry_price * config.STOP_LOSS_PERCENT
    if current <= stop_price:
        pnl = (current - entry_price) * shares
        pnl_pct = (current - entry_price) / entry_price
        return {
            "entry_price": entry_price,
            "exit_price": current,
            "shares": shares,
            "position_usd": position_usd,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": "stop_loss",
            "market": title,
            "outcome": outcome,
        }

    # 4) Применяем тейк-профит (упрощённо — полный выход при TP2)
    tp2_price = entry_price * (1 + config.TAKE_PROFIT_2_PCT)
    if current >= tp2_price:
        # Частичный выход: 50% по TP1, 25% по TP2, остаток по текущей
        tp1_price = entry_price * (1 + config.TAKE_PROFIT_1_PCT)
        shares_tp1 = shares * config.TAKE_PROFIT_1_CLOSE_RATIO
        shares_tp2 = shares * config.TAKE_PROFIT_2_CLOSE_RATIO
        shares_rest = shares - shares_tp1 - shares_tp2

        pnl = (
            (tp1_price - entry_price) * shares_tp1
            + (tp2_price - entry_price) * shares_tp2
            + (current - entry_price) * shares_rest
        )
        avg_exit = (tp1_price * shares_tp1 + tp2_price * shares_tp2 + current * shares_rest) / shares
        pnl_pct = (avg_exit - entry_price) / entry_price
        return {
            "entry_price": entry_price,
            "exit_price": round(avg_exit, 4),
            "shares": shares,
            "position_usd": position_usd,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": "take_profit",
            "market": title,
            "outcome": outcome,
        }

    # 5) Обычная открытая позиция
    pnl = (current - entry_price) * shares
    pnl_pct = (current - entry_price) / entry_price
    status = "open_profit" if pnl >= 0 else "open_loss"
    return {
        "entry_price": entry_price,
        "exit_price": current,
        "shares": shares,
        "position_usd": position_usd,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "status": status,
        "market": title,
        "outcome": outcome,
    }


# ────────────────────────────────────────────────────────────
# Основной бэктест
# ────────────────────────────────────────────────────────────

def run_backtest(limit: int = 100):
    print("=" * 70)
    print(f"  БЭКТЕСТ КОПИ-ТРЕЙДИНГА — последние {limit} сделок на трейдера")
    print(f"  Фильтры: цена {config.MIN_ENTRY_PRICE}–{config.MAX_ENTRY_PRICE}")
    print(f"  Стоп-лосс: {(1 - config.STOP_LOSS_PERCENT) * 100:.0f}% | "
          f"TP1: +{config.TAKE_PROFIT_1_PCT * 100:.0f}% | "
          f"TP2: +{config.TAKE_PROFIT_2_PCT * 100:.0f}%")
    print(f"  Позиция: MEDIUM=${config.MEDIUM_POSITION_USD} | HIGH=${config.HIGH_POSITION_USD}")
    print("=" * 70)

    all_results = []
    trader_stats = {}

    for trader in config.TRADERS:
        name = trader["name"]
        address = trader["address"]
        print(f"\n{'─' * 50}")
        print(f"  Трейдер: {name} ({trader['strategy']})")
        print(f"  Адрес: {address}")
        print(f"{'─' * 50}")

        # Загружаем сделки
        activities = fetch_trader_activities(address, limit)
        print(f"  Загружено активностей: {len(activities)}")

        if not activities:
            print("  ⚠️  Нет данных!")
            continue

        passed = []
        skip_reasons: dict[str, int] = {}

        for act in activities:
            ok, reason = passes_filters(act)
            if ok:
                passed.append(act)
            else:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        print(f"  Прошли фильтры: {len(passed)} / {len(activities)}")
        if skip_reasons:
            top_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])[:5]
            for reason, count in top_reasons:
                print(f"    ↳ отклонено ({count}): {reason}")

        if not passed:
            continue

        # Симулируем каждую сделку
        trader_results = []
        for i, trade in enumerate(passed):
            price = float(trade["price"])
            signal = classify_signal(price)
            pos_usd = get_position_size(signal)

            result = simulate_trade(trade, pos_usd)
            result["trader"] = name
            result["signal"] = signal
            trader_results.append(result)

            # Прогресс
            sys.stdout.write(f"\r  Симуляция: {i + 1}/{len(passed)}...")
            sys.stdout.flush()

        print(f"\r  Симуляция: {len(passed)}/{len(passed)} ✓       ")

        # Статистика по трейдеру
        total_pnl = sum(r["pnl"] for r in trader_results)
        total_invested = sum(r["position_usd"] for r in trader_results)
        wins = sum(1 for r in trader_results if r["pnl"] > 0)
        losses = sum(1 for r in trader_results if r["pnl"] < 0)
        neutral = sum(1 for r in trader_results if r["pnl"] == 0)

        status_counts: dict[str, int] = {}
        for r in trader_results:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

        win_rate = wins / max(wins + losses, 1) * 100

        trader_stats[name] = {
            "trades": len(trader_results),
            "wins": wins,
            "losses": losses,
            "neutral": neutral,
            "win_rate": win_rate,
            "pnl": total_pnl,
            "invested": total_invested,
            "roi_pct": (total_pnl / total_invested * 100) if total_invested > 0 else 0,
            "statuses": status_counts,
        }

        pnl_sign = "+" if total_pnl >= 0 else ""
        print(f"\n  📊 {name}: {pnl_sign}${total_pnl:.2f} "
              f"(ROI: {pnl_sign}{trader_stats[name]['roi_pct']:.1f}%)")
        print(f"     Win/Loss/Neutral: {wins}/{losses}/{neutral} "
              f"(WR: {win_rate:.0f}%)")
        for status, cnt in sorted(status_counts.items()):
            print(f"     {status}: {cnt}")

        # Топ-3 лучших и худших сделок
        sorted_results = sorted(trader_results, key=lambda r: r["pnl"], reverse=True)
        if sorted_results:
            print(f"\n  🏆 Лучшие:")
            for r in sorted_results[:3]:
                sign = "+" if r["pnl"] >= 0 else ""
                print(f"     {sign}${r['pnl']:.2f} ({sign}{r['pnl_pct']*100:.1f}%) "
                      f"@ {r['entry_price']:.3f}→{r['exit_price']:.3f} "
                      f"[{r['status']}] {r['market'][:50]}")
            print(f"\n  💀 Худшие:")
            for r in sorted_results[-3:]:
                sign = "+" if r["pnl"] >= 0 else ""
                print(f"     {sign}${r['pnl']:.2f} ({sign}{r['pnl_pct']*100:.1f}%) "
                      f"@ {r['entry_price']:.3f}→{r['exit_price']:.3f} "
                      f"[{r['status']}] {r['market'][:50]}")

        all_results.extend(trader_results)

    # ════════════════════════════════════════════════════════
    # ИТОГИ
    # ════════════════════════════════════════════════════════
    print("\n")
    print("=" * 70)
    print("  ИТОГИ БЭКТЕСТА")
    print("=" * 70)

    if not all_results:
        print("  Нет сделок для анализа!")
        return

    total_pnl = sum(r["pnl"] for r in all_results)
    total_invested = sum(r["position_usd"] for r in all_results)
    total_wins = sum(1 for r in all_results if r["pnl"] > 0)
    total_losses = sum(1 for r in all_results if r["pnl"] < 0)
    total_neutral = sum(1 for r in all_results if r["pnl"] == 0)
    overall_wr = total_wins / max(total_wins + total_losses, 1) * 100
    overall_roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    pnl_sign = "+" if total_pnl >= 0 else ""
    print(f"\n  Всего сделок:    {len(all_results)}")
    print(f"  Win/Loss/Neutral: {total_wins}/{total_losses}/{total_neutral}")
    print(f"  Win Rate:         {overall_wr:.1f}%")
    print(f"  Общий P&L:       {pnl_sign}${total_pnl:.2f}")
    print(f"  Общий ROI:       {pnl_sign}{overall_roi:.1f}%")
    print(f"  Инвестировано:    ${total_invested:.2f}")

    # Средний P&L на сделку
    avg_pnl = total_pnl / len(all_results)
    avg_sign = "+" if avg_pnl >= 0 else ""
    print(f"  Сред. P&L/сделку: {avg_sign}${avg_pnl:.2f}")

    # Макс. просадка (последовательный убыток)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for r in all_results:
        cumulative += r["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_drawdown:
            max_drawdown = dd
    print(f"  Макс. просадка:   -${max_drawdown:.2f}")

    # Распределение по статусам
    all_statuses: dict[str, int] = {}
    for r in all_results:
        all_statuses[r["status"]] = all_statuses.get(r["status"], 0) + 1
    print(f"\n  Статусы сделок:")
    for status, cnt in sorted(all_statuses.items()):
        pct = cnt / len(all_results) * 100
        print(f"    {status:20s} {cnt:>4d}  ({pct:.1f}%)")

    # Рейтинг трейдеров
    print(f"\n  Рейтинг трейдеров:")
    print(f"  {'Трейдер':20s} {'Сделок':>6s} {'WR':>6s} {'P&L':>10s} {'ROI':>8s}")
    print(f"  {'─' * 52}")
    for name, st in sorted(trader_stats.items(), key=lambda x: -x[1]["pnl"]):
        pnl_s = f"{'+'if st['pnl']>=0 else ''}${st['pnl']:.2f}"
        roi_s = f"{'+'if st['roi_pct']>=0 else ''}{st['roi_pct']:.1f}%"
        print(f"  {name:20s} {st['trades']:>6d} {st['win_rate']:>5.0f}% {pnl_s:>10s} {roi_s:>8s}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    run_backtest(limit=100)
