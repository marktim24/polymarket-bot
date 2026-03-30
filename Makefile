# ============================================================
# Makefile для Polymarket Copy-Trading Bot
# ============================================================
# Использование:
#   make install  — установить зависимости
#   make run      — запустить бота
#   make test     — dry-run тест без реальных сделок
#   make dashboard — только веб-дашборд
#   make logs     — последние 50 строк лога
#   make clean    — очистить кэш Python
# ============================================================

.PHONY: install run test dashboard logs clean check-env

# Путь к Python (автоопределение)
PYTHON := $(shell which python3 || which python)
PIP    := $(shell which pip3 || which pip)
LOG_FILE := logs/bot.log

# Цвета для вывода
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
NC     := \033[0m  # No Color

## install — установить все зависимости из requirements.txt
install:
	@echo "$(GREEN)📦 Установка зависимостей...$(NC)"
	@$(PIP) install -r requirements.txt
	@echo "$(GREEN)✅ Зависимости установлены$(NC)"
	@echo ""
	@echo "$(YELLOW)Следующий шаг:$(NC)"
	@echo "  1. cp .env.example .env"
	@echo "  2. Заполните .env своими данными"
	@echo "  3. make test   (проверка без реальных сделок)"
	@echo "  4. make run    (реальный запуск)"

## check-env — проверить наличие .env файла
check-env:
	@if [ ! -f .env ]; then \
		echo "$(RED)❌ Файл .env не найден!$(NC)"; \
		echo "   Выполните: cp .env.example .env"; \
		echo "   Затем заполните реальными данными"; \
		exit 1; \
	fi
	@echo "$(GREEN)✅ .env файл найден$(NC)"

## run — запустить бота (читает настройки из .env)
run: check-env
	@echo "$(GREEN)🚀 Запуск Polymarket Copy-Trading Bot...$(NC)"
	@echo "   Дашборд: http://localhost:5000"
	@echo "   Для остановки: Ctrl+C"
	@echo ""
	@mkdir -p logs
	@$(PYTHON) bot.py

## test — запустить в dry-run режиме (без реальных сделок)
test: check-env
	@echo "$(YELLOW)🔒 Запуск в DRY-RUN режиме (тест подключений)...$(NC)"
	@echo "   Реальные ордера НЕ будут размещены"
	@mkdir -p logs
	@DRY_RUN=true $(PYTHON) bot.py

## dashboard — только веб-дашборд (без бота)
dashboard:
	@echo "$(GREEN)🌐 Запуск дашборда на http://localhost:5000$(NC)"
	@$(PYTHON) dashboard.py

## logs — показать последние 50 строк лога
logs:
	@if [ -f $(LOG_FILE) ]; then \
		echo "$(GREEN)📋 Последние 50 строк $(LOG_FILE):$(NC)"; \
		tail -n 50 $(LOG_FILE); \
	else \
		echo "$(YELLOW)⚠️  Лог-файл не найден: $(LOG_FILE)$(NC)"; \
		echo "   Запустите бота: make run"; \
	fi

## follow — следить за логом в реальном времени
follow:
	@if [ -f $(LOG_FILE) ]; then \
		echo "$(GREEN)📋 Слежение за $(LOG_FILE) (Ctrl+C для выхода):$(NC)"; \
		tail -f $(LOG_FILE); \
	else \
		echo "$(YELLOW)⚠️  Лог-файл не найден$(NC)"; \
	fi

## clean — очистить кэш Python
clean:
	@echo "$(YELLOW)🧹 Очистка кэша...$(NC)"
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@find . -name "*.pyo" -delete 2>/dev/null || true
	@echo "$(GREEN)✅ Готово$(NC)"

## help — показать список команд
help:
	@echo ""
	@echo "$(GREEN)Polymarket Copy-Trading Bot$(NC)"
	@echo "=============================="
	@echo ""
	@echo "$(YELLOW)Доступные команды:$(NC)"
	@echo "  make install   — установить зависимости"
	@echo "  make run       — запустить бота"
	@echo "  make test      — dry-run тест"
	@echo "  make dashboard — только веб-дашборд"
	@echo "  make logs      — последние 50 строк лога"
	@echo "  make follow    — следить за логом"
	@echo "  make clean     — очистить кэш Python"
	@echo ""

.DEFAULT_GOAL := help
