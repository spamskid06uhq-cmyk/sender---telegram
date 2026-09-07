import os
import re
import json
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Config ---
BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
SMTP_HOST     = os.environ.get("SMTP_HOST", "mail.gmx.net")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SECRET_CODE   = "zizi1306"
FREE_CREDITS  = 3
UNLIMITED     = -1
TIMEOUT_SEC   = 60

CREDITS_FILE = Path("credits.json")

# Contacts pré-enregistrés depuis l'env : "Alice:alice@gmail.com,Bob:bob@mail.fr"
def parse_contacts() -> list:
    raw = os.environ.get("CONTACTS", "")
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, email = entry.split(":", 1)
            result.append((name.strip(), email.strip()))
    return result

CONTACTS = parse_contacts()

# --- Crédits ---
def load_credits() -> dict:
    if CREDITS_FILE.exists():
        return json.loads(CREDITS_FILE.read_text())
    return {}

def save_credits(data: dict) -> None:
    CREDITS_FILE.write_text(json.dumps(data))

def get_credits(uid: str) -> int:
    return load_credits().get(uid, FREE_CREDITS)

def set_credits(uid: str, value: int) -> None:
    data = load_credits()
    data[uid] = value
    save_credits(data)

def use_credit(uid: str) -> bool:
    c = get_credits(uid)
    if c == UNLIMITED:
        return True
    if c <= 0:
        return False
    set_credits(uid, c - 1)
    return True

def credits_label(uid: str) -> str:
    c = get_credits(uid)
    return "Illimité" if c == UNLIMITED else str(c)

# --- States ---
MENU, RCPT_CHOICE, RCPT_INPUT, SUBJECT, MESSAGE, CONFIRM, CODE_INPUT = range(7)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- Claviers ---
def main_menu_kb(uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📧 Envoyer mail", callback_data="menu:email"),
            InlineKeyboardButton("🔑 Activer licence", callback_data="menu:code"),
        ],
        [
            InlineKeyboardButton(f"💳 Crédits : {credits_label(uid)}", callback_data="menu:credits"),
            InlineKeyboardButton("ℹ️ Aide", callback_data="menu:help"),
        ],
    ])

def recipient_kb() -> InlineKeyboardMarkup:
    rows = []
    for name, email in CONTACTS:
        rows.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"rcpt:{email}:{name}")])
    rows.append([InlineKeyboardButton("✏️ Entrer une adresse", callback_data="rcpt:custom")])
    rows.append([InlineKeyboardButton("❌ Annuler", callback_data="rcpt:cancel")])
    return InlineKeyboardMarkup(rows)

def step_kb(step: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("⏭️ Passer", callback_data=f"skip:{step}")]]
    if step == "message":
        rows.insert(0, [InlineKeyboardButton("✅ Envoyer maintenant", callback_data="skip:send_now")])
    rows.append([InlineKeyboardButton("❌ Annuler", callback_data="skip:cancel")])
    return InlineKeyboardMarkup(rows)

CONFIRM_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Envoyer", callback_data="confirm:send")],
    [InlineKeyboardButton("❌ Annuler", callback_data="confirm:cancel")],
])

# --- Timer ---
async def timeout_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        context.job.chat_id,
        "⏱️ Délai de 60 secondes expiré. Email annulé.\n\nUtilise /start pour recommencer.",
    )

def start_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: int) -> None:
    stop_timer(context)
    job = context.job_queue.run_once(
        timeout_callback, TIMEOUT_SEC, chat_id=chat_id, name=f"t_{uid}"
    )
    context.user_data["_job"] = job

def stop_timer(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.pop("_job", None)
    if job:
        job.schedule_removal()

# --- Menu principal ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Que veux-tu faire ?") -> int:
    stop_timer(context)
    uid = str(update.effective_user.id)
    kb = main_menu_kb(uid)
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
    return await show_menu(update, context, "Bienvenue ! Que veux-tu faire ?")

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = str(update.effective_user.id)

    if q.data == "menu:email":
        if get_credits(uid) == 0:
            await q.edit_message_text("Tu n'as plus de crédits. Active une licence 🔑.", reply_markup=main_menu_kb(uid))
            return MENU
        await q.edit_message_text("📧 Choisis un destinataire :", reply_markup=recipient_kb())
        return RCPT_CHOICE

    if q.data == "menu:code":
        await q.edit_message_text("🔑 Entre ton code de licence :")
        return CODE_INPUT

    if q.data == "menu:credits":
        await q.answer(f"Crédits restants : {credits_label(uid)}", show_alert=True)
        return MENU

    if q.data == "menu:help":
        await q.edit_message_text(
            f"ℹ️ Aide\n\n{FREE_CREDITS} emails gratuits à l'inscription.\n"
            "Code licence = envois illimités.\n"
            f"Chaque email a un délai de {TIMEOUT_SEC}s.",
            reply_markup=main_menu_kb(uid),
        )
        return MENU

    return MENU

# --- Destinataire ---
async def rcpt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "rcpt:cancel":
        return await show_menu(update, context, "Annulé.")

    if q.data == "rcpt:custom":
        await q.edit_message_text("✏️ Entre l'adresse email du destinataire :")
        return RCPT_INPUT

    parts = q.data.split(":", 2)
    if len(parts) == 3:
        email = parts[1]
        name = parts[2]
        context.user_data["rcpt"] = email
        start_timer(context, update.effective_chat.id, update.effective_user.id)
        await q.edit_message_text(
            f"📧 Destinataire : {name} <{email}>\n"
            f"⏱️ {TIMEOUT_SEC}s pour envoyer.\n\n"
            "Objet ? (optionnel)",
            reply_markup=step_kb("subject"),
        )
        return SUBJECT

    return RCPT_CHOICE

async def rcpt_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.message.text.strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Adresse invalide. Réessaie ou /annuler.")
        return RCPT_INPUT
    context.user_data["rcpt"] = email
    start_timer(context, update.effective_chat.id, update.effective_user.id)
    await update.message.reply_text(
        f"📧 Destinataire : {email}\n"
        f"⏱️ {TIMEOUT_SEC}s pour envoyer.\n\n"
        "Objet ? (optionnel)",
        reply_markup=step_kb("subject"),
    )
    return SUBJECT

# --- Objet ---
async def subject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "skip:cancel":
        return await show_menu(update, context, "Annulé.")
    context.user_data.setdefault("subject", "")
    await q.edit_message_text(
        f"📧 {context.user_data['rcpt']}\n"
        f"📋 Objet : {context.user_data['subject'] or '(aucun)'}\n"
        f"⏱️ {TIMEOUT_SEC}s\n\n"
        "Message ? (optionnel)",
        reply_markup=step_kb("message"),
    )
    return MESSAGE

async def subject_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["subject"] = update.message.text.strip()
    await update.message.reply_text(
        f"📧 {context.user_data['rcpt']}\n"
        f"📋 Objet : {context.user_data['subject']}\n"
        f"⏱️ {TIMEOUT_SEC}s\n\n"
        "Message ? (optionnel)",
        reply_markup=step_kb("message"),
    )
    return MESSAGE

# --- Message ---
async def message_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "skip:cancel":
        return await show_menu(update, context, "Annulé.")
    context.user_data.setdefault("body", "")
    if q.data == "skip:send_now":
        return await do_send(update, context)
    return await show_confirm(update, context)

async def message_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["body"] = update.message.text.strip()
    return await show_confirm(update, context)

# --- Confirmation ---
async def show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rcpt = context.user_data.get("rcpt", "")
    subj = context.user_data.get("subject") or "(aucun)"
    body = context.user_data.get("body") or "(aucun)"
    text = f"📧 {rcpt}\n📋 {subj}\n💬 {body}\n\nEnvoyer ?"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=CONFIRM_KB)
    else:
        await update.effective_message.reply_text(text, reply_markup=CONFIRM_KB)
    return CONFIRM

async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "confirm:send":
        return await do_send(update, context)
    return await show_menu(update, context, "Annulé.")

# --- Envoi ---
async def do_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stop_timer(context)
    uid = str(update.effective_user.id)

    if not use_credit(uid):
        msg = "Plus de crédits. Active une licence 🔑."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return await show_menu(update, context)

    rcpt = context.user_data.get("rcpt", "")
    subj = context.user_data.get("subject", "") or "(sans objet)"
    body = context.user_data.get("body", "") or "(sans message)"

    try:
        _send_email(rcpt, subj, body)
        result = f"✅ Email envoyé à {rcpt}."
    except Exception as exc:
        logger.exception("Échec envoi")
        result = f"❌ Échec : {exc}"

    if update.callback_query:
        await update.callback_query.edit_message_text(result)
    else:
        await update.effective_message.reply_text(result)

    context.user_data.clear()
    return await show_menu(update, context)

# --- Code licence ---
async def code_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = str(update.effective_user.id)
    if update.message.text.strip() == SECRET_CODE:
        set_credits(uid, UNLIMITED)
        await update.message.reply_text("✅ Licence activée. Envois illimités.")
    else:
        await update.message.reply_text("❌ Code invalide.")
    return await show_menu(update, context)

# --- Annuler ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stop_timer(context)
    context.user_data.clear()
    return await show_menu(update, context, "Annulé.")

# --- SMTP ---
def _send_email(to_addr: str, subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(SMTP_USER, [to_addr], msg.as_string())

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",   "Ouvrir le menu"),
        BotCommand("annuler", "Annuler"),
    ])

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU:       [CallbackQueryHandler(menu_cb, pattern="^menu:")],
            RCPT_CHOICE:[CallbackQueryHandler(rcpt_cb, pattern="^rcpt:")],
            RCPT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rcpt_text)],
            SUBJECT:    [
                CallbackQueryHandler(subject_cb, pattern="^skip:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, subject_text),
            ],
            MESSAGE:    [
                CallbackQueryHandler(message_cb, pattern="^skip:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, message_text),
            ],
            CONFIRM:    [CallbackQueryHandler(confirm_cb, pattern="^confirm:")],
            CODE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_text)],
        },
        fallbacks=[CommandHandler("annuler", cancel)],
    )

    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
