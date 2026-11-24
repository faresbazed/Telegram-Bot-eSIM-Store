# -*- coding: utf-8 -*-
import asyncio
import csv
import html
import io
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import qrcode
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden, RetryAfter
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
import binance_pay as bp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_CUSTOMER = "عزيزي العميل"
NO_DESC = "بدون وصف"
NO_LPA_QR = "لا يوجد (صورة QR)"
NONE_STR = "لا يوجد"
NO_USER = "بدون"
ATTACHED_RECEIPT = "مرفق صورة إيصال"



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


def generate_qr_bytes_async(data: str):
    return asyncio.to_thread(generate_qr_bytes, data)


def _read_binary_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("state", None)
    context.user_data.pop("temp", None)


def ensure_user_synced(user, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """Avoid repeat write transactions for an unchanged user profile."""
    sig = ((user.username or "").strip().lstrip("@"), (user.full_name or "").strip())
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict) and user_data.get("_user_sig") == sig:
        existing = db.get_user(user.id)
        if existing is not None:
            return existing
    row = db.upsert_user(user.id, user.username, user.full_name)
    if isinstance(user_data, dict):
        user_data["_user_sig"] = sig
    return row


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
            "⛔ <b>عذراً، تم حظر حسابك من استخدام هذا البوت.</b>\n\n"
            f"للتواصل مع الإدارة والاستفسار: @{html.escape(support)}"
        )
        if update.callback_query:
            await update.callback_query.answer("⛔ حسابك محظور من استخدام البوت.", show_alert=True)
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
    header_title: str = "🎉 <b>تم شراء شريحة eSIM بنجاح!</b>",
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
        "━━━━━━━━━━━━━━━━━━━━",
        f"🌍 <b>الدولة:</b> {country_name}",
        f"📦 <b>الباقة:</b> {plan_name}",
        f"💵 <b>السعر:</b> {paid_price:.2f} {html.escape(currency)}",
    ]
    if new_balance is not None:
        caption_lines.append(f"💰 <b>رصيدك المتبقي:</b> {db.fmt_money(new_balance)} {html.escape(currency)}")
    if lpa_code:
        caption_lines.extend([
            "",
            "🔑 <b>كود التفعيل المباشر (LPA):</b>",
            f"<code>{html.escape(lpa_code)}</code>",
        ])
    if notes:
        caption_lines.extend([
            "",
            f"📝 <b>ملاحظات إضافية:</b>\n{html.escape(notes)}",
        ])
    caption_lines.extend([
        "",
        "📲 <b>طريقة التفعيل:</b>",
        "1️⃣ افتح الإعدادات في هاتفك ثم (بيانات الهاتف / الخلوية).",
        "2️⃣ اختر <b>إضافة شريحة إلكترونية (Add eSIM)</b>.",
        "3️⃣ امسح كود الـ QR المرفق أو انسخ كود الـ LPA أعلاه وأدخله يدوياً.",
    ])
    caption = "\n".join(caption_lines)
    kb = InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]])

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
            raw_bytes = await asyncio.to_thread(_read_binary_file, qr_local_path)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(io.BytesIO(raw_bytes), filename="esim_qr.png"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            sent_photo = True

    if not sent_photo and lpa_code:
        qr_bio = await generate_qr_bytes_async(lpa_code)
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


async def _send_with_flood_retry(send_coro_factory, max_retries: int = 3):
    """Retry a Telegram send after the requested flood-wait delay."""
    for attempt in range(max_retries + 1):
        try:
            return await send_coro_factory()
        except RetryAfter as exc:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(float(exc.retry_after) + 0.25)


async def broadcast_new_inventory(
    context: ContextTypes.DEFAULT_TYPE,
    plan_id: int,
    added_count: int,
    mode: str = "all",
) -> Tuple[int, int]:
    """Notify waitlisted users and/or all active users after a restock."""
    if mode == "silent":
        return 0, 0
    plan = db.get_plan(plan_id)
    if not plan:
        return 0, 0
    currency = get_currency()
    waitlisted = db.get_plan_alert_subscribers(plan_id)
    waitlisted_ids = {int(u["user_id"]) for u in waitlisted}
    desc_line = f"📝 <b>التفاصيل:</b> {html.escape(plan['description'])}\n" if plan.get("description") else ""
    common = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>الدولة:</b> {html.escape(plan['country_name'])}\n"
        f"📦 <b>الباقة:</b> {html.escape(plan['name'])}\n"
        f"{desc_line}"
        f"💵 <b>السعر:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
        f"➕ <b>الكمية المضافة:</b> {added_count} شريحة\n"
        f"✅ <b>المتاح الآن:</b> {plan['available_count']} شريحة\n\n"
        "👇 <b>اضغط للشراء الآن:</b>"
    )
    waitlist_text = "🎉 <b>توفرت الآن الباقة التي طلبت تنبيه توفرها!</b>\n" + common
    public_text = "🔔 <b>إشعار توفر مخزون جديد من شرائح eSIM!</b>\n" + common
    kb = InlineKeyboardMarkup([
        [IKB("🛒 شراء هذه الباقة الآن", callback_data=f"u_plan:{plan_id}")],
        [IKB("🏠 القائمة الرئيسية", callback_data="home")],
    ])
    sent = failed = 0
    delivered_waitlist: List[int] = []
    if mode in ("all", "waitlist"):
        for u in waitlisted:
            uid = int(u["user_id"])
            try:
                await _send_with_flood_retry(lambda uid=uid: context.bot.send_message(
                    chat_id=uid, text=waitlist_text, parse_mode=ParseMode.HTML, reply_markup=kb
                ))
                sent += 1
                delivered_waitlist.append(uid)
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest, TelegramError):
                failed += 1
        if delivered_waitlist:
            db.clear_plan_alerts(plan_id, delivered_waitlist)
    if mode == "all":
        for u in db.get_all_active_users():
            uid = int(u["user_id"])
            if uid in waitlisted_ids:
                continue
            try:
                await _send_with_flood_retry(lambda uid=uid: context.bot.send_message(
                    chat_id=uid, text=public_text, parse_mode=ParseMode.HTML, reply_markup=kb
                ))
                sent += 1
                await asyncio.sleep(0.04)
            except (Forbidden, BadRequest, TelegramError):
                failed += 1
    return sent, failed


async def send_admin_sale_alert(
    context: ContextTypes.DEFAULT_TYPE,
    buyer,
    esim: Dict[str, Any],
) -> None:
    """Notify the owner of the sale without sending activation secrets."""
    if not ADMIN_ID:
        return
    currency = get_currency()
    user_tag = f"@{buyer.username}" if getattr(buyer, "username", None) else (getattr(buyer, "full_name", "") or "")
    items = esim.get("items") or [esim]
    qty = len(items)
    ids_str = ", ".join(f"#{it['id']}" for it in items)
    total_paid = float(esim.get("total_paid") or esim.get("paid_price") or 0.0)
    unit_price = float(esim.get("unit_price") or (total_paid / max(qty, 1)))
    types_set = {it.get("item_type") for it in items}
    item_label = "كود نصي (LPA)" if types_set == {"lpa"} else "صورة QR" if types_set == {"qr"} else "مختلط (LPA + QR)"
    qty_line = f"🔢 <b>الكمية المباعة:</b> {qty} شرائح ({unit_price:.2f} × {qty})\n" if qty > 1 else ""
    title = f"🛒 <b>عملية بيع {qty} شرائح eSIM جديدة!</b>" if qty > 1 else "🛒 <b>عملية بيع شريحة eSIM جديدة!</b>"
    text = (
        f"{title}\n━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>العميل:</b> {html.escape(user_tag)} (<code>{buyer.id}</code>)\n"
        f"🌍 <b>الدولة:</b> {html.escape(str(esim.get('country_name', '')))}\n"
        f"📦 <b>الباقة:</b> {html.escape(str(esim.get('plan_name', '')))}\n"
        f"{qty_line}"
        f"💵 <b>الإجمالي المخصوم:</b> {total_paid:.2f} {html.escape(currency)}\n"
        f"🆔 <b>أرقام الشرائح بالمخزون:</b> {ids_str}\n"
        f"🏷 <b>نوع الشريحة:</b> {item_label}\n"
        f"🕒 <b>وقت البيع:</b> {esim.get('sold_at') or items[0].get('sold_at') or ''}"
    )
    first_id = items[0]["id"]
    kb = InlineKeyboardMarkup([[
        IKB(f"🔍 فحص الشريحة #{first_id}", callback_data=f"adm_esim_item:{first_id}"),
        IKB("👤 ملف المشتري", callback_data=f"adm_user_view:{buyer.id}"),
    ]])
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as exc:
        logger.warning("Failed to notify admin of sale: %s", exc)

async def send_refund_video_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    req: Dict[str, Any],
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Forwards the customer's refund video proof to the owner with full request details."""
    if not ADMIN_ID:
        return
    currency = get_currency()
    caption = (
        f"↩️ <b>طلب استرداد جديد #{req['id']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>العميل:</b> {html.escape(req.get('full_name') or '')} (@{html.escape(req.get('username') or NO_USER)})\n"
        f"🆔 <b>معرف العميل:</b> <code>{req['user_id']}</code>\n"
        f"📲 <b>رقم الشريحة:</b> #{req['esim_id']}\n"
        f"🌍 <b>الدولة:</b> {html.escape(str(req.get('country_name') or ''))}\n"
        f"📦 <b>الباقة:</b> {html.escape(str(req.get('plan_name') or ''))}\n"
        f"💵 <b>السعر المدفوع للشريحة:</b> <code>{float(req.get('refund_amount') or 0):.2f}</code> {html.escape(currency)}\n"
        f"📝 <b>وصف المشكلة:</b> {html.escape(req.get('reason') or NONE_STR)}\n"
        f"📅 <b>التاريخ:</b> {req.get('created_at', '')}\n\n"
        "🎬 <b>فيديو إثبات المشكلة مرفق مع هذا الطلب.</b>"
    )
    video_file_id = req.get("video_file_id", "")
    video_type = req.get("video_type", "video")
    try:
        if video_type == "video_note":
            await context.bot.send_message(chat_id=ADMIN_ID, text=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
            await context.bot.send_video_note(chat_id=ADMIN_ID, video_note=video_file_id)
        elif video_type == "document":
            await context.bot.send_document(chat_id=ADMIN_ID, document=video_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await context.bot.send_video(chat_id=ADMIN_ID, video=video_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as exc:
        logger.warning("Failed to forward refund video to admin: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=caption + "\n\n⚠️ تعذر إرسال الفيديو تلقائياً، يرجى مراجعة الطلب من لوحة التحكم.",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramError:
            pass



# ==============================================================================
# Main Menus (User & Admin)
# ==============================================================================

# Colored inline buttons: Bot API 9.4 (Feb 9, 2026) added `style` to
# InlineKeyboardButton; PTB 21.6 predates the field, so it goes via api_kwargs.
_InlineKeyboardButtonBase = InlineKeyboardButton


def _infer_button_style(text, callback_data=None, url=None):
    t = text or ""
    if any(k in t for k in ("❌", "⛔", "🚫", "🗑",
                            "إلغاء", "رفض",
                            "حذف", "حظر")):
        return "danger"
    if any(k in t for k in ("✅", "✔", "🛒", "💳", "➕",
                            "تأكيد", "موافقة",
                            "شحن", "شراء")):
        return "success"
    return "primary"


def IKB(text, callback_data=None, url=None, style=None, **kwargs):
    """InlineKeyboardButton with an explicit Bot API `style` color."""
    if style is None:
        style = _infer_button_style(text, callback_data, url)
    api_kwargs = dict(kwargs.pop("api_kwargs", None) or {})
    api_kwargs["style"] = style
    return _InlineKeyboardButtonBase(
        text, url=url, callback_data=callback_data, api_kwargs=api_kwargs, **kwargs
    )


def build_home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [IKB("🛒 شراء شريحة eSIM", callback_data="u_countries")],
        [
            IKB("💳 شحن الرصيد", callback_data="u_topup"),
            IKB("💰 رصيدي وحسابي", callback_data="u_account"),
        ],
        [
            IKB("📦 مشترياتي السابقة", callback_data="u_purchases"),
            IKB("↩️ طلب استرداد", callback_data="u_refund"),
        ],
        [IKB("🛟 الدعم الفني", callback_data="u_support")],
    ]
    if is_admin(user_id):
        rows.append([IKB("🛠 لوحة تحكم الأدمن (المالك)", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


# ==============================================================================
# Persistent Bottom Reply Keyboard (colored quick menu)
# ==============================================================================
BTN_STORE = "🛍️ المتجر"
BTN_TOPUP = "💳 شحن الرصيد"
BTN_ACCOUNT = "💰 رصيدي وحسابي"
BTN_PURCHASES = "📦 مشترياتي"
BTN_REFUND = "↩️ طلب استرداد"
BTN_SUPPORT = "🛟 الدعم الفني"
BTN_ADMIN = "🛠️ لوحة الأدمن"

REPLY_MENU_ACTIONS = {
    BTN_STORE: "u_countries",
    BTN_TOPUP: "u_topup",
    BTN_ACCOUNT: "u_account",
    BTN_PURCHASES: "u_purchases",
    BTN_REFUND: "u_refund",
    BTN_SUPPORT: "u_support",
    BTN_ADMIN: "adm_home",
}

# Keywords from older bottom-keyboard layouts so legacy buttons keep working
# until every client receives the refreshed keyboard (matched in order).
MENU_KEYWORD_ACTIONS = [
    ("شحن", "u_topup"),
    ("استرداد", "u_refund"),
    ("رصيدي", "u_account"),
    ("حسابي", "u_account"),
    ("مشترياتي", "u_purchases"),
    ("طلباتي", "u_purchases"),
    ("منتجاتي", "u_purchases"),
    ("الدعم", "u_support"),
    ("الأدمن", "adm_home"),
    ("المتجر", "u_countries"),
    ("المخزون", "u_countries"),
    ("الشرائح", "u_countries"),
]


def _norm_menu_text(text: str) -> str:
    return " ".join((text or "").replace("\ufe0f", "").split())


def match_reply_menu_action(text: str, exact_only: bool = False) -> Optional[str]:
    """Map a bottom-menu press (exact new button text, or a legacy menu phrase) to a callback action."""
    normalized = _norm_menu_text(text)
    if not normalized:
        return None
    exact_map = {_norm_menu_text(btn): action for btn, action in REPLY_MENU_ACTIONS.items()}
    if normalized in exact_map:
        return exact_map[normalized]
    if exact_only:
        return None
    for keyword, action in MENU_KEYWORD_ACTIONS:
        if keyword in normalized:
            return action
    return None


def build_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard. Button colors use the Bot API `style` field
    (danger/success/primary); PTB 21.6 predates it, so it goes through api_kwargs."""
    def _btn(text: str, style: str) -> KeyboardButton:
        return KeyboardButton(text, api_kwargs={"style": style})

    rows = [
        [_btn(BTN_STORE, "primary")],
        [_btn(BTN_TOPUP, "success")],
        [_btn(BTN_ACCOUNT, "primary"), _btn(BTN_PURCHASES, "primary")],
        [_btn(BTN_REFUND, "primary"), _btn(BTN_SUPPORT, "danger")],
    ]
    if is_admin(user_id):
        rows.append([_btn(BTN_ADMIN, "primary")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


class _ReplyMenuMessage:
    """Minimal stand-in for the message attached to a CallbackQuery."""

    def __init__(self, message) -> None:
        self._message = message
        self.photo = None
        self.chat = message.chat
        self.chat_id = message.chat_id

    async def delete(self) -> None:
        return None


class _ReplyMenuQuery:
    """Lets a bottom reply-keyboard press reuse the existing inline callback flows.
    Text edits become new chat messages, since reply-keyboard presses have no
    message to edit."""

    def __init__(self, message, data: str) -> None:
        self.data = data
        self.message = _ReplyMenuMessage(message)
        self._message = message

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def edit_message_text(self, text: str, parse_mode=None, reply_markup=None, **kwargs: Any):
        return await self._message.reply_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)


class _ReplyMenuUpdate:
    """Read-through proxy around a real Update exposing a synthetic
    callback_query. PTB Update attributes are frozen, so the real update
    cannot be mutated; handlers only read attributes off the proxy."""

    def __init__(self, update: Update, query) -> None:
        self._update = update
        self.callback_query = query

    def __getattr__(self, name):
        return getattr(self._update, name)


async def route_reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    """Dispatch a bottom-menu press through the existing callback router."""
    proxy = _ReplyMenuUpdate(update, _ReplyMenuQuery(update.message, action))
    await callback_router(proxy, context)


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    clear_state(context)
    user = update.effective_user
    if not user:
        return
    u_row = ensure_user_synced(user, context)
    currency = get_currency()

    text = (
        f"👋 <b>أهلاً بك يا {html.escape(user.first_name or DEFAULT_CUSTOMER)} في متجر شرائح eSIM الرقمية!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 نقدم لك شرائح اتصال إلكترونية (eSIM) فورية لجميع دول العالم بأسعار تنافسية واستلام فوري لكود الـ QR.\n\n"
        f"🆔 <b>معرف حسابك:</b> <code>{user.id}</code>\n"
        f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(u_row['balance'])}</code> {html.escape(currency)}\n\n"
        "👇 <b>اختر من القائمة التالية ما يناسبك:</b>"
    )
    if update.callback_query:
        await safe_edit_or_send(update, text)
    else:
        await safe_edit_or_send(update, text, build_reply_keyboard(user.id))


async def show_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ هذه اللوحة مخصصة لمالك البوت فقط.", show_alert=True)
        return
    clear_state(context)
    stats = db.get_stats()
    currency = get_currency()

    text = (
        "🛠 <b>لوحة تحكم المالك (Admin Panel)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>إجمالي المستخدمين:</b> {stats['total_users']} (المحظورين: {stats['banned_users']})\n"
        f"🌍 <b>عدد الدول المضافة:</b> {stats['total_countries']}\n"
        f"📦 <b>عدد الباقات:</b> {stats['total_plans']}\n"
        f"📲 <b>الشرائح المتاحة بالمخزون:</b> {stats['available_esims']}\n"
        f"✅ <b>الشرائح المباعة:</b> {stats['sold_esims']}\n"
        f"💵 <b>إجمالي المبيعات:</b> {stats['total_revenue']:.2f} {html.escape(currency)}\n"
        f"⏳ <b>طلبات شحن الرصيد المعلقة:</b> {stats['pending_topups']}\n"
        f"↩️ <b>طلبات الاسترداد المعلقة:</b> {stats['pending_refunds']}\n"
        f"🔔 <b>تنبيهات توفر نشطة:</b> {stats.get('active_stock_alerts', 0)}\n\n"
        "👇 <b>اختر القسم الذي تريد إدارته:</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            IKB("🌍 إدارة الدول", callback_data="adm_countries"),
            IKB("📦 إدارة الباقات", callback_data="adm_plans_SelectCountry"),
        ],
        [
            IKB("📲 إدارة مخزون الشرائح (eSIMs)", callback_data="adm_inv_SelectCountry"),
        ],
        [
            IKB("📊 تقارير الأرباح وCSV", callback_data="adm_analytics"),
            IKB("🔍 فحص شريحة / عميل", callback_data="adm_inspector"),
        ],
        [
            IKB(
                f"💳 طلبات الشحن والأرصدة ({stats['pending_topups']})",
                callback_data="adm_topups_menu",
            ),
        ],
        [
            IKB(
                f"↩️ طلبات الاسترداد ({stats['pending_refunds']})",
                callback_data="adm_refunds_menu",
            ),
        ],
        [
            IKB("👥 إدارة المستخدمين والحظر", callback_data="adm_users_menu"),
            IKB("📋 قائمة المستخدمين بالأرصدة", callback_data="adm_users_list:0"),
        ],
        [
            IKB("🎟 أكواد الخصم", callback_data="adm_discounts"),
            IKB("📢 رسالة جماعية للكل", callback_data="adm_broadcast_start"),
        ],
        [
            IKB("⚙️ إعدادات الدفع والدعم", callback_data="adm_settings"),
        ],
        [IKB("🏠 العودة لواجهة المستخدم", callback_data="home")],
    ])
    await safe_edit_or_send(update, text, kb)


async def render_user_plan_view(update, context, user, plan_id: int, qty: int = 1) -> None:
    plan = db.get_plan(plan_id)
    if not plan:
        await show_home(update, context)
        return
    if int(plan.get("is_archived") or 0):
        await safe_edit_or_send(
            update,
            "⚠️ هذه الباقة لم تعد متاحة حالياً.",
            InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
        )
        return
    currency = get_currency()
    u_row = db.get_user(user.id)
    if not u_row:
        u_row = ensure_user_synced(user, context)
    available = int(plan.get("available_count") or 0)
    balance = db.to_decimal(u_row.get("balance") or 0)
    unit = db.to_decimal(plan["price"]).quantize(db.PRICE_QUANT)
    max_qty = max(1, min(available, 10))
    qty = max(1, min(int(qty), max_qty))
    total = unit * qty
    disc_code = (context.user_data.get("temp", {}) or {}).get("discount_code")
    disc = db.get_discount_code(disc_code) if disc_code else None
    discount_amount = Decimal("0")
    if disc:
        discount_amount = (total * float(disc["percent"]) / Decimal("100")).quantize(db.PRICE_QUANT)
        total = (total - discount_amount).quantize(db.PRICE_QUANT)
    stock = f"✅ متوفر للاستلام الفوري ({available} شريحة)" if available else "❌ نفدت الكمية حالياً"
    desc = f"📝 <b>تفاصيل الباقة:</b>\n{html.escape(plan['description'])}\n\n" if plan.get("description") else ""
    qty_lines = ""
    if available > 1:
        qty_lines = f"🔢 <b>الكمية المختارة:</b> {qty} شريحة\n🧾 <b>الإجمالي:</b> <code>{(unit * qty):.2f}</code> {html.escape(currency)}\n"
    disc_line = ""
    if disc:
        disc_line = (
            f"🎟 <b>كود الخصم {html.escape(disc_code)} ({float(disc['percent']):g}%):</b> <code>-{discount_amount:.2f}</code>\n"
            f"💳 <b>الإجمالي بعد الخصم:</b> <code>{total:.2f}</code> {html.escape(currency)}\n"
        )
    text = (
        "📦 <b>تفاصيل الباقة المختارة</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>الدولة:</b> {html.escape(plan['country_name'])}\n"
        f"🏷 <b>الباقة:</b> {html.escape(plan['name'])}\n"
        f"💵 <b>سعر الشريحة الواحدة:</b> <code>{unit:.2f}</code> {html.escape(currency)}\n"
        f"{qty_lines}{disc_line}📊 <b>حالة المخزون:</b> {stock}\n\n{desc}"
        f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(balance)}</code> {html.escape(currency)}"
    )
    rows = []
    if available:
        if available > 1:
            rows.append([
                IKB("➖", callback_data=f"u_plan:{plan_id}:{max(1, qty-1)}"),
                IKB(f"🔢 الكمية: {qty}/{max_qty}", callback_data=f"u_plan:{plan_id}:{qty}"),
                IKB("➕", callback_data=f"u_plan:{plan_id}:{min(max_qty, qty+1)}"),
            ])
        if balance >= total:
            label = f"✅ تأكيد شراء {qty} شرائح ({total:.2f} {currency})" if qty > 1 else f"✅ تأكيد الشراء ({total:.2f} {currency})"
            rows.append([IKB(label, callback_data=f"u_buy_confirm:{plan_id}:{qty}", style="success")])
            rows.append([IKB("🎟 لديك كود خصم؟", callback_data=f"u_plan_discount:{plan_id}:{qty}")])
        else:
            diff = total - balance
            text += f"\n\n⚠️ <b>رصيدك غير كافٍ لهذه الكمية. تحتاج إلى شحن <code>{diff:.2f}</code> {html.escape(currency)} إضافية.</b>"
            rows.append([IKB("💳 شحن الرصيد الآن", callback_data="u_topup")])
    else:
        if db.has_stock_alert(user.id, plan_id):
            text += "\n\n🔔 <b>تنبيه التوفر مفعّل لديك لهذه الباقة.</b>"
            rows.append([IKB("🔕 إلغاء تنبيه التوفر", callback_data=f"u_alert_toggle:{plan_id}", style="danger")])
        else:
            text += "\n\n💡 فعّل التنبيه ليصلك إشعار فور توفر الباقة من جديد."
            rows.append([IKB("🔔 أعلمني فور توفر هذه الباقة", callback_data=f"u_alert_toggle:{plan_id}", style="success")])
    rows += [
        [IKB("🔙 العودة لباقات الدولة", callback_data=f"u_country:{plan['country_id']}")],
        [IKB("🏠 القائمة الرئيسية", callback_data="home")],
    ]
    await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def render_admin_add_esim(update, context, plan_id: int, mode: str = "all") -> None:
    plan = db.get_plan(plan_id)
    if not plan:
        return
    mode = mode if mode in ("all", "waitlist", "silent") else "all"
    wait_count = db.get_plan_alert_count(plan_id)
    prev_temp = context.user_data.get("temp", {}) if context.user_data.get("state") == "ADM_ADD_ESIM" else {}
    keep_batch = prev_temp.get("plan_id") == plan_id and prev_temp.get("batch_id")
    context.user_data["state"] = "ADM_ADD_ESIM"
    new_temp = {"plan_id": plan_id, "notify_mode": mode}
    if keep_batch:
        for k in ("batch_id", "batch_added", "batch_progress_id"):
            if k in prev_temp:
                new_temp[k] = prev_temp[k]
    else:
        new_temp["batch_id"] = int(time.time() * 1000)
    context.user_data["temp"] = new_temp
    labels = {
        "all": "📢 إشعار عام للكل + المنتظرين",
        "waitlist": f"🔔 للمنتظرين فقط ({wait_count} عميل)",
        "silent": "🔕 إضافة صامتة بدون إشعارات",
    }
    text = (
        "➕ <b>إضافة شرائح eSIM إلى المخزون</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>الدولة:</b> {html.escape(plan['country_name'])}\n"
        f"📦 <b>الباقة:</b> {html.escape(plan['name'])}\n"
        f"🔔 <b>المنتظرون:</b> {wait_count} عميل\n"
        f"📡 <b>وضع الإشعار:</b> {labels[mode]}\n\n"
        "أرسل كود LPA أو عدة أكواد (كل كود في سطر)، أو صور QR — صورة واحدة أو عدة صور أو ألبوم كامل.\n"
        "كل صورة تُضاف فور وصولها. عند الانتهاء من الصور اضغط ✅ إنهاء الإضافة. الأكواد المكررة سيتم تجاهلها."
    )
    kb = InlineKeyboardMarkup([
        [IKB(f"{'✅ ' if mode == 'all' else ''}📢 للكل", callback_data=f"adm_esim_mode:{plan_id}:all"),
         IKB(f"{'✅ ' if mode == 'waitlist' else ''}🔔 للمنتظرين", callback_data=f"adm_esim_mode:{plan_id}:waitlist")],
        [IKB(f"{'✅ ' if mode == 'silent' else ''}🔕 صامت", callback_data=f"adm_esim_mode:{plan_id}:silent")],
        [IKB("🔙 إلغاء والعودة للباقة", callback_data=f"adm_plan_view:{plan_id}")],
    ])
    await safe_edit_or_send(update, text, kb)


async def render_admin_user_profile(update, target: Dict[str, Any]) -> None:
    currency=get_currency(); uid=int(target["user_id"])
    purchases=db.get_user_purchases(uid)
    uname="@"+html.escape(target["username"]) if target.get("username") else "بدون يوزر"
    status="🚫 محظور" if target.get("is_banned") else "✅ نشط"
    text=(
        "👤 <b>ملف العميل</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 الاسم: {html.escape(target.get('full_name') or NONE_STR)}\n🔗 اليوزر: {uname}\n🆔 ID: <code>{uid}</code>\n"
        f"🛡 الحالة: {status}\n💰 الرصيد: <code>{db.fmt_money(target.get('balance') or 0)}</code> {html.escape(currency)}\n"
        f"📲 المشتريات: {target.get('purchases_count',len(purchases))} | 💵 الإنفاق: <code>{float(target.get('total_spent') or 0):.2f}</code> {html.escape(currency)}\n"
        f"💳 الشحن المقبول: {target.get('approved_topups_count',0)} (<code>{float(target.get('approved_topups_sum') or 0):.2f}</code>)\n"
        f"⏳ الشحن المعلق: {target.get('pending_topups_count',0)} | ↩️ الاستردادات: {target.get('refunds_count',0)} | 🔔 تنبيهات التوفر: {target.get('stock_alerts_count',0)}"
    )
    rows=[[IKB("💰 تعديل الرصيد",callback_data=f"adm_bal_user:{uid}"),IKB("🔓 فك الحظر" if target.get("is_banned") else "🚫 حظر",callback_data=f"adm_toggle_ban_view:{uid}")]]
    for e in purchases[:8]:
        rows.append([IKB(f"📲 #{e['id']} | {e['country_name']} - {e['plan_name']}",callback_data=f"adm_esim_item:{e['id']}")])
    rows += [[IKB("🔎 بحث عن عميل آخر",callback_data="adm_lookup_user"),IKB("🔍 مركز الفحص",callback_data="adm_inspector")],[IKB("🔙 لوحة الأدمن",callback_data="adm_home")]]
    await safe_edit_or_send(update,text,InlineKeyboardMarkup(rows))


# ==============================================================================
# Command Handlers
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if update.message and user:
        banned = (not is_admin(user.id)) and db.is_user_banned(user.id)
        if not banned:
            await mirror_to_owner(update, context, "أمر من عميل")
        if not banned:
            await update.message.reply_text(
                "⌨️ <b>القائمة السفلية الملوّنة جاهزة!</b>\n"
                "استخدم الأزرار بالأسفل للتنقل السريع داخل المتجر 👇",
                parse_mode=ParseMode.HTML,
                reply_markup=build_reply_keyboard(user.id),
            )
    await show_home(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ عذراً، هذا الأمر مخصص لمالك البوت فقط.")
        return
    await show_admin_home(update, context)


# ==============================================================================
# Callback Query Router
# ==============================================================================
async def render_admin_plan_view(update, plan_id: int) -> None:
    plan = db.get_plan(plan_id)
    if not plan:
        return
    currency = get_currency()
    archived_note = "\n🗄 <b>الحالة: مخفية من المتجر (سجل المشتريات محفوظ)</b>" if int(plan.get("is_archived") or 0) else ""
    text = (
        f"📦 <b>إدارة الباقة: {html.escape(plan['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>الدولة:</b> {html.escape(plan['country_name'])}\n"
        f"💵 <b>السعر:</b> {float(plan['price']):.2f} {html.escape(currency)}\n"
        f"📝 <b>الوصف:</b> {html.escape(plan['description'] or NO_DESC)}\n"
        f"📲 <b>الشرائح المتاحة بالمخزون:</b> {int(plan['available_count'] or 0)} شريحة"
        f"{archived_note}"
    )
    kb = InlineKeyboardMarkup([
        [IKB("➕ إضافة شرائح eSIM (LPA أو QR)", callback_data=f"adm_esim_add:{plan_id}")],
        [IKB("📋 عرض وتعديل/حذف الشرائح بالمخزون", callback_data=f"adm_esim_list:{plan_id}")],
        [IKB("✏️ تعديل بيانات وسعر الباقة", callback_data=f"adm_plan_edit:{plan_id}")],
        [IKB("👁 إظهار في المتجر", callback_data=f"adm_plan_unarchive:{plan_id}")] if int(plan.get("is_archived") or 0) else [IKB("🗄 إخفاء من المتجر", callback_data=f"adm_plan_archive:{plan_id}")],
        [IKB("🗑️ حذف الباقة نهائياً", callback_data=f"adm_plan_del:{plan_id}")],
        [IKB("🔙 العودة لباقات الدولة", callback_data=f"adm_plans_country:{plan['country_id']}")],
    ])
    await safe_edit_or_send(update, text, kb)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    q = update.callback_query
    if not q or not q.data:
        return
    data = q.data
    user = update.effective_user
    currency = get_currency()
    temp = context.user_data.get("temp", {})

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
            "💰 <b>تفاصيل حسابي ورصيدي</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>الاسم:</b> {html.escape(user.full_name or '')}\n"
            f"🆔 <b>المعرف (ID):</b> <code>{user.id}</code>\n"
            f"💵 <b>الرصيد المتاح:</b> <code>{db.fmt_money(u_row['balance'])}</code> {html.escape(currency)}\n"
            f"📦 <b>إجمالي الشرائح المشتراة:</b> {len(purchases)} شريحة\n"
        )
        kb = InlineKeyboardMarkup([
            [IKB("💳 شحن الرصيد الآن", callback_data="u_topup")],
            [IKB("📦 عرض مشترياتي", callback_data="u_purchases")],
            [IKB("📜 سجل طلبات الشحن", callback_data="u_topup_history:0")],
            [IKB("🏠 القائمة الرئيسية", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data == "u_support":
        await q.answer()
        support_user = db.get_setting("support_username", "Support").lstrip("@")
        text = (
            "🛟 <b>مركز الدعم الفني وخدمة العملاء</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "إذا واجهتك أي مشكلة في شحن الرصيد أو تفعيل شريحة الـ eSIM الخاصة بك، يسعدنا تواصلك معنا مباشرة:\n\n"
            f"👨‍💻 <b>حساب الدعم الفني:</b> @{html.escape(support_user)}\n"
            f"🆔 <b>معرف حسابك لتزويد الدعم به:</b> <code>{user.id}</code>"
        )
        kb = InlineKeyboardMarkup([
            [IKB("💬 مراسلة الدعم الفني مباشرة", url=f"https://t.me/{support_user}")],
            [IKB("🏠 القائمة الرئيسية", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ---------------- USER: BUY eSIM FLOW ----------------
    if data == "u_countries":
        await q.answer()
        countries = db.get_countries()
        if not countries:
            text = (
                "🌍 <b>قائمة الدول المتاحة</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ لا توجد دول مضافة حالياً في المتجر. يرجى المحاولة لاحقاً."
            )
            kb = InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for c in countries:
            stock = int(c["available_stock"] or 0)
            badge = f"✅ ({stock})" if stock > 0 else "❌ نفد"
            rows.append([
                IKB(
                    f"{c['name']} — {badge}",
                    callback_data=f"u_country:{c['id']}",
                )
            ])
        rows.append([IKB("🏠 القائمة الرئيسية", callback_data="home")])
        text = (
            "🌍 <b>اختر الدولة التي ترغب بشراء شريحة eSIM لها:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "يظهر بجانب كل دولة عدد الشرائح المتوفرة للاستلام الفوري:"
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
                f"🌍 <b>الدولة: {html.escape(country['name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ لا توجد باقات مضافة لهذه الدولة حالياً."
            )
            kb = InlineKeyboardMarkup([
                [IKB("🔙 العودة لقائمة الدول", callback_data="u_countries")],
                [IKB("🏠 القائمة الرئيسية", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for p in plans:
            avail = int(p["available_count"] or 0)
            status_str = f"متاح ({avail})" if avail > 0 else "نفدت الكمية"
            rows.append([
                IKB(
                    f"📦 {p['name']} | {float(p['price']):.2f} {currency} ({status_str})",
                    callback_data=f"u_plan:{p['id']}",
                )
            ])
        rows.append([IKB("🔙 العودة لقائمة الدول", callback_data="u_countries")])
        rows.append([IKB("🏠 القائمة الرئيسية", callback_data="home")])
        text = (
            f"🌍 <b>باقات شرائح eSIM لدولة: {html.escape(country['name'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 اختر الباقة المناسبة لك لعرض التفاصيل والشراء الفوري:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_plan:"):
        await q.answer()
        parts = data.split(":")
        plan_id = int(parts[1])
        qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await render_user_plan_view(update, context, user, plan_id, qty=qty)
        return

    if data.startswith("u_alert_toggle:"):
        plan_id = int(data.split(":")[1])
        if not db.get_plan(plan_id):
            await q.answer("⚠️ الباقة غير موجودة.", show_alert=True)
            return
        subscribed = db.toggle_stock_alert(user.id, plan_id)
        await q.answer(
            "🔔 تم تفعيل التنبيه! سنرسل لك إشعاراً فور توفر الباقة." if subscribed else "🔕 تم إلغاء تنبيه توفر هذه الباقة.",
            show_alert=subscribed,
        )
        await render_user_plan_view(update, context, user, plan_id, qty=1)
        return

    if data.startswith("u_plan_discount:"):
        parts = data.split(":")
        plan_id = int(parts[1])
        qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await q.answer()
        context.user_data["state"] = "USER_DISCOUNT_CODE"
        context.user_data["temp"] = {"plan_id": plan_id, "qty": qty}
        await q.message.reply_text(
            "🎟 أرسل كود الخصم الآن:",
            reply_markup=InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data=f"u_plan:{plan_id}:{qty}")]]),
        )
        return

    if data.startswith("u_buy_confirm:"):
        parts = data.split(":")
        plan_id = int(parts[1])
        qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        plan = db.get_plan(plan_id)
        if not plan:
            await q.answer("⚠️ الباقة غير موجودة.", show_alert=True)
            return
        qty = max(1, min(qty, min(int(plan.get("available_count") or 0), 10)))
        disc_code = (context.user_data.get("temp", {}) or {}).get("discount_code")
        ok, code, result = db.purchase_esims_atomic(user.id, plan_id, quantity=qty, discount_code=disc_code)
        if not ok:
            if code == "invalid_discount":
                context.user_data.get("temp", {}).pop("discount_code", None)
                await q.answer("⚠️ كود الخصم غير صالح أو انتهى عدد استخداماته.", show_alert=True)
                await render_user_plan_view(update, context, user, plan_id, qty=qty)
            elif code == "insufficient_balance":
                await q.answer("⚠️ رصيدك غير كافٍ لإتمام عملية الشراء!", show_alert=True)
                await render_user_plan_view(update, context, user, plan_id, qty=qty)
            elif code == "out_of_stock":
                available = int((result or {}).get("available") or 0)
                await q.answer("⚠️ تغير المخزون، تم تحديث الكمية المتاحة.", show_alert=True)
                await render_user_plan_view(update, context, user, plan_id, qty=max(1, available))
            else:
                await q.answer("⚠️ تعذر إتمام الشراء، حاول مرة أخرى.", show_alert=True)
            return
        context.user_data.get("temp", {}).pop("discount_code", None)
        items = result.get("items") or [result]
        total = len(items)
        await q.answer(f"🎉 تم شراء {total} شرائح بنجاح!" if total > 1 else "🎉 تمت عملية الشراء بنجاح!")
        for idx, item in enumerate(items, 1):
            header = (
                f"🎉 <b>تم شراء شريحة eSIM بنجاح! (شريحة {idx} من {total})</b>"
                if total > 1 else "🎉 <b>تم شراء شريحة eSIM بنجاح!</b>"
            )
            await deliver_esim_to_chat(context, user.id, item, header_title=header)
        await send_admin_sale_alert(context, user, result)
        return

    if data == "u_purchases" or data.startswith("u_purchases:"):
        await q.answer()
        page = int(data.split(":")[1]) if ":" in data and data.split(":")[1].isdigit() else 0
        purchases = db.get_user_purchases(user.id)
        if not purchases:
            text = (
                "📦 <b>مشترياتي السابقة</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "لم تقم بشراء أي شريحة eSIM حتى الآن."
            )
            kb = InlineKeyboardMarkup([
                [IKB("🛒 تصفح الدول والباقات", callback_data="u_countries")],
                [IKB("🏠 القائمة الرئيسية", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return
        per_page = 12
        total_pages = max(1, (len(purchases) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        rows = []
        for item in purchases[page * per_page:(page + 1) * per_page]:
            rows.append([IKB(
                f"📲 #{item['id']} | {item['country_name']} - {item['plan_name']} ({float(item['sold_price'] or 0):.2f} {currency})",
                callback_data=f"u_view_esim:{item['id']}",
            )])
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(IKB("⬅️ السابق", callback_data=f"u_purchases:{page - 1}"))
            nav.append(IKB(f"📄 {page + 1} / {total_pages}", callback_data=f"u_purchases:{page}"))
            if page + 1 < total_pages:
                nav.append(IKB("التالي ➡️", callback_data=f"u_purchases:{page + 1}"))
            rows.append(nav)
        rows.append([IKB("🏠 القائمة الرئيسية", callback_data="home")])
        text = (
            f"📦 <b>سجل مشترياتي من شرائح eSIM (الإجمالي: {len(purchases)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 اضغط على أي شريحة لإعادة عرض بياناتها:"
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_view_esim:"):
        esim_id = int(data.split(":")[1])
        esim = db.get_esim(esim_id)
        if not esim or esim.get("sold_to") != user.id:
            await q.answer("⚠️ تعذر العثور على هذه الشريحة.", show_alert=True)
            return
        await q.answer("📲 جاري إرسال بيانات الشريحة...")
        await deliver_esim_to_chat(
            context=context,
            chat_id=user.id,
            esim=esim,
            header_title=f"📦 <b>بيانات شريحتك المشتراة (#{esim['id']})</b>",
        )
        return

    # ---------------- USER: REFUND REQUEST FLOW ----------------
    if data == "u_refund":
        await q.answer()
        clear_state(context)
        purchases = db.get_user_purchases(user.id)
        my_requests = db.get_user_refunds(user.id)
        status_labels = {"pending": "⏳ قيد المراجعة", "reviewed": "✅ تمت المراجعة"}
        requests_lines = ""
        if my_requests:
            requests_lines = "\n📋 <b>طلباتك السابقة:</b>\n" + "\n".join(
                f"• طلب #{r['id']} | شريحة #{r['esim_id']} ({html.escape(r['country_name'])} - {html.escape(r['plan_name'])}) — {status_labels.get(r['status'], r['status'])}"
                for r in my_requests[:10]
            ) + "\n"

        if not purchases:
            text = (
                "↩️ <b>طلب استرداد</b>\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "لم تقم بأي عملية شراء حتى الآن."
                + requests_lines
            )
            kb = InlineKeyboardMarkup([
                [IKB("🛒 تصفح الدول والباقات", callback_data="u_countries")],
                [IKB("🏠 القائمة الرئيسية", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        rows = []
        for item in purchases[:20]:
            rows.append([
                IKB(
                    f"📲 #{item['id']} | {item['country_name']} - {item['plan_name']} ({float(item['sold_price'] or 0):.2f} {currency})",
                    callback_data=f"u_refund_esim:{item['id']}",
                )
            ])
        rows.append([IKB("🏠 القائمة الرئيسية", callback_data="home")])
        text = (
            "↩️ <b>طلب استرداد</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "اختر الشريحة التي توجد بها مشكلة لإرسال طلب استرداد للإدارة:\n\n"
            "⚠️ <b>ملاحظة:</b> سيُطلب منك إرسال <b>فيديو إثبات</b> يوضح كافة تفاصيل المشكلة، وسيتم مراجعة طلبك من الإدارة."
            + requests_lines
        )
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("u_refund_esim:"):
        esim_id = int(data.split(":")[1])
        esim = db.get_esim(esim_id)
        if not esim or esim.get("sold_to") != user.id:
            await q.answer("⚠️ تعذر العثور على هذه الشريحة ضمن مشترياتك.", show_alert=True)
            return
        await q.answer()
        context.user_data["state"] = "USER_REFUND_REASON"
        context.user_data["temp"] = {"esim_id": esim_id}
        text = (
            f"↩️ <b>طلب استرداد للشريحة #{esim_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🌍 <b>الدولة:</b> {html.escape(esim['country_name'])}\n"
            f"📦 <b>الباقة:</b> {html.escape(esim['plan_name'])}\n"
            f"💵 <b>السعر المدفوع:</b> <code>{float(esim['sold_price'] or 0):.2f}</code> {html.escape(currency)}\n\n"
            "1️⃣ <b>الخطوة الأولى:</b> اكتب الآن <b>وصفاً مختصراً للمشكلة</b> في رسالة نصية "
            "(مثال: <code>كود QR لا يعمل</code> أو <code>الشريحة مفعلة مسبقاً</code>):"
        )
        kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء الطلب", callback_data="u_refund")]])
        await safe_edit_or_send(update, text, kb)
        return

    # ---------------- USER: TOP-UP FLOW ----------------
    if data == "u_topup_history" or data.startswith("u_topup_history:"):
        await q.answer()
        clear_state(context)
        page = int(data.split(":")[1]) if ":" in data and data.split(":")[1].isdigit() else 0
        reqs = db.get_user_topups(user.id)
        if not reqs:
            text = "📜 <b>سجل طلبات شحن الرصيد</b>\n━━━━━━━━━━━━━━━━━━━━\nلا توجد لديك طلبات شحن سابقة."
            kb = InlineKeyboardMarkup([
                [IKB("💳 شحن الرصيد الآن", callback_data="u_topup")],
                [IKB("🏠 القائمة الرئيسية", callback_data="home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return
        approved = [r for r in reqs if r["status"] == "approved"]
        pending = sum(1 for r in reqs if r["status"] == "pending")
        rejected = sum(1 for r in reqs if r["status"] == "rejected")
        total_approved = sum(float(r.get("amount") or 0) for r in approved)
        per_page = 10
        pages = max(1, (len(reqs) + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        labels = {"pending": "⏳ قيد المراجعة", "approved": "✅ مقبول", "rejected": "❌ مرفوض"}
        lines = []
        for r in reqs[page * per_page:(page + 1) * per_page]:
            method = "Binance Pay" if r["method"] == "binance" else "InstaPay EG"
            lines.append(
                f"• <b>#{r['id']}</b> | <code>{float(r['amount']):.2f}</code> {html.escape(currency)} | "
                f"{method} — {labels.get(r['status'], r['status'])} <i>({str(r.get('created_at') or '')[:16]})</i>"
            )
        text = (
            f"📜 <b>سجل طلبات شحن الرصيد ({len(reqs)})</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ مقبولة: {len(approved)} (<code>{total_approved:.2f}</code> {html.escape(currency)}) | "
            f"⏳ معلقة: {pending} | ❌ مرفوضة: {rejected}\n━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
        )
        rows = []
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(IKB("⬅️ السابق", callback_data=f"u_topup_history:{page - 1}"))
            nav.append(IKB(f"📄 {page + 1} / {pages}", callback_data=f"u_topup_history:{page}"))
            if page + 1 < pages:
                nav.append(IKB("التالي ➡️", callback_data=f"u_topup_history:{page + 1}"))
            rows.append(nav)
        rows.append([IKB("💳 طلب شحن جديد", callback_data="u_topup"), IKB("💰 حسابي", callback_data="u_account")])
        rows.append([IKB("🏠 القائمة الرئيسية", callback_data="home")])
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
        return

    if data == "u_topup":
        await q.answer()
        clear_state(context)
        auto_line = (
            "⚡ <b>شحن فوري تلقائي عبر Binance Pay (يُضاف الرصيد لحظة الدفع)</b>\n"
            if bp.is_enabled()
            else ""
        )
        text = (
            "💳 <b>شحن الرصيد في البوت</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "اختر وسيلة الدفع المناسبة لك لشحن رصيدك:\n\n"
            f"{auto_line}"
            "🟡 <b>Binance Pay (عملات رقمية - USDT)</b>\n"
            "🟣 <b>InstaPay EG (إنستا باي مصر)</b>"
        )
        auto_row = (
            [[IKB("⚡ شحن فوري تلقائي عبر Binance Pay", callback_data="u_topup_auto")]]
            if bp.is_enabled()
            else []
        )
        kb = InlineKeyboardMarkup(auto_row + [
            [IKB("🟡 الدفع عبر Binance ID (عملات رقمية)", callback_data="u_topup_m:binance")],
            [IKB("🟣 الدفع عبر InstaPay EG (إنستا باي)", callback_data="u_topup_m:instapay")],
            [IKB("📜 سجل طلبات الشحن السابقة", callback_data="u_topup_history:0")],
            [IKB("🏠 القائمة الرئيسية", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data == "u_topup_auto":
        await q.answer()
        if not bp.is_enabled():
            await q.answer("⚠️ الشحن التلقائي غير متاح حالياً، جرب وسيلة دفع أخرى.", show_alert=True)
            return
        clear_state(context)
        context.user_data["temp"] = {"method": "binance_auto"}
        if bp.mode() == "monitor":
            context.user_data["state"] = "USER_TOPUP_AUTO_PROOF"
            pay_target = db.get_setting("binance_id", "غير محدد")
            text = (
                "⚡ <b>شحن تلقائي عبر Binance Pay</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ حوّل المبلغ اللي عايز تشحنه (USDT) إلى Binance ID التالي:\n"
                f"<code>{html.escape(pay_target)}</code>\n"
                "<i>(اضغط على الرقم لنسخه)</i>\n\n"
                "2️⃣ بعد نجاح التحويل، ابعت <b>صورة إيصال التحويل</b> (السكرين اللي فيه Order ID) هنا <b>خلال ساعة من التحويل</b>.\n\n"
                "✅ البوت هيتحقق من التحويل في سجل Binance ويضيف رصيدك <b>تلقائياً بنفس المبلغ الواصل بالظبط</b>."
            )
        else:
            context.user_data["state"] = "USER_TOPUP_AUTO_AMOUNT"
            text = (
                "⚡ <b>شحن فوري تلقائي عبر Binance Pay</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ أرسل الآن <b>قيمة المبلغ المراد شحنه</b> في رسالة نصية بالأسفل (مثال: <code>10</code> أو <code>25.5</code>).\n"
                "2️⃣ سيظهر لك زر دفع، اضغطه وأكمل الدفع من تطبيق Binance.\n"
                "3️⃣ فور إتمام الدفع سيُضاف الرصيد إلى حسابك <b>تلقائياً</b> دون انتظار مراجعة."
            )
        kb = InlineKeyboardMarkup([
            [IKB("🔙 تغيير وسيلة الدفع", callback_data="u_topup")],
            [IKB("🏠 إلغاء والعودة للرئيسية", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    if data.startswith("u_topup_m:"):
        await q.answer()
        method = data.split(":")[1]
        context.user_data["state"] = "USER_TOPUP_AMOUNT"
        context.user_data["temp"] = {"method": method}

        if method == "binance":
            pay_target = db.get_setting("binance_id", "غير محدد")
            method_title = "🟡 Binance Pay (عملات رقمية)"
            target_label = "معرف Binance ID للتحويل"
        else:
            pay_target = db.get_setting("instapay_address", "غير محدد")
            method_title = "🟣 InstaPay EG (إنستا باي مصر)"
            target_label = "عنوان الدفع / رقم InstaPay EG"

        text = (
            f"💳 <b>شحن الرصيد عبر {method_title}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{target_label}:</b>\n"
            f"<code>{html.escape(pay_target)}</code>\n"
            "<i>(اضغط على الكود أعلاه لنسخه)</i>\n\n"
            "1️⃣ قم بتحويل المبلغ المطلوب إلى الحساب الموضح أعلاه.\n"
            f"2️⃣ أرسل الآن <b>قيمة المبلغ المراد شحنه ({html.escape(currency)})</b> في رسالة نصية بالأسفل (مثال: <code>10</code> أو <code>25.5</code>):"
        )
        kb = InlineKeyboardMarkup([
            [IKB("🔙 تغيير وسيلة الدفع", callback_data="u_topup")],
            [IKB("🏠 إلغاء والعودة للرئيسية", callback_data="home")],
        ])
        await safe_edit_or_send(update, text, kb)
        return

    # ==========================================================================
    # ADMIN PANEL CALLBACKS (OWNER ONLY)
    # ==========================================================================
    if data.startswith("adm_"):
        if not is_admin(user.id):
            await q.answer("⛔ غير مصرح لك بالدخول!", show_alert=True)
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
            rows = [[IKB("➕ إضافة دولة جديدة", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    IKB(
                        f"🌍 {c['name']} (الباقات: {c['plans_count']} | المخزون: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            text = (
                "🌍 <b>إدارة الدول</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "يمكنك إضافة دولة جديدة أو الضغط على أي دولة لتعديلها أو حذفها أو إدارة باقاتها:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_country_add":
            await q.answer()
            context.user_data["state"] = "ADM_ADD_COUNTRY"
            text = (
                "➕ <b>إضافة دولة جديدة</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن اسم الدولة مع العلم (مثال: <code>🇹🇷 تركيا</code> أو <code>🇸🇦 السعودية</code>):"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_countries")]])
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
                f"🌍 <b>إدارة الدولة: {html.escape(country['name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>عدد الباقات المضافة:</b> {len(plans)} باقة\n\n"
                "اختر الإجراء المطلوب:"
            )
            kb = InlineKeyboardMarkup([
                [IKB("📦 إدارة باقات هذه الدولة", callback_data=f"adm_plans_country:{country_id}")],
                [IKB("✏️ تعديل اسم الدولة", callback_data=f"adm_country_edit:{country_id}")],
                [IKB("🗑️ حذف الدولة بالكامل", callback_data=f"adm_country_del:{country_id}")],
                [IKB("🔙 العودة لقائمة الدول", callback_data="adm_countries")],
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
                f"✏️ <b>تعديل اسم الدولة: {html.escape(country['name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الاسم الجديد للدولة الآن:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data=f"adm_country_view:{country_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_country_del:"):
            country_id = int(data.split(":")[1])
            db.delete_country(country_id)
            await q.answer("🗑️ تم حذف الدولة وباقاتها بنجاح!", show_alert=True)
            # Return to countries list
            countries = db.get_countries()
            rows = [[IKB("➕ إضافة دولة جديدة", callback_data="adm_country_add")]]
            for c in countries:
                rows.append([
                    IKB(
                        f"🌍 {c['name']} (الباقات: {c['plans_count']} | المخزون: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_country_view:{c['id']}",
                    )
                ])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            await safe_edit_or_send(update, "✅ <b>تم حذف الدولة بنجاح.</b>\n\n🌍 <b>إدارة الدول:</b>", InlineKeyboardMarkup(rows))
            return

        # ---------------- ADMIN: PLANS ----------------
        if data == "adm_plans_SelectCountry":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            if not countries:
                await safe_edit_or_send(
                    update,
                    "⚠️ <b>لا توجد دول مضافة بعد. قم بإضافة دولة أولاً ثم أضف باقاتها.</b>",
                    InlineKeyboardMarkup([
                        [IKB("➕ إضافة دولة جديدة", callback_data="adm_country_add")],
                        [IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")],
                    ]),
                )
                return
            rows = [
                [IKB(f"🌍 {c['name']} ({c['plans_count']} باقة)", callback_data=f"adm_plans_country:{c['id']}")]
                for c in countries
            ]
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "📦 <b>إدارة الباقات</b>\n━━━━━━━━━━━━━━━━━━━━\nاختر الدولة لإدارة باقاتها:",
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
            plans = db.get_plans_by_country(country_id, include_archived=True)
            rows = [[IKB("➕ إضافة باقة جديدة لهذه الدولة", callback_data=f"adm_plan_add:{country_id}")]]
            for p in plans:
                arch = "🗄 " if int(p.get("is_archived") or 0) else ""
                rows.append([
                    IKB(
                        f"{arch}📦 {p['name']} — {float(p['price']):.2f} {currency} (المتاح: {int(p['available_count'] or 0)})",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([IKB("🔙 اختيار دولة أخرى", callback_data="adm_plans_SelectCountry")])
            rows.append([IKB("🛠 لوحة تحكم الأدمن", callback_data="adm_home")])
            text = (
                f"📦 <b>باقات دولة: {html.escape(country['name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "اضغط على أي باقة لتعديلها أو حذفها أو إدارة شرائح الـ eSIM الخاصة بها:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_plan_add:"):
            await q.answer()
            country_id = int(data.split(":")[1])
            context.user_data["state"] = "ADM_ADD_PLAN_NAME"
            context.user_data["temp"] = {"country_id": country_id}
            text = (
                "➕ <b>إضافة باقة جديدة (الخطوة 1 من 3)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>اسم الباقة</b> (مثال: <code>باقة 10 جيجابايت - 30 يوم</code>):"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data=f"adm_plans_country:{country_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_view:"):
            await q.answer()
            clear_state(context)
            plan_id = int(data.split(":")[1])
            await render_admin_plan_view(update, plan_id)
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
                f"✏️ <b>تعديل الباقة: {html.escape(plan['name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل <b>الاسم الجديد للباقة</b> (أو أرسل نقطة <code>.</code> للإبقاء على الاسم الحالي):"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data=f"adm_plan_view:{plan_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_plan_archive:") or data.startswith("adm_plan_unarchive:"):
            plan_id = int(data.split(":")[1])
            if data.startswith("adm_plan_unarchive:"):
                db.unarchive_plan(plan_id)
                await q.answer("👁 الباقة ظاهرة في المتجر الآن.")
            else:
                db.archive_plan(plan_id)
                await q.answer("🗄 تم إخفاء الباقة من المتجر — سجل المشتريات محفوظ.")
            plan = db.get_plan(plan_id)
            if plan:
                await render_admin_plan_view(update, plan_id)
            return

        if data.startswith("adm_plan_del_confirm:"):
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            country_id = plan["country_id"] if plan else 0
            if plan and db.count_sold_esims(plan_id) > 0:
                # Sold history appeared meanwhile: archive instead of deleting.
                db.archive_plan(plan_id)
                await q.answer("🗄 الباقة فيها مبيعات — تم إخفاؤها بدل الحذف.", show_alert=True)
                await render_admin_plan_view(update, plan_id)
                return
            db.delete_plan(plan_id)
            await q.answer("🗑️ تم حذف الباقة بنجاح!", show_alert=True)
            if country_id:
                await safe_edit_or_send(
                    update,
                    "✅ <b>تم حذف الباقة نهائياً.</b>",
                    InlineKeyboardMarkup([[IKB("🔙 العودة لباقات الدولة", callback_data=f"adm_plans_country:{country_id}")]]),
                )
            else:
                await show_admin_home(update, context)
            return

        if data.startswith("adm_plan_del:"):
            plan_id = int(data.split(":")[1])
            plan = db.get_plan(plan_id)
            if not plan:
                await q.answer("⚠️ الباقة غير موجودة.", show_alert=True)
                return
            sold_count = db.count_sold_esims(plan_id)
            if sold_count > 0:
                # Never delete purchase history: archive (hide) instead.
                db.archive_plan(plan_id)
                await q.answer("🗄 تم إخفاء الباقة — سجل المشتريات محفوظ.", show_alert=True)
                await safe_edit_or_send(
                    update,
                    f"🗄 <b>باقة «{html.escape(plan['name'])}» فيها {sold_count} شريحة مباعة.</b>\n"
                    "حذفها كان هيمسح سجل مشتريات العملاء، لذلك تم <b>إخفاؤها من المتجر</b> بدل الحذف.\n"
                    "المشتريات وسجلات العملاء محفوظة بالكامل، وتقدر تظهرها تاني في أي وقت.",
                    InlineKeyboardMarkup([
                        [IKB("👁 إظهار في المتجر", callback_data=f"adm_plan_unarchive:{plan_id}")],
                        [IKB("🔙 العودة لباقات الدولة", callback_data=f"adm_plans_country:{plan['country_id']}")],
                    ]),
                )
                return
            avail = int(plan.get("available_count") or 0)
            await q.answer()
            await safe_edit_or_send(
                update,
                f"🗑️ <b>تأكيد حذف باقة «{html.escape(plan['name'])}» نهائياً؟</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"سيتم حذف <b>{avail}</b> شريحة متاحة معها.\n"
                "✅ لا يوجد سجل مبيعات مرتبط — المشتريات لن تتأثر.",
                InlineKeyboardMarkup([
                    [IKB("✅ تأكيد الحذف النهائي", callback_data=f"adm_plan_del_confirm:{plan_id}", style="danger")],
                    [IKB("❌ إلغاء", callback_data=f"adm_plan_view:{plan_id}")],
                ]),
            )
            return

        # ---------------- ADMIN: eSIM INVENTORY (ADD / EDIT / DELETE) ----------------
        if data == "adm_inv_SelectCountry":
            await q.answer()
            clear_state(context)
            countries = db.get_countries()
            if not countries:
                await safe_edit_or_send(
                    update,
                    "⚠️ <b>لا توجد دول مضافة بعد. قم بإضافة دولة وباقة أولاً.</b>",
                    InlineKeyboardMarkup([[IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")]]),
                )
                return
            rows = [
                [
                    IKB(
                        f"🌍 {c['name']} (المتاح بالمخزون: {int(c['available_stock'] or 0)})",
                        callback_data=f"adm_inv_country:{c['id']}",
                    )
                ]
                for c in countries
            ]
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            await safe_edit_or_send(
                update,
                "📲 <b>إدارة مخزون الشرائح (eSIMs)</b>\n━━━━━━━━━━━━━━━━━━━━\nاختر الدولة لعرض باقاتها وإدارة شرائحها:",
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
                    f"⚠️ <b>لا توجد باقات في دولة {html.escape(country['name'] if country else '')}. أضف باقة أولاً.</b>",
                    InlineKeyboardMarkup([
                        [IKB("➕ إضافة باقة جديدة", callback_data=f"adm_plan_add:{country_id}")],
                        [IKB("🔙 رجوع", callback_data="adm_inv_SelectCountry")],
                    ]),
                )
                return
            rows = []
            for p in plans:
                rows.append([
                    IKB(
                        f"📦 {p['name']} (المتوفر: {int(p['available_count'] or 0)} شريحة)",
                        callback_data=f"adm_plan_view:{p['id']}",
                    )
                ])
            rows.append([IKB("🔙 اختيار دولة أخرى", callback_data="adm_inv_SelectCountry")])
            await safe_edit_or_send(
                update,
                f"📲 <b>اختر الباقة لإدارة مخزون الشرائح ({html.escape(country['name'])})</b>:",
                InlineKeyboardMarkup(rows),
            )
            return

        if data in ("adm_esim_add_done", "adm_esim_add_done_silent"):
            temp2 = context.user_data.get("temp", {})
            bid2 = temp2.get("batch_id")
            lock2 = context.bot_data.setdefault("esim_add_locks", {}).setdefault(bid2, asyncio.Lock()) if bid2 else None
            if lock2:
                async with lock2:
                    temp2["batch_closing"] = True
                    plan_id2 = int(temp2.get("plan_id") or 0)
                    mode2 = temp2.get("notify_mode", "all")
                    batch = int(temp2.get("batch_added") or 0)
            else:
                plan_id2 = int(temp2.get("plan_id") or 0)
                mode2 = temp2.get("notify_mode", "all")
                batch = int(temp2.get("batch_added") or 0)
            await q.answer()
            plan2 = db.get_plan(plan_id2) if plan_id2 else None
            clear_state(context)
            buttons2 = InlineKeyboardMarkup([
                [IKB("➕ إضافة شرائح أخرى", callback_data=f"adm_esim_add:{plan_id2}")],
                [IKB("📋 عرض المخزون", callback_data=f"adm_esim_list:{plan_id2}")],
                [IKB("🔙 العودة للباقة", callback_data=f"adm_plan_view:{plan_id2}")],
            ])
            if not plan2 or not batch:
                await safe_edit_or_send(update, "⚠️ لا توجد إضافة صور قيد التنفيذ.", buttons2)
                return
            if data == "adm_esim_add_done_silent" or mode2 == "silent":
                await safe_edit_or_send(
                    update,
                    f"✅ تمت إضافة {batch} شريحة QR إلى {html.escape(plan2['name'])}.\n🔕 بدون إشعارات.",
                    buttons2,
                )
                return
            await safe_edit_or_send(update, "✅ تمت إضافة الصور. جارٍ إرسال إشعارات التوفر...", None)
            sent, failed = await broadcast_new_inventory(context, plan_id2, batch, mode=mode2)
            label2 = "المنتظرين" if mode2 == "waitlist" else "المستخدمين والمنتظرين"
            await safe_edit_or_send(
                update,
                f"✅ تمت إضافة {batch} شريحة QR إلى {html.escape(plan2['name'])}.\n"
                f"🔔 تم إشعار {sent} من {label2} (تعذر إرسال {failed}).",
                buttons2,
            )
            return

        if data.startswith("adm_esim_add:"):
            await q.answer()
            plan_id = int(data.split(":")[1])
            await render_admin_add_esim(update, context, plan_id, "all")
            return

        if data.startswith("adm_esim_mode:"):
            parts = data.split(":")
            plan_id = int(parts[1])
            mode = parts[2] if len(parts) > 2 else "all"
            await q.answer("✅ تم تحديث وضع الإشعار")
            await render_admin_add_esim(update, context, plan_id, mode)
            return

        if data.startswith("adm_esim_list:"):
            await q.answer()
            clear_state(context)
            parts = data.split(":")
            plan_id = int(parts[1])
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            plan = db.get_plan(plan_id)
            if not plan:
                return
            esims = db.get_available_esims(plan_id)
            if not esims:
                text = (
                    f"📋 <b>مخزون الباقة: {html.escape(plan['name'])}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "⚠️ لا توجد شرائح متاحة حالياً في مخزون هذه الباقة."
                )
                kb = InlineKeyboardMarkup([
                    [IKB("➕ إضافة شريحة جديدة", callback_data=f"adm_esim_add:{plan_id}")],
                    [IKB("🔙 العودة للباقة", callback_data=f"adm_plan_view:{plan_id}")],
                ])
                await safe_edit_or_send(update, text, kb)
                return

            per_page = 20
            total_pages = max(1, (len(esims) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            page_esims = esims[page * per_page:(page + 1) * per_page]
            rows = []
            for e in page_esims:
                t_badge = "🔑 LPA" if e["item_type"] == "lpa" else "🖼 QR"
                short_code = (e["lpa_code"][:22] + "...") if e["lpa_code"] and len(e["lpa_code"]) > 22 else (e["lpa_code"] or "صورة QR")
                rows.append([
                    IKB(
                        f"#{e['id']} | {t_badge} | {short_code}",
                        callback_data=f"adm_esim_item:{e['id']}",
                    )
                ])
            if total_pages > 1:
                nav = []
                if page > 0:
                    nav.append(IKB("⬅️ السابق", callback_data=f"adm_esim_list:{plan_id}:{page - 1}"))
                nav.append(IKB(f"📄 {page + 1}/{total_pages}", callback_data=f"adm_esim_list:{plan_id}:{page}"))
                if page + 1 < total_pages:
                    nav.append(IKB("التالي ➡️", callback_data=f"adm_esim_list:{plan_id}:{page + 1}"))
                rows.append(nav)
            rows.append([IKB("➕ إضافة شريحة جديدة", callback_data=f"adm_esim_add:{plan_id}")])
            rows.append([IKB("🔙 العودة للباقة", callback_data=f"adm_plan_view:{plan_id}")])
            text = (
                f"📋 <b>الشرائح المتاحة في مخزون: {html.escape(plan['name'])}</b>\n"
                f"إجمالي المتاح: <b>{len(esims)}</b> شريحة.\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "👇 اضغط على أي شريحة لمعاينتها أو تعديلها أو حذفها من المخزون:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_esim_item:"):
            await q.answer()
            clear_state(context)
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("⚠️ الشريحة غير موجودة.", show_alert=True)
                return
            sold = esim.get("status") == "sold"
            status = "✅ مباعة" if sold else "🟢 متاحة بالمخزون"
            label = "كود نصي (LPA)" if esim["item_type"] == "lpa" else "صورة QR"
            extra = ""
            if sold:
                buyer = html.escape(esim.get("buyer_full_name") or "")
                username = "@" + html.escape(esim["buyer_username"]) if esim.get("buyer_username") else "بدون يوزر"
                refunds = db.get_esim_refunds(esim_id)
                refund_text = "لا توجد طلبات استرداد"
                if refunds:
                    refund_text = "، ".join(
                        f"#{r['id']} ({'قيد المراجعة' if r['status']=='pending' else 'تمت المراجعة'})"
                        for r in refunds
                    )
                extra = (
                    f"\n👤 <b>المشتري:</b> {buyer} ({username})\n"
                    f"🆔 <b>معرف المشتري:</b> <code>{esim.get('sold_to') or ''}</code>\n"
                    f"💵 <b>سعر البيع:</b> <code>{float(esim.get('sold_price') or 0):.2f}</code> {html.escape(currency)}\n"
                    f"🕒 <b>وقت البيع:</b> {esim.get('sold_at') or ''}\n"
                    f"↩️ <b>طلبات الاسترداد:</b> {refund_text}"
                )
            text = (
                f"🔎 <b>تفاصيل الشريحة #{esim['id']}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>الحالة:</b> {status}\n"
                f"🌍 <b>الدولة:</b> {html.escape(esim['country_name'])}\n"
                f"📦 <b>الباقة:</b> {html.escape(esim['plan_name'])}\n"
                f"🏷 <b>النوع:</b> {label}\n"
                f"🔑 <b>كود LPA:</b> <code>{html.escape(esim['lpa_code'] or NO_LPA_QR)}</code>\n"
                f"📝 <b>ملاحظات:</b> {html.escape(esim['notes'] or NONE_STR)}\n"
                f"📅 <b>تاريخ الإضافة:</b> {esim.get('created_at') or ''}{extra}"
            )
            rows = [[IKB("👁 معاينة الشريحة", callback_data=f"adm_esim_preview:{esim_id}")]]
            if sold:
                if esim.get("sold_to"):
                    rows.append([IKB("👤 ملف المشتري", callback_data=f"adm_user_view:{esim['sold_to']}")])
                rows.append([IKB("🧾 أحدث المبيعات", callback_data="adm_recent_sales:0"), IKB("🔍 مركز الفحص", callback_data="adm_inspector")])
            else:
                rows.append([IKB("✏️ تعديل الشريحة", callback_data=f"adm_esim_edit:{esim_id}"), IKB("🗑️ حذف الشريحة", callback_data=f"adm_esim_del:{esim_id}")])
                rows.append([IKB("🔙 العودة للمخزون", callback_data=f"adm_esim_list:{esim['plan_id']}")])
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_esim_preview:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            if not esim:
                await q.answer("⚠️ غير موجودة.", show_alert=True)
                return
            await q.answer("👁 جاري إرسال المعاينة...")
            await deliver_esim_to_chat(
                context=context,
                chat_id=user.id,
                esim=esim,
                header_title=f"👁 <b>معاينة الشريحة بالمخزون (#{esim['id']})</b>",
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
                f"✏️ <b>تعديل الشريحة رقم #{esim_id}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>كود الـ LPA الجديد</b> كنص، أو أرسل <b>صورة QR Code جديدة</b> لتحديث هذه الشريحة:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data=f"adm_esim_item:{esim_id}")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_esim_del:"):
            esim_id = int(data.split(":")[1])
            esim = db.get_esim(esim_id)
            plan_id = esim["plan_id"] if esim else 0
            db.delete_esim(esim_id)
            await q.answer("🗑️ تم حذف الشريحة من المخزون!", show_alert=True)
            if plan_id:
                await safe_edit_or_send(
                    update,
                    "✅ <b>تم حذف الشريحة من المخزون بنجاح.</b>",
                    InlineKeyboardMarkup([[IKB("🔙 العودة لقائمة الشرائح", callback_data=f"adm_esim_list:{plan_id}")]]),
                )
            return

        # ---------------- ADMIN: REFUND REQUESTS ----------------
        if data == "adm_refunds_menu":
            await q.answer()
            clear_state(context)
            pending = db.get_pending_refunds()
            rows = []
            for req in pending[:20]:
                rows.append([
                    IKB(
                        f"⏳ طلب #{req['id']} | {req['full_name'] or req['user_id']} | {req['country_name']} - {req['plan_name']} ({float(req['refund_amount'] or 0):.2f} {currency})",
                        callback_data=f"adm_refund_view:{req['id']}",
                    )
                ])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            text = (
                "↩️ <b>طلبات الاسترداد المعلقة</b>\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"العدد الحالي: <b>{len(pending)}</b> طلب\n\n"
                "اضغط على أي طلب لعرض فيديو الإثبات والتفاصيل والبت فيه:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_refund_view:"):
            await q.answer()
            req_id = int(data.split(":")[1])
            req = db.get_refund_request(req_id)
            if not req:
                return
            adm_kb = InlineKeyboardMarkup([
                [IKB("✅ تمت المراجعة (بدون تعديل رصيد)", callback_data=f"adm_refund_done:{req['id']}")],
                [IKB("🔙 العودة للطلبات", callback_data="adm_refunds_menu")],
            ])
            await send_refund_video_to_admin(context, req, markup=adm_kb)
            return

        if data.startswith("adm_refund_done:"):
            req_id = int(data.split(":")[1])
            ok, status, res = db.mark_refund_reviewed(req_id)
            if not ok:
                await q.answer("⚠️ تم معالجة هذا الطلب مسبقاً أو أنه غير موجود.", show_alert=True)
                return

            await q.answer("✅ تم تعيين الطلب كمتمة مراجعته.", show_alert=True)
            # Review-only: no balance is credited or debited here. Any compensation
            # remains a manual owner decision (e.g. the existing manual balance tool).
            user_msg = (
                f"📋 <b>تمت مراجعة طلب الاسترداد رقم #{req_id}.</b>\n"
                "سيتواصل معك الدعم الفني بخصوص نتيجة الطلب."
            )
            try:
                await context.bot.send_message(
                    chat_id=res["user_id"],
                    text=user_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
                )
            except TelegramError:
                pass

            await show_admin_home(update, context)
            return


        # ---------------- ADMIN: TOP-UPS & BALANCE ----------------
        if data == "adm_topups_menu":
            await q.answer()
            clear_state(context)
            pending = db.get_pending_topups()
            rows = [
                [IKB("➕ إضافة / خصم رصيد لمستخدم يدوياً", callback_data="adm_bal_manual")],
            ]
            for req in pending[:20]:
                m_icon = "🟡 Binance" if req["method"] == "binance" else "🟣 InstaPay"
                rows.append([
                    IKB(
                        f"⏳ طلب #{req['id']} | {m_icon} | {float(req['amount']):.2f} {currency}",
                        callback_data=f"adm_topup_view:{req['id']}",
                    )
                ])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            text = (
                "💳 <b>إدارة الأرصدة وطلبات الشحن</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ <b>طلبات الشحن المعلقة حالياً:</b> {len(pending)} طلب\n\n"
                "يمكنك تعديل رصيد أي مستخدم يدوياً أو مراجعة طلبات الشحن بالأسفل:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_topup_view:"):
            await q.answer()
            req_id = int(data.split(":")[1])
            req = db.get_topup_request(req_id)
            if not req:
                return
            m_name = "Binance Pay (عملات رقمية)" if req["method"] == "binance" else "InstaPay EG (إنستا باي)"
            text = (
                f"🧾 <b>تفاصيل طلب شحن الرصيد #{req['id']}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>العميل:</b> {html.escape(req['full_name'] or '')} (@{html.escape(req['username'] or NO_USER)})\n"
                f"🆔 <b>معرف العميل:</b> <code>{req['user_id']}</code>\n"
                f"💳 <b>الوسيلة:</b> {m_name}\n"
                f"💵 <b>المبلغ المطلوب:</b> <code>{float(req['amount']):.2f}</code> {html.escape(currency)}\n"
                f"📝 <b>بيانات التحويل المرسلة:</b> {html.escape(req['proof_text'] or ATTACHED_RECEIPT)}\n"
                f"📅 <b>التاريخ:</b> {req['created_at']}"
            )
            kb = InlineKeyboardMarkup([
                [
                    IKB("✅ قبول وإضافة الرصيد", callback_data=f"adm_topup_ok:{req['id']}"),
                    IKB("❌ رفض الطلب", callback_data=f"adm_topup_no:{req['id']}"),
                ],
                [IKB("🔙 العودة لقائمة الطلبات", callback_data="adm_topups_menu")],
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

        if data.startswith("adm_topup_cancel:"):
            await q.answer("تم الإلغاء — الطلب ما زال معلقًا.")
            return

        if data.startswith("adm_topup_confirm:"):
            _, req_id_s, tid = data.split(":", 2)
            req_id = int(req_id_s)
            req = db.get_topup_request(req_id)
            if not req or req["status"] != "pending":
                await q.answer("⚠️ تم معالجة هذا الطلب مسبقاً أو أنه غير موجود.", show_alert=True)
                return
            await q.answer("⏳ جاري إعادة التحقق والشحن...")
            cutoff_ms = int(db.get_setting("auto_topup_cutoff_ms", "0") or 0)
            try:
                tx2, _amb = await bp.find_pay_transaction(tid, min_time_ms=cutoff_ms)
                valid, reason, tx_amount, tx_currency, payer_name = bp.validate_topup_transaction(tx2, min_time_ms=cutoff_ms)
            except Exception as exc:
                logger.error("فشل إعادة التحقق من تحويل الطلب %s: %s", req_id, exc)
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⏸ الطلب <code>#{req_id}</code> ما زال معلقًا: تعذر الاتصال بـ Binance حالياً. لم يُضف أي رصيد.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if not valid:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⏸ الطلب <code>#{req_id}</code> ما زال معلقًا: التحويل لم يعد صالحًا (<code>{html.escape(reason)}</code>). لم يُضف أي رصيد.",
                    parse_mode=ParseMode.HTML,
                )
                return
            ok, status, res = db.approve_binance_topup_with_transfer(req_id, tid, tx_amount, tx_currency, payer_name)
            if not ok:
                reason_map = {"transfer_already_used": "هذا التحويل استُخدم بالفعل.", "already_processed": "الطلب عُولج مسبقاً."}
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⏸ الطلب <code>#{req_id}</code> ما زال معلقًا: {reason_map.get(status, html.escape(str(status)))} لم يُضف أي رصيد.",
                    parse_mode=ParseMode.HTML,
                )
                return
            try:
                await context.bot.send_message(
                    chat_id=res["user_id"],
                    text=(
                        "🎉 <b>تم قبول طلب شحن الرصيد الخاص بك!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"➕ <b>المبلغ المضاف:</b> <code>{db.fmt_money(res['amount'])}</code> {html.escape(currency)}\n"
                        f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(res['new_balance'])}</code> {html.escape(currency)}\n\n"
                        "شكراً لثقتك بنا! يمكنك الآن شراء شريحتك فوراً."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "✅ <b>تم الشحن اليدوي المؤكد عبر Binance Pay</b>\n"
                    f"🧾 الطلب: <code>#{req_id}</code> | العميل: <code>{res['user_id']}</code>\n"
                    f"💵 المبلغ المضاف: <code>{db.fmt_money(res['amount'])}</code> {html.escape(currency)}\n"
                    f"🧾 Transaction ID: <code>{html.escape(tid)}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if data.startswith("adm_topup_ok:") or data.startswith("adm_topup_no:"):
            approve = data.startswith("adm_topup_ok:")
            req_id = int(data.split(":")[1])

            if approve:
                req_check = db.get_topup_request(req_id)
                if req_check and req_check["status"] == "pending" and (req_check.get("method") or "").strip().lower() == "binance":
                    await q.answer("⏳ جاري التحقق من التحويل في سجل Binance...")
                    image_bytes = b""
                    if req_check.get("proof_file_id"):
                        try:
                            tg_file = await context.bot.get_file(req_check["proof_file_id"])
                            image_bytes = bytes(await tg_file.download_as_bytearray())
                        except Exception as exc:
                            logger.warning("تعذر تحميل إيصال الطلب اليدوي %s: %s", req_id, exc)
                    cutoff_ms = int(db.get_setting("auto_topup_cutoff_ms", "0") or 0)
                    try:
                        r_status, r_tx, r_reason = await bp.resolve_transfer_for_manual_approval(
                            req_check.get("proof_text") or "", image_bytes, cutoff_ms
                        )
                    except Exception as exc:
                        logger.error("فشل التحقق من تحويل الطلب اليدوي %s: %s", req_id, exc)
                        r_status, r_reason = "none", "connection_error"
                    if r_status != "found":
                        explain_map = {
                            "no_order_id": "لم نتمكن من قراءة رقم الطلب (Order ID) من الإيصال. اطلب من العميل إرسال رقم الطلب كنص.",
                            "not_found": "لا يوجد تحويل وارد في سجل Binance يطابق رقم الطلب الموجود في الإيصال.",
                            "ambiguous": "رقم الطلب يطابق أكثر من عملية في السجل.",
                            "multi": "أرقام الإيصال تطابق أكثر من عملية مختلفة في السجل.",
                            "already_used": "هذا التحويل تم استخدامه من قبل وأُضيف رصيده سابقاً.",
                            "outgoing": "العملية المطابقة صادرة وليست تحويلاً وارداً.",
                            "wrong_type": "العملية المطابقة ليست تحويلاً مباشراً (C2C).",
                            "too_old": "التحويل المطابق مر عليه أكثر من ساعة أو يسبق تفعيل الشحن التلقائي.",
                            "wrong_currency": "عملة التحويل المطابق ليست USDT.",
                            "connection_error": "تعذر الاتصال بـ Binance حالياً، حاول مرة أخرى بعد قليل.",
                        }
                        await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                "⏸ <b>طلب الشحن اليدوي ما زال معلقًا — لم يُضف أي رصيد</b>\n"
                                f"🧾 الطلب: <code>#{req_id}</code>\n"
                                f"السبب: {explain_map.get(r_reason, html.escape(str(r_reason)))}"
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                        return
                    tx_amount = db.to_decimal(r_tx.get("amount"))
                    tx_tid = str(r_tx.get("transactionId") or "")
                    tx_payer = ((r_tx.get("payerInfo") or {}).get("name") or "غير متاح")
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "🔎 <b>تأكيد الشحن اليدوي — تم العثور على التحويل</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"🧾 الطلب: <code>#{req_id}</code> | العميل طلب: <code>{db.fmt_money(req_check['amount'])}</code> {html.escape(currency)}\n"
                            f"💵 <b>المبلغ الفعلي الوارد:</b> <code>{db.fmt_money(tx_amount)}</code> {html.escape(str(r_tx.get('currency') or ''))}\n"
                            f"🆔 <b>Order ID:</b> <code>{html.escape(str(r_tx.get('orderId') or ''))}</code>\n"
                            f"🧾 <b>Transaction ID:</b> <code>{html.escape(tx_tid)}</code>\n"
                            f"👤 <b>المرسل:</b> {html.escape(tx_payer)}\n\n"
                            "سيُضاف <b>المبلغ الفعلي بالظبط</b> لرصيد العميل."
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [IKB(f"✅ تأكيد وشحن {db.fmt_money(tx_amount)}", callback_data=f"adm_topup_confirm:{req_id}:{tx_tid}")],
                            [IKB("❌ إلغاء (يبقى الطلب معلقًا)", callback_data=f"adm_topup_cancel:{req_id}")],
                        ]),
                    )
                    return

            ok, status, res = db.process_topup_request(req_id, approve=approve)
            if not ok:
                await q.answer("⚠️ تم معالجة هذا الطلب مسبقاً أو أنه غير موجود.", show_alert=True)
                return

            if approve:
                await q.answer("✅ تم قبول الطلب وإضافة الرصيد للعميل!", show_alert=True)
                user_msg = (
                    "🎉 <b>تم قبول طلب شحن الرصيد الخاص بك!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"➕ <b>المبلغ المضاف:</b> <code>{float(res['amount']):.2f}</code> {html.escape(currency)}\n"
                    f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(res['new_balance'])}</code> {html.escape(currency)}\n\n"
                    "شكراً لثقتك بنا! يمكنك الآن شراء شريحتك فوراً."
                )
            else:
                await q.answer("❌ تم رفض طلب الشحن.", show_alert=True)
                user_msg = (
                    "❌ <b>نعتذر، تم رفض طلب شحن الرصيد رقم #{req_id}.</b>\n"
                    f"المبلغ: {float(res['amount']):.2f} {html.escape(currency)}\n"
                    "يرجى التأكد من إيصال التحويل أو التواصل مع الدعم الفني."
                ).replace("{req_id}", str(req_id))

            try:
                await context.bot.send_message(
                    chat_id=res["user_id"],
                    text=user_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
                )
            except TelegramError:
                pass

            await show_admin_home(update, context)
            return

        if data == "adm_bal_manual":
            await q.answer()
            context.user_data["state"] = "ADM_MANUAL_BAL_USER"
            text = (
                "💰 <b>إضافة أو خصم رصيد لمستخدم</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>معرف المستخدم (User ID)</b> الذي تريد تعديل رصيده:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_topups_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        # ---------------- ADMIN: ANALYTICS, EXPORT & INSPECTOR ----------------
        if data == "adm_analytics":
            await q.answer()
            clear_state(context)
            a = db.get_revenue_analytics()
            top = "\n".join(
                f"{i}. {html.escape(x['country_name'])} — {html.escape(x['plan_name'])}: {x['sold_count']} مباعة، {float(x['revenue']):.2f} {html.escape(currency)}"
                for i, x in enumerate(a["top_plans"], 1)
            ) or "لا توجد مبيعات للباقات حتى الآن."
            text = (
                "📊 <b>تحليلات الإيرادات</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 اليوم: {a['today_sold']} شريحة — <code>{a['today_revenue']:.2f}</code> {html.escape(currency)}\n"
                f"📆 آخر 7 أيام: {a['week_sold']} — <code>{a['week_revenue']:.2f}</code> {html.escape(currency)}\n"
                f"🗓 آخر 30 يوماً: {a['month_sold']} — <code>{a['month_revenue']:.2f}</code> {html.escape(currency)}\n"
                f"💰 إجمالي المبيعات: {a['sold_esims']} — <code>{a['total_revenue']:.2f}</code> {html.escape(currency)}\n"
                f"💳 الشحن المقبول: {a['approved_topups_count']} — <code>{a['approved_topups_total']:.2f}</code> {html.escape(currency)}\n"
                f"  Binance Pay: <code>{a['binance_topups_total']:.2f}</code> | InstaPay: <code>{a['instapay_topups_total']:.2f}</code>\n"
                f"🏦 أرصدة المحافظ الحالية: <code>{db.fmt_money(a['total_user_balances'])}</code> {html.escape(currency)}\n"
                f"📲 المخزون المتاح: {a['available_esims']} | 🔔 تنبيهات التوفر: {a['active_stock_alerts']}\n\n"
                f"🏆 <b>أفضل الباقات:</b>\n{top}\n\n📥 <b>تصدير البيانات:</b>"
            )
            kb = InlineKeyboardMarkup([
                [IKB("📥 تصدير المبيعات CSV", callback_data="adm_export_csv:sales")],
                [IKB("📥 تصدير المخزون CSV", callback_data="adm_export_csv:inventory")],
                [IKB("📥 تصدير المستخدمين CSV", callback_data="adm_export_csv:users")],
                [IKB("🧾 أحدث المبيعات", callback_data="adm_recent_sales:0"), IKB("🔄 تحديث", callback_data="adm_analytics")],
                [IKB("🔙 لوحة الأدمن", callback_data="adm_home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_export_csv:"):
            kind = data.split(":")[1]
            await q.answer("📥 جاري تجهيز ملف CSV...")
            stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
            sio = io.StringIO()
            sio.write("\ufeff")
            writer = csv.writer(sio)
            if kind == "sales":
                data_rows = db.get_sales_export_rows()
                writer.writerow(["رقم الشريحة", "الدولة", "الباقة", "النوع", f"سعر البيع ({currency})", "معرف المشتري", "يوزر المشتري", "اسم المشتري", "وقت البيع"])
                for r in data_rows:
                    writer.writerow([r["esim_id"], r["country_name"], r["plan_name"], r["item_type"], r["sold_price"], r["buyer_id"], r["buyer_username"], r["buyer_full_name"], r["sold_at"]])
                filename=f"sales_{stamp}.csv"
            elif kind == "inventory":
                data_rows = db.get_inventory_export_rows()
                writer.writerow(["رقم الشريحة", "الحالة", "الدولة", "الباقة", "نوع الشريحة", "كود LPA", "ملاحظات", "معرف المشتري", "سعر البيع", "تاريخ الإضافة", "تاريخ البيع"])
                for r in data_rows:
                    writer.writerow([r["esim_id"], r["status"], r["country_name"], r["plan_name"], r["item_type"], r["lpa_code"], r["notes"], r["buyer_id"], r["sold_price"], r["created_at"], r["sold_at"]])
                filename=f"inventory_{stamp}.csv"
            else:
                data_rows = db.get_users_export_rows()
                writer.writerow(["معرف المستخدم", "اسم المستخدم", "الاسم", "الرصيد", "محظور", "عدد المشتريات", "إجمالي الإنفاق", "تاريخ التسجيل"])
                for r in data_rows:
                    writer.writerow([r["user_id"], r["username"], r["full_name"], r["balance"], "نعم" if r["is_banned"] else "لا", r["purchases_count"], r["total_spent"], r["created_at"]])
                filename=f"users_{stamp}.csv"
            bio=io.BytesIO(sio.getvalue().encode("utf-8"))
            bio.seek(0)
            await context.bot.send_document(chat_id=user.id, document=InputFile(bio, filename=filename), caption=f"✅ تم تجهيز التصدير ({len(data_rows)} سجل).")
            return

        if data == "adm_inspector":
            await q.answer()
            clear_state(context)
            text = "🔍 <b>مركز الفحص</b>\n━━━━━━━━━━━━━━━━━━━━\nابحث عن شريحة بالرقم، أو عميل بالمعرف / @username، أو استعرض أحدث المبيعات."
            kb = InlineKeyboardMarkup([
                [IKB("🔎 بحث عن شريحة #ID", callback_data="adm_lookup_esim")],
                [IKB("👤 بحث عن عميل", callback_data="adm_lookup_user")],
                [IKB("🧾 أحدث المبيعات", callback_data="adm_recent_sales:0")],
                [IKB("🔙 لوحة الأدمن", callback_data="adm_home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data in ("adm_lookup_esim", "adm_lookup_user"):
            await q.answer()
            context.user_data["state"] = "ADM_LOOKUP_ESIM" if data == "adm_lookup_esim" else "ADM_LOOKUP_USER"
            prompt = "أرسل رقم الشريحة مثل <code>#25</code>:" if data == "adm_lookup_esim" else "أرسل معرف العميل الرقمي أو اسم المستخدم مثل <code>@username</code>:"
            await safe_edit_or_send(update, "🔍 <b>بحث الإدارة</b>\n━━━━━━━━━━━━━━━━━━━━\n"+prompt, InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_inspector")]]))
            return

        if data.startswith("adm_recent_sales:"):
            await q.answer()
            clear_state(context)
            page=int(data.split(":")[1]) if data.split(":")[1].isdigit() else 0
            per=12
            items,total=db.get_recent_sold_esims(per,page*per)
            rows=[]
            for e in items:
                who=e.get("buyer_username") or e.get("buyer_full_name") or e.get("sold_to") or ""
                rows.append([IKB(f"#{e['id']} | {e['country_name']} - {e['plan_name']} | {float(e['sold_price'] or 0):.2f} {currency} ({who})",callback_data=f"adm_esim_item:{e['id']}")])
            pages=max(1,(total+per-1)//per)
            if pages>1:
                nav=[]
                if page: nav.append(IKB("⬅️ السابق",callback_data=f"adm_recent_sales:{page-1}"))
                nav.append(IKB(f"📄 {page+1}/{pages}",callback_data=f"adm_recent_sales:{page}"))
                if page+1<pages: nav.append(IKB("التالي ➡️",callback_data=f"adm_recent_sales:{page+1}"))
                rows.append(nav)
            rows.append([IKB("🔙 مركز الفحص",callback_data="adm_inspector")])
            text=f"🧾 <b>أحدث المبيعات ({total})</b>\n━━━━━━━━━━━━━━━━━━━━\nاضغط على عملية البيع لعرض تفاصيل الشريحة والمشتري."
            await safe_edit_or_send(update,text,InlineKeyboardMarkup(rows))
            return

        if data.startswith("adm_user_view:"):
            await q.answer()
            clear_state(context)
            target=db.find_user_admin(data.split(":")[1])
            if not target:
                await q.answer("⚠️ لم يتم العثور على العميل.",show_alert=True)
                return
            await render_admin_user_profile(update,target)
            return

        if data.startswith("adm_bal_user:"):
            await q.answer()
            uid=int(data.split(":")[1])
            target=db.get_user(uid)
            if not target: return
            context.user_data["state"]="ADM_MANUAL_BAL_AMOUNT"
            context.user_data["temp"]={"target_id":uid}
            text=f"💰 <b>تعديل رصيد العميل #{uid}</b>\nالرصيد الحالي: <code>{db.fmt_money(target['balance'])}</code> {html.escape(currency)}\nأرسل مبلغ الإضافة أو الخصم (مثال: <code>20</code> أو <code>-5</code>)."
            await safe_edit_or_send(update,text,InlineKeyboardMarkup([[IKB("🔙 إلغاء",callback_data=f"adm_user_view:{uid}")]]))
            return

        if data.startswith("adm_toggle_ban_view:"):
            uid=int(data.split(":")[1])
            if uid==ADMIN_ID:
                await q.answer("لا يمكن حظر مالك البوت.",show_alert=True); return
            banned=not db.is_user_banned(uid)
            db.set_user_ban(uid,banned)
            await q.answer("تم تحديث حالة الحظر.",show_alert=True)
            target=db.find_user_admin(str(uid))
            if target: await render_admin_user_profile(update,target)
            return

        # ---------------- ADMIN: USERS & BAN MANAGEMENT ----------------
        if data == "adm_users_menu":
            await q.answer()
            clear_state(context)
            stats = db.get_stats()
            banned_list = db.get_banned_users()
            rows = [
                [IKB("🚫 حظر مستخدم بواسطة الـ ID", callback_data="adm_ban_input")],
                [IKB("✅ فك حظر مستخدم بواسطة الـ ID", callback_data="adm_unban_input")],
            ]
            for b in banned_list[:15]:
                rows.append([
                    IKB(
                        f"🔓 فك حظر: {b['full_name'] or b['user_id']} ({b['user_id']})",
                        callback_data=f"adm_unban_direct:{b['user_id']}",
                    )
                ])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            text = (
                "👥 <b>إدارة المستخدمين والحظر</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>إجمالي المستخدمين:</b> {stats['total_users']}\n"
                f"🚫 <b>عدد المحظورين حالياً:</b> {stats['banned_users']}\n\n"
                "يمكنك حظر أو فك حظر أي مستخدم عبر الـ ID أو الضغط على اسم المحظور بالأسفل لفك حظره فوراً:"
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_ban_input":
            await q.answer()
            context.user_data["state"] = "ADM_BAN_USER"
            text = (
                "🚫 <b>حظر مستخدم من البوت</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>معرف المستخدم (User ID)</b> الذي تريد حظره نهائياً من استخدام البوت:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data == "adm_unban_input":
            await q.answer()
            context.user_data["state"] = "ADM_UNBAN_USER"
            text = (
                "✅ <b>فك حظر مستخدم</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>معرف المستخدم (User ID)</b> لإلغاء حظره:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_users_menu")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_unban_direct:"):
            target_id = int(data.split(":")[1])
            db.set_user_ban(target_id, False)
            await q.answer(f"✅ تم فك الحظر عن المستخدم {target_id}", show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="✅ <b>تم رفع الحظر عن حسابك! يمكنك الآن استخدام البوت بشكل طبيعي.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            await show_admin_home(update, context)
            return

        # ---------------- ADMIN: PUBLIC BROADCAST ----------------
        if data.startswith("adm_users_list:"):
            await q.answer()
            try:
                page = max(0, int(data.split(":")[1]))
            except (IndexError, ValueError):
                page = 0
            per_page = 10
            total_users = db.count_users()
            users_page = db.list_users_by_balance(offset=page * per_page, limit=per_page)
            text = (
                "📋 <b>قائمة المستخدمين (مرتبون حسب الرصيد)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 إجمالي المستخدمين: {total_users} | صفحة {page + 1}\n\n"
                "اختر مستخدماً لعرض رصيده ومشترياته كاملة:"
            )
            rows = []
            for u in users_page:
                name = u.get("full_name") or (f"@{u['username']}" if u.get("username") else str(u["user_id"]))
                mark = "🚫 " if u.get("is_banned") else ""
                label = (
                    f"{mark}{name} — {db.fmt_money(u.get('balance') or 0)} {currency} "
                    f"({u.get('purchases_count') or 0} شريحة)"
                )
                if len(label) > 60:
                    label = label[:57] + "..."
                rows.append([IKB(label, callback_data=f"adm_user_view:{u['user_id']}")])
            nav = []
            if page > 0:
                nav.append(IKB("⬅️ السابق", callback_data=f"adm_users_list:{page - 1}"))
            if (page + 1) * per_page < total_users:
                nav.append(IKB("➡️ التالي", callback_data=f"adm_users_list:{page + 1}"))
            if nav:
                rows.append(nav)
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_discounts" or data.startswith("adm_discount_off:"):
            if data.startswith("adm_discount_off:"):
                code_off = data.split(":", 1)[1]
                db.deactivate_discount_code(code_off)
                await q.answer(f"⛔ تم إيقاف الكود {code_off}.")
            else:
                await q.answer()
            codes = db.list_discount_codes()
            text = "🎟 <b>أكواد الخصم</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            rows = []
            if not codes:
                text += "لا توجد أكواد خصم بعد."
            for dc in codes:
                uses = str(dc["used_count"]) + (f"/{dc['max_uses']}" if dc["max_uses"] else "")
                status = "✅" if dc["active"] else "⛔"
                text += (
                    f"\n{status} <code>{html.escape(dc['code'])}</code> — خصم "
                    f"{float(dc['percent']):g}% (استُخدم {uses})"
                )
                if dc["active"]:
                    rows.append([IKB(f"🗑 إيقاف {dc['code']}", callback_data=f"adm_discount_off:{dc['code']}", style="danger")])
            rows.append([IKB("➕ إضافة كود جديد", callback_data="adm_discount_add", style="success")])
            rows.append([IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")])
            await safe_edit_or_send(update, text, InlineKeyboardMarkup(rows))
            return

        if data == "adm_discount_add":
            await q.answer()
            context.user_data["state"] = "ADM_DISCOUNT_ADD"
            context.user_data["temp"] = {}
            text = (
                "➕ <b>إضافة كود خصم جديد</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الكود ونسبة الخصم (والحد الأقصى للاستخدام اختيارياً) بالصيغة:\n"
                "<code>SAVE10 15</code> أو <code>SAVE10 15 100</code>\n\n"
                "الكود: حروف إنجليزية وأرقام فقط (2-20 خانة)."
            )
            await safe_edit_or_send(update, text, InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_discounts")]]))
            return

        if data == "adm_broadcast_start":
            await q.answer()
            context.user_data["state"] = "ADM_BROADCAST_MSG"
            text = (
                "📢 <b>إرسال رسالة جماعية عامة لجميع المستخدمين</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل الآن <b>الرسالة النصية</b> أو <b>الصورة مع الوصف</b> التي تريد إرسالها لجميع مستخدمي البوت:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_home")]])
            await safe_edit_or_send(update, text, kb)
            return

        if data == "adm_broadcast_cancel":
            await q.answer("❌ تم إلغاء الرسالة الجماعية.")
            temp.pop("bc_from_chat", None)
            temp.pop("bc_msg_id", None)
            await safe_edit_or_send(update, "❌ تم إلغاء الرسالة الجماعية — لم يتم إرسال أي شيء.", InlineKeyboardMarkup([[IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")]]))
            return

        if data == "adm_broadcast_confirm":
            from_chat = temp.pop("bc_from_chat", None)
            bc_id = temp.pop("bc_msg_id", None)
            if not from_chat or not bc_id:
                await q.answer("⚠️ انتهت صلاحية الطلب — أعد إرسال الرسالة.", show_alert=True)
                return
            await q.answer()
            users = db.get_all_active_users()
            progress = await q.message.reply_text(f"📢 جاري إرسال الرسالة الجماعية إلى {len(users)} مستخدم...")
            sent = 0
            failed = 0
            for u in users:
                try:
                    await _send_with_flood_retry(lambda uid=u["user_id"]: context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=from_chat,
                        message_id=bc_id,
                    ))
                    sent += 1
                    await asyncio.sleep(0.04)
                except (Forbidden, BadRequest, TelegramError):
                    failed += 1
            await progress.edit_text(
                f"✅ <b>اكتمل إرسال الرسالة الجماعية!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📨 <b>تم الإرسال بنجاح إلى:</b> {sent} مستخدم\n"
                f"⚠️ <b>فشل / قاموا بحظر البوت:</b> {failed} مستخدم",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")]]),
            )
            return

        # ---------------- ADMIN: SETTINGS (BINANCE / INSTAPAY / SUPPORT) ----------------
        if data == "adm_settings":
            await q.answer()
            clear_state(context)
            binance_id = db.get_setting("binance_id", "غير محدد")
            instapay = db.get_setting("instapay_address", "غير محدد")
            support = db.get_setting("support_username", "Support")
            curr = db.get_setting("currency", "USD")

            text = (
                "⚙️ <b>إعدادات الدفع والدعم الفني</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🟡 <b>معرف Binance ID:</b> <code>{html.escape(binance_id)}</code>\n"
                f"🟣 <b>عنوان InstaPay EG:</b> <code>{html.escape(instapay)}</code>\n"
                f"🛟 <b>يوزر الدعم الفني:</b> @{html.escape(support.lstrip('@'))}\n"
                f"💵 <b>عملة المتجر:</b> <code>{html.escape(curr)}</code>\n\n"
                "👇 اضغط على أي إعداد لتعديله مباشرة:"
            )
            kb = InlineKeyboardMarkup([
                [IKB("🟡 تعديل معرف Binance ID", callback_data="adm_set_key:binance_id")],
                [IKB("🟣 تعديل عنوان InstaPay EG", callback_data="adm_set_key:instapay_address")],
                [IKB("🛟 تعديل يوزر الدعم الفني", callback_data="adm_set_key:support_username")],
                [IKB("💵 تعديل رمز العملة (USD / EGP)", callback_data="adm_set_key:currency")],
                [IKB("🔙 لوحة تحكم الأدمن", callback_data="adm_home")],
            ])
            await safe_edit_or_send(update, text, kb)
            return

        if data.startswith("adm_set_key:"):
            await q.answer()
            key = data.split(":")[1]
            labels = {
                "binance_id": "معرف Binance ID الجديد",
                "instapay_address": "عنوان أو رقم InstaPay EG الجديد",
                "support_username": "يوزر الدعم الفني في تيليجرام (بدون @)",
                "currency": "رمز العملة (مثال: USD أو ج.م)",
            }
            context.user_data["state"] = "ADM_SET_SETTING"
            context.user_data["temp"] = {"key": key}
            text = (
                f"⚙️ <b>تحديث الإعداد: {labels.get(key, key)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "أرسل القيمة الجديدة الآن:"
            )
            kb = InlineKeyboardMarkup([[IKB("🔙 إلغاء", callback_data="adm_settings")]])
            await safe_edit_or_send(update, text, kb)
            return


# ==============================================================================
# Message Handler (Text & Photos for User Top-Ups & Admin Flows)
# ==============================================================================
async def relay_customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    try:
        header = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📩 <b>رسالة جديدة من عميل</b>\n"
                f"👤 {html.escape(user.full_name or 'بدون اسم')}"
                + (f" (@{user.username})" if user.username else "")
                + f"\n🆔 <code>{user.id}</code>\n\n"
                "↩️ للرد على العميل: اضغط «رد» على الرسالة المحوّلة أدناه ثم اكتب ردك."
            ),
            parse_mode=ParseMode.HTML,
        )
        copied = await context.bot.copy_message(
            chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
        )
        db.record_relay_message(header.message_id, user.id)
        db.record_relay_message(copied.message_id, user.id)
        await msg.reply_text("📩 وصلت رسالتك لفريق الدعم وسيتم الرد عليك في أقرب وقت.")
    except Exception as exc:
        logger.warning("relay to admin failed: %s", exc)


async def handle_admin_relay_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    customer_id = db.get_relay_customer(msg.reply_to_message.message_id)
    if not customer_id:
        await msg.reply_text("⚠️ لم يتم التعرف على العميل. اضغط «رد» على رسالة محوّلة من عميل ليتم إرسال ردك له.")
        return
    try:
        await context.bot.send_message(chat_id=customer_id, text="💬 <b>رد من الدعم:</b>", parse_mode=ParseMode.HTML)
        await context.bot.copy_message(chat_id=customer_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
        await msg.reply_text("✅ تم إرسال ردك إلى العميل.")
    except (Forbidden, BadRequest):
        await msg.reply_text("⚠️ تعذر إرسال الرد — العميل قد يكون حظر البوت.")


async def mirror_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE, label: str) -> None:
    """Copy customer activity (commands, menu presses, flow inputs, payment images) to the owner.
    Notification only: never changes the customer's flow."""
    msg = update.message
    user = update.effective_user
    if not msg or not user or is_admin(user.id):
        return
    try:
        header = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔔 <b>{html.escape(label)}</b>\n"
                f"👤 {html.escape(user.full_name or 'بدون اسم')}"
                + (f" (@{user.username})" if user.username else "")
                + f"\n🆔 <code>{user.id}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
        db.record_relay_message(header.message_id, user.id)
        if not (msg.text and msg.text.startswith("/")):
            copied = await context.bot.copy_message(
                chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
            db.record_relay_message(copied.message_id, user.id)
        else:
            body = await context.bot.send_message(chat_id=ADMIN_ID, text=msg.text[:500])
            db.record_relay_message(body.message_id, user.id)
    except Exception as exc:
        logger.warning("mirror to admin failed: %s", exc)


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_ban_guard(update):
        return
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    ensure_user_synced(user, context)
    state = context.user_data.get("state")
    temp = context.user_data.get("temp", {})
    currency = get_currency()

    # Bottom reply keyboard (persistent colored menu) presses.
    # Inside an active flow only an exact button text interrupts it.
    if msg.text:
        menu_action = match_reply_menu_action(msg.text, exact_only=bool(state))
        if menu_action:
            await mirror_to_owner(update, context, "ضغط زر قائمة من عميل")
            clear_state(context)
            if menu_action == "adm_home" and not is_admin(user.id):
                await show_home(update, context)
                return
            await route_reply_menu(update, context, menu_action)
            return

    # If no active state: relay customer messages to the owner (owner replies by
    # replying to the relayed message), then show main menu.
    if not state:
        if is_admin(user.id):
            await handle_admin_relay_reply(update, context)
        else:
            await relay_customer_message(update, context)
        await show_home(update, context)
        return


    # --------------------------------------------------------------------------
    # USER: DISCOUNT CODE ENTRY
    # --------------------------------------------------------------------------
    if state == "USER_DISCOUNT_CODE":
        if not msg.text:
            await msg.reply_text("أرسل كود الخصم كنص.")
            return
        plan_id = int(temp.get("plan_id") or 0)
        qty = int(temp.get("qty") or 1)
        code_in = msg.text.strip().upper()
        dc = db.get_discount_code(code_in)
        clear_state(context)
        if not dc:
            await msg.reply_text("⚠️ كود الخصم غير صالح أو منتهي.")
            await render_user_plan_view(update, context, user, plan_id, qty=qty)
            return
        context.user_data.setdefault("temp", {})["discount_code"] = code_in
        await msg.reply_text(
            f"✅ تم تطبيق كود الخصم <b>{html.escape(code_in)}</b> بنسبة {float(dc['percent']):g}%.",
            parse_mode=ParseMode.HTML,
        )
        await render_user_plan_view(update, context, user, plan_id, qty=qty)
        return

    # --------------------------------------------------------------------------
    # USER TOP-UP STATES
    # --------------------------------------------------------------------------
    if state == "USER_TOPUP_AUTO_AMOUNT":
        if not msg.text:
            await msg.reply_text("⚠️ يرجى إرسال قيمة المبلغ كأرقام فقط (مثال: 15).")
            return
        try:
            amount_dec = Decimal(msg.text.strip().replace(",", ".")).quantize(Decimal("0.01"))
            if amount_dec <= 0:
                raise ValueError()
        except (InvalidOperation, ValueError):
            await msg.reply_text("⚠️ قيمة غير صحيحة! يرجى إدخال رقم أكبر من صفر (مثال: <code>10</code>).", parse_mode=ParseMode.HTML)
            return
        amount = float(amount_dec)

        current_mode = bp.mode()
        if current_mode not in ("monitor", "merchant"):
            clear_state(context)
            await msg.reply_text(
                "⚠️ الشحن التلقائي غير متاح حالياً. استخدم وسيلة الدفع اليدوية من قائمة «💳 شحن الرصيد».",
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            return

        clear_state(context)
        wait_msg = await msg.reply_text("⏳ جاري تجهيز فاتورة الدفع لدى Binance Pay...")
        order_id, trade_no = db.create_binance_pay_order(user.id, amount, bp.order_currency())
        try:
            resp = await bp.create_order(trade_no, amount, f"eSIM Store TopUp {trade_no}")
        except Exception as exc:
            logger.error("Binance Pay create_order failed: %s", exc)
            resp = {}

        checkout_url = ""
        if isinstance(resp, dict) and resp.get("status") == "SUCCESS":
            data_obj = resp.get("data") or {}
            checkout_url = data_obj.get("checkoutUrl") or data_obj.get("universalUrl") or data_obj.get("qrcodeLink") or ""
            db.set_binance_pay_order_created(order_id, data_obj.get("prepayId") or "", checkout_url)

        if not checkout_url:
            db.set_binance_pay_order_failed(order_id)
            err_code = resp.get("code", "") if isinstance(resp, dict) else ""
            err_msg = resp.get("errorMessage", "") if isinstance(resp, dict) else ""
            logger.error("Binance Pay order creation error: %s %s", err_code, err_msg)
            await wait_msg.edit_text(
                "❌ <b>تعذر إنشاء فاتورة الدفع حالياً.</b>\n"
                "يرجى المحاولة مرة أخرى بعد قليل أو استخدام وسيلة دفع أخرى.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⚠️ <b>فشل إنشاء فاتورة Binance Pay</b>\n"
                            f"🆔 العميل: <code>{user.id}</code> | المبلغ: <code>{amount:.2f}</code>\n"
                            f"الخطأ: <code>{html.escape(str(err_code))} {html.escape(str(err_msg))[:200]}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
            return

        await wait_msg.edit_text(
            f"⚡ <b>فاتورة شحن رصيد بقيمة <code>{amount:.2f}</code> {html.escape(currency)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🧾 <b>رقم العملية:</b> <code>{trade_no}</code>\n\n"
            "اضغط زر <b>«ادفع الآن»</b> بالأسفل وأكمل الدفع من تطبيق Binance.\n"
            "✅ فور إتمام الدفع سيُضاف الرصيد إلى حسابك <b>تلقائياً</b> ويصلك إشعار.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IKB("🟡 ادفع الآن عبر Binance Pay", url=checkout_url)],
                [IKB("🏠 القائمة الرئيسية", callback_data="home")],
            ]),
        )
        return

    if state == "USER_TOPUP_AUTO_PROOF":
        file_obj = None
        if msg.photo:
            file_obj = msg.photo[-1]
        elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
            file_obj = msg.document
        if file_obj is None:
            await msg.reply_text(
                "📸 أرسل <b>صورة إيصال التحويل</b> (السكرين اللي فيه Order ID) هنا كصورة.",
                parse_mode=ParseMode.HTML,
            )
            return

        wait_msg = await msg.reply_text("⏳ جاري قراءة الإيصال والتحقق من التحويل في سجل Binance Pay...")
        try:
            tg_file = await file_obj.get_file()
            image_bytes = bytes(await tg_file.download_as_bytearray())
        except Exception as exc:
            logger.error("فشل تحميل صورة إيصال الشحن التلقائي: %s", exc)
            clear_state(context)
            await wait_msg.edit_text(
                "❌ <b>تعذر تحميل الصورة.</b> حاول إرسالها مرة أخرى.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            return

        try:
            order_ids = await bp.ocr_space_extract_order_ids(image_bytes)
        except Exception as exc:
            clear_state(context)
            if str(exc) == "ocr_not_configured":
                logger.error("OCR.space غير مكوّن: تعذر قراءة إيصال شحن تلقائي")
                await wait_msg.edit_text(
                    "❌ <b>خدمة قراءة الإيصالات غير متاحة حالياً.</b>\n"
                    "استخدم وسيلة الدفع اليدوية من قائمة «💳 شحن الرصيد» وسيُضاف رصيدك بعد المراجعة.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
                )
                if ADMIN_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=ADMIN_ID,
                            text=(
                                "⚠️ <b>محاولة شحن تلقائي وخدمة OCR غير مكوّنة</b>\n"
                                f"🆔 العميل: <code>{user.id}</code>"
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    except TelegramError:
                        pass
            else:
                logger.error("فشل OCR.space: %s", exc)
                await wait_msg.edit_text(
                    "❌ <b>تعذر قراءة الصورة حالياً.</b> حاول مرة أخرى بعد قليل.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
                )
            return

        if not order_ids:
            await wait_msg.edit_text(
                "❌ <b>لم نتمكن من قراءة رقم الطلب (Order ID) من الصورة.</b>\n"
                "أرسل صورة أوضح لإيصال التحويل يظهر فيها Order ID كاملاً.",
                parse_mode=ParseMode.HTML,
            )
            return

        cutoff_ms = int(db.get_setting("auto_topup_cutoff_ms", "0") or 0)
        matches: Dict[str, Any] = {}
        ambiguous = False
        try:
            for cand in order_ids:
                tx, amb = await bp.find_pay_transaction(cand, min_time_ms=cutoff_ms)
                if amb:
                    ambiguous = True
                    break
                if tx:
                    matches[str(tx.get("transactionId") or "")] = tx
        except Exception as exc:
            logger.error("فشل الاتصال بواجهة Binance: %s", exc)
            clear_state(context)
            await wait_msg.edit_text(
                "❌ <b>تعذر التحقق من العملية حالياً بسبب مشكلة اتصال.</b>\n"
                "حاول مرة أخرى بعد دقيقة، أو استخدم وسيلة الدفع اليدوية.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            return

        if ambiguous or len(matches) > 1:
            clear_state(context)
            await wait_msg.edit_text(
                "❌ <b>رقم الطلب في الصورة يطابق أكثر من عملية.</b>\n"
                "تواصل مع الدعم وسيتم إضافة رصيدك بعد المراجعة.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "🚫 <b>شحن تلقائي مرفوض: تطابق غير فريد</b>\n"
                            f"🆔 العميل: <code>{user.id}</code>\n"
                            f"🔢 أرقام مقروءة: <code>{html.escape(', '.join(order_ids))}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
            return

        if not matches:
            clear_state(context)
            await wait_msg.edit_text(
                "❌ <b>لم يتم العثور على تحويل وارد يطابق رقم الطلب في الصورة.</b>\n"
                "تأكد أن التحويل تم فعلاً إلى حساب المتجر، وأن الصورة لإيصال التحويل نفسه، ثم حاول مرة أخرى.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 محاولة جديدة", callback_data="u_topup")]]),
            )
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "🚫 <b>شحن تلقائي مرفوض: لا توجد عملية مطابقة</b>\n"
                            f"🆔 العميل: <code>{user.id}</code>\n"
                            f"🔢 أرقام مقروءة: <code>{html.escape(', '.join(order_ids))}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
            return

        tx = next(iter(matches.values()))
        valid, reason, tx_amount, tx_currency, payer_name = bp.validate_topup_transaction(tx, min_time_ms=cutoff_ms)
        if not valid:
            clear_state(context)
            if reason == "already_used":
                reject_text = "❌ <b>رقم العملية هذا تم استخدامه من قبل</b> وأُضيف رصيده سابقاً. لا يمكن استخدام نفس التحويل مرتين."
            elif reason == "outgoing":
                reject_text = "❌ <b>هذه عملية صادرة وليست تحويلاً وارداً</b> إلى حساب المتجر."
            elif reason == "wrong_type":
                reject_text = "❌ <b>نوع العملية غير مقبول.</b> يُقبل فقط التحويل المباشر (C2C) من حساب Binance آخر."
            elif reason == "too_old":
                reject_text = "❌ <b>هذا التحويل قديم.</b> يُقبل فقط التحويل الذي تم خلال الساعة الأخيرة. لو التحويل حصل من أكثر من ساعة، تواصل مع الدعم."
            elif reason == "wrong_currency":
                reject_text = (
                    f"❌ <b>عملة التحويل {html.escape(tx_currency)} غير مقبولة.</b> "
                    f"يجب التحويل بعملة <b>{html.escape(bp.order_currency())}</b> فقط."
                )
            else:
                reject_text = "❌ <b>لم يتم العثور على تحويل وارد يطابق رقم الطلب في الصورة.</b>"
            await wait_msg.edit_text(
                reject_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 محاولة جديدة", callback_data="u_topup")]]),
            )
            if ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "🚫 <b>محاولة شحن تلقائي مرفوضة</b>\n"
                            f"🆔 العميل: <code>{user.id}</code> | السبب: <code>{reason}</code>\n"
                            f"🧾 رقم العملية: <code>{html.escape(str(tx.get('transactionId') or ''))}</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
            return

        canonical_id = str(tx.get("transactionId") or "")
        ok, status, result = db.credit_topup_from_transaction(user.id, canonical_id, tx_amount, tx_currency, payer_name)
        clear_state(context)
        if not ok:
            await wait_msg.edit_text(
                "❌ <b>رقم العملية هذا تم استخدامه من قبل.</b> لا يمكن استخدام نفس التحويل مرتين.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("💳 وسائل الدفع", callback_data="u_topup")]]),
            )
            return

        new_balance = result["new_balance"]
        await wait_msg.edit_text(
            "🎉 <b>تم التحقق من التحويل وشحن رصيدك بنجاح!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>المبلغ المضاف:</b> <code>{db.fmt_money(tx_amount)}</code> {html.escape(currency)}\n"
            f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(new_balance)}</code> {html.escape(currency)}\n"
            f"🧾 <b>رقم العملية:</b> <code>{html.escape(canonical_id)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
        )
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⚡ <b>شحن تلقائي ناجح عبر Binance Pay</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>العميل:</b> {html.escape(user.full_name or '')} (@{html.escape(user.username or NO_USER)})\n"
                        f"🆔 <b>معرف العميل:</b> <code>{user.id}</code>\n"
                        f"💵 <b>المبلغ:</b> <code>{db.fmt_money(tx_amount)}</code> {html.escape(currency)}\n"
                        f"🧾 <b>رقم العملية:</b> <code>{html.escape(canonical_id)}</code>\n"
                        f"👤 <b>اسم المرسل:</b> {html.escape(payer_name or 'غير متاح')}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
        return

    if state == "USER_TOPUP_AMOUNT":
        if not msg.text:
            await msg.reply_text("⚠️ يرجى إرسال قيمة المبلغ كأرقام فقط (مثال: 15).")
            return
        try:
            amount = round(float(msg.text.strip().replace(",", ".")), 2)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await msg.reply_text("⚠️ قيمة غير صحيحة! يرجى إدخال رقم أكبر من صفر (مثال: <code>10</code>).", parse_mode=ParseMode.HTML)
            return

        temp["amount"] = amount
        context.user_data["temp"] = temp
        context.user_data["state"] = "USER_TOPUP_PROOF"

        await msg.reply_text(
            f"✅ <b>المبلغ المحدد للشحن:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n\n"
            "📸 <b>الخطوة الأخيرة:</b>\n"
            "أرسل الآن <b>صورة إيصال التحويل (Screenshot)</b> أو أرسل <b>رقم العملية المرجعي (Transaction ID / اسم المحول)</b> في رسالة:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🏠 إلغاء", callback_data="home")]]),
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
            await msg.reply_text("⚠️ يرجى إرسال صورة إيصال التحويل أو كتابة رقم العملية المرجعي.")
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
            f"✅ <b>تم إرسال طلب شحن الرصيد رقم #{req_id} بنجاح!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>المبلغ:</b> {amount:.2f} {html.escape(currency)}\n"
            "⏳ جاري مراجعة التحويل من قِبل الإدارة، وسيصل إليك إشعار فور إضافة الرصيد لحسابك.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
        )

        # Notify Admin immediately
        if ADMIN_ID:
            m_label = "🟡 Binance Pay" if method == "binance" else "🟣 InstaPay EG"
            adm_caption = (
                f"🔔 <b>طلب شحن رصيد جديد #{req_id}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>العميل:</b> {html.escape(user.full_name or '')} (@{html.escape(user.username or NO_USER)})\n"
                f"🆔 <b>معرف العميل:</b> <code>{user.id}</code>\n"
                f"💳 <b>طريقة الدفع:</b> {m_label}\n"
                f"💵 <b>المبلغ:</b> <code>{amount:.2f}</code> {html.escape(currency)}\n"
                f"📝 <b>التفاصيل/المرجع:</b> {html.escape(proof_text or ATTACHED_RECEIPT)}"
            )
            adm_kb = InlineKeyboardMarkup([
                [
                    IKB("✅ قبول وإضافة الرصيد", callback_data=f"adm_topup_ok:{req_id}"),
                    IKB("❌ رفض الطلب", callback_data=f"adm_topup_no:{req_id}"),
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
    # USER REFUND STATES
    # --------------------------------------------------------------------------
    if state == "USER_REFUND_REASON":
        if not msg.text:
            await msg.reply_text("⚠️ يرجى كتابة وصف مختصر للمشكلة كنص.")
            return
        temp["reason"] = msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "USER_REFUND_VIDEO"
        await msg.reply_text(
            "✅ <b>تم تسجيل وصف المشكلة.</b>\n\n"
            "2️⃣ <b>الخطوة الأخيرة (إلزامية):</b>\n"
            "🎬 أرسل الآن <b>فيديو إثبات يوضح كافة تفاصيل المشكلة</b> "
            "(مثل: تسجيل شاشة يظهر فيه كود الـ QR ورسالة الخطأ عند التفعيل).\n"
            "⚠️ <b>لن يتم قبول الطلب بدون فيديو.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🏠 إلغاء والعودة للرئيسية", callback_data="home")]]),
        )
        return

    if state == "USER_REFUND_VIDEO":
        video_file_id = ""
        video_type = "video"
        if msg.video:
            video_file_id = msg.video.file_id
            video_type = "video"
        elif msg.video_note:
            video_file_id = msg.video_note.file_id
            video_type = "video_note"
        elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
            video_file_id = msg.document.file_id
            video_type = "document"

        if not video_file_id:
            await msg.reply_text(
                "⚠️ <b>يجب إرسال فيديو إثبات للمشكلة.</b>\n"
                "الصور أو الرسائل النصية وحدها غير كافية. يرجى إرسال فيديو يوضح كافة تفاصيل المشكلة:",
                parse_mode=ParseMode.HTML,
            )
            return

        esim_id = int(temp.get("esim_id", 0))
        esim = db.get_esim(esim_id)
        if not esim or esim.get("sold_to") != user.id:
            clear_state(context)
            await msg.reply_text("⚠️ تعذر العثور على الشريحة المختارة. تم إلغاء الطلب.")
            return

        req_id = db.create_refund_request(
            user_id=user.id,
            esim_id=esim_id,
            reason=temp.get("reason", ""),
            video_file_id=video_file_id,
            video_type=video_type,
            refund_amount=float(esim.get("sold_price") or 0.0),
        )
        clear_state(context)

        if req_id is None:
            await msg.reply_text(
                "⚠️ <b>يوجد بالفعل طلب استرداد لهذه الشريحة.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
            )
            return

        await msg.reply_text(
            f"✅ <b>تم إرسال طلب الاسترداد رقم #{req_id} بنجاح!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"📲 <b>الشريحة:</b> #{esim_id} ({html.escape(esim['country_name'])} - {html.escape(esim['plan_name'])})\n"
            f"💵 <b>السعر المدفوع:</b> {float(esim.get('sold_price') or 0):.2f} {html.escape(currency)}\n"
            "🎬 <b>تم إرسال فيديو الإثبات إلى الإدارة.</b>\n"
            "⏳ طلبك قيد المراجعة لدى الإدارة، وسيتم إشعارك فور اكتمال المراجعة.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🏠 القائمة الرئيسية", callback_data="home")]]),
        )

        # Forward the video proof and request details to the owner
        req = db.get_refund_request(req_id)
        if req:
            adm_kb = InlineKeyboardMarkup([[
                IKB("✅ تمت المراجعة (بدون تعديل رصيد)", callback_data=f"adm_refund_done:{req_id}"),
            ]])
            await send_refund_video_to_admin(context, req, markup=adm_kb)
        return


    # --------------------------------------------------------------------------
    # ADMIN STATES (OWNER ONLY)
    # --------------------------------------------------------------------------
    if not is_admin(user.id):
        clear_state(context)
        return

    if state == "ADM_ADD_COUNTRY":
        if not msg.text:
            await msg.reply_text("⚠️ يرجى إرسال اسم الدولة كنص.")
            return
        cid = db.add_country(msg.text.strip())
        clear_state(context)
        if not cid:
            await msg.reply_text(
                "⚠️ هذه الدولة موجودة بالفعل!",
                reply_markup=InlineKeyboardMarkup([[IKB("🔙 العودة للدول", callback_data="adm_countries")]]),
            )
            return
        await msg.reply_text(
            f"✅ <b>تمت إضافة الدولة ({html.escape(msg.text.strip())}) بنجاح!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IKB("➕ إضافة باقة لهذه الدولة", callback_data=f"adm_plan_add:{cid}")],
                [IKB("🌍 العودة لقائمة الدول", callback_data="adm_countries")],
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
            "✅ <b>تم تحديث اسم الدولة بنجاح!</b>" if ok else "⚠️ تعذر تحديث الاسم.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🔙 العودة للدولة", callback_data=f"adm_country_view:{country_id}")]]),
        )
        return

    if state == "ADM_ADD_PLAN_NAME":
        if not msg.text:
            return
        temp["name"] = msg.text.strip()
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_ADD_PLAN_DESC"
        await msg.reply_text(
            "📝 <b>إضافة باقة جديدة (الخطوة 2 من 3)</b>\n"
            "أرسل الآن <b>وصف وتفاصيل الباقة</b> (مثال: <code>صلاحية 30 يوم - تدعم الجيل الخامس 5G - قابلة لنقطة الاتصال</code>) أو أرسل نقطة <code>.</code> لتخطي الوصف:",
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
            f"💵 <b>إضافة باقة جديدة (الخطوة 3 من 3)</b>\n"
            f"أرسل الآن <b>سعر الباقة ({html.escape(currency)})</b> كأرقام فقط (مثال: <code>12.5</code>):",
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
            await msg.reply_text("⚠️ يرجى إدخال سعر رقمي صحيح (مثال: 10 أو 14.99).")
            return
        country_id = int(temp["country_id"])
        plan_id = db.add_plan(country_id, temp["name"], temp.get("description", ""), price)
        clear_state(context)
        await msg.reply_text(
            f"✅ <b>تمت إضافة الباقة ({html.escape(temp['name'])}) بسعر {price:.2f} {html.escape(currency)} بنجاح!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IKB("➕ إضافة شرائح eSIM لهذه الباقة الآن", callback_data=f"adm_esim_add:{plan_id}")],
                [IKB("🔙 العودة لباقات الدولة", callback_data=f"adm_plans_country:{country_id}")],
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
            "📝 أرسل الآن <b>الوصف الجديد للباقة</b> (أو أرسل نقطة <code>.</code> للإبقاء على الوصف الحالي):",
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
            f"💵 أرسل الآن <b>السعر الجديد للباقة ({html.escape(currency)})</b> (أو أرسل نقطة <code>.</code> للإبقاء على السعر الحالي {float(plan['price']):.2f}):",
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
                await msg.reply_text("⚠️ يرجى إدخال سعر رقمي صحيح.")
                return
        db.update_plan(plan_id, temp["name"], temp["description"], price)
        clear_state(context)
        await msg.reply_text(
            "✅ <b>تم تحديث بيانات الباقة بنجاح!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🔙 العودة للباقة", callback_data=f"adm_plan_view:{plan_id}")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: ADD OR EDIT eSIM (LPA OR QR CODE + AUTO BROADCAST)
    # --------------------------------------------------------------------------
    if state == "ADM_ADD_ESIM":
        plan_id = int(temp["plan_id"])
        mode = temp.get("notify_mode", "all")
        plan = db.get_plan(plan_id)
        if not plan:
            clear_state(context)
            return
        added = skipped = 0
        if msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/")):
            batch_id = temp.get("batch_id")
            if temp.get("batch_closing") or not batch_id:
                await msg.reply_text("⚠️ دفعة الإضافة اتقفلت بالفعل. افتح «إضافة شرائح» من الباقة لبدء دفعة جديدة.")
                return
            file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
            fuid = (msg.photo[-1].file_unique_id if msg.photo else getattr(msg.document, "file_unique_id", "")) or ""
            dup = db.find_esim_by_qr_unique(fuid)
            if dup:
                dplan = db.get_plan(int(dup["plan_id"]))
                await msg.reply_text(
                    f"⚠️ هذه الصورة مضافة مسبقاً كشريحة رقم {dup['id']}"
                    + (f" في باقة «{html.escape(dplan['name'])}»" if dplan else "")
                    + ". لم تتم إضافتها مرة أخرى."
                )
                return
            tg_file = await context.bot.get_file(file_id)
            local_name = f"qr_plan{plan_id}_{fuid or file_id[-16:]}.png"
            local_path = str(QR_DIR / local_name)
            await tg_file.download_to_drive(custom_path=local_path)
            # Serialize with Done under the same per-batch lock; recheck that this
            # photo still belongs to the live batch (not closed / not replaced by
            # a new batch for this or another plan) before inserting.
            lock = context.bot_data.setdefault("esim_add_locks", {}).setdefault(batch_id, asyncio.Lock())
            async with lock:
                temp_now = context.user_data.get("temp", {})
                batch_live = (
                    context.user_data.get("state") == "ADM_ADD_ESIM"
                    and temp_now.get("batch_id") == batch_id
                    and not temp_now.get("batch_closing")
                    and int(temp_now.get("plan_id") or 0) == plan_id
                )
                if not batch_live:
                    new_esim_id = None
                    rejected_closed = True
                else:
                    caption = (msg.caption or "").strip()
                    lpa = caption if caption.upper().startswith("LPA:") else ""
                    notes = "" if lpa else caption
                    new_esim_id = db.add_esim(plan_id, "qr", lpa_code=lpa, qr_file_id=file_id, qr_local_path=local_path, notes=notes, qr_unique_id=fuid)
                    rejected_closed = False
                    if new_esim_id is not None:
                        batch = int(temp_now.get("batch_added") or 0) + 1
                        temp_now["batch_added"] = batch
            if new_esim_id is None:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                if rejected_closed:
                    await msg.reply_text("⚠️ دفعة الإضافة اتقفلت أو اتغيّرت أثناء الرفع. الصورة لم تُضف — افتح «إضافة شرائح» من الباقة وأعد إرسالها.")
                else:
                    await msg.reply_text("⚠️ هذه الصورة مضافة مسبقاً. لم تتم إضافتها مرة أخرى.")
                return
            # Stay in ADM_ADD_ESIM state so more photos / album items keep adding.
            done_kb = InlineKeyboardMarkup([
                [IKB("✅ إنهاء الإضافة", callback_data="adm_esim_add_done", style="success")],
                [IKB("🔕 إنهاء بدون إشعارات", callback_data="adm_esim_add_done_silent")],
            ])
            progress_text = (
                f"📥 تمت إضافة <b>{batch}</b> صورة QR حتى الآن إلى <b>{html.escape(plan['name'])}</b>.\n"
                "أرسل المزيد من الصور الآن، أو اضغط إنهاء."
            )
            prog_id = temp.get("batch_progress_id")
            if prog_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=msg.chat_id, message_id=prog_id,
                        text=progress_text, parse_mode=ParseMode.HTML, reply_markup=done_kb,
                    )
                    return
                except (BadRequest, TelegramError):
                    pass
            prog = await msg.reply_text(progress_text, parse_mode=ParseMode.HTML, reply_markup=done_kb)
            context.user_data["temp"]["batch_progress_id"] = prog.message_id
            return
        elif msg.text:
            codes = [line.strip() for line in msg.text.splitlines() if line.strip()]
            added, skipped = db.add_lpa_esims_bulk(plan_id, codes)
        else:
            await msg.reply_text("⚠️ يرجى إرسال أكواد LPA أو صورة QR.")
            return
        clear_state(context)
        buttons = InlineKeyboardMarkup([
            [IKB("➕ إضافة شرائح أخرى", callback_data=f"adm_esim_add:{plan_id}")],
            [IKB("📋 عرض المخزون", callback_data=f"adm_esim_list:{plan_id}")],
            [IKB("🔙 العودة للباقة", callback_data=f"adm_plan_view:{plan_id}")],
        ])
        dup = f"\nℹ️ تم تجاهل {skipped} كود مكرر." if skipped else ""
        if not added:
            await msg.reply_text(f"⚠️ لم تتم إضافة شرائح جديدة؛ الأكواد المرسلة مكررة ({skipped}).", reply_markup=buttons)
            return
        if mode == "silent":
            await msg.reply_text(f"✅ تمت إضافة {added} شريحة إلى {html.escape(plan['name'])}.{dup}\n🔕 تمت الإضافة دون إرسال إشعارات.", parse_mode=ParseMode.HTML, reply_markup=buttons)
            return
        progress = await msg.reply_text("✅ تمت إضافة الشرائح. جارٍ إرسال إشعارات التوفر...", parse_mode=ParseMode.HTML)
        sent, failed = await broadcast_new_inventory(context, plan_id, added, mode=mode)
        label = "المنتظرين" if mode == "waitlist" else "المستخدمين والمنتظرين"
        await progress.edit_text(
            f"✅ تمت إضافة {added} شريحة إلى {html.escape(plan['name'])}.{dup}\n"
            f"🔔 تم إشعار {sent} من {label} (تعذر إرسال {failed}).",
            parse_mode=ParseMode.HTML, reply_markup=buttons,
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
            await msg.reply_text("⚠️ يرجى إرسال كود LPA نصي أو صورة QR Code.")
            return

        clear_state(context)
        await msg.reply_text(
            f"✅ <b>تم تحديث بيانات الشريحة #{esim_id} بنجاح!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IKB("🔎 عرض الشريحة بعد التعديل", callback_data=f"adm_esim_item:{esim_id}")],
                [IKB("📋 العودة لقائمة الشرائح", callback_data=f"adm_esim_list:{plan_id}")],
            ]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: MANUAL BALANCE ADJUSTMENT
    # --------------------------------------------------------------------------
    if state == "ADM_MANUAL_BAL_USER":
        if not msg.text or not msg.text.strip().isdigit():
            await msg.reply_text("⚠️ يرجى إرسال معرف المستخدم (User ID) كأرقام فقط.")
            return
        target_id = int(msg.text.strip())
        target_user = db.get_user(target_id)
        if not target_user:
            db.upsert_user(target_id, "", "مستخدم")
            target_user = db.get_user(target_id)
        temp["target_id"] = target_id
        context.user_data["temp"] = temp
        context.user_data["state"] = "ADM_MANUAL_BAL_AMOUNT"
        await msg.reply_text(
            f"👤 <b>المستخدم:</b> <code>{target_id}</code> ({html.escape(target_user.get('full_name') or '')})\n"
            f"💰 <b>رصيده الحالي:</b> <code>{db.fmt_money(target_user['balance'])}</code> {html.escape(currency)}\n\n"
            "أرسل الآن <b>المبلغ المراد إضافته أو خصمه</b>:\n"
            "• لإضافة رصيد أرسل الرقم مباشرة (مثال: <code>20</code>)\n"
            "• لخصم رصيد ضع علامة سالب قبل الرقم (مثال: <code>-10</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "ADM_MANUAL_BAL_AMOUNT":
        if not msg.text:
            return
        try:
            delta = round(float(msg.text.strip().replace(",", ".")), 2)
        except ValueError:
            await msg.reply_text("⚠️ يرجى إرسال رقم صحيح (مثال: 20 أو -5).")
            return
        target_id = int(temp["target_id"])
        new_bal = db.update_user_balance(target_id, delta)
        clear_state(context)
        await msg.reply_text(
            f"✅ <b>تم تحديث رصيد المستخدم <code>{target_id}</code> بنجاح!</b>\n"
            f"💰 <b>الرصيد الجديد:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IKB("👤 عرض ملف العميل", callback_data=f"adm_user_view:{target_id}")],
                [IKB("🔙 العودة لإدارة الأرصدة", callback_data="adm_topups_menu")],
            ]),
        )
        try:
            action_word = f"إضافة {delta:.2f}" if delta >= 0 else f"خصم {abs(delta):.2f}"
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🔔 <b>إشعار تحديث رصيد حسابك</b>\n"
                    f"تم {action_word} {html.escape(currency)} في حسابك بواسطة الإدارة.\n"
                    f"💰 <b>رصيدك الحالي:</b> <code>{float(new_bal or 0):.2f}</code> {html.escape(currency)}"
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
            await msg.reply_text("⚠️ يرجى إرسال معرف المستخدم (User ID) كأرقام فقط.")
            return
        target_id = int(msg.text.strip())
        if target_id == ADMIN_ID:
            await msg.reply_text("⚠️ لا يمكنك حظر حساب مالك البوت!")
            return
        ban_flag = state == "ADM_BAN_USER"
        db.set_user_ban(target_id, ban_flag)
        clear_state(context)
        status_str = "🚫 تم حظر المستخدم" if ban_flag else "✅ تم فك الحظر عن المستخدم"
        await msg.reply_text(
            f"<b>{status_str} (<code>{target_id}</code>) بنجاح!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🔙 العودة لإدارة المستخدمين", callback_data="adm_users_menu")]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: PUBLIC BROADCAST MESSAGE TO ALL USERS
    # --------------------------------------------------------------------------
    if state == "ADM_BROADCAST_MSG":
        bc_chat = msg.chat_id
        bc_id = msg.message_id
        clear_state(context)
        context.user_data["temp"] = {"bc_from_chat": bc_chat, "bc_msg_id": bc_id}
        users = db.get_all_active_users()
        await msg.reply_text(
            "📢 <b>تأكيد الرسالة الجماعية</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"سيتم إرسال رسالتك الأخيرة إلى <b>{len(users)}</b> مستخدم.\n"
            "هل أنت متأكد من الإرسال؟",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                IKB("✅ تأكيد الإرسال للجميع", callback_data="adm_broadcast_confirm", style="success"),
                IKB("❌ إلغاء", callback_data="adm_broadcast_cancel", style="danger"),
            ]]),
        )
        return

    # --------------------------------------------------------------------------
    # ADMIN: UPDATE SETTINGS
    # --------------------------------------------------------------------------
    if state == "ADM_DISCOUNT_ADD":
        if not msg.text:
            return
        parts = msg.text.strip().split()
        if len(parts) < 2:
            await msg.reply_text("⚠️ الصيغة: <code>الكود النسبة [الحد الأقصى]</code> — مثال <code>SAVE10 15</code>", parse_mode=ParseMode.HTML)
            return
        code_new = parts[0].upper()
        if not re.fullmatch(r"[A-Z0-9]{2,20}", code_new):
            await msg.reply_text("⚠️ الكود يجب أن يكون حروفاً إنجليزية وأرقاماً فقط (2-20 خانة).")
            return
        try:
            pct = Decimal(parts[1])
            if pct <= 0 or pct > 100:
                raise ValueError()
        except (InvalidOperation, ValueError):
            await msg.reply_text("⚠️ نسبة الخصم يجب أن تكون رقماً بين 0 و 100.")
            return
        max_uses = None
        if len(parts) > 2:
            if not parts[2].isdigit() or int(parts[2]) < 1:
                await msg.reply_text("⚠️ الحد الأقصى للاستخدام يجب أن يكون رقماً صحيحاً أكبر من صفر.")
                return
            max_uses = int(parts[2])
        created = db.create_discount_code(code_new, pct, max_uses)
        clear_state(context)
        if created:
            await msg.reply_text(
                f"✅ تم إنشاء كود الخصم <code>{html.escape(code_new)}</code> بنسبة {pct:g}%"
                + (f" (حد أقصى {max_uses} استخدام)." if max_uses else "."),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[IKB("🎟 أكواد الخصم", callback_data="adm_discounts")]]),
            )
        else:
            await msg.reply_text("⚠️ هذا الكود موجود مسبقاً أو غير صالح.")
        return

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
            f"✅ <b>تم تحديث الإعداد بنجاح إلى:</b> <code>{html.escape(val)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[IKB("🔙 العودة للإعدادات", callback_data="adm_settings")]]),
        )
        return


    if state == "ADM_LOOKUP_ESIM":
        if not msg.text:
            await msg.reply_text("أرسل رقم الشريحة مثل #15.")
            return
        raw=msg.text.strip().lstrip("#").strip()
        if not raw.isdigit():
            await msg.reply_text("⚠️ رقم غير صالح. أرسل رقماً صحيحاً مثل 15.")
            return
        esim=db.get_esim(int(raw))
        if not esim:
            await msg.reply_text("⚠️ لم يتم العثور على الشريحة. أرسل رقماً آخر أو ألغِ البحث.",reply_markup=InlineKeyboardMarkup([[IKB("🔙 مركز الفحص",callback_data="adm_inspector")]]))
            return
        clear_state(context)
        # Reuse the standard detail view through a synthetic callback query.
        text=f"🔎 <b>الشريحة #{esim['id']}</b> — {html.escape(esim['country_name'])} / {html.escape(esim['plan_name'])}\nالحالة: {'مباعة' if esim.get('status')=='sold' else 'متاحة'}\nكود LPA: <code>{html.escape(esim.get('lpa_code') or NO_LPA_QR)}</code>"
        rows=[[IKB("👁 معاينة",callback_data=f"adm_esim_preview:{esim['id']}")]]
        rows.append([IKB("🧾 عرض كامل التفاصيل",callback_data=f"adm_esim_item:{esim['id']}")])
        if esim.get("sold_to"):
            rows.append([IKB("👤 ملف المشتري",callback_data=f"adm_user_view:{esim['sold_to']}")])
        rows.append([IKB("🔍 بحث آخر",callback_data="adm_inspector")])
        await msg.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
        return

    if state == "ADM_LOOKUP_USER":
        if not msg.text:
            await msg.reply_text("أرسل معرف العميل أو @username.")
            return
        target=db.find_user_admin(msg.text.strip())
        if not target:
            await msg.reply_text("⚠️ لم يتم العثور على العميل. تحقق من المعرف أو أرسل محاولة أخرى.",reply_markup=InlineKeyboardMarkup([[IKB("🔙 مركز الفحص",callback_data="adm_inspector")]]))
            return
        clear_state(context)
        await render_admin_user_profile(update,target)
        return


# ==============================================================================
# Application Entrypoint
# ==============================================================================
def main() -> None:
    db.init_db()
    if not BOT_TOKEN:
        raise RuntimeError("خطأ: لم يتم ضبط BOT_TOKEN في ملف .env! قم بتشغيل ./install.sh أو تعديل ملف .env")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).post_init(bp.post_init_hook).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))

    logger.info("✅ تم تشغيل بوت متجر شرائح eSIM بنجاح (يعمل على مدار الساعة 24/7)...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
