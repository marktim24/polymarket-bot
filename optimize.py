"""
optimize.py — Подбор оптимальных фильтров для каждого трейдера.

1) Загружает все доступные BUY-сделки
2) Делит на train (первые ~50%) и test (вторые ~50%)
3) Перебирает комбинации MIN/MAX цены на train
4) Выбирает фильтры с лучшим P&L
5) Прогоняет на test для валидации

Запуск: python optimize.py
"""

import time
import sys
import json
from datetime import datetime, timezone
from typing import Optional, Union
from itertools import product

import requests
import config


session = requests.Session()
session.headers.update({"Accept": "application/json"})

RATE_LIMIT_DELAY = 0.25

_price_cache = {}
_event_cache = {}
_market_cache = {}


def api_get(url, retries=2):
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


def get_event_markets(event_slug):
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


def find_market_by_title(event_slug, title):
    markets = get_event_markets(event_slug)
    for m in markets:
        q = m.get("question", "")
        if q == title or title in q or q in title:
            return m
    return markets[0] if markets else None


def get_market_info(condition_id):
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


def get_current_price(token_id):
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


def get_resolved_price(condition_id, outcome_index, event_slug="", title="", outcome=""):
    market = None
    if event_slug and title:
        market = find_market_by_title(event_slug, title)
    if not market and condition_id:
        market = get_market_info(condition_id)
    if not market:
        return None
    if not market.get("closed", False):
        return None
    outcome_prices = market.get("outcomePrices")
    if outcome_prices:
        try:
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            if isinstance(outcome_prices, list):
                prices = [float(p) for p in outcome_prices]
                if outcome_index < len(prices):
                    return prices[outcome_index]
                if outcome.lower() in ("yes", "over") and len(prices) >= 1:
                    return prices[0]
                if outcome.lower() in ("no", "under") and len(prices) >= 2:
                    return prices[1]
        except (ValueError, IndexError, json.JSONDecodeError):
            pass
    return None


# ────────────────────────────────────────────────────────────
# Загрузка и подготовка данных
# ────────────────────────────────────────────────────────────

def fetch_all_buys(address, limit=500):
    """Загружает BUY-сделки трейдера."""
    url = f"{config.DATA_API_HOST}/activity?user={address}&limit={limit}"
    data = api_get(url)
    if not isinstance(data, list):
        data = []
    buys = []
    for d in data:
        t = (d.get("type") or d.get("side") or "").upper()
        side = (d.get("side") or "").upper()
        try:
            price = float(d.get("price", 0) or 0)
            usdc = float(d.get("usdcSize", 0) or 0)
        except (TypeError, ValueError):
            continue
        if t in ("TRADE", "BUY", "PURCHASE") and side != "SELL" and price > 0 and usdc > 0:
            buys.append(d)
    return buys


def resolve_trade(trade):
    """
    Получает исход сделки. Возвращает (exit_price, status).
    exit_price: 0.0 или 1.0 для resolved, float для open, None если неизвестно.
    """
    cid = trade.get("conditionId", "")
    oi = trade.get("outcomeIndex", 0)
    asset = trade.get("asset", "")
    title = trade.get("title", "")
    eslug = trade.get("eventSlug", "")
    outcome = trade.get("outcome", "")

    resolved = get_resolved_price(cid, oi, event_slug=eslug, title=title, outcome=outcome)
    if resolved is not None:
        return resolved, "resolved"

    current = get_current_price(asset) if asset else None
    if current is not None:
        return current, "open"

    return None, "unknown"


def simulate_pnl(entry_price, exit_price, position_usd,
                 stop_loss_pct=0.80, tp1_pct=0.20, tp2_pct=0.40):
    """Рассчитывает P&L одной сделки с учётом SL/TP."""
    shares = position_usd / entry_price

    # Стоп-лосс
    stop_price = entry_price * stop_loss_pct
    if exit_price <= stop_price and exit_price < entry_price:
        return (exit_price - entry_price) * shares

    # Тейк-профит TP2
    tp2_price = entry_price * (1 + tp2_pct)
    if exit_price >= tp2_price:
        tp1_price = entry_price * (1 + tp1_pct)
        s1 = shares * 0.50
        s2 = shares * 0.25
        s3 = shares - s1 - s2
        return (tp1_price - entry_price) * s1 + (tp2_price - entry_price) * s2 + (exit_price - entry_price) * s3

    return (exit_price - entry_price) * shares


# ────────────────────────────────────────────────────────────
# Оптимизация фильтров
# ────────────────────────────────────────────────────────────

def evaluate_filters(trades_with_outcomes, min_price, max_price, position_usd=5.0):
    """
    Применяет фильтры к подготовленным сделкам и считает P&L.
    trades_with_outcomes: list of (trade_dict, exit_price, status)
    """
    total_pnl = 0.0
    count = 0
    wins = 0
    losses = 0

    for trade, exit_price, status in trades_with_outcomes:
        if status == "unknown":
            continue
        entry = float(trade["price"])
        if entry < min_price or entry > max_price:
            continue

        pnl = simulate_pnl(entry, exit_price, position_usd)
        total_pnl += pnl
        count += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    wr = wins / max(wins + losses, 1) * 100
    roi = (total_pnl / (count * position_usd) * 100) if count > 0 else 0
    return {
        "pnl": total_pnl,
        "count": count,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "roi": roi,
    }


def find_best_filters(trades_with_outcomes, position_usd=5.0):
    """
    Grid search по MIN_PRICE и MAX_PRICE.
    Returns: best (min_price, max_price, stats)
    """
    # Диапазон поиска
    min_prices = [round(x * 0.05, 2) for x in range(1, 12)]   # 0.05 to 0.55
    max_prices = [round(x * 0.05, 2) for x in range(3, 15)]   # 0.15 to 0.70

    best = None
    best_pnl = -999999

    for min_p, max_p in product(min_prices, max_prices):
        if max_p <= min_p:
            continue

        stats = evaluate_filters(trades_with_outcomes, min_p, max_p, position_usd)

        # Минимум 5 сделок для статистической значимости
        if stats["count"] < 5:
            continue

        # Оптимизируем по P&L, но с штрафом за слишком малое кол-во сделок
        score = stats["pnl"]
        if score > best_pnl:
            best_pnl = score
            best = (min_p, max_p, stats)

    return best


# ────────────────────────────────────────────────────────────
# Основной скрипт
# ────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  ОПТИМИЗАЦИЯ ФИЛЬТРОВ — Train/Test Split")
    print("=" * 70)

    traders = config.TRADERS
    results = {}

    for trader in traders:
        name = trader["name"]
        address = trader["address"]
        print(f"\n{'━' * 70}")
        print(f"  📊 {name} ({trader['strategy']})")
        print(f"{'━' * 70}")

        # 1) Загрузка данных
        sys.stdout.write(f"  Загрузка сделок...")
        sys.stdout.flush()
        all_buys = fetch_all_buys(address, limit=500)
        print(f" {len(all_buys)} BUY-сделок загружено")

        if len(all_buys) < 20:
            print("  ⚠️ Недостаточно данных для оптимизации")
            continue

        # 2) Разделение: старые → train, новые → test
        # API возвращает сделки от новых к старым, поэтому:
        # all_buys[0] — самая новая, all_buys[-1] — самая старая
        mid = len(all_buys) // 2
        test_buys = all_buys[:mid]        # новые (test)
        train_buys = all_buys[mid:]       # старые (train)
        print(f"  Train: {len(train_buys)} сделок (старые) | Test: {len(test_buys)} сделок (новые)")

        # 3) Resolve исходов для train
        sys.stdout.write(f"  Resolve train outcomes...")
        sys.stdout.flush()
        train_outcomes = []
        for i, trade in enumerate(train_buys):
            exit_price, status = resolve_trade(trade)
            if exit_price is not None:
                train_outcomes.append((trade, exit_price, status))
            sys.stdout.write(f"\r  Resolve train outcomes... {i+1}/{len(train_buys)}")
            sys.stdout.flush()
        resolved_train = len([x for x in train_outcomes if x[2] == "resolved"])
        print(f"\r  Train: {len(train_outcomes)} с ценами ({resolved_train} resolved)          ")

        if len(train_outcomes) < 10:
            print("  ⚠️ Недостаточно resolved сделок для оптимизации")
            continue

        # 4) Оптимизация фильтров на train
        best = find_best_filters(train_outcomes, position_usd=5.0)
        if not best:
            print("  ❌ Не найдены фильтры с положительным P&L")
            # Показываем базовые результаты
            base = evaluate_filters(train_outcomes, 0.05, 0.70, 5.0)
            print(f"  Базовые (0.05–0.70): {base['count']} сделок, WR={base['wr']:.0f}%, "
                  f"P&L={'+'if base['pnl']>=0 else ''}${base['pnl']:.2f}, ROI={base['roi']:.1f}%")
            results[name] = {"has_filters": False, "base_train": base}
            continue

        opt_min, opt_max, train_stats = best
        sign = "+" if train_stats["pnl"] >= 0 else ""
        print(f"\n  ✅ ЛУЧШИЕ ФИЛЬТРЫ (train):")
        print(f"     Цена: {opt_min:.2f} – {opt_max:.2f}")
        print(f"     Сделок: {train_stats['count']}")
        print(f"     Win Rate: {train_stats['wr']:.0f}%")
        print(f"     P&L: {sign}${train_stats['pnl']:.2f}")
        print(f"     ROI: {sign}{train_stats['roi']:.1f}%")

        # 5) Resolve test outcomes
        sys.stdout.write(f"  Resolve test outcomes...")
        sys.stdout.flush()
        test_outcomes = []
        for i, trade in enumerate(test_buys):
            exit_price, status = resolve_trade(trade)
            if exit_price is not None:
                test_outcomes.append((trade, exit_price, status))
            sys.stdout.write(f"\r  Resolve test outcomes... {i+1}/{len(test_buys)}")
            sys.stdout.flush()
        resolved_test = len([x for x in test_outcomes if x[2] == "resolved"])
        print(f"\r  Test: {len(test_outcomes)} с ценами ({resolved_test} resolved)          ")

        # 6) Валидация на test
        test_stats = evaluate_filters(test_outcomes, opt_min, opt_max, position_usd=5.0)
        test_base = evaluate_filters(test_outcomes, 0.05, 0.70, position_usd=5.0)

        sign_t = "+" if test_stats["pnl"] >= 0 else ""
        sign_b = "+" if test_base["pnl"] >= 0 else ""
        print(f"\n  📋 ВАЛИДАЦИЯ (test):")
        print(f"     Оптимальные ({opt_min:.2f}–{opt_max:.2f}):")
        print(f"       Сделок: {test_stats['count']}, WR: {test_stats['wr']:.0f}%, "
              f"P&L: {sign_t}${test_stats['pnl']:.2f}, ROI: {sign_t}{test_stats['roi']:.1f}%")
        print(f"     Базовые (0.05–0.70):")
        print(f"       Сделок: {test_base['count']}, WR: {test_base['wr']:.0f}%, "
              f"P&L: {sign_b}${test_base['pnl']:.2f}, ROI: {sign_b}{test_base['roi']:.1f}%")

        improvement = test_stats["pnl"] - test_base["pnl"]
        print(f"     Разница: {'+'if improvement>=0 else ''}${improvement:.2f}")

        results[name] = {
            "has_filters": True,
            "min_price": opt_min,
            "max_price": opt_max,
            "train": train_stats,
            "test_optimized": test_stats,
            "test_base": test_base,
        }

    # ════════════════════════════════════════════════════════
    # СВОДКА
    # ════════════════════════════════════════════════════════
    print("\n\n" + "=" * 70)
    print("  СВОДНАЯ ТАБЛИЦА")
    print("=" * 70)
    print(f"\n  {'Трейдер':15s} {'Фильтр':12s} │ {'Train':>30s} │ {'Test':>30s}")
    print(f"  {'':15s} {'':12s} │ {'Сделок WR%   P&L     ROI':>30s} │ {'Сделок WR%   P&L     ROI':>30s}")
    print(f"  {'─'*15} {'─'*12} ┼ {'─'*30} ┼ {'─'*30}")

    for name, r in results.items():
        if not r.get("has_filters"):
            base = r.get("base_train", {})
            print(f"  {name:15s} {'нет':12s} │ {base.get('count',0):>5d} {base.get('wr',0):>4.0f}% "
                  f"{'+'if base.get('pnl',0)>=0 else ''}{base.get('pnl',0):>7.2f} "
                  f"{'+'if base.get('roi',0)>=0 else ''}{base.get('roi',0):>5.1f}% │ {'—':>30s}")
            continue

        tr = r["train"]
        te = r["test_optimized"]
        flt = f"{r['min_price']:.2f}–{r['max_price']:.2f}"
        print(f"  {name:15s} {flt:12s} │ "
              f"{tr['count']:>5d} {tr['wr']:>4.0f}% {'+'if tr['pnl']>=0 else ''}{tr['pnl']:>7.2f} "
              f"{'+'if tr['roi']>=0 else ''}{tr['roi']:>5.1f}% │ "
              f"{te['count']:>5d} {te['wr']:>4.0f}% {'+'if te['pnl']>=0 else ''}{te['pnl']:>7.2f} "
              f"{'+'if te['roi']>=0 else ''}{te['roi']:>5.1f}%")

    # Рекомендации
    print(f"\n  РЕКОМЕНДАЦИИ:")
    for name, r in results.items():
        if r.get("has_filters") and r["test_optimized"]["pnl"] > 0:
            print(f"  ✅ {name}: использовать фильтр {r['min_price']:.2f}–{r['max_price']:.2f} "
                  f"(test P&L: +${r['test_optimized']['pnl']:.2f})")
        elif r.get("has_filters") and r["train"]["pnl"] > 0 and r["test_optimized"]["pnl"] <= 0:
            print(f"  ⚠️  {name}: фильтр {r['min_price']:.2f}–{r['max_price']:.2f} "
                  f"переобучен (train +${r['train']['pnl']:.2f}, test ${r['test_optimized']['pnl']:.2f})")
        else:
            print(f"  ❌ {name}: нет прибыльных фильтров, рекомендуется отключить")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
