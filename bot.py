import os
import re
import json
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

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

# --- Configuration ---
BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
SMTP_HOST    = os.environ.get("SMTP_HOST", "mail.gmx.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]

SECRET_CODE     = "zizi1306"
FREE_CREDITS    = 3          # crédits offerts à chaque nouvel utilisateur
UNLIMITED       = -1         # valeur = crédits illimités

CREDITS_FILE = Path("credits.json")

# --- Persistance des crédits ---
def load_credits() -> dict:
    if CREDITS_FILE.exists():
        return json.loads(CREDITS_FILE.read_text())
    return {}

def save_credits(data: dict) -> None:
    CREDITS_FILE.write_text(json.dumps(data))

def get_credits(user_id: str) -> int:
    data = load_credits()
    return data.get(user_id, FREE_CREDITS)

def set_credits(user_id: str, value: int) -> None:
    data = load_credits()
    data[user_id] = value
    save_credits(data)

def use_credit(user_id: str) -> bool:
    c = get_credits(user_id)
    if c == UNLIMITED:
        return True
    if c <= 0:
        return False
    set_credits(user_id, c - 1)
    return True

def credits_label(user_id: str) -> str:
    c = get_credits(user_id)
    return "Illimité" if c == UNLIMITED else str(c)

# --- États ---
MENU, RECIPIENT, SUBJECT, MESSAGE, CONFIRM, CODE_INPUT = range(6)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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


def main_menu_kb(user_id: str) -> InlineKeyboardMarkup:
    c = credits_label(user_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📧 Envoyer mail", callback_data="menu:email"),
            InlineKeyboardButton("🔑 Activer licence", callback_data="menu:code"),
        ],
        [
            InlineKeyboardButton(f"💳 Crédits : {c}", callback_data="menu:credits"),
            InlineKeyboardButton("ℹ️ Aide", callback_data="menu:help"),
        ],
    ])

CONFIRM_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Envoyer", callback_data="confirm:send")],
    [InlineKeyboardButton("✏️ Recommencer", callback_data="confirm:restart")],
    [InlineKeyboardButton("❌ Annuler", callback_data="confirm:cancel")],
])


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Que veux-tu faire ?") -> int:
    uid = str(update.effective_user.id)
    kb  = main_menu_kb(uid)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    uid = str(update.effective_user.id)
    if uid not in load_credits():
        set_credits(uid, FREE_CREDITS)
    return await send_menu(update, context, "Bienvenue ! Que veux-tu faire ?")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)

    if query.data == "menu:email":
        c = get_credits(uid)
        if c == 0:
            await query.edit_message_text(
                "Tu n'as plus de crédits. Active une licence avec le bouton 🔑.",
                reply_markup=main_menu_kb(uid),
            )
            return MENU
        await query.edit_message_text("Quel est le destinataire ? (adresse email)")
        return RECIPIENT

    if query.data == "menu:code":
        await query.edit_message_text("Entre ton code de licence :")
        return CODE_INPUT

    if query.data == "menu:credits":
        c = credits_label(uid)
        await query.answer(f"Crédits restants : {c}", show_alert=True)
        return MENU

    if query.data == "menu:help":
        await query.edit_message_text(
            "ℹ️ Aide\n\n"
            f"Tu as {FREE_CREDITS} emails gratuits à l'inscription.\n"
            "Active une licence avec le code secret pour des envois illimités.",
            reply_markup=main_menu_kb(uid),
        )
        return MENU

    return MENU


async def code_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = str(update.effective_user.id)
    code = update.message.text.strip()

    if code == SECRET_CODE:
        set_credits(uid, UNLIMITED)
        await update.message.reply_text("✅ Licence activée. Envois illimités.")
    else:
        await update.message.reply_text("❌ Code invalide.")

    return await send_menu(update, context)


async def recipient_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    recipient = update.message.text.strip()
    if not EMAIL_REGEX.match(recipient):
        await update.message.reply_text("Cette adresse ne semble pas valide. Réessaie, ou /annuler.")
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
    uid = str(update.effective_user.id)

    if query.data == "confirm:send":
        if not use_credit(uid):
            await query.edit_message_text("Plus de crédits. Active une licence.")
            return await send_menu(update, context)

        from_header = CUSTOM_FROM_MAP.get(uid, SMTP_USER)
        recipient   = context.user_data.get("recipient")
        subject     = context.user_data.get("subject")
        body        = context.user_data.get("body")

        try:
            send_email(from_header, recipient, subject, body)
            await query.edit_message_text(f"✅ Email envoyé à {recipient}.")
        except Exception as exc:
            logger.exception("Échec d'envoi")
            await query.edit_message_text(f"❌ Échec : {exc}")

        context.user_data.clear()
        return await send_menu(update, context)

    if query.data == "confirm:restart":
        context.user_data.clear()
        await query.edit_message_text("Quel est le destinataire ? (adresse email)")
        return RECIPIENT

    context.user_data.clear()
    await query.edit_message_text("Annulé.")
    return await send_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    return await send_menu(update, context, "Annulé.")


def send_email(from_header: str, to_addr: str, subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"]    = from_header
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_addr], msg.as_string())


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",   "Ouvrir le menu"),
        BotCommand("annuler", "Annuler l'opération en cours"),
    ])


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU:      [CallbackQueryHandler(menu_callback, pattern="^menu:")],
            RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recipient_received)],
            SUBJECT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, subject_received)],
            MESSAGE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, message_received)],
            CONFIRM:   [CallbackQueryHandler(confirm_callback, pattern="^confirm:")],
            CODE_INPUT:[MessageHandler(filters.TEXT & ~filters.COMMAND, code_received)],
        },
        fallbacks=[CommandHandler("annuler", cancel)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()

