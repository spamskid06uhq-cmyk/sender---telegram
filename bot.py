
"""
MAIL SENDER — Bot Telegram
Architecture : message unique édité en place, machine à états stricte
"""
import os
import re
import sqlite3
import asyncio
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MAIL_SENDER")

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
SECRET_CODE     = os.environ["SECRET_CODE"]
EMAIL_ADDRESS   = os.environ["EMAIL_ADDRESS"]   # adresse expéditeur vérifiée sur Brevo
BREVO_API_KEY   = os.environ["BREVO_API_KEY"]   # clé API Brevo
TIMEOUT_SEC     = 60
DB_PATH        = Path("licenses.db")

# ── États ──────────────────────────────────────────────────────────────────
IDLE              = "IDLE"
WAITING_LICENSE   = "WAITING_LICENSE"
EMAIL_MENU        = "EMAIL_MENU"
WAITING_RECIPIENT = "WAITING_RECIPIENT"
WAITING_SUBJECT   = "WAITING_SUBJECT"
WAITING_BODY      = "WAITING_BODY"

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── Données en mémoire ─────────────────────────────────────────────────────
user_states  : dict[int, str]          = {}
user_drafts  : dict[int, dict]         = {}
user_timers  : dict[int, asyncio.Task] = {}
user_sessions: dict[int, bool]         = {}

# ── SQLite ─────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                telegram_user_id INTEGER PRIMARY KEY,
                activated_at     TEXT    NOT NULL,
                active           INTEGER NOT NULL DEFAULT 1
            )
        """)
    logger.info("DB initialisée.")

def is_licensed(uid: int) -> bool:
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT 1 FROM licenses WHERE telegram_user_id=? AND active=1", (uid,)
        ).fetchone() is not None

def grant_license(uid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            INSERT INTO licenses (telegram_user_id, activated_at, active) VALUES (?,?,1)
            ON CONFLICT(telegram_user_id)
            DO UPDATE SET active=1, activated_at=excluded.activated_at
        """, (uid, now))
    logger.info(f"Licence accordée → uid={uid}")

# ── Claviers ───────────────────────────────────────────────────────────────
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📧 Envoyer un e-mail", callback_data="m:email")],
    [
        InlineKeyboardButton("📋 Mes contacts",  callback_data="m:contacts"),
        InlineKeyboardButton("⚙️ Paramètres",    callback_data="m:settings"),
    ],
    [InlineKeyboardButton("🔒 Verrouiller", callback_data="m:lock")],
])

CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Annuler la saisie", callback_data="e:back")],
])

def email_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Destinataire", callback_data="e:recipient")],
        [InlineKeyboardButton("📝 Objet",        callback_data="e:subject")],
        [InlineKeyboardButton("💬 Message",      callback_data="e:body")],
        [
            InlineKeyboardButton("📤 Envoyer", callback_data="e:send"),
            InlineKeyboardButton("❌ Annuler", callback_data="e:cancel"),
        ],
    ])

AFTER_SEND_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📧 Nouvel e-mail", callback_data="m:email"),
        InlineKeyboardButton("🏠 Menu",          callback_data="m:home"),
    ],
])

RETRY_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔄 Réessayer", callback_data="e:send"),
        InlineKeyboardButton("❌ Annuler",   callback_data="e:cancel"),
    ],
])

TIMEOUT_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏠 Menu", callback_data="m:home")],
])

# ── Textes ─────────────────────────────────────────────────────────────────
MAIN_TEXT = "✉️ MAIL SENDER\n\nQue veux-tu faire ?"

def draft_text(uid: int) -> str:
    d    = user_drafts.get(uid, {})
    rcpt = d.get("recipient") or "Non renseigné"
    subj = d.get("subject")   or "Non renseigné"
    body = d.get("body")      or "Non renseigné"
    secs = d.get("secs_left", TIMEOUT_SEC)
    return (
        "📧 ENVOI D'UN E-MAIL\n\n"
        f"⏱️ Temps restant : {secs} sec\n\n"
        f"📨 Destinataire :\n{rcpt}\n\n"
        f"📝 Objet :\n{subj}\n\n"
        f"💬 Message :\n{body}"
    )

# ── Édition du message central ─────────────────────────────────────────────
async def edit_main(bot, chat_id: int, msg_id: int,
                    text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text, reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"edit_main ignoré : {e}")

async def show_main_menu(bot, chat_id: int, msg_id: int = None) -> int:
    if msg_id:
        await edit_main(bot, chat_id, msg_id, MAIN_TEXT, MAIN_KB)
    else:
        m = await bot.send_message(chat_id, MAIN_TEXT, reply_markup=MAIN_KB)
        return m.message_id
    return msg_id

async def show_email_menu(bot, chat_id: int, uid: int) -> None:
    d      = user_drafts.get(uid, {})
    msg_id = d.get("msg_id")
    if msg_id:
        await edit_main(bot, chat_id, msg_id, draft_text(uid), email_kb())

async def show_input_prompt(bot, chat_id: int, msg_id: int, text: str) -> None:
    await edit_main(bot, chat_id, msg_id, text, CANCEL_KB)

# ── Timer ──────────────────────────────────────────────────────────────────
async def _timer(bot, chat_id: int, uid: int, msg_id: int) -> None:
    try:
        for remaining in range(TIMEOUT_SEC, 0, -1):
            await asyncio.sleep(1)
            if user_states.get(uid) not in (
                EMAIL_MENU, WAITING_RECIPIENT, WAITING_SUBJECT, WAITING_BODY
            ):
                return
            if uid in user_drafts:
                user_drafts[uid]["secs_left"] = remaining - 1

        # Expiré
        logger.info(f"Timeout → uid={uid}")
        user_states.pop(uid, None)
        user_drafts.pop(uid, None)
        user_timers.pop(uid, None)
        await edit_main(bot, chat_id, msg_id, "⏰ Temps écoulé.", TIMEOUT_KB)

    except asyncio.CancelledError:
        pass

def start_timer(bot, chat_id: int, uid: int, msg_id: int) -> None:
    stop_timer(uid)
    t = asyncio.create_task(_timer(bot, chat_id, uid, msg_id))
    user_timers[uid] = t

def stop_timer(uid: int) -> None:
    t = user_timers.pop(uid, None)
    if t and not t.done():
        t.cancel()

# ── Vérification session ───────────────────────────────────────────────────
def ok(uid: int) -> bool:
    return is_licensed(uid) and user_sessions.get(uid, False)

# ── Brevo API (HTTPS — jamais bloqué par les hébergeurs) ──────────────────
def _smtp_send(to: str, subject: str, body: str) -> None:
    logger.info(f"Brevo API → to={to}")
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "accept":       "application/json",
            "api-key":      BREVO_API_KEY,
            "content-type": "application/json",
        },
        json={
            "sender":      {"email": EMAIL_ADDRESS},
            "to":          [{"email": to}],
            "subject":     subject,
            "textContent": body,
        },
        timeout=20,
    )
    if not r.ok:
        raise Exception(f"Brevo {r.status_code}: {r.text}")
    logger.info(f"Brevo envoi OK → {to}")

# ── /start ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass

    stop_timer(uid)
    user_drafts.pop(uid, None)

    if is_licensed(uid):
        user_sessions[uid] = True
        user_states[uid]   = IDLE
        logger.info(f"/start autorisé uid={uid}")
        await show_main_menu(context.bot, chat_id)
    else:
        user_states[uid] = WAITING_LICENSE
        await context.bot.send_message(
            chat_id,
            "🔐 LICENCE REQUISE\n\nEntre ton code d'activation :"
        )

# ── /cancel ────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass
    stop_timer(uid)
    draft  = user_drafts.pop(uid, {})
    msg_id = draft.get("msg_id")
    user_states[uid] = IDLE
    await show_main_menu(context.bot, chat_id, msg_id)

# ── Messages texte ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()
    state   = user_states.get(uid, IDLE)

    try:
        await update.message.delete()
    except Exception:
        pass

    # ── Activation de licence ──────────────────────────────────────────────
    if state == WAITING_LICENSE:
        if text == SECRET_CODE:
            grant_license(uid)
            user_sessions[uid] = True
            user_states[uid]   = IDLE
            m = await context.bot.send_message(chat_id, "✅ Licence activée !")
            await asyncio.sleep(1.5)
            try:
                await m.delete()
            except Exception:
                pass
            await show_main_menu(context.bot, chat_id)
        else:
            await context.bot.send_message(
                chat_id, "❌ Code invalide.\n\nEntre ton code d'activation :"
            )
        return

    if not ok(uid):
        await context.bot.send_message(chat_id, "🔐 Session expirée. Utilise /start.")
        return

    draft  = user_drafts.get(uid, {})
    msg_id = draft.get("msg_id")

    # ── Destinataire ───────────────────────────────────────────────────────
    if state == WAITING_RECIPIENT:
        if not EMAIL_REGEX.match(text):
            await show_input_prompt(
                context.bot, chat_id, msg_id,
                "📨 Adresse invalide. Réessaie :"
            )
            return
        user_drafts[uid]["recipient"] = text
        user_states[uid] = EMAIL_MENU
        logger.info(f"Destinataire enregistré uid={uid} → {text}")
        await show_email_menu(context.bot, chat_id, uid)
        return

    # ── Objet ──────────────────────────────────────────────────────────────
    if state == WAITING_SUBJECT:
        user_drafts[uid]["subject"] = text
        user_states[uid] = EMAIL_MENU
        await show_email_menu(context.bot, chat_id, uid)
        return

    # ── Corps ──────────────────────────────────────────────────────────────
    if state == WAITING_BODY:
        user_drafts[uid]["body"] = text
        user_states[uid] = EMAIL_MENU
        await show_email_menu(context.bot, chat_id, uid)
        return

# ── Callbacks ──────────────────────────────────────────────────────────────
async def handle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q       = update.callback_query
    await q.answer()
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    data    = q.data
    msg_id  = q.message.message_id

    # ── Navigation principale ──────────────────────────────────────────────
    if data in ("m:home", "m:menu"):
        stop_timer(uid)
        user_drafts.pop(uid, None)
        user_states[uid] = IDLE
        await edit_main(context.bot, chat_id, msg_id, MAIN_TEXT, MAIN_KB)
        return

    if data == "m:lock":
        stop_timer(uid)
        user_drafts.pop(uid, None)
        user_sessions[uid] = False
        user_states[uid]   = IDLE
        logger.info(f"Session verrouillée uid={uid}")
        await edit_main(
            context.bot, chat_id, msg_id,
            "🔒 Session verrouillée.\n\nUtilise /start pour déverrouiller.",
            InlineKeyboardMarkup([])
        )
        return

    if data in ("m:contacts", "m:settings"):
        await q.answer("Fonctionnalité à venir.", show_alert=True)
        return

    # ── Vérification session ───────────────────────────────────────────────
    if not ok(uid):
        await q.answer("🔐 Session expirée. Utilise /start.", show_alert=True)
        return

    # ── Ouvrir le menu e-mail ──────────────────────────────────────────────
    if data == "m:email":
        user_drafts[uid] = {
            "recipient": "",
            "subject":   "",
            "body":      "",
            "msg_id":    msg_id,
            "secs_left": TIMEOUT_SEC,
        }
        user_states[uid] = EMAIL_MENU
        start_timer(context.bot, chat_id, uid, msg_id)
        await show_email_menu(context.bot, chat_id, uid)
        return

    # ── Saisies (éditent le message central) ──────────────────────────────
    if data == "e:recipient":
        user_states[uid] = WAITING_RECIPIENT
        await show_input_prompt(context.bot, chat_id, msg_id, "📨 Entrez l'adresse e-mail :")
        return

    if data == "e:subject":
        user_states[uid] = WAITING_SUBJECT
        await show_input_prompt(context.bot, chat_id, msg_id, "📝 Entrez l'objet :")
        return

    if data == "e:body":
        user_states[uid] = WAITING_BODY
        await show_input_prompt(context.bot, chat_id, msg_id, "💬 Entrez votre message :")
        return

    if data == "e:back":
        user_states[uid] = EMAIL_MENU
        await show_email_menu(context.bot, chat_id, uid)
        return

    if data == "e:cancel":
        stop_timer(uid)
        user_drafts.pop(uid, None)
        user_states[uid] = IDLE
        await edit_main(context.bot, chat_id, msg_id, MAIN_TEXT, MAIN_KB)
        return

    # ── Envoi ──────────────────────────────────────────────────────────────
    if data == "e:send":
        draft     = user_drafts.get(uid, {})
        recipient = draft.get("recipient", "").strip()

        if not recipient:
            await q.answer("❌ Destinataire manquant.", show_alert=True)
            return

        subject = draft.get("subject", "").strip() or "(sans objet)"
        body    = draft.get("body",    "").strip() or "(sans message)"

        stop_timer(uid)
        await edit_main(
            context.bot, chat_id, msg_id,
            "📤 Envoi en cours...",
            InlineKeyboardMarkup([])
        )

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _smtp_send, recipient, subject, body
            )
            user_states[uid] = IDLE
            user_drafts.pop(uid, None)
            logger.info(f"E-mail envoyé uid={uid} → {recipient}")
            await edit_main(
                context.bot, chat_id, msg_id,
                f"✅ E-mail envoyé !\n\n📨 {recipient}",
                AFTER_SEND_KB,
            )
        except Exception as exc:
            logger.error(f"Brevo erreur uid={uid} : {exc}")
            await edit_main(
                context.bot, chat_id, msg_id,
                f"❌ Échec de l'envoi.\n\nErreur : {exc}",
                RETRY_KB,
            )
        return

# ── Démarrage ──────────────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Démarrer / Se connecter"),
        BotCommand("cancel", "Annuler"),
    ])

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("MAIL SENDER démarré.")
    app.run_polling()

if __name__ == "__main__":
    main()
