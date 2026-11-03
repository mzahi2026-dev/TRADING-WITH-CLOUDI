"""
Automated Safety Tests: P0-01 Environment Safety Foundation
Fix Batch 01 Review Corrections (R01–R04)
All tests run in isolated mocks with ZERO network calls and ZERO order submissions.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

from app.safety import (
    validate_environment,
    assert_execution_allowed,
    mask_secret,
    validate_endpoint,
    get_startup_safety_banner,
    SafetyViolationError,
    BYBIT_DEMO_REST_URL,
    BYBIT_DEMO_PRIVATE_WS_URL,
    BYBIT_PUBLIC_LINEAR_WS_URL,
)
from app.trade_engine import Engine
from app.ai_engine import AIEngine
from app.notifier import send_bot_online_alert


class TestP001EnvironmentSafety:

    # -------------------------------------------------------------------------
    # Baseline checks
    # -------------------------------------------------------------------------

    def test_missing_configuration_blocks_execution(self):
        """Verify that an empty/missing configuration blocks order execution."""
        empty_env = {}
        report = validate_environment(empty_env)
        assert report.execution_enabled is False
        assert report.account_verified is False
        assert report.status_code == "CONFIG_MISSING"
        assert any("No Bybit V5 Demo configuration provided" in r for r in report.block_reasons)

        with pytest.raises(SafetyViolationError) as exc_info:
            assert_execution_allowed(empty_env)
        assert "Execution BLOCKED" in str(exc_info.value)

    def test_binance_trading_configuration_cannot_enable_execution(self):
        """Verify that Binance configuration cannot enable execution under locked Bybit V5 architecture."""
        binance_env = {
            "TARGET_EXCHANGE": "binance",
            "BINANCE_API_KEY": "sample_binance_api_key_12345",
            "BINANCE_API_SECRET": "sample_binance_api_secret_67890",
            "BINANCE_FAPI_BASE": "https://demo-fapi.binance.com",
            "USE_DEMO": "true",
        }
        report = validate_environment(binance_env)
        assert report.execution_enabled is False
        assert any("Binance trading configuration is rejected" in r for r in report.block_reasons)

        with pytest.raises(SafetyViolationError) as exc_info:
            assert_execution_allowed(binance_env)
        assert "Binance trading configuration is rejected" in str(exc_info.value)

    def test_bybit_production_trading_configuration_rejected(self):
        """Verify that Bybit production REST endpoints are strictly rejected."""
        prod_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api.bybit.com",
            "BYBIT_DEMO_API_KEY": "sample_bybit_key_12345",
            "BYBIT_DEMO_API_SECRET": "sample_bybit_secret_67890",
        }
        report = validate_environment(prod_env)
        assert report.execution_enabled is False
        assert any("PROHIBITED" in r or "strictly prohibited" in r for r in report.block_reasons)

        with pytest.raises(SafetyViolationError) as exc_info:
            assert_execution_allowed(prod_env)
        assert "BLOCKED" in str(exc_info.value)

    def test_bybit_testnet_trading_configuration_rejected(self):
        """Verify that Bybit testnet REST endpoints are rejected (only Demo allowed)."""
        testnet_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-testnet.bybit.com",
            "BYBIT_DEMO_API_KEY": "sample_bybit_key_12345",
            "BYBIT_DEMO_API_SECRET": "sample_bybit_secret_67890",
        }
        report = validate_environment(testnet_env)
        assert report.execution_enabled is False
        assert any("PROHIBITED" in r or "testnet" in r.lower() for r in report.block_reasons)

        with pytest.raises(SafetyViolationError) as exc_info:
            assert_execution_allowed(testnet_env)
        assert "BLOCKED" in str(exc_info.value)

    # -------------------------------------------------------------------------
    # R01: False Account Verification Separation
    # -------------------------------------------------------------------------

    def test_r01_fake_but_correctly_formatted_credentials_cannot_set_account_verified(self):
        """
        R01: Credential presence and format validation must NEVER set account_verified=True.
        Until authenticated Bybit Demo account identity is genuinely verified,
        account_verified must remain False.
        """
        fake_formatted_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com/v5/private",
            "BYBIT_PUBLIC_LINEAR_WS_URL": "wss://stream.bybit.com/v5/public/linear",
            "BYBIT_DEMO_API_KEY": "WELL_FORMED_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "WELL_FORMED_SECRET_9876543210ABCDEF",
        }
        report = validate_environment(fake_formatted_env)
        # Credential format validation passes
        assert report.credentials_present is True
        assert report.credentials_format_valid is True
        # But actual account verification MUST remain False!
        assert report.account_verified is False
        assert any("R01: Account identity is UNVERIFIED" in r for r in report.block_reasons)
        assert report.execution_enabled is False

    def test_r01_demo_url_alone_does_not_count_as_verified_account_identity(self):
        """Verify that configuring the demo URL without credentials fails closed."""
        url_only_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_DEMO_API_KEY": "",
            "BYBIT_DEMO_API_SECRET": "",
        }
        report = validate_environment(url_only_env)
        assert report.credentials_present is False
        assert report.credentials_format_valid is False
        assert report.account_verified is False
        assert report.execution_enabled is False

        with pytest.raises(SafetyViolationError):
            assert_execution_allowed(url_only_env)

    def test_r01_invalid_or_mismatched_credentials_fail_closed(self):
        """Verify that invalid, partial, or placeholder credentials fail closed."""
        # Key present, secret missing
        partial_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_DEMO_API_KEY": "some_valid_looking_key_123",
            "BYBIT_DEMO_API_SECRET": "",
        }
        report_a = validate_environment(partial_env)
        assert report_a.credentials_present is True
        assert report_a.credentials_format_valid is False
        assert report_a.account_verified is False
        assert report_a.execution_enabled is False

        # Placeholder dummy credentials
        dummy_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_DEMO_API_KEY": "test",
            "BYBIT_DEMO_API_SECRET": "test_secret_12345",
        }
        report_b = validate_environment(dummy_env)
        assert report_b.credentials_format_valid is False
        assert report_b.account_verified is False
        assert report_b.execution_enabled is False

    # -------------------------------------------------------------------------
    # R02: Strict Endpoint Validation (Scheme, Host, Port, Path, Query, Fragment)
    # -------------------------------------------------------------------------

    def test_r02_incorrect_rest_url_rejected(self):
        """R02: Negative tests for Bybit Demo REST endpoint."""
        base = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_DEMO_API_KEY": "VALID_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "VALID_SECRET_1234567890ABCDEF",
        }

        # 1. Scheme mismatch (http instead of https)
        env1 = {**base, "BYBIT_REST_URL": "http://api-demo.bybit.com"}
        r1 = validate_environment(env1)
        assert r1.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("scheme mismatch" in e for e in r1.block_reasons)

        # 2. Unexpected Port (:8080)
        env2 = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com:8080"}
        r2 = validate_environment(env2)
        assert r2.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("unexpected port '8080'" in e for e in r2.block_reasons)

        # 3. Unexpected Path (/v5/order instead of root)
        env3 = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com/v5/order"}
        r3 = validate_environment(env3)
        assert r3.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("path mismatch" in e for e in r3.block_reasons)

        # 4. Unexpected Query Parameters
        env4 = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com/?env=test"}
        r4 = validate_environment(env4)
        assert r4.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("unexpected query parameters" in e for e in r4.block_reasons)

        # 5. Unexpected Fragment
        env5 = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com/#section"}
        r5 = validate_environment(env5)
        assert r5.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("unexpected URL fragment" in e for e in r5.block_reasons)

        # 6. User credentials in authority
        env6 = {**base, "BYBIT_REST_URL": "https://user:pass@api-demo.bybit.com/"}
        r6 = validate_environment(env6)
        assert r6.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("forbidden user credentials" in e for e in r6.block_reasons)

    def test_r02_incorrect_private_ws_url_rejected(self):
        """R02: Negative tests for Bybit Demo Private WebSocket endpoint."""
        base = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_DEMO_API_KEY": "VALID_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "VALID_SECRET_1234567890ABCDEF",
        }

        # 1. Scheme mismatch (ws instead of wss)
        env1 = {**base, "BYBIT_PRIVATE_WS_URL": "ws://stream-demo.bybit.com/v5/private"}
        r1 = validate_environment(env1)
        assert r1.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("scheme mismatch" in e for e in r1.block_reasons)

        # 2. Host mismatch
        env2 = {**base, "BYBIT_PRIVATE_WS_URL": "wss://wrong-stream.bybit.com/v5/private"}
        r2 = validate_environment(env2)
        assert r2.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("host mismatch" in e for e in r2.block_reasons)

        # 3. Path mismatch (/v5/public instead of /v5/private)
        env3 = {**base, "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com/v5/public"}
        r3 = validate_environment(env3)
        assert r3.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("path mismatch" in e for e in r3.block_reasons)

        # 4. Custom port
        env4 = {**base, "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com:9443/v5/private"}
        r4 = validate_environment(env4)
        assert r4.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("unexpected port '9443'" in e for e in r4.block_reasons)

    def test_r02_incorrect_public_linear_ws_url_rejected(self):
        """R02: Negative tests for Bybit Public Linear WebSocket endpoint."""
        base = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_DEMO_API_KEY": "VALID_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "VALID_SECRET_1234567890ABCDEF",
        }

        # 1. Using stream-demo for public linear (prohibited by Bybit specification)
        env1 = {**base, "BYBIT_PUBLIC_LINEAR_WS_URL": "wss://stream-demo.bybit.com/v5/public/linear"}
        r1 = validate_environment(env1)
        assert r1.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("host mismatch" in e for e in r1.block_reasons)

        # 2. Path mismatch (/v5/public/spot instead of /v5/public/linear)
        env2 = {**base, "BYBIT_PUBLIC_LINEAR_WS_URL": "wss://stream.bybit.com/v5/public/spot"}
        r2 = validate_environment(env2)
        assert r2.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("path mismatch" in e for e in r2.block_reasons)

        # 3. Query parameter appended
        env3 = {**base, "BYBIT_PUBLIC_LINEAR_WS_URL": "wss://stream.bybit.com/v5/public/linear?symbol=BTCUSDT"}
        r3 = validate_environment(env3)
        assert r3.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert any("unexpected query parameters" in e for e in r3.block_reasons)

    # -------------------------------------------------------------------------
    # R03: Startup Status Consistency
    # -------------------------------------------------------------------------

    def test_r03_startup_status_consistency_never_reports_enabled_solely_on_use_demo(self):
        """
        R03: Startup Telegram/status messaging must accurately reflect the execution state.
        Never report trading enabled or verified solely because USE_DEMO=true.
        """
        # Scenario: USE_DEMO=true in environment
        demo_env = {"USE_DEMO": "true"}
        report = validate_environment(demo_env)
        assert report.execution_enabled is False

        # Banner check
        banner = get_startup_safety_banner()
        assert "Order Execution     : DISABLED (Fail-Closed Safety Active)" in banner
        assert "Account Identity    : UNVERIFIED" in banner

        # Telegram notification check (mocked requests.post)
        with patch("app.notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            send_bot_online_alert(
                mode=report.environment,
                execution_enabled=report.execution_enabled,
                identity_verified=report.account_verified,
            )
            assert mock_post.called
            sent_payload = mock_post.call_args[1]["json"]
            sent_text = sent_payload["text"]

            assert "Order Execution:</b> DISABLED (Fail-Closed Safe Mode)" in sent_text
            assert "Account Identity:</b> UNVERIFIED" in sent_text
            assert "Order Execution:</b> ENABLED" not in sent_text
            assert "Account Identity:</b> VERIFIED" not in sent_text

    # -------------------------------------------------------------------------
    # R04: All Order-Capable Startup Paths Respect Execution Guard
    # -------------------------------------------------------------------------

    def test_r04_all_order_capable_startup_paths_blocked(self):
        """
        R04: All available order-capable startup paths default to execution disabled
        and raise SafetyViolationError if live orders are attempted.
        """
        # Path 1: Engine default instantiation
        eng_default = Engine()
        assert eng_default.dry is True
        assert eng_default.dc is None

        # Path 2: Engine(dry=False) must raise SafetyViolationError
        with pytest.raises(SafetyViolationError) as exc_eng:
            Engine(dry=False)
        assert "Fail-closed guard active" in str(exc_eng.value)

        # Path 3: AIEngine default instantiation
        ai_default = AIEngine()
        assert ai_default.dry is True
        assert ai_default.dc is None

        # Path 4: AIEngine(dry=False) must raise SafetyViolationError
        with pytest.raises(SafetyViolationError) as exc_ai:
            AIEngine(dry=False)
        assert "Fail-closed guard active" in str(exc_ai.value)

        # Path 5: Engine.check() in safe mode does not invoke balance/order calls
        eng_default.book = MagicMock(return_value=(50000.0, 50001.0))
        eng_default.check()  # Must not crash even without DemoClient and without network

    def test_r04_order_execution_guard_remains_disabled(self):
        """
        R04: Under all scenarios in Fix Batch 01, order execution guard remains disabled.
        """
        valid_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": "https://api-demo.bybit.com",
            "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com/v5/private",
            "BYBIT_PUBLIC_LINEAR_WS_URL": "wss://stream.bybit.com/v5/public/linear",
            "BYBIT_DEMO_API_KEY": "VALID_BYBIT_KEY_12345",
            "BYBIT_DEMO_API_SECRET": "VALID_BYBIT_SECRET_67890ABCDEF",
        }
        report = validate_environment(valid_env)
        assert report.execution_enabled is False
        assert report.status_code == "DEMO_CONFIG_VALID_EXECUTION_BLOCKED"

        with pytest.raises(SafetyViolationError):
            assert_execution_allowed(valid_env)

    # -------------------------------------------------------------------------
    # Secret Masking Tests
    # -------------------------------------------------------------------------

    def test_logs_do_not_expose_api_keys_or_secrets(self):
        """Verify secret masking helper and ensure banners/logs do not expose raw secrets."""
        raw_secret = "SUPER_SECRET_BYBIT_KEY_123456789"
        masked = mask_secret(raw_secret)
        assert raw_secret not in masked
        assert masked == f"SUP...789 (len={len(raw_secret)})"

        short_secret = "secret"
        assert mask_secret(short_secret) == "[REDACTED]"
        assert mask_secret("") == "<EMPTY>"
        assert mask_secret(None) == "<EMPTY>"

        banner = get_startup_safety_banner()
        assert "SUPER_SECRET" not in banner
        assert "DISABLED" in banner

    # -------------------------------------------------------------------------
    # Batch 01 Final Corrections Regression Tests
    # -------------------------------------------------------------------------

    def test_malformed_and_out_of_range_ports_handled_without_exception(self):
        """
        Correction 1: Handle malformed and out-of-range URL ports without uncaught exceptions.
        Return ENDPOINT_VALIDATION_FAILED safely.
        """
        base = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_DEMO_API_KEY": "VALID_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "VALID_SECRET_1234567890ABCDEF",
        }

        # Port out of range (> 65535)
        env_high_port = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com:999999"}
        report_high = validate_environment(env_high_port)
        assert report_high.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert report_high.execution_enabled is False
        assert any("malformed or out-of-range port" in r for r in report_high.block_reasons)

        # Non-integer port
        env_alpha_port = {**base, "BYBIT_REST_URL": "https://api-demo.bybit.com:abc"}
        report_alpha = validate_environment(env_alpha_port)
        assert report_alpha.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert report_alpha.execution_enabled is False
        assert any("malformed or out-of-range port" in r for r in report_alpha.block_reasons)

        # Negative port
        env_neg_port = {**base, "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com:-1/v5/private"}
        report_neg = validate_environment(env_neg_port)
        assert report_neg.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert report_neg.execution_enabled is False
        assert any("malformed or out-of-range port" in r for r in report_neg.block_reasons)

    def test_sensitive_url_values_never_included_in_errors_reports_or_logs(self):
        """
        Correction 2: Never include sensitive URL query values, URL passwords,
        userinfo, or parameter values in validation errors, logs, or safety reports.
        """
        sensitive_pass = "TopSecretPassword999!"
        sensitive_token = "SUPER_SECRET_QUERY_TOKEN_777"
        sensitive_frag = "SUPER_SECRET_FRAGMENT_888"
        sensitive_param = "SUPER_SECRET_PARAM_555"

        leak_env = {
            "TARGET_EXCHANGE": "bybit",
            "BYBIT_REST_URL": f"https://myuser:{sensitive_pass}@api-demo.bybit.com/;{sensitive_param}?token={sensitive_token}#{sensitive_frag}",
            "BYBIT_DEMO_API_KEY": "VALID_KEY_12345678",
            "BYBIT_DEMO_API_SECRET": "VALID_SECRET_1234567890ABCDEF",
        }
        report = validate_environment(leak_env)
        assert report.status_code == "ENDPOINT_VALIDATION_FAILED"
        assert report.execution_enabled is False

        # Build full string representation of the report and banner
        all_reasons_text = " ".join(report.block_reasons)
        summary_text = report.summary_message
        url_text = report.rest_url
        banner_text = get_startup_safety_banner()
        all_text = f"{all_reasons_text} {summary_text} {url_text} {banner_text}"

        # Assert zero sensitive items leak into errors, reports, or banner
        assert sensitive_pass not in all_text
        assert sensitive_token not in all_text
        assert sensitive_frag not in all_text
        assert sensitive_param not in all_text
        assert "myuser" not in all_text

        # Assert redaction tags are used instead
        assert "[REDACTED]" in all_text

    def test_hourly_report_trading_mode_synchronized_with_fail_closed_guard(self):
        """
        Correction 3: Hourly report trading-mode determination must synchronize with
        the fail-closed safety state used by run_unified.py.
        Never claim DEMO order execution when execution is disabled.
        """
        from app.notifier import send_hourly_report

        with patch("app.notifier.requests.post") as mock_post, \
             patch("app.notifier.sqlite3.connect") as mock_conn:
            # Mock DB queries
            mock_db = MagicMock()
            mock_conn.return_value = mock_db
            mock_db.execute.return_value.fetchone.return_value = None
            mock_db.execute.return_value.fetchall.return_value = []
            mock_post.return_value.status_code = 200

            # Run hourly report under default environment (execution disabled)
            result = send_hourly_report()
            assert result is True
            assert mock_post.called

            sent_payload = mock_post.call_args[1]["json"]
            sent_text = sent_payload["text"]

            # Must accurately report safe dry run, not active DEMO trading
            assert "DRY RUN (Execution Disabled)" in sent_text
            assert "🧪 <b>DEMO</b>" not in sent_text
            assert "🧪 <b>LIVE</b>" not in sent_text
            # Must clearly separate Last 1 Hour and 24h Overall Summary
            assert "Last 1 Hour" in sent_text
            assert "24h Overall Summary" in sent_text
