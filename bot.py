import os
import re
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from telegram import (
    Update,
    ReplyKeyboardRemove,
    ReplyKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

MAIN_MENU = ReplyKeyboardMarkup(
    [["📧 Nouvel email"], ["ℹ️ Aide"]],
    resize_keyboard=True,
)

CONFIRM_MENU = ReplyKeyboardMarkup(
    [["✅ Envoyer", "✏️ Recommencer"], ["❌ Annuler"]],
    resize_keyboard=True,
)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Que veux-tu faire ?", reply_markup=MAIN_MENU
    )
    return MENU


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()

    if choice == "📧 Nouvel email":
        await update.message.reply_text(
            "Quel est le destinataire ? (adresse email)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return RECIPIENT

    if choice == "ℹ️ Aide":
        await update.message.reply_text(
            "Ce bot envoie un email : destinataire, objet, message, puis "
            "confirmation avant l'envoi.",
            reply_markup=MAIN_MENU,
        )
        return MENU

    await update.message.reply_text("Choisis une option du menu.", reply_markup=MAIN_MENU)
    return MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await show_menu(update, context)


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
    await update.message.reply_text(recap, reply_markup=CONFIRM_MENU)
    return CONFIRM


async def confirm_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()

    if choice == "✅ Envoyer":
        user_id = str(update.effective_user.id)
        from_header = CUSTOM_FROM_MAP.get(user_id, SMTP_USER)

        recipient = context.user_data["recipient"]
        subject = context.user_data["subject"]
        body = context.user_data["body"]

        try:
            send_email(from_header, recipient, subject, body)
            await update.message.reply_text(f"Email envoyé à {recipient}.")
        except Exception as exc:
            logger.exception("Échec d'envoi de l'email")
            await update.message.reply_text(f"Échec de l'envoi : {exc}")

        context.user_data.clear()
        return await show_menu(update, context)

    if choice == "✏️ Recommencer":
        context.user_data.clear()
        await update.message.reply_text(
            "Quel est le destinataire ? (adresse email)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return RECIPIENT

    context.user_data.clear()
    return await cancel(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Annulé.", reply_markup=MAIN_MENU)
    return MENU


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
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recipient_received)],
            SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, subject_received)],
            MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, message_received)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_choice)],
        },
        fallbacks=[CommandHandler("annuler", cancel)],
    )

    app.add_handler(conv_handler)
    app.run_polling()


if __name__ == "__main__":
    main()
