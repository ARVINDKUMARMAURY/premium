from __future__ import annotations

import asyncio
import logging
import os
import sys
import html
import io
import zipfile
import requests
from urllib.parse import quote
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from bson import ObjectId
from telethon import TelegramClient, events
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.errors.rpcerrorlist import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.errors.rpcerrorlist import FreshResetAuthorisationForbiddenError

try:
    from telegram import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
        ReplyKeyboardRemove,
        Update,
    )
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, NetworkError, TimedOut
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as e:
    raise RuntimeError(
        "Wrong 'telegram' package installed. This project requires 'python-telegram-bot'.\n\n"
        "Fix:\n"
        "  pip uninstall -y telegram\n"
        "  pip uninstall -y python-telegram-bot telegram-bot\n"
        "  pip install -U python-telegram-bot\n\n"
        "Original error: " + str(e)
    )

import admin as admin_module
import device_manager
from config import (
    ADMIN_USER_IDS,
    BOT_TOKEN,
    BOT_USERNAME,
    CHANNEL_USERNAME,
    REPORT_CHANNEL_USERNAME,
    START_IMAGE,
    SUPPORT_USERNAME,
    BHARATPE_UPI_ID,
    BHARATPE_TOKEN,
    BHARATPE_MERCHANT_ID,
)

try:
    from config import REFERRAL_PERCENT
except Exception:
    REFERRAL_PERCENT = float(os.getenv("REFERRAL_PERCENT", "3.0"))

from database import Repo, get_db, init_indexes

# Logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s:%(name)s:%(message)s",
)

for _name in (
    "httpx",
    "telegram",
    "telegram.ext",
    "telethon",
    "telethon.network.mtprotosender",
    "telethon.client.users",
    "telethon.client.telegrambaseclient",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

STATE: Dict[int, Dict[str, Any]] = {}

def is_admin(user_id: int) -> bool:
    return int(user_id) in set(ADMIN_USER_IDS)

def require_token() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty.")

def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)

async def bharatpe_find_txn_by_utr(utr: str) -> dict | None:
    """Search BharatPe merchant transactions for a SUCCESS payment matching the given UTR
    (matches against bankReferenceNo, case-insensitive). Returns the transaction dict or None."""
    if not BHARATPE_TOKEN or not BHARATPE_MERCHANT_ID:
        return None
    utr_norm = utr.strip().upper()
    try:
        resp = requests.get(
            "https://payments-tesseract.bharatpe.in/api/v1/merchant/transactions",
            params={"module": "PAYMENT_QR", "merchantId": BHARATPE_MERCHANT_ID},
            headers={"token": BHARATPE_TOKEN},
            timeout=12,
        )
        data = resp.json()
    except Exception:
        logging.exception("BharatPe transactions API call failed")
        return None

    if not data.get("status"):
        return None

    txns = (data.get("data") or {}).get("transactions") or []
    for txn in txns:
        ref = str(txn.get("bankReferenceNo") or "").strip().upper()
        internal = str(txn.get("internalUtr") or "").strip().upper()
        if utr_norm and (utr_norm == ref or utr_norm == internal):
            if str(txn.get("status")) == "SUCCESS" and txn.get("type") == "PAYMENT_RECV":
                return txn
    return None

async def safe_query_answer(query, *args, **kwargs) -> None:
    try:
        await query.answer(*args, **kwargs)
    except (TimedOut, NetworkError):
        return
    except Exception:
        return

async def safe_reply_text(message, text: str, **kwargs):
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            return await message.reply_text(text, **kwargs)
        except (TimedOut, NetworkError) as e:
            last_exc = e
            await asyncio.sleep(1)
        except BadRequest:
            raise
    if last_exc:
        raise last_exc

async def safe_bot_send(bot, method_name: str, **kwargs):
    last_exc: Exception | None = None
    fn = getattr(bot, method_name)
    for _ in range(3):
        try:
            return await fn(**kwargs)
        except (TimedOut, NetworkError) as e:
            last_exc = e
            await asyncio.sleep(1)
        except BadRequest:
            raise
    if last_exc:
        raise last_exc

async def safe_edit(
    message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode=ParseMode.MARKDOWN,
):
    try:
        if getattr(message, "photo", None) and (getattr(message, "text", None) in (None, "")):
            return await message.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return await message.edit_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return None
        raise
    except Exception:
        try:
            return await message.edit_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return None
            raise

def cancel_only_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Cancel")]], resize_keyboard=True, is_persistent=True)

def reply_menu(is_admin_user: bool):
    # Bottom persistent keyboard removed — was duplicating the inline main menu.
    return ReplyKeyboardRemove()

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Buy Account", style="primary", icon_custom_emoji_id="5424972470023104089", callback_data="shop:countries"),
            InlineKeyboardButton("History", style="primary", icon_custom_emoji_id="5323442290708985472", callback_data="me:history:0"),
        ],
        [
            InlineKeyboardButton("Buy Session", style="primary", icon_custom_emoji_id="5424972470023104089", callback_data="session:start"),
            InlineKeyboardButton("Balance", style="primary", icon_custom_emoji_id="5409048419211682843", callback_data="me:balance"),
        ],
        [
            InlineKeyboardButton("Deposit", style="primary", icon_custom_emoji_id="5409048419211682843", callback_data="dep:start"),
            InlineKeyboardButton("🆘 Support", url=f"https://t.me/{SUPPORT_USERNAME}"),
        ],
        [InlineKeyboardButton("Find by Credits", style="primary", icon_custom_emoji_id="5323442290708985472", callback_data="find:credits")],
        [InlineKeyboardButton("Refer & Earn", style="primary", icon_custom_emoji_id="5337080053119336309", callback_data="ref:menu")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Admin Panel", style="primary", icon_custom_emoji_id="4963511421280192936", callback_data="admin:menu")])
    return kb(rows)

def back_to_menu() -> InlineKeyboardMarkup:
    return kb([[InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")]])

async def build_country_price_text(repo: "Repo", countries: list[dict]) -> str:
    lines = ["🛒 Buy Account — Prices:\n"]
    for c in countries:
        code = c.get("country") or "?"
        emoji = c.get("country_emoji") or ""
        count = c.get("count", 0)
        pr = await repo.available_price_range(country=code, year=None)
        min_p = pr.get("min_price")
        max_p = pr.get("max_price")
        if min_p is None:
            price_str = "N/A"
        elif min_p == max_p:
            price_str = f"₹{min_p}"
        else:
            price_str = f"₹{min_p}–₹{max_p}"
        lines.append(f"{emoji} {code}: {price_str} ({count} in stock)")
    lines.append("\n⬇️ Neeche se country chunein:")
    return "\n".join(lines)

def countries_keyboard(countries: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for c in countries:
        code = c.get("country") or "?"
        emoji = c.get("country_emoji") or ""
        count = c.get("count", 0)
        current.append(InlineKeyboardButton(f"{emoji} {code} ({count})", callback_data=f"shop:country:{code}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")])
    return kb(rows)

def _find_results_kb(groups: list[dict[str, Any]], *, max_price: int, page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for g in groups:
        emoji = g.get("country_emoji") or "🌍"
        country = g.get("country") or "?"
        year = g.get("year")
        year_token = "none" if year is None else str(year)
        if year == "premium":
            m = g.get("premium_months")
            year_txt = f"⭐ Premium ({m}m)" if m else "⭐ Premium"
        else:
            year_txt = str(year) if year is not None else "Unknown"
        price = int(g.get("price") or 0)
        count = int(g.get("count") or 0)
        label = f"{emoji} {year_txt} • {price}c ({count})"
        cur.append(
            InlineKeyboardButton(label, callback_data=f"find:pickgrp:{country}:{year_token}:{price}")
        )
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    page_size = 10
    max_page = max(0, (total - 1) // page_size) if total else 0
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", style="primary", callback_data=f"find:page:{max_price}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton("Next ➡️", style="primary", callback_data=f"find:page:{max_price}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")])
    return kb(rows)

def years_keyboard(country: str, years: list[dict]) -> InlineKeyboardMarkup:
    def _sort_key(item: dict):
        y = item.get("year")
        if y == "premium":
            return (0, 0)
        if isinstance(y, int):
            return (1, -y)
        if isinstance(y, str) and y.isdigit():
            return (1, -int(y))
        return (2, 0)
    years_sorted = sorted(years, key=_sort_key)
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for y in years_sorted:
        year = y.get("year")
        count = y.get("count", 0)
        val = str(year) if year is not None else "none"
        if year == "premium":
            label = f"⭐ Premium ({count})"
        else:
            label = f"{year} ({count})" if year is not None else f"Unknown ({count})"
        cur.append(InlineKeyboardButton(label, callback_data=f"shop:year:{country}:{val}"))
        if len(cur) == 3:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton("⬅️ Back", style="primary", callback_data="shop:countries")])
    return kb(rows)

def buy_confirm_keyboard(country: str, year_token: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [InlineKeyboardButton("Confirm Buy", style="success", icon_custom_emoji_id="5206607081334906820", callback_data=f"shop:buy:{country}:{year_token}")],
            [
                InlineKeyboardButton("⬅️ Back", style="primary", callback_data=f"shop:country:{country}"),
                InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home"),
            ],
        ]
    )

@dataclass
class PendingLogin:
    api_id: int
    api_hash: str
    phone: str
    client: TelegramClient

def _mask_phone_e164_like(phone_digits: str) -> str:
    digits = "".join(ch for ch in str(phone_digits) if ch.isdigit())
    if len(digits) <= 4:
        return f"+{digits}" if digits else "+"
    start = digits[:2]
    end = digits[-2:]
    return f"+{start}{'•' * (len(digits) - 4)}{end}"

async def _send_sold_report(
    bot,
    *,
    account_doc: dict[str, Any],
    otp_text: str,
) -> None:
    try:
        me = await bot.get_me()
        bot_uname = f"@{me.username}" if getattr(me, "username", None) else "(no username)"
    except Exception:
        bot_uname = "(unknown)"
    country = account_doc.get("country") or ""
    country_emoji = account_doc.get("country_emoji") or ""
    year = account_doc.get("year")
    premium_months = account_doc.get("premium_months")
    if year == "premium":
        year_txt = f"⭐ Premium ({premium_months}m)" if premium_months else "⭐ Premium"
    elif year is None:
        year_txt = "N/A"
    else:
        year_txt = str(year)
    phone = str(account_doc.get("phone", ""))
    masked = _mask_phone_e164_like(phone)
    buyer_username = (account_doc.get("sold_to_username") or "").strip()
    buyer_line = f"@{buyer_username}" if buyer_username else "N/A"
    sold_at = account_doc.get("price")
    sold_at_txt = f"{sold_at} Credits" if sold_at is not None else "N/A"
    text = (
        "🎉 ACCOUNT SOLD\n"
        "━━━━━━━━━━━━━━\n"
        f"🌍 Country  : {country_emoji} {country}\n"
        f"🗓️ Year     : {year_txt}\n"
        f"📱 Number   : {masked}\n"
        f"🔐 OTP Code : {otp_text}\n"
        f"💸 Sold At  : {sold_at_txt}\n"
        "━━━━━━━━━━━━━━\n"
        f"👤 Buyer : {buyer_line}\n"
        f"🤖 Bot   : {bot_uname}"
    )
    try:
        await bot.send_photo(
            chat_id=f"@{REPORT_CHANNEL_USERNAME}",
            photo=START_IMAGE,
            caption=text,
        )
    except Exception:
        return

class AccountManager:
    def __init__(
        self,
        send_message: Callable[[int, str], "asyncio.Future[Any]"],
        *,
        bot,
    ):
        self._send_message = send_message
        self._bot = bot
        self._clients: Dict[ObjectId, TelegramClient] = {}
        self._buyers: Dict[ObjectId, int] = {}
        self._admin_monitors: Dict[ObjectId, int] = {}
        self._sold_report_sent: set[ObjectId] = set()
        self._pending_admin_login: Dict[int, PendingLogin] = {}

    @staticmethod
    async def _terminate_other_sessions(client: TelegramClient) -> bool:
        """Log out every other active session for this account, keep only this one.
        Returns True if terminated, False if Telegram refused (session too new)."""
        try:
            await client(ResetAuthorizationsRequest())
            return True
        except FreshResetAuthorisationForbiddenError:
            logging.warning("ResetAuthorizationsRequest: session too new (<24h), skipped")
            return False
        except Exception:
            logging.exception("ResetAuthorizationsRequest failed")
            return False

    async def admin_begin_login(self, admin_user_id: int, api_id: int, api_hash: str, phone_e164: str) -> None:
        if admin_user_id in self._pending_admin_login:
            await self.admin_cancel_login(admin_user_id)
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        await client.send_code_request(phone_e164)
        self._pending_admin_login[admin_user_id] = PendingLogin(
            api_id=int(api_id), api_hash=api_hash, phone=phone_e164, client=client
        )

    async def admin_complete_code(self, admin_user_id: int, code: str) -> tuple[Optional[dict[str, Any]], str]:
        pending = self._pending_admin_login.get(admin_user_id)
        if not pending:
            return None, "no_pending"
        try:
            await pending.client.sign_in(phone=pending.phone, code=code)
        except PhoneCodeInvalidError:
            return None, "invalid_code"
        except PhoneCodeExpiredError:
            return None, "code_expired"
        except SessionPasswordNeededError:
            return None, "need_password"
        except Exception:
            logging.exception("admin_complete_code failed")
            return None, "error"
        me = await pending.client.get_me()
        terminated_others = await self._terminate_other_sessions(pending.client)
        session_string = pending.client.session.save()
        doc = {
            "phone": pending.phone.lstrip("+"),
            "api_id": pending.api_id,
            "api_hash": pending.api_hash,
            "session_string": session_string,
            "tg_user_id": me.id,
            "tg_username": me.username,
            "other_sessions_terminated": terminated_others,
        }
        await pending.client.disconnect()
        self._pending_admin_login.pop(admin_user_id, None)
        return doc, "ok"

    async def admin_complete_password(self, admin_user_id: int, password: str) -> tuple[Optional[dict[str, Any]], str]:
        pending = self._pending_admin_login.get(admin_user_id)
        if not pending:
            return None, "no_pending"
        try:
            await pending.client.sign_in(password=password)
        except PasswordHashInvalidError:
            return None, "invalid_password"
        except RPCError as e:
            if e.__class__.__name__ == "PasswordHashInvalidError":
                return None, "invalid_password"
            logging.exception("admin_complete_password RPCError")
            return None, "error"
        except Exception as e:
            if e.__class__.__name__ == "PasswordHashInvalidError":
                return None, "invalid_password"
            logging.exception("admin_complete_password failed")
            return None, "error"
        me = await pending.client.get_me()
        terminated_others = await self._terminate_other_sessions(pending.client)
        session_string = pending.client.session.save()
        doc = {
            "phone": pending.phone.lstrip("+"),
            "api_id": pending.api_id,
            "api_hash": pending.api_hash,
            "session_string": session_string,
            "tg_user_id": me.id,
            "tg_username": me.username,
            "other_sessions_terminated": terminated_others,
        }
        await pending.client.disconnect()
        self._pending_admin_login.pop(admin_user_id, None)
        return doc, "ok"

    async def admin_cancel_login(self, admin_user_id: int) -> None:
        pending = self._pending_admin_login.pop(admin_user_id, None)
        if pending:
            await pending.client.disconnect()

    async def ensure_connected_for_account(self, account_id: ObjectId, account_doc: dict[str, Any], buyer_user_id: int) -> None:
        self._buyers[account_id] = int(buyer_user_id)
        if account_id in self._clients:
            return
        await self._connect_client(account_id, account_doc)

    async def ensure_connected_for_admin_monitor(self, account_id: ObjectId, account_doc: dict[str, Any]) -> None:
        if account_id in self._clients:
            return
        await self._connect_client(account_id, account_doc)

    async def _connect_client(self, account_id: ObjectId, account_doc: dict[str, Any]) -> None:
        client = TelegramClient(
            StringSession(account_doc["session_string"]),
            int(account_doc["api_id"]),
            account_doc["api_hash"],
        )
        await client.connect()

        @client.on(events.NewMessage(from_users=777000))
        async def otp_listener(event):
            text = (event.raw_text or event.text or "").strip()
            admin_monitor = self._admin_monitors.get(account_id)
            if admin_monitor:
                import re
                m5a = re.search(r"\b(\d{5})\b", text)
                if m5a:
                    otp_admin = m5a.group(1)
                    try:
                        await self._bot.send_message(
                            chat_id=admin_monitor,
                            text=f"📱 OTP for +{account_doc.get('phone','')}: {otp_admin}",
                        )
                    except Exception:
                        pass
            buyer = self._buyers.get(account_id)
            if not buyer:
                return
            import re
            m5 = re.search(r"\b(\d{5})\b", text)
            m6 = re.search(r"\b(\d{6})\b", text)
            m4 = re.search(r"\b(\d{4})\b", text)
            otp_code = (m5.group(1) if m5 else (m6.group(1) if m6 else (m4.group(1) if m4 else "")))
            if not otp_code:
                return
            if self._buyers.get(account_id) is None:
                return
            self._buyers.pop(account_id, None)
            try:
                await self._bot.send_message(
                    chat_id=buyer,
                    text=(
                        f"🔐 OTP received for +{account_doc.get('phone','')}:\n\n{text}\n\n"
                        "✅ Account successfully sold.\n"
                        "🛠️ You can manage devices for a few minutes from the button below."
                    ),
                    reply_markup=kb(
                        [
                            [
                                InlineKeyboardButton(
                                    "🛠️ Manage Devices", style="primary",
                                    callback_data=f"dev:menu:{str(account_id)}",
                                )
                            ]
                        ]
                    ),
                )
            except Exception:
                await self._send_message(
                    buyer,
                    f"🔐 OTP received for +{account_doc.get('phone','')}:\n\n{text}\n\n✅ Account successfully sold.",
                )
            admin_monitor = self._admin_monitors.get(account_id)
            if admin_monitor and admin_monitor != buyer:
                try:
                    await self._bot.send_message(
                        chat_id=admin_monitor,
                        text=f"📱 OTP for +{account_doc.get('phone','')}:\n\n{text}",
                    )
                except Exception:
                    pass
            if account_id not in self._sold_report_sent:
                self._sold_report_sent.add(account_id)
                try:
                    await _send_sold_report(
                        self._bot,
                        account_doc=account_doc,
                        otp_text=str(otp_code),
                    )
                except Exception:
                    pass
            asyncio.create_task(self.disconnect_later(account_id, seconds=600))
            return

        self._clients[account_id] = client
        return

    def get_buyer(self, account_id: ObjectId) -> int | None:
        return self._buyers.get(account_id)

    def get_client(self, account_id: ObjectId) -> TelegramClient | None:
        return self._clients.get(account_id)

    def start_admin_monitor(self, account_id: ObjectId, admin_user_id: int) -> None:
        self._admin_monitors[account_id] = int(admin_user_id)

    def stop_admin_monitor(self, account_id: ObjectId) -> None:
        self._admin_monitors.pop(account_id, None)

    def get_admin_monitor(self, account_id: ObjectId) -> int | None:
        return self._admin_monitors.get(account_id)

    async def disconnect_account(self, account_id: ObjectId) -> None:
        self._buyers.pop(account_id, None)
        self._admin_monitors.pop(account_id, None)
        client = self._clients.pop(account_id, None)
        if client:
            await client.disconnect()

    async def disconnect_later(self, account_id: ObjectId, *, seconds: int) -> None:
        await asyncio.sleep(max(1, int(seconds)))
        await self.disconnect_account(account_id)

    async def shutdown(self) -> None:
        for admin_id in list(self._pending_admin_login.keys()):
            await self.admin_cancel_login(admin_id)
        for acc_id in list(self._clients.keys()):
            await self.disconnect_account(acc_id)

# ---------- Core Handlers ----------
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    await safe_reply_text(update.message, "pong")

async def bd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message and send /bd")
        return
    repo: Repo = context.application.bot_data["repo"]
    db = repo.db
    sent = 0
    failed = 0
    cursor = db.users.find({}, {"user_id": 1})
    async for u in cursor:
        user_id = int(u.get("user_id"))
        try:
            await update.message.reply_to_message.forward(chat_id=user_id)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}")

async def _is_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str | None, list[str]]:
    uid = update.effective_user.id
    async def _in(channel: str) -> tuple[bool, str | None]:
        try:
            member = await context.bot.get_chat_member(chat_id=f"@{channel}", user_id=uid)
            ok = member.status in {"creator", "administrator", "member", "restricted"}
            return ok, None
        except Exception as e:
            return False, str(e)
    last_err: str | None = None
    for _ in range(3):
        ok1, err1 = await _in(CHANNEL_USERNAME)
        ok2, err2 = await _in(REPORT_CHANNEL_USERNAME)
        missing: list[str] = []
        if not ok1:
            missing.append(CHANNEL_USERNAME)
        if not ok2:
            missing.append(REPORT_CHANNEL_USERNAME)
        if ok1 and ok2:
            return True, None, []
        comb_err = err1 or err2
        if comb_err:
            low = comb_err.lower()
            if "forbidden" in low or "not enough rights" in low or "chat not found" in low:
                return False, comb_err, missing
            last_err = comb_err
        await asyncio.sleep(1)
    return False, last_err, missing

def _ref_link(user_id: int) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    return f"/start ref_{user_id}"

def _home_caption(*, uid: int, credits: int, stock: int) -> str:
    return (
        "◤ ID STORE BOT ◢\n"
        "━━━━━━━━━━━━━━━\n"
        f"▸ User ID  : {uid}\n"
        f"▸ Credits  : {credits}\n"
        "▸ Price    : Set per account\n"
        f"▸ Stock    : {stock}\n"
        "━━━━━━━━━━━━━━━"
    )

def join_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [
            [InlineKeyboardButton("Join Main Channel", style="primary", icon_custom_emoji_id="5456140674028019486", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton("Join Report Channel", style="primary", icon_custom_emoji_id="5456140674028019486", url=f"https://t.me/{REPORT_CHANNEL_USERNAME}")],
            [InlineKeyboardButton("Verify", style="success", icon_custom_emoji_id="5206607081334906820", callback_data="join:verify")],
        ]
    )

async def _ban_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        repo: Repo = context.application.bot_data["repo"]
        uid = update.effective_user.id
        if await repo.is_banned(uid):
            await update.effective_message.reply_text("Access denied. You have been banned. Contact support.")
            return True
    except Exception:
        return False
    return False

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _ban_guard(update, context):
        return
    uid = update.effective_user.id
    repo: Repo = context.application.bot_data["repo"]
    try:
        is_new = await repo.is_new_user(uid)
        if is_new and context.args:
            arg0 = str(context.args[0]).strip()
            if arg0.startswith("ref_"):
                referrer_id_s = arg0.split("_", 1)[1]
                if referrer_id_s.isdigit():
                    referrer_id = int(referrer_id_s)
                    ref_un = None
                    try:
                        ch = await context.bot.get_chat(referrer_id)
                        ref_un = getattr(ch, "username", None)
                    except Exception:
                        ref_un = None
                    saved = await repo.save_referral_if_new(
                        referred_user_id=uid,
                        referred_username=update.effective_user.username,
                        referrer_user_id=referrer_id,
                        referrer_username=ref_un,
                    )
                    if saved:
                        await update.message.reply_text(
                            f"✅ You were referred by user: {referrer_id}\n\nInvite friends and earn {REFERRAL_PERCENT:.1f}% of their deposits forever!\n\nYour Referral Link:\n{_ref_link(uid)}",
                            parse_mode=None,
                        )
    except Exception:
        pass
    await repo.ensure_user(uid, username=update.effective_user.username)
    joined, join_err, missing = await _is_joined(update, context)
    if not joined:
        join_text = (
            "◤ ID STORE BOT ◢\n"
            "━━━━━━━━━━━━━━━\n"
            "🔒 Channel Verification Required\n\n"
            f"📢 Join: @{CHANNEL_USERNAME}\n"
            f"📢 Join: @{REPORT_CHANNEL_USERNAME}\n\n"
            "✅ After joining both channels, press Verify below.\n\n"
            "If you leave any required channel, access may be blocked."
        )
        try:
            await update.message.reply_photo(
                photo=START_IMAGE,
                caption=join_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=join_keyboard(),
            )
        except Exception:
            await update.message.reply_text(
                join_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=join_keyboard(),
            )
        return
    available = await repo.count_available_accounts()
    user = await repo.ensure_user(uid, username=update.effective_user.username)
    is_admin_user = admin_module.is_admin(uid)
    credits = user.get("credits", 0)
    text = _home_caption(uid=uid, credits=credits, stock=available)
    try:
        await update.message.reply_photo(
            photo=START_IMAGE,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(is_admin_user),
        )
    except Exception:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(is_admin_user),
        )
    await update.message.reply_text("✅ Menu enabled.", reply_markup=reply_menu(is_admin_user))

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    uid = update.effective_user.id
    repo: Repo = context.application.bot_data["repo"]
    user = await repo.ensure_user(uid, username=update.effective_user.username)
    text = f"💰 *Your Balance*\n\nCredits: *{user.get('credits', 0)}*"
    if edit:
        await safe_edit(update.effective_message, text, reply_markup=back_to_menu(), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_to_menu())

async def send_purchase_details(update: Update, context: ContextTypes.DEFAULT_TYPE, account: dict[str, Any]) -> None:
    uid = update.effective_user.id
    account_manager: AccountManager = context.application.bot_data["account_manager"]
    phone = str(account.get("phone", ""))
    country_emoji = account.get("country_emoji") or ""
    country = account.get("country") or ""
    year = account.get("year")
    premium_months = account.get("premium_months")
    twofa = account.get("twofa_password")
    original_price = account.get("_original_price")
    final_price = account.get("_final_price")
    discount_used = bool(account.get("_discount_used"))
    if discount_used and original_price is not None and final_price is not None:
        price_line = f"Price: *{original_price}* → *{final_price}* credit(s) (Discount -5)"
    else:
        price = account.get("price")
        price_text = str(price) if price is not None else "-"
        price_line = f"Price: *{price_text}* credit(s)"
    msg = (
        "✅ *Purchase successful*\n\n"
        f"Phone: `{country_emoji} +{phone}`\n"
        f"Country: *{country}*\n"
        f"Year: *{('⭐ Premium (' + str(premium_months) + 'm)') if year == 'premium' and premium_months else ('⭐ Premium' if year == 'premium' else (year if year is not None else '-'))}*\n"
        f"{price_line}\n\n"
        "Now login to Telegram using this phone number.\n"
        "I will forward OTP here."
    )
    if twofa:
        msg += f"\n\n🔑 *2FA Password:* `{twofa}`"
    await update.effective_message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb(
            [[InlineKeyboardButton("🛠️ Manage Devices", style="primary", callback_data=f"dev:menu:{str(account['_id'])}")]]
        ),
    )
    await account_manager.ensure_connected_for_account(account["_id"], account, uid)

async def post_init(app: Application) -> None:
    try:
        await init_indexes()
    except Exception as e:
        logger.error(f"Mongo init_indexes failed: {e}")

async def post_shutdown(app: Application) -> None:
    account_manager: AccountManager = app.bot_data.get("account_manager")
    if account_manager:
        await account_manager.shutdown()

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    import traceback
    err = context.error
    try:
        import httpx
        if isinstance(err, (httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout)):
            return
    except Exception:
        pass
    if isinstance(err, (TimedOut, NetworkError)):
        return
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    try:
        with open(os.path.join(BASE_DIR, "error.txt"), "a", encoding="utf-8") as f:
            f.write("\n\n--- ERROR ---\n")
            f.write(tb)
    except Exception:
        pass
    logger.exception("Unhandled exception: %s", err)

# ---------- Referral Award ----------
async def _notify_referral_award(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    repo: Repo,
    referred_user_id: int,
    deposit_amount: int,
    admin_id: int,
    deposit_id: str | None = None,
) -> None:
    ref = await repo.db.referrals.find_one({"referred_user_id": int(referred_user_id)})
    if not ref:
        return
    referrer_id = int(ref.get("referrer_user_id") or 0)
    if not referrer_id:
        return
    reward = int(round((int(deposit_amount) * float(REFERRAL_PERCENT)) / 100.0))
    if reward <= 0:
        return
    user = await repo.add_referral_earning(
        referrer_user_id=referrer_id,
        referred_user_id=int(referred_user_id),
        amount=float(reward),
        by_admin=int(admin_id),
        deposit_id=str(deposit_id) if deposit_id else None,
        deposit_amount=int(deposit_amount),
    )
    referred_un = (ref.get("referred_username") or "").strip()
    ref_line = f"@{referred_un}" if referred_un else "N/A"
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                "🎉 Referral Reward Added!\n"
                f"• From user: {referred_user_id} {ref_line}\n"
                f"• Deposit: ₹{int(deposit_amount)}\n"
                f"• Reward: +₹{reward} credits ({REFERRAL_PERCENT:.1f}%)\n"
                f"• New Balance: {int((user or {}).get('credits', 0))} credits"
            ),
        )
    except Exception:
        pass

async def _bharatpe_verify_and_credit(
    repo: Repo, context: ContextTypes.DEFAULT_TYPE, uid: int, username: str, utr: str
) -> tuple[bool, str]:
    """Looks up the UTR in BharatPe's real transaction list. If found (and not already
    claimed), credits the user for the real amount received. Returns (success, message)."""
    dup = await repo.db.deposits.find_one({"bank_ref": utr, "method": "bharatpe", "status": "approved"})
    if dup:
        return False, "❌ This payment has already been claimed."

    txn = await bharatpe_find_txn_by_utr(utr)
    if not txn:
        return False, "⏳ Payment not found yet.\n\nIf you just paid, it can take a minute to reflect. Tap Check Again."

    amount = int(txn.get("amount", 0))
    if amount <= 0:
        return False, "❌ Invalid transaction amount returned. Please contact support."

    deposit_id = await repo.create_deposit_request(
        user_id=uid, username=username, amount=amount, method="bharatpe", network=None, amount_text=utr,
    )
    await repo.db.deposits.update_one(
        {"_id": ObjectId(deposit_id)},
        {"$set": {"bank_ref": utr, "txn_id": txn.get("id")}},
    )
    dep2 = await repo.mark_deposit(deposit_id, "approved", admin_id=uid, credits_added=amount)
    if not dep2:
        return False, "❌ Something went wrong recording the deposit. Please contact support."
    await repo.add_credits(uid, amount, by_admin=uid)

    try:
        await _notify_referral_award(
            context=context, repo=repo, referred_user_id=uid, deposit_amount=amount, admin_id=uid, deposit_id=deposit_id,
        )
    except Exception:
        pass

    try:
        udoc = await repo.db.users.find_one({"user_id": uid})
        bal = int((udoc or {}).get("credits", 0))
    except Exception:
        bal = amount

    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "💳 BharatPe Deposit — Verified & credited\n\n"
                    f"User: {uid} @{username if username else 'N/A'}\n"
                    f"Amount: ₹{amount}\nUTR: {utr}\nDeposit ID: {deposit_id}"
                ),
            )
        except Exception:
            pass

    return True, f"✅ Payment verified!\n\nAmount: ₹{amount}\nUTR: {utr}\nCredits added: {amount}\nNew balance: {bal} credits"

# ---------- Text Handler ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _ban_guard(update, context):
        return
    account_manager: AccountManager = context.application.bot_data["account_manager"]
    handled = await admin_module.handle_admin_text(update, context, STATE, account_manager)
    if handled:
        return
    if not update.message:
        return
    uid = update.effective_user.id
    text_in = (update.message.text or "").strip()
    repo: Repo = context.application.bot_data["repo"]

    if uid in STATE and STATE[uid].get("flow") == "buy_session" and STATE[uid].get("step") == "qty_text":
        if text_in.lower() == "cancel":
            STATE.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=reply_menu(is_admin(uid)))
            return
        if not text_in.isdigit() or int(text_in) <= 0:
            await update.message.reply_text("Please send a valid quantity as a number (e.g. 10), or press Cancel.")
            return
        qty = int(text_in)
        st = STATE[uid]
        country = st.get("country")
        session_price = int(st.get("price", 0))
        if not country or session_price <= 0:
            STATE.pop(uid, None)
            await update.message.reply_text("Something went wrong. Please start again from Buy Session.", reply_markup=reply_menu(is_admin(uid)))
            return

        total_cost = qty * session_price
        user = await repo.ensure_user(uid, username=update.effective_user.username)
        if int(user.get("credits", 0)) < total_cost:
            STATE.pop(uid, None)
            await update.message.reply_text(
                f"❌ Insufficient balance.\n\nNeed: ₹{total_cost} for {qty} session(s)\nYour balance: {int(user.get('credits', 0))} credits",
                reply_markup=reply_menu(is_admin(uid)),
            )
            return

        STATE.pop(uid, None)
        accounts, status = await repo.buy_session_accounts(
            user_id=uid, username=update.effective_user.username, country=country, quantity=qty, unit_price=session_price
        )
        if status == "insufficient_credits":
            await update.message.reply_text("❌ Insufficient balance.", reply_markup=reply_menu(is_admin(uid)))
            return
        if status == "not_available" or not accounts:
            await update.message.reply_text(
                f"❌ Not enough {country} sessions in stock right now (requested {qty}). Please try a smaller quantity.",
                reply_markup=reply_menu(is_admin(uid)),
            )
            return

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for acc in accounts:
                phone = str(acc.get("phone") or acc.get("_id"))
                session_str = acc.get("session_string") or ""
                zf.writestr(f"{phone}.session", session_str)
        buf.seek(0)
        buf.name = f"sessions_{country}_{qty}.zip"

        await update.message.reply_document(
            document=buf,
            filename=f"sessions_{country}_{qty}.zip",
            caption=f"✅ {len(accounts)} {country} session(s) purchased for ₹{total_cost}.",
            reply_markup=reply_menu(is_admin(uid)),
        )
        return

    if uid in STATE and STATE[uid].get("flow") == "deposit" and STATE[uid].get("step") == "upi_amount_text":
        if text_in.lower() == "cancel":
            STATE.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=reply_menu(is_admin(uid)))
            return
        if not text_in.isdigit() or int(text_in) <= 0:
            await update.message.reply_text("Please send a valid amount as a number (e.g. 100), or press Cancel.")
            return
        amount = int(text_in)
        STATE[uid] = {"flow": "deposit", "step": "upi_utr_text", "method": "bharatpe", "amount": amount}

        upi_link = f"upi://pay?pa={BHARATPE_UPI_ID}&pn=Payment&am={amount}&cu=INR"
        qr_img_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote(upi_link)}"
        caption = (
            f"💳 Pay ₹{amount}\n\n"
            f"UPI ID: `{BHARATPE_UPI_ID}`\n\n"
            "Scan the QR or pay to the UPI ID above using any UPI app.\n\n"
            "After paying, send the *UTR / Transaction Reference Number* here as a message "
            "(you'll find it in your payment app's transaction history)."
        )
        markup = kb([[InlineKeyboardButton("Cancel", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data="dep:cancel")]])
        try:
            await update.message.reply_photo(photo=qr_img_url, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception:
            await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return

    if uid in STATE and STATE[uid].get("flow") == "deposit" and STATE[uid].get("step") == "upi_utr_text":
        if text_in.lower() == "cancel":
            STATE.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=reply_menu(is_admin(uid)))
            return
        utr = text_in.strip().upper()
        if not (utr.isalnum() and 8 <= len(utr) <= 22):
            await update.message.reply_text(
                "❌ That doesn't look like a valid UTR / transaction reference number "
                "(should be 8–22 letters/digits, usually 12 digits). Please check your payment app and send the correct UTR, or press Cancel."
            )
            return

        username = update.effective_user.username or ""
        ok, msg = await _bharatpe_verify_and_credit(repo, context, uid, username, utr)

        if ok:
            STATE.pop(uid, None)
            await update.message.reply_text(msg, reply_markup=reply_menu(is_admin(uid)))
        else:
            retry_kb = kb([
                [InlineKeyboardButton("🔄 Check Again", callback_data=f"bpcheck:{utr}")],
                [InlineKeyboardButton("Cancel", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data="dep:cancel")],
            ])
            await update.message.reply_text(msg, reply_markup=retry_kb)
        return


    if uid in STATE and STATE[uid].get("flow") == "find_credits" and STATE[uid].get("step") == "input":
        if text_in.lower() == "cancel":
            STATE.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=reply_menu(is_admin(uid)))
            return
        if not text_in.isdigit() or int(text_in) <= 0:
            await update.message.reply_text("Send credits as number only, or press Cancel.")
            return
        max_price = int(text_in)
        STATE[uid] = {"flow": "find_credits", "step": "show", "max_price": max_price}
        total_groups = await repo.count_groups_under_price(max_price=max_price)
        if total_groups <= 0:
            await update.message.reply_text("No accounts available in this credits range.", reply_markup=reply_menu(is_admin(uid)))
            STATE.pop(uid, None)
            return
        await update.message.reply_text("✅", reply_markup=reply_menu(is_admin(uid)))
        groups = await repo.list_groups_under_price_page(max_price=max_price, page=0, page_size=10)
        await update.message.reply_text(
            "Results:",
            reply_markup=_find_results_kb(groups, max_price=max_price, page=0, total=total_groups),
        )
        return

    if text_in == "🛒 Buy":
        countries = await repo.list_available_countries()
        if not countries:
            await safe_reply_text(update.message, "No stock available.")
            return
        price_text = await build_country_price_text(repo, countries)
        await safe_reply_text(update.message, price_text, reply_markup=countries_keyboard(countries))
        return

    if text_in == "💳 Deposit":
        STATE[uid] = {"flow": "deposit", "step": "choose"}
        await update.message.reply_text(
            "💳 *Deposit*\n\nChoose deposit method:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb(
                [
                    [InlineKeyboardButton("BharatPe (UPI)", style="primary", icon_custom_emoji_id="5409048419211682843", callback_data="dep:upi")],
                    [InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home")],
                ]
            ),
        )
        return

    if text_in == "💰 Balance":
        await show_balance(update, context, edit=False)
        return

    if text_in == "📜 History":
        total = await repo.count_purchases(user_id=uid)
        items = await repo.list_purchases_page(user_id=uid, page=0, page_size=6)
        lines = ["📜 *Purchase History* (Page 1)", ""]
        if not items:
            lines.append("No purchases yet.")
        else:
            for p in items:
                lines.append(f"• +{p.get('phone','')} | {p.get('country','')} | {p.get('year')} | {p.get('price')} credits")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb([[InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home")]]))
        return

    if text_in in {"🤝 Refer & Earn", "🎁 Refer & Earn", "🎁 Refer & Get Discount"}:
        stats = await repo.get_referral_stats(uid)
        referrals = int(stats.get("referrals", 0))
        earned = float(stats.get("total_earned", 0.0))
        msg = (
            "🤝 Refer & Earn\n\n"
            f"Invite friends and earn {REFERRAL_PERCENT:.1f}% of their deposits forever!\n\n"
            "📊 Your Stats\n"
            f"• 👥 Referrals: {referrals}\n"
            f"• 💰 Total Earned: ₹{earned:.2f}\n\n"
            "🔗 Your Referral Link\n"
            f"{_ref_link(uid)}"
        )
        await update.message.reply_text(msg, parse_mode=None, reply_markup=reply_menu(is_admin(uid)))
        return

    if text_in == "🆘 Support":
        await update.message.reply_text(f"Support: @{SUPPORT_USERNAME}")
        return

    if text_in == "🛠 Admin" and is_admin(uid):
        await update.message.reply_text("Admin Panel:", reply_markup=kb([[InlineKeyboardButton("Open", style="primary", callback_data="admin:menu")]]))
        return

# ---------- Document Handler ----------
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document uploads – currently only admin session zip upload."""
    if await _ban_guard(update, context):
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Unauthorized.")
        return
    if uid not in STATE or STATE[uid].get("flow") != "admin_upload_session":
        await update.message.reply_text("Please start with 'Upload Session' from admin panel.")
        return
    from admin import process_uploaded_session
    handled = await process_uploaded_session(update, context, uid, STATE)
    if not handled:
        await update.message.reply_text("❌ Invalid document. Please send a ZIP file.")

# ---------- Callback Handler ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _ban_guard(update, context):
        return
    query = update.callback_query
    if not query:
        return
    uid = update.effective_user.id
    data = query.data or ""

    handled = await admin_module.handle_admin_callback(update, context, STATE)
    if handled and data.startswith("admin:"):
        return

    repo: Repo = context.application.bot_data["repo"]
    account_manager: AccountManager = context.application.bot_data["account_manager"]

    handled = await device_manager.handle_device_callbacks(query, context, uid, data, repo, account_manager)
    if handled:
        return

    # ============ PAYTM AUTO-VERIFY ============
    if data.startswith("paytm:check:"):
        # Legacy auto-verify callback (old messages). Auto-verify API removed; direct user to new flow.
        await safe_query_answer(query, cache_time=0)
        await safe_edit(
            query.message,
            "This payment method has been replaced. Please start a new deposit.",
            reply_markup=kb([[InlineKeyboardButton("New Deposit", style="primary", icon_custom_emoji_id="5409048419211682843", callback_data="dep:start")]]),
            parse_mode=None,
        )
        return

    if data.startswith("bpcheck:"):
        await safe_query_answer(query, cache_time=0)
        utr = data.split(":", 1)[1]
        username = update.effective_user.username or ""
        ok, msg = await _bharatpe_verify_and_credit(repo, context, uid, username, utr)
        if ok:
            STATE.pop(uid, None)
            await safe_edit(query.message, msg, reply_markup=kb([[InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home")]]), parse_mode=None)
        else:
            retry_kb = kb([
                [InlineKeyboardButton("🔄 Check Again", callback_data=f"bpcheck:{utr}")],
                [InlineKeyboardButton("Cancel", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data="dep:cancel")],
            ])
            await safe_edit(query.message, msg, reply_markup=retry_kb, parse_mode=None)
        return

    # ---------- Join Verify ----------
    if data == "join:verify":
        joined, join_err, missing = await _is_joined(update, context)
        if not joined:
            if join_err and ("not enough rights" in join_err.lower() or "forbidden" in join_err.lower() or "chat not found" in join_err.lower()):
                await safe_query_answer(
                    query,
                    f"⚠️ Verification unavailable. Bot must be admin in @{CHANNEL_USERNAME} and @{REPORT_CHANNEL_USERNAME}.",
                    show_alert=True,
                )
                return
            miss_txt = ", ".join([f"@{c}" for c in (missing or [CHANNEL_USERNAME, REPORT_CHANNEL_USERNAME])])
            await safe_query_answer(
                query,
                f"❌ Not joined yet. Please join {miss_txt} then click Verify again.",
                show_alert=True,
            )
            return
        available = await repo.count_available_accounts()
        user = await repo.ensure_user(uid, username=update.effective_user.username)
        is_admin_user = admin_module.is_admin(uid)
        text = _home_caption(uid=uid, credits=int(user.get('credits', 0)), stock=available)
        await safe_edit(query.message, text, reply_markup=main_menu(is_admin_user), parse_mode=None)
        await safe_query_answer(query, "✅ Verified", show_alert=False)
        try:
            await query.message.reply_text("✅ Verified. Menu enabled.", reply_markup=reply_menu(is_admin_user))
        except Exception:
            pass
        return

    if data == "ref:menu":
        await safe_query_answer(query, cache_time=0)
        stats = await repo.get_referral_stats(uid)
        referrals = int(stats.get("referrals", 0))
        earned = float(stats.get("total_earned", 0.0))
        text = (
            "🤝 Refer & Earn\n\n"
            f"Invite friends and earn {REFERRAL_PERCENT:.1f}% of their deposits forever!\n\n"
            "📊 Your Stats\n"
            f"• 👥 Referrals: {referrals}\n"
            f"• 💰 Total Earned: ₹{earned:.2f}\n\n"
            "🔗 Your Referral Link\n"
            f"{_ref_link(uid)}"
        )
        await safe_edit(query.message, text, reply_markup=kb([[InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")]]), parse_mode=None)
        return

    if data == "menu:home":
        await safe_query_answer(query, cache_time=0)
        available = await repo.count_available_accounts()
        user = await repo.ensure_user(uid, username=update.effective_user.username)
        is_admin_user = admin_module.is_admin(uid)
        text = _home_caption(uid=uid, credits=int(user.get('credits', 0)), stock=available)
        await safe_edit(query.message, text, reply_markup=main_menu(is_admin_user), parse_mode=None)
        return

    if data == "me:balance":
        await safe_query_answer(query, cache_time=0)
        await show_balance(update, context, edit=True)
        return

    if data.startswith("me:history:"):
        await safe_query_answer(query, cache_time=0)
        page = int(data.split(":", 2)[2])
        total = await repo.count_purchases(user_id=uid)
        page_size = 6
        max_page = max(0, (total - 1) // page_size) if total else 0
        if page > max_page:
            page = max_page
        items = await repo.list_purchases_page(user_id=uid, page=page, page_size=page_size)
        lines = [f"📜 *Purchase History*  (Page {page+1}/{max_page+1 if total else 1})", ""]
        if not items:
            lines.append("No purchases yet.")
        else:
            for p in items:
                phone = p.get("phone") or ""
                country = p.get("country") or ""
                year = p.get("year")
                price = p.get("price")
                lines.append(f"• +{phone} | {country} | {year} | {price} credits")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", style="primary", callback_data=f"me:history:{page-1}"))
        if page < max_page:
            nav.append(InlineKeyboardButton("Next ➡️", style="primary", callback_data=f"me:history:{page+1}"))
        rows = []
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home")])
        await safe_edit(query.message, "\n".join(lines), reply_markup=kb(rows), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "dep:start":
        await safe_query_answer(query, cache_time=0)
        STATE[uid] = {"flow": "deposit", "step": "choose"}
        await safe_edit(
            query.message,
            "💳 *Deposit*\n\nChoose deposit method:",
            reply_markup=kb(
                [
                    [InlineKeyboardButton("BharatPe (UPI)", style="primary", icon_custom_emoji_id="5409048419211682843", callback_data="dep:upi")],
                    [InlineKeyboardButton("🏠 Menu", style="primary", callback_data="menu:home")],
                ]
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data in {"dep:reject", "dep:cancel"}:
        await safe_query_answer(query, cache_time=0)
        if uid in STATE and STATE[uid].get("flow") in {"deposit", "buy_session"}:
            STATE.pop(uid, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        available = await repo.count_available_accounts()
        user = await repo.ensure_user(uid, username=update.effective_user.username)
        is_admin_user = admin_module.is_admin(uid)
        text = _home_caption(uid=uid, credits=int(user.get('credits', 0)), stock=available)
        try:
            await context.bot.send_photo(
                chat_id=uid,
                photo=START_IMAGE,
                caption=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu(is_admin_user),
            )
        except Exception:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu(is_admin_user),
            )
        return

    if data == "dep:upi":
        await safe_query_answer(query, cache_time=0)
        STATE[uid] = {"flow": "deposit", "step": "upi_amount_text", "method": "bharatpe"}
        await safe_edit(
            query.message,
            "💳 BharatPe UPI Deposit\n\nType the amount (₹) you want to deposit and send it as a message.\n\nExample: 100",
            reply_markup=kb([[InlineKeyboardButton("Cancel", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data="dep:cancel")]]),
            parse_mode=None,
        )
        return

    # ---------- Buy Session (raw session files, no live OTP relay) ----------
    if data == "session:start":
        await safe_query_answer(query, cache_time=0)
        countries = await repo.list_available_countries()
        prices = await repo.get_session_prices()
        priced_countries = [c for c in countries if int(prices.get(c.get("country"), 0)) > 0]
        if not priced_countries:
            await safe_edit(
                query.message,
                "🗂 Buy Session\n\nNo countries are priced for session sale yet. Please contact support.",
                reply_markup=back_to_menu(),
                parse_mode=None,
            )
            return
        rows: list[list[InlineKeyboardButton]] = []
        current: list[InlineKeyboardButton] = []
        for c in priced_countries:
            code = c.get("country") or "?"
            emoji = c.get("country_emoji") or ""
            count = c.get("count", 0)
            price = int(prices.get(code, 0))
            current.append(InlineKeyboardButton(f"{emoji} {code} ₹{price} ({count})", callback_data=f"session:country:{code}"))
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        rows.append([InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")])
        await safe_edit(
            query.message,
            "🗂 Buy Session\n\nChoose a country (price shown per session):",
            reply_markup=kb(rows),
            parse_mode=None,
        )
        return

    if data.startswith("session:country:"):
        await safe_query_answer(query, cache_time=0)
        country = data.split(":", 2)[2]
        price = await repo.get_session_price_for_country(country)
        available = await repo.count_available_accounts()  # overall stock display; per-country count already shown at selection
        if price <= 0:
            await safe_edit(
                query.message,
                "❌ This country isn't priced for session sale anymore. Please go back and pick another.",
                reply_markup=kb([[InlineKeyboardButton("⬅️ Back", style="primary", callback_data="session:start")]]),
                parse_mode=None,
            )
            return
        STATE[uid] = {"flow": "buy_session", "step": "qty_text", "country": country, "price": price}
        await safe_edit(
            query.message,
            f"🗂 Buy Session — {country}\n\nPrice: ₹{price} per session\n\n"
            "Type how many sessions you want to buy (e.g. 10).",
            reply_markup=kb([[InlineKeyboardButton("Cancel", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data="dep:cancel")]]),
            parse_mode=None,
        )
        return

    # ---------- Shop ----------
    if data == "shop:countries":
        await safe_query_answer(query, cache_time=0)
        countries = await repo.list_available_countries()
        if not countries:
            await safe_query_answer(query, "❌ No stock available right now.", show_alert=True)
            return
        price_text = await build_country_price_text(repo, countries)
        await safe_edit(query.message, price_text, reply_markup=countries_keyboard(countries), parse_mode=None)
        return

    if data.startswith("shop:country:"):
        await safe_query_answer(query, cache_time=0)
        country = data.split(":", 2)[2]
        years = await repo.list_available_years_for_country(country)
        await safe_edit(
            query.message,
            f"{country}: Select year:",
            reply_markup=years_keyboard(country, years),
            parse_mode=None,
        )
        return

    if data.startswith("shop:year:"):
        await safe_query_answer(query, cache_time=0)
        _, _, country, year_token = data.split(":", 3)
        year_text = year_token
        if year_token == "none":
            year_for_range = None
        elif year_token.isdigit():
            year_for_range = int(year_token)
        else:
            year_for_range = year_token
        pr = await repo.available_price_range(country=country, year=year_for_range)
        min_p = pr.get("min_price")
        max_p = pr.get("max_price")
        if min_p is None:
            price_line = "Price: not set"
        elif min_p == max_p:
            price_line = f"Price: {min_p} credit(s)"
        else:
            price_line = f"Price: {min_p} - {max_p} credit(s)"
        tokens = await repo.get_tokens(uid)
        if tokens > 0:
            price_line += "\nDiscount available: -5 credits (1 token will be used)"
        await safe_edit(
            query.message,
            f"Confirm purchase\n\nCountry: {country}\nYear: {year_text}\n{price_line}",
            reply_markup=buy_confirm_keyboard(country, year_token),
            parse_mode=None,
        )
        return

    if data.startswith("shop:buy:"):
        await safe_query_answer(query, cache_time=0)
        _, _, country, year_token = data.split(":", 3)
        terms = (
            "📌 *Buyer Terms & Conditions*\n\n"
            "• ✅ No refunds after purchase.\n"
            "• ✅ Only refund case: OTP not received.\n"
            "• ✅ Login immediately and use it.\n"
            "• ✅ By purchasing, you accept full responsibility.\n\n"
            "Do you agree?"
        )
        await safe_edit(
            query.message,
            terms,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton("I Agree", style="success", icon_custom_emoji_id="5206607081334906820", callback_data=f"shop:agree:{country}:{year_token}"),
                        InlineKeyboardButton("Decline", style="danger", icon_custom_emoji_id="5440660757194744323", callback_data=f"shop:decline:{country}:{year_token}"),
                    ],
                    [InlineKeyboardButton("⬅️ Back", style="primary", callback_data=f"shop:country:{country}")],
                ]
            ),
        )
        return

    if data.startswith("shop:decline:"):
        await safe_query_answer(query, "Cancelled", show_alert=False)
        _, _, country, _yt = data.split(":", 3)
        years = await repo.list_available_years_for_country(country)
        await safe_edit(
            query.message,
            f"{country}: Select year:",
            reply_markup=years_keyboard(country, years),
            parse_mode=None,
        )
        return

    if data.startswith("shop:agree:"):
        _, _, country, year_token = data.split(":", 3)
        if year_token == "none":
            year = None
        elif year_token.isdigit():
            year = int(year_token)
        else:
            year = year_token
        account, reason = await repo.buy_account_filtered(
            user_id=uid,
            username=(update.effective_user.username or ""),
            country=country,
            year=year,
        )
        if not account:
            if reason in {"insufficient_credits", "no_affordable"}:
                udoc = await repo.ensure_user(uid, username=update.effective_user.username)
                have = int(udoc.get("credits", 0))
                if year_token == "none":
                    year_for_range = None
                elif year_token.isdigit():
                    year_for_range = int(year_token)
                else:
                    year_for_range = year_token
                pr = await repo.available_price_range(country=country, year=year_for_range)
                need = pr.get("min_price")
                if need is None:
                    await safe_query_answer(query, f"❌ Not enough credits.\nYou have: {have}", show_alert=True)
                else:
                    await safe_query_answer(query, f"❌ Not enough credits.\nYou have: {have}\nMinimum price: {int(need)}", show_alert=True)
                return
            if reason == "no_accounts":
                await safe_query_answer(query, "❌ No account left in this category.", show_alert=True)
                return
            await safe_query_answer(query, f"❌ Purchase failed ({reason}).", show_alert=True)
            return
        await query.message.reply_text(
            "✅ Purchase confirmed.\n\n⚠️ *No refunds on any issue other than OTP not received.*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_purchase_details(update, context, account)
        return

    if data == "find:credits":
        await safe_query_answer(query, cache_time=0)
        STATE[uid] = {"flow": "find_credits", "step": "input"}
        await query.message.reply_text(
            "🔎 Find by Credits\n\nSend max credits (numbers only):\nExample: 40\n\nPress Cancel to stop.",
            reply_markup=cancel_only_menu(),
        )
        return

    if data.startswith("find:page:"):
        await safe_query_answer(query, cache_time=0)
        _, _, max_price_s, page_s = data.split(":", 3)
        max_price = int(max_price_s) if max_price_s.isdigit() else 0
        page = int(page_s) if page_s.isdigit() else 0
        total = await repo.count_groups_under_price(max_price=max_price)
        groups = await repo.list_groups_under_price_page(max_price=max_price, page=page, page_size=10)
        await safe_edit(
            query.message,
            f"Results (Page {page+1}):",
            reply_markup=_find_results_kb(groups, max_price=max_price, page=page, total=total),
            parse_mode=None,
        )
        return

    if data.startswith("find:pickgrp:"):
        await safe_query_answer(query, cache_time=0)
        _, _, country, year_token, price_s = data.split(":", 4)
        tokens = await repo.get_tokens(uid)
        discount_line = f"\nDiscount: -5 (tokens available: {tokens})\nFinal: {max(0, int(price_s) - 5)} credits" if tokens > 0 and price_s.isdigit() else ""
        await safe_edit(
            query.message,
            f"Confirm purchase\n\nCountry: {country}\nYear: {year_token}\nPrice: {price_s} credits{discount_line}\n\n⚠️ No refunds other than OTP not received.",
            reply_markup=kb(
                [
                    [InlineKeyboardButton("Confirm Buy", style="success", icon_custom_emoji_id="5206607081334906820", callback_data=f"find:buygrp:{country}:{year_token}:{price_s}")],
                    [InlineKeyboardButton("⬅️ Back", style="primary", callback_data="menu:home")],
                ]
            ),
            parse_mode=None,
        )
        return

    if data.startswith("find:buygrp:"):
        _, _, country, year_token, price_s = data.split(":", 4)
        price = int(price_s) if price_s.isdigit() else 0
        year = None if year_token == "none" else (int(year_token) if year_token.isdigit() else year_token)
        account, reason = await repo.buy_account_by_group(
            user_id=uid,
            username=(update.effective_user.username or ""),
            country=country,
            year=year,
            price=price,
        )
        if not account:
            if reason == "insufficient_credits":
                udoc = await repo.ensure_user(uid, username=update.effective_user.username)
                have = int(udoc.get("credits", 0))
                await safe_query_answer(query, f"❌ Not enough credits. You have: {have}", show_alert=True)
                return
            await safe_query_answer(query, "❌ Purchase failed.", show_alert=True)
            return
        await query.message.reply_text(
            "✅ Purchase confirmed.\n\n⚠️ No refunds on any issue other than OTP not received.",
            parse_mode=None,
        )
        await send_purchase_details(update, context, account)
        return

# ---------- Build Application ----------
def build_app() -> Application:
    require_token()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    repo = Repo(get_db())
    async def send_message(chat_id: int, text: str):
        await app.bot.send_message(chat_id=chat_id, text=text)
    account_manager = AccountManager(send_message, bot=app.bot)
    app.bot_data["repo"] = repo
    app.bot_data["account_manager"] = account_manager

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("bd", bd_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_error_handler(on_error)
    return app

def main() -> None:
    from premium_emoji import patch_premium_emojis
    patch_premium_emojis()
    if not ADMIN_USER_IDS:
        logger.warning("ADMIN_USER_IDS is empty.")
    while True:
        try:
            app = build_app()
            print("ID Store Bot started (Paytm auto‑verify + session upload enabled)")
            app.run_polling(close_loop=False, drop_pending_updates=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.exception("Bot crashed; restarting in 5 seconds: %s", e)
            import time
            time.sleep(5)
        import time
        time.sleep(2)

if __name__ == "__main__":
    main()