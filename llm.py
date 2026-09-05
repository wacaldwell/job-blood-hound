"""Single provider-configurable path to an Anthropic-shaped Messages API.

All three LLM call sites (gate, fit, generate) route their HTTP through here so a
second provider can be selected by env without touching each call site. Kimi K2
(Moonshot) works because it exposes an Anthropic-compatible /v1/messages
endpoint, so only base_url + key + model change.

Manual switch only: JOB_PROVIDER selects the provider. Nothing here auto-fails-over.
No em dashes in any emitted string.
"""
import json
import os
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import requests

Provider = namedtuple("Provider", "name base_url api_key model version timeout",
                      defaults=(180,))

DEFAULT_PROVIDER = "anthropic"
ANTHROPIC_VERSION = "2023-06-01"

# Cheap triage, strongest model only where the decision or artifact warrants it.
# JOB_<COMPONENT>_MODEL overrides these defaults, and the existing JOB_MODEL
# remains a global fallback override for operators who intentionally want one
# model everywhere.
_ANTHROPIC_COMPONENT_MODELS = {
    "fit": "claude-haiku-4-5",
    "gate": "claude-opus-4-8",
    "draft": "claude-opus-4-8",
}

# Standard, non-batch API prices in USD per million tokens. These values are
# recorded with each event so a future price change cannot rewrite history.
_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0),
}

# Per-provider defaults. api_key is read from the named env var; component and
# global model overrides are applied in resolve_provider().
# timeout is per-provider because throughput is a property of the provider, not
# of the call site; JOB_LLM_TIMEOUT overrides it.
_REGISTRY = {
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-opus-4-8",
        "timeout": 180,
    },
    "kimi": {
        # Moonshot's Anthropic-compatible endpoint. Model id verified 2026-07-24
        # against the live /v1/models list. Moonshot is much slower than Anthropic
        # on the gate's extraction: measured 255s for a 10k-char JD where Opus
        # takes about 40s, so this carries a longer default timeout. At 180s the
        # gate timed out mid-response and failed closed with ERROR.
        "base_url": "https://api.moonshot.ai/anthropic",
        "key_env": "MOONSHOT_API_KEY",
        "model": "kimi-k2.6",
        "timeout": 600,
    },
}


class OutputTruncated(Exception):
    """Raised when the model stopped because it hit max_tokens."""

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens
        super().__init__(f"output truncated at {max_tokens} tokens")


def resolve_provider(name=None, component=None):
    """Resolve the active provider from env (or an explicit name).

    Precedence for each piece: explicit JOB_LLM_* override, then the registry.
    A component-specific model variable has highest precedence, then JOB_MODEL,
    then the component default for Anthropic, then the provider default.
    JOB_LLM_TIMEOUT overrides the timeout for whichever provider is active.
    api_key is NOT validated here; a missing key is the caller's fail-closed path.
    """
    name = name or os.environ.get("JOB_PROVIDER", DEFAULT_PROVIDER)
    reg = _REGISTRY.get(name)
    if reg is None:
        raise ValueError(f"unknown JOB_PROVIDER '{name}'; known: {sorted(_REGISTRY)}")
    base_url = os.environ.get("JOB_LLM_BASE_URL", reg["base_url"]).rstrip("/")
    api_key = os.environ.get("JOB_LLM_API_KEY") or os.environ.get(reg["key_env"], "")
    component_env = f"JOB_{component.upper()}_MODEL" if component else None
    component_default = (_ANTHROPIC_COMPONENT_MODELS.get(component)
                         if name == DEFAULT_PROVIDER else None)
    model = ((os.environ.get(component_env) if component_env else None)
             or os.environ.get("JOB_MODEL")
             or component_default
             or reg["model"])
    raw_timeout = os.environ.get("JOB_LLM_TIMEOUT")
    if raw_timeout:
        try:
            timeout = int(raw_timeout)
        except ValueError:
            # Name the variable that is actually wrong. Callers surface this
            # message verbatim, and a bare int() error sends the reader to the
            # wrong env var.
            raise ValueError("JOB_LLM_TIMEOUT must be a whole number of "
                             f"seconds, got {raw_timeout!r}")
    else:
        timeout = reg["timeout"]
    return Provider(name, base_url, api_key, model, ANTHROPIC_VERSION, timeout)


def _usage_log_path():
    """Return the append-only usage log path, or None when logging is disabled."""
    explicit = os.environ.get("JOB_LLM_USAGE_LOG")
    if explicit and explicit.strip().lower() in ("off", "0", "false", "no"):
        return None
    if explicit:
        return Path(explicit).expanduser()
    log_dir = os.environ.get("LOG_DIR")
    base = Path(log_dir).expanduser() if log_dir else Path.home() / "logs"
    return base / "job-hound" / "llm-usage.log"


def _record_usage(component, provider, usage):
    """Append one secret-free usage event. Telemetry failure never fails a call."""
    path = _usage_log_path()
    if path is None or not isinstance(usage, dict):
        return
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": "INFO",
            "component": component or "unknown",
            "provider": provider.name,
            "model": provider.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": int(
                usage.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(
                usage.get("cache_read_input_tokens") or 0),
        }
        price = _PRICING.get(provider.model)
        if price and not (event["cache_creation_input_tokens"]
                          or event["cache_read_input_tokens"]):
            event["estimated_cost_usd"] = round(
                (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000,
                8)
            event["pricing_usd_per_million"] = {
                "input": price[0], "output": price[1]}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        # Accounting must never turn a successful model response into a failed
        # gate or draft. Operators can detect a missing log in the host check.
        return


def call_messages(system, user, *, max_tokens, provider=None,
                  raise_on_truncation=False, timeout=None, component=None):
    """POST one system+user turn to an Anthropic-shaped Messages API; return text.

    provider defaults to resolve_provider(). timeout defaults to the provider's,
    so a slow provider does not need every call site edited; pass one explicitly
    only to override. raise_on_truncation surfaces a max_tokens cutoff as
    OutputTruncated (the gate wants this; the other sites tolerate a cut
    response). The key rides on the provider; a missing key lets the HTTP layer
    401 exactly as before.
    """
    provider = provider or resolve_provider()
    if timeout is None:
        timeout = provider.timeout
    resp = requests.post(
        f"{provider.base_url}/v1/messages",
        headers={"x-api-key": provider.api_key,
                 "anthropic-version": provider.version,
                 "content-type": "application/json"},
        json={"model": provider.model, "max_tokens": max_tokens,
              "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    _record_usage(component, provider, data.get("usage"))
    if raise_on_truncation and data.get("stop_reason") == "max_tokens":
        raise OutputTruncated(max_tokens)
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
