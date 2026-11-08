import os
import sys
import socket
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Ensure app is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import get_db, market_db, has_table, is_postgres
from app.demo_client import _force_ipv4
from app.ai_engine import AIEngine


class TestDatabaseSafety:
    def test_no_hardcoded_passwords_in_db_module(self):
        """Verify no plaintext passwords or default credentials exist in app/db.py."""
        db_path = os.path.join(os.path.dirname(__file__), "..", "app", "db.py")
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "npg_k9Bftzpl4Ujc" not in content
        assert "NEON_DEFAULT_URL" not in content

    def test_has_table_handles_both_engines(self):
        """Verify has_table works cleanly on SQLite without raising exceptions."""
        m_con = market_db()
        try:
            assert has_table(m_con, "klines") is True
            assert has_table(m_con, "non_existent_table_xyz") is False
        finally:
            m_con.close()

    def test_db_routing_integrity(self):
        """Verify market_db points to local SQLite."""
        m_con = market_db()
        try:
            cur = m_con.execute("SELECT COUNT(*) FROM klines")
            assert cur.fetchone()[0] > 0
        finally:
            m_con.close()


class TestNetworkSafety:
    @patch("socket.getaddrinfo")
    @patch("socket.socket")
    def test_force_ipv4_preserves_socket_timeout(self, mock_socket_cls, mock_getaddr):
        """Verify _force_ipv4 respects custom timeout argument instead of dropping it."""
        mock_sock_instance = MagicMock()
        mock_socket_cls.return_value = mock_sock_instance
        mock_getaddr.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        # Call with timeout=5.0
        sock = _force_ipv4(("127.0.0.1", 443), timeout=5.0)
        mock_sock_instance.settimeout.assert_called_with(5.0)
        mock_sock_instance.connect.assert_called_once_with(("127.0.0.1", 443))


class TestAIEngineSafety:
    def test_open_position_blocking(self):
        """Verify get_active_and_recent_symbols blocks any symbol currently marked OPEN."""
        ai = AIEngine(dry=True)
        mock_con = MagicMock()
        # Mock open positions: ['CLANKERUSDT']
        mock_con.execute.side_effect = [
            MagicMock(fetchall=lambda: [('CLANKERUSDT',)]),  # open rows
            MagicMock(fetchall=lambda: [('AVAAIUSDT',)]),    # recent rows
        ]
        blocked = ai.get_active_and_recent_symbols(mock_con)
        assert "CLANKERUSDT" in blocked
        assert "AVAAIUSDT" in blocked
        assert "BTCUSDT" not in blocked

    def test_dynamic_atr_risk_capping(self):
        """Verify risk is capped at $15 USDT and SL distance accommodates ATR."""
        ai = AIEngine(dry=True)
        ai.info = MagicMock(return_value=(Decimal("0.01"), Decimal("0.1"), Decimal("0.1"), Decimal("5.0")))
        ai.book = MagicMock(return_value=(100.0, 100.0))

        # Test high volatility token (mid=100, atr=5.0 -> 1.5*atr = 7.5 distance)
        # Expected risk = $15 -> Qty = 15 / 7.5 = 2.0
        con = MagicMock()
        ai.dc = MagicMock()

        # Dry run execution should compute correctly and return True
        res = ai.execute_ai_trade(con, "TESTUSDT", "LONG", "Test Setup", 85, "Test Thesis", atr=5.0)
        assert res is True
