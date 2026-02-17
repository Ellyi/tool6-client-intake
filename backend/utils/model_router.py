"""
Model Router Utility - LocalOS Tools
=====================================
Purpose: Route Claude API calls to the right model based on task complexity.
         Sonnet for fast/cheap conversational tasks, Opus for high-stakes deliverables.

Usage:
    from utils.model_router import get_model, get_model_config

    model = get_model("intake")         # → claude-sonnet-4-5-20250929
    model = get_model("audit_report")   # → claude-opus-4-5-20251101

Cost logic:
    - Sonnet: ~3x cheaper, faster, sufficient for chat/intake/scanning
    - Opus:   Best reasoning, use only when output is a client deliverable

Author: LocalOS / Eli Ombogo
"""

# ─────────────────────────────────────────────
# MODEL CONSTANTS
# ─────────────────────────────────────────────

SONNET = "claude-sonnet-4-5-20250929"
OPUS = "claude-opus-4-5-20251101"

# ─────────────────────────────────────────────
# TASK ROUTING CONFIG
# ─────────────────────────────────────────────

# Tasks that justify Opus cost (client-facing, high-stakes output)
OPUS_TASKS = {
    "proposal",           # Full project proposals sent to clients
    "audit_report",       # Business Intelligence Audit deliverables
    "client_email",       # Outbound emails representing LocalOS
    "roi_analysis",       # ROI Projector final output
    "contract",           # Engagement contracts or SOW generation
    "deliverable",        # Generic high-stakes client output
    "intelligence_audit", # Deep intelligence waste audit output
    "executive_summary",  # C-suite facing summaries
    "cip_report",         # Monthly CIP Engine learning report
}

# Tasks that use Sonnet (internal, conversational, fast-turnaround)
SONNET_TASKS = {
    "intake",             # Nuru client intake conversation
    "nuru_chat",          # Any Nuru conversational turn
    "readiness_scan",     # AI Readiness Scanner analysis
    "cost_analysis",      # Internal cost dashboard calculations
    "classification",     # Task/intent classification
    "summarization",      # Internal data summarization
    "chat",               # Generic chat/conversational use
    "validation",         # Input validation or sanity checks
    "cip_learning",       # CIP Engine data ingestion/processing
    "webhook_processing", # Background webhook/data processing
}


# ─────────────────────────────────────────────
# PRIMARY ROUTING FUNCTION
# ─────────────────────────────────────────────

def get_model(task_type: str) -> str:
    """
    Return the appropriate Claude model string for a given task type.

    Args:
        task_type (str): The task category. See OPUS_TASKS and SONNET_TASKS above.

    Returns:
        str: Model identifier string ready for use in Anthropic API calls.

    Raises:
        ValueError: If task_type is unrecognized (prevents silent misrouting).

    Example:
        model = get_model("intake")       # → SONNET
        model = get_model("proposal")     # → OPUS
        model = get_model("audit_report") # → OPUS
    """
    task_normalized = task_type.strip().lower()

    if task_normalized in OPUS_TASKS:
        return OPUS

    if task_normalized in SONNET_TASKS:
        return SONNET

    # Unknown task: fail loudly so you know to add it to config, not silently misroute
    raise ValueError(
        f"Unknown task_type: '{task_type}'. "
        f"Add it to OPUS_TASKS or SONNET_TASKS in model_router.py. "
        f"Known Opus tasks: {sorted(OPUS_TASKS)}. "
        f"Known Sonnet tasks: {sorted(SONNET_TASKS)}."
    )


# ─────────────────────────────────────────────
# EXTENDED CONFIG (for tools that need token/temp settings)
# ─────────────────────────────────────────────

def get_model_config(task_type: str) -> dict:
    """
    Return full model config dict (model + recommended tokens + temperature).
    Use this when you want consistent defaults per task type, not just the model string.

    Returns:
        dict: {
            "model": str,
            "max_tokens": int,
            "temperature": float
        }

    Example:
        config = get_model_config("audit_report")
        response = client.messages.create(**config, messages=[...])
    """
    model = get_model(task_type)

    # Opus tasks: longer output, lower temp for precision
    if model == OPUS:
        return {
            "model": model,
            "max_tokens": 4096,
            "temperature": 0.3,
        }

    # Sonnet tasks: shorter output, slightly warmer for conversational feel
    return {
        "model": model,
        "max_tokens": 1500,
        "temperature": 0.7,
    }


# ─────────────────────────────────────────────
# CONVENIENCE: LOG WHAT MODEL WAS USED (optional)
# ─────────────────────────────────────────────

def get_model_with_log(task_type: str, logger=None) -> str:
    """
    Same as get_model() but logs the routing decision.
    Pass your app's logger instance for traceability in Railway logs.

    Example:
        import logging
        logger = logging.getLogger(__name__)
        model = get_model_with_log("proposal", logger=logger)
        # Railway logs: "Model router: proposal → claude-opus-4-5-20251101"
    """
    model = get_model(task_type)
    message = f"Model router: {task_type} → {model}"

    if logger:
        logger.info(message)
    else:
        print(message)

    return model


# ─────────────────────────────────────────────
# QUICK SELF-TEST (run file directly to verify)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    test_cases = [
        ("intake", SONNET),
        ("nuru_chat", SONNET),
        ("readiness_scan", SONNET),
        ("proposal", OPUS),
        ("audit_report", OPUS),
        ("client_email", OPUS),
        ("roi_analysis", OPUS),
        ("cip_report", OPUS),
    ]

    print("=" * 55)
    print("Model Router Self-Test")
    print("=" * 55)

    all_passed = True
    for task, expected in test_cases:
        result = get_model(task)
        status = "✅ PASS" if result == expected else "❌ FAIL"
        if result != expected:
            all_passed = False
        model_label = "SONNET" if result == SONNET else "OPUS"
        print(f"{status}  {task:<22} → {model_label}")

    print("=" * 55)
    print("All tests passed ✅" if all_passed else "FAILURES DETECTED ❌")

    # Test ValueError on unknown task
    print("\nTesting unknown task error...")
    try:
        get_model("random_unknown_task")
        print("❌ Should have raised ValueError")
    except ValueError as e:
        print(f"✅ ValueError raised correctly: unknown task rejected")

    # Test get_model_config
    print("\nTesting get_model_config...")
    config = get_model_config("audit_report")
    assert config["model"] == OPUS
    assert config["max_tokens"] == 4096
    print(f"✅ audit_report config: {config}")

    config = get_model_config("intake")
    assert config["model"] == SONNET
    assert config["max_tokens"] == 1500
    print(f"✅ intake config: {config}")