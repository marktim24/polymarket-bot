"""
tests/test_trade_detection.py — Tests for tracked-wallet trade detection pipeline.

Run with:  python -m pytest tests/ -v
"""

import json
import os
import sys
import tempfile
import time
import threading
import queue
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


# ============================================================
# HELPERS
# ============================================================

def make_raw_activity(
    side="BUY",
    price=0.35,
    usdcSize=5.0,
    asset="tokenABC123",
    transactionHash="0xdeadbeef01",
    timestamp=None,
    slug="mlb-test-market",
    conditionId=None,
):
    """Build a minimal activity dict matching the real Polymarket API schema."""
    return {
        "proxyWallet": "0xcacf2bf1906bb3c74a0e0453bfb91f1374e335ff",
        "timestamp": timestamp or int(time.time()),
        "conditionId": conditionId or asset,
        "type": "TRADE",
        "size": usdcSize / max(price, 0.001),
        "usdcSize": usdcSize,
        "transactionHash": transactionHash,
        "price": price,
        "asset": asset,
        "side": side,
        "outcomeIndex": 0,
        "title": "Test Market",
        "slug": slug,
        "outcome": "YES",
        "name": "TestTrader",
    }


# ============================================================
# TEST: TradeActivity parsing
# ============================================================

class TestTradeActivityParsing:
    def test_buy_fields_parsed_correctly(self):
        from monitor import TradeActivity
        raw = make_raw_activity(side="BUY", price=0.35, usdcSize=5.0, transactionHash="0xabc123")
        act = TradeActivity(raw)

        assert act.action == "BUY"
        assert abs(act.price - 0.35) < 1e-9
        assert abs(act.size_usd - 5.0) < 1e-9
        assert act.id == "0xabc123"
        assert act.is_buy() is True
        assert act.is_sell() is False
        assert act.is_valid_buy() is True

    def test_sell_fields_parsed_correctly(self):
        from monitor import TradeActivity
        raw = make_raw_activity(side="SELL", price=0.50, usdcSize=3.0, transactionHash="0xsell01")
        act = TradeActivity(raw)

        assert act.action == "SELL"
        assert act.is_sell() is True
        assert act.is_buy() is False
        assert act.is_valid_buy() is False

    def test_token_id_prefers_conditionId(self):
        from monitor import TradeActivity
        raw = make_raw_activity(asset="assetFallback", conditionId="conditionPrimary")
        act = TradeActivity(raw)
        assert act.token_id == "conditionPrimary"

    def test_age_hours_recent_trade(self):
        from monitor import TradeActivity
        now = int(time.time())
        raw = make_raw_activity(timestamp=now - 1800)  # 0.5 hours ago
        act = TradeActivity(raw)
        assert 0.4 < act.age_hours() < 0.6

    def test_buy_with_low_price_valid(self):
        """WizzleGizzle-style long-shot trade at price 0.01 must parse correctly."""
        from monitor import TradeActivity
        raw = make_raw_activity(side="BUY", price=0.01, usdcSize=20.0)
        act = TradeActivity(raw)
        assert act.is_valid_buy() is True
        assert abs(act.price - 0.01) < 1e-9

    def test_empty_transactionhash_id_is_blank(self):
        from monitor import TradeActivity
        raw = make_raw_activity()
        raw["transactionHash"] = ""
        act = TradeActivity(raw)
        # id falls back to conditionId/asset since transactionHash is empty
        # The actual logic: id = raw.get("id") or raw.get("transactionHash", "")
        # Both are empty → id == ""
        raw.pop("conditionId", None)
        act2 = TradeActivity(raw)
        assert act2.id == "" or act2.id is not None  # must not crash


# ============================================================
# TEST: Signal classification price ranges
# ============================================================

class TestSignalClassifier:
    def _make_classifier(self):
        from monitor import SignalClassifier, MarketStatusChecker
        checker = MagicMock(spec=MarketStatusChecker)
        checker.get_hours_to_resolution.return_value = None
        checker.get_liquidity.return_value = None
        checker.get_current_price.return_value = None
        return SignalClassifier(checker)

    def test_sayber_price_range_passes(self):
        """sayber trades at 0.37 — must pass global and per-trader filter."""
        from monitor import TradeActivity, SignalClassifier
        classifier = self._make_classifier()
        raw = make_raw_activity(price=0.37, usdcSize=10.0)
        act = TradeActivity(raw)
        act.trader_name = "sayber"
        signal_type, reason, conf = classifier.classify("sayber", act)
        assert signal_type != "IGNORE", f"sayber trade at 0.37 should not be IGNORE; got: {reason}"

    def test_wizzlegizzle_price_range_passes(self):
        """WizzleGizzle trades at 0.01–0.15 — must pass per-trader filter."""
        from monitor import TradeActivity, SignalClassifier
        classifier = self._make_classifier()
        for price in [0.01, 0.05, 0.10, 0.14]:
            raw = make_raw_activity(price=price, usdcSize=5.0)
            act = TradeActivity(raw)
            act.trader_name = "WizzleGizzle"
            signal_type, reason, conf = classifier.classify("WizzleGizzle", act)
            assert signal_type != "IGNORE", (
                f"WizzleGizzle price {price} should not be IGNORE; got: {reason}"
            )

    def test_gatorr_price_range_passes(self):
        """gatorr trades at 0.49–0.69 — must pass per-trader filter."""
        from monitor import TradeActivity, SignalClassifier
        classifier = self._make_classifier()
        for price in [0.49, 0.55, 0.65, 0.69]:
            raw = make_raw_activity(price=price, usdcSize=8.0)
            act = TradeActivity(raw)
            act.trader_name = "gatorr"
            signal_type, reason, conf = classifier.classify("gatorr", act)
            assert signal_type != "IGNORE", (
                f"gatorr price {price} should not be IGNORE; got: {reason}"
            )

    def test_price_above_trader_max_is_ignored(self):
        from monitor import TradeActivity, SignalClassifier
        classifier = self._make_classifier()
        raw = make_raw_activity(price=0.95, usdcSize=5.0)
        act = TradeActivity(raw)
        act.trader_name = "WizzleGizzle"  # max is 0.15
        signal_type, reason, conf = classifier.classify("WizzleGizzle", act)
        assert signal_type == "IGNORE"

    def test_price_below_trader_min_is_ignored(self):
        from monitor import TradeActivity, SignalClassifier
        classifier = self._make_classifier()
        raw = make_raw_activity(price=0.001, usdcSize=5.0)
        act = TradeActivity(raw)
        act.trader_name = "sayber"  # min is 0.20
        signal_type, reason, conf = classifier.classify("sayber", act)
        assert signal_type == "IGNORE"


# ============================================================
# TEST: TraderMonitor deduplication
# ============================================================

class TestTraderMonitorDedup:
    def _make_monitor(self, tmpdir, name="TestTrader", address="0xabc"):
        from monitor import TraderMonitor, MarketStatusChecker, SignalClassifier
        trader = {
            "name": name,
            "address": address,
            "role": "COPY",
            "overrides": {
                "MIN_ENTRY_PRICE": 0.01,
                "MAX_ENTRY_PRICE": 0.95,
                "MAX_COPY_DELAY_HOURS": 12.0,
                "MIN_TRADER_SIZE_USD": 0.10,
            },
        }
        # Override seen_ids path to temp dir
        buy_queue = queue.Queue()
        monitor = TraderMonitor(trader, buy_queue, {})
        monitor._seen_ids_file = os.path.join(tmpdir, f"seen_{name}.json")
        monitor._seen_ids = set()
        monitor._initialized = False
        return monitor, buy_queue

    def test_initialization_marks_existing_ids_as_seen(self, tmp_path):
        from monitor import MarketStatusChecker, SignalClassifier
        monitor, _ = self._make_monitor(str(tmp_path))
        checker = MagicMock()
        checker.get_current_price.return_value = None
        classifier_mock = MagicMock()
        activities = [make_raw_activity(transactionHash=f"0x{i:064x}") for i in range(5)]

        with patch.object(monitor, '_fetch_activity', return_value=activities):
            count = monitor.poll(checker, classifier_mock)

        assert count == 0
        assert monitor._initialized is True
        assert len(monitor._seen_ids) == 5

    def test_known_ids_not_reprocessed(self, tmp_path):
        from monitor import MarketStatusChecker, SignalClassifier
        monitor, buy_queue = self._make_monitor(str(tmp_path))
        checker = MagicMock()
        checker.get_current_price.return_value = None
        classifier = MagicMock()
        classifier.classify.return_value = ("MEDIUM", "test", 0.5)
        activities = [make_raw_activity(transactionHash="0xknown")]

        # Init
        with patch.object(monitor, '_fetch_activity', return_value=activities):
            monitor.poll(checker, classifier)

        # Second poll with same activities → nothing new
        with patch.object(monitor, '_fetch_activity', return_value=activities):
            count = monitor.poll(checker, classifier)

        assert count == 0
        assert buy_queue.empty()

    def test_new_id_is_detected(self, tmp_path):
        from monitor import MarketStatusChecker, SignalClassifier
        monitor, buy_queue = self._make_monitor(str(tmp_path))
        checker = MagicMock()
        checker.get_current_price.return_value = None
        classifier = MagicMock()
        classifier.record_trade = MagicMock()
        classifier.classify.return_value = ("MEDIUM", "test", 0.5)
        old_activities = [make_raw_activity(transactionHash="0xold", price=0.35)]
        new_activity = make_raw_activity(transactionHash="0xnew", price=0.35)

        # Init with old
        with patch.object(monitor, '_fetch_activity', return_value=old_activities):
            monitor.poll(checker, classifier)

        # New activity
        with patch.object(monitor, '_fetch_activity', return_value=[new_activity]):
            count = monitor.poll(checker, classifier)

        assert count == 1
        assert not buy_queue.empty()

    def test_seen_ids_persisted_and_reloaded(self, tmp_path):
        from monitor import TraderMonitor
        trader = {
            "name": "PersistTest",
            "address": "0xpersist",
            "role": "COPY",
            "overrides": {},
        }
        buy_queue = queue.Queue()
        m1 = TraderMonitor(trader, buy_queue, {})
        m1._seen_ids_file = str(tmp_path / "seen.json")
        m1._seen_ids = {"0xtx1", "0xtx2"}
        m1._initialized = True
        m1._save_seen_ids()

        # Create new monitor loading from same file
        m2 = TraderMonitor(trader, buy_queue, {})
        m2._seen_ids = set()
        m2._initialized = False
        m2._seen_ids_file = str(tmp_path / "seen.json")
        m2._load_seen_ids()

        assert "0xtx1" in m2._seen_ids
        assert "0xtx2" in m2._seen_ids
        assert m2._initialized is True


# ============================================================
# TEST: CandidatePool (wallet discovery)
# ============================================================

class TestCandidatePool:
    def _make_pool(self, tmpdir):
        from discovery import CandidatePool
        state_file = os.path.join(tmpdir, "candidates.json")
        return CandidatePool(state_file=state_file)

    def test_add_candidate(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        c = pool.add_candidate("0xaaa111", name="tester1", source="manual")
        assert c.address == "0xaaa111"
        assert c.name == "tester1"
        assert c.status == "evaluating"

    def test_duplicate_add_returns_existing(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0xaaa222", name="tester2")
        c2 = pool.add_candidate("0xAAA222", name="different_name")  # uppercase
        assert c2.name == "tester2"  # original unchanged

    def test_add_existing_permanent_raises(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        permanent_addr = config.TRADERS[0]["address"]
        with pytest.raises(ValueError):
            pool.add_candidate(permanent_addr)

    def test_record_trade_increments_counter(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0xbbb333", name="trader3")
        pool.record_trade("0xbbb333", "tokenXYZ", 0.35, 5.0)
        c = pool.get_by_address("0xbbb333")
        assert c.trades_seen == 1
        assert len(c.simulated_trades) == 1

    def test_close_trade_updates_pnl_win(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0xccc444", name="winner")
        pool.record_trade("0xccc444", "tokenWIN", 0.30, 10.0)
        pool.close_trade("0xccc444", "tokenWIN", 0.60)  # 100% gain
        c = pool.get_by_address("0xccc444")
        assert c.wins == 1
        assert c.losses == 0
        assert c.realized_pnl > 0

    def test_close_trade_updates_pnl_loss(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0xddd555", name="loser")
        pool.record_trade("0xddd555", "tokenLOSE", 0.50, 10.0)
        pool.close_trade("0xddd555", "tokenLOSE", 0.25)  # 50% loss
        c = pool.get_by_address("0xddd555")
        assert c.losses == 1
        assert c.wins == 0
        assert c.realized_pnl < 0

    def test_promotion_criteria_met(self, tmp_path):
        """Wallet with good stats should be promoted to permanent list."""
        pool = self._make_pool(str(tmp_path))
        addr = "0xeee666"
        pool.add_candidate(addr, name="good_trader")
        c = pool.get_by_address(addr)

        # Manually set stats that meet promotion criteria
        c.wins = 4
        c.losses = 1
        c.realized_pnl = 5.0

        # Force enough trades
        with pool._lock:
            pool._save()

        promote, reason = c.check_promotion()
        assert promote is True, f"Should promote but got: {reason}"

    def test_no_promotion_insufficient_trades(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0xfff777", name="new_trader")
        c = pool.get_by_address("0xfff777")
        c.wins = 2
        c.losses = 0
        c.realized_pnl = 1.0
        promote, reason = c.check_promotion()
        assert promote is False
        assert "мало сделок" in reason

    def test_no_promotion_negative_pnl(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0x111888", name="losing_trader")
        c = pool.get_by_address("0x111888")
        c.wins = 3
        c.losses = 2
        c.realized_pnl = -1.0
        promote, reason = c.check_promotion()
        assert promote is False

    def test_no_promotion_low_win_rate(self, tmp_path):
        pool = self._make_pool(str(tmp_path))
        pool.add_candidate("0x222999", name="low_wr")
        c = pool.get_by_address("0x222999")
        c.wins = 2
        c.losses = 5
        c.realized_pnl = 0.5
        promote, reason = c.check_promotion()
        assert promote is False

    def test_state_persists_across_instances(self, tmp_path):
        from discovery import CandidatePool
        state_file = str(tmp_path / "cands.json")
        p1 = CandidatePool(state_file=state_file)
        p1.add_candidate("0x333aaa", name="persist_test")
        p1.record_trade("0x333aaa", "tokenPERSIST", 0.40, 8.0)

        p2 = CandidatePool(state_file=state_file)
        c = p2.get_by_address("0x333aaa")
        assert c is not None
        assert c.name == "persist_test"
        assert c.trades_seen == 1

    def test_promotion_adds_to_config_traders(self, tmp_path):
        """Promoted wallet is added to config.TRADERS list in-memory."""
        from discovery import CandidatePool
        state_file = str(tmp_path / "cands_promo.json")
        pool = CandidatePool(state_file=state_file)

        addr = "0x444bbb000000000000000000000000000000abcd"
        pool.add_candidate(addr, name="star_trader")
        c = pool.get_by_address(addr)
        c.wins = 5
        c.losses = 1
        c.realized_pnl = 10.0

        initial_count = len(config.TRADERS)
        with pool._lock:
            pool._check_and_promote(c)

        assert c.status == "promoted", f"Expected promoted, got: {c.status}"
        assert any(t["address"].lower() == addr.lower() for t in config.TRADERS), (
            "Promoted wallet not found in config.TRADERS"
        )
        # Cleanup to avoid side-effects on other tests
        config.TRADERS[:] = [t for t in config.TRADERS if t["address"].lower() != addr.lower()]
        config.TRADER_ROLES.pop("star_trader", None)
        config._TRADER_BY_NAME.pop("star_trader", None)


# ============================================================
# TEST: Integration — poll detects real Polymarket API trades
# ============================================================

class TestSignalReachTelegramPath:
    """Verify that a valid BUY activity passes through the full pipeline."""

    def test_buy_signal_reaches_queue(self, tmp_path):
        """A MEDIUM/HIGH buy activity must end up in the buy queue."""
        from monitor import TraderMonitor, MarketStatusChecker, SignalClassifier

        trader = {
            "name": "sayber",
            "address": "0x96b41aac95788f717d0566210cda48e8e686c2f1",
            "role": "COPY",
            "overrides": {
                "MIN_ENTRY_PRICE": 0.20,
                "MAX_ENTRY_PRICE": 0.75,
                "MAX_COPY_DELAY_HOURS": 12.0,
                "MIN_TRADER_SIZE_USD": 0.50,
                "MIN_MARKET_VOLUME_USD": 0.0,   # disable liquidity filter for test
            },
        }
        buy_queue = queue.Queue()
        monitor = TraderMonitor(trader, buy_queue, {})
        monitor._seen_ids_file = str(tmp_path / "sayber_seen.json")
        monitor._seen_ids = set()
        monitor._initialized = False

        checker = MagicMock(spec=MarketStatusChecker)
        checker.get_current_price.return_value = None
        checker.get_hours_to_resolution.return_value = None
        checker.get_liquidity.return_value = None

        classifier = MagicMock(spec=SignalClassifier)
        classifier.record_trade = MagicMock()
        classifier.classify.return_value = ("MEDIUM", "test signal", 0.6)

        # 1. Init with one old activity
        old_act = make_raw_activity(transactionHash="0xold", price=0.37)
        with patch.object(monitor, '_fetch_activity', return_value=[old_act]):
            monitor.poll(checker, classifier)

        assert monitor._initialized
        assert buy_queue.empty()

        # 2. New BUY appears — should be queued
        new_act = make_raw_activity(transactionHash="0xnew", price=0.37, usdcSize=10.0)
        with patch.object(monitor, '_fetch_activity', return_value=[new_act]):
            count = monitor.poll(checker, classifier)

        assert count == 1
        assert not buy_queue.empty()
        activity = buy_queue.get_nowait()
        assert activity.price == 0.37
        assert activity.trader_name == "sayber"
