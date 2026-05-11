"""
AI Digest Bot — Telegram bot that sends a daily AI news digest as PDF.
Runs every morning at 07:00 Dubai time (UTC+4).
"""

import os
import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from digest_builder import build_digest
from pdf_generator import generate_pdf

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Chat IDs that receive the daily digest (comma-separated in env var)
DIGEST_CHAT_IDS = [
    int(x.strip())
    for x in os.environ.get("DIGEST_CHAT_IDS", "").split(",")
    if x.strip()
]
DUBAI_TZ = timezone(timedelta(hours=4))

# Conversation history per chat_id for the interactive Q&A mode
conversation_history: dict[int, list[dict]] = {}
# Last digest text per chat_id (so the bot can answer questions about it)
last_digest: dict[int, str] = {}

DIGEST_STORE_DIR = "/data"


def _digest_path(chat_id: int) -> str:
    os.makedirs(DIGEST_STORE_DIR, exist_ok=True)
    return os.path.join(DIGEST_STORE_DIR, f"last_digest_{chat_id}.txt")


def _save_last_digest(chat_id: int, text: str) -> None:
    try:
        with open(_digest_path(chat_id), "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"Saved last digest to disk for chat_id={chat_id}")
    except Exception as e:
        logger.warning(f"Could not save digest for {chat_id}: {e}")


def _load_last_digest(chat_id: int) -> Optional[str]:
    path = _digest_path(chat_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                logger.info(f"Loaded last digest from disk for chat_id={chat_id}")
                return text
        except Exception as e:
            logger.warning(f"Could not load digest for {chat_id}: {e}")
    return None


# ── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in DIGEST_CHAT_IDS:
        DIGEST_CHAT_IDS.append(chat_id)
    await update.message.reply_text(
        "👋 Привет! Я буду присылать тебе ежедневный AI-дайджест каждое утро в 7:00 по Дубаю.\n\n"
        "Команды:\n"
        "/digest — получить дайджест прямо сейчас\n"
        "/help — справка\n\n"
        "Или просто напиши мне любой вопрос — я отвечу на основе последнего дайджеста и своих знаний об ИИ."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>AI Digest Bot</b>\n\n"
        "Каждое утро в 7:00 (Дубай) я собираю свежие новости об ИИ из 50+ источников "
        "и отправляю тебе PDF-дайджест из 20 пунктов.\n\n"
        "<b>Команды:</b>\n"
        "/digest — сгенерировать и получить дайджест сейчас\n"
        "/start — зарегистрировать чат для ежедневной рассылки\n"
        "/help — эта справка\n\n"
        "<b>Интерактивный режим:</b>\n"
        "После получения дайджеста ты можешь написать мне любой вопрос — "
        "например <i>«Расскажи подробнее про пункт 3»</i> или "
        "<i>«Что это значит для рынка недвижимости?»</i>",
        parse_mode="HTML"
    )


async def send_digest_to_chat(app: Application, chat_id: int) -> None:
    """Build digest, generate PDF, send to Telegram chat."""
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text="⏳ Собираю утренний дайджест... Это займёт около минуты."
        )
        logger.info(f"Building digest for chat_id={chat_id}")

        digest_text = await build_digest()
        last_digest[chat_id] = digest_text
        _save_last_digest(chat_id, digest_text)
        conversation_history[chat_id] = []

        pdf_bytes = generate_pdf(digest_text)
        now_dubai = datetime.now(DUBAI_TZ)
        filename = f"AI_Digest_{now_dubai.strftime('%Y-%m-%d')}.pdf"

        await app.bot.send_document(
            chat_id=chat_id,
            document=InputFile(pdf_bytes, filename=filename),
            caption=(
                f"🗞 <b>AI-дайджест за {now_dubai.strftime('%d.%m.%Y')}</b>\n"
                "Задавай любые вопросы — я готов обсудить!"
            ),
            parse_mode="HTML"
        )
        logger.info(f"Digest sent to chat_id={chat_id}")

    except Exception as e:
        logger.error(f"Error sending digest to {chat_id}: {e}", exc_info=True)
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Ошибка при генерации дайджеста: {e}"
        )


async def digest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await send_digest_to_chat(ctx.application, chat_id)


async def chat_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form messages — interactive Q&A about the digest or AI topics."""
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    # Load digest from disk if not in memory (e.g. after redeployment)
    if chat_id not in last_digest:
        loaded = _load_last_digest(chat_id)
        if loaded:
            last_digest[chat_id] = loaded

    # Build system prompt
    digest_context = ""
    if chat_id in last_digest:
        digest_context = (
            "\n\nВот последний дайджест который был отправлен пользователю:\n"
            + last_digest[chat_id]
        )

    system_prompt = (
        "Ты — ассистент, который помогает разобраться в новостях из ежедневного AI-дайджеста. "
        "Отвечай ТОЛЬКО на русском языке.\n\n"
        "СТРОГИЕ ПРАВИЛА:\n"
        "1. ТОЛЬКО ФАКТЫ: отвечай исключительно на основе информации из дайджеста и его источников. "
        "Не придумывай объяснений, не строй гипотез, не додумывай детали. "
        "Если информации нет в дайджесте — прямо скажи: «В дайджесте эта информация отсутствует».\n"
        "2. ИСТОЧНИКИ: к каждому факту прикладывай ссылку на источник из дайджеста. "
        "Формат: «Источник: [название] — [url]». Если ссылки нет — укажи название источника.\n"
        "3. БЕЗ ГИПОТЕЗ: не используй фразы «возможно», «вероятно», «могло произойти», «скорее всего» — "
        "если пользователь не попросил тебя порассуждать.\n"
        "4. ФОРМАТИРОВАНИЕ: используй HTML-теги для Telegram. "
        "Жирный текст: <b>текст</b>. Курсив: <i>текст</i>. "
        "НЕ используй символы ** или ## — они не рендерятся в Telegram.\n"
        "5. СТРУКТУРА ОТВЕТА: заголовок пункта, затем факты из дайджеста, затем ссылка на источник."
        + digest_context
    )

    # Add user message to history
    conversation_history[chat_id].append({"role": "user", "content": user_text})

    # Keep last 20 messages to avoid huge context
    if len(conversation_history[chat_id]) > 20:
        conversation_history[chat_id] = conversation_history[chat_id][-20:]

    try:
        await update.message.chat.send_action("typing")

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": conversation_history[chat_id],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        assistant_reply = data["content"][0]["text"]
        conversation_history[chat_id].append(
            {"role": "assistant", "content": assistant_reply}
        )

        # Telegram limit is 4096 chars — split long replies into chunks
        MAX_MSG = 4000
        if len(assistant_reply) <= MAX_MSG:
            await update.message.reply_text(assistant_reply, parse_mode="HTML")
        else:
            # Split at paragraph boundaries to keep text readable
            chunks = []
            current = ""
            for paragraph in assistant_reply.split("\n\n"):
                if len(current) + len(paragraph) + 2 > MAX_MSG:
                    if current:
                        chunks.append(current.strip())
                    current = paragraph
                else:
                    current = current + "\n\n" + paragraph if current else paragraph
            if current:
                chunks.append(current.strip())
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Chat error for {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Не удалось получить ответ. Попробуй ещё раз."
        )


# ── Scheduler ───────────────────────────────────────────────────────────────

async def scheduled_digest(app: Application) -> None:
    logger.info("Scheduled digest triggered")
    for chat_id in list(DIGEST_CHAT_IDS):
        await send_digest_to_chat(app, chat_id)


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Dubai")
    scheduler.add_job(
        scheduled_digest,
        trigger=CronTrigger(hour=7, minute=0, timezone="Asia/Dubai"),
        args=[app],
        id="daily_digest",
        replace_existing=True,
    )
    return scheduler


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))

    scheduler = setup_scheduler(app)

    async def on_startup(application: Application) -> None:
        scheduler.start()
        logger.info("Scheduler started")

    async def on_shutdown(application: Application) -> None:
        scheduler.shutdown()

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
