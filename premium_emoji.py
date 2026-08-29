"""
Global premium-emoji patch.

Automatically converts known plain emoji inside any outgoing message text
or caption into Telegram "custom emoji" entities (the premium animated
icons), using the same 10 emoji IDs already used on buttons.

Because Telegram does not allow combining `parse_mode` with `entities` /
`caption_entities` in the same call, any call that would get custom emoji
entities has its `parse_mode` stripped (Markdown/HTML formatting in that
text will show as plain characters instead of being rendered).

Call `patch_premium_emojis()` once, early, before the bot starts polling.
"""

from telegram import MessageEntity, Bot, Message, CallbackQuery

# Same IDs used for the button icons.
EMOJI_MAP = {
    "🔥": "5424972470023104089",
    "🛒": "5424972470023104089",
    "🇮🇳": "5229086076873748176",
    "🇨🇦": "5222001124592071204",
    "🇲🇲": "5433666360003540231",
    "🇮🇩": "5224405893960969756",
    "🇨🇳": "5931311876855041546",
    "⬇️": "5373260879095686059",
    "📥": "5443127283898405358",
    "✅": "5206607081334906820",
    "💲": "5409048419211682843",
    "💰": "5409048419211682843",
    "💳": "5409048419211682843",
    "💸": "5409048419211682843",
    "⚠️": "5447644880824181073",
    "🔒": "5447644880824181073",
    "📊": "5449683594425410231",
    "👍": "5337080053119336309",
    "🔔": "5456140674028019486",
    "📢": "5456140674028019486",
    "ℹ️": "5323442290708985472",
    "‼️": "5440660757194744323",
    "💎": "4963511421280192936",
}

# Longer keys first so "⚠️" (with variation selector) matches before a bare "⚠".
_EMOJI_KEYS = sorted(EMOJI_MAP.keys(), key=len, reverse=True)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def build_premium_entities(text: str):
    """Scan text and return MessageEntity(custom_emoji) for every known emoji found."""
    if not text:
        return []
    entities = []
    i = 0
    while i < len(text):
        matched = None
        for emoji in _EMOJI_KEYS:
            if text.startswith(emoji, i):
                matched = emoji
                break
        if matched:
            offset = _utf16_len(text[:i])
            length = _utf16_len(matched)
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=EMOJI_MAP[matched],
                )
            )
            i += len(matched)
        else:
            i += 1
    return entities


def _apply(kwargs, text_key, entities_key):
    """Given kwargs for a send/edit call, inject entities for text_key if it
    contains known emoji, and drop parse_mode so Telegram accepts entities."""
    text = kwargs.get(text_key)
    if isinstance(text, str) and kwargs.get(entities_key) is None:
        ents = build_premium_entities(text)
        if ents:
            kwargs[entities_key] = ents
            kwargs.pop("parse_mode", None)
    return kwargs


def _wrap(orig, text_pos, text_key, entities_key):
    async def wrapper(self, *args, **kwargs):
        # Normalize positional text arg into kwargs so _apply can see it.
        if text_key not in kwargs and len(args) > text_pos and isinstance(args[text_pos], str):
            args = list(args)
            kwargs[text_key] = args.pop(text_pos)
        kwargs = _apply(kwargs, text_key, entities_key)
        return await orig(self, *args, **kwargs)
    return wrapper


def patch_premium_emojis():
    """Monkey-patch PTB's send/edit methods so ANY outgoing text/caption
    containing a tracked emoji automatically gets the premium custom-emoji
    entity, everywhere in the bot, with no per-call-site changes needed."""

    Message.reply_text = _wrap(Message.reply_text, 0, "text", "entities")
    Message.edit_text = _wrap(Message.edit_text, 0, "text", "entities")
    Message.reply_photo = _wrap(Message.reply_photo, 1, "caption", "caption_entities")
    Message.edit_caption = _wrap(Message.edit_caption, 0, "caption", "caption_entities")

    CallbackQuery.edit_message_text = _wrap(CallbackQuery.edit_message_text, 0, "text", "entities")
    CallbackQuery.edit_message_caption = _wrap(CallbackQuery.edit_message_caption, 0, "caption", "caption_entities")

    Bot.send_message = _wrap(Bot.send_message, 1, "text", "entities")
    Bot.edit_message_text = _wrap(Bot.edit_message_text, 0, "text", "entities")
    Bot.send_photo = _wrap(Bot.send_photo, 2, "caption", "caption_entities")
