"""
TRADING WITH CLOUDI — P0-01 Environment Safety Foundation
Locked Target: Bybit V5 USDT Linear Perpetual Futures (Demo Only)
Security Posture: Fail-Closed, Zero Real-Money Order Risk
"""

from dataclasses import dataclass, field
import os
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# ---- Approved Canonical Bybit V5 Endpoints ----
BYBIT_DEMO_REST_URL = "https://api-demo.bybit.com"
BYBIT_DEMO_PRIVATE_WS_URL = "wss://stream-demo.bybit.com/v5/private"
BYBIT_PUBLIC_LINEAR_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Prohibited Hosts (Fail-Closed)
PROHIBITED_REST_HOSTS = {
    "api.bybit.com",
    "api.bytick.com",
    "api-testnet.bybit.com",
}

PROHIBITED_ORDER_HOSTS = {
    "fapi.binance.com",
    "demo-fapi.binance.com",
    "api.binance.com",
    "testnet.binance.vision",
}


class SafetyViolationError(RuntimeError):
    """Raised whenever an operation violates environment safety boundaries."""
    pass


@dataclass
class SafetyReport:
    is_safe_to_boot: bool = False
    execution_enabled: bool = False
    target_exchange: str = "UNKNOWN"
    environment: str = "UNKNOWN"
    rest_url: str = ""
    public_ws_url: str = ""
    private_ws_url: str = ""
    credentials_present: bool = False
    credentials_format_valid: bool = False
    account_verified: bool = False
    status_code: str = "BLOCKED"
    summary_message: str = ""
    block_reasons: List[str] = field(default_factory=list)


def mask_secret(val: Optional[str]) -> str:
    """Mask credentials so raw API keys or secrets are never exposed in logs."""
    if not val:
        return "<EMPTY>"
    s = str(val).strip()
    if len(s) <= 6:
        return "[REDACTED]"
    return f"{s[:3]}...{s[-3:]} (len={len(s)})"


def sanitize_url(url_str: str) -> str:
    """
    Sanitize a URL string to ensure passwords, userinfo, query parameter values,
    and fragments are NEVER leaked in logs, reports, or error messages.
    """
    if not url_str or not url_str.strip():
        return ""
    try:
        p = urlparse(url_str.strip())
        host = (p.hostname or "").lower()
        port_str = ""
        try:
            if p.port:
                port_str = f":{p.port}"
        except ValueError:
            port_str = ":[INVALID_PORT]"

        clean_netloc = f"{host}{port_str}"
        path = p.path or ""
        q_tag = "?[REDACTED]" if p.query else ""
        frag_tag = "#[REDACTED]" if p.fragment else ""
        param_tag = ";[REDACTED]" if p.params else ""
        return f"{p.scheme}://{clean_netloc}{path}{param_tag}{q_tag}{frag_tag}"
    except Exception:
        return "[MALFORMED_URL_REDACTED]"


def validate_endpoint(
    url_str: str,
    expected_scheme: str,
    expected_host: str,
    allowed_paths: Tuple[str, ...],
    endpoint_name: str
) -> List[str]:
    """
    R02 & Final Corrections: Validate exact approved endpoint URL components:
    scheme, host, port, path, query, fragment.
    - Handles malformed and out-of-range ports safely without uncaught exceptions.
    - Never includes sensitive URL query values, URL passwords, userinfo, or parameters in errors.
    """
    errors: List[str] = []
    if not url_str or not url_str.strip():
        errors.append(f"{endpoint_name} URL is missing or empty.")
        return errors

    raw = url_str.strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        errors.append(f"{endpoint_name} URL parsing failed.")
        return errors

    # 1. Scheme Validation
    if parsed.scheme.lower() != expected_scheme.lower():
        errors.append(
            f"{endpoint_name} scheme mismatch: expected '{expected_scheme}', got '{parsed.scheme}'."
        )

    # 2. Host Validation
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        errors.append(f"{endpoint_name} URL is missing a valid hostname.")
    elif hostname != expected_host.lower():
        if hostname in PROHIBITED_REST_HOSTS:
            errors.append(f"{endpoint_name} host '{hostname}' is PROHIBITED (mainnet/testnet).")
        else:
            errors.append(
                f"{endpoint_name} host mismatch: expected '{expected_host}', got '{hostname}'."
            )

    # 3. Port Validation: Catch malformed and out-of-range ports safely
    port = None
    try:
        port = parsed.port
    except ValueError:
        errors.append(f"{endpoint_name} contains malformed or out-of-range port.")

    default_port = 443 if expected_scheme.lower() in ("https", "wss") else 80
    if port is not None and port != default_port:
        errors.append(
            f"{endpoint_name} contains unexpected port '{port}'; standard port expected."
        )

    # 4. User Credentials in URL (UserInfo): Never echo username or password
    has_userinfo = bool(parsed.username or parsed.password or ("@" in (parsed.netloc or "")))
    if has_userinfo:
        errors.append(f"{endpoint_name} contains forbidden user credentials in URL authority [REDACTED].")

    # 5. Path Validation
    raw_path = parsed.path.rstrip("/")
    normalized_path = "/" + raw_path.lstrip("/") if raw_path else ""
    if normalized_path not in allowed_paths:
        errors.append(
            f"{endpoint_name} path mismatch: expected one of {allowed_paths}, got '{parsed.path}'."
        )

    # 6. Unexpected Query Parameters: Never echo query string or sensitive values
    if parsed.query:
        errors.append(f"{endpoint_name} contains unexpected query parameters [REDACTED].")

    # 7. Unexpected Fragment: Never echo fragment values
    if parsed.fragment:
        errors.append(f"{endpoint_name} contains unexpected URL fragment [REDACTED].")

    # 8. Unexpected Params: Never echo parameter values
    if parsed.params:
        errors.append(f"{endpoint_name} contains unexpected URL parameters [REDACTED].")

    return errors


def validate_environment(env: Optional[Dict[str, str]] = None) -> SafetyReport:
    """
    Perform a complete fail-closed validation of the environment.
    All trading execution defaults to DISABLED.
    Returns a comprehensive SafetyReport with strictly sanitized URLs and messages.
    """
    if env is None:
        env = dict(os.environ)

    report = SafetyReport()
    reasons: List[str] = []

    # 1. Inspect Environment Input
    raw_exchange = env.get("TARGET_EXCHANGE", "").strip().lower()
    bybit_demo_key = env.get("BYBIT_DEMO_API_KEY", "").strip()
    bybit_demo_secret = env.get("BYBIT_DEMO_API_SECRET", "").strip()
    bybit_rest = env.get("BYBIT_REST_URL", "").strip() or BYBIT_DEMO_REST_URL
    bybit_priv_ws = env.get("BYBIT_PRIVATE_WS_URL", "").strip() or BYBIT_DEMO_PRIVATE_WS_URL
    bybit_pub_ws = env.get("BYBIT_PUBLIC_LINEAR_WS_URL", "").strip() or BYBIT_PUBLIC_LINEAR_WS_URL

    binance_key = env.get("BINANCE_API_KEY", "").strip() or env.get("DEMO_API_KEY", "").strip()
    binance_fapi = env.get("BINANCE_FAPI_BASE", "").strip()

    # 2. Check for Prohibited Binance Trading Configuration
    if raw_exchange == "binance" or binance_fapi:
        try:
            parsed_b = urlparse(binance_fapi if binance_fapi.startswith("http") else f"https://{binance_fapi}")
            b_host = (parsed_b.hostname or "").lower()
        except Exception:
            b_host = ""
        if b_host in PROHIBITED_ORDER_HOSTS or "binance.com" in b_host:
            reasons.append(
                "Binance trading configuration is rejected: locked target architecture is Bybit V5."
            )

    # 3. Check Bybit Target Designation
    is_bybit_target = (
        raw_exchange in ("bybit", "bybit_demo", "bybit-v5")
        or bool(bybit_demo_key)
        or bool(env.get("BYBIT_REST_URL", "").strip())
    )

    if not is_bybit_target:
        reasons.append("No Bybit V5 Demo configuration provided. Trading execution is blocked.")
        report.target_exchange = "NONE"
        report.environment = "UNCONFIGURED"
        report.block_reasons = reasons
        report.is_safe_to_boot = True
        report.execution_enabled = False
        report.status_code = "CONFIG_MISSING"
        report.summary_message = "Order execution DISABLED (configuration missing or unconfigured)."
        return report

    report.target_exchange = "BYBIT_V5"
    report.environment = "DEMO"
    report.rest_url = sanitize_url(bybit_rest)
    report.private_ws_url = sanitize_url(bybit_priv_ws)
    report.public_ws_url = sanitize_url(bybit_pub_ws)

    # 4. Strict Endpoint Validation (Scheme, Host, Port, Path, Query, Fragment)
    rest_errs = validate_endpoint(
        bybit_rest,
        expected_scheme="https",
        expected_host="api-demo.bybit.com",
        allowed_paths=("", "/"),
        endpoint_name="Bybit Demo REST"
    )
    reasons.extend(rest_errs)

    priv_ws_errs = validate_endpoint(
        bybit_priv_ws,
        expected_scheme="wss",
        expected_host="stream-demo.bybit.com",
        allowed_paths=("/v5/private",),
        endpoint_name="Bybit Demo Private WebSocket"
    )
    reasons.extend(priv_ws_errs)

    pub_ws_errs = validate_endpoint(
        bybit_pub_ws,
        expected_scheme="wss",
        expected_host="stream.bybit.com",
        allowed_paths=("/v5/public/linear",),
        endpoint_name="Bybit Public Linear WebSocket"
    )
    reasons.extend(pub_ws_errs)

    # 5. R01: Credential Format vs Authenticated Account Verification Separation
    if bybit_demo_key or bybit_demo_secret:
        report.credentials_present = True

    if not bybit_demo_key or not bybit_demo_secret:
        reasons.append("Missing BYBIT_DEMO_API_KEY and/or BYBIT_DEMO_API_SECRET.")
        report.credentials_format_valid = False
    else:
        if len(bybit_demo_key) < 8 or len(bybit_demo_secret) < 16:
            reasons.append("BYBIT_DEMO_API_KEY or BYBIT_DEMO_API_SECRET length is invalid or malformed.")
            report.credentials_format_valid = False
        elif bybit_demo_key.lower() in ("mock", "test", "your_api_key", "demo_key", "fake_key"):
            reasons.append("Placeholder or dummy Bybit API credentials detected.")
            report.credentials_format_valid = False
        else:
            report.credentials_format_valid = True

    # R01 Core: Credential format validation must NEVER set account_verified = True.
    report.account_verified = False
    reasons.append(
        "R01: Account identity is UNVERIFIED. Authenticated exchange verification requires "
        "an active Bybit Demo session (not authorized in Fix Batch 01)."
    )

    # 6. Current Batch Scope Enforcement (Fix Batch 01)
    reasons.append(
        "Fix Batch 01 safety foundation: Bybit V5 order submission adapter is not yet connected. "
        "Execution remains fail-closed."
    )

    report.block_reasons = reasons
    report.execution_enabled = False  # Strict default: ALWAYS False in this batch
    report.is_safe_to_boot = len(rest_errs + priv_ws_errs + pub_ws_errs) == 0

    if rest_errs or priv_ws_errs or pub_ws_errs:
        report.status_code = "ENDPOINT_VALIDATION_FAILED"
        report.summary_message = f"Order execution BLOCKED: Endpoint validation failed ({reasons[0]})."
    elif report.credentials_format_valid and not any("prohibited" in r.lower() or "rejected" in r.lower() for r in reasons):
        report.status_code = "DEMO_CONFIG_VALID_EXECUTION_BLOCKED"
        report.summary_message = (
            "Bybit Demo configuration verified. Account identity remains UNVERIFIED. "
            "Order execution DISABLED (Batch 01 Guard Active)."
        )
    else:
        report.status_code = "FAIL_CLOSED_BLOCKED"
        report.summary_message = f"Order execution BLOCKED: {reasons[0]}"

    return report


def assert_execution_allowed(env: Optional[Dict[str, str]] = None) -> None:
    """
    Fail-closed guard for order execution.
    Raises SafetyViolationError if order execution is attempted while not permitted.
    """
    report = validate_environment(env)
    if not report.execution_enabled:
        raise SafetyViolationError(
            f"Execution BLOCKED by Environment Safety Guard: {report.summary_message} | "
            f"Reasons: {'; '.join(report.block_reasons)}"
        )


def get_startup_safety_banner() -> str:
    """Generate an accurate, audited startup status banner."""
    report = validate_environment()
    lines = [
        "=" * 68,
        "          TRADING WITH CLOUDI — SAFETY GUARDIAN ACTIVE",
        "=" * 68,
        f" [✓] Target Architecture : Bybit V5 USDT Linear Perpetuals",
        f" [✓] Target Environment  : {report.environment}",
        f" [✓] Demo REST Endpoint  : {report.rest_url or BYBIT_DEMO_REST_URL}",
        f" [✓] Public WS Endpoint  : {report.public_ws_url or BYBIT_PUBLIC_LINEAR_WS_URL}",
        f" [✓] Private WS Endpoint : {report.private_ws_url or BYBIT_DEMO_PRIVATE_WS_URL}",
        f" [✓] Order Execution     : {'ENABLED' if report.execution_enabled else 'DISABLED (Fail-Closed Safety Active)'}",
        f" [✓] Credential Format   : {'VALID' if report.credentials_format_valid else ('PRESENT (INVALID FORMAT)' if report.credentials_present else 'NOT CONFIGURED')}",
        f" [✓] Account Identity    : {'VERIFIED' if report.account_verified else 'UNVERIFIED (Requires Authenticated Exchange Session)'}",
        f" [✓] Safety Status       : {report.status_code}",
        "=" * 68,
    ]
    return "\n".join(lines)
