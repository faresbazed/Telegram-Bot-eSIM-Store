# -*- coding: utf-8 -*-
"""
ربط الدفع التلقائي عبر Binance Pay — بنظامين (يُختار من ملف .env):

1) وضع التحقق برقم العملية (BINANCE_PAY_MODE=monitor):
   مفتاح API عادي بصلاحية قراءة فقط على حساب باينانس الشخصي. العميل يحوّل
   إلى Binance ID الخاص بالمتجر ثم يُرسل للبوت المبلغ ورقم العملية
   (Transaction ID) الظاهر في تفاصيل التحويل بتطبيق Binance. البوت يبحث عن
   الرقم في سجل التحويلات (GET /sapi/v1/pay/transactions) ويتأكد أنه تحويل
   وارد حقيقي بنفس المبلغ والعملة وأنه لم يُستخدم من قبل، ثم يضيف الرصيد.
   رقم العملية نفسه (P_...) يظهر للمرسل والمستلم، فالتحقق مضمون.

2) وضع التاجر (BINANCE_PAY_MODE=merchant):
   إنشاء فاتورة دفع عبر Binance Pay Merchant API + خادم Webhook يستقبل
   إشعار "تم الدفع" ويضيف الرصيد فوراً. يتطلب حساب تاجر مفعّل (KYB).

لا يتم حفظ أو طباعة أي مفاتيح سرية؛ كل البيانات تُقرأ من ملف .env عبر config.py.
"""
import asyncio
import hashlib
from decimal import Decimal, InvalidOperation
import hmac
import io
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

import config
import database as db

logger = logging.getLogger(__name__)

# ----------------- Merchant mode (Binance Pay Merchant API) -----------------
PAY_API_BASE = "https://bpay.binanceapi.com"
CREATE_ORDER_PATH = "/binancepay/openapi/v2/order"
WEBHOOK_PATH = "/binance/webhook"

# ----------------- Monitor mode (regular account, read-only Spot API) -------
SPOT_API_BASE = "https://api.binance.com"
PAY_TRANSACTIONS_PATH = "/sapi/v1/pay/transactions"

# OCR.space receipt reading (external image processing; only used after owner consent)
OCR_API_URL = "https://api.ocr.space/parse/image"
OCR_MAX_BYTES = 950_000  # free tier rejects files over ~1MB

# طلب المالك (2026-11-24): لا يُقبل أي تحويل مر عليه أكثر من ساعة لحظة المطالبة.
MAX_TRANSFER_AGE_MS = 60 * 60 * 1000

_runner: Optional[web.AppRunner] = None


def mode() -> Optional[str]:
    """يرجع 'merchant' أو 'monitor' حسب الإعدادات، أو None إن لم يُفعَّل أي وضع."""
    m = (config.BINANCE_PAY_MODE or "").strip().lower()
    if m == "merchant" and (
        config.BINANCE_PAY_API_KEY
        and config.BINANCE_PAY_SECRET_KEY
        and config.BINANCE_PAY_WEBHOOK_URL
    ):
        return "merchant"
    if m == "monitor" and (config.BINANCE_API_KEY and config.BINANCE_API_SECRET):
        return "monitor"
    return None


def is_enabled() -> bool:
    return mode() is not None


def order_currency() -> str:
    return (config.BINANCE_PAY_CURRENCY or "USDT").strip() or "USDT"


# ==============================================================================
# وضع التاجر: التوقيع + إنشاء الفاتورة + Webhook
# ==============================================================================
def _pay_sign(payload: str) -> str:
    return (
        hmac.new(
            config.BINANCE_PAY_SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha512,
        )
        .hexdigest()
        .upper()
    )


def _pay_request_headers(body: str) -> Dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": config.BINANCE_PAY_API_KEY,
        "BinancePay-Signature": _pay_sign(f"{timestamp}\n{nonce}\n{body}\n"),
    }


async def create_order(merchant_trade_no: str, amount: float, description: str) -> Dict[str, Any]:
    """إنشاء فاتورة دفع لدى Binance Pay وإرجاع رد المنصة كاملاً."""
    body = json.dumps(
        {
            "env": {"terminalType": "WEB"},
            "merchantTradeNo": merchant_trade_no,
            "orderAmount": f"{float(amount):.2f}",
            "currency": order_currency(),
            "description": description[:128],
            "webhookUrl": config.BINANCE_PAY_WEBHOOK_URL,
        },
        ensure_ascii=False,
    )
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            PAY_API_BASE + CREATE_ORDER_PATH,
            data=body,
            headers=_pay_request_headers(body),
        ) as resp:
            return await resp.json(content_type=None)


def verify_notification(headers, raw_body: bytes) -> bool:
    """التحقق من أن الإشعار قادم فعلاً من Binance (توقيع HMAC-SHA512)."""
    timestamp = headers.get("BinancePay-Timestamp", "")
    nonce = headers.get("BinancePay-Nonce", "")
    signature = headers.get("BinancePay-Signature", "")
    if not (timestamp and nonce and signature):
        return False
    body = raw_body.decode("utf-8", errors="replace")
    expected = _pay_sign(f"{timestamp}\n{nonce}\n{body}\n")
    return hmac.compare_digest(expected, signature.upper())


# ==============================================================================
# وضع التاجر: خادم الـ Webhook
# ==============================================================================
async def _notify_credited(bot, user_id: int, amount: float, new_balance: float, reference: str) -> None:
    from telegram.constants import ParseMode
    from telegram.error import TelegramError

    currency = db.get_setting("currency", "USD")
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>تم شحن رصيدك تلقائياً بنجاح!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 <b>المبلغ المضاف:</b> <code>{db.fmt_money(amount)}</code> {currency}\n"
                f"💰 <b>رصيدك الحالي:</b> <code>{db.fmt_money(new_balance)}</code> {currency}\n\n"
                "يمكنك الآن شراء شرائح eSIM مباشرة من القائمة الرئيسية."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.warning("تعذر إشعار العميل %s بالشحن التلقائي: %s", user_id, exc)

    if config.ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=config.ADMIN_ID,
                text=(
                    "⚡ <b>شحن تلقائي عبر Binance Pay</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"🧾 <b>المرجع:</b> <code>{reference}</code>\n"
                    f"🆔 <b>معرف العميل:</b> <code>{user_id}</code>\n"
                    f"💵 <b>المبلغ:</b> <code>{db.fmt_money(amount)}</code> {currency}\n"
                    f"💰 <b>رصيده الحالي:</b> <code>{db.fmt_money(new_balance)}</code> {currency}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            logger.warning("تعذر إشعار المالك بالشحن التلقائي: %s", exc)


def build_webhook_app(bot) -> web.Application:
    async def handle_webhook(request: web.Request) -> web.Response:
        raw = await request.read()
        if not verify_notification(request.headers, raw):
            logger.warning("إشعار Binance Pay بتوقيع غير صالح من %s", request.remote)
            return web.json_response(
                {"returnCode": "FAIL", "returnMessage": "INVALID_SIGNATURE"}, status=401
            )

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return web.json_response(
                {"returnCode": "FAIL", "returnMessage": "INVALID_BODY"}, status=400
            )

        # أي حالة غير PAY_SUCCESS نؤكد استلامها ولا نفعل شيئاً.
        if payload.get("bizStatus") != "PAY_SUCCESS":
            return web.json_response({"returnCode": "SUCCESS", "returnMessage": None})

        try:
            data = json.loads(payload.get("data") or "{}")
        except ValueError:
            data = {}

        trade_no = str(data.get("merchantTradeNo") or "").strip()
        try:
            paid_amount = float(data.get("totalFee") or data.get("orderAmount") or 0.0)
        except (TypeError, ValueError):
            paid_amount = 0.0

        ok, status, result = db.mark_binance_pay_order_paid(trade_no, paid_amount)
        if ok and result:
            if abs(paid_amount - float(result["amount"])) > 0.01:
                logger.warning(
                    "فرق بين المبلغ المدفوع (%s) والمطلوب (%s) للعملية %s",
                    paid_amount,
                    result["amount"],
                    trade_no,
                )
            asyncio.get_event_loop().create_task(
                _notify_credited(
                    bot,
                    int(result["user_id"]),
                    float(result["amount"]),
                    float(result["new_balance"]),
                    trade_no,
                )
            )
        elif status != "already_processed":
            logger.error("فشل معالجة إشعار الدفع %s: %s", trade_no, status)

        return web.json_response({"returnCode": "SUCCESS", "returnMessage": None})

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    return app


async def start_webhook_server(bot) -> None:
    """تشغيل خادم الـ Webhook داخل نفس عملية البوت (نفس حلقة asyncio)."""
    global _runner
    app = build_webhook_app(bot)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(
        _runner,
        config.BINANCE_PAY_LISTEN_HOST,
        config.BINANCE_PAY_LISTEN_PORT,
    )
    await site.start()
    logger.info(
        "✅ خادم Binance Pay (وضع التاجر) يعمل على %s:%s (المسار %s)",
        config.BINANCE_PAY_LISTEN_HOST,
        config.BINANCE_PAY_LISTEN_PORT,
        WEBHOOK_PATH,
    )


# ==============================================================================
# وضع التحقق برقم العملية (مفتاح قراءة فقط على الحساب الشخصي)
# ==============================================================================
def _spot_signature(query: str) -> str:
    return hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _fetch_pay_page(start_ms: int) -> List[Dict[str, Any]]:
    """يجلب صفحة واحدة (حتى 100 عملية) من سجل Binance Pay ابتداءً من startTime."""
    params = {
        "startTime": int(start_ms),
        "limit": 100,
        "recvWindow": 10000,
        "timestamp": int(time.time() * 1000),
    }
    query = urlencode(params)
    url = f"{SPOT_API_BASE}{PAY_TRANSACTIONS_PATH}?{query}&signature={_spot_signature(query)}"
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"X-MBX-APIKEY": config.BINANCE_API_KEY}) as resp:
            payload = await resp.json(content_type=None)

    if not isinstance(payload, dict) or payload.get("code") != "000000":
        raise RuntimeError(f"Binance pay transactions API error: code={payload.get('code') if isinstance(payload, dict) else '???'} msg={payload.get('msg') if isinstance(payload, dict) else ''}")
    return payload.get("data") or []


async def fetch_pay_transactions(start_ms: int, max_pages: int = 10) -> List[Dict[str, Any]]:
    """يجلب سجل التحويلات كاملاً من startTime بالتصفح الزمني (100 عملية لكل صفحة).

    إذا رجعت الصفحة ممتلئة (100) نتقدم إلى ما بعد آخر عملية فيها، حتى صفحة
    غير ممتلئة أو بلوغ الحد الأقصى للصفحات. عند أي شك نفشل بأمان (لا اعتماد).
    """
    results: List[Dict[str, Any]] = []
    cursor = int(start_ms)
    for _ in range(max(1, int(max_pages))):
        page = await _fetch_pay_page(cursor)
        results.extend(page)
        if len(page) < 100:
            break
        times = [int(tx.get("transactionTime") or 0) for tx in page]
        nxt = max(times) + 1 if times else cursor + 1
        if nxt <= cursor:  # لا تقدم — نخرج لتفادي حلقة لا نهائية
            break
        cursor = nxt
    return results


async def find_pay_transaction(order_id: str, min_time_ms: int = 0) -> Tuple[Optional[Dict[str, Any]], bool]:
    """يبحث عن تحويل وارد يطابق Order ID الظاهر في إيصال العميل.

    يرجع (العملية أو None, ambiguous).

    مؤكد من سجل حي (2026-11-24): رقم الـOrder ID الذي يراه العميل في الإيصال
    (أرقام فقط، مثل 467386478032011264) يظهر في سجل الـAPI في الحقل orderId،
    بينما transactionId شكل مختلف تماماً (P_A25UN64A6AS71114). المطابقة تامة
    على orderId أو transactionId؛ يُسجَّل دائماً الـtransactionId الكامل في
    قاعدة البيانات كمفتاح منع تكرار.
    """
    wanted = (order_id or "").strip()
    if not wanted:
        return None, False
    floor_ms = int(time.time() * 1000) - 89 * 24 * 60 * 60 * 1000
    start_ms = max(floor_ms, int(min_time_ms or 0))
    matches: Dict[str, Dict[str, Any]] = {}
    for tx in await fetch_pay_transactions(start_ms):
        oid = str(tx.get("orderId") or "").strip()
        tid = str(tx.get("transactionId") or "").strip()
        if wanted and wanted in (oid, tid):
            matches[tid or oid] = tx
    if len(matches) > 1:
        return None, True
    if matches:
        return next(iter(matches.values())), False
    return None, False


def validate_topup_transaction(
    tx: Optional[Dict[str, Any]], min_time_ms: int = 0
) -> Tuple[bool, str, Decimal, str, str]:
    """يفحص العملية ويرجع (صالحة؟, سبب الرفض, المبلغ الفعلي, العملة, اسم المرسل).

    لا يوجد مبلغ متوقع: العميل يحوّل أولاً ثم يرسل صورة الإيصال، والرصيد يُضاف
    بنفس المبلغ الوارد الفعلي في سجل Binance بالظبط (بدون أي تقريب).

    الشروط كلها إلزامية:
    - العملية موجودة وواصلة فعلاً للحساب (amount موجب حسب توثيق Binance).
    - نوعها C2C (تحويل شخصي مباشر) — تُرفض المرتجعات والصناديق والمدفوعات التجارية.
    - وقتها بعد لحظة تفعيل الشحن التلقائي (cutoff) — التحويلات الأقدم كانت تُشحن يدوياً.
    - العملة المطلوبة بالظبط.
    - لم تُستخدم من قبل (مفتاح أساسي في قاعدة البيانات).

    أسباب الرفض: not_found / outgoing / already_used / wrong_type / too_old /
    wrong_currency
    """
    zero = Decimal("0")
    if not tx:
        return False, "not_found", zero, "", ""
    try:
        amount = Decimal(str(tx.get("amount") or "0"))
    except (InvalidOperation, ValueError):
        return False, "not_found", zero, "", ""
    currency = str(tx.get("currency") or "").upper()
    payer_name = ((tx.get("payerInfo") or {}).get("name") or "")
    tx_id = str(tx.get("transactionId") or "")
    order_type = str(tx.get("orderType") or "").upper()
    try:
        tx_time = int(tx.get("transactionTime") or 0)
    except (TypeError, ValueError):
        tx_time = 0

    if amount <= zero:
        return False, "outgoing", amount, currency, payer_name
    if db.is_pay_transaction_processed(tx_id):
        return False, "already_used", amount, currency, payer_name
    if order_type != "C2C":
        return False, "wrong_type", amount, currency, payer_name
    if tx_time < int(time.time() * 1000) - MAX_TRANSFER_AGE_MS:
        return False, "too_old", amount, currency, payer_name
    if min_time_ms and tx_time < int(min_time_ms):
        return False, "too_old", amount, currency, payer_name
    if currency != order_currency().upper():
        return False, "wrong_currency", amount, currency, payer_name
    return True, "ok", amount, currency, payer_name


async def extract_order_id_candidates(proof_text: str = "", image_bytes: bytes = b"") -> List[str]:
    """يجمع أرقام الطلبات المرشحة (15-20 رقماً) من نص الإيصال ومن صورته عبر OCR."""
    ids: List[str] = []
    seen = set()
    for m in re.finditer(r"\d{15,20}", proof_text or ""):
        d = m.group(0)
        if d not in seen:
            seen.add(d)
            ids.append(d)
    if image_bytes and (getattr(config, "OCR_SPACE_API_KEY", "") or "").strip():
        try:
            for d in await ocr_space_extract_order_ids(image_bytes):
                if d not in seen:
                    seen.add(d)
                    ids.append(d)
        except RuntimeError as exc:
            logger.warning("تعذر قراءة صورة إيصال طلب يدوي عبر OCR: %s", exc)
    return ids


async def resolve_transfer_for_manual_approval(
    proof_text: str, image_bytes: bytes, cutoff_ms: int
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """يحدد التحويل القانوني لطلب شحن يدوي قبل موافقة الأدمن.

    يرجع (status, tx, reason):
    - found: تحويل واحد صالح ومؤكد — tx جاهز للعرض على الأدمن للتأكيد.
    - none: لا يوجد رقم طلب في الإيصال (no_order_id) أو لا توجد عملية مطابقة (not_found).
    - ambiguous: أكثر من عملية تطابق.
    - invalid: عملية واحدة وجدت لكنها مرفوضة — reason من validate_topup_transaction.
    لا يُقبل التثبيت بالمبلغ وحده أبداً: المطابقة على رقم الطلب/العملية فقط.
    """
    cands = await extract_order_id_candidates(proof_text, image_bytes)
    if not cands:
        return "none", None, "no_order_id"
    matches: Dict[str, Dict[str, Any]] = {}
    for cand in cands:
        tx, amb = await find_pay_transaction(cand, min_time_ms=cutoff_ms)
        if amb:
            return "ambiguous", None, "ambiguous"
        if tx:
            matches[str(tx.get("transactionId") or "")] = tx
    if len(matches) > 1:
        return "ambiguous", None, "multi"
    if not matches:
        return "none", None, "not_found"
    tx = next(iter(matches.values()))
    valid, reason, _amount, _currency, _payer = validate_topup_transaction(tx, min_time_ms=cutoff_ms)
    if not valid:
        return "invalid", tx, reason
    return "found", tx, "ok"


# ==============================================================================
# قراءة صورة الإيصال عبر OCR.space (معالجة خارجية — بعد موافقة المالك فقط)
# ==============================================================================
def _compress_for_ocr(image_bytes: bytes, max_bytes: int = OCR_MAX_BYTES) -> bytes:
    """يضغط الصورة JPEG تحت حد الطبقة المجانية من OCR.space."""
    try:
        from PIL import Image
    except ImportError:
        if len(image_bytes) <= max_bytes:
            return image_bytes
        raise RuntimeError("ocr_image_too_large")
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
        if quality > 50:
            quality -= 15
        else:
            w, h = img.size
            if min(w, h) <= 400:
                return data
            img = img.resize((max(1, w * 3 // 4), max(1, h * 3 // 4)))


async def ocr_space_extract_order_ids(image_bytes: bytes) -> List[str]:
    """يرسل صورة الإيصال إلى OCR.space ويرجع أرقام الطلبات المرشحة (15-20 رقماً).

    يرفع RuntimeError('ocr_not_configured') لو المفتاح غير موجود.
    """
    api_key = (getattr(config, "OCR_SPACE_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError("ocr_not_configured")
    data = await asyncio.to_thread(_compress_for_ocr, image_bytes)
    form = aiohttp.FormData()
    form.add_field("file", data, filename="receipt.jpg", content_type="image/jpeg")
    form.add_field("OCREngine", "2")
    form.add_field("scale", "true")
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OCR_API_URL, data=form, headers={"apikey": api_key}) as resp:
            result = await resp.json(content_type=None)
    if not isinstance(result, dict):
        raise RuntimeError("ocr_bad_response")
    if result.get("IsErroredOnProcessing"):
        raise RuntimeError("ocr_error: " + str(result.get("ErrorMessage") or "")[:120])
    parsed = "\n".join(
        str(p.get("ParsedText") or "") for p in (result.get("ParsedResults") or [])
    )
    seen, ids = set(), []
    for m in re.finditer(r"\d{15,20}", parsed):
        digits = m.group(0)
        if digits not in seen:
            seen.add(digits)
            ids.append(digits)
    return ids


# ==============================================================================
# نقطة الدخول من بوت تيليجرام
# ==============================================================================
async def post_init_hook(application) -> None:
    """يُستدعى تلقائياً من مكتبة python-telegram-bot بعد تهيئة التطبيق."""
    m = mode()
    if m == "merchant":
        await start_webhook_server(application.bot)
    elif m == "monitor":
        logger.info("✅ الشحن التلقائي برقم عملية Binance Pay مفعّل (مفتاح قراءة فقط).")
    else:
        logger.info(
            "الشحن التلقائي غير مفعّل. اضبط BINANCE_PAY_MODE=monitor مع "
            "BINANCE_API_KEY/SECRET، أو BINANCE_PAY_MODE=merchant مع مفاتيح التاجر في .env."
        )
