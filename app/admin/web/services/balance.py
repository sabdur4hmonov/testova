"""
Gemini credit-balance tracking. Google exposes no balance API, so the admin
enters their current credit once (a CreditBaseline row) and the panel shows
`remaining = credit_usd − Σ(computed cost since as_of)`, live as usage logs.
"""
from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.models.credit_baseline import CreditBaseline

from . import costs


async def current_baseline(session) -> CreditBaseline | None:
    """The most recent baseline, or None if the admin has never set one."""
    return (await session.execute(
        select(CreditBaseline).order_by(CreditBaseline.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def set_baseline(session, credit_usd: float, note: str | None, admin_id: int | None) -> CreditBaseline:
    """Anchor a NEW baseline (append-only). Usage from now on counts against it."""
    row = CreditBaseline(credit_usd=float(credit_usd), note=note, created_by=admin_id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def status(session) -> dict:
    """Balance snapshot: {baseline, spent_usd/som, remaining_usd/som} — or
    {baseline: None} when no baseline is set yet."""
    baseline = await current_baseline(session)
    if baseline is None:
        return {"baseline": None}
    spent_usd = await costs.window_computed_usd(session, baseline.as_of)
    remaining_usd = baseline.credit_usd - spent_usd
    uzs = settings.UZS_PER_USD
    return {
        "baseline": baseline,
        "spent_usd": spent_usd,
        "spent_som": spent_usd * uzs,
        "remaining_usd": remaining_usd,
        "remaining_som": remaining_usd * uzs,
    }
