# -*- coding: utf-8 -*-
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

DEFAULT_CUSTOMER = "\u0639\u0632\u064a\u0632\u064a \u0627\u0644\u0639\u0645\u064a\u0644"
NO_DESC = "\u0628\u062f\u0648\u0646 \u0648\u0635\u0641"
NO_LPA_QR = "\u0644\u0627 \u064a\u0648\u062c\u062f (\u0635\u0648\u0631\u0629 QR)"
NONE_STR = "\u0644\u0627 \u064a\u0648\u062c\u062f"
NO_USER = "\u0628\u062f\u0648\u0646"
ATTACHED_RECEIPT = "\u0645\u0631\u0641\u0642 \u0635\u0648\u0631\u0629 \u0625\u064a\u0635\u0627\u0644"



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
            "\u26d4 <b>\u0639\u0630\u0631\u0627\u064b\u060c \u062a\u0645 \u062d\u0638\u0631 \u062d\u0633\u0627\u0628\u0643 \u0645\u0646 \u0627\u0633\u062a\u062e\u062f\u0627\u0645 \u0647\u0630\u0627 \u0627\u0644\u0628\u0648\u062a.</b>\n\n"
            f"\u0644\u0644\u062a\u0648\u0627\u0635\u0644 \u0645\u0639 \u0627\u0644\u0625\u062f\u0627\u0631\u0629 \u0648\u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631: @{html.escape(support)}"
        )
        if update.callback_query:
            await update.callback_query.answer("\u26d4 \u062d\u0633\u0627\u0628\u0643 \u0645\u062d\u0638\u0648\u0631 \u0645\u0646 \u0627\u0633\u062a\u062e\u062f\u0627\u0645 \u0627\u0644\u0628\u0648\u062a.", show_alert=True)
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
    header_title: str = "\U0001f389 <b>\u062a\u0645 \u0634\u0631\u0627\u0621 \u0634\u0631\u064a\u062d\u0629 eSIM \u0628\u0646\u062c\u0627\u062d!</b>",
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
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {country_name}",
        f"\U0001f4e6 <b>\u0627\u0644\u0628\u0627\u0642\u0629:</b> {plan_name}",
        f"\U0001f4b5 <b>\u0627\u0644\u0633\u0639\u0631:</b> {paid_price:.2f} {html.escape(currency)}",
    ]
    if new_balance is not None:
        caption_lines.append(f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0643 \u0627\u0644\u0645\u062a\u0628\u0642\u064a:</b> {float(new_balance):.2f} {html.escape(currency)}")
    if lpa_code:
        caption_lines.extend([
            "",
            "\U0001f511 <b>\u0643\u0648\u062f \u0627\u0644\u062a\u0641\u0639\u064a\u0644 \u0627\u0644\u0645\u0628\u0627\u0634\u0631 (LPA):</b>",
            f"<code>{html.escape(lpa_code)}</code>",
        ])
    if notes:
        caption_lines.extend([
            "",
            f"\U0001f4dd <b>\u0645\u0644\u0627\u062d\u0638\u0627\u062a \u0625\u0636\u0627\u0641\u064a\u0629:</b>\n{html.escape(notes)}",
        ])
    caption_lines.extend([
        "",
        "\U0001f4f2 <b>\u0637\u0631\u064a\u0642\u0629 \u0627\u0644\u062a\u0641\u0639\u064a\u0644:</b>",
        "1\ufe0f\u20e3 \u0627\u0641\u062a\u062d \u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a \u0641\u064a \u0647\u0627\u062a\u0641\u0643 \u062b\u0645 (\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0647\u0627\u062a\u0641 / \u0627\u0644\u062e\u0644\u0648\u064a\u0629).",
        "2\ufe0f\u20e3 \u0627\u062e\u062a\u0631 <b>\u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u064a\u062d\u0629 \u0625\u0644\u0643\u062a\u0631\u0648\u0646\u064a\u0629 (Add eSIM)</b>.",
        "3\ufe0f\u20e3 \u0627\u0645\u0633\u062d \u0643\u0648\u062f \u0627\u0644\u0640 QR \u0627\u0644\u0645\u0631\u0641\u0642 \u0623\u0648 \u0627\u0646\u0633\u062e \u0643\u0648\u062f \u0627\u0644\u0640 LPA \u0623\u0639\u0644\u0627\u0647 \u0648\u0623\u062f\u062e\u0644\u0647 \u064a\u062f\u0648\u064a\u0627\u064b.",
    ])
    caption = "\n".join(caption_lines)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")]])

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

    desc_line = f"\U0001f4dd <b>\u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644:</b> {html.escape(plan['description'])}\n" if plan.get("description") else ""
    text = (
        "\U0001f514 <b>\u0625\u0634\u0639\u0627\u0631 \u062a\u0648\u0641\u0631 \u0645\u062e\u0632\u0648\u0646 \u062c\u062f\u064a\u062f \u0645\u0646 \u0634\u0631\u0627\u0626\u062d eSIM!</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(plan['country_name'])}\n"
        f"\U0001f4e6 <b>\u0627\u0644\u0628\u0627\u0642\u0629:</b> {html.escape(plan['name'])}\n"
        f"{desc_line}"
        f"\U0001f4b5 <b>\u0627\u0644\u0633\u0639\u0631:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
        f"\u2795 <b>\u0627\u0644\u0643\u0645\u064a\u0629 \u0627\u0644\u0645\u0636\u0627\u0641\u0629 \u0627\u0644\u0622\u0646:</b> {added_count} \u0634\u0631\u064a\u062d\u0629\n"
        f"\u2705 <b>\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u062a\u0627\u062d \u062d\u0627\u0644\u064a\u0627\u064b:</b> {plan['available_count']} \u0634\u0631\u064a\u062d\u0629\n\n"
        "\U0001f447 <b>\u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0627\u0644\u0632\u0631 \u0628\u0627\u0644\u0623\u0633\u0641\u0644 \u0644\u0634\u0631\u0627\u0621 \u0627\u0644\u0628\u0627\u0642\u0629 \u0641\u0648\u0631\u0627\u064b:</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f6d2 \u0634\u0631\u0627\u0621 \u0647\u0630\u0647 \u0627\u0644\u0628\u0627\u0642\u0629 \u0627\u0644\u0622\u0646", callback_data=f"u_plan:{plan_id}")],
        [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
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
        [InlineKeyboardButton("\U0001f6d2 \u0634\u0631\u0627\u0621 \u0634\u0631\u064a\u062d\u0629 eSIM", callback_data="u_countries")],
        [
            InlineKeyboardButton("\U0001f4b3 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f", callback_data="u_topup"),
            InlineKeyboardButton("\U0001f4b0 \u0631\u0635\u064a\u062f\u064a \u0648\u062d\u0633\u0627\u0628\u064a", callback_data="u_account"),
        ],
        [
            InlineKeyboardButton("\U0001f4e6 \u0645\u0634\u062a\u0631\u064a\u0627\u062a\u064a \u0627\u0644\u0633\u0627\u0628\u0642\u0629", callback_data="u_purchases"),
            InlineKeyboardButton("\U0001f6df \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a", callback_data="u_support"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("\U0001f6e0 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646 (\u0627\u0644\u0645\u0627\u0644\u0643)", callback_data="adm_home")])
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
        f"\U0001f44b <b>\u0623\u0647\u0644\u0627\u064b \u0628\u0643 \u064a\u0627 {html.escape(user.first_name or DEFAULT_CUSTOMER)} \u0641\u064a \u0645\u062a\u062c\u0631 \u0634\u0631\u0627\u0626\u062d eSIM \u0627\u0644\u0631\u0642\u0645\u064a\u0629!</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\U0001f310 \u0646\u0642\u062f\u0645 \u0644\u0643 \u0634\u0631\u0627\u0626\u062d \u0627\u062a\u0635\u0627\u0644 \u0625\u0644\u0643\u062a\u0631\u0648\u0646\u064a\u0629 (eSIM) \u0641\u0648\u0631\u064a\u0629 \u0644\u062c\u0645\u064a\u0639 \u062f\u0648\u0644 \u0627\u0644\u0639\u0627\u0644\u0645 \u0628\u0623\u0633\u0639\u0627\u0631 \u062a\u0646\u0627\u0641\u0633\u064a\u0629 \u0648\u0627\u0633\u062a\u0644\u0627\u0645 \u0641\u0648\u0631\u064a \u0644\u0643\u0648\u062f \u0627\u0644\u0640 QR.\n\n"
        f"\U0001f194 <b>\u0645\u0639\u0631\u0641 \u062d\u0633\u0627\u0628\u0643:</b> <code>{user.id}</code>\n"
        f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0643 \u0627\u0644\u062d\u0627\u0644\u064a:</b> <code>{float(u_row['balance']):.2f}</code> {html.escape(currency)}\n\n"
        "\U0001f447 <b>\u0627\u062e\u062a\u0631 \u0645\u0646 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062a\u0627\u0644\u064a\u0629 \u0645\u0627 \u064a\u0646\u0627\u0633\u0628\u0643:</b>"
    )
    await safe_edit_or_send(update, text, build_home_keyboard(user.id))


async def show_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("\u26d4 \u0647\u0630\u0647 \u0627\u0644\u0644\u0648\u062d\u0629 \u0645\u062e\u0635\u0635\u0629 \u0644\u0645\u0627\u0644\u0643 \u0627\u0644\u0628\u0648\u062a \u0641\u0642\u0637.", show_alert=True)
        return
    clear_state(context)
    stats = db.get_stats()
    currency = get_currency()

    text = (
        "\U0001f6e0 <b>\u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0645\u0627\u0644\u0643 (Admin Panel)</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f465 <b>\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646:</b> {stats['total_users']} (\u0627\u0644\u0645\u062d\u0638\u0648\u0631\u064a\u0646: {stats['banned_users']})\n"
        f"\U0001f30d <b>\u0639\u062f\u062f \u0627\u0644\u062f\u0648\u0644 \u0627\u0644\u0645\u0636\u0627\u0641\u0629:</b> {stats['total_countries']}\n"
        f"\U0001f4e6 <b>\u0639\u062f\u062f \u0627\u0644\u0628\u0627\u0642\u0627\u062a:</b> {stats['total_plans']}\n"
        f"\U0001f4f2 <b>\u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u062a\u0627\u062d\u0629 \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646:</b> {stats['available_esims']}\n"
        f"\u2705 <b>\u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u0628\u0627\u0639\u0629:</b> {stats['sold_esims']}\n"
        f"\U0001f4b5 <b>\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0628\u064a\u0639\u0627\u062a:</b> {stats['total_revenue']:.2f} {html.escape(currency)}\n"
        f"\u23f3 <b>\u0637\u0644\u0628\u0627\u062a \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u0645\u0639\u0644\u0642\u0629:</b> {stats['pending_topups']}\n\n"
        "\U0001f447 <b>\u0627\u062e\u062a\u0631 \u0627\u0644\u0642\u0633\u0645 \u0627\u0644\u0630\u064a \u062a\u0631\u064a\u062f \u0625\u062f\u0627\u0631\u062a\u0647:</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f30d \u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u062f\u0648\u0644", callback_data="adm_countries"),
            InlineKeyboardButton("\U0001f4e6 \u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0628\u0627\u0642\u0627\u062a", callback_data="adm_plans_SelectCountry"),
        ],
        [
            InlineKeyboardButton("\U0001f4f2 \u0625\u062f\u0627\u0631\u0629 \u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u0634\u0631\u0627\u0626\u062d (eSIMs)", callback_data="adm_inv_SelectCountry"),
        ],
        [
            InlineKeyboardButton(
                f"\U0001f4b3 \u0637\u0644\u0628\u0627\u062a \u0627\u0644\u0634\u062d\u0646 \u0648\u0627\u0644\u0623\u0631\u0635\u062f\u0629 ({stats['pending_topups']})",
                callback_data="adm_topups_menu",
            ),
        ],
        [
            InlineKeyboardButton("\U0001f465 \u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646 \u0648\u0627\u0644\u062d\u0638\u0631", callback_data="adm_users_menu"),
            InlineKeyboardButton("\U0001f4e2 \u0631\u0633\u0627\u0644\u0629 \u062c\u0645\u0627\u0639\u064a\u0629 \u0644\u0644\u0643\u0644", callback_data="adm_broadcast_start"),
        ],
        [
            InlineKeyboardButton("\u2699\ufe0f \u0625\u0639\u062f\u0627\u062f\u0627\u062a \u0627\u0644\u062f\u0641\u0639 \u0648\u0627\u0644\u062f\u0639\u0645", callback_data="adm_settings"),
        ],
        [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0648\u0627\u062c\u0647\u0629 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645", callback_data="home")],
    ])
    await safe_edit_or_send(update, text, kb)


# ==============================================================================
# Command Handlers
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_home(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("\u26d4 \u0639\u0630\u0631\u0627\u064b\u060c \u0647\u0630\u0627 \u0627\u0644\u0623\u0645\u0631 \u0645\u062e\u0635\u0635 \u0644\u0645\u0627\u0644\u0643 \u0627\u0644\u0628\u0648\u062a \u0641\u0642\u0637.")
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
            "\U0001f4b0 <b>\u062a\u0641\u0627\u0635\u064a\u0644 \u062d\u0633\u0627\u0628\u064a \u0648\u0631\u0635\u064a\u062f\u064a</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f464 <b>\u0627\u0644\u0627\u0633\u0645:</b> {html.escape(user.full_name or '')}\n"
            f"\U0001f194 <b>\u0627\u0644\u0645\u0639\u0631\u0641 (ID):</b> <code>{user.id}</code>\n"
            f"\U0001f4b5 <b>\u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u0645\u062a\u0627\u062d:</b> <code>{float(u_row['balance']):.2f}</code> {html.escape(currency)}\n"
            f"\U0001f4e6 <b>\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u0634\u062a\u0631\u0627\u0629:</b> {len(purchases)} \u0634\u0631\u064a\u062d\u0629\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4b3 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u0622\u0646", callback_data="u_topup")],
            [InlineKeyboardButton("\U0001f4e6 \u0639\u0631\u0636 \u0645\u0634\u062a\u0631\u064a\u0627\u062a\u064a", callback_data="u_purchases")],
            [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data == "u_support":
        await q.answer()
        support_user = db.get_setting("support_username", "Support").lstrip("@")
        text = (
            "\U0001f6df <b>\u0645\u0631\u0643\u0632 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a \u0648\u062e\u062f\u0645\u0629 \u0627\u0644\u0639\u0645\u0644\u0627\u0621</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\u0625\u0630\u0627 \u0648\u0627\u062c\u0647\u062a\u0643 \u0623\u064a \u0645\u0634\u0643\u0644\u0629 \u0641\u064a \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0623\u0648 \u062a\u0641\u0639\u064a\u0644 \u0634\u0631\u064a\u062d\u0629 \u0627\u0644\u0640 eSIM \u0627\u0644\u062e\u0627\u0635\u0629 \u0628\u0643\u060c \u064a\u0633\u0639\u062f\u0646\u0627 \u062a\u0648\u0627\u0635\u0644\u0643 \u0645\u0639\u0646\u0627 \u0645\u0628\u0627\u0634\u0631\u0629:\n\n"
            f"\U0001f468\u200d\U0001f4bb <b>\u062d\u0633\u0627\u0628 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a:</b> @{html.escape(support_user)}\n"
            f"\U0001f194 <b>\u0645\u0639\u0631\u0641 \u062d\u0633\u0627\u0628\u0643 \u0644\u062a\u0632\u0648\u064a\u062f \u0627\u0644\u062f\u0639\u0645 \u0628\u0647:</b> <code>{user.id}</code>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4ac \u0645\u0631\u0627\u0633\u0644\u0629 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a \u0645\u0628\u0627\u0634\u0631\u0629", url=f"https://t.me/{support_user}")],
            [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ---------------- USER: BUY eSIM FLOW ----------------
    if data == "u_countries":
        await q.answer()
        countries = db.get_countries()
        if not countries:
            text = (
                "\U0001f30d <b>\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062f\u0648\u0644 \u0627\u0644\u0645\u062a\u0627\u062d\u0629</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u26a0\ufe0f \u0644\u0627 \u062a\u0648\u062c\u062f \u062f\u0648\u0644 \u0645\u0636\u0627\u0641\u0629 \u062d\u0627\u0644\u064a\u0627\u064b \u0641\u064a \u0627\u0644\u0645\u062a\u062c\u0631. \u064a\u0631\u062c\u0649 \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0629 \u0644\u0627\u062d\u0642\u0627\u064b."
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")]])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for c in countries:
            stock = int(c["available_stock"] or 0)
            badge = f"\u2705 ({stock})" if stock > 0 else "\u274c \u0646\u0641\u062f"
            rows.append([
                InlineKeyboardButton(
                    f"{c['name']} \u2014 {badge}",
                    callback_data=f"u_country:{c['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")])
        text = (
            "\U0001f30d <b>\u0627\u062e\u062a\u0631 \u0627\u0644\u062f\u0648\u0644\u0629 \u0627\u0644\u062a\u064a \u062a\u0631\u063a\u0628 \u0628\u0634\u0631\u0627\u0621 \u0634\u0631\u064a\u062d\u0629 eSIM \u0644\u0647\u0627:</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\u064a\u0638\u0647\u0631 \u0628\u062c\u0627\u0646\u0628 \u0643\u0644 \u062f\u0648\u0644\u0629 \u0639\u062f\u062f \u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u062a\u0648\u0641\u0631\u0629 \u0644\u0644\u0627\u0633\u062a\u0644\u0627\u0645 \u0627\u0644\u0641\u0648\u0631\u064a:"
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
                f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629: {html.escape(country['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u26a0\ufe0f \u0644\u0627 \u062a\u0648\u062c\u062f \u0628\u0627\u0642\u0627\u062a \u0645\u0636\u0627\u0641\u0629 \u0644\u0647\u0630\u0647 \u0627\u0644\u062f\u0648\u0644\u0629 \u062d\u0627\u0644\u064a\u0627\u064b."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062f\u0648\u0644", callback_data="u_countries")],
                [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for p in plans:
            avail = int(p["available_count"] or 0)
            status_str = f"\u0645\u062a\u0627\u062d ({avail})" if avail > 0 else "\u0646\u0641\u062f\u062a \u0627\u0644\u0643\u0645\u064a\u0629"
            rows.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {p['name']} | {float(p['price']):.2f} {currency} ({status_str})",
                    callback_data=f"u_plan:{p['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062f\u0648\u0644", callback_data="u_countries")])
        rows.append([InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")])
        text = (
            f"\U0001f30d <b>\u0628\u0627\u0642\u0627\u062a \u0634\u0631\u0627\u0626\u062d eSIM \u0644\u062f\u0648\u0644\u0629: {html.escape(country['name'])}</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\U0001f447 \u0627\u062e\u062a\u0631 \u0627\u0644\u0628\u0627\u0642\u0629 \u0627\u0644\u0645\u0646\u0627\u0633\u0628\u0629 \u0644\u0643 \u0644\u0639\u0631\u0636 \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0648\u0627\u0644\u0634\u0631\u0627\u0621 \u0627\u0644\u0641\u0648\u0631\u064a:"
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

        desc_block = f"\U0001f4dd <b>\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0628\u0627\u0642\u0629:</b>\n{html.escape(plan['description'])}\n\n" if plan.get("description") else ""
        stock_line = f"\u2705 \u0645\u062a\u0648\u0641\u0631 \u0644\u0644\u0627\u0633\u062a\u0644\u0627\u0645 \u0627\u0644\u0641\u0648\u0631\u064a ({avail} \u0634\u0631\u064a\u062d\u0629)" if avail > 0 else "\u274c \u0646\u0641\u062f\u062a \u0627\u0644\u0643\u0645\u064a\u0629 \u062d\u0627\u0644\u064a\u0627\u064b"

        text = (
            f"\U0001f4e6 <b>\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0628\u0627\u0642\u0629 \u0627\u0644\u0645\u062e\u062a\u0627\u0631\u0629</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(plan['country_name'])}\n"
            f"\U0001f3f7 <b>\u0627\u0633\u0645 \u0627\u0644\u0628\u0627\u0642\u0629:</b> {html.escape(plan['name'])}\n"
            f"\U0001f4b5 <b>\u0633\u0639\u0631 \u0627\u0644\u0628\u0627\u0642\u0629:</b> <code>{price:.2f}</code> {html.escape(currency)}\n"
            f"\U0001f4ca <b>\u062d\u0627\u0644\u0629 \u0627\u0644\u0645\u062e\u0632\u0648\u0646:</b> {stock_line}\n\n"
            f"{desc_block}"
            f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0643 \u0627\u0644\u062d\u0627\u0644\u064a:</b> <code>{balance:.2f}</code> {html.escape(currency)}"
        )
        rows = []
        if avail > 0:
            if balance >= price:
                rows.append([
                    InlineKeyboardButton(
                        f"\u2705 \u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0634\u0631\u0627\u0621 ({price:.2f} {currency})",
                        callback_data=f"u_buy_confirm:{plan_id}",
                    )
                ])
            else:
                diff = round(price - balance, 2)
                text += f"\n\n\u26a0\ufe0f <b>\u0631\u0635\u064a\u062f\u0643 \u063a\u064a\u0631 \u0643\u0627\u0641\u064d \u0644\u0625\u062a\u0645\u0627\u0645 \u0627\u0644\u0634\u0631\u0627\u0621. \u062a\u062d\u062a\u0627\u062c \u0625\u0644\u0649 \u0634\u062d\u0646 <code>{diff:.2f}</code> {html.escape(currency)} \u0625\u0636\u0627\u0641\u064a\u0629.</b>"
                rows.append([InlineKeyboardButton("\U0001f4b3 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u0622\u0646", callback_data="u_topup")])
        rows.append([InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0628\u0627\u0642\u0627\u062a \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"u_country:{plan['country_id']}")])
        rows.append([InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")])
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_buy_confirm:"):
        plan_id = int(data.split(":")[1])
        ok, code, result = db.purchase_esim_atomic(user.id, plan_id)
        if not ok:
            if code == "insufficient_balance":
                await q.answer("\u26a0\ufe0f \u0631\u0635\u064a\u062f\u0643 \u063a\u064a\u0631 \u0643\u0627\u0641\u064d \u0644\u0625\u062a\u0645\u0627\u0645 \u0639\u0645\u0644\u064a\u0629 \u0627\u0644\u0634\u0631\u0627\u0621!", show_alert=True)
            elif code == "out_of_stock":
                await q.answer("\u26a0\ufe0f \u0639\u0630\u0631\u0627\u064b\u060c \u0646\u0641\u062f\u062a \u0643\u0645\u064a\u0629 \u0647\u0630\u0647 \u0627\u0644\u0628\u0627\u0642\u0629 \u0644\u0644\u062a\u0648!", show_alert=True)
            else:
                await q.answer("\u26a0\ufe0f \u062a\u0639\u0630\u0631 \u0625\u062a\u0645\u0627\u0645 \u0627\u0644\u0634\u0631\u0627\u0621\u060c \u062d\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649.", show_alert=True)
            return

        await q.answer("\U0001f389 \u062a\u0645\u062a \u0639\u0645\u0644\u064a\u0629 \u0627\u0644\u0634\u0631\u0627\u0621 \u0628\u0646\u062c\u0627\u062d!")
        # Deliver QR & LPA to buyer
        await deliver_esim_to_chat(
            context=context,
            chat_id=user.id,
            esim=result,
            header_title="\U0001f389 <b>\u062a\u0645 \u0634\u0631\u0627\u0621 \u0634\u0631\u064a\u062d\u0629 eSIM \u0628\u0646\u062c\u0627\u062d!</b>",
        )
        # Notify Admin of sale
        if ADMIN_ID:
            try:
                user_tag = f"@{user.username}" if user.username else user.full_name
                admin_msg = (
                    "\U0001f6d2 <b>\u0639\u0645\u0644\u064a\u0629 \u0628\u064a\u0639 \u0634\u0631\u064a\u062d\u0629 eSIM \u062c\u062f\u064a\u062f\u0629!</b>\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\U0001f464 <b>\u0627\u0644\u0639\u0645\u064a\u0644:</b> {html.escape(user_tag or '')} (<code>{user.id}</code>)\n"
                    f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(result['country_name'])}\n"
                    f"\U0001f4e6 <b>\u0627\u0644\u0628\u0627\u0642\u0629:</b> {html.escape(result['plan_name'])}\n"
                    f"\U0001f4b5 <b>\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0645\u062e\u0635\u0648\u0645:</b> {float(result['paid_price']):.2f} {html.escape(currency)}\n"
                    f"\U0001f194 <b>\u0631\u0642\u0645 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646:</b> #{result['id']}"
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
                "\U0001f4e6 <b>\u0645\u0634\u062a\u0631\u064a\u0627\u062a\u064a \u0627\u0644\u0633\u0627\u0628\u0642\u0629</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0644\u0645 \u062a\u0642\u0645 \u0628\u0634\u0631\u0627\u0621 \u0623\u064a \u0634\u0631\u064a\u062d\u0629 eSIM \u062d\u062a\u0649 \u0627\u0644\u0622\u0646."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f6d2 \u062a\u0635\u0641\u062d \u0627\u0644\u062f\u0648\u0644 \u0648\u0627\u0644\u0628\u0627\u0642\u0627\u062a", callback_data="u_countries")],
                [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for item in purchases[:20]:
            rows.append([
                InlineKeyboardButton(
                    f"\U0001f4f2 #{item['id']} | {item['country_name']} - {item['plan_name']} ({float(item['sold_price'] or 0):.2f} {currency})",
                    callback_data=f"u_view_esim:{item['id']}",
                )
            ])
        rows.append([InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")])
        text = (
            "\U0001f4e6 <b>\u0633\u062c\u0644 \u0645\u0634\u062a\u0631\u064a\u0627\u062a\u064a \u0645\u0646 \u0634\u0631\u0627\u0626\u062d eSIM</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\U0001f447 \u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0623\u064a \u0634\u0631\u064a\u062d\u0629 \u0644\u0625\u0639\u0627\u062f\u0629 \u0639\u0631\u0636 \u0643\u0648\u062f \u0627\u0644\u0640 QR \u0648\u0643\u0648\u062f \u0627\u0644\u062a\u0641\u0639\u064a\u0644 \u0627\u0644\u062e\u0627\u0635 \u0628\u0647\u0627:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_view_esim:"):
        esim_id = int(data.split(":")[1])
        esim = db.get_esim(esim_id)
        if not esim or esim.get("sold_to") != user.id:
            await q.answer("\u26a0\ufe0f \u062a\u0639\u0630\u0631 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0647\u0630\u0647 \u0627\u0644\u0634\u0631\u064a\u062d\u0629.", show_alert=True)
            return
        await q.answer("\U0001f4f2 \u062c\u0627\u0631\u064a \u0625\u0631\u0633\u0627\u0644 \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0634\u0631\u064a\u062d\u0629...")
        await deliver_esim_to_chat(
            context=context,
            chat_id=user.id,
            esim=esim,
            header_title=f"\U0001f4e6 <b>\u0628\u064a\u0627\u0646\u0627\u062a \u0634\u0631\u064a\u062d\u062a\u0643 \u0627\u0644\u0645\u0634\u062a\u0631\u0627\u0629 (#{esim['id']})</b>",
        )
        return

    # ---------------- USER: TOP-UP FLOW ----------------
    if data == "u_topup":
        await q.answer()
        clear_state(context)
        text = (
            "\U0001f4b3 <b>\u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0641\u064a \u0627\u0644\u0628\u0648\u062a</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "\u0627\u062e\u062a\u0631 \u0648\u0633\u064a\u0644\u0629 \u0627\u0644\u062f\u0641\u0639 \u0627\u0644\u0645\u0646\u0627\u0633\u0628\u0629 \u0644\u0643 \u0644\u0634\u062d\u0646 \u0631\u0635\u064a\u062f\u0643:\n\n"
            "\U0001f7e1 <b>Binance Pay (\u0639\u0645\u0644\u0627\u062a \u0631\u0642\u0645\u064a\u0629 - USDT)</b>\n"
            "\U0001f7e3 <b>InstaPay EG (\u0625\u0646\u0633\u062a\u0627 \u0628\u0627\u064a \u0645\u0635\u0631)</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f7e1 \u0627\u0644\u062f\u0641\u0639 \u0639\u0628\u0631 Binance ID (\u0639\u0645\u0644\u0627\u062a \u0631\u0642\u0645\u064a\u0629)", callback_data="u_topup_m:binance")],
            [InlineKeyboardButton("\U0001f7e3 \u0627\u0644\u062f\u0641\u0639 \u0639\u0628\u0631 InstaPay EG (\u0625\u0646\u0633\u062a\u0627 \u0628\u0627\u064a)", callback_data="u_topup_m:instapay")],
            [InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data.startswith("u_topup_m:"):
        await q.answer()
        method = data.split(":")[1]
        context.user_data["state"] = "USER_TOPUP_AMOUNT"
        context.user_data["temp"] = {"method": method}

        if method == "binance":
            pay_target = db.get_setting("binance_id", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f")
            method_title = "\U0001f7e1 Binance Pay (\u0639\u0645\u0644\u0627\u062a \u0631\u0642\u0645\u064a\u0629)"
            target_label = "\u0645\u0639\u0631\u0641 Binance ID \u0644\u0644\u062a\u062d\u0648\u064a\u0644"
        else:
            pay_target = db.get_setting("instapay_address", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f")
            method_title = "\U0001f7e3 InstaPay EG (\u0625\u0646\u0633\u062a\u0627 \u0628\u0627\u064a \u0645\u0635\u0631)"
            target_label = "\u0639\u0646\u0648\u0627\u0646 \u0627\u0644\u062f\u0641\u0639 / \u0631\u0642\u0645 InstaPay EG"

        text = (
            f"\U0001f4b3 <b>\u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0639\u0628\u0631 {method_title}</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4cc <b>{target_label}:</b>\n"
            f"<code>{html.escape(pay_target)}</code>\n"
            "<i>(\u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0627\u0644\u0643\u0648\u062f \u0623\u0639\u0644\u0627\u0647 \u0644\u0646\u0633\u062e\u0647)</i>\n\n"
            "1\ufe0f\u20e3 \u0642\u0645 \u0628\u062a\u062d\u0648\u064a\u0644 \u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u0637\u0644\u0648\u0628 \u0625\u0644\u0649 \u0627\u0644\u062d\u0633\u0627\u0628 \u0627\u0644\u0645\u0648\u0636\u062d \u0623\u0639\u0644\u0627\u0647.\n"
            f"2\ufe0f\u20e3 \u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0642\u064a\u0645\u0629 \u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u0631\u0627\u062f \u0634\u062d\u0646\u0647 ({html.escape(currency)})</b> \u0641\u064a \u0631\u0633\u0627\u0644\u0629 \u0646\u0635\u064a\u0629 \u0628\u0627\u0644\u0623\u0633\u0641\u0644 (\u0645\u062b\u0627\u0644: <code>10</code> \u0623\u0648 <code>25.5</code>):"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f519 \u062a\u063a\u064a\u064a\u0631 \u0648\u0633\u064a\u0644\u0629 \u0627\u0644\u062f\u0641\u0639", callback_data="u_topup")],
            [InlineKeyboardButton("\U0001f3e0 \u0625\u0644\u063a\u0627\u0621 \u0648\u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ==========================================================================
    # ADMIN PANEL CALLBACKS (OWNER ONLY)
    # ==========================================================================
    if data.startswith("adm_"):
        if not is_admin(user.id):
            await q.answer("\u26d4 \u063a\u064a\u0631 \u0645\u0635\u0631\u062d \u0644\u0643 \u0628\u0627\u0644\u062f\u062e\u0648\u0644!", show_alert=True)
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
            rows = [[InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    InlineKeyboardButton(
                        f"\U0001f30d {c['name']} (\u0627\u0644\u0628\u0627\u0642\u0627\u062a: {c['plans_count']} | \u0627\u0644\u0645\u062e\u0632\u0648\u0646: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            text = (
                "\U0001f30d <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u062f\u0648\u0644</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u064a\u0645\u0643\u0646\u0643 \u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u062c\u062f\u064a\u062f\u0629 \u0623\u0648 \u0627\u0644\u0636\u063a\u0637 \u0639\u0644\u0649 \u0623\u064a \u062f\u0648\u0644\u0629 \u0644\u062a\u0639\u062f\u064a\u0644\u0647\u0627 \u0623\u0648 \u062d\u0630\u0641\u0647\u0627 \u0623\u0648 \u0625\u062f\u0627\u0631\u0629 \u0628\u0627\u0642\u0627\u062a\u0647\u0627:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_country_add":
            await q.answer()
            context.user_data["state"] = "ADM_ADD_COUNTRY"
            text = (
                "\u2795 <b>\u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u062c\u062f\u064a\u062f\u0629</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 \u0627\u0633\u0645 \u0627\u0644\u062f\u0648\u0644\u0629 \u0645\u0639 \u0627\u0644\u0639\u0644\u0645 (\u0645\u062b\u0627\u0644: <code>\U0001f1f9\U0001f1f7 \u062a\u0631\u0643\u064a\u0627</code> \u0623\u0648 <code>\U0001f1f8\U0001f1e6 \u0627\u0644\u0633\u0639\u0648\u062f\u064a\u0629</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_countries")]])
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
                f"\U0001f30d <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u062f\u0648\u0644\u0629: {html.escape(country['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f4e6 <b>\u0639\u062f\u062f \u0627\u0644\u0628\u0627\u0642\u0627\u062a \u0627\u0644\u0645\u0636\u0627\u0641\u0629:</b> {len(plans)} \u0628\u0627\u0642\u0629\n\n"
                "\u0627\u062e\u062a\u0631 \u0627\u0644\u0625\u062c\u0631\u0627\u0621 \u0627\u0644\u0645\u0637\u0644\u0648\u0628:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f4e6 \u0625\u062f\u0627\u0631\u0629 \u0628\u0627\u0642\u0627\u062a \u0647\u0630\u0647 \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plans_country:{country_id}")],
                [InlineKeyboardButton("\u270f\ufe0f \u062a\u0639\u062f\u064a\u0644 \u0627\u0633\u0645 \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_country_edit:{country_id}")],
                [InlineKeyboardButton("\U0001f5d1\ufe0f \u062d\u0630\u0641 \u0627\u0644\u062f\u0648\u0644\u0629 \u0628\u0627\u0644\u0643\u0627\u0645\u0644", callback_data=f"adm_country_del:{country_id}")],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062f\u0648\u0644", callback_data="adm_countries")],
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
                f"\u270f\ufe0f <b>\u062a\u0639\u062f\u064a\u0644 \u0627\u0633\u0645 \u0627\u0644\u062f\u0648\u0644\u0629: {html.escape(country['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u062c\u062f\u064a\u062f \u0644\u0644\u062f\u0648\u0644\u0629 \u0627\u0644\u0622\u0646:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data=f"adm_country_view:{country_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_country_del:"):
            country_id = int(data.split(":")[1])
            db.delete_country(country_id)
            await q.answer("\U0001f5d1\ufe0f \u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u062f\u0648\u0644\u0629 \u0648\u0628\u0627\u0642\u0627\u062a\u0647\u0627 \u0628\u0646\u062c\u0627\u062d!", show_alert=True)
            # Return to countries list
            countries = db.get_countries()
            rows = [[InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    InlineKeyboardButton(
                        f"\U0001f30d {c['name']} (\u0627\u0644\u0628\u0627\u0642\u0627\u062a: {c['plans_count']} | \u0627\u0644\u0645\u062e\u0632\u0648\u0646: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            await safe_edit_or_send(update, "\u2705 <b>\u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u062f\u0648\u0644\u0629 \u0628\u0646\u062c\u0627\u062d.</b>\n\n\U0001f30d <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u062f\u0648\u0644:</b>", InlineKeyboardMarkup(rows))
            return

        # ---------------- ADMIN: PLANS ----------------
        if data == "adm_plans_SelectCountry":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            if not countries:
                await safe_edit_or_send(
                    update,
                    "\u26a0\ufe0f <b>\u0644\u0627 \u062a\u0648\u062c\u062f \u062f\u0648\u0644 \u0645\u0636\u0627\u0641\u0629 \u0628\u0639\u062f. \u0642\u0645 \u0628\u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u0623\u0648\u0644\u0627\u064b \u062b\u0645 \u0623\u0636\u0641 \u0628\u0627\u0642\u0627\u062a\u0647\u0627.</b>",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data="adm_country_add")],
                        [InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")],
                    ]),
                )
                return
            rows = [
                [InlineKeyboardButton(f"\U0001f30d {c['name']} ({c['plans_count']} \u0628\u0627\u0642\u0629)", callback_data=f"adm_plans_country:{c['id']}")]
                for c in countries
            ]
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "\U0001f4e6 <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0628\u0627\u0642\u0627\u062a</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u0627\u062e\u062a\u0631 \u0627\u0644\u062f\u0648\u0644\u0629 \u0644\u0625\u062f\u0627\u0631\u0629 \u0628\u0627\u0642\u0627\u062a\u0647\u0627:",
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
            rows = [[InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u062c\u062f\u064a\u062f\u0629 \u0644\u0647\u0630\u0647 \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plan_add:{country_id}")]]
            for p in plans:
                rows.append([
                    InlineKeyboardButton(
                        f"\U0001f4e6 {p['name']} \u2014 {float(p['price']):.2f} {currency} (\u0627\u0644\u0645\u062a\u0627\u062d: {int(p['available_count'] or 0)})",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0627\u062e\u062a\u064a\u0627\u0631 \u062f\u0648\u0644\u0629 \u0623\u062e\u0631\u0649", callback_data="adm_plans_SelectCountry")])
            rows.append([InlineKeyboardButton("\U0001f6e0 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            text = (
                f"\U0001f4e6 <b>\u0628\u0627\u0642\u0627\u062a \u062f\u0648\u0644\u0629: {html.escape(country['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0623\u064a \u0628\u0627\u0642\u0629 \u0644\u062a\u0639\u062f\u064a\u0644\u0647\u0627 \u0623\u0648 \u062d\u0630\u0641\u0647\u0627 \u0623\u0648 \u0625\u062f\u0627\u0631\u0629 \u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0640 eSIM \u0627\u0644\u062e\u0627\u0635\u0629 \u0628\u0647\u0627:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_plan_add:"):
            await q.answer()
            country_id = int(data.split(":")[1])
            context.user_data["state"] = "ADM_ADD_PLAN_NAME"
            context.user_data["temp"] = {"country_id": country_id}
            text = (
                "\u2795 <b>\u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u062c\u062f\u064a\u062f\u0629 (\u0627\u0644\u062e\u0637\u0648\u0629 1 \u0645\u0646 3)</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0627\u0633\u0645 \u0627\u0644\u0628\u0627\u0642\u0629</b> (\u0645\u062b\u0627\u0644: <code>\u0628\u0627\u0642\u0629 10 \u062c\u064a\u062c\u0627\u0628\u0627\u064a\u062a - 30 \u064a\u0648\u0645</code>):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data=f"adm_plans_country:{country_id}")]])
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
                f"\U0001f4e6 <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0628\u0627\u0642\u0629: {html.escape(plan['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(plan['country_name'])}\n"
                f"\U0001f4b5 <b>\u0627\u0644\u0633\u0639\u0631:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
                f"\U0001f4dd <b>\u0627\u0644\u0648\u0635\u0641:</b> {html.escape(plan['description'] or NO_DESC)}\n"
                f"\U0001f4f2 <b>\u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u062a\u0627\u062d\u0629 \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646:</b> {int(plan['available_count'] or 0)} \u0634\u0631\u064a\u062d\u0629"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u0627\u0626\u062d eSIM (LPA \u0623\u0648 QR)", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("\U0001f4cb \u0639\u0631\u0636 \u0648\u062a\u0639\u062f\u064a\u0644/\u062d\u0630\u0641 \u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646", callback_data=f"adm_esim_list:{plan_id}")],
                [InlineKeyboardButton("\u270f\ufe0f \u062a\u0639\u062f\u064a\u0644 \u0628\u064a\u0627\u0646\u0627\u062a \u0648\u0633\u0639\u0631 \u0627\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_edit:{plan_id}")],
                [InlineKeyboardButton("\U0001f5d1\ufe0f \u062d\u0630\u0641 \u0627\u0644\u0628\u0627\u0642\u0629 \u0646\u0647\u0627\u0626\u064a\u0627\u064b", callback_data=f"adm_plan_del:{plan_id}")],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0628\u0627\u0642\u0627\u062a \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plans_country:{plan['country_id']}")],
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
                f"\u270f\ufe0f <b>\u062a\u0639\u062f\u064a\u0644 \u0627\u0644\u0628\u0627\u0642\u0629: {html.escape(plan['name'])}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 <b>\u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u062c\u062f\u064a\u062f \u0644\u0644\u0628\u0627\u0642\u0629</b> (\u0623\u0648 \u0623\u0631\u0633\u0644 \u0646\u0642\u0637\u0629 <code>.</code> \u0644\u0644\u0625\u0628\u0642\u0627\u0621 \u0639\u0644\u0649 \u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u062d\u0627\u0644\u064a):"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data=f"adm_plan_view:{plan_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_del:"):
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            country_id = plan["country_id"] if plan else 0
            db.delete_plan(plan_id)
            await q.answer("\U0001f5d1\ufe0f \u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u0628\u0627\u0642\u0629 \u0628\u0646\u062c\u0627\u062d!", show_alert=True)
            if country_id:
                await safe_edit_or_send(
                    update,
                    "\u2705 <b>\u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u0628\u0627\u0642\u0629 \u0628\u0646\u062c\u0627\u062d.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0628\u0627\u0642\u0627\u062a \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plans_country:{country_id}")]]),
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
                    "\u26a0\ufe0f <b>\u0644\u0627 \u062a\u0648\u062c\u062f \u062f\u0648\u0644 \u0645\u0636\u0627\u0641\u0629 \u0628\u0639\u062f. \u0642\u0645 \u0628\u0625\u0636\u0627\u0641\u0629 \u062f\u0648\u0644\u0629 \u0648\u0628\u0627\u0642\u0629 \u0623\u0648\u0644\u0627\u064b.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")]]),
                )
                return
            rows = [
                [
                    InlineKeyboardButton(
                        f"\U0001f30d {c['name']} (\u0627\u0644\u0645\u062a\u0627\u062d \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_inv_country:{c['id']}",
                    )
                ]
                for c in countries
            ]
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "\U0001f4f2 <b>\u0625\u062f\u0627\u0631\u0629 \u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u0634\u0631\u0627\u0626\u062d (eSIMs)</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u0627\u062e\u062a\u0631 \u0627\u0644\u062f\u0648\u0644\u0629 \u0644\u0639\u0631\u0636 \u0628\u0627\u0642\u0627\u062a\u0647\u0627 \u0648\u0625\u062f\u0627\u0631\u0629 \u0634\u0631\u0627\u0626\u062d\u0647\u0627:",
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
                    f"\u26a0\ufe0f <b>\u0644\u0627 \u062a\u0648\u062c\u062f \u0628\u0627\u0642\u0627\u062a \u0641\u064a \u062f\u0648\u0644\u0629 {html.escape(country['name'] if country else '')}. \u0623\u0636\u0641 \u0628\u0627\u0642\u0629 \u0623\u0648\u0644\u0627\u064b.</b>",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data=f"adm_plan_add:{country_id}")],
                        [InlineKeyboardButton("\U0001f519 \u0631\u062c\u0648\u0639", callback_data="adm_inv_SelectCountry")],
                    ]),
                )
                return
            rows = []
            for p in plans:
                rows.append([
                    InlineKeyboardButton(
                        f"\U0001f4e6 {p['name']} (\u0627\u0644\u0645\u062a\u0648\u0641\u0631: {int(p['available_count'] or 0)} \u0634\u0631\u064a\u062d\u0629)",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0627\u062e\u062a\u064a\u0627\u0631 \u062f\u0648\u0644\u0629 \u0623\u062e\u0631\u0649", callback_data="adm_inv_SelectCountry")])
            await safe_edit_or_send(
                update,
                f"\U0001f4f2 <b>\u0627\u062e\u062a\u0631 \u0627\u0644\u0628\u0627\u0642\u0629 \u0644\u0625\u062f\u0627\u0631\u0629 \u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u0634\u0631\u0627\u0626\u062d ({html.escape(country['name'])})</b>:",
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
                f"\u2795 <b>\u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u0627\u0626\u062d eSIM \u062c\u062f\u064a\u062f\u0629 \u0625\u0644\u0649 \u0627\u0644\u0645\u062e\u0632\u0648\u0646</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(plan['country_name'])}\n"
                f"\U0001f4e6 <b>\u0627\u0644\u0628\u0627\u0642\u0629:</b> {html.escape(plan['name'])}\n\n"
                "\U0001f4e5 <b>\u0643\u064a\u0641 \u064a\u0645\u0643\u0646\u0643 \u0627\u0644\u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0622\u0646\u061f</b>\n"
                "1\ufe0f\u20e3 <b>\u0625\u0631\u0633\u0627\u0644 \u0643\u0648\u062f LPA \u0646\u0635\u064a:</b> \u0623\u0631\u0633\u0644 \u0643\u0648\u062f \u0627\u0644\u0640 LPA \u0645\u0628\u0627\u0634\u0631\u0629 (\u0645\u062b\u0627\u0644: <code>LPA:1$smdp.io$XXXX</code>). \u0643\u0645\u0627 \u064a\u0645\u0643\u0646\u0643 \u0625\u0631\u0633\u0627\u0644 \u0639\u062f\u0629 \u0623\u0643\u0648\u0627\u062f \u0641\u064a \u0631\u0633\u0627\u0644\u0629 \u0648\u0627\u062d\u062f\u0629 (\u0643\u0644 \u0643\u0648\u062f \u0641\u064a \u0633\u0637\u0631 \u0645\u0633\u062a\u0642\u0644) \u0644\u0625\u0636\u0627\u0641\u062a\u0647\u0627 \u062f\u0641\u0639\u0629 \u0648\u0627\u062d\u062f\u0629!\n"
                "2\ufe0f\u20e3 <b>\u0623\u0648 \u0625\u0631\u0633\u0627\u0644 \u0635\u0648\u0631\u0629 QR Code:</b> \u0623\u0631\u0633\u0644 \u0635\u0648\u0631\u0629 \u0643\u0648\u062f \u0627\u0644\u0640 QR \u0645\u0628\u0627\u0634\u0631\u0629 (\u0648\u064a\u0645\u0643\u0646\u0643 \u0643\u062a\u0627\u0628\u0629 \u0643\u0648\u062f \u0627\u0644\u0640 LPA \u0623\u0648 \u0645\u0644\u0627\u062d\u0638\u0627\u062a \u0641\u064a \u0648\u0635\u0641 \u0627\u0644\u0635\u0648\u0631\u0629 Caption).\n\n"
                "\U0001f514 <i>\u0645\u0644\u0627\u062d\u0638\u0629: \u0628\u0645\u062c\u0631\u062f \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0634\u0631\u064a\u062d\u0629\u060c \u0633\u064a\u0642\u0648\u0645 \u0627\u0644\u0628\u0648\u062a \u062a\u0644\u0642\u0627\u0626\u064a\u0627\u064b \u0628\u0625\u0631\u0633\u0627\u0644 \u0625\u0634\u0639\u0627\u0631 \u0639\u0627\u0645 \u0644\u062c\u0645\u064a\u0639 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646 \u0628\u062a\u0648\u0641\u0631 \u0645\u062e\u0632\u0648\u0646 \u062c\u062f\u064a\u062f!</i>"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621 \u0648\u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_view:{plan_id}")]])
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
                    f"\U0001f4cb <b>\u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u0628\u0627\u0642\u0629: {html.escape(plan['name'])}</b>\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    "\u26a0\ufe0f \u0644\u0627 \u062a\u0648\u062c\u062f \u0634\u0631\u0627\u0626\u062d \u0645\u062a\u0627\u062d\u0629 \u062d\u0627\u0644\u064a\u0627\u064b \u0641\u064a \u0645\u062e\u0632\u0648\u0646 \u0647\u0630\u0647 \u0627\u0644\u0628\u0627\u0642\u0629."
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u064a\u062d\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data=f"adm_esim_add:{plan_id}")],
                    [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_view:{plan_id}")],
                ])
                await safe_edit_or_send(update, text, kb)
                return

            rows = []
            for e in esims[:30]:
                t_badge = "\U0001f511 LPA" if e["item_type"] == "lpa" else "\U0001f5bc QR"
                short_code = (e["lpa_code"][:22] + "...") if e["lpa_code"] and len(e["lpa_code"]) > 22 else (e["lpa_code"] or "\u0635\u0648\u0631\u0629 QR")
                rows.append([
                    InlineKeyboardButton(
                        f"#{e['id']} | {t_badge} | {short_code}",
                        callback_data=f"adm_esim_item:{e['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u064a\u062d\u0629 \u062c\u062f\u064a\u062f\u0629", callback_data=f"adm_esim_add:{plan_id}")])
            rows.append([InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_view:{plan_id}")])
            text = (
                f"\U0001f4cb <b>\u0627\u0644\u0634\u0631\u0627\u0626\u062d \u0627\u0644\u0645\u062a\u0627\u062d\u0629 \u0641\u064a \u0645\u062e\u0632\u0648\u0646: {html.escape(plan['name'])}</b>\n"
                f"\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u062a\u0627\u062d: <b>{len(esims)}</b> \u0634\u0631\u064a\u062d\u0629.\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\U0001f447 \u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0623\u064a \u0634\u0631\u064a\u062d\u0629 \u0644\u0645\u0639\u0627\u064a\u0646\u062a\u0647\u0627 \u0623\u0648 \u062a\u0639\u062f\u064a\u0644\u0647\u0627 \u0623\u0648 \u062d\u0630\u0641\u0647\u0627 \u0645\u0646 \u0627\u0644\u0645\u062e\u0632\u0648\u0646:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_esim_item:"):
            await q.answer()
            clear_state(context)
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("\u26a0\ufe0f \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f\u0629.", show_alert=True)
                return
            t_label = "\u0643\u0648\u062f \u0646\u0635\u064a (LPA)" if esim["item_type"] == "lpa" else "\u0635\u0648\u0631\u0629 \u0643\u0648\u062f (QR Code)"
            text = (
                f"\U0001f50e <b>\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646 #{esim['id']}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f30d <b>\u0627\u0644\u062f\u0648\u0644\u0629:</b> {html.escape(esim['country_name'])}\n"
                f"\U0001f4e6 <b>\u0627\u0644\u0628\u0627\u0642\u0629:</b> {html.escape(esim['plan_name'])}\n"
                f"\U0001f3f7 <b>\u0627\u0644\u0646\u0648\u0639:</b> {t_label}\n"
                f"\U0001f511 <b>\u0643\u0648\u062f LPA:</b> <code>{html.escape(esim['lpa_code'] or NO_LPA_QR)}</code>\n"
                f"\U0001f4dd <b>\u0645\u0644\u0627\u062d\u0638\u0627\u062a:</b> {html.escape(esim['notes'] or NONE_STR)}\n"
                f"\U0001f4c5 <b>\u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0625\u0636\u0627\u0641\u0629:</b> {esim['created_at']}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f441 \u0645\u0639\u0627\u064a\u0646\u0629 \u0643\u0648\u062f \u0627\u0644\u0640 QR \u0643\u0645\u0627 \u064a\u0638\u0647\u0631 \u0644\u0644\u0639\u0645\u064a\u0644", callback_data=f"adm_esim_preview:{esim['id']}")],
                [InlineKeyboardButton("\u270f\ufe0f \u062a\u0639\u062f\u064a\u0644 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 (LPA \u0623\u0648 QR)", callback_data=f"adm_esim_edit:{esim['id']}")],
                [InlineKeyboardButton("\U0001f5d1\ufe0f \u062d\u0630\u0641 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0645\u0646 \u0627\u0644\u0645\u062e\u0632\u0648\u0646", callback_data=f"adm_esim_del:{esim['id']}")],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0634\u0631\u0627\u0626\u062d", callback_data=f"adm_esim_list:{esim['plan_id']}")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_preview:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("\u26a0\ufe0f \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f\u0629.", show_alert=True)
                return
            await q.answer("\U0001f441 \u062c\u0627\u0631\u064a \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0639\u0627\u064a\u0646\u0629...")
            await deliver_esim_to_chat(
                context=context,
                chat_id=user.id,
                esim=esim,
                header_title=f"\U0001f441 <b>\u0645\u0639\u0627\u064a\u0646\u0629 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0628\u0627\u0644\u0645\u062e\u0632\u0648\u0646 (#{esim['id']})</b>",
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
                f"\u270f\ufe0f <b>\u062a\u0639\u062f\u064a\u0644 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0631\u0642\u0645 #{esim_id}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0643\u0648\u062f \u0627\u0644\u0640 LPA \u0627\u0644\u062c\u062f\u064a\u062f</b> \u0643\u0646\u0635\u060c \u0623\u0648 \u0623\u0631\u0633\u0644 <b>\u0635\u0648\u0631\u0629 QR Code \u062c\u062f\u064a\u062f\u0629</b> \u0644\u062a\u062d\u062f\u064a\u062b \u0647\u0630\u0647 \u0627\u0644\u0634\u0631\u064a\u062d\u0629:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data=f"adm_esim_item:{esim_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_del:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            plan_id = esim["plan_id"] if esim else 0
            db.delete_esim(esim_id)
            await q.answer("\U0001f5d1\ufe0f \u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0645\u0646 \u0627\u0644\u0645\u062e\u0632\u0648\u0646!", show_alert=True)
            if plan_id:
                await safe_edit_or_send(
                    update,
                    "\u2705 <b>\u062a\u0645 \u062d\u0630\u0641 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0645\u0646 \u0627\u0644\u0645\u062e\u0632\u0648\u0646 \u0628\u0646\u062c\u0627\u062d.</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0634\u0631\u0627\u0626\u062d", callback_data=f"adm_esim_list:{plan_id}")]]),
                )
            return

        # ---------------- ADMIN: TOP-UPS & BALANCE ----------------
        if data == "adm_topups_menu":
            await q.answer()
            clear_state(context)
            pending = db.get_pending_topups()
            rows = [
                [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 / \u062e\u0635\u0645 \u0631\u0635\u064a\u062f \u0644\u0645\u0633\u062a\u062e\u062f\u0645 \u064a\u062f\u0648\u064a\u0627\u064b", callback_data="adm_bal_manual")],
            ]
            for req in pending[:20]:
                m_icon = "\U0001f7e1 Binance" if req["method"] == "binance" else "\U0001f7e3 InstaPay"
                rows.append([
                    InlineKeyboardButton(
                        f"\u23f3 \u0637\u0644\u0628 #{req['id']} | {m_icon} | {float(req['amount']):.2f} {currency}",
                        callback_data=f"adm_topup_view:{req['id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            text = (
                "\U0001f4b3 <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0623\u0631\u0635\u062f\u0629 \u0648\u0637\u0644\u0628\u0627\u062a \u0627\u0644\u0634\u062d\u0646</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\u23f3 <b>\u0637\u0644\u0628\u0627\u062a \u0627\u0644\u0634\u062d\u0646 \u0627\u0644\u0645\u0639\u0644\u0642\u0629 \u062d\u0627\u0644\u064a\u0627\u064b:</b> {len(pending)} \u0637\u0644\u0628\n\n"
                "\u064a\u0645\u0643\u0646\u0643 \u062a\u0639\u062f\u064a\u0644 \u0631\u0635\u064a\u062f \u0623\u064a \u0645\u0633\u062a\u062e\u062f\u0645 \u064a\u062f\u0648\u064a\u0627\u064b \u0623\u0648 \u0645\u0631\u0627\u062c\u0639\u0629 \u0637\u0644\u0628\u0627\u062a \u0627\u0644\u0634\u062d\u0646 \u0628\u0627\u0644\u0623\u0633\u0641\u0644:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_topup_view:"):
            await q.answer()
            req_id = int(data.split(":")[1])
            req = db.get_topup_request(req_id)
            if not req:
                return
            m_name = "Binance Pay (\u0639\u0645\u0644\u0627\u062a \u0631\u0642\u0645\u064a\u0629)" if req["method"] == "binance" else "InstaPay EG (\u0625\u0646\u0633\u062a\u0627 \u0628\u0627\u064a)"
            text = (
                f"\U0001f9fe <b>\u062a\u0641\u0627\u0635\u064a\u0644 \u0637\u0644\u0628 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f #{req['id']}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f464 <b>\u0627\u0644\u0639\u0645\u064a\u0644:</b> {html.escape(req['full_name'] or '')} (@{html.escape(req['username'] or NO_USER)})\n"
                f"\U0001f194 <b>\u0645\u0639\u0631\u0641 \u0627\u0644\u0639\u0645\u064a\u0644:</b> <code>{req['user_id']}</code>\n"
                f"\U0001f4b3 <b>\u0627\u0644\u0648\u0633\u064a\u0644\u0629:</b> {m_name}\n"
                f"\U0001f4b5 <b>\u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u0637\u0644\u0648\u0628:</b> <code>{float(req['amount']):.2f}</code> {html.escape(currency)}\n"
                f"\U0001f4dd <b>\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u062a\u062d\u0648\u064a\u0644 \u0627\u0644\u0645\u0631\u0633\u0644\u0629:</b> {html.escape(req['proof_text'] or ATTACHED_RECEIPT)}\n"
                f"\U0001f4c5 <b>\u0627\u0644\u062a\u0627\u0631\u064a\u062e:</b> {req['created_at']}"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\u2705 \u0642\u0628\u0648\u0644 \u0648\u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0631\u0635\u064a\u062f", callback_data=f"adm_topup_ok:{req['id']}"),
                    InlineKeyboardButton("\u274c \u0631\u0641\u0636 \u0627\u0644\u0637\u0644\u0628", callback_data=f"adm_topup_no:{req['id']}"),
                ],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0637\u0644\u0628\u0627\u062a", callback_data="adm_topups_menu")],
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
                await q.answer("\u26a0\ufe0f \u062a\u0645 \u0645\u0639\u0627\u0644\u062c\u0629 \u0647\u0630\u0627 \u0627\u0644\u0637\u0644\u0628 \u0645\u0633\u0628\u0642\u0627\u064b \u0623\u0648 \u0623\u0646\u0647 \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f.", show_alert=True)
                return

            if approve:
                await q.answer("\u2705 \u062a\u0645 \u0642\u0628\u0648\u0644 \u0627\u0644\u0637\u0644\u0628 \u0648\u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0631\u0635\u064a\u062f \u0644\u0644\u0639\u0645\u064a\u0644!", show_alert=True)
                user_msg = (
                    "\U0001f389 <b>\u062a\u0645 \u0642\u0628\u0648\u0644 \u0637\u0644\u0628 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u062e\u0627\u0635 \u0628\u0643!</b>\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\u2795 <b>\u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u0636\u0627\u0641:</b> <code>{float(res['amount']):.2f}</code> {html.escape(currency)}\n"
                    f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0643 \u0627\u0644\u062d\u0627\u0644\u064a:</b> <code>{float(res['new_balance']):.2f}</code> {html.escape(currency)}\n\n"
                    "\u0634\u0643\u0631\u0627\u064b \u0644\u062b\u0642\u062a\u0643 \u0628\u0646\u0627! \u064a\u0645\u0643\u0646\u0643 \u0627\u0644\u0622\u0646 \u0634\u0631\u0627\u0621 \u0634\u0631\u064a\u062d\u062a\u0643 \u0641\u0648\u0631\u0627\u064b."
                )
            else:
                await q.answer("\u274c \u062a\u0645 \u0631\u0641\u0636 \u0637\u0644\u0628 \u0627\u0644\u0634\u062d\u0646.", show_alert=True)
                user_msg = (
                    "\u274c <b>\u0646\u0639\u062a\u0630\u0631\u060c \u062a\u0645 \u0631\u0641\u0636 \u0637\u0644\u0628 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0631\u0642\u0645 #{req_id}.</b>\n"
                    f"\u0627\u0644\u0645\u0628\u0644\u063a: {float(res['amount']):.2f} {html.escape(currency)}\n"
                    "\u064a\u0631\u062c\u0649 \u0627\u0644\u062a\u0623\u0643\u062f \u0645\u0646 \u0625\u064a\u0635\u0627\u0644 \u0627\u0644\u062a\u062d\u0648\u064a\u0644 \u0623\u0648 \u0627\u0644\u062a\u0648\u0627\u0635\u0644 \u0645\u0639 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a."
                ).replace("{req_id}", str(req_id))

            try:
                await context.bot.send_message(
                    chat_id=res["user_id"],
                    text=user_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")]]),
                )
            except TelegramError:
                pass

            await show_admin_home(update, context)
            return

        if data == "adm_bal_manual":
            await q.answer()
            context.user_data["state"] = "ADM_MANUAL_BAL_USER"
            text = (
                "\U0001f4b0 <b>\u0625\u0636\u0627\u0641\u0629 \u0623\u0648 \u062e\u0635\u0645 \u0631\u0635\u064a\u062f \u0644\u0645\u0633\u062a\u062e\u062f\u0645</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 (User ID)</b> \u0627\u0644\u0630\u064a \u062a\u0631\u064a\u062f \u062a\u0639\u062f\u064a\u0644 \u0631\u0635\u064a\u062f\u0647:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_topups_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        # ---------------- ADMIN: USERS & BAN MANAGEMENT ----------------
        if data == "adm_users_menu":
            await q.answer()
            clear_state(context)
            stats = db.get_stats()
            banned_list = db.get_banned_users()
            rows = [
                [InlineKeyboardButton("\U0001f6ab \u062d\u0638\u0631 \u0645\u0633\u062a\u062e\u062f\u0645 \u0628\u0648\u0627\u0633\u0637\u0629 \u0627\u0644\u0640 ID", callback_data="adm_ban_input")],
                [InlineKeyboardButton("\u2705 \u0641\u0643 \u062d\u0638\u0631 \u0645\u0633\u062a\u062e\u062f\u0645 \u0628\u0648\u0627\u0633\u0637\u0629 \u0627\u0644\u0640 ID", callback_data="adm_unban_input")],
            ]
            for b in banned_list[:15]:
                rows.append([
                    InlineKeyboardButton(
                        f"\U0001f513 \u0641\u0643 \u062d\u0638\u0631: {b['full_name'] or b['user_id']} ({b['user_id']})",
                        callback_data=f"adm_unban_direct:{b['user_id']}",
                    )
                ])
            rows.append([InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")])
            text = (
                "\U0001f465 <b>\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646 \u0648\u0627\u0644\u062d\u0638\u0631</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f464 <b>\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646:</b> {stats['total_users']}\n"
                f"\U0001f6ab <b>\u0639\u062f\u062f \u0627\u0644\u0645\u062d\u0638\u0648\u0631\u064a\u0646 \u062d\u0627\u0644\u064a\u0627\u064b:</b> {stats['banned_users']}\n\n"
                "\u064a\u0645\u0643\u0646\u0643 \u062d\u0638\u0631 \u0623\u0648 \u0641\u0643 \u062d\u0638\u0631 \u0623\u064a \u0645\u0633\u062a\u062e\u062f\u0645 \u0639\u0628\u0631 \u0627\u0644\u0640 ID \u0623\u0648 \u0627\u0644\u0636\u063a\u0637 \u0639\u0644\u0649 \u0627\u0633\u0645 \u0627\u0644\u0645\u062d\u0638\u0648\u0631 \u0628\u0627\u0644\u0623\u0633\u0641\u0644 \u0644\u0641\u0643 \u062d\u0638\u0631\u0647 \u0641\u0648\u0631\u0627\u064b:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_ban_input":
            await q.answer()
            context.user_data["state"] = "ADM_BAN_USER"
            text = (
                "\U0001f6ab <b>\u062d\u0638\u0631 \u0645\u0633\u062a\u062e\u062f\u0645 \u0645\u0646 \u0627\u0644\u0628\u0648\u062a</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 (User ID)</b> \u0627\u0644\u0630\u064a \u062a\u0631\u064a\u062f \u062d\u0638\u0631\u0647 \u0646\u0647\u0627\u0626\u064a\u0627\u064b \u0645\u0646 \u0627\u0633\u062a\u062e\u062f\u0627\u0645 \u0627\u0644\u0628\u0648\u062a:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data == "adm_unban_input":
            await q.answer()
            context.user_data["state"] = "ADM_UNBAN_USER"
            text = (
                "\u2705 <b>\u0641\u0643 \u062d\u0638\u0631 \u0645\u0633\u062a\u062e\u062f\u0645</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 (User ID)</b> \u0644\u0625\u0644\u063a\u0627\u0621 \u062d\u0638\u0631\u0647:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_unban_direct:"):
            target_id = int(data.split(":")[1])
            db.set_user_ban(target_id, False)
            await q.answer(f"\u2705 \u062a\u0645 \u0641\u0643 \u0627\u0644\u062d\u0638\u0631 \u0639\u0646 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 {target_id}", show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="\u2705 <b>\u062a\u0645 \u0631\u0641\u0639 \u0627\u0644\u062d\u0638\u0631 \u0639\u0646 \u062d\u0633\u0627\u0628\u0643! \u064a\u0645\u0643\u0646\u0643 \u0627\u0644\u0622\u0646 \u0627\u0633\u062a\u062e\u062f\u0627\u0645 \u0627\u0644\u0628\u0648\u062a \u0628\u0634\u0643\u0644 \u0637\u0628\u064a\u0639\u064a.</b>",
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
                "\U0001f4e2 <b>\u0625\u0631\u0633\u0627\u0644 \u0631\u0633\u0627\u0644\u0629 \u062c\u0645\u0627\u0639\u064a\u0629 \u0639\u0627\u0645\u0629 \u0644\u062c\u0645\u064a\u0639 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0627\u0644\u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u0646\u0635\u064a\u0629</b> \u0623\u0648 <b>\u0627\u0644\u0635\u0648\u0631\u0629 \u0645\u0639 \u0627\u0644\u0648\u0635\u0641</b> \u0627\u0644\u062a\u064a \u062a\u0631\u064a\u062f \u0625\u0631\u0633\u0627\u0644\u0647\u0627 \u0644\u062c\u0645\u064a\u0639 \u0645\u0633\u062a\u062e\u062f\u0645\u064a \u0627\u0644\u0628\u0648\u062a:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_home")]])
            await safe_edit_or_send(update, text, kb)
            return

        # ---------------- ADMIN: SETTINGS (BINANCE / INSTAPAY / SUPPORT) ----------------
        if data == "adm_settings":
            await q.answer()
            clear_state(context)
            binance_id = db.get_setting("binance_id", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f")
            instapay = db.get_setting("instapay_address", "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f")
            support = db.get_setting("support_username", "Support")
            curr = db.get_setting("currency", "USD")

            text = (
                "\u2699\ufe0f <b>\u0625\u0639\u062f\u0627\u062f\u0627\u062a \u0627\u0644\u062f\u0641\u0639 \u0648\u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f7e1 <b>\u0645\u0639\u0631\u0641 Binance ID:</b> <code>{html.escape(binance_id)}</code>\n"
                f"\U0001f7e3 <b>\u0639\u0646\u0648\u0627\u0646 InstaPay EG:</b> <code>{html.escape(instapay)}</code>\n"
                f"\U0001f6df <b>\u064a\u0648\u0632\u0631 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a:</b> @{html.escape(support.lstrip('@'))}\n"
                f"\U0001f4b5 <b>\u0639\u0645\u0644\u0629 \u0627\u0644\u0645\u062a\u062c\u0631:</b> <code>{html.escape(curr)}</code>\n\n"
                "\U0001f447 \u0627\u0636\u063a\u0637 \u0639\u0644\u0649 \u0623\u064a \u0625\u0639\u062f\u0627\u062f \u0644\u062a\u0639\u062f\u064a\u0644\u0647 \u0645\u0628\u0627\u0634\u0631\u0629:"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f7e1 \u062a\u0639\u062f\u064a\u0644 \u0645\u0639\u0631\u0641 Binance ID", callback_data="adm_set_key:binance_id")],
                [InlineKeyboardButton("\U0001f7e3 \u062a\u0639\u062f\u064a\u0644 \u0639\u0646\u0648\u0627\u0646 InstaPay EG", callback_data="adm_set_key:instapay_address")],
                [InlineKeyboardButton("\U0001f6df \u062a\u0639\u062f\u064a\u0644 \u064a\u0648\u0632\u0631 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a", callback_data="adm_set_key:support_username")],
                [InlineKeyboardButton("\U0001f4b5 \u062a\u0639\u062f\u064a\u0644 \u0631\u0645\u0632 \u0627\u0644\u0639\u0645\u0644\u0629 (USD / EGP)", callback_data="adm_set_key:currency")],
                [InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_set_key:"):
            await q.answer()
            key = data.split(":")[1]
            labels = {
                "binance_id": "\u0645\u0639\u0631\u0641 Binance ID \u0627\u0644\u062c\u062f\u064a\u062f",
                "instapay_address": "\u0639\u0646\u0648\u0627\u0646 \u0623\u0648 \u0631\u0642\u0645 InstaPay EG \u0627\u0644\u062c\u062f\u064a\u062f",
                "support_username": "\u064a\u0648\u0632\u0631 \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a \u0641\u064a \u062a\u064a\u0644\u064a\u062c\u0631\u0627\u0645 (\u0628\u062f\u0648\u0646 @)",
                "currency": "\u0631\u0645\u0632 \u0627\u0644\u0639\u0645\u0644\u0629 (\u0645\u062b\u0627\u0644: USD \u0623\u0648 \u062c.\u0645)",
            }
            context.user_data["state"] = "ADM_SET_SETTING"
            context.user_data["temp"] = {"key": key}
            text = (
                f"\u2699\ufe0f <b>\u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0625\u0639\u062f\u0627\u062f: {labels.get(key, key)}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\u0623\u0631\u0633\u0644 \u0627\u0644\u0642\u064a\u0645\u0629 \u0627\u0644\u062c\u062f\u064a\u062f\u0629 \u0627\u0644\u0622\u0646:"
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0625\u0644\u063a\u0627\u0621", callback_data="adm_settings")]])
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0642\u064a\u0645\u0629 \u0627\u0644\u0645\u0628\u0644\u063a \u0643\u0623\u0631\u0642\u0627\u0645 \u0641\u0642\u0637 (\u0645\u062b\u0627\u0644: 15).")
            return
        try:
            amount = round(float(msg.text.strip().replace(",", ".")), 2)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await msg.reply_text("\u26a0\ufe0f \u0642\u064a\u0645\u0629 \u063a\u064a\u0631 \u0635\u062d\u064a\u062d\u0629! \u064a\u0631\u062c\u0649 \u0625\u062f\u062e\u0627\u0644 \u0631\u0642\u0645 \u0623\u0643\u0628\u0631 \u0645\u0646 \u0635\u0641\u0631 (\u0645\u062b\u0627\u0644: <code>10</code>).", parse_mode=ParseMode.HTML)
            return

        temp["amount"] = amount
        context.user_data["temp"] = temp
        context.user_data["state"] = "USER_TOPUP_PROOF"

        await msg.reply_text(
            f"\u2705 <b>\u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u062d\u062f\u062f \u0644\u0644\u0634\u062d\u0646:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n\n"
            "\U0001f4f8 <b>\u0627\u0644\u062e\u0637\u0648\u0629 \u0627\u0644\u0623\u062e\u064a\u0631\u0629:</b>\n"
            "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0635\u0648\u0631\u0629 \u0625\u064a\u0635\u0627\u0644 \u0627\u0644\u062a\u062d\u0648\u064a\u0644 (Screenshot)</b> \u0623\u0648 \u0623\u0631\u0633\u0644 <b>\u0631\u0642\u0645 \u0627\u0644\u0639\u0645\u0644\u064a\u0629 \u0627\u0644\u0645\u0631\u062c\u0639\u064a (Transaction ID / \u0627\u0633\u0645 \u0627\u0644\u0645\u062d\u0648\u0644)</b> \u0641\u064a \u0631\u0633\u0627\u0644\u0629:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f3e0 \u0625\u0644\u063a\u0627\u0621", callback_data="home")]]),
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0635\u0648\u0631\u0629 \u0625\u064a\u0635\u0627\u0644 \u0627\u0644\u062a\u062d\u0648\u064a\u0644 \u0623\u0648 \u0643\u062a\u0627\u0628\u0629 \u0631\u0642\u0645 \u0627\u0644\u0639\u0645\u0644\u064a\u0629 \u0627\u0644\u0645\u0631\u062c\u0639\u064a.")
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
            f"\u2705 <b>\u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0637\u0644\u0628 \u0634\u062d\u0646 \u0627\u0644\u0631\u0635\u064a\u062f \u0631\u0642\u0645 #{req_id} \u0628\u0646\u062c\u0627\u062d!</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4b5 <b>\u0627\u0644\u0645\u0628\u0644\u063a:</b> {amount:.2f} {html.escape(currency)}\n"
            "\u23f3 \u062c\u0627\u0631\u064a \u0645\u0631\u0627\u062c\u0639\u0629 \u0627\u0644\u062a\u062d\u0648\u064a\u0644 \u0645\u0646 \u0642\u0650\u0628\u0644 \u0627\u0644\u0625\u062f\u0627\u0631\u0629\u060c \u0648\u0633\u064a\u0635\u0644 \u0625\u0644\u064a\u0643 \u0625\u0634\u0639\u0627\u0631 \u0641\u0648\u0631 \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0631\u0635\u064a\u062f \u0644\u062d\u0633\u0627\u0628\u0643.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f3e0 \u0627\u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629", callback_data="home")]]),
        )

        # Notify Admin immediately
        if ADMIN_ID:
            m_label = "\U0001f7e1 Binance Pay" if method == "binance" else "\U0001f7e3 InstaPay EG"
            adm_caption = (
                f"\U0001f514 <b>\u0637\u0644\u0628 \u0634\u062d\u0646 \u0631\u0635\u064a\u062f \u062c\u062f\u064a\u062f #{req_id}</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f464 <b>\u0627\u0644\u0639\u0645\u064a\u0644:</b> {html.escape(user.full_name or '')} (@{html.escape(user.username or NO_USER)})\n"
                f"\U0001f194 <b>\u0645\u0639\u0631\u0641 \u0627\u0644\u0639\u0645\u064a\u0644:</b> <code>{user.id}</code>\n"
                f"\U0001f4b3 <b>\u0637\u0631\u064a\u0642\u0629 \u0627\u0644\u062f\u0641\u0639:</b> {m_label}\n"
                f"\U0001f4b5 <b>\u0627\u0644\u0645\u0628\u0644\u063a:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n"
                f"\U0001f4dd <b>\u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644/\u0627\u0644\u0645\u0631\u062c\u0639:</b> {html.escape(proof_text or ATTACHED_RECEIPT)}"
            )
            adm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\u2705 \u0642\u0628\u0648\u0644 \u0648\u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0631\u0635\u064a\u062f", callback_data=f"adm_topup_ok:{req_id}"),
                    InlineKeyboardButton("\u274c \u0631\u0641\u0636 \u0627\u0644\u0637\u0644\u0628", callback_data=f"adm_topup_no:{req_id}"),
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0627\u0633\u0645 \u0627\u0644\u062f\u0648\u0644\u0629 \u0643\u0646\u0635.")
            return
        cid = db.add_country(msg.text.strip())
        clear_state(context)
        if not cid:
            await msg.reply_text(
                "\u26a0\ufe0f \u0647\u0630\u0647 \u0627\u0644\u062f\u0648\u0644\u0629 \u0645\u0648\u062c\u0648\u062f\u0629 \u0628\u0627\u0644\u0641\u0639\u0644!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u062f\u0648\u0644", callback_data="adm_countries")]]),
            )
            return
        await msg.reply_text(
            f"\u2705 <b>\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u062f\u0648\u0644\u0629 ({html.escape(msg.text.strip())}) \u0628\u0646\u062c\u0627\u062d!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u0644\u0647\u0630\u0647 \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plan_add:{cid}")],
                [InlineKeyboardButton("\U0001f30d \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062f\u0648\u0644", callback_data="adm_countries")],
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
            "\u2705 <b>\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0627\u0633\u0645 \u0627\u0644\u062f\u0648\u0644\u0629 \u0628\u0646\u062c\u0627\u062d!</b>" if ok else "\u26a0\ufe0f \u062a\u0639\u0630\u0631 \u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0627\u0633\u0645.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_country_view:{country_id}")]]),
        )
        return

    if state == "ADM_ADD_PLAN_NAME":
        if not msg.text:
            return
        temp["name"] = msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_ADD_PLAN_DESC"
        await msg.reply_text(
            "\U0001f4dd <b>\u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u062c\u062f\u064a\u062f\u0629 (\u0627\u0644\u062e\u0637\u0648\u0629 2 \u0645\u0646 3)</b>\n"
            "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0648\u0635\u0641 \u0648\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0628\u0627\u0642\u0629</b> (\u0645\u062b\u0627\u0644: <code>\u0635\u0644\u0627\u062d\u064a\u0629 30 \u064a\u0648\u0645 - \u062a\u062f\u0639\u0645 \u0627\u0644\u062c\u064a\u0644 \u0627\u0644\u062e\u0627\u0645\u0633 5G - \u0642\u0627\u0628\u0644\u0629 \u0644\u0646\u0642\u0637\u0629 \u0627\u0644\u0627\u062a\u0635\u0627\u0644</code>) \u0623\u0648 \u0623\u0631\u0633\u0644 \u0646\u0642\u0637\u0629 <code>.</code> \u0644\u062a\u062e\u0637\u064a \u0627\u0644\u0648\u0635\u0641:",
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
            f"\U0001f4b5 <b>\u0625\u0636\u0627\u0641\u0629 \u0628\u0627\u0642\u0629 \u062c\u062f\u064a\u062f\u0629 (\u0627\u0644\u062e\u0637\u0648\u0629 3 \u0645\u0646 3)</b>\n"
            f"\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0633\u0639\u0631 \u0627\u0644\u0628\u0627\u0642\u0629 ({html.escape(currency)})</b> \u0643\u0623\u0631\u0642\u0627\u0645 \u0641\u0642\u0637 (\u0645\u062b\u0627\u0644: <code>12.5</code>):",
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u062f\u062e\u0627\u0644 \u0633\u0639\u0631 \u0631\u0642\u0645\u064a \u0635\u062d\u064a\u062d (\u0645\u062b\u0627\u0644: 10 \u0623\u0648 14.99).")
            return
        country_id = int(temp["country_id"])
        plan_id = db.add_plan(country_id, temp["name"], temp.get("description", ""), price)
        clear_state(context)
        await msg.reply_text(
            f"\u2705 <b>\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0628\u0627\u0642\u0629 ({html.escape(temp['name'])}) \u0628\u0633\u0639\u0631 {price:.2f} {html.escape(currency)} \u0628\u0646\u062c\u0627\u062d!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u0627\u0626\u062d eSIM \u0644\u0647\u0630\u0647 \u0627\u0644\u0628\u0627\u0642\u0629 \u0627\u0644\u0622\u0646", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0628\u0627\u0642\u0627\u062a \u0627\u0644\u062f\u0648\u0644\u0629", callback_data=f"adm_plans_country:{country_id}")],
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
            "\U0001f4dd \u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0627\u0644\u0648\u0635\u0641 \u0627\u0644\u062c\u062f\u064a\u062f \u0644\u0644\u0628\u0627\u0642\u0629</b> (\u0623\u0648 \u0623\u0631\u0633\u0644 \u0646\u0642\u0637\u0629 <code>.</code> \u0644\u0644\u0625\u0628\u0642\u0627\u0621 \u0639\u0644\u0649 \u0627\u0644\u0648\u0635\u0641 \u0627\u0644\u062d\u0627\u0644\u064a):",
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
            f"\U0001f4b5 \u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u062c\u062f\u064a\u062f \u0644\u0644\u0628\u0627\u0642\u0629 ({html.escape(currency)})</b> (\u0623\u0648 \u0623\u0631\u0633\u0644 \u0646\u0642\u0637\u0629 <code>.</code> \u0644\u0644\u0625\u0628\u0642\u0627\u0621 \u0639\u0644\u0649 \u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u062d\u0627\u0644\u064a {float(plan['price']):.2f}):",
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
                await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u062f\u062e\u0627\u0644 \u0633\u0639\u0631 \u0631\u0642\u0645\u064a \u0635\u062d\u064a\u062d.")
                return
        db.update_plan(plan_id, temp["name"], temp["description"], price)
        clear_state(context)
        await msg.reply_text(
            "\u2705 <b>\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0628\u0627\u0642\u0629 \u0628\u0646\u062c\u0627\u062d!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_view:{plan_id}")]]),
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0643\u0648\u062f LPA \u0646\u0635\u064a \u0623\u0648 \u0625\u0631\u0633\u0627\u0644 \u0635\u0648\u0631\u0629 QR Code.")
            return

        clear_state(context)
        status_msg = await msg.reply_text(
            f"\u2705 <b>\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 {added_count} \u0634\u0631\u064a\u062d\u0629 eSIM \u0625\u0644\u0649 \u0645\u062e\u0632\u0648\u0646 ({html.escape(plan['name'])}) \u0628\u0646\u062c\u0627\u062d!</b>\n"
            "\U0001f4e2 \u062c\u0627\u0631\u064a \u0625\u0631\u0633\u0627\u0644 \u0625\u0634\u0639\u0627\u0631 \u0639\u0627\u0645 \u0644\u062c\u0645\u064a\u0639 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646 \u0628\u062a\u0648\u0641\u0631 \u0627\u0644\u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u062c\u062f\u064a\u062f...",
            parse_mode=ParseMode.HTML,
        )
        sent, failed = await broadcast_new_inventory(context, plan_id=plan_id, added_count=added_count)
        await status_msg.edit_text(
            f"\u2705 <b>\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u0629 {added_count} \u0634\u0631\u064a\u062d\u0629 eSIM \u0625\u0644\u0649 \u0645\u062e\u0632\u0648\u0646 ({html.escape(plan['name'])}) \u0628\u0646\u062c\u0627\u062d!</b>\n"
            f"\U0001f4e2 <b>\u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0625\u0634\u0639\u0627\u0631 \u0627\u0644\u0639\u0627\u0645 \u0628\u0646\u062c\u0627\u062d \u0625\u0644\u0649:</b> {sent} \u0645\u0633\u062a\u062e\u062f\u0645 (\u0641\u0634\u0644/\u0645\u062d\u0638\u0648\u0631: {failed}).",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2795 \u0625\u0636\u0627\u0641\u0629 \u0634\u0631\u0627\u0626\u062d \u0623\u062e\u0631\u0649 \u0644\u0646\u0641\u0633 \u0627\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_esim_add:{plan_id}")],
                [InlineKeyboardButton("\U0001f4cb \u0639\u0631\u0636 \u0645\u062e\u0632\u0648\u0646 \u0627\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_esim_list:{plan_id}")],
                [InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0628\u0627\u0642\u0629", callback_data=f"adm_plan_view:{plan_id}")],
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0643\u0648\u062f LPA \u0646\u0635\u064a \u0623\u0648 \u0635\u0648\u0631\u0629 QR Code.")
            return

        clear_state(context)
        await msg.reply_text(
            f"\u2705 <b>\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0634\u0631\u064a\u062d\u0629 #{esim_id} \u0628\u0646\u062c\u0627\u062d!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f50e \u0639\u0631\u0636 \u0627\u0644\u0634\u0631\u064a\u062d\u0629 \u0628\u0639\u062f \u0627\u0644\u062a\u0639\u062f\u064a\u0644", callback_data=f"adm_esim_item:{esim_id}")],
                [InlineKeyboardButton("\U0001f4cb \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0634\u0631\u0627\u0626\u062d", callback_data=f"adm_esim_list:{plan_id}")],
            ]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: MANUAL BALANCE ADJUSTMENT
    # --------------------------------------------------------------------------
    if state == "ADM_MANUAL_BAL_USER":
        if not msg.text or not msg.text.strip().isdigit():
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 (User ID) \u0643\u0623\u0631\u0642\u0627\u0645 \u0641\u0642\u0637.")
            return
        target_id = int(msg.text.strip())
        target_user = db.get_user(target_id)
        if not target_user:
            db.upsert_user(target_id, "", "\u0645\u0633\u062a\u062e\u062f\u0645")
            target_user = db.get_user(target_id)
        temp["target_id"] = target_id
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_MANUAL_BAL_AMOUNT"
        await msg.reply_text(
            f"\U0001f464 <b>\u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645:</b> <code>{target_id}</code> ({html.escape(target_user.get('full_name') or '')})\n"
            f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0647 \u0627\u0644\u062d\u0627\u0644\u064a:</b> <code>{float(target_user['balance']):.2f}</code> {html.escape(currency)}\n\n"
            "\u0623\u0631\u0633\u0644 \u0627\u0644\u0622\u0646 <b>\u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u0631\u0627\u062f \u0625\u0636\u0627\u0641\u062a\u0647 \u0623\u0648 \u062e\u0635\u0645\u0647</b>:\n"
            "\u2022 \u0644\u0625\u0636\u0627\u0641\u0629 \u0631\u0635\u064a\u062f \u0623\u0631\u0633\u0644 \u0627\u0644\u0631\u0642\u0645 \u0645\u0628\u0627\u0634\u0631\u0629 (\u0645\u062b\u0627\u0644: <code>20</code>)\n"
            "\u2022 \u0644\u062e\u0635\u0645 \u0631\u0635\u064a\u062f \u0636\u0639 \u0639\u0644\u0627\u0645\u0629 \u0633\u0627\u0644\u0628 \u0642\u0628\u0644 \u0627\u0644\u0631\u0642\u0645 (\u0645\u062b\u0627\u0644: <code>-10</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_MANUAL_BAL_AMOUNT":
        if not msg.text:
            return
        try:
            delta = round(float(msg.text.strip().replace(",", ".")), 2)
        except ValueError:
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0631\u0642\u0645 \u0635\u062d\u064a\u062d (\u0645\u062b\u0627\u0644: 20 \u0623\u0648 -5).")
            return
        target_id = int(temp["target_id"])
        new_bal = db.update_user_balance(target_id, delta)
        clear_state(context)
        await msg.reply_text(
            f"\u2705 <b>\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0631\u0635\u064a\u062f \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 <code>{target_id}</code> \u0628\u0646\u062c\u0627\u062d!</b>\n"
            f"\U0001f4b0 <b>\u0627\u0644\u0631\u0635\u064a\u062f \u0627\u0644\u062c\u062f\u064a\u062f:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0623\u0631\u0635\u062f\u0629", callback_data="adm_topups_menu")]]),
        )
        try:
            action_word = f"\u0625\u0636\u0627\u0641\u0629 {delta:.2f}" if delta >= 0 else f"\u062e\u0635\u0645 {abs(delta):.2f}"
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"\U0001f514 <b>\u0625\u0634\u0639\u0627\u0631 \u062a\u062d\u062f\u064a\u062b \u0631\u0635\u064a\u062f \u062d\u0633\u0627\u0628\u0643</b>\n"
                    f"\u062a\u0645 {action_word} {html.escape(currency)} \u0641\u064a \u062d\u0633\u0627\u0628\u0643 \u0628\u0648\u0627\u0633\u0637\u0629 \u0627\u0644\u0625\u062f\u0627\u0631\u0629.\n"
                    f"\U0001f4b0 <b>\u0631\u0635\u064a\u062f\u0643 \u0627\u0644\u062d\u0627\u0644\u064a:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}"
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
            await msg.reply_text("\u26a0\ufe0f \u064a\u0631\u062c\u0649 \u0625\u0631\u0633\u0627\u0644 \u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645 (User ID) \u0643\u0623\u0631\u0642\u0627\u0645 \u0641\u0642\u0637.")
            return
        target_id = int(msg.text.strip())
        if target_id == ADMIN_ID:
            await msg.reply_text("\u26a0\ufe0f \u0644\u0627 \u064a\u0645\u0643\u0646\u0643 \u062d\u0638\u0631 \u062d\u0633\u0627\u0628 \u0645\u0627\u0644\u0643 \u0627\u0644\u0628\u0648\u062a!")
            return
        ban_flag = state == "ADM_BAN_USER"
        db.set_user_ban(target_id, ban_flag)
        clear_state(context)
        status_str = "\U0001f6ab \u062a\u0645 \u062d\u0638\u0631 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645" if ban_flag else "\u2705 \u062a\u0645 \u0641\u0643 \u0627\u0644\u062d\u0638\u0631 \u0639\u0646 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645"
        await msg.reply_text(
            f"<b>{status_str} (<code>{target_id}</code>) \u0628\u0646\u062c\u0627\u062d!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645\u064a\u0646", callback_data="adm_users_menu")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: PUBLIC BROADCAST MESSAGE TO ALL USERS
    # --------------------------------------------------------------------------
    if state == "ADM_BROADCAST_MSG":
        clear_state(context)
        users = db.get_all_active_users()
        progress = await msg.reply_text(f"\U0001f4e2 \u062c\u0627\u0631\u064a \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u062c\u0645\u0627\u0639\u064a\u0629 \u0625\u0644\u0649 {len(users)} \u0645\u0633\u062a\u062e\u062f\u0645...")
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
            f"\u2705 <b>\u0627\u0643\u062a\u0645\u0644 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0631\u0633\u0627\u0644\u0629 \u0627\u0644\u062c\u0645\u0627\u0639\u064a\u0629!</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4e8 <b>\u062a\u0645 \u0627\u0644\u0625\u0631\u0633\u0627\u0644 \u0628\u0646\u062c\u0627\u062d \u0625\u0644\u0649:</b> {sent} \u0645\u0633\u062a\u062e\u062f\u0645\n"
            f"\u26a0\ufe0f <b>\u0641\u0634\u0644 / \u0642\u0627\u0645\u0648\u0627 \u0628\u062d\u0638\u0631 \u0627\u0644\u0628\u0648\u062a:</b> {failed} \u0645\u0633\u062a\u062e\u062f\u0645",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0644\u0648\u062d\u0629 \u062a\u062d\u0643\u0645 \u0627\u0644\u0623\u062f\u0645\u0646", callback_data="adm_home")]]),
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
            f"\u2705 <b>\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0625\u0639\u062f\u0627\u062f \u0628\u0646\u062c\u0627\u062d \u0625\u0644\u0649:</b> <code>{html.escape(val)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f519 \u0627\u0644\u0639\u0648\u062f\u0629 \u0644\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a", callback_data="adm_settings")]]),
        )
        return


# ==============================================================================
# Application Entrypoint
# ==============================================================================
def main() -> None:
    db.init_db()
    if not BOT_TOKEN:
        raise RuntimeError("\u062e\u0637\u0623: \u0644\u0645 \u064a\u062a\u0645 \u0636\u0628\u0637 BOT_TOKEN \u0641\u064a \u0645\u0644\u0641 .env! \u0642\u0645 \u0628\u062a\u0634\u063a\u064a\u0644 ./install.sh \u0623\u0648 \u062a\u0639\u062f\u064a\u0644 \u0645\u0644\u0641 .env")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))

    logger.info("\u2705 \u062a\u0645 \u062a\u0634\u063a\u064a\u0644 \u0628\u0648\u062a \u0645\u062a\u062c\u0631 \u0634\u0631\u0627\u0626\u062d eSIM \u0628\u0646\u062c\u0627\u062d (\u064a\u0639\u0645\u0644 \u0639\u0644\u0649 \u0645\u062f\u0627\u0631 \u0627\u0644\u0633\u0627\u0639\u0629 24/7)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()