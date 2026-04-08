"""
backtest.py — Бэктест копи-трейдинга по историческим сделкам трейдеров.

Загружает сделки каждого трейдера из Polymarket API,
применяет per-trader фильтры из config.py и моделирует результат.

v3.0 — slippage + per-trader overrides + 30-day filter:
  - Slippage: 1% для нормальных рынков, 2% для low-liq (< $1000 volume)
  - Per-trader фильтры: price range, min trader size, crypto skip
  - Per-trader выходы: SL, TP1, TP2, max hold (WizzleGizzle = 180 дней)
  - Фильтр по возрасту сделки: только последние N дней

Запуск: python backtest.py [--days 30] [--limit 200]
"""

import time
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

import requests

import config


# ────────────────────────────────────────────────────────────
# SLIPPAGE МОДЕЛЬ
# ────────────────────────────────────────────────────────────

SLIPPAGE_NORMAL = 0.01   # 1% для нормальных рынков
SLIPPAGE_LOW_LIQ = 0.02  # 2% для низколиквидных рынков
LOW_LIQ_THRESHOLD = 1000  # $1000 порог ликвидности


def apply_slippage(price: float, direction: str = "buy", liquidity: Optional[float] = None) -> float:
    """
    Применяет slippage к цене.
    BUY: цена увеличивается (покупаем дороже).
    SELL: цена уменьшается (продаём дешевле).
    """
    if liquidity is not None and liquidity < LOW_LIQ_THRESHOLD:
        slip = SLIPPAGE_LOW_LIQ
    else:
        slip = SLIPPAGE_NORMAL

    if direction == "buy":
        return min(price * (1 + slip), 0.99)  # cap at 0.99
    else:
        return max(price * (1 - slip), 0.001)  # floor at 0.001


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

def passes_filters(trade: dict, trader_name: str, days_cutoff: Optional[datetime] = None) -> tuple[bool, str]:
    """
    Проверяет сделку по per-trader фильтрам из config.py.
    Returns: (passed, reason)
    """
    trade_type = (trade.get("type") or trade.get("side") or "").upper()
    if trade_type not in ("TRADE", "BUY", "PURCHASE"):
        return False, f"не BUY (type={trade_type})"

    side = (trade.get("side") or "").upper()
    if side == "SELL":
        return False, "SELL-сделка"

    # Фильтр по дате
    if days_cutoff:
        ts_raw = trade.get("timestamp") or trade.get("createdAt") or 0
        try:
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            if ts < days_cutoff:
                return False, "старше периода"
        except (ValueError, TypeError, OSError):
            return False, "невалидная дата"

    price = 0.0
    try:
        price = float(trade.get("price", 0))
    except (TypeError, ValueError):
        return False, "невалидная цена"

    if price <= 0:
        return False, "цена = 0"

    # Per-trader диапазон цен
    min_price = config.get_trader_config(trader_name, "MIN_ENTRY_PRICE")
    max_price = config.get_trader_config(trader_name, "MAX_ENTRY_PRICE")
    if price < min_price:
        return False, f"цена {price:.3f} < {min_price}"
    if price > max_price:
        return False, f"цена {price:.3f} > {max_price}"

    # Размер сделки трейдера
    usdc_size = 0.0
    try:
        usdc_size = float(trade.get("usdcSize", 0))
    except (TypeError, ValueError):
        pass
    if usdc_size <= 0:
        return False, "usdcSize = 0"

    # Per-trader мин. размер позиции трейдера
    min_trader_size = config.get_trader_config(trader_name, "MIN_TRADER_SIZE_USD", 0.0)
    if min_trader_size > 0 and usdc_size < min_trader_size:
        return False, f"микро-позиция ${usdc_size:.2f} < ${min_trader_size:.2f}"

    # Per-trader: пропуск крипто micro-рынков
    if config.get_trader_config(trader_name, "SKIP_CRYPTO_MICRO", False):
        slug = (trade.get("market") or trade.get("slug") or "").lower()
        title = (trade.get("title") or "").lower()
        text = slug + " " + title
        crypto_keywords = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana",
                           "crypto", "5-min", "5min", "1-min", "1min", "hourly"]
        if any(kw in text for kw in crypto_keywords):
            return False, f"крипто micro-market"

    return True, "OK"


def classify_signal(price: float, trader_name: str) -> str:
    """Классификация по per-trader price range."""
    min_p = config.get_trader_config(trader_name, "MIN_ENTRY_PRICE")
    max_p = config.get_trader_config(trader_name, "MAX_ENTRY_PRICE")
    if min_p <= price <= max_p:
        return "MEDIUM"  # без confluence в бэктесте
    return "MEDIUM"


def get_position_size(signal_type: str, trader_name: str) -> float:
    """Размер позиции по типу сигнала и трейдеру."""
    high = config.get_trader_config(trader_name, "HIGH_POSITION_USD", config.HIGH_POSITION_USD)
    medium = config.get_trader_config(trader_name, "MEDIUM_POSITION_USD", config.MEDIUM_POSITION_USD)
    base = config.get_trader_config(trader_name, "BASE_POSITION_USD", config.BASE_POSITION_USD)
    return {"HIGH": high, "MEDIUM": medium}.get(signal_type, base)


# ────────────────────────────────────────────────────────────
# Симуляция P&L одной сделки
# ────────────────────────────────────────────────────────────

def simulate_trade(trade: dict, position_usd: float, trader_name: str) -> dict:
    """
    Симулирует результат одной BUY-сделки с учётом slippage и per-trader SL/TP.

    Slippage:
      - BUY entry: +1% (normal) или +2% (low-liq)
      - SELL exit: -1%/-2% (для SL и TP)
      - Resolved: без slippage (финальная цена точная)
    """
    raw_entry = float(trade["price"])
    condition_id = trade.get("conditionId", "")
    outcome_index = trade.get("outcomeIndex", 0)
    token_id = trade.get("asset", "")
    title = trade.get("title", "???")
    outcome = trade.get("outcome", "")
    event_slug = trade.get("eventSlug", "")

    # Slippage на вход
    entry_price = apply_slippage(raw_entry, "buy")
    slippage_cost = entry_price - raw_entry

    shares = position_usd / entry_price

    # Per-trader SL/TP параметры
    sl_pct = config.get_trader_config(trader_name, "STOP_LOSS_PERCENT", config.STOP_LOSS_PERCENT)
    tp1_pct = config.get_trader_config(trader_name, "TAKE_PROFIT_1_PCT", config.TAKE_PROFIT_1_PCT)
    tp2_pct = config.get_trader_config(trader_name, "TAKE_PROFIT_2_PCT", config.TAKE_PROFIT_2_PCT)
    tp1_ratio = config.get_trader_config(trader_name, "TAKE_PROFIT_1_CLOSE_RATIO", config.TAKE_PROFIT_1_CLOSE_RATIO)
    tp2_ratio = config.get_trader_config(trader_name, "TAKE_PROFIT_2_CLOSE_RATIO", config.TAKE_PROFIT_2_CLOSE_RATIO)

    base_result = {
        "raw_entry": raw_entry,
        "entry_price": entry_price,
        "slippage_cost": slippage_cost,
        "shares": shares,
        "position_usd": position_usd,
        "market": title,
        "outcome": outcome,
    }

    # 1) Проверяем resolved
    resolved = get_resolved_price(
        condition_id, outcome_index,
        event_slug=event_slug, title=title, outcome=outcome,
    )
    if resolved is not None:
        exit_price = resolved  # resolved = точная цена, без slippage
        pnl = (exit_price - entry_price) * shares
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
        status = "resolved_win" if pnl >= 0 else "resolved_loss"
        return {**base_result, "exit_price": exit_price, "pnl": pnl,
                "pnl_pct": pnl_pct, "status": status}

    # 2) Открытый рынок — берём текущую цену
    current = get_current_price(token_id) if token_id else None
    if current is None:
        return {**base_result, "exit_price": entry_price, "pnl": 0.0,
                "pnl_pct": 0.0, "status": "no_price_data"}

    # 3) Стоп-лосс (per-trader)
    stop_price = entry_price * sl_pct
    if current <= stop_price:
        exit_price = apply_slippage(current, "sell")  # slippage на выход
        pnl = (exit_price - entry_price) * shares
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
        return {**base_result, "exit_price": exit_price, "pnl": pnl,
                "pnl_pct": pnl_pct, "status": "stop_loss"}

    # 4) Тейк-профит (per-trader)
    tp2_price = entry_price * (1 + tp2_pct)
    if current >= tp2_price:
        tp1_price = entry_price * (1 + tp1_pct)
        shares_tp1 = shares * tp1_ratio
        shares_tp2 = shares * tp2_ratio
        shares_rest = shares - shares_tp1 - shares_tp2

        # Slippage на каждый выход
        exit_tp1 = apply_slippage(tp1_price, "sell")
        exit_tp2 = apply_slippage(tp2_price, "sell")
        exit_rest = apply_slippage(current, "sell")

        pnl = (
            (exit_tp1 - entry_price) * shares_tp1
            + (exit_tp2 - entry_price) * shares_tp2
            + (exit_rest - entry_price) * shares_rest
        )
        avg_exit = (exit_tp1 * shares_tp1 + exit_tp2 * shares_tp2 + exit_rest * shares_rest) / shares if shares > 0 else 0
        pnl_pct = (avg_exit - entry_price) / entry_price if entry_price > 0 else 0
        return {**base_result, "exit_price": round(avg_exit, 4), "pnl": pnl,
                "pnl_pct": pnl_pct, "status": "take_profit"}

    # Только TP1?
    tp1_price = entry_price * (1 + tp1_pct)
    if current >= tp1_price:
        shares_tp1 = shares * tp1_ratio
        shares_rest = shares - shares_tp1
        exit_tp1 = apply_slippage(tp1_price, "sell")
        exit_rest = apply_slippage(current, "sell")
        pnl = (exit_tp1 - entry_price) * shares_tp1 + (exit_rest - entry_price) * shares_rest
        avg_exit = (exit_tp1 * shares_tp1 + exit_rest * shares_rest) / shares if shares > 0 else 0
        pnl_pct = (avg_exit - entry_price) / entry_price if entry_price > 0 else 0
        return {**base_result, "exit_price": round(avg_exit, 4), "pnl": pnl,
                "pnl_pct": pnl_pct, "status": "take_profit_partial"}

    # 5) Обычная открытая позиция (unrealized PnL с slippage на гипотетический exit)
    exit_price = apply_slippage(current, "sell")  # если бы сейчас закрылись
    pnl = (exit_price - entry_price) * shares
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
    status = "open_profit" if pnl >= 0 else "open_loss"
    return {**base_result, "exit_price": exit_price, "pnl": pnl,
            "pnl_pct": pnl_pct, "status": status}


# ────────────────────────────────────────────────────────────
# Основной бэктест
# ────────────────────────────────────────────────────────────

def run_backtest(limit: int = 200, days: int = 30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    print("=" * 70)
    print(f"  БЭКТЕСТ КОПИ-ТРЕЙДИНГА — последние {days} дней (макс. {limit} сделок)")
    print(f"  Slippage: {SLIPPAGE_NORMAL*100:.0f}% (normal) / {SLIPPAGE_LOW_LIQ*100:.0f}% (low-liq)")
    print(f"  C: {cutoff.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print("=" * 70)

    for t in config.TRADERS:
        n = t['name']
        mn = config.get_trader_config(n, 'MIN_ENTRY_PRICE')
        mx = config.get_trader_config(n, 'MAX_ENTRY_PRICE')
        sl = config.get_trader_config(n, 'STOP_LOSS_PERCENT')
        tp1 = config.get_trader_config(n, 'TAKE_PROFIT_1_PCT')
        tp2 = config.get_trader_config(n, 'TAKE_PROFIT_2_PCT')
        mh = config.get_trader_config(n, 'MAX_HOLD_HOURS')
        pos = config.get_trader_config(n, 'MEDIUM_POSITION_USD', config.MEDIUM_POSITION_USD)
        print(f"  {n}: цена {mn}–{mx} | SL={sl} | TP1=+{tp1*100:.0f}% TP2=+{tp2*100:.0f}% | "
              f"Hold≤{mh:.0f}ч | Size=${pos}")
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
            ok, reason = passes_filters(act, name, days_cutoff=cutoff)
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
        total_slippage = 0.0
        for i, trade in enumerate(passed):
            price = float(trade["price"])
            signal = classify_signal(price, name)
            pos_usd = get_position_size(signal, name)

            result = simulate_trade(trade, pos_usd, name)
            result["trader"] = name
            result["signal"] = signal
            total_slippage += result.get("slippage_cost", 0) * result["shares"]
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
            "slippage_total": total_slippage,
        }

        pnl_sign = "+" if total_pnl >= 0 else ""
        print(f"\n  📊 {name}: {pnl_sign}${total_pnl:.2f} "
              f"(ROI: {pnl_sign}{trader_stats[name]['roi_pct']:.1f}%)")
        print(f"     Win/Loss/Neutral: {wins}/{losses}/{neutral} "
              f"(WR: {win_rate:.0f}%)")
        print(f"     Slippage cost: -${total_slippage:.2f}")
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
    total_slippage = sum(r.get("slippage_cost", 0) * r["shares"] for r in all_results)
    total_wins = sum(1 for r in all_results if r["pnl"] > 0)
    total_losses = sum(1 for r in all_results if r["pnl"] < 0)
    total_neutral = sum(1 for r in all_results if r["pnl"] == 0)
    overall_wr = total_wins / max(total_wins + total_losses, 1) * 100
    overall_roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    pnl_sign = "+" if total_pnl >= 0 else ""
    print(f"\n  Всего сделок:     {len(all_results)}")
    print(f"  Win/Loss/Neutral:  {total_wins}/{total_losses}/{total_neutral}")
    print(f"  Win Rate:          {overall_wr:.1f}%")
    print(f"  Общий P&L:        {pnl_sign}${total_pnl:.2f}")
    print(f"  Общий ROI:        {pnl_sign}{overall_roi:.1f}%")
    print(f"  Инвестировано:     ${total_invested:.2f}")
    print(f"  Slippage (вход):   -${total_slippage:.2f}")

    # P&L без slippage для сравнения
    pnl_no_slip = sum(
        ((r["exit_price"] - r["raw_entry"]) * r["shares"]) if "raw_entry" in r else r["pnl"]
        for r in all_results
    )
    slip_impact = total_pnl - pnl_no_slip
    print(f"  P&L без slippage:  {'+'if pnl_no_slip>=0 else ''}${pnl_no_slip:.2f}")
    print(f"  Влияние slippage: ${slip_impact:.2f}")

    # Средний P&L на сделку
    avg_pnl = total_pnl / len(all_results)
    avg_sign = "+" if avg_pnl >= 0 else ""
    print(f"  Сред. P&L/сделку:  {avg_sign}${avg_pnl:.2f}")

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
    print(f"  Макс. просадка:    -${max_drawdown:.2f}")

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
    print(f"  {'Трейдер':20s} {'Сделок':>6s} {'WR':>6s} {'P&L':>10s} {'ROI':>8s} {'Slippage':>10s}")
    print(f"  {'─' * 66}")
    for name, st in sorted(trader_stats.items(), key=lambda x: -x[1]["pnl"]):
        pnl_s = f"{'+'if st['pnl']>=0 else ''}${st['pnl']:.2f}"
        roi_s = f"{'+'if st['roi_pct']>=0 else ''}{st['roi_pct']:.1f}%"
        slip_s = f"-${st.get('slippage_total', 0):.2f}"
        print(f"  {name:20s} {st['trades']:>6d} {st['win_rate']:>5.0f}% {pnl_s:>10s} {roi_s:>8s} {slip_s:>10s}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Бэктест копи-трейдинга")
    parser.add_argument("--days", type=int, default=30, help="Кол-во дней назад")
    parser.add_argument("--limit", type=int, default=200, help="Макс. сделок на трейдера")
    args = parser.parse_args()
    run_backtest(limit=args.limit, days=args.days)
