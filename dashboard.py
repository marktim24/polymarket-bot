"""
dashboard.py — Веб-дашборд на Flask (порт 5000).

Показывает в реальном времени:
- Статус бота (DRY-RUN / реальный)
- Открытые позиции с текущим PnL
- PnL за сессию
- Последние 10 скопированных сделок
- Параметры риск-менеджмента

Запуск отдельно: python dashboard.py
Или автоматически из bot.py в фоновом потоке.
"""

import threading
import logging
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

import config

logger = logging.getLogger(__name__)

# ============================================================
# HTML ШАБЛОН ДАШБОРДА (встроен в код для single-file решения)
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Copy-Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #0f0f1a;
            color: #e0e0f0;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1a1a2e, #16213e);
            border-bottom: 1px solid #2a2a4a;
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 {
            font-size: 20px;
            font-weight: 700;
            color: #7c83fd;
        }
        .badge {
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }
        .badge-dry { background: #2a2a1a; color: #f0c040; border: 1px solid #f0c040; }
        .badge-live { background: #1a2a1a; color: #40f080; border: 1px solid #40f080; }
        .badge-running { background: #1a2a1a; color: #40f080; }
        .badge-stopped { background: #2a1a1a; color: #f04040; }

        .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-card {
            background: #1a1a2e;
            border: 1px solid #2a2a4a;
            border-radius: 12px;
            padding: 20px;
            text-align: center;
        }
        .stat-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #7080a0;
            margin-bottom: 8px;
        }
        .stat-value {
            font-size: 28px;
            font-weight: 700;
            color: #e0e0f0;
        }
        .stat-value.positive { color: #40c870; }
        .stat-value.negative { color: #f05050; }
        .stat-value.neutral { color: #7c83fd; }

        .section {
            background: #1a1a2e;
            border: 1px solid #2a2a4a;
            border-radius: 12px;
            margin-bottom: 20px;
            overflow: hidden;
        }
        .section-header {
            padding: 14px 20px;
            background: #16162a;
            border-bottom: 1px solid #2a2a4a;
            font-size: 14px;
            font-weight: 600;
            color: #9090c0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th {
            padding: 10px 16px;
            text-align: left;
            font-size: 11px;
            color: #6070a0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid #2a2a4a;
        }
        td {
            padding: 12px 16px;
            font-size: 13px;
            border-bottom: 1px solid #1e1e38;
        }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: #1e1e38; }

        .pnl-pos { color: #40c870; }
        .pnl-neg { color: #f05050; }
        .status-open { color: #40c870; }
        .status-closed { color: #7080a0; }
        .status-stop { color: #f05050; }

        .mono { font-family: 'Courier New', monospace; font-size: 11px; color: #9090c0; }

        .traders-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
            padding: 16px;
        }
        .trader-card {
            background: #16162a;
            border: 1px solid #2a2a4a;
            border-radius: 8px;
            padding: 16px;
        }
        .trader-name {
            font-size: 16px;
            font-weight: 600;
            color: #7c83fd;
            margin-bottom: 8px;
        }
        .trader-row {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            margin-bottom: 4px;
            color: #8090b0;
        }
        .trader-row span:last-child { color: #c0d0e0; }

        .config-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
            padding: 16px;
        }
        .config-item {
            display: flex;
            justify-content: space-between;
            padding: 10px 14px;
            background: #16162a;
            border-radius: 6px;
            font-size: 13px;
        }
        .config-key { color: #7080a0; }
        .config-val { color: #a0c0f0; font-weight: 600; }

        .empty-msg {
            padding: 32px;
            text-align: center;
            color: #404070;
            font-size: 14px;
        }

        .refresh-info {
            font-size: 12px;
            color: #404060;
            text-align: right;
            padding: 8px 0;
        }

        .top-bar {
            display: flex;
            gap: 12px;
            align-items: center;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 Polymarket Copy-Bot Dashboard</h1>
        <div class="top-bar">
            <span id="mode-badge" class="badge">...</span>
            <span id="status-badge" class="badge">...</span>
        </div>
    </div>

    <div class="container">
        <p class="refresh-info">Обновление каждые 5 секунд | <span id="last-update">-</span></p>

        <!-- Основные метрики -->
        <div class="stats-grid" id="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Скопировано сделок</div>
                <div class="stat-value neutral" id="s-copied">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Пропущено</div>
                <div class="stat-value" id="s-skipped">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Открытых позиций</div>
                <div class="stat-value neutral" id="s-open">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Открыто в USD</div>
                <div class="stat-value" id="s-exposure">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Нереализованный PnL</div>
                <div class="stat-value" id="s-unrealized">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Реализованный PnL</div>
                <div class="stat-value" id="s-realized">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Итоговый PnL</div>
                <div class="stat-value" id="s-total-pnl">-</div>
            </div>
        </div>

        <!-- Открытые позиции -->
        <div class="section">
            <div class="section-header">📈 Открытые позиции</div>
            <div id="positions-container">
                <div class="empty-msg">Нет открытых позиций</div>
            </div>
        </div>

        <!-- Последние сделки -->
        <div class="section">
            <div class="section-header">🕒 Последние 10 сделок</div>
            <div id="trades-container">
                <div class="empty-msg">Нет скопированных сделок</div>
            </div>
        </div>

        <!-- Трейдеры -->
        <div class="section">
            <div class="section-header">👀 Отслеживаемые трейдеры</div>
            <div class="traders-grid" id="traders-grid"></div>
        </div>

        <!-- Конфигурация -->
        <div class="section">
            <div class="section-header">⚙️ Параметры риск-менеджмента</div>
            <div class="config-grid" id="config-grid"></div>
        </div>
    </div>

    <script>
        function fmtUsd(v) {
            return (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
        }
        function pnlClass(v) {
            if (v > 0) return 'pnl-pos';
            if (v < 0) return 'pnl-neg';
            return '';
        }
        function fmtDt(iso) {
            if (!iso) return '—';
            return new Date(iso).toLocaleString('ru-RU', {
                month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
        }

        async function refresh() {
            try {
                const r = await fetch('/api/status');
                const d = await r.json();

                // Режим и статус
                const modeBadge = document.getElementById('mode-badge');
                if (d.dry_run) {
                    modeBadge.textContent = '🔒 DRY-RUN';
                    modeBadge.className = 'badge badge-dry';
                } else {
                    modeBadge.textContent = '💰 РЕАЛЬНЫЙ';
                    modeBadge.className = 'badge badge-live';
                }
                const statusBadge = document.getElementById('status-badge');
                if (d.running) {
                    statusBadge.textContent = '● РАБОТАЕТ';
                    statusBadge.className = 'badge badge-running';
                } else {
                    statusBadge.textContent = '● ОСТАНОВЛЕН';
                    statusBadge.className = 'badge badge-stopped';
                }

                // Метрики
                const s = d.stats;
                document.getElementById('s-copied').textContent = s.total_copied;
                document.getElementById('s-skipped').textContent = s.total_skipped;
                document.getElementById('s-open').textContent = s.open_positions;
                document.getElementById('s-exposure').textContent = '$' + s.total_exposure_usd.toFixed(2);

                const uPnl = document.getElementById('s-unrealized');
                uPnl.textContent = fmtUsd(s.unrealized_pnl);
                uPnl.className = 'stat-value ' + pnlClass(s.unrealized_pnl);

                const rPnl = document.getElementById('s-realized');
                rPnl.textContent = fmtUsd(s.realized_pnl);
                rPnl.className = 'stat-value ' + pnlClass(s.realized_pnl);

                const tPnl = document.getElementById('s-total-pnl');
                tPnl.textContent = fmtUsd(s.total_pnl);
                tPnl.className = 'stat-value ' + pnlClass(s.total_pnl);

                // Открытые позиции
                const posContainer = document.getElementById('positions-container');
                if (d.open_positions.length === 0) {
                    posContainer.innerHTML = '<div class="empty-msg">Нет открытых позиций</div>';
                } else {
                    let html = '<table><thead><tr>' +
                        '<th>Ордер</th><th>Трейдер</th><th>Рынок</th>' +
                        '<th>Вход</th><th>Сейчас</th><th>Размер</th>' +
                        '<th>PnL</th><th>Открыта</th>' +
                        '</tr></thead><tbody>';
                    for (const p of d.open_positions) {
                        const pnlCls = p.unrealized_pnl > 0 ? 'pnl-pos' : (p.unrealized_pnl < 0 ? 'pnl-neg' : '');
                        html += `<tr>
                            <td class="mono">${p.order_id.substring(0,16)}…</td>
                            <td>${p.trader_name}</td>
                            <td class="mono">${(p.market_slug || p.token_id.substring(0,14) + '…')}</td>
                            <td>${p.entry_price.toFixed(4)}</td>
                            <td>${p.current_price > 0 ? p.current_price.toFixed(4) : '—'}</td>
                            <td>$${p.size_usd.toFixed(2)}</td>
                            <td class="${pnlCls}">${fmtUsd(p.unrealized_pnl)}</td>
                            <td>${fmtDt(p.opened_at)}</td>
                        </tr>`;
                    }
                    html += '</tbody></table>';
                    posContainer.innerHTML = html;
                }

                // Последние сделки
                const tradesContainer = document.getElementById('trades-container');
                if (d.recent_trades.length === 0) {
                    tradesContainer.innerHTML = '<div class="empty-msg">Нет скопированных сделок</div>';
                } else {
                    let html = '<table><thead><tr>' +
                        '<th>Ордер</th><th>Трейдер</th><th>Рынок</th>' +
                        '<th>Цена</th><th>Размер</th><th>PnL</th>' +
                        '<th>Статус</th><th>Время</th>' +
                        '</tr></thead><tbody>';
                    for (const t of d.recent_trades) {
                        const pnlVal = t.unrealized_pnl + t.realized_pnl;
                        const pnlCls = pnlVal > 0 ? 'pnl-pos' : (pnlVal < 0 ? 'pnl-neg' : '');
                        const stCls = t.status === 'open' ? 'status-open' :
                                     (t.status === 'stop_loss' ? 'status-stop' : 'status-closed');
                        html += `<tr>
                            <td class="mono">${t.order_id.substring(0,14)}…</td>
                            <td>${t.trader_name}</td>
                            <td class="mono">${(t.market_slug || t.token_id.substring(0,14) + '…')}</td>
                            <td>${t.entry_price.toFixed(4)}</td>
                            <td>$${t.size_usd.toFixed(2)}</td>
                            <td class="${pnlCls}">${fmtUsd(pnlVal)}</td>
                            <td class="${stCls}">${t.status}</td>
                            <td>${fmtDt(t.opened_at)}</td>
                        </tr>`;
                    }
                    html += '</tbody></table>';
                    tradesContainer.innerHTML = html;
                }

                // Трейдеры
                const tradersGrid = document.getElementById('traders-grid');
                let tHtml = '';
                for (const t of (d.monitor.traders || [])) {
                    tHtml += `<div class="trader-card">
                        <div class="trader-name">👤 ${t.name}</div>
                        <div class="trader-row">
                            <span>Адрес</span>
                            <span class="mono">${t.address.substring(0,10)}…</span>
                        </div>
                        <div class="trader-row">
                            <span>Обнаружено сделок</span>
                            <span>${t.total_detected}</span>
                        </div>
                        <div class="trader-row">
                            <span>Последний опрос</span>
                            <span>${fmtDt(t.last_poll)}</span>
                        </div>
                        ${t.last_error ? `<div class="trader-row"><span>⚠️ Ошибка</span><span style="color:#f07050">${t.last_error}</span></div>` : ''}
                    </div>`;
                }
                tradersGrid.innerHTML = tHtml || '<div class="empty-msg">Нет трейдеров</div>';

                // Конфиг
                const configGrid = document.getElementById('config-grid');
                const cfg = d.config;
                const cfgItems = [
                    ['MAX_POSITION_USD', '$' + cfg.max_position_usd],
                    ['COPY_RATIO', (cfg.copy_ratio * 100).toFixed(0) + '%'],
                    ['STOP_LOSS', ((1 - cfg.stop_loss_percent) * 100).toFixed(0) + '% падение'],
                    ['MAX_POSITIONS', cfg.max_open_positions],
                    ['POLL_INTERVAL', cfg.poll_interval_sec + ' сек'],
                ];
                configGrid.innerHTML = cfgItems.map(([k, v]) =>
                    `<div class="config-item"><span class="config-key">${k}</span><span class="config-val">${v}</span></div>`
                ).join('');

                document.getElementById('last-update').textContent =
                    'Обновлено: ' + new Date().toLocaleTimeString('ru-RU');

            } catch (e) {
                console.error('Ошибка обновления:', e);
            }
        }

        // Первый запрос сразу
        refresh();
        // Затем каждые 5 секунд
        setInterval(refresh, 5000);
    </script>
</body>
</html>
"""


# ============================================================
# FLASK ПРИЛОЖЕНИЕ
# ============================================================

# Глобальный экземпляр бота (устанавливается из bot.py)
_bot_instance = None


def create_app(bot_instance=None) -> Flask:
    """
    Создаёт Flask приложение.

    Args:
        bot_instance: экземпляр PolymarketCopyBot для получения данных.
                      Если None — возвращает заглушку данных.
    """
    global _bot_instance
    if bot_instance is not None:
        _bot_instance = bot_instance

    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)

    @app.route("/")
    def index():
        """Главная страница дашборда."""
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/status")
    def api_status():
        """JSON API: возвращает все данные для дашборда."""
        if _bot_instance is not None:
            data = _bot_instance.get_dashboard_data()
        else:
            # Заглушка если бот не запущен
            data = _get_stub_data()
        return jsonify(data)

    @app.route("/api/positions")
    def api_positions():
        """JSON API: только открытые позиции."""
        if _bot_instance is not None:
            positions = [
                p.to_dict()
                for p in _bot_instance.risk_manager.get_open_positions()
            ]
        else:
            positions = []
        return jsonify({"positions": positions})

    @app.route("/api/stats")
    def api_stats():
        """JSON API: статистика сессии."""
        if _bot_instance is not None:
            stats = _bot_instance.risk_manager.get_session_stats()
        else:
            stats = {}
        return jsonify(stats)

    @app.route("/health")
    def health():
        """Проверка живости (для мониторинга)."""
        return jsonify({
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_running": _bot_instance._running if _bot_instance else False,
        })

    return app


def _get_stub_data() -> dict:
    """Заглушка данных для запуска дашборда без бота."""
    return {
        "running": False,
        "dry_run": config.DRY_RUN,
        "stats": {
            "total_copied": 0,
            "total_skipped": 0,
            "open_positions": 0,
            "closed_positions": 0,
            "total_exposure_usd": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "total_pnl": 0.0,
        },
        "monitor": {"total_polls": 0, "start_time": None, "traders": []},
        "open_positions": [],
        "recent_trades": [],
        "config": {
            "max_position_usd": config.MAX_POSITION_USD,
            "copy_ratio": config.COPY_RATIO,
            "stop_loss_percent": config.STOP_LOSS_PERCENT,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "poll_interval_sec": config.POLL_INTERVAL_SEC,
        },
    }


def run_dashboard(bot_instance=None, host: str = None, port: int = None):
    """
    Запускает Flask-дашборд в фоновом потоке.

    Args:
        bot_instance: экземпляр бота для получения данных
        host: хост для привязки (по умолчанию из config)
        port: порт (по умолчанию из config)
    """
    _host = host or config.DASHBOARD_HOST
    _port = port or config.DASHBOARD_PORT

    app = create_app(bot_instance)

    def _run():
        logger.info(
            "🌐 Дашборд запущен: http://%s:%d",
            "localhost" if _host == "0.0.0.0" else _host,
            _port,
        )
        app.run(
            host=_host,
            port=_port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    thread = threading.Thread(target=_run, name="DashboardThread", daemon=True)
    thread.start()
    return thread


# ============================================================
# ЗАПУСК КАК ОТДЕЛЬНЫЙ СКРИПТ (только дашборд, без бота)
# ============================================================

if __name__ == "__main__":
    import sys
    import logging as _logging

    _logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"🌐 Запуск дашборда на http://localhost:{config.DASHBOARD_PORT}")
    print("   Для полноценной работы запустите: python bot.py")
    print("   Ctrl+C для остановки\n")

    app = create_app(bot_instance=None)
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=False,
        use_reloader=False,
    )
