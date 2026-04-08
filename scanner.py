"""
scanner.py — Поиск кошельков похожих на sayber по метрикам.

Загружает топ-адресов с лидерборда Polymarket, анализирует последние
N сделок каждого, и ранжирует по комбинации критериев:
  1. Win Rate ≥ 80% (по resolved сделкам)
  2. Средняя цена входа 0.40–0.70 ("value zone")
  3. Минимум 15 BUY-сделок за 30 дней (активность)
  4. Доля спорт-рынков ≥ 50% (фокус)
  5. Consistency — не зависит от 1–2 удачных крупных ставок
  6. Средний размер ≥ $10 (не микро-ботик)

Запуск:
  python scanner.py                      # дефолт: 50 адресов, 200 сделок
  python scanner.py --limit 300          # больше сделок на адрес
  python scanner.py --top 100            # больше адресов
  python scanner.py --days 30            # период анализа
  python scanner.py --min-wr 0.85        # жёстче порог WR
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Union
from dataclasses import dataclass, field

import requests

# ────────────────────────────────────────────────────────────────
# HTTP
# ────────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PolymarketScanner/1.0"})

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Задержка между запросами (rate-limit)
REQUEST_DELAY = 0.3


def api_get(url: str, params: Optional[dict] = None, retries: int = 3) -> Optional[Union[list, dict]]:
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"    ⏳ Rate-limit, жду {wait}с...")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            time.sleep(2)
    return None


# ────────────────────────────────────────────────────────────────
# ЗАГРУЗКА АДРЕСОВ С ЛИДЕРБОРДА
# ────────────────────────────────────────────────────────────────

def fetch_leaderboard_addresses(top_n: int = 50) -> list:
    """
    Получает адреса с лидерборда Polymarket.
    Парсит HTML страницы, т.к. JSON API лидерборда недоступен публично.
    """
    print(f"  📥 Загрузка лидерборда (топ {top_n})...")

    addresses = set()

    # Скачиваем страницу лидерборда
    try:
        resp = SESSION.get(
            "https://polymarket.com/leaderboard",
            params={"period": "month"},
            timeout=20,
        )
        if resp.status_code == 200:
            # Извлекаем все 0x-адреса из HTML (42 символа: 0x + 40 hex)
            found = re.findall(r'0x[0-9a-fA-F]{40}', resp.text)
            for addr in found:
                addresses.add(addr.lower())
    except Exception as e:
        print(f"    ⚠️ Ошибка загрузки лидерборда: {e}")

    # Резервный список — топ-20 All-time с лидерборда (апрель 2026)
    FALLBACK_TOP = [
        "0x492442eab586f242b53bda933fd5de859c8a3782",
        "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",
        "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",
        "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",
        "0x019782cab5d844f02bafb71f512758be78579f3c",
        "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",
        "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",
        "0xbddf61af533ff524d27154e589d2d7a81510c684",
        "0xdc876e6873772d38716fda7f2452a78d426d7ab6",
        "0xf195721ad850377c96cd634457c70cd9e8308057",
        "0xb45a797faa52b0fd8adc56d30382022b7b12192c",
        "0x2b3ff45c91540e46fae1e0c72f61f4b049453446",
        "0x93abbc022ce98d6f45d4444b594791cc4b7a9723",
        "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09",
        "0x50b1db131a24a9d9450bbd0372a95d32ea88f076",
        "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6",
        "0xb6d6e99d3bfe055874a04279f659f009fd57be17",
        "0x204f72f35326db932158cba6adff0b9a1da95e14",
        "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
        "0x0682990de7b862979a3cf330a1b088412ecefd43",
        "0x07bdcabf60da99be8fad11092bf4e8412cffe993",
        "0x2663daca3cecf3767ca1c3b126002a8578a8ed1f",
        "0x2785e7022dc20757108204b13c08cea8613b70ae",
        "0x37c1874a60d348903594a96703e0507c518fc53a",
        "0x43e98f912cd6ddadaad88d3297e78c0648e688e5",
        "0x49c2f016b9134f9232d2523bbb63634bf6a75a6d",
        "0x4b27447b0370371b9e2b25be6845d7f144cec899",
        "0x505da8075db50c4fe971aacf4b56cea1289c87b2",
        "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",
        "0x5bec79df9add70a3892041ab1a5516b60f53b215",
        "0x5d05b1f588780423488a09d9aefeb64df54d6320",
        "0x5d189e816b4149be00977c1a3c8840374aec4972",
        "0x61efd00829dbf08e4e80578aab589cb41bf05a75",
        "0x68146921df11eab44296dc4e58025ca84741a9e7",
        "0x9e9c8b080659b08c3474ea761790a20982e26421",
        "0xb2445087e45f114436ee0d4d5edf76347d79edcf",
        "0xc0a04738c05ea55b0a9a42bbcf29c84f702bc10c",
        "0xc21ea96be762bb55041529af6e386e7c53b80215",
        "0xd99f3bec8e060ada0aef0c4057695dd5bc22fcdc",
        "0xe1d6b51521bd4365769199f392f9818661bd907c",
        "0xead152b855effa6b5b5837f53b24c0756830c76a",
        "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
        "0xfe787d2da716d60e8acff57fb87eb13cd4d10319",
    ]
    for addr in FALLBACK_TOP:
        addresses.add(addr.lower())

    result = sorted(addresses)[:top_n]
    print(f"  ✅ Найдено {len(result)} уникальных адресов")
    return result


# ────────────────────────────────────────────────────────────────
# АНАЛИЗ ОТДЕЛЬНОГО ТРЕЙДЕРА
# ────────────────────────────────────────────────────────────────

# Спорт-ключевые слова для определения категории рынка
SPORT_KEYWORDS = [
    "vs.", "vs ", "o/u", "over/under", "total", "spread", "moneyline",
    "btts", "score", "goals", "points", "winner", "match", "game",
    "fc ", " fc", "nba", "nfl", "nhl", "mlb", "mls", "epl", "ucl",
    "uel", "lal", "ser", "bun",  # лига-слаги
    "basketball", "football", "soccer", "baseball", "hockey", "tennis",
    "boxing", "mma", "ufc", "esport",
]

CRYPTO_MICRO_KEYWORDS = [
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana",
    "crypto", "5-min", "5min", "1-min", "1min", "hourly",
]


def is_sport_market(title: str, slug: str) -> bool:
    text = (title + " " + slug).lower()
    return any(kw in text for kw in SPORT_KEYWORDS)


def is_crypto_micro(title: str, slug: str) -> bool:
    text = (title + " " + slug).lower()
    return any(kw in text for kw in CRYPTO_MICRO_KEYWORDS)


@dataclass
class TraderAnalysis:
    address: str
    name: str = ""
    total_activities: int = 0
    buy_trades: int = 0
    resolved_wins: int = 0
    resolved_losses: int = 0
    open_trades: int = 0
    avg_entry_price: float = 0.0
    avg_size_usd: float = 0.0
    median_size_usd: float = 0.0
    sport_pct: float = 0.0
    crypto_micro_pct: float = 0.0
    win_rate: float = 0.0
    consistency_score: float = 0.0  # 0-1, выше = стабильнее
    estimated_pnl: float = 0.0
    estimated_roi_pct: float = 0.0
    trades_per_month: float = 0.0
    value_zone_pct: float = 0.0  # % сделок в диапазоне 0.40-0.70
    categories: dict = field(default_factory=dict)
    entry_prices: list = field(default_factory=list)
    trade_pnls: list = field(default_factory=list)
    score: float = 0.0  # итоговый composite-скор 0-100


def analyze_trader(address: str, limit: int = 200, days: int = 30) -> Optional[TraderAnalysis]:
    """
    Полный анализ трейдера по его истории сделок.
    Возвращает TraderAnalysis или None если данных мало.
    """
    time.sleep(REQUEST_DELAY)

    activities = api_get(f"{DATA_API}/activity", {"user": address, "limit": limit})
    if not activities or not isinstance(activities, list):
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    analysis = TraderAnalysis(address=address)
    analysis.total_activities = len(activities)

    # Имя трейдера (берём из первой активности)
    if activities:
        analysis.name = activities[0].get("name", "") or activities[0].get("pseudonym", "") or address[:10]

    prices = []
    sizes = []
    sport_count = 0
    crypto_micro_count = 0
    value_zone_count = 0
    pnls = []
    total_invested = 0.0

    # Также подгрузим позиции для resolved P&L
    time.sleep(REQUEST_DELAY)
    positions = api_get(f"{DATA_API}/positions", {"user": address, "sizeThreshold": "0"})
    resolved_map = {}  # conditionId -> cashPnl
    if positions and isinstance(positions, list):
        for pos in positions:
            cid = pos.get("conditionId", "")
            cash_pnl = pos.get("cashPnl", 0)
            if cid:
                resolved_map[cid] = {
                    "cashPnl": float(cash_pnl) if cash_pnl else 0,
                    "percentPnl": float(pos.get("percentPnl", 0) or 0),
                    "curPrice": float(pos.get("curPrice", 0) or 0),
                    "redeemable": pos.get("redeemable", False),
                    "avgPrice": float(pos.get("avgPrice", 0) or 0),
                    "initialValue": float(pos.get("initialValue", 0) or 0),
                }

    for act in activities:
        # Фильтр по дате
        ts_raw = act.get("timestamp") or act.get("createdAt") or 0
        try:
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            if ts < cutoff:
                continue
        except (ValueError, TypeError, OSError):
            continue

        trade_type = (act.get("type") or "").upper()
        side = (act.get("side") or "").upper()

        # Считаем только BUY-сделки
        if trade_type not in ("TRADE", "BUY", "PURCHASE"):
            continue
        if side == "SELL":
            continue

        try:
            price = float(act.get("price", 0))
            usdc_size = float(act.get("usdcSize", 0))
        except (TypeError, ValueError):
            continue

        if price <= 0 or usdc_size <= 0:
            continue

        analysis.buy_trades += 1
        prices.append(price)
        sizes.append(usdc_size)
        total_invested += usdc_size

        title = act.get("title", "")
        slug = act.get("slug", "")

        # Категория
        if is_sport_market(title, slug):
            sport_count += 1
        if is_crypto_micro(title, slug):
            crypto_micro_count += 1

        # Value zone (0.40-0.70)
        if 0.40 <= price <= 0.70:
            value_zone_count += 1

        # Проверяем resolved через позиции
        condition_id = act.get("conditionId", "")
        if condition_id in resolved_map:
            pos_data = resolved_map[condition_id]
            if pos_data["curPrice"] == 0 or pos_data["curPrice"] == 1 or pos_data["redeemable"]:
                # Resolved
                trade_pnl = (pos_data["curPrice"] - price) * (usdc_size / price)
                pnls.append(trade_pnl)
                if pos_data["curPrice"] >= price:
                    analysis.resolved_wins += 1
                else:
                    analysis.resolved_losses += 1
            else:
                analysis.open_trades += 1
                trade_pnl = (pos_data["curPrice"] - price) * (usdc_size / price)
                pnls.append(trade_pnl)
        else:
            analysis.open_trades += 1

    # Расчёт метрик
    if analysis.buy_trades < 5:
        return None

    analysis.entry_prices = prices
    analysis.trade_pnls = pnls
    analysis.avg_entry_price = sum(prices) / len(prices) if prices else 0
    analysis.avg_size_usd = sum(sizes) / len(sizes) if sizes else 0
    if sizes:
        sorted_sizes = sorted(sizes)
        mid = len(sorted_sizes) // 2
        analysis.median_size_usd = sorted_sizes[mid]

    analysis.sport_pct = sport_count / analysis.buy_trades if analysis.buy_trades > 0 else 0
    analysis.crypto_micro_pct = crypto_micro_count / analysis.buy_trades if analysis.buy_trades > 0 else 0
    analysis.value_zone_pct = value_zone_count / analysis.buy_trades if analysis.buy_trades > 0 else 0

    resolved_total = analysis.resolved_wins + analysis.resolved_losses
    analysis.win_rate = analysis.resolved_wins / resolved_total if resolved_total > 0 else 0

    analysis.estimated_pnl = sum(pnls) if pnls else 0
    analysis.estimated_roi_pct = (analysis.estimated_pnl / total_invested * 100) if total_invested > 0 else 0
    analysis.trades_per_month = analysis.buy_trades * (30.0 / days)

    # Consistency: стабильность побед (без одной гигантской сделки)
    if len(pnls) >= 3:
        sorted_pnls = sorted(pnls, reverse=True)
        total_profit = sum(p for p in pnls if p > 0)
        if total_profit > 0:
            # Какая доля прибыли приходится на топ-3 сделки?
            top3_profit = sum(max(0, p) for p in sorted_pnls[:3])
            top_concentration = top3_profit / total_profit if total_profit > 0 else 1.0
            analysis.consistency_score = max(0, 1.0 - top_concentration + 0.3)
            analysis.consistency_score = min(1.0, analysis.consistency_score)
        else:
            analysis.consistency_score = 0.0
    else:
        analysis.consistency_score = 0.3

    return analysis


# ────────────────────────────────────────────────────────────────
# СКОРИНГ — composite score (0-100)
# ────────────────────────────────────────────────────────────────

def compute_score(a: TraderAnalysis,
                  min_wr: float = 0.80,
                  min_trades: int = 15,
                  min_avg_size: float = 10.0) -> float:
    """
    Composite score 0-100. Высокий = похож на sayber.

    Веса:
      Win Rate (≥80%)       — 30 баллов
      Value Zone             — 15 баллов (средняя цена 0.40–0.70)
      Активность             — 15 баллов (≥15 сделок/мес)
      Спорт-фокус            — 15 баллов (≥50% спорт-рынки)
      Consistency            — 15 баллов (не зависит от 1-2 сделок)
      Размер позиций         — 10 баллов (≥$10, не микро)
    """
    score = 0.0

    # 1. Win Rate (30 баллов)
    resolved = a.resolved_wins + a.resolved_losses
    if resolved < 5:
        return 0.0  # мало данных
    if a.win_rate >= min_wr:
        # 20 — порог, + до 10 бонус за превышение
        score += 20 + min(10, (a.win_rate - min_wr) * 100)
    else:
        # Частичный балл за WR > 60%
        if a.win_rate >= 0.60:
            score += (a.win_rate - 0.60) / (min_wr - 0.60) * 20
        # Ниже 60% — 0 баллов

    # 2. Value Zone (15 баллов) — средняя цена 0.40–0.70
    if 0.40 <= a.avg_entry_price <= 0.70:
        score += 15
    elif 0.30 <= a.avg_entry_price < 0.40 or 0.70 < a.avg_entry_price <= 0.80:
        score += 8  # рядом
    else:
        score += max(0, 5 - abs(a.avg_entry_price - 0.55) * 20)

    # 3. Активность (15 баллов) — ≥ min_trades/мес
    if a.trades_per_month >= min_trades:
        score += 15
    elif a.trades_per_month >= min_trades * 0.5:
        score += 15 * (a.trades_per_month / min_trades)

    # 4. Спорт-фокус (15 баллов)
    if a.sport_pct >= 0.50:
        score += 15
    elif a.sport_pct >= 0.25:
        score += 15 * (a.sport_pct / 0.50)

    # 5. Consistency (15 баллов)
    score += 15 * a.consistency_score

    # 6. Размер (10 баллов) — не микро-ботик
    if a.avg_size_usd >= min_avg_size:
        score += 10
    elif a.avg_size_usd >= 5:
        score += 10 * (a.avg_size_usd / min_avg_size)

    # Штрафы
    # Много крипто-микро → штраф
    if a.crypto_micro_pct > 0.3:
        score *= 0.7

    # Отрицательный PnL → штраф
    if a.estimated_pnl < 0:
        score *= 0.5

    return round(score, 1)


# ────────────────────────────────────────────────────────────────
# MAIN — ЗАПУСК СКАНЕРА
# ────────────────────────────────────────────────────────────────

def run_scanner(top_n: int = 50, limit: int = 200, days: int = 30,
                min_wr: float = 0.80, min_trades: int = 15,
                min_avg_size: float = 10.0):

    print("=" * 72)
    print( "  🔍 POLYMARKET SCANNER — поиск кошельков похожих на sayber")
    print(f"  Период: {days} дней | Лимит сделок: {limit} | Адресов: {top_n}")
    print(f"  Мин. WR: {min_wr*100:.0f}% | Мин. сделок/мес: {min_trades} | "
          f"Мин. размер: ${min_avg_size}")
    print("=" * 72)

    # 1. Загрузка адресов
    addresses = fetch_leaderboard_addresses(top_n)

    # 2. Анализ каждого
    results = []  # type: list
    skipped = 0

    for i, addr in enumerate(addresses):
        sys.stdout.write(f"\r  🔬 Анализ: {i + 1}/{len(addresses)} ({addr[:10]}...)  ")
        sys.stdout.flush()

        analysis = analyze_trader(addr, limit=limit, days=days)
        if analysis is None:
            skipped += 1
            continue

        analysis.score = compute_score(analysis, min_wr, min_trades, min_avg_size)
        results.append(analysis)

    print(f"\r  ✅ Проанализировано: {len(results)} | Пропущено: {skipped}            ")

    # 3. Ранжирование
    results.sort(key=lambda x: -x.score)

    # 4. Вывод — все с оценкой > 0
    scored = [r for r in results if r.score > 0]

    print(f"\n{'═' * 72}")
    print(f"  РЕЗУЛЬТАТЫ ({len(scored)} кандидатов)")
    print(f"{'═' * 72}\n")

    # Эталон — sayber
    print("  ─── ЭТАЛОН (sayber): WR≥80%, цена 0.40–0.70, спорт≥50%, ≥15 сд/мес ───\n")

    # Таблица топ-кандидатов
    print(f"  {'#':>2s}  {'Score':>5s}  {'Имя':15s}  {'WR':>5s}  {'Сд':>4s}  "
          f"{'Ø цена':>7s}  {'Ø $':>6s}  {'Спорт':>6s}  {'PnL':>10s}  {'ROI':>7s}  "
          f"{'Consist':>7s}")
    print(f"  {'─' * 98}")

    for rank, r in enumerate(scored[:30], 1):
        pnl_s = f"{'+'if r.estimated_pnl>=0 else ''}${r.estimated_pnl:.0f}"
        roi_s = f"{'+'if r.estimated_roi_pct>=0 else ''}{r.estimated_roi_pct:.0f}%"
        name = (r.name or r.address[:10])[:15]
        print(f"  {rank:>2d}  {r.score:>5.1f}  {name:15s}  "
              f"{r.win_rate*100:>4.0f}%  {r.buy_trades:>4d}  "
              f"{r.avg_entry_price:>7.3f}  {r.avg_size_usd:>5.0f}$  "
              f"{r.sport_pct*100:>5.0f}%  {pnl_s:>10s}  {roi_s:>7s}  "
              f"{r.consistency_score:>7.2f}")

    # Топ-5 подробно
    print(f"\n{'─' * 72}")
    print(f"  📋 ПОДРОБНОСТИ — ТОП-5 КАНДИДАТОВ")
    print(f"{'─' * 72}")

    for rank, r in enumerate(scored[:5], 1):
        name = r.name or r.address[:10]
        resolved = r.resolved_wins + r.resolved_losses
        pnl_sign = "+" if r.estimated_pnl >= 0 else ""

        print(f"\n  #{rank} {name} (score: {r.score})")
        print(f"     Адрес:          {r.address}")
        print(f"     BUY-сделок:     {r.buy_trades} ({r.trades_per_month:.0f}/мес)")
        print(f"     Resolved:       {resolved} (W:{r.resolved_wins} L:{r.resolved_losses})")
        print(f"     Win Rate:       {r.win_rate*100:.1f}%")
        print(f"     Сред. цена:     {r.avg_entry_price:.3f}")
        print(f"     Value zone:     {r.value_zone_pct*100:.0f}% сделок в 0.40–0.70")
        print(f"     Сред. размер:   ${r.avg_size_usd:.0f} (медиана: ${r.median_size_usd:.0f})")
        print(f"     Спорт:          {r.sport_pct*100:.0f}%")
        print(f"     Крипто-микро:   {r.crypto_micro_pct*100:.0f}%")
        print(f"     Consistency:    {r.consistency_score:.2f}")
        print(f"     PnL (оценка):   {pnl_sign}${r.estimated_pnl:.2f}")
        print(f"     ROI (оценка):   {pnl_sign}{r.estimated_roi_pct:.1f}%")

        # Для добавления в config.py
        print(f"\n     📎 Для config.py:")
        print(f"     {{")
        print(f"         \"name\": \"{name}\",")
        print(f"         \"address\": \"{r.address}\",")
        print(f"         \"role\": \"COPY\",")
        print(f"         \"strategy\": \"scanner-найден (спорт {r.sport_pct*100:.0f}%)\",")
        print(f"         \"win_rate\": {r.win_rate:.2f},")
        print(f"         \"entry_range\": ({max(0.01, r.avg_entry_price - 0.10):.2f}, "
              f"{min(0.95, r.avg_entry_price + 0.10):.2f}),")
        print(f"     }}")

    # Итого
    print(f"\n{'═' * 72}")
    print(f"  ИТОГО: {len(scored)} кандидатов из {len(results)} проанализированных")
    if scored:
        best = scored[0]
        print(f"  Лучший: {best.name or best.address[:10]} "
              f"(score={best.score}, WR={best.win_rate*100:.0f}%, "
              f"PnL={'+'if best.estimated_pnl>=0 else ''}${best.estimated_pnl:.0f})")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Scanner — поиск трейдеров")
    parser.add_argument("--top", type=int, default=50, help="Кол-во адресов для анализа")
    parser.add_argument("--limit", type=int, default=200, help="Макс. сделок на адрес")
    parser.add_argument("--days", type=int, default=30, help="Период анализа (дней)")
    parser.add_argument("--min-wr", type=float, default=0.80, help="Мин. win rate (0-1)")
    parser.add_argument("--min-trades", type=int, default=15, help="Мин. сделок/мес")
    parser.add_argument("--min-size", type=float, default=10.0, help="Мин. средний размер ($)")
    args = parser.parse_args()

    run_scanner(
        top_n=args.top,
        limit=args.limit,
        days=args.days,
        min_wr=args.min_wr,
        min_trades=args.min_trades,
        min_avg_size=args.min_size,
    )
