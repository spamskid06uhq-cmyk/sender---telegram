
import os
import re
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Configuration (via variables d'environnement) ---
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]          # ex: toi@gmail.com
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]  # mot de passe d'application

# Mapping optionnel : chaque utilisateur Telegram peut avoir son propre
# "From" personnalisé (nom affiché + adresse), sinon on retombe sur SMTP_USER.
# Format: "id_telegram:Nom Affiché <email@exemple.com>,id2:..."
CUSTOM_FROM_RAW = os.environ.get("CUSTOM_FROM_MAP", "")


def build_custom_from_map(raw: str) -> dict:
    mapping = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        user_id, from_value = entry.split(":", 1)
        mapping[user_id.strip()] = from_value.strip()
    return mapping


CUSTOM_FROM_MAP = build_custom_from_map(CUSTOM_FROM_RAW)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- États de la conversation ---
MENU, RECIPIENT, SUBJECT, MESSAGE, CONFIRM = range(5)

MAIN_MENU_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📧 Nouvel email", callback_data="menu:new_email")],
        [InlineKeyboardButton("ℹ️ Aide", callback_data="menu:help")],
    ]
)

CONFIRM_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✅ Envoyer", callback_data="confirm:send")],
        [InlineKeyboardButton("✏️ Recommencer", callback_data="confirm:restart")],
        [InlineKeyboardButton("❌ Annuler", callback_data="confirm:cancel")],
    ]
)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Que veux-tu faire ?") -> int:
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=MAIN_MENU_KB)
    else:
        await update.message.reply_text(text, reply_markup=MAIN_MENU_KB)
    return MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    return await send_menu(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "menu:new_email":
        await query.edit_message_text("✅ Nouvel email\n\nQuel est le destinataire ? (adresse email)")
        return RECIPIENT

    if query.data == "menu:help":
        await query.edit_message_text(
            "ℹ️ Aide\n\nCe bot envoie un email : destinataire, objet, message, "
            "puis confirmation avant l'envoi.",
            reply_markup=MAIN_MENU_KB,
        )
        return MENU

    return await send_menu(update, context)


async def recipient_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    recipient = update.message.text.strip()
    if not EMAIL_REGEX.match(recipient):
        await update.message.reply_text(
            "Cette adresse ne semble pas valide. Réessaie, ou /annuler."
        )
        return RECIPIENT

    context.user_data["recipient"] = recipient
    await update.message.reply_text("Objet du message ?")
    return SUBJECT


async def subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["subject"] = update.message.text.strip()
    await update.message.reply_text("Contenu du message ?")
    return MESSAGE


async def message_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["body"] = update.message.text.strip()

    recap = (
        f"Destinataire : {context.user_data['recipient']}\n"
        f"Objet : {context.user_data['subject']}\n"
        f"Message : {context.user_data['body']}\n\n"
        "Confirmer l'envoi ?"
    )
    await update.message.reply_text(recap, reply_markup=CONFIRM_KB)
    return CONFIRM


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:send":
        user_id = str(update.effective_user.id)
        from_header = CUSTOM_FROM_MAP.get(user_id, SMTP_USER)

        recipient = context.user_data.get("recipient")
        subject = context.user_data.get("subject")
        body = context.user_data.get("body")

        try:
            send_email(from_header, recipient, subject, body)
            await query.edit_message_text(f"✅ Email envoyé à {recipient}.")
        except Exception as exc:
            logger.exception("Échec d'envoi de l'email")
            await query.edit_message_text(f"❌ Échec de l'envoi : {exc}")

        context.user_data.clear()
        return await send_menu(update, context)

    if query.data == "confirm:restart":
        context.user_data.clear()
        await query.edit_message_text("Quel est le destinataire ? (adresse email)")
        return RECIPIENT

    context.user_data.clear()
    await query.edit_message_text("❌ Annulé.")
    return await send_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Annulé.")
    return await send_menu(update, context)


def send_email(from_header: str, to_addr: str, subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = from_header
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_addr], msg.as_string())


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Ouvrir le menu"),
            BotCommand("email", "Envoyer un nouvel email"),
            BotCommand("annuler", "Annuler l'opération en cours"),
        ]
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("email", start), CommandHandler("start", start)],
        states={
            MENU: [CallbackQueryHandler(menu_callback, pattern="^menu:")],
            RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recipient_received)],
            SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, subject_received)],
            MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, message_received)],
            CONFIRM: [CallbackQueryHandler(confirm_callback, pattern="^confirm:")],
        },
        fallbacks=[CommandHandler("annuler", cancel)],
    )

    app.add_handler(conv_handler)
    app.run_polling()


if __name__ == "__main__":
    main()
