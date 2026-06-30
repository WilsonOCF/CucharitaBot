#!/usr/bin/env python3
"""
bot.py – Telegram handlers y punto de entrada del bot.
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import BOT_TOKEN
from scraper import generate_ticket, parse_message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HELP_TEXT = (
    "*Bot del Comedor Universitario UNMSM*\n\n"
    "Envíame tu código de alumno para generar el ticket de almuerzo.\n\n"
    "*Formatos aceptados:*\n"
    "  `385`               → elige el turno con más disponibilidad\n"
    "  `385 nivel1 turno2` → solicita nivel(piso) y turno específicos\n"
    "  `385 n2 t1`         → forma abreviada\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = (update.message.text or "").strip()
    codigo, nivel, turno = parse_message(raw)

    if not codigo:
        await update.message.reply_text(
            "No reconocí ese formato.\n\n"
            "Ejemplos válidos:\n"
            "  `385`\n"
            "  `385 nivel1 turno2`",
            parse_mode="Markdown",
        )
        return

    detalle = f"Nivel {nivel}, Turno {turno}" if nivel else "mejor disponibilidad automática"
    wait_msg = await update.message.reply_text(
        f"⏳ Generando ticket para `{codigo}` ({detalle})…",
        parse_mode="Markdown",
    )

    result = await generate_ticket(codigo, nivel, turno)

    if result["success"]:
        await wait_msg.delete()
        pdf_path = result["pdf_path"]
        with open(pdf_path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=f"ticket_{codigo}.pdf",
                caption=(
                    f"🍽️ *Ticket generado exitosamente*\n"
                    f"Código: `{codigo}`\n"
                    f"Opción: {result['ticket_info']}"
                ),
                parse_mode="Markdown",
            )
        try:
            os.remove(pdf_path)
        except OSError:
            pass
    else:
        await wait_msg.edit_text(
            f"❌ {result['message']}",
            parse_mode="Markdown",
        )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot del Comedor UNMSM iniciado. Esperando mensajes…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
