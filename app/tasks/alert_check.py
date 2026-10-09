"""
Alert notifier — evaluates the panel's alerts and DMs the admin(s) when any
are active.

WIRED: `main.py` registers run_once on the bot's APScheduler as the hourly
`cost_alert_check` job, guarded so one bad run cannot kill the schedule. It
needs no external cron. It can still be run by hand for a one-off check:

    python -m app.tasks.alert_check

It only sends when there is at least one active alert, so a quiet system stays
quiet. Uses the SAME alerts.evaluate() the panel page shows, so the message and
the page can never disagree.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot

from app.config import settings
from app.database import async_session_factory
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _format(ev: dict) -> str | None:
    if not ev["active_count"]:
        return None
    lines = [f"🔔 <b>Testova ogohlantirish</b> ({ev['active_count']} ta)"]
    d = ev["daily"]
    if d["breached"]:
        lines.append(f"• Bugungi xarajat ${d['spend_usd']:.4f} ≥ chegara ${d['threshold']:.2f}")
    for u in ev["user_alerts"]:
        who = f"@{u['username']}" if u["username"] else str(u["telegram_id"])
        lines.append(f"• {who}: 30 kunlik xarajat ${u['usd']:.4f}")
    severity = ev.get("recon_severity") or {}
    for r in ev["recon_alerts"]:
        if severity.get(str(r.id)) == "critical":
            # delta_pct is stored as 0.0 here; "+0.0%" would read as all-clear.
            lines.append(
                f"• 🚨 Solishtirish {r.period_label}: Google ${r.google_total_usd:.2f} hisobladi, "
                f"biz $0 qayd etdik — hisobga olinmagan xarajat"
            )
        else:
            lines.append(
                f"• Solishtirish {r.period_label}: Google biznikidan "
                f"${r.delta_usd:.2f} ({r.delta_pct:+.1f}%) oshgan"
            )
    return "\n".join(lines)


async def run_once(bot) -> None:
    """Evaluate and notify using an EXISTING Bot.

    AUDIT B-31: this module was written but scheduled nowhere, so
    ALERT_DAILY_USD / ALERT_USER_USD_30D could never actually fire. main.py now
    registers it on the same APScheduler as the other maintenance jobs, and
    passes the running bot in rather than constructing a second one.
    """
    from app.admin.web.services import alerts

    async with async_session_factory() as session:
        ev = await alerts.evaluate(session)
    text = _format(ev)
    if not text:
        logger.info("alert_check_quiet")
        return
    if not settings.ADMIN_IDS:
        logger.warning("alert_check_no_admins")
        return
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:  # never let one bad chat abort the rest
            logger.warning("alert_check_send_failed", admin_id=admin_id, error=str(e))
    logger.info("alert_check_sent", alerts=ev["active_count"])


async def main() -> None:
    """Standalone entry point (python -m app.tasks.alert_check) - builds its
    own Bot. The in-process scheduler uses run_once instead."""
    from app.admin.web.services import alerts

    async with async_session_factory() as session:
        ev = await alerts.evaluate(session)
    text = _format(ev)
    if not text:
        logger.info("alert_check_quiet")
        return
    if not settings.ADMIN_IDS:
        logger.warning("alert_check_no_admins")
        return
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:  # never let one bad chat abort the rest
                logger.warning("alert_check_send_failed", admin_id=admin_id, error=str(e))
    finally:
        await bot.session.close()
    logger.info("alert_check_sent", alerts=ev["active_count"])


if __name__ == "__main__":
    asyncio.run(main())
