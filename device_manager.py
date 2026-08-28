"""Device management for sold Telegram accounts (Telethon sessions)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bson import ObjectId
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.errors.rpcerrorlist import (
    FreshResetAuthorisationForbiddenError,
    HashInvalidError,
    UserDeactivatedError,
)

if TYPE_CHECKING:
    from bot import AccountManager, Repo

from config import ADMIN_USER_IDS

def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_USER_IDS

def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)

async def safe_edit(msg, text: str, *, parse_mode=None, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        pass

def _parse_oid(s: str) -> ObjectId | None:
    try:
        return ObjectId(str(s))
    except Exception:
        return None

async def handle_device_callbacks(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    data: str,
    repo: "Repo",
    account_manager: "AccountManager",
) -> bool:
    if not data.startswith("dev:"):
        return False

    async def _ensure_buyer_access(account_id: ObjectId) -> dict[str, Any] | None:
        acc = await repo.get_account(account_id)
        if not acc:
            return None
        if is_admin(uid):
            return acc
        sold_to = acc.get("sold_to_user_id")
        if sold_to is not None and int(sold_to) != int(uid):
            return None
        return acc

    if data.startswith("dev:menu:"):
        acc_id_s = data.split(":", 2)[2]
        account_id = _parse_oid(acc_id_s)
        if not account_id:
            await query.answer("Invalid account.", show_alert=True)
            return True
        acc = await _ensure_buyer_access(account_id)
        if not acc:
            await query.answer("Access denied.", show_alert=True)
            return True
        try:
            if is_admin(uid):
                await account_manager.ensure_connected_for_admin_monitor(account_id, acc)
            else:
                await account_manager.ensure_connected_for_account(account_id, acc, uid)
        except Exception:
            pass
        client = account_manager.get_client(account_id)
        if not client:
            await query.answer("❌ Bot session not found.", show_alert=True)
            return True
        try:
            auths = await client(GetAuthorizationsRequest())
            items = list(getattr(auths, "authorizations", []) or [])
        except UserDeactivatedError:
            await query.answer("❌ Account is deactivated/banned.", show_alert=True)
            return True
        except Exception as e:
            logging.warning(f"GetAuthorizationsRequest failed: {e.__class__.__name__}")
            await query.answer("❌ Failed to fetch devices.", show_alert=True)
            return True
        if not items:
            await safe_edit(
                query.message,
                "🛠️ Manage Devices\n\nNo active devices found.",
                parse_mode=None,
                reply_markup=kb([[InlineKeyboardButton("⬅️ Back", callback_data="menu:home")]]),
            )
            return True
        rows: list[list[InlineKeyboardButton]] = []
        for i, a in enumerate(items, start=1):
            h = int(getattr(a, "hash", 0) or 0)
            dev = (getattr(a, "device_model", "") or "").strip()
            plat = (getattr(a, "platform", "") or "").strip()
            app = (getattr(a, "app_name", "") or "").strip()
            current = bool(getattr(a, "current", False))
            if current:
                label = "🤖 Bot Device (current)"
                rows.append([InlineKeyboardButton(label, callback_data=f"dev:logout_current:{str(account_id)}")])
                continue
            else:
                label_parts = [p for p in [dev, plat, app] if p]
                label = "📱 " + (" ".join(label_parts) if label_parts else f"Device {i}")
            rows.append([InlineKeyboardButton(label, callback_data=f"dev:logout:{str(account_id)}:{h}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
        await safe_edit(
            query.message,
            "🛠️ Manage Devices\n\nTap a device to log it out:",
            parse_mode=None,
            reply_markup=kb(rows),
        )
        return True

    if data.startswith("dev:logout_current:"):
        acc_id_s = data.split(":", 2)[2]
        account_id = _parse_oid(acc_id_s)
        if not account_id:
            await query.answer("Invalid request.", show_alert=True)
            return True
        acc = await _ensure_buyer_access(account_id)
        if not acc:
            await query.answer("Access denied.", show_alert=True)
            return True
        try:
            if is_admin(uid):
                await account_manager.ensure_connected_for_admin_monitor(account_id, acc)
            else:
                await account_manager.ensure_connected_for_account(account_id, acc, uid)
        except Exception:
            pass
        client = account_manager.get_client(account_id)
        if not client:
            await query.answer("❌ Bot session not found.", show_alert=True)
            return True
        try:
            await client.log_out()
        except Exception:
            await query.answer("❌ Failed to logout current session.", show_alert=True)
            return True
        await query.answer("✅ Logged out bot session.", show_alert=True)
        return True

    if data.startswith("dev:logout:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.answer("Invalid request.", show_alert=True)
            return True
        _, _, acc_id_s, hash_s = parts
        account_id = _parse_oid(acc_id_s)
        if not account_id or not str(hash_s).lstrip("-").isdigit():
            await query.answer("Invalid request.", show_alert=True)
            return True
        acc = await _ensure_buyer_access(account_id)
        if not acc:
            await query.answer("Access denied.", show_alert=True)
            return True
        try:
            if is_admin(uid):
                await account_manager.ensure_connected_for_admin_monitor(account_id, acc)
            else:
                await account_manager.ensure_connected_for_account(account_id, acc, uid)
        except Exception:
            pass
        client = account_manager.get_client(account_id)
        if not client:
            await query.answer("❌ Bot session not found.", show_alert=True)
            return True
        h = int(hash_s)
        try:
            await client(ResetAuthorizationRequest(hash=h))
        except FreshResetAuthorisationForbiddenError:
            await query.answer("❌ Session is too new to reset other devices. Try again in a few minutes.", show_alert=True)
            return True
        except HashInvalidError:
            await query.answer("❌ Device already logged out or hash invalid.", show_alert=True)
            return True
        except UserDeactivatedError:
            await query.answer("❌ Account is deactivated/banned.", show_alert=True)
            return True
        except Exception as e:
            logging.warning(f"ResetAuthorizationRequest failed: {e.__class__.__name__}")
            await query.answer("❌ Failed to logout device.", show_alert=True)
            return True
        await query.answer("✅ Logged out.", show_alert=True)
        try:
            auths = await client(GetAuthorizationsRequest())
            items = list(getattr(auths, "authorizations", []) or [])
        except (UserDeactivatedError, Exception):
            items = []
        if not items:
            await safe_edit(
                query.message,
                "🛠️ Manage Devices\n\nAll devices logged out or session ended.",
                parse_mode=None,
                reply_markup=kb([[InlineKeyboardButton("⬅️ Back", callback_data="menu:home")]]),
            )
            return True
        rows: list[list[InlineKeyboardButton]] = []
        for i, a in enumerate(items, start=1):
            hh = int(getattr(a, "hash", 0) or 0)
            dev = (getattr(a, "device_model", "") or "").strip()
            plat = (getattr(a, "platform", "") or "").strip()
            app = (getattr(a, "app_name", "") or "").strip()
            current = bool(getattr(a, "current", False))
            if current:
                label = "🤖 Bot Device (current)"
            else:
                label_parts = [p for p in [dev, plat, app] if p]
                label = "📱 " + (" ".join(label_parts) if label_parts else f"Device {i}")
            rows.append([InlineKeyboardButton(label, callback_data=f"dev:logout:{str(account_id)}:{hh}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
        await safe_edit(
            query.message,
            "🛠️ Manage Devices\n\nTap a device to log it out:",
            parse_mode=None,
            reply_markup=kb(rows),
        )
        return True

    await query.answer("Unknown action.", show_alert=True)
    return True