"""
MAIL SENDER — Bot Telegram d'envoi d'e-mails
Architecture : machine à états manuelle (pas de ConversationHandler)
Persistance  : SQLite (licenses.db)
"""
import os
import re
import sqlite3
import asyncio
import smtplib
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# CONFIG (variables d'environnement uniquement)
# ──────────────────────────────────────────────
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
SECRET_CODE    = os.environ["SECRET_CODE"]
EMAIL_ADDRESS  = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
SMTP_HOST      = os.environ.get("SMTP_HOST", "mail.gmx.net")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
TIMEOUT_SEC    = 60

# ──────────────────────────────────────────────
# BASE DE DONNÉES
# ──────────────────────────────────────────────
DB_PATH = Path("licenses.db")

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                telegram_user_id INTEGER PRIMARY KEY,
                activated_at     TEXT    NOT NULL,
                active           INTEGER NOT NULL DEFAULT 1
            )
        """)
    logger.info("DB initialisée.")

def is_licensed(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM licenses WHERE telegram_user_id = ? AND active = 1",
            (user_id,)
        ).fetchone()
    return row is not None

def grant_license(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO licenses (telegram_user_id, activated_at, active)
            VALUES (?, ?, 1)
            ON CONFLICT(telegram_user_id)
            DO UPDATE SET active = 1, activated_at = excluded.activated_at
        """, (user_id, now))
    logger.info(f"Licence accordée → user_id={user_id}")

# ──────────────────────────────────────────────
# ÉTATS (machine à états par utilisateur)
# ──────────────────────────────────────────────
IDLE              = "IDLE"
WAITING_LICENSE   = "WAITING_LICENSE"
EMAIL_MENU        = "EMAIL_MENU"
WAITING_RECIPIENT = "WAITING_RECIPIENT"
WAITING_SUBJECT   = "WAITING_SUBJECT"
WAITING_BODY      = "WAITING_BODY"

user_states  : dict[int, str]  = {}
user_drafts  : dict[int, dict] = {}
user_timers  : dict[int, asyncio.Task] = {}
user_sessions: dict[int, bool] = {}  # True = déverrouillé

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ──────────────────────────────────────────────
# CLAVIERS
# ──────────────────────────────────────────────
MAIN_MENU_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📧 Envoyer un e-mail", callback_data="m:email")],
    [
        InlineKeyboardButton("📋 Mes contacts",  callback_data="m:contacts"),
        InlineKeyboardButton("⚙️ Paramètres",    callback_data="m:settings"),
    ],
    [InlineKeyboardButton("🔒 Verrouiller", callback_data="m:lock")],
])

def email_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Destinataire", callback_data="e:recipient")],
        [InlineKeyboardButton("📝 Objet",        callback_data="e:subject")],
        [InlineKeyboardButton("💬 Message",      callback_data="e:body")],
        [
            InlineKeyboardButton("📤 Envoyer",  callback_data="e:send"),
            InlineKeyboardButton("❌ Annuler",  callback_data="e:cancel"),
        ],
    ])

def email_menu_text(user_id: int) -> str:
    draft = user_drafts.get(user_id, {})
    rcpt  = draft.get("recipient") or "Non renseigné"
    subj  = draft.get("subject")   or "Non renseigné"
    body  = draft.get("body")      or "Non renseigné"
    secs  = draft.get("secs_left", TIMEOUT_SEC)
    return (
        "📧 ENVOI D'UN E-MAIL\n\n"
        f"⏱️ Temps restant : {secs} sec\n\n"
        f"📨 Destinataire :\n{rcpt}\n\n"
        f"📝 Objet :\n{subj}\n\n"
        f"💬 Message :\n{body}"
    )

AFTER_SEND_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📧 Nouvel e-mail", callback_data="m:email"),
        InlineKeyboardButton("🏠 Menu",          callback_data="m:home"),
    ],
])

TIMEOUT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏠 Menu", callback_data="m:home")],
])

# ──────────────────────────────────────────────
# TIMER
# ──────────────────────────────────────────────
async def _run_timer(bot, chat_id: int, user_id: int, msg_id: int) -> None:
    try:
        for remaining in range(TIMEOUT_SEC, 0, -1):
            await asyncio.sleep(1)
            if user_states.get(user_id) not in (EMAIL_MENU, WAITING_RECIPIENT, WAITING_SUBJECT, WAITING_BODY):
                return
            if user_id in user_drafts:
                user_drafts[user_id]["secs_left"] = remaining - 1

        # Timeout atteint
        user_states.pop(user_id, None)
        user_drafts.pop(user_id, None)
        user_timers.pop(user_id, None)
        logger.info(f"Timeout email → user_id={user_id}")
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="⏰ Temps écoulé.",
                reply_markup=TIMEOUT_KB,
            )
        except Exception:
            await bot.send_message(chat_id, "⏰ Temps écoulé.", reply_markup=TIMEOUT_KB)
    except asyncio.CancelledError:
        pass

def start_timer(bot, chat_id: int, user_id: int, msg_id: int) -> None:
    stop_timer(user_id)
    task = asyncio.create_task(_run_timer(bot, chat_id, user_id, msg_id))
    user_timers[user_id] = task

def stop_timer(user_id: int) -> None:
    task = user_timers.pop(user_id, None)
    if task and not task.done():
        task.cancel()

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
async def send_main_menu(bot, chat_id: int, edit_msg_id: int = None) -> None:
    text = "✉️ MAIL SENDER\n\nQue veux-tu faire ?"
    if edit_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=edit_msg_id,
                text=text, reply_markup=MAIN_MENU_KB,
            )
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=MAIN_MENU_KB)

async def refresh_email_menu(bot, chat_id: int, user_id: int) -> None:
    draft  = user_drafts.get(user_id, {})
    msg_id = draft.get("msg_id")
    text   = email_menu_text(user_id)
    kb     = email_menu_kb()
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, reply_markup=kb,
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=kb)
    if user_id in user_drafts:
        user_drafts[user_id]["msg_id"] = sent.message_id

def require_session(user_id: int) -> bool:
    return is_licensed(user_id) and user_sessions.get(user_id, False)

# ──────────────────────────────────────────────
# HANDLERS — COMMANDES
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if is_licensed(user_id):
        user_sessions[user_id] = True
        user_states[user_id]   = IDLE
        stop_timer(user_id)
        user_drafts.pop(user_id, None)
        logger.info(f"/start — utilisateur autorisé user_id={user_id}")
        await send_main_menu(context.bot, chat_id)
    else:
        user_states[user_id] = WAITING_LICENSE
        logger.info(f"/start — licence requise user_id={user_id}")
        await update.message.reply_text(
            "🔐 LICENCE REQUISE\n\nEntre ton code d'activation :"
        )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    stop_timer(user_id)
    draft  = user_drafts.pop(user_id, {})
    msg_id = draft.get("msg_id")
    user_states[user_id] = IDLE
    logger.info(f"/cancel — user_id={user_id}")
    await send_main_menu(context.bot, chat_id, msg_id)

# ──────────────────────────────────────────────
# HANDLERS — MESSAGES TEXTE
# ──────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()
    state   = user_states.get(user_id, IDLE)

    # Supprimer le message utilisateur (interface propre)
    try:
        await update.message.delete()
    except Exception:
        pass

    # ── Activation de licence ──
    if state == WAITING_LICENSE:
        if text == SECRET_CODE:
            grant_license(user_id)
            user_sessions[user_id] = True
            user_states[user_id]   = IDLE
            await context.bot.send_message(chat_id, "✅ Licence activée !")
            await send_main_menu(context.bot, chat_id)
        else:
            await context.bot.send_message(
                chat_id,
                "❌ Code invalide.\n\nEntre ton code d'activation :"
            )
        return

    # ── Vérification session ──
    if not require_session(user_id):
        await context.bot.send_message(chat_id, "🔐 Session expirée. Utilise /start.")
        return

    # ── Destinataire ──
    if state == WAITING_RECIPIENT:
        if not EMAIL_REGEX.match(text):
            await context.bot.send_message(chat_id, "❌ Adresse invalide. Réessaie.")
            return
        user_drafts[user_id]["recipient"] = text
        user_states[user_id] = EMAIL_MENU
        logger.info(f"Destinataire enregistré — user_id={user_id} → {text}")
        await refresh_email_menu(context.bot, chat_id, user_id)
        return

    # ── Objet ──
    if state == WAITING_SUBJECT:
        user_drafts[user_id]["subject"] = text
        user_states[user_id] = EMAIL_MENU
        logger.info(f"Objet enregistré — user_id={user_id}")
        await refresh_email_menu(context.bot, chat_id, user_id)
        return

    # ── Corps du message ──
    if state == WAITING_BODY:
        user_drafts[user_id]["body"] = text
        user_states[user_id] = EMAIL_MENU
        logger.info(f"Corps enregistré — user_id={user_id}")
        await refresh_email_menu(context.bot, chat_id, user_id)
        return

# ──────────────────────────────────────────────
# HANDLERS — CALLBACKS
# ──────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    data    = query.data
    msg_id  = query.message.message_id

    # ── Navigation principale ──
    if data in ("m:home", "m:menu"):
        stop_timer(user_id)
        user_drafts.pop(user_id, None)
        user_states[user_id] = IDLE
        await send_main_menu(context.bot, chat_id, msg_id)
        return

    if data == "m:lock":
        stop_timer(user_id)
        user_drafts.pop(user_id, None)
        user_sessions[user_id] = False
        user_states[user_id]   = IDLE
        logger.info(f"Session verrouillée — user_id={user_id}")
        await query.edit_message_text(
            "🔒 Session verrouillée.\n\nUtilise /start pour déverrouiller."
        )
        return

    if data == "m:contacts":
        await query.answer("📋 Fonctionnalité à venir.", show_alert=True)
        return

    if data == "m:settings":
        await query.answer("⚙️ Fonctionnalité à venir.", show_alert=True)
        return

    # ── Vérification session avant actions e-mail ──
    if not require_session(user_id):
        await query.answer("🔐 Session expirée. Utilise /start.", show_alert=True)
        return

    # ── Ouvrir le menu e-mail ──
    if data == "m:email":
        user_drafts[user_id] = {
            "recipient": "",
            "subject":   "",
            "body":      "",
            "msg_id":    msg_id,
            "secs_left": TIMEOUT_SEC,
        }
        user_states[user_id] = EMAIL_MENU
        start_timer(context.bot, chat_id, user_id, msg_id)
        await refresh_email_menu(context.bot, chat_id, user_id)
        return

    # ── Actions dans le menu e-mail ──
    if data == "e:recipient":
        user_states[user_id] = WAITING_RECIPIENT
        await context.bot.send_message(chat_id, "📨 Entrez l'adresse e-mail :")
        return

    if data == "e:subject":
        user_states[user_id] = WAITING_SUBJECT
        await context.bot.send_message(chat_id, "📝 Entrez l'objet :")
        return

    if data == "e:body":
        user_states[user_id] = WAITING_BODY
        await context.bot.send_message(chat_id, "💬 Entrez votre message :")
        return

    if data == "e:cancel":
        stop_timer(user_id)
        user_drafts.pop(user_id, None)
        user_states[user_id] = IDLE
        await send_main_menu(context.bot, chat_id, msg_id)
        return

    if data == "e:send":
        draft     = user_drafts.get(user_id, {})
        recipient = draft.get("recipient", "").strip()

        if not recipient:
            await query.answer("❌ Destinataire manquant.", show_alert=True)
            return

        subject = draft.get("subject", "").strip() or "(sans objet)"
        body    = draft.get("body",    "").strip() or "(sans message)"

        stop_timer(user_id)

        try:
            await query.edit_message_text("📤 Envoi en cours...")
        except Exception:
            pass

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _send_smtp, recipient, subject, body
            )
            logger.info(f"E-mail envoyé → {recipient} (user_id={user_id})")
            user_states[user_id] = IDLE
            user_drafts.pop(user_id, None)
            await query.edit_message_text(
                f"✅ E-mail envoyé !\n\n📨 {recipient}",
                reply_markup=AFTER_SEND_KB,
            )
        except Exception as exc:
            logger.error(f"Échec SMTP user_id={user_id} : {exc}")
            await query.edit_message_text(
                f"❌ Échec de l'envoi.\n\nErreur : {type(exc).__name__}: {exc}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔄 Réessayer", callback_data="e:send"),
                        InlineKeyboardButton("❌ Annuler",   callback_data="e:cancel"),
                    ],
                ]),
            )
        return

# ──────────────────────────────────────────────
# SMTP
# ──────────────────────────────────────────────
def _send_smtp(to_addr: str, subject: str, body: str) -> None:
    logger.info(f"SMTP connect → {SMTP_HOST}:{SMTP_PORT}")
    msg = EmailMessage()
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        s.send_message(msg)
    logger.info(f"SMTP envoi OK → {to_addr}")

# ──────────────────────────────────────────────
# DÉMARRAGE
# ──────────────────────────────────────────────
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Démarrer / Se connecter"),
        BotCommand("cancel", "Annuler l'opération en cours"),
    ])

def main() -> None:
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot démarré.")
    app.run_polling()

if __name__ == "__main__":
    main()

