#!/usr/bin/env python3
"""
notify.py - push a plain-text digest to a Discord incoming webhook.

Used by the unattended daily run, which is a cron job (not Claude Code) and so
cannot use the Discord MCP. A webhook is a plain HTTPS POST. The URL is secret;
it lives in the host environment or companies.yaml (discord_webhook), never in
version control with a real value.
"""

import requests

DISCORD_LIMIT = 1900  # 2000 hard limit, leave headroom
SUPPRESS_EMBEDS = 4    # message flag: do not unfurl link previews


def post_discord(webhook_url, text, post_fn=None):
    if not webhook_url:
        return False
    post_fn = post_fn or (lambda url, json, timeout:
                          requests.post(url, json=json, timeout=timeout))
    body = text[:DISCORD_LIMIT]
    # flags=SUPPRESS_EMBEDS keeps the digest's job links clickable but stops
    # Discord from rendering a preview card per URL (a wall of embeds is noise).
    try:
        resp = post_fn(webhook_url, {"content": body, "flags": SUPPRESS_EMBEDS}, 20)
    except requests.RequestException:
        return False
    return 200 <= resp.status_code < 300
