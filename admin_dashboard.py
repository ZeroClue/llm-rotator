"""Operator dashboard (spec: docs/dashboard-spec.md; tickets #62-#64).

Server-rendered admin page plus htmx-polled fragment routes, served by the
proxy process itself. Loopback-trust auth: /admin* is exempt from the
bearer gate exactly like /health — the bind address is the trust boundary
(default loopback; exposure widening is a documented escalation path).

Read-only v1: renders live in-process state (health snapshot, selector
cursor, telemetry ring/buckets, ShutdownState, effective config). No JSON
API; fragments are HTML partials for htmx.
"""
import logging
import time

from flask import Blueprint, render_template

logger = logging.getLogger(__name__)


def _mask(value):
    """Config display: never render a secret, even a partial one."""
    return "****" if value else "(not set)"


def _relative_ts(ts, now=None):
    """Human 'Nm ago' for telemetry timestamps (monotonic clock)."""
    if ts is None:
        return ""
    if now is None:
        now = time.monotonic()
    seconds = max(0, int(now - ts))
    if seconds < 60:
        return f"{seconds}s ago"
    return f"{seconds // 60}m ago"


def _cooldown_display(seconds):
    """Countdown-first cooldown rendering (spec §2 amendment)."""
    if seconds and seconds > 0:
        return f"{seconds:.0f}s"
    return ""


def _spark_height(slot):
    """Bar height % for one minute slot: failures dominate, ok counts add."""
    if not (slot["ok"] or slot["fail"]):
        return 2
    return min(100, slot["fail"] * 25 + slot["ok"] * 12)


def register_admin_dashboard(application, *, context_provider):
    """Called from create_app(); context_provider() supplies nodes snapshot,
    cursor index, in-flight count, draining flag, settings, optimization
    config, uptime seconds — injected so this module never imports rotator."""
    bp = Blueprint("admin_dashboard", __name__)
    bp.add_app_template_global(_mask, "mask")
    bp.add_app_template_global(_spark_height, "spark_height")

    @bp.route("/admin")
    def admin_page():
        return render_template("admin.html", **context_provider(),
                               cooldown_display=_cooldown_display,
                               relative_ts=_relative_ts)

    @bp.route("/admin/fragments/nodes")
    def admin_nodes_fragment():
        return render_template("admin_nodes.html", **context_provider(),
                               cooldown_display=_cooldown_display,
                               relative_ts=_relative_ts)

    @bp.route("/admin/fragments/config")
    def admin_config_fragment():
        return render_template("admin_config.html", mask=_mask,
                               **context_provider())

    application.register_blueprint(bp)
    logger.info("Admin dashboard registered at /admin (read-only, "
                "loopback-trust)")
