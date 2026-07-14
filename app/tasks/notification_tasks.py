"""Celery tasks for Telegram push notifications from background workers."""
from __future__ import annotations

import asyncio

from app.config import settings
from app.tasks.celery_app import celery_app
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.notification_tasks.send_processing_done")
def send_processing_done(
    telegram_chat_id: int,
    project_id: str,
    question_count: int,
    bot_token: str,
) -> None:
    """Notify teacher that file processing finished."""
    import httpx

    text = (
        f"✅ Fayl tahlil qilindi!\n\n"
        f"📝 Topilgan savollar: {question_count}\n\n"
        f"Variantlar soni ni tanlang:"
    )
    payload = {
        "chat_id": telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "5 variant", "callback_data": f"variants:{project_id}:5"},
                    {"text": "10 variant", "callback_data": f"variants:{project_id}:10"},
                ],
                [
                    {"text": "20 variant", "callback_data": f"variants:{project_id}:20"},
                    {"text": "30 variant", "callback_data": f"variants:{project_id}:30"},
                ],
                [
                    {"text": "✏️ Boshqa son", "callback_data": f"variants:{project_id}:custom"},
                ],
            ]
        },
    }
    with httpx.Client(timeout=10) as client:
        client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
        )


@celery_app.task(name="app.tasks.notification_tasks.send_processing_failed")
def send_processing_failed(
    telegram_chat_id: int,
    error_message: str,
    bot_token: str,
) -> None:
    import httpx

    text = (
        f"❌ Fayl tahlil qilishda xatolik yuz berdi.\n\n"
        f"<code>{error_message[:200]}</code>\n\n"
        f"Iltimos, boshqa fayl yuboring yoki @{settings.ADMIN_USERNAME} ga murojaat qiling."
    )
    with httpx.Client(timeout=10) as client:
        client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": telegram_chat_id, "text": text, "parse_mode": "HTML"},
        )


@celery_app.task(name="app.tasks.notification_tasks.send_variants_ready")
def send_variants_ready(
    telegram_chat_id: int,
    variants_pdf_key: str,
    answer_key_pdf_key: str,
    variant_count: int,
    bot_token: str,
) -> None:
    """Send both PDFs to the teacher when variant generation is complete."""
    import httpx
    from app.services.storage import get_local_path

    text = f"✅ {variant_count} ta variant tayyor!\n\nQuyida PDF fayllar:"

    with httpx.Client(timeout=30) as client:
        # Send confirmation message
        client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": telegram_chat_id, "text": text},
        )

        # Send variants PDF
        variants_path = get_local_path(variants_pdf_key)
        if variants_path.exists():
            with open(variants_path, "rb") as f:
                client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendDocument",
                    data={"chat_id": telegram_chat_id, "caption": "📋 Variantlar"},
                    files={"document": ("variants.pdf", f, "application/pdf")},
                )

        # Send answer key PDF
        key_path = get_local_path(answer_key_pdf_key)
        if key_path.exists():
            with open(key_path, "rb") as f:
                client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendDocument",
                    data={"chat_id": telegram_chat_id, "caption": "🔑 Javob kaliti"},
                    files={"document": ("answer_keys.pdf", f, "application/pdf")},
                )
