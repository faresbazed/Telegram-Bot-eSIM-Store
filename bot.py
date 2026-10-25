import asyncio
import html
import io
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import qrcode
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID, QR_DIR
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==============================================================================
# Helper Functions
# ==============================================================================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and ADMIN_ID != 0


def get_currency() -> str:
    return db.get_setting("currency", "USD")


def generate_qr_bytes(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("state", None)
    context.user_data.pop("temp", None)


async def check_ban_guard(update: Update) -> bool:
    """Returns True if the user is banned and should be blocked."""
    user = update.effective_user
    if not user:
        return False
    if is_admin(user.id):
        return False
    if db.is_user_banned(user.id):
        support = db.get_setting("support_username", "Support").lstrip("@")
        msg = (
            "â <b>Ø¹Ø°Ø±Ø§ÙØ ØªÙ Ø­Ø¸Ø± Ø­Ø³Ø§Ø¨Ù ÙÙ Ø§Ø³ØªØ®Ø¯Ø§Ù ÙØ°Ø§ Ø§ÙØ¨ÙØª.</b>\n\n"
            f"ÙÙØªÙØ§ØµÙ ÙØ¹ Ø§ÙØ¥Ø¯Ø§Ø±Ø© ÙØ§ÙØ§Ø³ØªÙØ³Ø§Ø±: @{html.escape(support)}"
        )
        if update.callback_query:
            await update.callback_query.answer("â Ø­Ø³Ø§Ø¨Ù ÙØ­Ø¸ÙØ± ÙÙ Ø§Ø³ØªØ®Ø¯Ø§Ù Ø§ÙØ¨ÙØª.", show_alert=True)
        elif update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return True
    return False


async def safe_edit_or_send(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if update.callback_query:
        q = update.callback_query
        try:
            if q.message and q.message.photo:
                await q.message.delete()
                await q.message.chat.send_message(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            else:
                await q.edit_message_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            if q.message:
                await q.message.chat.send_message(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
    elif update.message:
        await update.message.reply_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def deliver_esim_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    esim: Dict[str, Any],
    header_title: str = "ð <b>ØªÙ Ø´Ø±Ø§Ø¡ Ø´Ø±ÙØ­Ø© eSIM Ø¨ÙØ¬Ø§Ø­!</b>",
) -> None:
    currency = get_currency()
    country_name = html.escape(str(esim.get("country_name", "")))
    plan_name = html.escape(str(esim.get("plan_name", "")))
    lpa_code = str(esim.get("lpa_code", "")).strip()
    notes = str(esim.get("notes", "")).strip()
    paid_price = esim.get("paid_price") or esim.get("sold_price") or esim.get("plan_price") or 0.0
    new_balance = esim.get("new_balance")

    caption_lines = [
        header_title,
        "ââââââââââââââââââââ",
        f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {country_name}",
        f"ð¦ <b>Ø§ÙØ¨Ø§ÙØ©:</b> {plan_name}",
        f"ðµ <b>Ø§ÙØ³Ø¹Ø±:</b> {paid_price:.2f} {html.escape(currency)}",
    ]
    if new_balance is not None:
        caption_lines.append(f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙÙØªØ¨ÙÙ:</b> {float(new_balance):.2f} {html.escape(currency)}")
    if lpa_code:
        caption_lines.extend([
            "",
            "ð <b>ÙÙØ¯ Ø§ÙØªÙØ¹ÙÙ Ø§ÙÙØ¨Ø§Ø´Ø± (LPA):</b>",
            f"<code>{html.escape(lpa_code)}</code>",
        ])
    if notes:
        caption_lines.extend([
            "",
            f"ð <b>ÙÙØ§Ø­Ø¸Ø§Øª Ø¥Ø¶Ø§ÙÙØ©:</b>\n{html.escape(notes)}",
        ])
    caption_lines.extend([
        "",
        "ð² <b>Ø·Ø±ÙÙØ© Ø§ÙØªÙØ¹ÙÙ:</b>",
        "1ï¸â£ Ø§ÙØªØ­ Ø§ÙØ¥Ø¹Ø¯Ø§Ø¯Ø§Øª ÙÙ ÙØ§ØªÙÙ Ø«Ù (Ø¨ÙØ§ÙØ§Øª Ø§ÙÙØ§ØªÙ / Ø§ÙØ®ÙÙÙØ©).",
        "2ï¸â£ Ø§Ø®ØªØ± <b>Ø¥Ø¶Ø§ÙØ© Ø´Ø±ÙØ­Ø© Ø¥ÙÙØªØ±ÙÙÙØ© (Add eSIM)</b>.",
        "3ï¸â£ Ø§ÙØ³Ø­ ÙÙØ¯ Ø§ÙÙ QR Ø§ÙÙØ±ÙÙ Ø£Ù Ø§ÙØ³Ø® ÙÙØ¯ Ø§ÙÙ LPA Ø£Ø¹ÙØ§Ù ÙØ£Ø¯Ø®ÙÙ ÙØ¯ÙÙØ§Ù.",
    ])
    caption = "\n".join(caption_lines)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")]])

    # Send QR photo (either stored QR image or dynamically generated from LPA)
    sent_photo = False
    if esim.get("item_type") == "qr":
        qr_file_id = esim.get("qr_file_id", "")
        qr_local_path = esim.get("qr_local_path", "")
        if qr_file_id:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=qr_file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
                sent_photo = True
            except TelegramError:
                sent_photo = False
        if not sent_photo and qr_local_path and os.path.exists(qr_local_path):
            with open(qr_local_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(f, filename="esim_qr.png"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
                sent_photo = True

    if not sent_photo and lpa_code:
        qr_bio = generate_qr_bytes(lpa_code)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(qr_bio, filename=f"esim_{esim['id']}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        sent_photo = True

    if not sent_photo:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def broadcast_new_inventory(
    context: ContextTypes.DEFAULT_TYPE,
    plan_id: int,
    added_count: int,
) -> Tuple[int, int]:
    """Broadcasts a public Arabic notification to all non-banned users when new eSIMs are added."""
    plan = db.get_plan(plan_id)
    if not plan:
        return 0, 0
    currency = get_currency()
    users = db.get_all_active_users()

    desc_line = f"ð <b>Ø§ÙØªÙØ§ØµÙÙ:</b> {html.escape(plan['description'])}\n" if plan.get("description") else ""
    text = (
        "ð <b>Ø¥Ø´Ø¹Ø§Ø± ØªÙÙØ± ÙØ®Ø²ÙÙ Ø¬Ø¯ÙØ¯ ÙÙ Ø´Ø±Ø§Ø¦Ø­ eSIM!</b>\n"
        "ââââââââââââââââââââ\n"
        f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(plan['country_name'])}\n"
        f"ð¦ <b>Ø§ÙØ¨Ø§ÙØ©:</b> {html.escape(plan['name'])}\n"
        f"{desc_line}"
        f"ðµ <b>Ø§ÙØ³Ø¹Ø±:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
        f"â <b>Ø§ÙÙÙÙØ© Ø§ÙÙØ¶Ø§ÙØ© Ø§ÙØ¢Ù:</b> {added_count} Ø´Ø±ÙØ­Ø©\n"
        f"â <b>Ø¥Ø¬ÙØ§ÙÙ Ø§ÙÙØªØ§Ø­ Ø­Ø§ÙÙØ§Ù:</b> {plan['available_count']} Ø´Ø±ÙØ­Ø©\n\n"
        "ð <b>Ø§Ø¶ØºØ· Ø¹ÙÙ Ø§ÙØ²Ø± Ø¨Ø§ÙØ£Ø³ÙÙ ÙØ´Ø±Ø§Ø¡ Ø§ÙØ¨Ø§ÙØ© ÙÙØ±Ø§Ù:</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ð Ø´Ø±Ø§Ø¡ ÙØ°Ù Ø§ÙØ¨Ø§ÙØ© Ø§ÙØ¢Ù", callback_data=f"u_plan:{plan_id}")],
        [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
    ])

    sent = 0
    failed = 0
    for u in users:
        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            sent += 1
            await asyncio.sleep(0.04)
        except (Forbidden, BadRequest, TelegramError):
            failed += 1
    return sent, failed


# ==============================================================================
# Main Menus (User & Admin)
# ==============================================================================
def build_home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("ð Ø´Ø±Ø§Ø¡ Ø´Ø±ÙØ­Ø© eSIM", callback_data="u_countries")],
        [
            InlineKeyboardButton("ð³ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯", callback_data="u_topup"),
            InlineKeyboardButton("ð° Ø±ØµÙØ¯Ù ÙØ­Ø³Ø§Ø¨Ù", callback_data="u_account"),
        ],
        [
            InlineKeyboardButton("ð¦ ÙØ´ØªØ±ÙØ§ØªÙ Ø§ÙØ³Ø§Ø¨ÙØ©", callback_data="u_purchases"),
            InlineKeyboardButton("ð Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ", callback_data="u_support"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("ð  ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ (Ø§ÙÙØ§ÙÙ)", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    clear_state(context)
    user = update.effective_user
    if not user:
        return
    u_row = db.upsert_user(user.id, user.username, user.full_name)
    currency = get_currency()

    text = (
        f"ð <b>Ø£ÙÙØ§Ù Ø¨Ù ÙØ§ {html.escape(user.first_name or 'Ø¹Ø²ÙØ²Ù Ø§ÙØ¹ÙÙÙ')} ÙÙ ÙØªØ¬Ø± Ø´Ø±Ø§Ø¦Ø­ eSIM Ø§ÙØ±ÙÙÙØ©!</b>\n"
        "ââââââââââââââââââââ\n"
        "ð ÙÙØ¯Ù ÙÙ Ø´Ø±Ø§Ø¦Ø­ Ø§ØªØµØ§Ù Ø¥ÙÙØªØ±ÙÙÙØ© (eSIM) ÙÙØ±ÙØ© ÙØ¬ÙÙØ¹ Ø¯ÙÙ Ø§ÙØ¹Ø§ÙÙ Ø¨Ø£Ø³Ø¹Ø§Ø± ØªÙØ§ÙØ³ÙØ© ÙØ§Ø³ØªÙØ§Ù ÙÙØ±Ù ÙÙÙØ¯ Ø§ÙÙ QR.\n\n"
        f"ð <b>ÙØ¹Ø±Ù Ø­Ø³Ø§Ø¨Ù:</b> <code>{user.id}</code>\n"
        f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙØ­Ø§ÙÙ:</b> <code>{float(u_row['balance']):.2f}</code> {html.escape(currency)}\n\n"
        "ð <b>Ø§Ø®ØªØ± ÙÙ Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØªØ§ÙÙØ© ÙØ§ ÙÙØ§Ø³Ø¨Ù:</b>"
    )
    await safe_edit_or_send(update, text, build_home_keyboard(user.id))


async def show_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("â ÙØ°Ù Ø§ÙÙÙØ­Ø© ÙØ®ØµØµØ© ÙÙØ§ÙÙ Ø§ÙØ¨ÙØª ÙÙØ·.", show_alert=True)
        return
    clear_state(context)
    stats = db.get_stats()
    currency = get_currency()

    text = (
        "ð  <b>ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙÙØ§ÙÙ (Admin Panel)</b>\n"
        "ââââââââââââââââââââ\n"
        f"ð¥ <b>Ø¥Ø¬ÙØ§ÙÙ Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ:</b> {stats['total_users']} (Ø§ÙÙØ­Ø¸ÙØ±ÙÙ: {stats['banned_users']})\n"
        f"ð <b>Ø¹Ø¯Ø¯ Ø§ÙØ¯ÙÙ Ø§ÙÙØ¶Ø§ÙØ©:</b> {stats['total_countries']}\n"
        f"ð¦ <b>Ø¹Ø¯Ø¯ Ø§ÙØ¨Ø§ÙØ§Øª:</b> {stats['total_plans']}\n"
        f"ð² <b>Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØªØ§Ø­Ø© Ø¨Ø§ÙÙØ®Ø²ÙÙ:</b> {stats['available_esims']}\n"
        f"â <b>Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØ¨Ø§Ø¹Ø©:</b> {stats['sold_esims']}\n"
        f"ðµ <b>Ø¥Ø¬ÙØ§ÙÙ Ø§ÙÙØ¨ÙØ¹Ø§Øª:</b> {stats['total_revenue']:.2f} {html.escape(currency)}\n"
        f"â³ <b>Ø·ÙØ¨Ø§Øª Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø§ÙÙØ¹ÙÙØ©:</b> {stats['pending_topups']}\n\n"
        "ð <b>Ø§Ø®ØªØ± Ø§ÙÙØ³Ù Ø§ÙØ°Ù ØªØ±ÙØ¯ Ø¥Ø¯Ø§Ø±ØªÙ:</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ð Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¯ÙÙ", callback_data="adm_countries"),
            InlineKeyboardButton("ð¦ Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¨Ø§ÙØ§Øª", callback_data="adm_plans_SelectCountry"),
        ],
        [
            InlineKeyboardButton("ð² Ø¥Ø¯Ø§Ø±Ø© ÙØ®Ø²ÙÙ Ø§ÙØ´Ø±Ø§Ø¦Ø­ (eSIMs)", callback_data="adm_inv_SelectCountry"),
        ],
        [
            InlineKeyboardButton(
                f"ð³ Ø·ÙØ¨Ø§Øª Ø§ÙØ´Ø­Ù ÙØ§ÙØ£Ø±ØµØ¯Ø© ({stats['pending_topups']})",
                callback_data="adm_topups_menu",
            ),
        ],
        [
            InlineKeyboardButton("ð¥ Ø¥Ø¯Ø§Ø±Ø© Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ ÙØ§ÙØ­Ø¸Ø±", callback_data="adm_users_menu"),
            InlineKeyboardButton("ð¢ Ø±Ø³Ø§ÙØ© Ø¬ÙØ§Ø¹ÙØ© ÙÙÙÙ", callback_data="adm_broadcast_start"),
        ],
        [
            InlineKeyboardButton("âï¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§ÙØ¯ÙØ¹ ÙØ§ÙØ¯Ø¹Ù", callback_data="adm_settings"),
        ],
        [InlineKeyboardButton("ð  Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¬ÙØ© Ø§ÙÙØ³ØªØ®Ø¯Ù", callback_data="home")],
    ])
    await safe_edit_or_send(update, text, kb)


# ==============================================================================
# Command Handlers
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_home(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("â Ø¹Ø°Ø±Ø§ÙØ ÙØ°Ø§ Ø§ÙØ£ÙØ± ÙØ®ØµØµ ÙÙØ§ÙÙ Ø§ÙØ¨ÙØª ÙÙØ·.")
        return
    await show_admin_home(update, context)


# ==============================================================================
# Callback Query Router
# ==============================================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    q = update.callback_query
    if not q or not q.data:
        return
    data = q.data
    user = update.effective_user
    currency = get_currency()

    # Ensure user exists in DB
    db.upsert_user(user.id, user.username, user.full_name)

    # ---------------- HOME ----------------
    if data == "home":
        await q.answer()
        await show_home(update, context)
        return

    # ---------------- USER: ACCOUNT & SUPPORT ----------------
    if data == "u_account":
        await q.answer()
        u_row = db.get_user(user.id)
        purchases = db.get_user_purchases(user.id)
        text = (
            "ð° <b>ØªÙØ§ØµÙÙ Ø­Ø³Ø§Ø¨Ù ÙØ±ØµÙØ¯Ù</b>\n"
            "ââââââââââââââââââââ\n"
            f"ð¤ <b>Ø§ÙØ§Ø³Ù:</b> {html.escape(user.full_name or '')}\n"
            f"ð <b>Ø§ÙÙØ¹Ø±Ù (ID):</b> <code>{user.id}</code>\n"
            f"ðµ <b>Ø§ÙØ±ØµÙØ¯ Ø§ÙÙØªØ§Ø­:</b> <code>{float(u_row['balance']):.2f}</code> {html.escape(currency)}\n"
            f"ð¦ <b>Ø¥Ø¬ÙØ§ÙÙ Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØ´ØªØ±Ø§Ø©:</b> {len(purchases)} Ø´Ø±ÙØ­Ø©\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("ð³ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø§ÙØ¢Ù", callback_data="u_topup")],
            [InlineKeyboardButton("ð¦ Ø¹Ø±Ø¶ ÙØ´ØªØ±ÙØ§ØªÙ", callback_data="u_purchases")],
            [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data == "u_support":
        await q.answer()
        support_user = db.get_setting("support_username", "Support").lstrip("@")
        text = (
            "ð <b>ÙØ±ÙØ² Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ ÙØ®Ø¯ÙØ© Ø§ÙØ¹ÙÙØ§Ø¡</b>\n"
            "ââââââââââââââââââââ\n"
            "Ø¥Ø°Ø§ ÙØ§Ø¬ÙØªÙ Ø£Ù ÙØ´ÙÙØ© ÙÙ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø£Ù ØªÙØ¹ÙÙ Ø´Ø±ÙØ­Ø© Ø§ÙÙ eSIM Ø§ÙØ®Ø§ØµØ© Ø¨ÙØ ÙØ³Ø¹Ø¯ÙØ§ ØªÙØ§ØµÙÙ ÙØ¹ÙØ§ ÙØ¨Ø§Ø´Ø±Ø©:\n\n"
            f"ð¨âð» <b>Ø­Ø³Ø§Ø¨ Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ:</b> @{html.escape(support_user)}\n"
            f"ð <b>ÙØ¹Ø±Ù Ø­Ø³Ø§Ø¨Ù ÙØªØ²ÙÙØ¯ Ø§ÙØ¯Ø¹Ù Ø¨Ù:</b> <code>{user.id}</code>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("ð¬ ÙØ±Ø§Ø³ÙØ© Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ ÙØ¨Ø§Ø´Ø±Ø©", url=f"https://t.me/{support_user}")],
            [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ---------------- USER: BUY eSIM FLOW ----------------
    if data == "u_countries":
        await q.answer()
        countries = db.get_countries()
        if not countries:
            text = (
                "ð <b>ÙØ§Ø¦ÙØ© Ø§ÙØ¯ÙÙ Ø§ÙÙØªØ§Ø­Ø©</b>\n"
                "ââââââââââââââââââââ\n"
                "â ï¸ ÙØ§ ØªÙØ¬Ø¯ Ø¯ÙÙ ÙØ¶Ø§ÙØ© Ø­Ø§ÙÙØ§Ù ÙÙ Ø§ÙÙØªØ¬Ø±. ÙØ±Ø¬Ù Ø§ÙÙØ­Ø§ÙÙØ© ÙØ§Ø­ÙØ§Ù."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")]])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for c in countries:
            stock = int(c["available_stock"] or 0)
            badge = f"â ({stock})" if stock > 0 else "â ÙÙØ¯"
            rows.append([
                InlineKeyboardButton(
                    f"{c['name']} â {badge}",
                    callback_data=f"u_country:{c['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")])
        text = (
            "ð <b>Ø§Ø®ØªØ± Ø§ÙØ¯ÙÙØ© Ø§ÙØªÙ ØªØ±ØºØ¨ Ø¨Ø´Ø±Ø§Ø¡ Ø´Ø±ÙØ­Ø© eSIM ÙÙØ§:</b>\n"
            "ââââââââââââââââââââ\n"
            "ÙØ¸ÙØ± Ø¨Ø¬Ø§ÙØ¨ ÙÙ Ø¯ÙÙØ© Ø¹Ø¯Ø¯ Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØªÙÙØ±Ø© ÙÙØ§Ø³ØªÙØ§Ù Ø§ÙÙÙØ±Ù:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_country:"):
        await q.answer()
        country_id = int(data.split(":")[1])
        country = db.get_country(country_id)
        if not country:
            await show_home(update, context)
            return
        plans = db.get_plans_by_country(country_id)
        if not plans:
            text = (
                f"ð <b>Ø§ÙØ¯ÙÙØ©: {html.escape(country['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                "â ï¸ ÙØ§ ØªÙØ¬Ø¯ Ø¨Ø§ÙØ§Øª ÙØ¶Ø§ÙØ© ÙÙØ°Ù Ø§ÙØ¯ÙÙØ© Ø­Ø§ÙÙØ§Ù."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ¯ÙÙ", callback_data="u_countries")],
                [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for p in plans:
            avail = int(p["available_count"] or 0)
            status_str = f"ÙØªØ§Ø­ ({avail})" if avail > 0 else "ÙÙØ¯Øª Ø§ÙÙÙÙØ©"
            rows.append([
                InlineKeyboardButton(
                    f"ð¦ {p['name']} | {float(p['price']):.2f} {currency} ({status_str})",
                    callback_data=f"u_plan:{p['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ¯ÙÙ", callback_data="u_countries")])
        rows.append([InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")])
        text = (
            f"ð <b>Ø¨Ø§ÙØ§Øª Ø´Ø±Ø§Ø¦Ø­ eSIM ÙØ¯ÙÙØ©: {html.escape(country['name'])}</b>\n"
            "ââââââââââââââââââââ\n"
            "ð Ø§Ø®ØªØ± Ø§ÙØ¨Ø§ÙØ© Ø§ÙÙÙØ§Ø³Ø¨Ø© ÙÙ ÙØ¹Ø±Ø¶ Ø§ÙØªÙØ§ØµÙÙ ÙØ§ÙØ´Ø±Ø§Ø¡ Ø§ÙÙÙØ±Ù:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_plan:"):
        await q.answer()
        plan_id = int(data.split(":")[1])
        plan = db.get_plan(plan_id)
        if not plan:
            await show_home(update, context)
            return
        u_row = db.get_user(user.id)
        avail = int(plan["available_count"] or 0)
        balance = float(u_row["balance"]) if u_row else 0.0
        price = float(plan["price"])

        desc_block = f"ð <b>ØªÙØ§ØµÙÙ Ø§ÙØ¨Ø§ÙØ©:</b>\n{html.escape(plan['description'])}\n\n" if plan.get("description") else ""
        stock_line = f"â ÙØªÙÙØ± ÙÙØ§Ø³ØªÙØ§Ù Ø§ÙÙÙØ±Ù ({avail} Ø´Ø±ÙØ­Ø©)" if avail > 0 else "â ÙÙØ¯Øª Ø§ÙÙÙÙØ© Ø­Ø§ÙÙØ§Ù"

        text = (
            f"ð¦ <b>ØªÙØ§ØµÙÙ Ø§ÙØ¨Ø§ÙØ© Ø§ÙÙØ®ØªØ§Ø±Ø©</b>\n"
            "ââââââââââââââââââââ\n"
            f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(plan['country_name'])}\n"
            f"ð· <b>Ø§Ø³Ù Ø§ÙØ¨Ø§ÙØ©:</b> {html.escape(plan['name'])}\n"
            f"ðµ <b>Ø³Ø¹Ø± Ø§ÙØ¨Ø§ÙØ©:</b> <code>{price:.2f}</code> {html.escape(currency)}\n"
            f"ð <b>Ø­Ø§ÙØ© Ø§ÙÙØ®Ø²ÙÙ:</b> {stock_line}\n\n"
            f"{desc_block}"
            f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙØ­Ø§ÙÙ:</b> <code>{balance:.2f}</code> {html.escape(currency)}"
        )
        rows = []
        if avail > 0:
            if balance >= price:
                rows.append([
                    InlineKeyboardButton(
                        f"â ØªØ£ÙÙØ¯ Ø§ÙØ´Ø±Ø§Ø¡ ({price:.2f} {currency})",
                        callback_data=f"u_buy_confirm:{plan_id}",
                    )
                ])
            else:
                diff = round(price - balance, 2)
                text += f"\n\nâ ï¸ <b>Ø±ØµÙØ¯Ù ØºÙØ± ÙØ§ÙÙ ÙØ¥ØªÙØ§Ù Ø§ÙØ´Ø±Ø§Ø¡. ØªØ­ØªØ§Ø¬ Ø¥ÙÙ Ø´Ø­Ù <code>{diff:.2f}</code> {html.escape(currency)} Ø¥Ø¶Ø§ÙÙØ©.</b>"
                rows.append([InlineKeyboardButton("ð³ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø§ÙØ¢Ù", callback_data="u_topup")])
        rows.append([InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¨Ø§ÙØ§Øª Ø§ÙØ¯ÙÙØ©", callback_data=f"u_country:{plan['country_id']}")])
        rows.append([InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")])
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_buy_confirm:"):
        plan_id = int(data.split(":")[1])
        ok, code, result = db.purchase_esim_atomic(user.id, plan_id)
        if not ok:
            if code == "insufficient_balance":
                await q.answer("â ï¸ Ø±ØµÙØ¯Ù ØºÙØ± ÙØ§ÙÙ ÙØ¥ØªÙØ§Ù Ø¹ÙÙÙØ© Ø§ÙØ´Ø±Ø§Ø¡!", show_alert=True)
            elif code == "out_of_stock":
                await q.answer("â ï¸ Ø¹Ø°Ø±Ø§ÙØ ÙÙØ¯Øª ÙÙÙØ© ÙØ°Ù Ø§ÙØ¨Ø§ÙØ© ÙÙØªÙ!", show_alert=True)
            else:
                await q.answer("â ï¸ ØªØ¹Ø°Ø± Ø¥ØªÙØ§Ù Ø§ÙØ´Ø±Ø§Ø¡Ø Ø­Ø§ÙÙ ÙØ±Ø© Ø£Ø®Ø±Ù.", show_alert=True)
            return

        await q.answer("ð ØªÙØª Ø¹ÙÙÙØ© Ø§ÙØ´Ø±Ø§Ø¡ Ø¨ÙØ¬Ø§Ø­!")
        # Deliver QR & LPA to buyer
        await deliver_esim_to_chat(
            context=context,
            chat_id=user.id,
            esim=result,
            header_title="ð <b>ØªÙ Ø´Ø±Ø§Ø¡ Ø´Ø±ÙØ­Ø© eSIM Ø¨ÙØ¬Ø§Ø­!</b>",
        )
        # Notify Admin of sale
        if ADMIN_ID:
            try:
                user_tag = f"@{user.username}" if user.username else user.full_name
                admin_msg = (
                    "ð <b>Ø¹ÙÙÙØ© Ø¨ÙØ¹ Ø´Ø±ÙØ­Ø© eSIM Ø¬Ø¯ÙØ¯Ø©!</b>\n"
                    "ââââââââââââââââââââ\n"
                    f"ð¤ <b>Ø§ÙØ¹ÙÙÙ:</b> {html.escape(user_tag or '')} (<code>{user.id}</code>)\n"
                    f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(result['country_name'])}\n"
                    f"ð¦ <b>Ø§ÙØ¨Ø§ÙØ©:</b> {html.escape(result['plan_name'])}\n"
                    f"ðµ <b>Ø§ÙØ³Ø¹Ø± Ø§ÙÙØ®ØµÙÙ:</b> {float(result['paid_price']):.2f} {html.escape(currency)}\n"
                    f"ð <b>Ø±ÙÙ Ø§ÙØ´Ø±ÙØ­Ø© Ø¨Ø§ÙÙØ®Ø²ÙÙ:</b> #{result['id']}"
                )
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
        return

    # ---------------- USER: MY PURCHASES ----------------
    if data == "u_purchases":
        await q.answer()
        purchases = db.get_user_purchases(user.id)
        if not purchases:
            text = (
                "ð¦ <b>ÙØ´ØªØ±ÙØ§ØªÙ Ø§ÙØ³Ø§Ø¨ÙØ©</b>\n"
                "ââââââââââââââââââââ\n"
                "ÙÙ ØªÙÙ Ø¨Ø´Ø±Ø§Ø¡ Ø£Ù Ø´Ø±ÙØ­Ø© eSIM Ø­ØªÙ Ø§ÙØ¢Ù."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ð ØªØµÙØ­ Ø§ÙØ¯ÙÙ ÙØ§ÙØ¨Ø§ÙØ§Øª", callback_data="u_countries")],
                [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for item in purchases[:20]:
            rows.append([
                InlineKeyboardButton(
                    f"ð² #{item['id']} | {item['country_name']} - {item['plan_name']} ({float(item['sold_price'] or 0):.2f} {currency})",
                    callback_data=f"u_view_esim:{item['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")])
        text = (
            "ð¦ <b>Ø³Ø¬Ù ÙØ´ØªØ±ÙØ§ØªÙ ÙÙ Ø´Ø±Ø§Ø¦Ø­ eSIM</b>\n"
            "ââââââââââââââââââââ\n"
            "ð Ø§Ø¶ØºØ· Ø¹ÙÙ Ø£Ù Ø´Ø±ÙØ­Ø© ÙØ¥Ø¹Ø§Ø¯Ø© Ø¹Ø±Ø¶ ÙÙØ¯ Ø§ÙÙ QR ÙÙÙØ¯ Ø§ÙØªÙØ¹ÙÙ Ø§ÙØ®Ø§Øµ Ø¨ÙØ§:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_view_esim:"):
        esim_id = int(data.split(":")[1])
        esim = db.get_esim(esim_id)
        if not esim or esim.get("sold_to") != user.id:
            await q.answer("â ï¸ ØªØ¹Ø°Ø± Ø§ÙØ¹Ø«ÙØ± Ø¹ÙÙ ÙØ°Ù Ø§ÙØ´Ø±ÙØ­Ø©.", show_alert=True)
            return
        await q.answer("ð² Ø¬Ø§Ø±Ù Ø¥Ø±Ø³Ø§Ù Ø¨ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ­Ø©...")
        await deliver_esim_to_chat(
            context=context,
            chat_id=user.id,
            esim=esim,
            header_title=f"ð¦ <b>Ø¨ÙØ§ÙØ§Øª Ø´Ø±ÙØ­ØªÙ Ø§ÙÙØ´ØªØ±Ø§Ø© (#{esim['id']})</b>",
        )
        return

    # ---------------- USER: TOP-UP FLOW ----------------
    if data == "u_topup":
        await q.answer()
        clear_state(context)
        text = (
            "ð³ <b>Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ ÙÙ Ø§ÙØ¨ÙØª</b>\n"
            "ââââââââââââââââââââ\n"
            "Ø§Ø®ØªØ± ÙØ³ÙÙØ© Ø§ÙØ¯ÙØ¹ Ø§ÙÙÙØ§Ø³Ø¨Ø© ÙÙ ÙØ´Ø­Ù Ø±ØµÙØ¯Ù:\n\n"
            "ð¡ <b>Binance Pay (Ø¹ÙÙØ§Øª Ø±ÙÙÙØ© - USDT)</b>\n"
            "ð£ <b>InstaPay EG (Ø¥ÙØ³ØªØ§ Ø¨Ø§Ù ÙØµØ±)</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("ð¡ Ø§ÙØ¯ÙØ¹ Ø¹Ø¨Ø± Binance ID (Ø¹ÙÙØ§Øª Ø±ÙÙÙØ©)", callback_data="u_topup_m:binance")],
            [InlineKeyboardButton("ð£ Ø§ÙØ¯ÙØ¹ Ø¹Ø¨Ø± InstaPay EG (Ø¥ÙØ³ØªØ§ Ø¨Ø§Ù)", callback_data="u_topup_m:instapay")],
            [InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data.startswith("u_topup_m:"):
        await q.answer()
        method = data.split(":")[1]
        context.user_data["state"] = "USER_TOPUP_AMOUNT"
        context.user_data["temp"] = {"method": method}

        if method == "binance":
            pay_target = db.get_setting("binance_id", "ØºÙØ± ÙØ­Ø¯Ø¯")
            method_title = "ð¡ Binance Pay (Ø¹ÙÙØ§Øª Ø±ÙÙÙØ©)"
            target_label = "ÙØ¹Ø±Ù Binance ID ÙÙØªØ­ÙÙÙ"
        else:
            pay_target = db.get_setting("instapay_address", "ØºÙØ± ÙØ­Ø¯Ø¯")
            method_title = "ð£ InstaPay EG (Ø¥ÙØ³ØªØ§ Ø¨Ø§Ù ÙØµØ±)"
            target_label = "Ø¹ÙÙØ§Ù Ø§ÙØ¯ÙØ¹ / Ø±ÙÙ InstaPay EG"

        text = (
            f"ð³ <b>Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø¹Ø¨Ø± {method_title}</b>\n"
            "ââââââââââââââââââââ\n"
            f"ð <b>{target_label}:</b>\n"
            f"<code>{html.escape(pay_target)}</code>\n"
            "<i>(Ø§Ø¶ØºØ· Ø¹ÙÙ Ø§ÙÙÙØ¯ Ø£Ø¹ÙØ§Ù ÙÙØ³Ø®Ù)</i>\n\n"
            "1ï¸â£ ÙÙ Ø¨ØªØ­ÙÙÙ Ø§ÙÙØ¨ÙØº Ø§ÙÙØ·ÙÙØ¨ Ø¥ÙÙ Ø§ÙØ­Ø³Ø§Ø¨ Ø§ÙÙÙØ¶Ø­ Ø£Ø¹ÙØ§Ù.\n"
            f"2ï¸â£ Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙÙÙØ© Ø§ÙÙØ¨ÙØº Ø§ÙÙØ±Ø§Ø¯ Ø´Ø­ÙÙ ({html.escape(currency)})</b> ÙÙ Ø±Ø³Ø§ÙØ© ÙØµÙØ© Ø¨Ø§ÙØ£Ø³ÙÙ (ÙØ«Ø§Ù: <code>10</code> Ø£Ù <code>25.5</code>):"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("ð ØªØºÙÙØ± ÙØ³ÙÙØ© Ø§ÙØ¯ÙØ¹", callback_data="u_topup")],
            [InlineKeyboardButton("ð  Ø¥ÙØºØ§Ø¡ ÙØ§ÙØ¹ÙØ¯Ø© ÙÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ==========================================================================
    # ADMIN PANEL CALLBACKS (OWNER ONLY)
    # ==========================================================================
    if data.startswith("adm_"):
        if not is_admin(user.id):
            await q.answer("â ØºÙØ± ÙØµØ±Ø­ ÙÙ Ø¨Ø§ÙØ¯Ø®ÙÙ!", show_alert=True)
            return

        if data == "adm_home":
            await q.answer()
            await show_admin_home(update, context)
            return

        # ---------------- ADMIN: COUNTRIES ----------------
        if data == "adm_countries":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            rows = [[InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø¬Ø¯ÙØ¯Ø©", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    InlineKeyboardButton(
                        f"ð {c['name']} (Ø§ÙØ¨Ø§ÙØ§Øª: {c['plans_count']} | Ø§ÙÙØ®Ø²ÙÙ: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            text = (
                "ð <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¯ÙÙ</b>\n"
                "ââââââââââââââââââââ\n"
                "ÙÙÙÙÙ Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø¬Ø¯ÙØ¯Ø© Ø£Ù Ø§ÙØ¶ØºØ· Ø¹ÙÙ Ø£Ù Ø¯ÙÙØ© ÙØªØ¹Ø¯ÙÙÙØ§ Ø£Ù Ø­Ø°ÙÙØ§ Ø£Ù Ø¥Ø¯Ø§Ø±Ø© Ø¨Ø§ÙØ§ØªÙØ§:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_country_add":
            await q.answer()
            context.user_data["state"] = "ADM_ADD_COUNTRY"
            text = (
                "â <b>Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø¬Ø¯ÙØ¯Ø©</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù Ø§Ø³Ù Ø§ÙØ¯ÙÙØ© ÙØ¹ Ø§ÙØ¹ÙÙ (ÙØ«Ø§Ù: <code>ð¹ð· ØªØ±ÙÙØ§</code> Ø£Ù <code>ð¸ð¦ Ø§ÙØ³Ø¹ÙØ¯ÙØ©</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_countries")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_country_view:"):
            await q.answer()
            clear_state(context)
            country_id = int(data.split(":")[1])
            country = db.get_country(country_id)
            if not country:
                await show_admin_home(update, context)
                return
            plans = db.get_plans_by_country(country_id)
            text = (
                f"ð <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¯ÙÙØ©: {html.escape(country['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð¦ <b>Ø¹Ø¯Ø¯ Ø§ÙØ¨Ø§ÙØ§Øª Ø§ÙÙØ¶Ø§ÙØ©:</b> {len(plans)} Ø¨Ø§ÙØ©\n\n"
                "Ø§Ø®ØªØ± Ø§ÙØ¥Ø¬Ø±Ø§Ø¡ Ø§ÙÙØ·ÙÙØ¨:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ð¦ Ø¥Ø¯Ø§Ø±Ø© Ø¨Ø§ÙØ§Øª ÙØ°Ù Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plans_country:{country_id}")],
                [InlineKeyboardButton("âï¸ ØªØ¹Ø¯ÙÙ Ø§Ø³Ù Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_country_edit:{country_id}")],
                [InlineKeyboardButton("ðï¸ Ø­Ø°Ù Ø§ÙØ¯ÙÙØ© Ø¨Ø§ÙÙØ§ÙÙ", callback_data=f"adm_country_del:{country_id}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ¯ÙÙ", callback_data="adm_countries")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_country_edit:"):
            await q.answer()
            country_id = int(data.split(":")[1])
            country = db.get_country(country_id)
            if not country:
                return
            context.user_data["state"] = "ADM_EDIT_COUNTRY"
            context.user_data["temp"] = {"country_id": country_id}
            text = (
                f"âï¸ <b>ØªØ¹Ø¯ÙÙ Ø§Ø³Ù Ø§ÙØ¯ÙÙØ©: {html.escape(country['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ§Ø³Ù Ø§ÙØ¬Ø¯ÙØ¯ ÙÙØ¯ÙÙØ© Ø§ÙØ¢Ù:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data=f"adm_country_view:{country_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_country_del:"):
            country_id = int(data.split(":")[1])
            db.delete_country(country_id)
            await q.answer("ðï¸ ØªÙ Ø­Ø°Ù Ø§ÙØ¯ÙÙØ© ÙØ¨Ø§ÙØ§ØªÙØ§ Ø¨ÙØ¬Ø§Ø­!", show_alert=True)
            # Return to countries list
            countries = db.get_countries()
            rows = [[InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø¬Ø¯ÙØ¯Ø©", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    InlineKeyboardButton(
                        f"ð {c['name']} (Ø§ÙØ¨Ø§ÙØ§Øª: {c['plans_count']} | Ø§ÙÙØ®Ø²ÙÙ: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            await safe_edit_or_send(update, "â <b>ØªÙ Ø­Ø°Ù Ø§ÙØ¯ÙÙØ© Ø¨ÙØ¬Ø§Ø­.</b>\n\nð <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¯ÙÙ:</b>", InlineKeyboardMarkup(rows))
            return

        # ---------------- ADMIN: PLANS ----------------
        if data == "adm_plans_SelectCountry":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            if not countries:
                await safe_edit_or_send(
                    update,
                    "â ï¸ <b>ÙØ§ ØªÙØ¬Ø¯ Ø¯ÙÙ ÙØ¶Ø§ÙØ© Ø¨Ø¹Ø¯. ÙÙ Ø¨Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø£ÙÙØ§Ù Ø«Ù Ø£Ø¶Ù Ø¨Ø§ÙØ§ØªÙØ§.</b>",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© Ø¬Ø¯ÙØ¯Ø©", callback_data="adm_country_add")],
                        [InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")],
                    ]),
                )
                return
            rows = [
                [InlineKeyboardButton(f"ð {c['name']} ({c['plans_count']} Ø¨Ø§ÙØ©)", callback_data=f"adm_plans_country:{c['id']}")]
                for c in countries
            ]
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "ð¦ <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¨Ø§ÙØ§Øª</b>\nââââââââââââââââââââ\nØ§Ø®ØªØ± Ø§ÙØ¯ÙÙØ© ÙØ¥Ø¯Ø§Ø±Ø© Ø¨Ø§ÙØ§ØªÙØ§:",
                InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("adm_plans_country:"):
            await q.answer()
            clear_state(context)
            country_id = int(data.split(":")[1])
            country = db.get_country(country_id)
            if not country:
                return
            plans = db.get_plans_by_country(country_id)
            rows = [[InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© Ø¬Ø¯ÙØ¯Ø© ÙÙØ°Ù Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plan_add:{country_id}")]]
            for p in plans:
                rows.append([
                    InlineKeyboardButton(
                        f"ð¦ {p['name']} â {float(p['price']):.2f} {currency} (Ø§ÙÙØªØ§Ø­: {int(p['available_count'] or 0)})",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð Ø§Ø®ØªÙØ§Ø± Ø¯ÙÙØ© Ø£Ø®Ø±Ù", callback_data="adm_plans_SelectCountry")])
            rows.append([InlineKeyboardButton("ð  ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            text = (
                f"ð¦ <b>Ø¨Ø§ÙØ§Øª Ø¯ÙÙØ©: {html.escape(country['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø§Ø¶ØºØ· Ø¹ÙÙ Ø£Ù Ø¨Ø§ÙØ© ÙØªØ¹Ø¯ÙÙÙØ§ Ø£Ù Ø­Ø°ÙÙØ§ Ø£Ù Ø¥Ø¯Ø§Ø±Ø© Ø´Ø±Ø§Ø¦Ø­ Ø§ÙÙ eSIM Ø§ÙØ®Ø§ØµØ© Ø¨ÙØ§:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_plan_add:"):
            await q.answer()
            country_id = int(data.split(":")[1])
            context.user_data["state"] = "ADM_ADD_PLAN_NAME"
            context.user_data["temp"] = {"country_id": country_id}
            text = (
                "â <b>Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© Ø¬Ø¯ÙØ¯Ø© (Ø§ÙØ®Ø·ÙØ© 1 ÙÙ 3)</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø§Ø³Ù Ø§ÙØ¨Ø§ÙØ©</b> (ÙØ«Ø§Ù: <code>Ø¨Ø§ÙØ© 10 Ø¬ÙØ¬Ø§Ø¨Ø§ÙØª - 30 ÙÙÙ</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data=f"adm_plans_country:{country_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_view:"):
            await q.answer()
            clear_state(context)
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            if not plan:
                return
            text = (
                f"ð¦ <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ¨Ø§ÙØ©: {html.escape(plan['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(plan['country_name'])}\n"
                f"ðµ <b>Ø§ÙØ³Ø¹Ø±:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
                f"ð <b>Ø§ÙÙØµÙ:</b> {html.escape(plan['description'] or 'Ø¨Ø¯ÙÙ ÙØµÙ')}\n"
                f"ð² <b>Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØªØ§Ø­Ø© Ø¨Ø§ÙÙØ®Ø²ÙÙ:</b> {int(plan['available_count'] or 0)} Ø´Ø±ÙØ­Ø©"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø´Ø±Ø§Ø¦Ø­ eSIM (LPA Ø£Ù QR)", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("ð Ø¹Ø±Ø¶ ÙØªØ¹Ø¯ÙÙ/Ø­Ø°Ù Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø¨Ø§ÙÙØ®Ø²ÙÙ", callback_data=f"adm_esim_list:{plan_id}")],
                [InlineKeyboardButton("âï¸ ØªØ¹Ø¯ÙÙ Ø¨ÙØ§ÙØ§Øª ÙØ³Ø¹Ø± Ø§ÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_edit:{plan_id}")],
                [InlineKeyboardButton("ðï¸ Ø­Ø°Ù Ø§ÙØ¨Ø§ÙØ© ÙÙØ§Ø¦ÙØ§Ù", callback_data=f"adm_plan_del:{plan_id}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¨Ø§ÙØ§Øª Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plans_country:{plan['country_id']}")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_edit:"):
            await q.answer()
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            if not plan:
                return
            context.user_data["state"] = "ADM_EDIT_PLAN_NAME"
            context.user_data["temp"] = {"plan_id": plan_id}
            text = (
                f"âï¸ <b>ØªØ¹Ø¯ÙÙ Ø§ÙØ¨Ø§ÙØ©: {html.escape(plan['name'])}</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù <b>Ø§ÙØ§Ø³Ù Ø§ÙØ¬Ø¯ÙØ¯ ÙÙØ¨Ø§ÙØ©</b> (Ø£Ù Ø£Ø±Ø³Ù ÙÙØ·Ø© <code>.</code> ÙÙØ¥Ø¨ÙØ§Ø¡ Ø¹ÙÙ Ø§ÙØ§Ø³Ù Ø§ÙØ­Ø§ÙÙ):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data=f"adm_plan_view:{plan_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_del:"):
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            country_id = plan["country_id"] if plan else 0
            db.delete_plan(plan_id)
            await q.answer("ðï¸ ØªÙ Ø­Ø°Ù Ø§ÙØ¨Ø§ÙØ© Ø¨ÙØ¬Ø§Ø­!", show_alert=True)
            if country_id:
                await safe_edit_or_send(
                    update,
                    "â <b>ØªÙ Ø­Ø°Ù Ø§ÙØ¨Ø§ÙØ© Ø¨ÙØ¬Ø§Ø­.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¨Ø§ÙØ§Øª Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plans_country:{country_id}")]]),
                )
            else:
                await show_admin_home(update, context)
            return

        # ---------------- ADMIN: eSIM INVENTORY (ADD / EDIT / DELETE) ----------------
        if data == "adm_inv_SelectCountry":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            if not countries:
                await safe_edit_or_send(
                    update,
                    "â ï¸ <b>ÙØ§ ØªÙØ¬Ø¯ Ø¯ÙÙ ÙØ¶Ø§ÙØ© Ø¨Ø¹Ø¯. ÙÙ Ø¨Ø¥Ø¶Ø§ÙØ© Ø¯ÙÙØ© ÙØ¨Ø§ÙØ© Ø£ÙÙØ§Ù.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")]]),
                )
                return
            rows = [
                [
                    InlineKeyboardButton(
                        f"ð {c['name']} (Ø§ÙÙØªØ§Ø­ Ø¨Ø§ÙÙØ®Ø²ÙÙ: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_inv_country:{c['id']}",
                    )
                ]
                for c in countries
            ]
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "ð² <b>Ø¥Ø¯Ø§Ø±Ø© ÙØ®Ø²ÙÙ Ø§ÙØ´Ø±Ø§Ø¦Ø­ (eSIMs)</b>\nââââââââââââââââââââ\nØ§Ø®ØªØ± Ø§ÙØ¯ÙÙØ© ÙØ¹Ø±Ø¶ Ø¨Ø§ÙØ§ØªÙØ§ ÙØ¥Ø¯Ø§Ø±Ø© Ø´Ø±Ø§Ø¦Ø­ÙØ§:",
                InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("adm_inv_country:"):
            await q.answer()
            country_id = int(data.split(":")[1])
            country = db.get_country(country_id)
            plans = db.get_plans_by_country(country_id)
            if not plans:
                await safe_edit_or_send(
                    update,
                    f"â ï¸ <b>ÙØ§ ØªÙØ¬Ø¯ Ø¨Ø§ÙØ§Øª ÙÙ Ø¯ÙÙØ© {html.escape(country['name'] if country else '')}. Ø£Ø¶Ù Ø¨Ø§ÙØ© Ø£ÙÙØ§Ù.</b>",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© Ø¬Ø¯ÙØ¯Ø©", callback_data=f"adm_plan_add:{country_id}")],
                        [InlineKeyboardButton("ð Ø±Ø¬ÙØ¹", callback_data="adm_inv_SelectCountry")],
                    ]),
                )
                return
            rows = []
            for p in plans:
                rows.append([
                    InlineKeyboardButton(
                        f"ð¦ {p['name']} (Ø§ÙÙØªÙÙØ±: {int(p['available_count'] or 0)} Ø´Ø±ÙØ­Ø©)",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð Ø§Ø®ØªÙØ§Ø± Ø¯ÙÙØ© Ø£Ø®Ø±Ù", callback_data="adm_inv_SelectCountry")])
            await safe_edit_or_send(
                update,
                f"ð² <b>Ø§Ø®ØªØ± Ø§ÙØ¨Ø§ÙØ© ÙØ¥Ø¯Ø§Ø±Ø© ÙØ®Ø²ÙÙ Ø§ÙØ´Ø±Ø§Ø¦Ø­ ({html.escape(country['name'])})</b>:",
                InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("adm_esim_add:"):
            await q.answer()
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            if not plan:
                return
            context.user_data["state"] = "ADM_ADD_ESIM"
            context.user_data["temp"] = {"plan_id": plan_id}
            text = (
                f"â <b>Ø¥Ø¶Ø§ÙØ© Ø´Ø±Ø§Ø¦Ø­ eSIM Ø¬Ø¯ÙØ¯Ø© Ø¥ÙÙ Ø§ÙÙØ®Ø²ÙÙ</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(plan['country_name'])}\n"
                f"ð¦ <b>Ø§ÙØ¨Ø§ÙØ©:</b> {html.escape(plan['name'])}\n\n"
                "ð¥ <b>ÙÙÙ ÙÙÙÙÙ Ø§ÙØ¥Ø¶Ø§ÙØ© Ø§ÙØ¢ÙØ</b>\n"
                "1ï¸â£ <b>Ø¥Ø±Ø³Ø§Ù ÙÙØ¯ LPA ÙØµÙ:</b> Ø£Ø±Ø³Ù ÙÙØ¯ Ø§ÙÙ LPA ÙØ¨Ø§Ø´Ø±Ø© (ÙØ«Ø§Ù: <code>LPA:1$smdp.io$XXXX</code>). ÙÙØ§ ÙÙÙÙÙ Ø¥Ø±Ø³Ø§Ù Ø¹Ø¯Ø© Ø£ÙÙØ§Ø¯ ÙÙ Ø±Ø³Ø§ÙØ© ÙØ§Ø­Ø¯Ø© (ÙÙ ÙÙØ¯ ÙÙ Ø³Ø·Ø± ÙØ³ØªÙÙ) ÙØ¥Ø¶Ø§ÙØªÙØ§ Ø¯ÙØ¹Ø© ÙØ§Ø­Ø¯Ø©!\n"
                "2ï¸â£ <b>Ø£Ù Ø¥Ø±Ø³Ø§Ù ØµÙØ±Ø© QR Code:</b> Ø£Ø±Ø³Ù ØµÙØ±Ø© ÙÙØ¯ Ø§ÙÙ QR ÙØ¨Ø§Ø´Ø±Ø© (ÙÙÙÙÙÙ ÙØªØ§Ø¨Ø© ÙÙØ¯ Ø§ÙÙ LPA Ø£Ù ÙÙØ§Ø­Ø¸Ø§Øª ÙÙ ÙØµÙ Ø§ÙØµÙØ±Ø© Caption).\n\n"
                "ð <i>ÙÙØ§Ø­Ø¸Ø©: Ø¨ÙØ¬Ø±Ø¯ Ø¥Ø¶Ø§ÙØ© Ø§ÙØ´Ø±ÙØ­Ø©Ø Ø³ÙÙÙÙ Ø§ÙØ¨ÙØª ØªÙÙØ§Ø¦ÙØ§Ù Ø¨Ø¥Ø±Ø³Ø§Ù Ø¥Ø´Ø¹Ø§Ø± Ø¹Ø§Ù ÙØ¬ÙÙØ¹ Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ Ø¨ØªÙÙØ± ÙØ®Ø²ÙÙ Ø¬Ø¯ÙØ¯!</i>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡ ÙØ§ÙØ¹ÙØ¯Ø© ÙÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_view:{plan_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_list:"):
            await q.answer()
            clear_state(context)
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            if not plan:
                return
            esims = db.get_available_esims(plan_id)
            if not esims:
                text = (
                    f"ð <b>ÙØ®Ø²ÙÙ Ø§ÙØ¨Ø§ÙØ©: {html.escape(plan['name'])}</b>\n"
                    "ââââââââââââââââââââ\n"
                    "â ï¸ ÙØ§ ØªÙØ¬Ø¯ Ø´Ø±Ø§Ø¦Ø­ ÙØªØ§Ø­Ø© Ø­Ø§ÙÙØ§Ù ÙÙ ÙØ®Ø²ÙÙ ÙØ°Ù Ø§ÙØ¨Ø§ÙØ©."
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø´Ø±ÙØ­Ø© Ø¬Ø¯ÙØ¯Ø©", callback_data=f"adm_esim_add:{plan_id}")],
                    [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_view:{plan_id}")],
                ])
                await safe_edit_or_send(update, text, kb)
                return

            rows = []
            for e in esims[:30]:
                t_badge = "ð LPA" if e["item_type"] == "lpa" else "ð¼ QR"
                short_code = (e["lpa_code"][:22] + "...") if e["lpa_code"] and len(e["lpa_code"]) > 22 else (e["lpa_code"] or "ØµÙØ±Ø© QR")
                rows.append([
                    InlineKeyboardButton(
                        f"#{e['id']} | {t_badge} | {short_code}",
                        callback_data=f"adm_esim_item:{e['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø´Ø±ÙØ­Ø© Ø¬Ø¯ÙØ¯Ø©", callback_data=f"adm_esim_add:{plan_id}")])
            rows.append([InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_view:{plan_id}")])
            text = (
                f"ð <b>Ø§ÙØ´Ø±Ø§Ø¦Ø­ Ø§ÙÙØªØ§Ø­Ø© ÙÙ ÙØ®Ø²ÙÙ: {html.escape(plan['name'])}</b>\n"
                f"Ø¥Ø¬ÙØ§ÙÙ Ø§ÙÙØªØ§Ø­: <b>{len(esims)}</b> Ø´Ø±ÙØ­Ø©.\n"
                "ââââââââââââââââââââ\n"
                "ð Ø§Ø¶ØºØ· Ø¹ÙÙ Ø£Ù Ø´Ø±ÙØ­Ø© ÙÙØ¹Ø§ÙÙØªÙØ§ Ø£Ù ØªØ¹Ø¯ÙÙÙØ§ Ø£Ù Ø­Ø°ÙÙØ§ ÙÙ Ø§ÙÙØ®Ø²ÙÙ:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_esim_item:"):
            await q.answer()
            clear_state(context)
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("â ï¸ Ø§ÙØ´Ø±ÙØ­Ø© ØºÙØ± ÙÙØ¬ÙØ¯Ø©.", show_alert=True)
                return
            t_label = "ÙÙØ¯ ÙØµÙ (LPA)" if esim["item_type"] == "lpa" else "ØµÙØ±Ø© ÙÙØ¯ (QR Code)"
            text = (
                f"ð <b>ØªÙØ§ØµÙÙ Ø§ÙØ´Ø±ÙØ­Ø© Ø¨Ø§ÙÙØ®Ø²ÙÙ #{esim['id']}</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð <b>Ø§ÙØ¯ÙÙØ©:</b> {html.escape(esim['country_name'])}\n"
                f"ð¦ <b>Ø§ÙØ¨Ø§ÙØ©:</b> {html.escape(esim['plan_name'])}\n"
                f"ð· <b>Ø§ÙÙÙØ¹:</b> {t_label}\n"
                f"ð <b>ÙÙØ¯ LPA:</b> <code>{html.escape(esim['lpa_code'] or 'ÙØ§ ÙÙØ¬Ø¯ (ØµÙØ±Ø© QR)')}</code>\n"
                f"ð <b>ÙÙØ§Ø­Ø¸Ø§Øª:</b> {html.escape(esim['notes'] or 'ÙØ§ ÙÙØ¬Ø¯')}\n"
                f"ð <b>ØªØ§Ø±ÙØ® Ø§ÙØ¥Ø¶Ø§ÙØ©:</b> {esim['created_at']}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ð ÙØ¹Ø§ÙÙØ© ÙÙØ¯ Ø§ÙÙ QR ÙÙØ§ ÙØ¸ÙØ± ÙÙØ¹ÙÙÙ", callback_data=f"adm_esim_preview:{esim['id']}")],
                [InlineKeyboardButton("âï¸ ØªØ¹Ø¯ÙÙ Ø§ÙØ´Ø±ÙØ­Ø© (LPA Ø£Ù QR)", callback_data=f"adm_esim_edit:{esim['id']}")],
                [InlineKeyboardButton("ðï¸ Ø­Ø°Ù Ø§ÙØ´Ø±ÙØ­Ø© ÙÙ Ø§ÙÙØ®Ø²ÙÙ", callback_data=f"adm_esim_del:{esim['id']}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ´Ø±Ø§Ø¦Ø­", callback_data=f"adm_esim_list:{esim['plan_id']}")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_preview:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("â ï¸ ØºÙØ± ÙÙØ¬ÙØ¯Ø©.", show_alert=True)
                return
            await q.answer("ð Ø¬Ø§Ø±Ù Ø¥Ø±Ø³Ø§Ù Ø§ÙÙØ¹Ø§ÙÙØ©...")
            await deliver_esim_to_chat(
                context=context,
                chat_id=user.id,
                esim=esim,
                header_title=f"ð <b>ÙØ¹Ø§ÙÙØ© Ø§ÙØ´Ø±ÙØ­Ø© Ø¨Ø§ÙÙØ®Ø²ÙÙ (#{esim['id']})</b>",
            )
            return

        if data.startswith("adm_esim_edit:"):
            await q.answer()
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                return
            context.user_data["state"] = "ADM_EDIT_ESIM"
            context.user_data["temp"] = {"esim_id": esim_id, "plan_id": esim["plan_id"]}
            text = (
                f"âï¸ <b>ØªØ¹Ø¯ÙÙ Ø§ÙØ´Ø±ÙØ­Ø© Ø±ÙÙ #{esim_id}</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙÙØ¯ Ø§ÙÙ LPA Ø§ÙØ¬Ø¯ÙØ¯</b> ÙÙØµØ Ø£Ù Ø£Ø±Ø³Ù <b>ØµÙØ±Ø© QR Code Ø¬Ø¯ÙØ¯Ø©</b> ÙØªØ­Ø¯ÙØ« ÙØ°Ù Ø§ÙØ´Ø±ÙØ­Ø©:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data=f"adm_esim_item:{esim_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_del:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            plan_id = esim["plan_id"] if esim else 0
            db.delete_esim(esim_id)
            await q.answer("ðï¸ ØªÙ Ø­Ø°Ù Ø§ÙØ´Ø±ÙØ­Ø© ÙÙ Ø§ÙÙØ®Ø²ÙÙ!", show_alert=True)
            if plan_id:
                await safe_edit_or_send(
                    update,
                    "â <b>ØªÙ Ø­Ø°Ù Ø§ÙØ´Ø±ÙØ­Ø© ÙÙ Ø§ÙÙØ®Ø²ÙÙ Ø¨ÙØ¬Ø§Ø­.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ´Ø±Ø§Ø¦Ø­", callback_data=f"adm_esim_list:{plan_id}")]]),
                )
            return

        # ---------------- ADMIN: TOP-UPS & BALANCE ----------------
        if data == "adm_topups_menu":
            await q.answer()
            clear_state(context)
            pending = db.get_pending_topups()
            rows = [
                [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© / Ø®ØµÙ Ø±ØµÙØ¯ ÙÙØ³ØªØ®Ø¯Ù ÙØ¯ÙÙØ§Ù", callback_data="adm_bal_manual")],
            ]
            for req in pending[:20]:
                m_icon = "ð¡ Binance" if req["method"] == "binance" else "ð£ InstaPay"
                rows.append([
                    InlineKeyboardButton(
                        f"â³ Ø·ÙØ¨ #{req['id']} | {m_icon} | {float(req['amount']):.2f} {currency}",
                        callback_data=f"adm_topup_view:{req['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            text = (
                "ð³ <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙØ£Ø±ØµØ¯Ø© ÙØ·ÙØ¨Ø§Øª Ø§ÙØ´Ø­Ù</b>\n"
                "ââââââââââââââââââââ\n"
                f"â³ <b>Ø·ÙØ¨Ø§Øª Ø§ÙØ´Ø­Ù Ø§ÙÙØ¹ÙÙØ© Ø­Ø§ÙÙØ§Ù:</b> {len(pending)} Ø·ÙØ¨\n\n"
                "ÙÙÙÙÙ ØªØ¹Ø¯ÙÙ Ø±ØµÙØ¯ Ø£Ù ÙØ³ØªØ®Ø¯Ù ÙØ¯ÙÙØ§Ù Ø£Ù ÙØ±Ø§Ø¬Ø¹Ø© Ø·ÙØ¨Ø§Øª Ø§ÙØ´Ø­Ù Ø¨Ø§ÙØ£Ø³ÙÙ:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_topup_view:"):
            await q.answer()
            req_id = int(data.split(":")[1])
            req = db.get_topup_request(req_id)
            if not req:
                return
            m_name = "Binance Pay (Ø¹ÙÙØ§Øª Ø±ÙÙÙØ©)" if req["method"] == "binance" else "InstaPay EG (Ø¥ÙØ³ØªØ§ Ø¨Ø§Ù)"
            text = (
                f"ð§¾ <b>ØªÙØ§ØµÙÙ Ø·ÙØ¨ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ #{req['id']}</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð¤ <b>Ø§ÙØ¹ÙÙÙ:</b> {html.escape(req['full_name'] or '')} (@{html.escape(req['username'] or 'Ø¨Ø¯ÙÙ')})\n"
                f"ð <b>ÙØ¹Ø±Ù Ø§ÙØ¹ÙÙÙ:</b> <code>{req['user_id']}</code>\n"
                f"ð³ <b>Ø§ÙÙØ³ÙÙØ©:</b> {m_name}\n"
                f"ðµ <b>Ø§ÙÙØ¨ÙØº Ø§ÙÙØ·ÙÙØ¨:</b> <code>{float(req['amount']):.2f}</code> {html.escape(currency)}\n"
                f"ð <b>Ø¨ÙØ§ÙØ§Øª Ø§ÙØªØ­ÙÙÙ Ø§ÙÙØ±Ø³ÙØ©:</b> {html.escape(req['proof_text'] or 'ÙØ±ÙÙ ØµÙØ±Ø© Ø¥ÙØµØ§Ù')}\n"
                f"ð <b>Ø§ÙØªØ§Ø±ÙØ®:</b> {req['created_at']}"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("â ÙØ¨ÙÙ ÙØ¥Ø¶Ø§ÙØ© Ø§ÙØ±ØµÙØ¯", callback_data=f"adm_topup_ok:{req['id']}"),
                    InlineKeyboardButton("â Ø±ÙØ¶ Ø§ÙØ·ÙØ¨", callback_data=f"adm_topup_no:{req['id']}"),
                ],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ·ÙØ¨Ø§Øª", callback_data="adm_topups_menu")],
            ])
            if req.get("proof_file_id"):
                try:
                    await context.bot.send_photo(
                        chat_id=user.id,
                        photo=req["proof_file_id"],
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                    )
                    return
                except TelegramError:
                    pass
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_topup_ok:") or data.startswith("adm_topup_no:"):
            approve = data.startswith("adm_topup_ok:")
            req_id = int(data.split(":")[1])
            ok, status, res = db.process_topup_request(req_id, approve=approve)
            if not ok:
                await q.answer("â ï¸ ØªÙ ÙØ¹Ø§ÙØ¬Ø© ÙØ°Ø§ Ø§ÙØ·ÙØ¨ ÙØ³Ø¨ÙØ§Ù Ø£Ù Ø£ÙÙ ØºÙØ± ÙÙØ¬ÙØ¯.", show_alert=True)
                return

            if approve:
                await q.answer("â ØªÙ ÙØ¨ÙÙ Ø§ÙØ·ÙØ¨ ÙØ¥Ø¶Ø§ÙØ© Ø§ÙØ±ØµÙØ¯ ÙÙØ¹ÙÙÙ!", show_alert=True)
                user_msg = (
                    "ð <b>ØªÙ ÙØ¨ÙÙ Ø·ÙØ¨ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø§ÙØ®Ø§Øµ Ø¨Ù!</b>\n"
                    "ââââââââââââââââââââ\n"
                    f"â <b>Ø§ÙÙØ¨ÙØº Ø§ÙÙØ¶Ø§Ù:</b> <code>{float(res['amount']):.2f}</code> {html.escape(currency)}\n"
                    f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙØ­Ø§ÙÙ:</b> <code>{float(res['new_balance']):.2f}</code> {html.escape(currency)}\n\n"
                    "Ø´ÙØ±Ø§Ù ÙØ«ÙØªÙ Ø¨ÙØ§! ÙÙÙÙÙ Ø§ÙØ¢Ù Ø´Ø±Ø§Ø¡ Ø´Ø±ÙØ­ØªÙ ÙÙØ±Ø§Ù."
                )
            else:
                await q.answer("â ØªÙ Ø±ÙØ¶ Ø·ÙØ¨ Ø§ÙØ´Ø­Ù.", show_alert=True)
                user_msg = (
                    "â <b>ÙØ¹ØªØ°Ø±Ø ØªÙ Ø±ÙØ¶ Ø·ÙØ¨ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø±ÙÙ #{req_id}.</b>\n"
                    f"Ø§ÙÙØ¨ÙØº: {float(res['amount']):.2f} {html.escape(currency)}\n"
                    "ÙØ±Ø¬Ù Ø§ÙØªØ£ÙØ¯ ÙÙ Ø¥ÙØµØ§Ù Ø§ÙØªØ­ÙÙÙ Ø£Ù Ø§ÙØªÙØ§ØµÙ ÙØ¹ Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ."
                ).replace("{req_id}", str(req_id))

            try:
                await context.bot.send_message(
                    chat_id=res["user_id"],
                    text=user_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")]]),
                )
            except TelegramError:
                pass

            await show_admin_home(update, context)
            return

        if data == "adm_bal_manual":
            await q.answer()
            context.user_data["state"] = "ADM_MANUAL_BAL_USER"
            text = (
                "ð° <b>Ø¥Ø¶Ø§ÙØ© Ø£Ù Ø®ØµÙ Ø±ØµÙØ¯ ÙÙØ³ØªØ®Ø¯Ù</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙØ¹Ø±Ù Ø§ÙÙØ³ØªØ®Ø¯Ù (User ID)</b> Ø§ÙØ°Ù ØªØ±ÙØ¯ ØªØ¹Ø¯ÙÙ Ø±ØµÙØ¯Ù:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_topups_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        # ---------------- ADMIN: USERS & BAN MANAGEMENT ----------------
        if data == "adm_users_menu":
            await q.answer()
            clear_state(context)
            stats = db.get_stats()
            banned_list = db.get_banned_users()
            rows = [
                [InlineKeyboardButton("ð« Ø­Ø¸Ø± ÙØ³ØªØ®Ø¯Ù Ø¨ÙØ§Ø³Ø·Ø© Ø§ÙÙ ID", callback_data="adm_ban_input")],
                [InlineKeyboardButton("â ÙÙ Ø­Ø¸Ø± ÙØ³ØªØ®Ø¯Ù Ø¨ÙØ§Ø³Ø·Ø© Ø§ÙÙ ID", callback_data="adm_unban_input")],
            ]
            for b in banned_list[:15]:
                rows.append([
                    InlineKeyboardButton(
                        f"ð ÙÙ Ø­Ø¸Ø±: {b['full_name'] or b['user_id']} ({b['user_id']})",
                        callback_data=f"adm_unban_direct:{b['user_id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")])
            text = (
                "ð¥ <b>Ø¥Ø¯Ø§Ø±Ø© Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ ÙØ§ÙØ­Ø¸Ø±</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð¤ <b>Ø¥Ø¬ÙØ§ÙÙ Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ:</b> {stats['total_users']}\n"
                f"ð« <b>Ø¹Ø¯Ø¯ Ø§ÙÙØ­Ø¸ÙØ±ÙÙ Ø­Ø§ÙÙØ§Ù:</b> {stats['banned_users']}\n\n"
                "ÙÙÙÙÙ Ø­Ø¸Ø± Ø£Ù ÙÙ Ø­Ø¸Ø± Ø£Ù ÙØ³ØªØ®Ø¯Ù Ø¹Ø¨Ø± Ø§ÙÙ ID Ø£Ù Ø§ÙØ¶ØºØ· Ø¹ÙÙ Ø§Ø³Ù Ø§ÙÙØ­Ø¸ÙØ± Ø¨Ø§ÙØ£Ø³ÙÙ ÙÙÙ Ø­Ø¸Ø±Ù ÙÙØ±Ø§Ù:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_ban_input":
            await q.answer()
            context.user_data["state"] = "ADM_BAN_USER"
            text = (
                "ð« <b>Ø­Ø¸Ø± ÙØ³ØªØ®Ø¯Ù ÙÙ Ø§ÙØ¨ÙØª</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙØ¹Ø±Ù Ø§ÙÙØ³ØªØ®Ø¯Ù (User ID)</b> Ø§ÙØ°Ù ØªØ±ÙØ¯ Ø­Ø¸Ø±Ù ÙÙØ§Ø¦ÙØ§Ù ÙÙ Ø§Ø³ØªØ®Ø¯Ø§Ù Ø§ÙØ¨ÙØª:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data == "adm_unban_input":
            await q.answer()
            context.user_data["state"] = "ADM_UNBAN_USER"
            text = (
                "â <b>ÙÙ Ø­Ø¸Ø± ÙØ³ØªØ®Ø¯Ù</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙØ¹Ø±Ù Ø§ÙÙØ³ØªØ®Ø¯Ù (User ID)</b> ÙØ¥ÙØºØ§Ø¡ Ø­Ø¸Ø±Ù:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_unban_direct:"):
            target_id = int(data.split(":")[1])
            db.set_user_ban(target_id, False)
            await q.answer(f"â ØªÙ ÙÙ Ø§ÙØ­Ø¸Ø± Ø¹Ù Ø§ÙÙØ³ØªØ®Ø¯Ù {target_id}", show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="â <b>ØªÙ Ø±ÙØ¹ Ø§ÙØ­Ø¸Ø± Ø¹Ù Ø­Ø³Ø§Ø¨Ù! ÙÙÙÙÙ Ø§ÙØ¢Ù Ø§Ø³ØªØ®Ø¯Ø§Ù Ø§ÙØ¨ÙØª Ø¨Ø´ÙÙ Ø·Ø¨ÙØ¹Ù.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            await show_admin_home(update, context)
            return

        # ---------------- ADMIN: PUBLIC BROADCAST ----------------
        if data == "adm_broadcast_start":
            await q.answer()
            context.user_data["state"] = "ADM_BROADCAST_MSG"
            text = (
                "ð¢ <b>Ø¥Ø±Ø³Ø§Ù Ø±Ø³Ø§ÙØ© Ø¬ÙØ§Ø¹ÙØ© Ø¹Ø§ÙØ© ÙØ¬ÙÙØ¹ Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø§ÙØ±Ø³Ø§ÙØ© Ø§ÙÙØµÙØ©</b> Ø£Ù <b>Ø§ÙØµÙØ±Ø© ÙØ¹ Ø§ÙÙØµÙ</b> Ø§ÙØªÙ ØªØ±ÙØ¯ Ø¥Ø±Ø³Ø§ÙÙØ§ ÙØ¬ÙÙØ¹ ÙØ³ØªØ®Ø¯ÙÙ Ø§ÙØ¨ÙØª:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_home")]])
            await safe_edit_or_send(update, text, kb)
            return

        # ---------------- ADMIN: SETTINGS (BINANCE / INSTAPAY / SUPPORT) ----------------
        if data == "adm_settings":
            await q.answer()
            clear_state(context)
            binance_id = db.get_setting("binance_id", "ØºÙØ± ÙØ­Ø¯Ø¯")
            instapay = db.get_setting("instapay_address", "ØºÙØ± ÙØ­Ø¯Ø¯")
            support = db.get_setting("support_username", "Support")
            curr = db.get_setting("currency", "USD")

            text = (
                "âï¸ <b>Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§ÙØ¯ÙØ¹ ÙØ§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð¡ <b>ÙØ¹Ø±Ù Binance ID:</b> <code>{html.escape(binance_id)}</code>\n"
                f"ð£ <b>Ø¹ÙÙØ§Ù InstaPay EG:</b> <code>{html.escape(instapay)}</code>\n"
                f"ð <b>ÙÙØ²Ø± Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ:</b> @{html.escape(support.lstrip('@'))}\n"
                f"ðµ <b>Ø¹ÙÙØ© Ø§ÙÙØªØ¬Ø±:</b> <code>{html.escape(curr)}</code>\n\n"
                "ð Ø§Ø¶ØºØ· Ø¹ÙÙ Ø£Ù Ø¥Ø¹Ø¯Ø§Ø¯ ÙØªØ¹Ø¯ÙÙÙ ÙØ¨Ø§Ø´Ø±Ø©:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("ð¡ ØªØ¹Ø¯ÙÙ ÙØ¹Ø±Ù Binance ID", callback_data="adm_set_key:binance_id")],
                [InlineKeyboardButton("ð£ ØªØ¹Ø¯ÙÙ Ø¹ÙÙØ§Ù InstaPay EG", callback_data="adm_set_key:instapay_address")],
                [InlineKeyboardButton("ð ØªØ¹Ø¯ÙÙ ÙÙØ²Ø± Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ", callback_data="adm_set_key:support_username")],
                [InlineKeyboardButton("ðµ ØªØ¹Ø¯ÙÙ Ø±ÙØ² Ø§ÙØ¹ÙÙØ© (USD / EGP)", callback_data="adm_set_key:currency")],
                [InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_set_key:"):
            await q.answer()
            key = data.split(":")[1]
            labels = {
                "binance_id": "ÙØ¹Ø±Ù Binance ID Ø§ÙØ¬Ø¯ÙØ¯",
                "instapay_address": "Ø¹ÙÙØ§Ù Ø£Ù Ø±ÙÙ InstaPay EG Ø§ÙØ¬Ø¯ÙØ¯",
                "support_username": "ÙÙØ²Ø± Ø§ÙØ¯Ø¹Ù Ø§ÙÙÙÙ ÙÙ ØªÙÙÙØ¬Ø±Ø§Ù (Ø¨Ø¯ÙÙ @)",
                "currency": "Ø±ÙØ² Ø§ÙØ¹ÙÙØ© (ÙØ«Ø§Ù: USD Ø£Ù Ø¬.Ù)",
            }
            context.user_data["state"] = "ADM_SET_SETTING"
            context.user_data["temp"] = {"key": key}
            text = (
                f"âï¸ <b>ØªØ­Ø¯ÙØ« Ø§ÙØ¥Ø¹Ø¯Ø§Ø¯: {labels.get(key, key)}</b>\n"
                "ââââââââââââââââââââ\n"
                "Ø£Ø±Ø³Ù Ø§ÙÙÙÙØ© Ø§ÙØ¬Ø¯ÙØ¯Ø© Ø§ÙØ¢Ù:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø¥ÙØºØ§Ø¡", callback_data="adm_settings")]])
            await safe_edit_or_send(update, text, kb)
            return


# ==============================================================================
# Message Handler (Text & Photos for User Top-Ups & Admin Flows)
# ==============================================================================
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    db.upsert_user(user.id, user.username, user.full_name)
    state = context.user_data.get("state")
    temp = context.user_data.get("temp", {})
    currency = get_currency()

    # If no active state, show main menu
    if not state:
        await show_home(update, context)
        return

    # --------------------------------------------------------------------------
    # USER TOP-UP STATES
    # --------------------------------------------------------------------------
    if state == "USER_TOPUP_AMOUNT":
        if not msg.text:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ÙÙÙØ© Ø§ÙÙØ¨ÙØº ÙØ£Ø±ÙØ§Ù ÙÙØ· (ÙØ«Ø§Ù: 15).")
            return
        try:
            amount = round(float(msg.text.strip().replace(",", ".")), 2)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await msg.reply_text("â ï¸ ÙÙÙØ© ØºÙØ± ØµØ­ÙØ­Ø©! ÙØ±Ø¬Ù Ø¥Ø¯Ø®Ø§Ù Ø±ÙÙ Ø£ÙØ¨Ø± ÙÙ ØµÙØ± (ÙØ«Ø§Ù: <code>10</code>).", parse_mode=ParseMode.HTML)
            return

        temp["amount"] = amount
        context.user_data["temp"] = temp
        context.user_data["state"] = "USER_TOPUP_PROOF"

        await msg.reply_text(
            f"â <b>Ø§ÙÙØ¨ÙØº Ø§ÙÙØ­Ø¯Ø¯ ÙÙØ´Ø­Ù:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n\n"
            "ð¸ <b>Ø§ÙØ®Ø·ÙØ© Ø§ÙØ£Ø®ÙØ±Ø©:</b>\n"
            "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ØµÙØ±Ø© Ø¥ÙØµØ§Ù Ø§ÙØªØ­ÙÙÙ (Screenshot)</b> Ø£Ù Ø£Ø±Ø³Ù <b>Ø±ÙÙ Ø§ÙØ¹ÙÙÙØ© Ø§ÙÙØ±Ø¬Ø¹Ù (Transaction ID / Ø§Ø³Ù Ø§ÙÙØ­ÙÙ)</b> ÙÙ Ø±Ø³Ø§ÙØ©:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð  Ø¥ÙØºØ§Ø¡", callback_data="home")]]),
        )
        return

    if state == "USER_TOPUP_PROOF":
        method = temp.get("method", "binance")
        amount = float(temp.get("amount", 0.0))
        proof_text = (msg.caption or msg.text or "").strip()
        proof_file_id = ""
        if msg.photo:
            proof_file_id = msg.photo[-1].file_id
        elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
            proof_file_id = msg.document.file_id

        if not proof_text and not proof_file_id:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ØµÙØ±Ø© Ø¥ÙØµØ§Ù Ø§ÙØªØ­ÙÙÙ Ø£Ù ÙØªØ§Ø¨Ø© Ø±ÙÙ Ø§ÙØ¹ÙÙÙØ© Ø§ÙÙØ±Ø¬Ø¹Ù.")
            return

        req_id = db.create_topup_request(
            user_id=user.id,
            method=method,
            amount=amount,
            proof_text=proof_text,
            proof_file_id=proof_file_id,
        )
        clear_state(context)

        await msg.reply_text(
            f"â <b>ØªÙ Ø¥Ø±Ø³Ø§Ù Ø·ÙØ¨ Ø´Ø­Ù Ø§ÙØ±ØµÙØ¯ Ø±ÙÙ #{req_id} Ø¨ÙØ¬Ø§Ø­!</b>\n"
            "ââââââââââââââââââââ\n"
            f"ðµ <b>Ø§ÙÙØ¨ÙØº:</b> {amount:.2f} {html.escape(currency)}\n"
            "â³ Ø¬Ø§Ø±Ù ÙØ±Ø§Ø¬Ø¹Ø© Ø§ÙØªØ­ÙÙÙ ÙÙ ÙÙØ¨Ù Ø§ÙØ¥Ø¯Ø§Ø±Ø©Ø ÙØ³ÙØµÙ Ø¥ÙÙÙ Ø¥Ø´Ø¹Ø§Ø± ÙÙØ± Ø¥Ø¶Ø§ÙØ© Ø§ÙØ±ØµÙØ¯ ÙØ­Ø³Ø§Ø¨Ù.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð  Ø§ÙÙØ§Ø¦ÙØ© Ø§ÙØ±Ø¦ÙØ³ÙØ©", callback_data="home")]]),
        )

        # Notify Admin immediately
        if ADMIN_ID:
            m_label = "ð¡ Binance Pay" if method == "binance" else "ð£ InstaPay EG"
            adm_caption = (
                f"ð <b>Ø·ÙØ¨ Ø´Ø­Ù Ø±ØµÙØ¯ Ø¬Ø¯ÙØ¯ #{req_id}</b>\n"
                "ââââââââââââââââââââ\n"
                f"ð¤ <b>Ø§ÙØ¹ÙÙÙ:</b> {html.escape(user.full_name or '')} (@{html.escape(user.username or 'Ø¨Ø¯ÙÙ')})\n"
                f"ð <b>ÙØ¹Ø±Ù Ø§ÙØ¹ÙÙÙ:</b> <code>{user.id}</code>\n"
                f"ð³ <b>Ø·Ø±ÙÙØ© Ø§ÙØ¯ÙØ¹:</b> {m_label}\n"
                f"ðµ <b>Ø§ÙÙØ¨ÙØº:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n"
                f"ð <b>Ø§ÙØªÙØ§ØµÙÙ/Ø§ÙÙØ±Ø¬Ø¹:</b> {html.escape(proof_text or 'ÙØ±ÙÙ ØµÙØ±Ø© Ø¥ÙØµØ§Ù')}"
            )
            adm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("â ÙØ¨ÙÙ ÙØ¥Ø¶Ø§ÙØ© Ø§ÙØ±ØµÙØ¯", callback_data=f"adm_topup_ok:{req_id}"),
                    InlineKeyboardButton("â Ø±ÙØ¶ Ø§ÙØ·ÙØ¨", callback_data=f"adm_topup_no:{req_id}"),
                ]
            ])
            try:
                if proof_file_id:
                    await context.bot.send_photo(
                        chat_id=ADMIN_ID,
                        photo=proof_file_id,
                        caption=adm_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=adm_kb,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=adm_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=adm_kb,
                    )
            except TelegramError as exc:
                logger.warning("Failed to notify admin of topup: %s", exc)
        return

    # --------------------------------------------------------------------------
    # ADMIN STATES (OWNER ONLY)
    # --------------------------------------------------------------------------
    if not is_admin(user.id):
        clear_state(context)
        return

    if state == "ADM_ADD_COUNTRY":
        if not msg.text:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù Ø§Ø³Ù Ø§ÙØ¯ÙÙØ© ÙÙØµ.")
            return
        cid = db.add_country(msg.text.strip())
        clear_state(context)
        if not cid:
            await msg.reply_text(
                "â ï¸ ÙØ°Ù Ø§ÙØ¯ÙÙØ© ÙÙØ¬ÙØ¯Ø© Ø¨Ø§ÙÙØ¹Ù!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¯ÙÙ", callback_data="adm_countries")]]),
            )
            return
        await msg.reply_text(
            f"â <b>ØªÙØª Ø¥Ø¶Ø§ÙØ© Ø§ÙØ¯ÙÙØ© ({html.escape(msg.text.strip())}) Ø¨ÙØ¬Ø§Ø­!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© ÙÙØ°Ù Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plan_add:{cid}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ¯ÙÙ", callback_data="adm_countries")],
            ]),
        )
        return

    if state == "ADM_EDIT_COUNTRY":
        if not msg.text:
            return
        country_id = int(temp["country_id"])
        ok = db.update_country(country_id, msg.text.strip())
        clear_state(context)
        await msg.reply_text(
            "â <b>ØªÙ ØªØ­Ø¯ÙØ« Ø§Ø³Ù Ø§ÙØ¯ÙÙØ© Ø¨ÙØ¬Ø§Ø­!</b>" if ok else "â ï¸ ØªØ¹Ø°Ø± ØªØ­Ø¯ÙØ« Ø§ÙØ§Ø³Ù.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¯ÙÙØ©", callback_data=f"adm_country_view:{country_id}")]]),
        )
        return

    if state == "ADM_ADD_PLAN_NAME":
        if not msg.text:
            return
        temp["name"] = msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_ADD_PLAN_DESC"
        await msg.reply_text(
            "ð <b>Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© Ø¬Ø¯ÙØ¯Ø© (Ø§ÙØ®Ø·ÙØ© 2 ÙÙ 3)</b>\n"
            "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>ÙØµÙ ÙØªÙØ§ØµÙÙ Ø§ÙØ¨Ø§ÙØ©</b> (ÙØ«Ø§Ù: <code>ØµÙØ§Ø­ÙØ© 30 ÙÙÙ - ØªØ¯Ø¹Ù Ø§ÙØ¬ÙÙ Ø§ÙØ®Ø§ÙØ³ 5G - ÙØ§Ø¨ÙØ© ÙÙÙØ·Ø© Ø§ÙØ§ØªØµØ§Ù</code>) Ø£Ù Ø£Ø±Ø³Ù ÙÙØ·Ø© <code>.</code> ÙØªØ®Ø·Ù Ø§ÙÙØµÙ:",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_ADD_PLAN_DESC":
        if not msg.text:
            return
        desc = "" if msg.text.strip() == "." else msg.text.strip()
        temp["description"] = desc
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_ADD_PLAN_PRICE"
        await msg.reply_text(
            f"ðµ <b>Ø¥Ø¶Ø§ÙØ© Ø¨Ø§ÙØ© Ø¬Ø¯ÙØ¯Ø© (Ø§ÙØ®Ø·ÙØ© 3 ÙÙ 3)</b>\n"
            f"Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø³Ø¹Ø± Ø§ÙØ¨Ø§ÙØ© ({html.escape(currency)})</b> ÙØ£Ø±ÙØ§Ù ÙÙØ· (ÙØ«Ø§Ù: <code>12.5</code>):",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_ADD_PLAN_PRICE":
        if not msg.text:
            return
        try:
            price = round(float(msg.text.strip().replace(",", ".")), 2)
            if price < 0:
                raise ValueError()
        except ValueError:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø¯Ø®Ø§Ù Ø³Ø¹Ø± Ø±ÙÙÙ ØµØ­ÙØ­ (ÙØ«Ø§Ù: 10 Ø£Ù 14.99).")
            return
        country_id = int(temp["country_id"])
        plan_id = db.add_plan(country_id, temp["name"], temp.get("description", ""), price)
        clear_state(context)
        await msg.reply_text(
            f"â <b>ØªÙØª Ø¥Ø¶Ø§ÙØ© Ø§ÙØ¨Ø§ÙØ© ({html.escape(temp['name'])}) Ø¨Ø³Ø¹Ø± {price:.2f} {html.escape(currency)} Ø¨ÙØ¬Ø§Ø­!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø´Ø±Ø§Ø¦Ø­ eSIM ÙÙØ°Ù Ø§ÙØ¨Ø§ÙØ© Ø§ÙØ¢Ù", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¨Ø§ÙØ§Øª Ø§ÙØ¯ÙÙØ©", callback_data=f"adm_plans_country:{country_id}")],
            ]),
        )
        return

    if state == "ADM_EDIT_PLAN_NAME":
        if not msg.text:
            return
        plan_id = int(temp["plan_id"])
        plan = db.get_plan(plan_id)
        if not plan:
            clear_state(context)
            return
        temp["name"] = plan["name"] if msg.text.strip() == "." else msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_EDIT_PLAN_DESC"
        await msg.reply_text(
            "ð Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø§ÙÙØµÙ Ø§ÙØ¬Ø¯ÙØ¯ ÙÙØ¨Ø§ÙØ©</b> (Ø£Ù Ø£Ø±Ø³Ù ÙÙØ·Ø© <code>.</code> ÙÙØ¥Ø¨ÙØ§Ø¡ Ø¹ÙÙ Ø§ÙÙØµÙ Ø§ÙØ­Ø§ÙÙ):",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_EDIT_PLAN_DESC":
        if not msg.text:
            return
        plan_id = int(temp["plan_id"])
        plan = db.get_plan(plan_id)
        if not plan:
            clear_state(context)
            return
        temp["description"] = plan["description"] if msg.text.strip() == "." else msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_EDIT_PLAN_PRICE"
        await msg.reply_text(
            f"ðµ Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø§ÙØ³Ø¹Ø± Ø§ÙØ¬Ø¯ÙØ¯ ÙÙØ¨Ø§ÙØ© ({html.escape(currency)})</b> (Ø£Ù Ø£Ø±Ø³Ù ÙÙØ·Ø© <code>.</code> ÙÙØ¥Ø¨ÙØ§Ø¡ Ø¹ÙÙ Ø§ÙØ³Ø¹Ø± Ø§ÙØ­Ø§ÙÙ {float(plan['price']):.2f}):",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_EDIT_PLAN_PRICE":
        if not msg.text:
            return
        plan_id = int(temp["plan_id"])
        plan = db.get_plan(plan_id)
        if not plan:
            clear_state(context)
            return
        if msg.text.strip() == ".":
            price = float(plan["price"])
        else:
            try:
                price = round(float(msg.text.strip().replace(",", ".")), 2)
                if price < 0:
                    raise ValueError()
            except ValueError:
                await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø¯Ø®Ø§Ù Ø³Ø¹Ø± Ø±ÙÙÙ ØµØ­ÙØ­.")
                return
        db.update_plan(plan_id, temp["name"], temp["description"], price)
        clear_state(context)
        await msg.reply_text(
            "â <b>ØªÙ ØªØ­Ø¯ÙØ« Ø¨ÙØ§ÙØ§Øª Ø§ÙØ¨Ø§ÙØ© Ø¨ÙØ¬Ø§Ø­!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_view:{plan_id}")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: ADD OR EDIT eSIM (LPA OR QR CODE + AUTO BROADCAST)
    # --------------------------------------------------------------------------
    if state == "ADM_ADD_ESIM":
        plan_id = int(temp["plan_id"])
        plan = db.get_plan(plan_id)
        if not plan:
            clear_state(context)
            return

        added_count = 0
        # Case 1: Admin sent QR Photo or Image Document
        if msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/")):
            file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            tg_file = await context.bot.get_file(file_id)
            local_path = str(QR_DIR / f"qr_plan{plan_id}_{file_id[:12]}.png")
            await tg_file.download_to_drive(custom_path=local_path)
            caption_str = (msg.caption or "").strip()
            lpa_in_caption = caption_str if caption_str.upper().startswith("LPA:") else ""
            notes_in_caption = "" if lpa_in_caption else caption_str

            db.add_esim(
                plan_id=plan_id,
                item_type="qr",
                lpa_code=lpa_in_caption,
                qr_file_id=file_id,
                qr_local_path=local_path,
                notes=notes_in_caption,
            )
            added_count = 1

        # Case 2: Admin sent LPA code(s) as text
        elif msg.text:
            lines = [ln.strip() for ln in msg.text.splitlines() if ln.strip()]
            for ln in lines:
                db.add_esim(
                    plan_id=plan_id,
                    item_type="lpa",
                    lpa_code=ln,
                )
                added_count += 1
        else:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ÙÙØ¯ LPA ÙØµÙ Ø£Ù Ø¥Ø±Ø³Ø§Ù ØµÙØ±Ø© QR Code.")
            return

        clear_state(context)
        status_msg = await msg.reply_text(
            f"â <b>ØªÙØª Ø¥Ø¶Ø§ÙØ© {added_count} Ø´Ø±ÙØ­Ø© eSIM Ø¥ÙÙ ÙØ®Ø²ÙÙ ({html.escape(plan['name'])}) Ø¨ÙØ¬Ø§Ø­!</b>\n"
            "ð¢ Ø¬Ø§Ø±Ù Ø¥Ø±Ø³Ø§Ù Ø¥Ø´Ø¹Ø§Ø± Ø¹Ø§Ù ÙØ¬ÙÙØ¹ Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ Ø¨ØªÙÙØ± Ø§ÙÙØ®Ø²ÙÙ Ø§ÙØ¬Ø¯ÙØ¯...",
            parse_mode=ParseMode.HTML,
        )
        sent, failed = await broadcast_new_inventory(context, plan_id=plan_id, added_count=added_count)
        await status_msg.edit_text(
            f"â <b>ØªÙØª Ø¥Ø¶Ø§ÙØ© {added_count} Ø´Ø±ÙØ­Ø© eSIM Ø¥ÙÙ ÙØ®Ø²ÙÙ ({html.escape(plan['name'])}) Ø¨ÙØ¬Ø§Ø­!</b>\n"
            f"ð¢ <b>ØªÙ Ø¥Ø±Ø³Ø§Ù Ø§ÙØ¥Ø´Ø¹Ø§Ø± Ø§ÙØ¹Ø§Ù Ø¨ÙØ¬Ø§Ø­ Ø¥ÙÙ:</b> {sent} ÙØ³ØªØ®Ø¯Ù (ÙØ´Ù/ÙØ­Ø¸ÙØ±: {failed}).",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("â Ø¥Ø¶Ø§ÙØ© Ø´Ø±Ø§Ø¦Ø­ Ø£Ø®Ø±Ù ÙÙÙØ³ Ø§ÙØ¨Ø§ÙØ©", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("ð Ø¹Ø±Ø¶ ÙØ®Ø²ÙÙ Ø§ÙØ¨Ø§ÙØ©", callback_data=f"adm_esim_list:{plan_id}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¨Ø§ÙØ©", callback_data=f"adm_plan_view:{plan_id}")],
            ]),
        )
        return

    if state == "ADM_EDIT_ESIM":
        esim_id = int(temp["esim_id"])
        plan_id = int(temp["plan_id"])
        esim = db.get_esim(esim_id)
        if not esim:
            clear_state(context)
            return

        if msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/")):
            file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            tg_file = await context.bot.get_file(file_id)
            local_path = str(QR_DIR / f"qr_edit_{esim_id}_{file_id[:10]}.png")
            await tg_file.download_to_drive(custom_path=local_path)
            caption_str = (msg.caption or "").strip()
            lpa_in_caption = caption_str if caption_str.upper().startswith("LPA:") else esim.get("lpa_code", "")
            notes_in_caption = "" if caption_str.upper().startswith("LPA:") else (caption_str or esim.get("notes", ""))
            db.update_esim(
                esim_id=esim_id,
                item_type="qr",
                lpa_code=lpa_in_caption,
                qr_file_id=file_id,
                qr_local_path=local_path,
                notes=notes_in_caption,
            )
        elif msg.text:
            db.update_esim(
                esim_id=esim_id,
                item_type="lpa",
                lpa_code=msg.text.strip(),
                qr_file_id="",
                qr_local_path="",
                notes=esim.get("notes", ""),
            )
        else:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ÙÙØ¯ LPA ÙØµÙ Ø£Ù ØµÙØ±Ø© QR Code.")
            return

        clear_state(context)
        await msg.reply_text(
            f"â <b>ØªÙ ØªØ­Ø¯ÙØ« Ø¨ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ­Ø© #{esim_id} Ø¨ÙØ¬Ø§Ø­!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ð Ø¹Ø±Ø¶ Ø§ÙØ´Ø±ÙØ­Ø© Ø¨Ø¹Ø¯ Ø§ÙØªØ¹Ø¯ÙÙ", callback_data=f"adm_esim_item:{esim_id}")],
                [InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ§Ø¦ÙØ© Ø§ÙØ´Ø±Ø§Ø¦Ø­", callback_data=f"adm_esim_list:{plan_id}")],
            ]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: MANUAL BALANCE ADJUSTMENT
    # --------------------------------------------------------------------------
    if state == "ADM_MANUAL_BAL_USER":
        if not msg.text or not msg.text.strip().isdigit():
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ÙØ¹Ø±Ù Ø§ÙÙØ³ØªØ®Ø¯Ù (User ID) ÙØ£Ø±ÙØ§Ù ÙÙØ·.")
            return
        target_id = int(msg.text.strip())
        target_user = db.get_user(target_id)
        if not target_user:
            db.upsert_user(target_id, "", "ÙØ³ØªØ®Ø¯Ù")
            target_user = db.get_user(target_id)
        temp["target_id"] = target_id
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_MANUAL_BAL_AMOUNT"
        await msg.reply_text(
            f"ð¤ <b>Ø§ÙÙØ³ØªØ®Ø¯Ù:</b> <code>{target_id}</code> ({html.escape(target_user.get('full_name') or '')})\n"
            f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙØ­Ø§ÙÙ:</b> <code>{float(target_user['balance']):.2f}</code> {html.escape(currency)}\n\n"
            "Ø£Ø±Ø³Ù Ø§ÙØ¢Ù <b>Ø§ÙÙØ¨ÙØº Ø§ÙÙØ±Ø§Ø¯ Ø¥Ø¶Ø§ÙØªÙ Ø£Ù Ø®ØµÙÙ</b>:\n"
            "â¢ ÙØ¥Ø¶Ø§ÙØ© Ø±ØµÙØ¯ Ø£Ø±Ø³Ù Ø§ÙØ±ÙÙ ÙØ¨Ø§Ø´Ø±Ø© (ÙØ«Ø§Ù: <code>20</code>)\n"
            "â¢ ÙØ®ØµÙ Ø±ØµÙØ¯ Ø¶Ø¹ Ø¹ÙØ§ÙØ© Ø³Ø§ÙØ¨ ÙØ¨Ù Ø§ÙØ±ÙÙ (ÙØ«Ø§Ù: <code>-10</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_MANUAL_BAL_AMOUNT":
        if not msg.text:
            return
        try:
            delta = round(float(msg.text.strip().replace(",", ".")), 2)
        except ValueError:
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù Ø±ÙÙ ØµØ­ÙØ­ (ÙØ«Ø§Ù: 20 Ø£Ù -5).")
            return
        target_id = int(temp["target_id"])
        new_bal = db.update_user_balance(target_id, delta)
        clear_state(context)
        await msg.reply_text(
            f"â <b>ØªÙ ØªØ­Ø¯ÙØ« Ø±ØµÙØ¯ Ø§ÙÙØ³ØªØ®Ø¯Ù <code>{target_id}</code> Ø¨ÙØ¬Ø§Ø­!</b>\n"
            f"ð° <b>Ø§ÙØ±ØµÙØ¯ Ø§ÙØ¬Ø¯ÙØ¯:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¥Ø¯Ø§Ø±Ø© Ø§ÙØ£Ø±ØµØ¯Ø©", callback_data="adm_topups_menu")]]),
        )
        try:
            action_word = f"Ø¥Ø¶Ø§ÙØ© {delta:.2f}" if delta >= 0 else f"Ø®ØµÙ {abs(delta):.2f}"
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"ð <b>Ø¥Ø´Ø¹Ø§Ø± ØªØ­Ø¯ÙØ« Ø±ØµÙØ¯ Ø­Ø³Ø§Ø¨Ù</b>\n"
                    f"ØªÙ {action_word} {html.escape(currency)} ÙÙ Ø­Ø³Ø§Ø¨Ù Ø¨ÙØ§Ø³Ø·Ø© Ø§ÙØ¥Ø¯Ø§Ø±Ø©.\n"
                    f"ð° <b>Ø±ØµÙØ¯Ù Ø§ÙØ­Ø§ÙÙ:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    # --------------------------------------------------------------------------
    # ADMIN: BAN / UNBAN USER
    # --------------------------------------------------------------------------
    if state in ("ADM_BAN_USER", "ADM_UNBAN_USER"):
        if not msg.text or not msg.text.strip().isdigit():
            await msg.reply_text("â ï¸ ÙØ±Ø¬Ù Ø¥Ø±Ø³Ø§Ù ÙØ¹Ø±Ù Ø§ÙÙØ³ØªØ®Ø¯Ù (User ID) ÙØ£Ø±ÙØ§Ù ÙÙØ·.")
            return
        target_id = int(msg.text.strip())
        if target_id == ADMIN_ID:
            await msg.reply_text("â ï¸ ÙØ§ ÙÙÙÙÙ Ø­Ø¸Ø± Ø­Ø³Ø§Ø¨ ÙØ§ÙÙ Ø§ÙØ¨ÙØª!")
            return
        ban_flag = state == "ADM_BAN_USER"
        db.set_user_ban(target_id, ban_flag)
        clear_state(context)
        status_str = "ð« ØªÙ Ø­Ø¸Ø± Ø§ÙÙØ³ØªØ®Ø¯Ù" if ban_flag else "â ØªÙ ÙÙ Ø§ÙØ­Ø¸Ø± Ø¹Ù Ø§ÙÙØ³ØªØ®Ø¯Ù"
        await msg.reply_text(
            f"<b>{status_str} (<code>{target_id}</code>) Ø¨ÙØ¬Ø§Ø­!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙØ¥Ø¯Ø§Ø±Ø© Ø§ÙÙØ³ØªØ®Ø¯ÙÙÙ", callback_data="adm_users_menu")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: PUBLIC BROADCAST MESSAGE TO ALL USERS
    # --------------------------------------------------------------------------
    if state == "ADM_BROADCAST_MSG":
        clear_state(context)
        users = db.get_all_active_users()
        progress = await msg.reply_text(f"ð¢ Ø¬Ø§Ø±Ù Ø¥Ø±Ø³Ø§Ù Ø§ÙØ±Ø³Ø§ÙØ© Ø§ÙØ¬ÙØ§Ø¹ÙØ© Ø¥ÙÙ {len(users)} ÙØ³ØªØ®Ø¯Ù...")
        sent = 0
        failed = 0
        for u in users:
            try:
                await context.bot.copy_message(
                    chat_id=u["user_id"],
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
                sent += 1
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest, TelegramError):
                failed += 1
        await progress.edit_text(
            f"â <b>Ø§ÙØªÙÙ Ø¥Ø±Ø³Ø§Ù Ø§ÙØ±Ø³Ø§ÙØ© Ø§ÙØ¬ÙØ§Ø¹ÙØ©!</b>\n"
            "ââââââââââââââââââââ\n"
            f"ð¨ <b>ØªÙ Ø§ÙØ¥Ø±Ø³Ø§Ù Ø¨ÙØ¬Ø§Ø­ Ø¥ÙÙ:</b> {sent} ÙØ³ØªØ®Ø¯Ù\n"
            f"â ï¸ <b>ÙØ´Ù / ÙØ§ÙÙØ§ Ø¨Ø­Ø¸Ø± Ø§ÙØ¨ÙØª:</b> {failed} ÙØ³ØªØ®Ø¯Ù",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð ÙÙØ­Ø© ØªØ­ÙÙ Ø§ÙØ£Ø¯ÙÙ", callback_data="adm_home")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: UPDATE SETTINGS
    # --------------------------------------------------------------------------
    if state == "ADM_SET_SETTING":
        if not msg.text:
            return
        key = temp["key"]
        val = msg.text.strip()
        if key == "support_username":
            val = val.lstrip("@")
        db.set_setting(key, val)
        clear_state(context)
        await msg.reply_text(
            f"â <b>ØªÙ ØªØ­Ø¯ÙØ« Ø§ÙØ¥Ø¹Ø¯Ø§Ø¯ Ø¨ÙØ¬Ø§Ø­ Ø¥ÙÙ:</b> <code>{html.escape(val)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ð Ø§ÙØ¹ÙØ¯Ø© ÙÙØ¥Ø¹Ø¯Ø§Ø¯Ø§Øª", callback_data="adm_settings")]]),
        )
        return


# ==============================================================================
# Application Entrypoint
# ==============================================================================
def main() -> None:
    db.init_db()
    if not BOT_TOKEN:
        raise RuntimeError("Ø®Ø·Ø£: ÙÙ ÙØªÙ Ø¶Ø¨Ø· BOT_TOKEN ÙÙ ÙÙÙ .env! ÙÙ Ø¨ØªØ´ØºÙÙ ./install.sh Ø£Ù ØªØ¹Ø¯ÙÙ ÙÙÙ .env")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))

    logger.info("â ØªÙ ØªØ´ØºÙÙ Ø¨ÙØª ÙØªØ¬Ø± Ø´Ø±Ø§Ø¦Ø­ eSIM Ø¨ÙØ¬Ø§Ø­ (ÙØ¹ÙÙ Ø¹ÙÙ ÙØ¯Ø§Ø± Ø§ÙØ³Ø§Ø¹Ø© 24/7)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
