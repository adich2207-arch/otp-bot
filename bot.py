import os
import logging
import traceback
import imaplib
import email
import re
import asyncio
from email.header import decode_header
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv

# Load .env from the script's own directory — works on Render, KataBump, and local
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
DATABASE_URL     = os.getenv("DATABASE_URL", "")
PORT             = int(os.getenv("PORT", "8080"))
API_ID           = int(os.getenv("API_ID", "0"))
API_HASH         = os.getenv("API_HASH", "")
REFERRAL_COMMISSION = 0.02

# ── Channel IDs ───────────────────────────────────────────────────────────────
# TRADES_CHANNEL : all activity goes here — buy/sell, deposits, withdrawals
TRADES_CHANNEL = int(os.getenv("TRADES_CHANNEL_ID", "0"))

# ── Force Join ────────────────────────────────────────────────────────────────
# Users must join before using the bot.
# FORCE_CHANNEL_1 / FORCE_CHANNEL_2 : channels (read-only announcements etc.)
# FORCE_GROUP                        : your community group (users interact here)
# Set IDs as negative numbers (e.g. -1001234567890) or @username strings.
# Leave as "0" to disable that slot.
FORCE_CHANNEL_1     = os.getenv("FORCE_CHANNEL_1",     "0")
FORCE_CHANNEL_2     = os.getenv("FORCE_CHANNEL_2",     "0")
FORCE_GROUP         = os.getenv("FORCE_GROUP",          "0")
FORCE_CHANNEL_1_URL = os.getenv("FORCE_CHANNEL_1_URL", "https://t.me/yourchannel1")
FORCE_CHANNEL_2_URL = os.getenv("FORCE_CHANNEL_2_URL", "https://t.me/yourchannel2")
FORCE_GROUP_URL     = os.getenv("FORCE_GROUP_URL",      "https://t.me/yourgroup")
# Optional custom display names for force-join buttons (overrides live title fetch)
FORCE_CHANNEL_1_NAME = os.getenv("FORCE_CHANNEL_1_NAME", "")
FORCE_CHANNEL_2_NAME = os.getenv("FORCE_CHANNEL_2_NAME", "")
FORCE_GROUP_NAME     = os.getenv("FORCE_GROUP_NAME",     "Otp Seller Group")

def _fc(val: str):
    """Convert a force-channel env value to int (if numeric) or str (@username). Returns None if disabled."""
    val = val.strip()
    if not val or val == "0":
        return None
    try:
        return int(val)
    except ValueError:
        return val  # @username string

ptb_app: Application = None

# ── Channel helper ────────────────────────────────────────────────────────────
async def send_to_channel(bot, channel_id: int, text: str, parse_mode: str = "HTML", reply_markup=None):
    """Send a message to a channel. Silently skips if channel_id is 0 or not configured."""
    if not channel_id:
        return
    try:
        await bot.send_message(channel_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Channel send failed (channel={channel_id}): {e}")

# ── Force Join helper ─────────────────────────────────────────────────────────
async def check_force_join(bot, user_id: int) -> list:
    """Return a list of (channel_id_or_username, invite_url, channel_title) for channels/groups the user has NOT joined."""
    not_joined = []
    pairs = [
        (_fc(FORCE_CHANNEL_1), FORCE_CHANNEL_1_URL, FORCE_CHANNEL_1_NAME),
        (_fc(FORCE_CHANNEL_2), FORCE_CHANNEL_2_URL, FORCE_CHANNEL_2_NAME),
        (_fc(FORCE_GROUP),     FORCE_GROUP_URL,     FORCE_GROUP_NAME),
    ]
    for channel, url, custom_name in pairs:
        if not channel:
            continue  # slot disabled

        # Use custom name if set, otherwise fetch live from Telegram
        if custom_name:
            title = custom_name
        else:
            try:
                chat = await bot.get_chat(channel)
                title = chat.title or str(channel)
            except Exception as e:
                logger.warning(f"[ForceJoin] get_chat({channel}) failed: {e}")
                title = str(channel)

        try:
            member = await bot.get_chat_member(channel, user_id)
            logger.info(f"[ForceJoin] user={user_id} channel={channel} status={member.status}")
            if member.status in ("left", "kicked", "banned"):
                not_joined.append((channel, url, title))
        except Exception as e:
            logger.warning(f"[ForceJoin] get_chat_member({channel}, {user_id}) failed: {e} — treating as not joined")
            not_joined.append((channel, url, title))
    return not_joined

# ── Unicode bold text helper ──────────────────────────────────────────────────
_BM = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _BM[_c] = chr(0x1D5D4 + _i)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BM[_c] = chr(0x1D5EE + _i)
for _i, _c in enumerate("0123456789"):
    _BM[_c] = chr(0x1D7EC + _i)

def b(text: str) -> str:
    """Convert text to Unicode Mathematical Bold Sans-Serif — works in buttons too."""
    return "".join(_BM.get(c, c) for c in text)

# ── Currency formatter ────────────────────────────────────────────────────────
# USD to INR conversion rate — update this value to adjust the displayed INR amount.
USD_TO_INR = float(os.getenv("USD_TO_INR", "90.0"))

def fmt(usd: float) -> str:
    """Format a USD amount as  ₹{inr} ($x.xx)  e.g. ₹417 ($5.00)"""
    inr = round(float(usd) * USD_TO_INR)
    return f"₹{inr} (${float(usd):.2f})"

def mask_phone(phone: str) -> str:
    """Format phone as +923*******19 — shows first 3 and last 2 digits, masks the rest."""
    p = phone.strip().lstrip("+")
    if len(p) < 6:
        return phone  # too short to mask
    visible_start = p[:3]
    visible_end   = p[-2:]
    masked        = "*" * (len(p) - 5)
    return f"+{visible_start}{masked}{visible_end}"

_BUILTIN_DIAL_MAP = [
    # sorted longest-first so more specific codes match before shorter ones
    ("880", "BD", "Bangladesh"), ("977", "NP", "Nepal"), ("971", "AE", "UAE"),
    ("966", "SA", "Saudi Arabia"), ("234", "NG", "Nigeria"), ("380", "UA", "Ukraine"),
    ("351", "PT", "Portugal"), ("353", "IE", "Ireland"), ("358", "FI", "Finland"),
    ("370", "LT", "Lithuania"), ("371", "LV", "Latvia"), ("372", "EE", "Estonia"),
    ("375", "BY", "Belarus"), ("994", "AZ", "Azerbaijan"), ("998", "UZ", "Uzbekistan"),
    ("996", "KG", "Kyrgyzstan"), ("992", "TJ", "Tajikistan"), ("993", "TM", "Turkmenistan"),
    ("995", "GE", "Georgia"), ("374", "AM", "Armenia"), ("964", "IQ", "Iraq"),
    ("963", "SY", "Syria"), ("962", "JO", "Jordan"), ("961", "LB", "Lebanon"),
    ("967", "YE", "Yemen"), ("968", "OM", "Oman"), ("974", "QA", "Qatar"),
    ("973", "BH", "Bahrain"), ("965", "KW", "Kuwait"), ("213", "DZ", "Algeria"),
    ("216", "TN", "Tunisia"), ("212", "MA", "Morocco"), ("218", "LY", "Libya"),
    ("249", "SD", "Sudan"), ("251", "ET", "Ethiopia"), ("254", "KE", "Kenya"),
    ("255", "TZ", "Tanzania"), ("256", "UG", "Uganda"), ("260", "ZM", "Zambia"),
    ("263", "ZW", "Zimbabwe"), ("233", "GH", "Ghana"), ("225", "CI", "Ivory Coast"),
    ("221", "SN", "Senegal"), ("237", "CM", "Cameroon"), ("243", "CD", "DR Congo"),
    ("94",  "LK", "Sri Lanka"), ("95",  "MM", "Myanmar"), ("98",  "IR", "Iran"),
    ("92",  "PK", "Pakistan"), ("91",  "IN", "India"),   ("90",  "TR", "Turkey"),
    ("86",  "CN", "China"),    ("84",  "VN", "Vietnam"), ("82",  "KR", "South Korea"),
    ("81",  "JP", "Japan"),    ("66",  "TH", "Thailand"), ("65",  "SG", "Singapore"),
    ("63",  "PH", "Philippines"), ("62", "ID", "Indonesia"), ("60", "MY", "Malaysia"),
    ("55",  "BR", "Brazil"),   ("54",  "AR", "Argentina"), ("52", "MX", "Mexico"),
    ("51",  "PE", "Peru"),     ("57",  "CO", "Colombia"), ("56", "CL", "Chile"),
    ("49",  "DE", "Germany"),  ("48",  "PL", "Poland"),  ("47", "NO", "Norway"),
    ("46",  "SE", "Sweden"),   ("45",  "DK", "Denmark"), ("43", "AT", "Austria"),
    ("41",  "CH", "Switzerland"), ("40", "RO", "Romania"), ("39", "IT", "Italy"),
    ("38",  "UA", "Ukraine"),  ("36",  "HU", "Hungary"), ("34", "ES", "Spain"),
    ("33",  "FR", "France"),   ("32",  "BE", "Belgium"), ("31", "NL", "Netherlands"),
    ("30",  "GR", "Greece"),   ("27",  "ZA", "South Africa"), ("20", "EG", "Egypt"),
    ("64",  "NZ", "New Zealand"), ("61", "AU", "Australia"),
    ("44",  "GB", "UK"),       ("7",   "RU", "Russia"),  ("1",  "US", "USA"),
]

def phone_to_country(phone: str) -> tuple:
    """Return (flag_emoji, country_name) by matching dial code.
    First checks country_prices table, then falls back to built-in map."""
    try:
        p = phone.strip().lstrip("+")
        # 1. Try the database table first (admin-configured countries take priority)
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT country_code, country_name, dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
                ).fetchall()
            for r in rows:
                if r["dial_code"] and p.startswith(r["dial_code"]):
                    return country_flag(r["country_code"]), r["country_name"]
        except Exception:
            pass
        # 2. Fallback to built-in dial code map
        for dial, code, name in _BUILTIN_DIAL_MAP:
            if p.startswith(dial):
                return country_flag(code), name
    except Exception:
        pass
    return "🌍", "Unknown"

# ── Conversation state integers ───────────────────────────────────────────────
# Each ConversationHandler uses its own namespace via name= parameter.
# Keep all state ints distinct to avoid cross-handler interference.
DEPOSIT_AMOUNT    = 100
DEPOSIT_UTR       = 101
DEPOSIT_PROOF     = 102
DEPOSIT_SCREENSHOT= 103

ADMIN_PHONE       = 200
ADMIN_OTP         = 201
ADMIN_ADD_PRICE   = 202
ADMIN_2FA_PASSWORD= 203
ADMIN_SERVER_SELECT=204

SELL_PHONE        = 300
SELL_OTP          = 301
SELL_PRICE        = 302
SELL_APPROVE_PRICE_STATE = 303

WITHDRAW_UPI      = 400
WITHDRAW_AMOUNT   = 401

APANEL_ADD_WAITING  = 500
APANEL_EDIT_WAITING = 501

# ── Payment details (set these in Render env vars) ────────────────────────────
PAYMENT_UPI    = os.getenv("PAYMENT_UPI", "yourname@upi")
PAYMENT_QR     = os.getenv("PAYMENT_QR_FILE_ID", "")   # Telegram file_id (optional)
PAYMENT_QR_PATH = os.getenv("PAYMENT_QR_PATH", "qr.png")  # local image file path

# ── UPI Auto-Verification via Gmail IMAP ─────────────────────────────────────
# Set GMAIL_USER and GMAIL_APP_PASSWORD in your .env / Render env vars.
# GMAIL_APP_PASSWORD = 16-char App Password from Gmail → Settings → Security → App Passwords
# Leave blank to disable auto-verification (bot falls back to manual admin review).
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Regex patterns to extract UTR and Amount from payment emails
_UTR_PATTERN    = re.compile(
    r"(?:UTR|Ref\s*(?:No|ID)?|RRN|Transaction\s*(?:ID|No|Ref))[:\s#]*([A-Z0-9]{10,22})",
    re.IGNORECASE
)
_UTR_FALLBACK   = re.compile(r"\b(\d{12})\b")  # any standalone 12-digit number
_AMOUNT_PATTERN = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE
)

# ── Database ──────────────────────────────────────────────────────────────────
_pool: ConnectionPool = None

def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            max_idle=300,
            reconnect_timeout=5,
            kwargs={"row_factory": dict_row},
        )
    return _pool

def get_db():
    return get_pool().connection()

def init_db():
    get_pool()  # warm up the connection pool on startup
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT DEFAULT '',
            balance NUMERIC(12,2) DEFAULT 0, referred_by BIGINT DEFAULT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS deposits (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount NUMERIC(12,2),
            status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id BIGSERIAL PRIMARY KEY, session TEXT, phone TEXT DEFAULT '',
            price NUMERIC(12,2),
            status TEXT DEFAULT 'available', buyer_id BIGINT DEFAULT NULL,
            server INTEGER DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone TEXT DEFAULT ''")
        conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS server INTEGER DEFAULT 1")
        conn.execute("""CREATE TABLE IF NOT EXISTS referral_earnings (
            id BIGSERIAL PRIMARY KEY, referrer_id BIGINT, referred_id BIGINT,
            deposit_id BIGINT, commission NUMERIC(12,2),
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS country_prices (
            country_code TEXT PRIMARY KEY,
            country_name TEXT NOT NULL,
            dial_code    TEXT NOT NULL DEFAULT '',
            price        NUMERIC(12,2) NOT NULL,
            updated_at   TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE country_prices ADD COLUMN IF NOT EXISTS dial_code TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            amount NUMERIC(12,2),
            upi_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS upi_id TEXT DEFAULT ''")

        # ── Sequence continuity across database resets ────────────────────────
        # Set STARTING_ACCOUNT_ID / STARTING_DEPOSIT_ID / STARTING_WITHDRAWAL_ID
        # in your Render env vars so IDs continue from where the last DB left off.
        # Example: if your last account ID was 47, set STARTING_ACCOUNT_ID=48
        for env_var, sequence in (
            ("STARTING_ACCOUNT_ID",    "accounts_id_seq"),
            ("STARTING_DEPOSIT_ID",    "deposits_id_seq"),
            ("STARTING_WITHDRAWAL_ID", "withdrawals_id_seq"),
        ):
            raw = os.getenv(env_var, "").strip()
            if raw:
                try:
                    start_val = int(raw)
                    if start_val > 1:
                        # setval(seq, val, is_called=false) → next INSERT gets exactly val
                        conn.execute(
                            "SELECT setval(%s, %s, false)",
                            (sequence, start_val)
                        )
                        logger.info(f"✅ Sequence {sequence} starting from {start_val}")
                except (ValueError, Exception) as e:
                    logger.warning(f"Could not set sequence {sequence} from {env_var}: {e}")

    logger.info("✅ Database initialised.")

# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str = "", referred_by: int = None):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,)).fetchone():
            conn.execute(
                "INSERT INTO users (user_id, username, referred_by) VALUES (%s,%s,%s)",
                (user_id, username or "", referred_by)
            )

def get_balance(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()
        return float(row["balance"]) if row else 0.0

def get_referral_count(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE referred_by=%s", (user_id,)).fetchone()
        return row["cnt"] if row else 0

def get_referral_earnings(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(commission),0) AS total FROM referral_earnings WHERE referrer_id=%s",
            (user_id,)
        ).fetchone()
        return float(row["total"]) if row else 0.0

# ── Premium emoji helpers ──────────────────────────────────────────────────────
def pe(emoji_id: str, fallback: str) -> str:
    """Wrap a premium emoji ID for use in HTML parse_mode messages."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# Premium emoji IDs
PE_BUY      = "6298691319086712919"
PE_SELL     = "6298356878573307709"
PE_RECHARGE = "6255738287462288807"
PE_WITHDRAW = "6129731974291527294"
PE_WALLET   = "6129801569941592173"
PE_REFER    = "6129700535130922338"
PE_SUPPORT  = "6296577138615125756"

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard(user_id: int = 0):
    buttons = [
        [InlineKeyboardButton("🛒  Buy Account",    callback_data="menu_buy"),
         InlineKeyboardButton("💰  Sell Account",   callback_data="menu_sell")],
        [InlineKeyboardButton("💵  Recharge",       callback_data="menu_deposit"),
         InlineKeyboardButton("💸  Withdraw",       callback_data="menu_withdraw")],
        [InlineKeyboardButton("📊  My Wallet",      callback_data="menu_balance"),
         InlineKeyboardButton("👥  Refer & Earn",   callback_data="menu_refer")],
        [InlineKeyboardButton("📦  My Orders",      callback_data="menu_orders")],
        [InlineKeyboardButton("🆘  Support",        url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("⚙️  Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]])


# ── PTB error handler (logs ALL handler exceptions to console) ────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs real errors, silently ignores Telegram non-issues."""
    import telegram.error as tg_err

    err = ctx.error

    # ── Silently ignore errors that are NOT real failures ─────────────────────
    # These happen routinely (e.g. user clicks a button on an old message, double-tap, etc.)
    ignored = (
        tg_err.BadRequest,        # "Message is not modified", "Query is too old", etc.
        tg_err.MessageNotModified,
    )
    if isinstance(err, ignored):
        msg = str(err).lower()
        # Only truly ignore the benign ones; let real bad requests bubble up
        benign = (
            "message is not modified",
            "query is too old",
            "message to edit not found",
            "message can't be edited",
            "there is no text in the message",
        )
        if any(b in msg for b in benign):
            return  # completely silent — no user message

    logger.error("Exception while handling update:", exc_info=err)

    # ── For real errors, notify the user once via answer() or reply ───────────
    try:
        if isinstance(update, Update):
            if update.callback_query:
                try:
                    await update.callback_query.answer(
                        "⚠️ Something went wrong. Please try again.", show_alert=False
                    )
                except Exception:
                    pass
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again or contact support."
                )
    except Exception:
        pass


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start from user_id={user.id} username={user.username}")
    referred_by = None
    if ctx.args and ctx.args[0].startswith("ref_"):
        try:
            ref_id = int(ctx.args[0].split("_")[1])
            if ref_id != user.id:
                referred_by = ref_id
        except (IndexError, ValueError):
            pass
    try:
        ensure_user(user.id, user.username or "", referred_by)
    except Exception as e:
        logger.error(f"ensure_user failed: {e}\n{traceback.format_exc()}")

    # ── Force Join check ──────────────────────────────────────────────────────
    not_joined = await check_force_join(ctx.bot, user.id)
    if not_joined:
        buttons = []
        for channel, url, title in not_joined:
            buttons.append([InlineKeyboardButton(f"📢 Join {title}", url=url)])
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_joined")])
        await update.message.reply_text(
            f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
            f"⚠️ <b>You must join our channel(s) to use this bot.</b>\n\n"
            f"Please join the channel(s) below, then tap <b>✅ I've Joined</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons))
        return
    # ─────────────────────────────────────────────────────────────────────────

    if referred_by:
        try:
            await ctx.bot.send_message(referred_by,
                f"<b>🎉 New Referral!</b>\n\n"
                f"<b>@{user.username or user.first_name}</b> just joined using your link!\n"
                f"You'll earn <b>{int(REFERRAL_COMMISSION*100)}%</b> on their deposits.",
                parse_mode="HTML")
        except Exception:
            pass
    await update.message.reply_text(
        f"<b>⚡ TG MARKET — Official Bot</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"<b>The #1 trusted marketplace</b> to buy &amp; sell\n"
        f"Telegram accounts securely using <b>INR</b>.\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💡 How It Works</b>\n\n"
        f"  <b>💵</b>  Recharge INR to your wallet\n"
        f"  <b>🛒</b>  Browse &amp; buy Telegram accounts\n"
        f"  <b>🔑</b>  Receive session instantly after purchase\n"
        f"  <b>👥</b>  Refer friends &amp; earn <b>2% commission</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>🔒 Secure  •  Fast  •  Trusted</b>\n\n"
        f"Select an option below 👇",
        parse_mode="HTML", reply_markup=main_menu_keyboard(user.id))
async def check_joined_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    await query.answer()

    try:
        not_joined = await check_force_join(ctx.bot, user.id)
    except Exception as e:
        logger.error(f"[check_joined_cb] check_force_join crashed: {e}\n{traceback.format_exc()}")
        await query.edit_message_text(
            "⚠️ Could not verify your membership right now. Please try again in a moment.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Try Again", callback_data="check_joined")]
            ]))
        return

    if not_joined:
        buttons = []
        for channel, url, title in not_joined:
            buttons.append([InlineKeyboardButton(f"📢 Join {title}", url=url)])
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_joined")])
        await query.edit_message_text(
            f"❌ <b>You haven't joined all required channels yet.</b>\n\n"
            f"Please join the channel(s) below and tap <b>✅ I've Joined</b> again.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons))
        return

    # All channels joined — show the welcome menu
    await query.edit_message_text(
        f"<b>⚡ TG MARKET — Official Bot</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"<b>The #1 trusted marketplace</b> to buy &amp; sell\n"
        f"Telegram accounts securely using <b>INR</b>.\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💡 How It Works</b>\n\n"
        f"  <b>💵</b>  Recharge INR to your wallet\n"
        f"  <b>🛒</b>  Browse &amp; buy Telegram accounts\n"
        f"  <b>🔑</b>  Receive session instantly after purchase\n"
        f"  <b>👥</b>  Refer friends &amp; earn <b>2% commission</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>🔒 Secure  •  Fast  •  Trusted</b>\n\n"
        f"Select an option below 👇",
        parse_mode="HTML", reply_markup=main_menu_keyboard(user.id))

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal = get_balance(query.from_user.id)
    text = (
        f"<b>⚡ TG MARKET — Main Menu</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💼 Wallet Balance</b>\n"
        f"<b>💲 {fmt(bal)}</b>\n\n"
        f"<b>🔒 Secure  •  Fast  •  Trusted</b>\n\n"
        f"What would you like to do?"
    )
    # If the message has a photo/caption, delete it and send a fresh message
    # instead of trying to edit (edit_message_text fails on photo messages)
    try:
        if query.message.photo or query.message.document:
            await query.message.delete()
            await ctx.bot.send_message(
                query.from_user.id, text,
                parse_mode="HTML", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=main_menu_keyboard(query.from_user.id))
    except Exception:
        await ctx.bot.send_message(
            query.from_user.id, text,
            parse_mode="HTML", reply_markup=main_menu_keyboard(query.from_user.id))
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ <b>Action cancelled.</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── UPI Auto-Verification helpers ─────────────────────────────────────────────
def _extract_email_body(msg) -> str:
    """Extract plain-text or HTML body from an email.Message object."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="ignore")
    return body


def _parse_payment_email(subject: str, body: str):
    """Extract (utr, amount_inr) from an email subject + body. Returns (None,None) on failure."""
    text = f"{subject}\n{body}"
    # UTR
    m = _UTR_PATTERN.search(text)
    utr = m.group(1) if m else None
    if not utr:
        m = _UTR_FALLBACK.search(text)
        utr = m.group(1) if m else None
    # Amount
    m = _AMOUNT_PATTERN.search(text)
    amount = float(m.group(1).replace(",", "")) if m else None
    return utr, amount


def poll_gmail_for_utr(utr: str, expected_amount_inr: float,
                       timeout_seconds: int = 120, interval_seconds: int = 8) -> bool:
    """
    Connect to Gmail via IMAP and repeatedly poll for a matching UPI credit email.
    Retries every `interval_seconds` for up to `timeout_seconds` (default 120s / 2 min).
    Returns True immediately when a match is found.
    Runs synchronously — call via loop.run_in_executor from async code.
    """
    import time as _t
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False

    deadline = _t.monotonic() + timeout_seconds
    attempt  = 0

    while _t.monotonic() < deadline:
        attempt += 1
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            mail.select("INBOX")

            # Broad search — cast a wide net so we don't miss any UPI emails
            query = '(OR OR OR SUBJECT "credited" SUBJECT "received" SUBJECT "UPI" SUBJECT "payment")'
            status, data = mail.search(None, query)
            if status == "OK" and data[0]:
                email_ids = data[0].split()
                # Check latest 20 emails (more coverage on retry attempts)
                for eid in reversed(email_ids[-20:]):
                    try:
                        status2, msg_data = mail.fetch(eid, "(RFC822)")
                        if status2 != "OK":
                            continue
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)

                        # Decode subject safely
                        raw_subject = msg.get("Subject", "")
                        parts = decode_header(raw_subject)
                        subject = ""
                        for part, enc in parts:
                            if isinstance(part, bytes):
                                subject += part.decode(enc or "utf-8", errors="ignore")
                            else:
                                subject += str(part)

                        body = _extract_email_body(msg)
                        found_utr, found_amount = _parse_payment_email(subject, body)

                        if found_utr and found_amount:
                            utr_match    = found_utr.strip().upper() == utr.strip().upper()
                            amount_match = abs(found_amount - expected_amount_inr) < 1.0
                            if utr_match and amount_match:
                                mail.logout()
                                logger.info(f"[AutoVerify] Match found on attempt {attempt}: UTR={utr}")
                                return True
                    except Exception:
                        continue

            mail.logout()

        except Exception as e:
            logger.warning(f"[AutoVerify] Attempt {attempt} IMAP error: {e}")

        # Wait before retrying (don't wait if we've just hit the deadline)
        remaining = deadline - _t.monotonic()
        if remaining > 0:
            _t.sleep(min(interval_seconds, remaining))

    logger.info(f"[AutoVerify] No match found after {attempt} attempts for UTR={utr}")
    return False


# ── DEPOSIT ───────────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 — Ask how much they want to deposit."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"<b>💵 RECHARGE WALLET</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"Enter the amount in <b>₹ INR</b> you want to deposit.\n\n"
        f"📌 <b>Example:</b> <code>500</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"✏️ <b>Type the amount below</b> or /cancel to go back:",
        parse_mode="HTML")
    return DEPOSIT_AMOUNT


async def deposit_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Show QR + UPI with I Paid / QR Not Working / Cancel buttons."""
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    try:
        amount_inr = float(update.message.text.strip())
        if amount_inr <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>❌ Invalid amount.</b> Enter a positive number in ₹ like <code>500</code>.",
            parse_mode="HTML")
        return DEPOSIT_AMOUNT

    amount_usd = round(amount_inr / USD_TO_INR, 2)
    ctx.user_data["dep_amount"]     = amount_usd
    ctx.user_data["dep_amount_inr"] = amount_inr

    caption = (
        f"⚡ <b>Pay ₹{amount_inr:.0f} — UPI Auto</b>\n\n"
        f"� Scan QR code below (works on all UPI apps)\n"
        f"🪙 <b>UPI ID:</b> <code>{PAYMENT_UPI}</code>\n"
        f"──────────────────────\n"
        f"• <b>Amount:</b> ₹{amount_inr:.0f}\n"
        f"• <b>Expires:</b> 30 minutes\n\n"
        f"📝 <b>Steps:</b>\n"
        f"1. Scan QR or send ₹{amount_inr:.0f} to UPI ID above\n"
        f"2. Tap <b>💰 I Paid ✅</b> after payment\n"
        f"3. Submit UTR (12 digit) or TXN ID + screenshot"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰  I Paid ✅",       callback_data="dep_ipaid")],
        [InlineKeyboardButton("📷  QR Not Working?", callback_data="dep_qrnw")],
        [InlineKeyboardButton("❌  Cancel",          callback_data="dep_cancel")],
    ])
    if PAYMENT_QR:
        await update.message.reply_photo(photo=PAYMENT_QR, caption=caption,
                                         parse_mode="HTML", reply_markup=kb)
    elif os.path.isfile(PAYMENT_QR_PATH):
        with open(PAYMENT_QR_PATH, "rb") as qr_file:
            await update.message.reply_photo(photo=qr_file, caption=caption,
                                             parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb)
    return DEPOSIT_AMOUNT  # wait for inline button tap


async def deposit_ipaid_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped '💰 I Paid ✅' — ask for UTR / TXN ID."""
    query = update.callback_query
    await query.answer()
    prompt = (
        f"<b>✏️ ENTER PAYMENT REF</b>\n\n"
        f"┌── ✏️ STEP 1/2 ──┐\n"
        f"│\n"
        f"│  Send <b>12-digit UTR</b> (bank/UPI app) <b>OR</b>\n"
        f"│  <b>TXN ID</b> (e.g. <code>FMPIB...</code>) from payment screen.\n"
        f"│\n"
        f"└──────────────────────┘\n\n"
        f"💡 <i>Send /cancel to abort</i>"
    )
    try:
        await query.edit_message_caption(caption=prompt, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_text(prompt, parse_mode="HTML")
        except Exception:
            await ctx.bot.send_message(query.from_user.id, prompt, parse_mode="HTML")
    return DEPOSIT_UTR


async def deposit_qrnw_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped 'QR Not Working?' — show UPI text payment option."""
    query = update.callback_query
    await query.answer()
    amount_inr = ctx.user_data.get("dep_amount_inr", 0)
    msg = (
        f"<b>📲 Pay Manually via UPI</b>\n\n"
        f"Open any UPI app and send <b>₹{amount_inr:.0f}</b> to:\n\n"
        f"<code>{PAYMENT_UPI}</code>\n\n"
        f"After paying, tap <b>💰 I Paid ✅</b> below."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰  I Paid ✅", callback_data="dep_ipaid")],
        [InlineKeyboardButton("❌  Cancel",    callback_data="dep_cancel")],
    ])
    try:
        await query.edit_message_caption(caption=msg, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)
    return DEPOSIT_AMOUNT


async def deposit_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped ❌ Cancel on the payment message."""
    query = update.callback_query
    await query.answer("Cancelled.")
    try:
        await query.edit_message_caption(caption="❌ <b>Recharge cancelled.</b>", parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_text("❌ <b>Recharge cancelled.</b>", parse_mode="HTML")
        except Exception:
            pass
    await ctx.bot.send_message(query.from_user.id, "❌ <b>Recharge cancelled.</b>",
                               parse_mode="HTML",
                               reply_markup=main_menu_keyboard(query.from_user.id))
    return ConversationHandler.END


async def deposit_utr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Got UTR/TXN ID, now ask for payment screenshot."""
    user = update.effective_user
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "<b>❌ Please send your UTR or TXN ID as text.</b>\n"
            "📌 Example: <code>612345678901</code>",
            parse_mode="HTML")
        return DEPOSIT_UTR

    raw = update.message.text.strip().replace(" ", "").upper()
    if not re.match(r"^[A-Z0-9]{6,30}$", raw):
        await update.message.reply_text(
            "<b>❌ Invalid ID format.</b>\n\n"
            "• UTR: 12-digit number — e.g. <code>612345678901</code>\n"
            "• TXN ID: alphanumeric — e.g. <code>FMPIB7978114324</code>\n\n"
            "Please send the correct ID:",
            parse_mode="HTML")
        return DEPOSIT_UTR

    ctx.user_data["dep_utr"] = raw
    await update.message.reply_text(
        f"<b>✅ UTR / TXN ID received:</b> <code>{raw}</code>\n\n"
        f"┌── 📸 STEP 2/2 ──┐\n"
        f"│\n"
        f"│  Now send the <b>screenshot</b> of your\n"
        f"│  payment confirmation from your UPI app.\n"
        f"│\n"
        f"└──────────────────────┘\n\n"
        f"💡 <i>Send /cancel to abort</i>",
        parse_mode="HTML")
    return DEPOSIT_PROOF


async def deposit_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 4 — Got screenshot. Immediately confirm receipt, then verify via Gmail IMAP."""
    user       = update.effective_user
    amount_usd = ctx.user_data.get("dep_amount", 0)
    amount_inr = ctx.user_data.get("dep_amount_inr", round(float(amount_usd) * USD_TO_INR))
    utr        = ctx.user_data.get("dep_utr", "")

    if not update.message.photo:
        await update.message.reply_text(
            "<b>❌ Please send a screenshot photo</b> of your payment confirmation.",
            parse_mode="HTML")
        return DEPOSIT_PROOF

    photo_id = update.message.photo[-1].file_id

    # Create deposit record immediately
    with get_db() as conn:
        dep_id = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s,%s) RETURNING id",
            (user.id, amount_usd)
        ).fetchone()["id"]

    # Generate a human-readable request ID (like FP_timestamp_userid)
    import time as _time
    req_id = f"FP_{int(_time.time())}_{user.id}"
    ctx.user_data["dep_req_id"] = req_id

    # ── Step 1: Send the screenshot back with "PROOF SUBMITTED" confirmation ──
    await ctx.bot.send_photo(
        user.id,
        photo=photo_id,
        caption=(
            f"<b>✅ PAYMENT SUBMITTED</b>\n\n"
            f"┌── ✅ PROOF SUBMITTED ──┐\n"
            f"│\n"
            f"│  💰 <b>Amount:</b> ₹{amount_inr:.0f}\n"
            f"│  🪪 <b>UTR:</b> <code>{utr}</code>\n"
            f"│  📸 <b>Screenshot:</b> Received\n"
            f"│  📝 <b>Request:</b> <code>{req_id}</code>\n"
            f"│\n"
            f"└──────────────────────┘"
        ),
        parse_mode="HTML"
    )

    # ── Step 2: Send the "Verifying" status message ───────────────────────────
    verifying_msg = await update.message.reply_text(
        f"⏳ <b>Verifying payment</b>\n"
        f"Checking UPI Auto Gmail for your payment ref and amount...\n"
        f"<i>Usually under 2 minutes. If not found, admin will review manually.</i>",
        parse_mode="HTML"
    )

    # ── Step 3: Run Gmail IMAP check — retries every 8s for up to 120s ───────
    loop    = asyncio.get_event_loop()
    matched = await loop.run_in_executor(
        None, poll_gmail_for_utr, utr, float(amount_inr), 120, 8
    )

    # Delete the "verifying" spinner message
    try:
        await verifying_msg.delete()
    except Exception:
        pass

    # ── Step 4: Auto-approve or reject based on result ───────────────────────
    if matched:
        with get_db() as conn:
            conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
            conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s",
                         (amount_usd, user.id))
            referrer = conn.execute(
                "SELECT referred_by FROM users WHERE user_id=%s", (user.id,)).fetchone()
            commission = 0.0
            if referrer and referrer["referred_by"]:
                commission = float(amount_usd) * REFERRAL_COMMISSION
                conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s",
                             (commission, referrer["referred_by"]))
                conn.execute(
                    "INSERT INTO referral_earnings (referrer_id,referred_id,deposit_id,commission) "
                    "VALUES (%s,%s,%s,%s)",
                    (referrer["referred_by"], user.id, dep_id, commission))

        await update.message.reply_text(
            f"<b>✅ Payment Verified &amp; Approved!</b>\n\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"<b>💵 Credited:</b> ₹{amount_inr:.0f} (${amount_usd:.2f})\n"
            f"<b>🔑 UTR/TXN:</b> <code>{utr}</code>\n"
            f"<b>🆔 Ref ID:</b> <code>{dep_id}</code>\n\n"
            f"<b>🎉 Your wallet has been topped up instantly!</b>",
            parse_mode="HTML", reply_markup=main_menu_keyboard(user.id))

        # Notify admin with screenshot
        await ctx.bot.send_photo(ADMIN_ID, photo=photo_id, caption=(
            f"<b>✅ AUTO-APPROVED DEPOSIT #{dep_id}</b>\n"
            f"<b>👤</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
            f"<b>💵</b> ₹{amount_inr:.0f} (${amount_usd:.2f})\n"
            f"<b>🔑 UTR/TXN:</b> <code>{utr}</code>\n"
            f"<b>📝 Req:</b> <code>{req_id}</code>\n"
            f"<b>🤖 Verified via Gmail IMAP — auto-credited</b>"
            + (f"\n<b>🤝 Referral:</b> {fmt(commission)} paid" if commission else "")
        ), parse_mode="HTML")

        await send_to_channel(ctx.bot, TRADES_CHANNEL,
            f"╔══════════════════════╗\n║  ✅  AUTO DEPOSIT       ║\n╚══════════════════════╝\n\n"
            f"🆔 <code>#{dep_id}</code>  👤 <code>{user.id}</code>\n"
            f"💵 <b>₹{amount_inr:.0f} (${amount_usd:.2f})</b>  🔑 <code>{utr}</code>\n"
            f"📊 ✅ <i>Auto-Verified &amp; Credited</i>")

        if referrer and referrer["referred_by"] and commission > 0:
            try:
                await ctx.bot.send_message(referrer["referred_by"],
                    f"<b>💰 Referral Commission!</b>\n\nYou earned <b>{fmt(commission)}</b>!",
                    parse_mode="HTML")
            except Exception:
                pass

    else:
        # Gmail check failed → mark rejected, send screenshot to admin for override
        with get_db() as conn:
            conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))

        await update.message.reply_text(
            f"<b>❌ Payment Not Verified</b>\n\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"<b>💵 Amount:</b> ₹{amount_inr:.0f}  •  <b>UTR/TXN:</b> <code>{utr}</code>\n\n"
            f"⚠️ No matching payment found.\n\n"
            f"Please check:\n"
            f"• UTR/TXN ID is correct\n"
            f"• Payment was sent to <code>{PAYMENT_UPI}</code>\n"
            f"• Amount matches exactly\n\n"
            f"Contact support if you believe this is an error.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄  Try Again", callback_data="menu_deposit")],
                [InlineKeyboardButton("🆘  Support",   url=f"https://t.me/{SUPPORT_USERNAME}")],
                [InlineKeyboardButton("🔙  Main Menu", callback_data="menu_back")],
            ]))

        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Override Approve", callback_data=f"dep_approve_{dep_id}"),
             InlineKeyboardButton("🗑 Dismiss",          callback_data=f"dep_reject_{dep_id}")]
        ])
        # Send screenshot + details to admin
        await ctx.bot.send_photo(ADMIN_ID, photo=photo_id, caption=(
            f"<b>❌ REJECTED (not verified) #{dep_id}</b>\n"
            f"<b>👤</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
            f"<b>💵</b> ₹{amount_inr:.0f} (${amount_usd:.2f})\n"
            f"<b>🔑 UTR/TXN:</b> <code>{utr}</code>\n"
            f"<b>📝 Req:</b> <code>{req_id}</code>\n\n"
            f"⚠️ <i>Gmail IMAP found no match.\nOverride approve only if verified manually.</i>"
        ), parse_mode="HTML", reply_markup=admin_kb)

        await send_to_channel(ctx.bot, TRADES_CHANNEL,
            f"╔══════════════════════╗\n║  ❌  DEPOSIT REJECTED   ║\n╚══════════════════════╝\n\n"
            f"🆔 <code>#{dep_id}</code>  👤 <code>{user.id}</code>\n"
            f"💵 <b>₹{amount_inr:.0f}</b>  🔑 <code>{utr}</code>\n"
            f"📊 ❌ <i>Not Verified</i>")

    return ConversationHandler.END


async def deposit_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback — only used when Gmail is NOT configured (screenshot-only manual mode)."""
    user       = update.effective_user
    amount     = ctx.user_data.get("dep_amount", 0)
    amount_inr = ctx.user_data.get("dep_amount_inr", round(float(amount) * USD_TO_INR))

    if not update.message.photo:
        await update.message.reply_text(
            "<b>❌ Please send a screenshot photo</b> of your payment.",
            parse_mode="HTML")
        return DEPOSIT_SCREENSHOT

    photo_id = update.message.photo[-1].file_id
    with get_db() as conn:
        dep_id = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s,%s) RETURNING id",
            (user.id, amount)).fetchone()["id"]

    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"dep_approve_{dep_id}"),
         InlineKeyboardButton("❌ Reject",  callback_data=f"dep_reject_{dep_id}")]
    ])
    await ctx.bot.send_photo(ADMIN_ID, photo=photo_id, caption=(
        f"<b>📥 NEW DEPOSIT REQUEST</b>\n"
        f"<b>👤</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
        f"<b>💵</b> ₹{amount_inr:.0f} (${amount:.2f})\n"
        f"<b>🆔</b> Deposit ID: <code>{dep_id}</code>\n"
        f"📸 Screenshot attached."
    ), parse_mode="HTML", reply_markup=admin_kb)

    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"╔══════════════════════╗\n║  📥  DEPOSIT REQUEST    ║\n╚══════════════════════╝\n\n"
        f"🆔 <code>#{dep_id}</code>  👤 <code>{user.id}</code>\n"
        f"💵 <b>₹{amount_inr:.0f} (${amount:.2f})</b>\n📊 ⏳ <i>Awaiting Verification</i>")

    await update.message.reply_text(
        f"<b>✅ Screenshot Received!</b>\n\n"
        f"<b>💵</b> ₹{amount_inr:.0f}  •  <b>🆔 Ref:</b> <code>{dep_id}</code>\n\n"
        f"⏳ Admin will verify and credit your balance shortly.",
        parse_mode="HTML", reply_markup=main_menu_keyboard(user.id))
    return ConversationHandler.END
async def dep_approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ✅ Approve on deposit message."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    dep_id = int(query.data.split("_")[2])
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await query.answer("Not found.", show_alert=True); return
        if dep["status"] != "pending":
            await query.answer("Already processed.", show_alert=True); return
        conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (dep["amount"], dep["user_id"]))
        referrer = conn.execute("SELECT referred_by FROM users WHERE user_id=%s", (dep["user_id"],)).fetchone()
        commission = 0.0
        if referrer and referrer["referred_by"]:
            commission = float(dep["amount"]) * REFERRAL_COMMISSION
            conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (commission, referrer["referred_by"]))
            conn.execute("INSERT INTO referral_earnings (referrer_id,referred_id,deposit_id,commission) VALUES (%s,%s,%s,%s)",
                (referrer["referred_by"], dep["user_id"], dep_id, commission))
    approved_text = (
        f"<b>✅ Deposit #{dep_id} — APPROVED</b>\n"
        f"<b>💵 {fmt(dep['amount'])}</b> credited to <code>{dep['user_id']}</code>"
    )
    try:
        if query.message.photo or query.message.document:
            await query.edit_message_caption(caption=approved_text, parse_mode="HTML")
        else:
            await query.edit_message_text(approved_text, parse_mode="HTML")
    except Exception:
        pass
    await ctx.bot.send_message(dep["user_id"],
        f"<b>🎉 Deposit Approved!</b>\n\n"
        f"<b>💵 {fmt(dep['amount'])}</b> has been added to your wallet.\n"
        f"<b>🆔 Ref:</b> <code>{dep_id}</code>\n\n"
        f"<b>Start shopping now! 🛒</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    bot_username = (await ctx.bot.get_me()).username
    dep_buy_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{bot_username}?start=buy")]
    ])
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"✅ <b>DEPOSIT APPROVED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Deposit ID: <code>{dep_id}</code>\n"
        f"🆔 User ID: <code>{dep['user_id']}</code>\n"
        f"💵 Amount: <b>{fmt(dep['amount'])}</b>\n"
        f"📊 Status: <b>✅ Approved</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 @OtpSellerStore_Bot"
        + (f"\n🤝 Referral: <b>{fmt(commission)}</b>" if commission else ""),
        reply_markup=dep_buy_kb)
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(referrer["referred_by"],
                f"<b>💰 Referral Commission Earned!</b>\n\nYou earned <b>{fmt(commission)}</b>!", parse_mode="HTML")
        except Exception:
            pass
    await query.answer("✅ Approved!")

async def dep_reject_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ❌ Reject on deposit message."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    dep_id = int(query.data.split("_")[2])
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await query.answer("Not found or already processed.", show_alert=True); return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))

    rejected_text = f"<b>❌ Deposit #{dep_id} — REJECTED</b>"
    try:
        if query.message.photo or query.message.document:
            await query.edit_message_caption(caption=rejected_text, parse_mode="HTML")
        else:
            await query.edit_message_text(rejected_text, parse_mode="HTML")
    except Exception:
        pass
    await ctx.bot.send_message(dep["user_id"],
        f"<b>❌ Deposit Rejected</b>\n\n"
        f"Your deposit of <b>{fmt(dep['amount'])}</b> (ID: <code>{dep_id}</code>) was not approved.\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"❌ <b>DEPOSIT REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Deposit ID: <code>{dep_id}</code>\n"
        f"🆔 User ID: <code>{dep['user_id']}</code>\n"
        f"💵 Amount: <b>{fmt(dep['amount'])}</b>\n"
        f"📊 Status: <b>❌ Rejected</b>")
    await query.answer("❌ Rejected.")

async def admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Legacy text command fallback: /approve_<id>"""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve_<id>"); return
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await update.message.reply_text("❌ Not found."); return
        if dep["status"] != "pending":
            await update.message.reply_text("⚠️ Already processed."); return
        conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (dep["amount"], dep["user_id"]))
        referrer = conn.execute("SELECT referred_by FROM users WHERE user_id=%s", (dep["user_id"],)).fetchone()
        commission = 0.0
        if referrer and referrer["referred_by"]:
            commission = float(dep["amount"]) * REFERRAL_COMMISSION
            conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (commission, referrer["referred_by"]))
            conn.execute("INSERT INTO referral_earnings (referrer_id,referred_id,deposit_id,commission) VALUES (%s,%s,%s,%s)",
                (referrer["referred_by"], dep["user_id"], dep_id, commission))
    await update.message.reply_text(
        f"✅ Deposit #{dep_id} approved! <b>{fmt(dep['amount'])}</b> credited."
        + (f"\n🤝 Referral <b>{fmt(commission)}</b> paid." if commission else ""), parse_mode="HTML")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>🎉 Deposit Approved!</b>\n\n"
        f"<b>💵 {fmt(dep['amount'])}</b> added to your wallet.\n"
        f"<b>🆔 Ref:</b> <code>{dep_id}</code>\n\n"
        f"<b>Start shopping! 🛒</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(referrer["referred_by"],
                f"<b>💰 Referral Commission Earned!</b>\nYou earned <b>{fmt(commission)}</b>!", parse_mode="HTML")
        except Exception:
            pass

async def admin_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Legacy text command fallback: /reject_<id>"""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reject_<id>"); return
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await update.message.reply_text("❌ Not found or already processed."); return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))
    await update.message.reply_text(f"❌ Deposit #{dep_id} rejected.")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>❌ Deposit Rejected</b>\n\n"
        f"Your deposit of <b>{fmt(dep['amount'])}</b> (ID: <code>{dep_id}</code>) was not approved.\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML", reply_markup=main_menu_keyboard())

# ── Admin: credit / deduct ────────────────────────────────────────────────────
async def admin_credit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        user_id = int(parts[1])
        amount  = float(parts[2])
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: <code>/credit &lt;user_id&gt; &lt;amount&gt;</code>\nExample: <code>/credit 123456789 50</code>", parse_mode="HTML")
        return
    ensure_user(user_id, "")
    with get_db() as conn:
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ <b>{fmt(amount)} credited to <code>{user_id}</code></b>\nNew balance: <b>{fmt(new_bal)}</b>", parse_mode="HTML")
    try:
        await ctx.bot.send_message(user_id,
            f"<b>🎉 {fmt(amount)} added to your balance by admin!</b>\n\nNew balance: <b>{fmt(new_bal)}</b>\n\n<b>Start shopping! 🛒</b>",
            parse_mode="HTML", reply_markup=main_menu_keyboard())
    except Exception:
        await update.message.reply_text("⚠️ Credited but could not notify user.")

async def admin_deduct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        user_id = int(parts[1])
        amount  = float(parts[2])
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: <code>/deduct &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode="HTML")
        return
    with get_db() as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()
        if not bal or float(bal["balance"]) < amount:
            await update.message.reply_text("❌ User not found or insufficient balance."); return
        conn.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ <b>{fmt(amount)} deducted from <code>{user_id}</code></b>\nNew balance: <b>{fmt(new_bal)}</b>", parse_mode="HTML")


# ── Admin: login via OTP ──────────────────────────────────────────────────────
async def admin_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>📱 LOGIN ACCOUNT</b>\n"
        "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        "Send the phone number with country code.\n\n"
        "<b>📌 Example:</b> <code>+12345678900</code>\n\n"
        "/cancel to abort.",
        parse_mode="HTML")
    return ADMIN_PHONE

async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    ctx.user_data["phone"] = phone
    await update.message.reply_text("⏳ Sending OTP...")
    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "<b>❌ Configuration Error</b>\n\n<code>API_ID</code> or <code>API_HASH</code> not set in Render env vars.",
            parse_mode="HTML")
        return ConversationHandler.END
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        ctx.user_data["client"] = client
        ctx.user_data["phone_code_hash"] = result.phone_code_hash
        await update.message.reply_text(
            "<b>📩 OTP sent!</b>\n\nEnter the OTP you received <i>(digits only, e.g. <code>12345</code>)</i>:",
            parse_mode="HTML")
        return ADMIN_OTP
    except Exception as e:
        logger.error(f"OTP error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"<b>❌ Failed to send OTP</b>\n\n<code>{type(e).__name__}: {e}</code>\n\n"
            f"• <b>API_ID set:</b> <code>{'Yes' if API_ID else 'No'}</code>\n"
            f"• <b>API_HASH set:</b> <code>{'Yes' if API_HASH else 'No'}</code>",
            parse_mode="HTML")
        return ConversationHandler.END

async def get_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    otp       = update.message.text.strip().replace(" ", "")
    client    = ctx.user_data.get("client")
    phone     = ctx.user_data.get("phone")
    code_hash = ctx.user_data.get("phone_code_hash")
    if not client:
        await update.message.reply_text("❌ Session expired. Run /login_account again.")
        return ConversationHandler.END
    try:
        from telethon.sessions import StringSession
        await client.sign_in(phone, otp, phone_code_hash=code_hash)
        session_string = client.session.save()
        await client.disconnect()
        ctx.user_data["session"] = session_string
        await update.message.reply_text(
            "<b>✅ Login successful!</b>\n\n<b>💵 Now enter the price</b> for this account (e.g. <code>25</code>):",
            parse_mode="HTML")
        return ADMIN_ADD_PRICE
    except Exception as e:
        err = str(e)
        # ── Two-step verification required ────────────────────────────────────
        if "Two-steps verification" in err or "SessionPasswordNeeded" in err or "password" in err.lower():
            await update.message.reply_text(
                "<b>🔐 Two-Step Verification Required</b>\n\n"
                "This account has a <b>2FA password</b> enabled.\n\n"
                "Please enter the <b>account password</b> now:",
                parse_mode="HTML")
            return ADMIN_2FA_PASSWORD
        await update.message.reply_text(f"<b>❌ Login failed</b>\n\n<code>{e}</code>\n\nRun /login_account to try again.", parse_mode="HTML")
        return ConversationHandler.END

async def get_2fa_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin entered the 2FA password after OTP was accepted."""
    password = update.message.text.strip()
    client   = ctx.user_data.get("client")
    if not client:
        await update.message.reply_text("❌ Session expired. Run /login_account again.")
        return ConversationHandler.END
    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await client.disconnect()
        ctx.user_data["session"] = session_string
        await update.message.reply_text(
            "<b>✅ Login successful!</b>\n\n<b>💵 Now enter the price</b> for this account (e.g. <code>25</code>):",
            parse_mode="HTML")
        return ADMIN_ADD_PRICE
    except Exception as e:
        await update.message.reply_text(
            f"<b>❌ 2FA Password incorrect</b>\n\n<code>{e}</code>\n\n"
            "Please enter the correct password, or run /login_account to start over.",
            parse_mode="HTML")
        return ADMIN_2FA_PASSWORD  # let them retry the password

async def set_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive price.")
        return ADMIN_ADD_PRICE
    ctx.user_data["add_price"] = price
    await update.message.reply_text(
        f"<b>✅ Price set: {fmt(price)}</b>\n\n"
        f"<b>📡 Now select the server for this account:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔵 Server 1 — Mixed Quality",   callback_data="addacc_server_1")],
            [InlineKeyboardButton("🟢 Server 2 — Quality Accounts", callback_data="addacc_server_2")],
        ])
    )
    return ADMIN_SERVER_SELECT

async def set_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin picks the server after setting price when adding an account via /login_account."""
    query = update.callback_query
    await query.answer()
    server = int(query.data.split("addacc_server_")[1])
    price  = ctx.user_data.get("add_price", 0)

    with get_db() as conn:
        acc_id = conn.execute(
            "INSERT INTO accounts (session, phone, price, server) VALUES (%s,%s,%s,%s) RETURNING id",
            (ctx.user_data["session"], ctx.user_data.get("phone", ""), price, server)
        ).fetchone()["id"]

    server_label = "🔵 Server 1 (Mixed)" if server == 1 else "🟢 Server 2 (Quality)"
    await query.edit_message_text(
        f"<b>🎉 Account #{acc_id} Added!</b>\n\n"
        f"<b>💵 Price:</b> {fmt(price)}\n"
        f"<b>📡 Server:</b> {server_label}\n"
        f"🟢 <b>Now visible in the marketplace.</b>",
        parse_mode="HTML")

    phone_raw = ctx.user_data.get("phone", "")
    flag, country_name = phone_to_country(phone_raw)
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"➕ <b>NEW ACCOUNT ADDED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country {flag} {country_name}\n"
        f"📱 Phone: <code>{mask_phone(phone_raw)}</code>\n"
        f"💵 Price: <b>{fmt(price)}</b>\n"
        f"� Server: {server_label}\n"
        f"📊 Status: 🟢 Available\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 @OtpSellerStore_Bot"
    )
    return ConversationHandler.END

# ── Admin: view commands ──────────────────────────────────────────────────────
async def admin_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT id, price, status, buyer_id FROM accounts ORDER BY id DESC").fetchall()
    if not rows:
        await update.message.reply_text("📦 No accounts yet."); return
    icons = {"available": "🟢", "sold": "✅", "pending_review": "🔄"}
    lines = [f"<b>📦 All Accounts ({len(rows)})</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    for r in rows:
        lines.append(f"{icons.get(r['status'],'⚪')} <b>#{r['id']}</b> — <b>{fmt(r['price'])}</b> ({r['status']})"
            + (f" → <code>{r['buyer_id']}</code>" if r["buyer_id"] else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("⏳ Fetching stats, checking reachable users...")

    try:
        with get_db() as conn:
            users      = conn.execute("SELECT user_id, username, balance, created_at FROM users").fetchall()
            dep_count  = conn.execute("SELECT COUNT(*) AS c FROM deposits WHERE status='approved'").fetchone()["c"]
            sell_count = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE status='sold'").fetchone()["c"]
            wd_count   = conn.execute("SELECT COUNT(*) AS c FROM withdrawals WHERE status='approved'").fetchone()["c"]
            total_bal  = conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users").fetchone()["s"]
    except Exception as e:
        logger.error(f"admin_users DB error: {e}")
        await update.message.reply_text("❌ Could not fetch stats from database.")
        return

    total  = len(users)
    funded = sum(1 for u in users if float(u["balance"]) > 0)

    from datetime import datetime, timezone, timedelta
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    new_7d   = 0
    for u in users:
        try:
            ts = u["created_at"]
            if ts is None:
                continue
            # make timezone-aware if naive
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= week_ago:
                new_7d += 1
        except Exception:
            pass

    # Check who blocked the bot by sending a typing action to each user
    blocked   = 0
    reachable = 0
    for u in users:
        try:
            await ctx.bot.send_chat_action(u["user_id"], action="typing")
            reachable += 1
        except Exception:
            blocked += 1

    await update.message.reply_text(
        f"<b>👥 USER STATISTICS</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>📊 User Overview</b>\n"
        f"  👤 Total Users:        <b>{total}</b>\n"
        f"  ✅ Reachable (Active): <b>{reachable}</b>\n"
        f"  🚫 Blocked the Bot:    <b>{blocked}</b>\n"
        f"  💰 Users with Balance: <b>{funded}</b>\n"
        f"  🆕 Joined (Last 7d):   <b>{new_7d}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💹 Platform Activity</b>\n"
        f"  ✅ Approved Deposits:  <b>{dep_count}</b>\n"
        f"  🛒 Accounts Sold:      <b>{sell_count}</b>\n"
        f"  💸 Withdrawals Paid:   <b>{wd_count}</b>\n"
        f"  🏦 Total Wallet Funds: <b>{fmt(float(total_bal))}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML"
    )

async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        deps = conn.execute(
            "SELECT d.*, u.username FROM deposits d JOIN users u ON d.user_id=u.user_id WHERE d.status='pending'"
        ).fetchall()
    if not deps:
        await update.message.reply_text("✅ No pending deposits."); return
    lines = [f"<b>📥 Pending Deposits ({len(deps)})</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    for d in deps:
        lines.append(f"<b>🆔</b> <code>{d['id']}</code> — @{d['username'] or d['user_id']} — <b>{fmt(d['amount'])}</b>\n"
            f"   ✅ /approve_{d['id']}   ❌ /reject_{d['id']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        acc_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /del_<id>"); return
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id=%s", (acc_id,))
    await update.message.reply_text(f"🗑 Account #{acc_id} deleted.")

async def admin_add_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sets price for a pending_review account submitted by a seller.
    Usage: /add_sell <account_id> <price>
    The account_id is shown in the notification sent when user submits.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        acc_id = int(parts[1])
        price  = float(parts[2])
        if price <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: <code>/add_sell &lt;account_id&gt; &lt;price&gt;</code>\n"
            "Example: <code>/add_sell 5 25.00</code>",
            parse_mode="HTML")
        return
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
        if not acc:
            await update.message.reply_text(
                f"❌ Account #{acc_id} not found or not pending review."); return
        conn.execute(
            "UPDATE accounts SET price=%s, status='available' WHERE id=%s",
            (price, acc_id)
        )
    await update.message.reply_text(
        f"✅ Account #{acc_id} listed at <b>{fmt(price)}</b> — now visible in marketplace.",
        parse_mode="HTML"
    )

# ── OTP background watcher ────────────────────────────────────────────────────
async def _watch_for_otp(bot, user_id: int, session_str: str, phone: str, acc_id: int):
    """
    Runs in the background after a purchase.
    Connects with the sold account's session, waits for a new message from
    Telegram's service account (777000), extracts the OTP, and forwards it
    to the buyer. Does NOT trigger the OTP itself — the user does that by
    entering the phone number on their own device.
    """
    import re
    import asyncio
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetHistoryRequest

    try:
        session_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await session_client.connect()

        # Snapshot the latest message ID from 777000 right now (before OTP arrives)
        baseline_id = 0
        service_peer = None
        try:
            service_peer = await session_client.get_input_entity(777000)
            history = await session_client(GetHistoryRequest(
                peer=service_peer,
                limit=1,
                offset_date=None, offset_id=0,
                max_id=0, min_id=0, add_offset=0, hash=0
            ))
            if history.messages:
                baseline_id = history.messages[0].id
        except Exception as e:
            logger.warning(f"[OTP watcher #{acc_id}] baseline error: {e}")

        # Poll every 4 seconds for up to 5 minutes
        otp_code = None
        deadline = asyncio.get_event_loop().time() + 300

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(4)
            try:
                history = await session_client(GetHistoryRequest(
                    peer=service_peer,
                    limit=5,
                    offset_date=None, offset_id=0,
                    max_id=0, min_id=0, add_offset=0, hash=0
                ))
                for msg in history.messages:
                    if msg.id <= baseline_id:
                        continue  # skip old messages
                    text = getattr(msg, "message", "") or ""
                    match = re.search(r'\b(\d{5,6})\b', text)
                    if match:
                        otp_code = match.group(1)
                        break
            except Exception as e:
                logger.warning(f"[OTP watcher #{acc_id}] poll error: {e}")
            if otp_code:
                break

        await session_client.disconnect()

        if otp_code:
            await bot.send_message(
                user_id,
                f"<b>🔐 Your OTP Has Arrived!</b>\n\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"<b>📱 Phone:</b> <code>{phone}</code>\n"
                f"<b>🔒 Password:</b> <code>Pass1211</code>\n"
                f"<b>🔑 OTP Code:</b> <code>{otp_code}</code>\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"⚠️ <b>Enter this code in Telegram now.</b>\n"
                f"⚠️ <b>OTP expires in a few minutes.</b>\n"
                f"⚠️ <b>Do NOT share these details with anyone.</b>",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                user_id,
                f"<b>⏰ OTP Not Detected Automatically</b>\n\n"
                f"Telegram may have sent the code via SMS instead.\n\n"
                f"<b>📱 Phone:</b> <code>{phone}</code>\n\n"
                f"Please check your SMS or contact support.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆘  Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
                ])
            )

    except Exception as e:
        logger.error(f"[OTP watcher #{acc_id}] fatal: {e}\n{traceback.format_exc()}")
        await bot.send_message(
            user_id,
            f"<b>⚠️ OTP Auto-Detection Failed</b>\n\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n\n"
            f"Please request the OTP manually and contact support if needed.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆘  Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
            ])
        )


# ── BUY FLOW ──────────────────────────────────────────────────────────────────
BUY_PAGE_SIZE = 20  # countries per page (2 columns × 10 rows)

async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry — show Server 1 / Server 2 choice."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        f"<b>🛒 MARKETPLACE</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"Select a server to browse available accounts:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Server (1)", callback_data="buy_server_1")],
            [InlineKeyboardButton("Server (2)", callback_data="buy_server_2")],
            [InlineKeyboardButton("• back •",   callback_data="menu_back")],
        ])
    )


async def buy_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show country grid for the chosen server, with pagination."""
    query = update.callback_query
    await query.answer()

    # callback_data is either  buy_server_1 / buy_server_2
    #                       or buy_spage_1_2  (server 1, page 2)
    data = query.data
    if data.startswith("buy_spage_"):
        parts  = data.split("_")   # ['buy','spage','<srv>','<page>']
        server = int(parts[2])
        page   = int(parts[3])
    else:
        server = int(data.split("buy_server_")[1])
        page   = 0

    with get_db() as conn:
        accounts  = conn.execute(
            "SELECT id, price, phone FROM accounts WHERE status='available' AND server=%s ORDER BY price ASC",
            (server,)
        ).fetchall()
        countries = conn.execute(
            "SELECT country_code, country_name, dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
        ).fetchall()

    server_label = "🔵 Server 1 — Mixed Quality" if server == 1 else "🟢 Server 2 — Quality Accounts"
    back_cb      = "menu_buy"

    if not accounts:
        await query.edit_message_text(
            f"<b>🛒 {server_label}</b>\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
            f"<b>😔 No accounts available right now.</b>\nCheck back soon!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
            ]))
        return

    def get_country(phone):
        p = (phone or "").strip().lstrip("+")
        for c in countries:
            if c["dial_code"] and p.startswith(c["dial_code"]):
                return c["country_code"], c["country_name"], c["dial_code"]
        for dial, code, name in _BUILTIN_DIAL_MAP:
            if p.startswith(dial):
                return code, name, dial
        return "XX", "Other", "0"

    country_data = {}
    for acc in accounts:
        code, name, dial = get_country(acc["phone"] or "")
        if code not in country_data:
            country_data[code] = {"name": name, "dial": dial, "count": 0, "price": float(acc["price"])}
        country_data[code]["count"] += 1
        country_data[code]["price"] = min(country_data[code]["price"], float(acc["price"]))

    sorted_countries = sorted(country_data.items(), key=lambda x: x[1]["name"])
    total_countries  = len(sorted_countries)
    total_pages      = max(1, (total_countries + BUY_PAGE_SIZE - 1) // BUY_PAGE_SIZE)
    page             = max(0, min(page, total_pages - 1))
    page_slice       = sorted_countries[page * BUY_PAGE_SIZE : (page + 1) * BUY_PAGE_SIZE]

    buttons = []
    row = []
    for code, info in page_slice:
        flag  = country_flag(code)
        inr   = round(info["price"] * USD_TO_INR)
        label = f"{flag} {code}+{info['dial']} | ₹{inr}"
        btn   = InlineKeyboardButton(label, callback_data=f"buycountry_{server}_{code}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"buy_spage_{server}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"[{page + 1}] of {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"buy_spage_{server}_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])

    total_accs = sum(v["count"] for v in country_data.values())
    await query.edit_message_text(
        f"<b>🛒 {server_label}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"✅ Page {page + 1} of {total_pages}\n"
        f"✅ <b>{total_accs} account(s)</b> across <b>{total_countries} countries</b>\n\n"
        f"Select a country to browse:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons))


async def buy_country(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show individual accounts for a selected country+server."""
    query = update.callback_query
    await query.answer()

    # callback_data = buycountry_<server>_<country_code>
    parts        = query.data.split("buycountry_", 1)[1].split("_", 1)
    server       = int(parts[0])
    country_code = parts[1]

    with get_db() as conn:
        country   = conn.execute(
            "SELECT country_name, dial_code FROM country_prices WHERE country_code=%s",
            (country_code,)
        ).fetchone()
        all_accs  = conn.execute(
            "SELECT id, price, phone FROM accounts WHERE status='available' AND server=%s ORDER BY price ASC",
            (server,)
        ).fetchall()
        all_dials = conn.execute(
            "SELECT dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
        ).fetchall()

    if country:
        dial = country["dial_code"]
        name = country["country_name"]
        flag = country_flag(country_code)
        accs = [a for a in all_accs if (a["phone"] or "").strip().lstrip("+").startswith(dial)]
    else:
        builtin_match = next(((d, n) for d, c, n in _BUILTIN_DIAL_MAP if c == country_code), None)
        if builtin_match:
            dial, name = builtin_match
            flag = country_flag(country_code)
            accs = [a for a in all_accs if (a["phone"] or "").strip().lstrip("+").startswith(dial)]
        else:
            dial, name, flag = "0", "Other", "🌍"
            known_dials = [r["dial_code"] for r in all_dials if r["dial_code"]]
            known_dials += [d for d, c, n in _BUILTIN_DIAL_MAP]
            def matches_any(phone):
                p = (phone or "").strip().lstrip("+")
                return any(p.startswith(d) for d in known_dials)
            accs = [a for a in all_accs if not matches_any(a["phone"])]

    server_label = "🔵 Server 1" if server == 1 else "🟢 Server 2"
    back_cb      = f"buy_server_{server}"

    if not accs:
        await query.edit_message_text(
            f"😔 No {flag} {name} accounts available on {server_label} right now.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
            ]))
        return

    buttons = []
    for a in accs:
        buttons.append([InlineKeyboardButton(
            f"🔑 Account #{a['id']}  —  {fmt(a['price'])}",
            callback_data=f"view_{server}_{a['id']}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])

    await query.edit_message_text(
        f"<b>🛒 {server_label} — {flag} {name}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>📦 {len(accs)} available</b>\n\n"
        f"Tap any account to view details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons))

async def view_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data = view_<server>_<acc_id>
    parts   = query.data.split("_")   # ['view','<srv>','<id>']
    server  = int(parts[1])
    acc_id  = int(parts[2])
    user_id = query.from_user.id
    ensure_user(user_id, query.from_user.username or "")
    with get_db() as conn:
        acc = conn.execute(
            "SELECT id, price FROM accounts WHERE id=%s AND status='available' AND server=%s",
            (acc_id, server)
        ).fetchone()
    server_label = "🔵 Server 1" if server == 1 else "🟢 Server 2"
    if not acc:
        await query.edit_message_text("❌ No longer available.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Browse Others", callback_data="menu_buy")]])); return
    balance   = get_balance(user_id)
    has_funds = balance >= float(acc["price"])
    await query.edit_message_text(
        f"<b>🔑 ACCOUNT DETAILS</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>🆔 Account ID:  #{acc['id']}</b>\n"
        f"<b>� Server:      {server_label}</b>\n"
        f"<b>�💵 Price:       {fmt(acc['price'])}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💼 Your Balance: {fmt(balance)}</b>\n"
        f"{'<b>✅ You have enough funds.</b>' if has_funds else '<b>❌ Insufficient balance — deposit first.</b>'}\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅  Buy Now", callback_data=f"confirm_{server}_{acc_id}")],
            [InlineKeyboardButton("🔙  Back",    callback_data=f"buy_server_{server}")]]))

async def confirm_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data = confirm_<server>_<acc_id>
    parts   = query.data.split("_")   # ['confirm','<srv>','<id>']
    server  = int(parts[1])
    acc_id  = int(parts[2])
    user_id = query.from_user.id
    ensure_user(user_id, query.from_user.username or "")

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='available' AND server=%s",
            (acc_id, server)
        ).fetchone()
        if not acc:
            await query.edit_message_text("❌ Account no longer available."); return

        balance = get_balance(user_id)
        if balance < float(acc["price"]):
            await query.edit_message_text(
                f"<b>❌ Insufficient Balance</b>\n\n"
                f"<b>💼 Your balance: {fmt(balance)}</b>\n"
                f"<b>💵 Required:     {fmt(acc['price'])}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰  Deposit Now", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙  Back",        callback_data="menu_back")]
                ])
            )
            return

        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s", (acc["price"], user_id)
        )
        conn.execute(
            "UPDATE accounts SET status='sold', buyer_id=%s WHERE id=%s", (user_id, acc_id)
        )

    server_label = "🔵 Server 1" if server == 1 else "🟢 Server 2"
    await query.edit_message_text(
        f"<b>🎉 Purchase Successful!</b>\n\n"
        f"<b>🔑 Account #{acc_id} is yours!</b>\n"
        f"<b>📡 Server: {server_label}</b>\n"
        f"<b>💵 Paid: {fmt(acc['price'])}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"⏳ <b>Sending login details...</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

    phone = acc.get("phone", "").strip()

    # ── Send phone + password with on-demand OTP button ──────────────────────
    await ctx.bot.send_message(
        user_id,
        f"<b>📱 Your Account Details</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📞 Phone Number:</b> <code>{phone}</code>\n"
        f"<b>🔒 2FA Password:</b> <code>Pass1211</code>\n"
        f"<b>📡 Server:</b> {server_label}\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>How to login:</b>\n"
        f"<b>1️⃣</b>  Open Telegram on any device\n"
        f"<b>2️⃣</b>  Enter the phone number above\n"
        f"<b>3️⃣</b>  Telegram sends an OTP to <b>this</b> account\n"
        f"<b>4️⃣</b>  Press <b>🔐 Get Telegram Code</b> below to fetch it\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Get Telegram Code", callback_data=f"getotp_{acc_id}")],
        ])
    )

    # Notify admin
    await ctx.bot.send_message(
        ADMIN_ID,
        f"<b>💸 Account Sold</b>\n\n"
        f"<b>🔑 Account #{acc_id}</b> sold to <code>{user_id}</code> for <b>{fmt(acc['price'])}</b>.\n"
        f"<b>📱 Phone:</b> <code>{phone}</code>\n"
        f"<b>📡 Server:</b> {server_label}",
        parse_mode="HTML"
    )
    flag, country_name = phone_to_country(phone)
    bot_username = (await ctx.bot.get_me()).username
    buy_now_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url=f"https://t.me/{bot_username}?start=buy")]
    ])
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"🛒 <b>ACCOUNT SOLD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country {flag} {country_name}\n"
        f"📱 Phone: <code>{mask_phone(phone)}</code>\n"
        f"💵 Price: <b>{fmt(acc['price'])}</b>\n"
        f"📡 Server: {server_label}\n"
        f"👤 Buyer: <code>{user_id}</code>\n"
        f"📊 Status: ✅ Sold\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 @OtpSellerStore_Bot",
        reply_markup=buy_now_kb
    )


# ── On-demand OTP fetcher ─────────────────────────────────────────────────────
async def getotp_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    User taps 🔐 Get Telegram Code or 🔄 Refresh Code.
    Connects to the sold account's session, reads the LATEST message from
    Telegram service (777000), extracts and returns the OTP immediately.
    No background polling — fast on-demand fetch every time.
    """
    import re
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetHistoryRequest

    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer("⏳ Fetching code...")

    acc_id = int(query.data.split("getotp_")[1])

    # Verify this account was bought by this user
    with get_db() as conn:
        acc = conn.execute(
            "SELECT session, phone FROM accounts WHERE id=%s AND buyer_id=%s",
            (acc_id, user_id)
        ).fetchone()

    if not acc:
        await query.answer("❌ Account not found or not yours.", show_alert=True)
        return

    phone       = acc["phone"] or ""
    session_str = acc["session"]

    # Show loading state on the button while fetching
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏳ Fetching code...", callback_data="noop")],
            ])
        )
    except Exception:
        pass

    otp_code = None
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        try:
            service_peer = await client.get_input_entity(777000)
            history = await client(GetHistoryRequest(
                peer=service_peer,
                limit=5,
                offset_date=None, offset_id=0,
                max_id=0, min_id=0, add_offset=0, hash=0
            ))
            for msg in history.messages:
                text = getattr(msg, "message", "") or ""
                match = re.search(r'\b(\d{5,6})\b', text)
                if match:
                    otp_code = match.group(1)
                    break
        finally:
            await client.disconnect()
    except Exception as e:
        logger.error(f"[getotp #{acc_id}] error: {e}")

    if otp_code:
        try:
            await query.edit_message_text(
                f"<b>📱 Your Account Details</b>\n\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"<b>📞 Phone:</b> <code>{phone}</code>\n"
                f"<b>🔒 2FA Password:</b> <code>Pass1211</code>\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"<b>🔑 Telegram Code:</b>  <code>{otp_code}</code>\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"⚠️ Enter this code in Telegram now.\n"
                f"⚠️ OTP expires in a few minutes.\n\n"
                f"🔄 Need a newer code? Tap <b>Refresh</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Code", callback_data=f"getotp_{acc_id}")],
                ])
            )
        except Exception:
            pass
    else:
        # No code found yet — restore button so user can retry
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 Get Telegram Code", callback_data=f"getotp_{acc_id}")],
                ])
            )
        except Exception:
            pass
        await ctx.bot.send_message(
            user_id,
            f"<b>❌ No Telegram code found yet.</b>\n\n"
            f"Make sure you entered <code>{phone}</code> in Telegram first,\n"
            f"then tap <b>🔐 Get Telegram Code</b> again.\n\n"
            f"If Telegram sent the code via SMS instead, check your messages directly.",
            parse_mode="HTML"
        )



def country_flag(code: str) -> str:
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper()[:2])
    except Exception:
        return "🌍"

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Public /prices command — shows the buy price list."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    if not rows:
        await update.message.reply_text(
            "No prices available yet. Check back soon!",
            reply_markup=main_menu_keyboard())
        return
    lines = ["<b>💰 We Buy From You — Price List</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"]
    for r in rows:
        flag = country_flag(r["country_code"])
        lines.append(
            f"<b>{flag} +{r['dial_code']}-{r['country_code']}:</b>  <b>{r['price']}$</b>  <i>({r['country_name']})</i>"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰  Sell Account", callback_data="menu_sell")]
        ])
    )


# ── ADMIN PRICE PANEL ─────────────────────────────────────────────────────────
APANEL_DEL_CONFIRM  = 502
SELL_APPROVE_PRICE  = 303  # alias — same value as SELL_APPROVE_PRICE_STATE

def _prices_panel_keyboard(rows):
    """Build the admin price panel keyboard."""
    buttons = []
    for r in rows:
        flag = country_flag(r["country_code"])
        buttons.append([
            InlineKeyboardButton(
                f"{flag} {r['country_code']} +{r['dial_code']} — {r['price']}$",
                callback_data=f"ap_view_{r['country_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add Country", callback_data="ap_add")])
    buttons.append([InlineKeyboardButton("🔙 Close Panel", callback_data="ap_close")])
    return InlineKeyboardMarkup(buttons)

async def admin_prices_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin command /adminprices — opens the interactive price management panel."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    text = (
        "<b>Admin Price Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\n"
        "Tap a country to edit or delete it.\n"
        "Tap ➕ Add Country to add a new one."
    )
    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )

async def ap_refresh(query, ctx):
    """Refresh the admin panel in-place."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    text = (
        "<b>Admin Price Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\n"
        "Tap a country to edit or delete it.\n"
        "Tap ➕ Add Country to add a new one."
    )
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )

async def ap_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show edit/delete options for a specific country."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_view_")[1]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM country_prices WHERE country_code=%s", (code,)
        ).fetchone()
    if not row:
        await query.answer("Not found.", show_alert=True)
        return
    flag = country_flag(code)
    await query.edit_message_text(
        f"<b>{flag} {row['country_name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Code:      <code>{row['country_code']}</code>\n"
        f"Dial code: <code>+{row['dial_code']}</code>\n"
        f"Price:     <b>{row['price']}$</b>\n\n"
        f"What would you like to do?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Price", callback_data=f"ap_edit_{code}")],
            [InlineKeyboardButton("🗑 Delete",     callback_data=f"ap_del_{code}")],
            [InlineKeyboardButton("🔙 Back",       callback_data="ap_back")],
        ])
    )

async def ap_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask admin for new price."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_edit_")[1]
    ctx.user_data["ap_edit_code"] = code
    await query.edit_message_text(
        f"✏️ Enter the new price for <code>{code}</code> (e.g. <code>1.50</code>):\n\n"
        f"Send /apcancel to go back.",
        parse_mode="HTML"
    )
    return APANEL_EDIT_WAITING

async def ap_edit_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save the new price."""
    if update.effective_user.id != ADMIN_ID:
        return
    code = ctx.user_data.get("ap_edit_code")
    try:
        price = float(update.message.text.strip().replace("$", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a number like <code>1.50</code>", parse_mode="HTML")
        return APANEL_EDIT_WAITING
    with get_db() as conn:
        conn.execute(
            "UPDATE country_prices SET price=%s, updated_at=NOW() WHERE country_code=%s",
            (price, code)
        )
    await update.message.reply_text(
        f"✅ <code>{code}</code> price updated to <b>{price}$</b>",
        parse_mode="HTML"
    )
    # Re-show the panel
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END

async def ap_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Confirm deletion."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_del_")[1]
    await query.edit_message_text(
        f"🗑 Are you sure you want to delete <code>{code}</code>?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"ap_delconfirm_{code}")],
            [InlineKeyboardButton("❌ Cancel",      callback_data=f"ap_view_{code}")],
        ])
    )

async def ap_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Execute deletion."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_delconfirm_")[1]
    with get_db() as conn:
        conn.execute("DELETE FROM country_prices WHERE country_code=%s", (code,))
    await query.answer(f"✅ {code} deleted.", show_alert=True)
    await ap_refresh(query, ctx)

async def ap_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask admin for new country details."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ <b>Add New Country</b>\n\n"
        "Send the details in this format:\n"
        "<code>CODE DIALCODE PRICE Country Name</code>\n\n"
        "Example:\n"
        "<code>IN 91 2.00 India</code>\n"
        "<code>US 1 8.00 United States</code>\n\n"
        "Send /apcancel to go back.",
        parse_mode="HTML"
    )
    return APANEL_ADD_WAITING

async def ap_add_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save the new country."""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split(None, 3)
        code     = parts[0].upper()
        dial     = parts[1].lstrip("+")
        price    = float(parts[2])
        name     = parts[3]
        if price <= 0 or not dial.isdigit():
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Wrong format. Use:\n<code>CODE DIALCODE PRICE Country Name</code>\n"
            "Example: <code>IN 91 2.00 India</code>",
            parse_mode="HTML"
        )
        return APANEL_ADD_WAITING
    with get_db() as conn:
        conn.execute(
            "INSERT INTO country_prices (country_code, country_name, dial_code, price) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (country_code) DO UPDATE "
            "SET country_name=%s, dial_code=%s, price=%s, updated_at=NOW()",
            (code, name, dial, price, name, dial, price)
        )
    await update.message.reply_text(
        f"✅ <b>{name}</b> (<code>{code}</code>) added at <b>{price}$</b>",
        parse_mode="HTML"
    )
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END

async def ap_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Go back to the main panel list."""
    query = update.callback_query
    await query.answer()
    await ap_refresh(query, ctx)

async def ap_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Close the panel."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Price panel closed.")
    return ConversationHandler.END

async def ap_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel add/edit conversation."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END


# ── SELL FLOW ─────────────────────────────────────────────────────────────────
async def sell_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — shown when user taps Sell Account button."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "<b>💰 SELL ACCOUNT</b>\n"
        "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        "📱 <b>Send your phone number</b> with country code.\n\n"
        "<b>Example:</b> <code>+919876543210</code>\n\n"
        "Check /prices to see payouts per country.\n\n"
        "Type /cancel to go back.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]
        ])
    )
    return SELL_PHONE


async def sell_get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent their phone number — send OTP via Telethon."""
    phone = update.message.text.strip()
    ctx.user_data["sell_phone"] = phone
    await update.message.reply_text("⏳ Sending OTP to your account...")

    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "❌ <b>Configuration Error</b>\n\n<code>API_ID</code> or <code>API_HASH</code> not set.",
            parse_mode="HTML")
        return ConversationHandler.END

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        ctx.user_data["sell_client"] = client
        ctx.user_data["sell_phone_code_hash"] = result.phone_code_hash
        await update.message.reply_text(
            f"<b>📩 OTP Sent!</b>\n\n"
            f"A login code was sent to <code>{phone}</code>.\n\n"
            f"Enter the OTP with spaces between each digit.\n\n"
            f"<b>Example:</b> <code>1 2 3 4 5</code>",
            parse_mode="HTML")
        return SELL_OTP
    except Exception as e:
        logger.error(f"Sell OTP error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ <b>Failed to send OTP</b>\n\n<code>{type(e).__name__}: {e}</code>\n\n"
            f"Please check the phone number and try again.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END


async def sell_get_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent OTP — sign in, save session, notify admin with approve/reject buttons."""
    # Accept both spaced "1 2 3 4 5" and plain "12345"
    otp       = update.message.text.strip().replace(" ", "")
    client    = ctx.user_data.get("sell_client")
    phone     = ctx.user_data.get("sell_phone")
    code_hash = ctx.user_data.get("sell_phone_code_hash")
    user      = update.effective_user

    if not client:
        await update.message.reply_text(
            "❌ Session expired. Please tap Sell Account again.",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        from telethon.sessions import StringSession
        await client.sign_in(phone, otp, phone_code_hash=code_hash)
        session_string = client.session.save()
        await client.disconnect()

        # Store pending sell in DB
        with get_db() as conn:
            new_acc = conn.execute(
                "INSERT INTO accounts (session, phone, price, status) VALUES (%s,%s,%s,'pending_review') RETURNING id",
                (session_string, phone, 0)
            ).fetchone()
            acc_id = new_acc["id"]

        flag, country_name = phone_to_country(phone)
        seller_name = f"@{user.username}" if user.username else user.first_name

        # Admin message with inline Approve / Reject buttons
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"sell_approve_{acc_id}"),
             InlineKeyboardButton("❌ Reject",  callback_data=f"sell_reject_{acc_id}")]
        ])
        await update.message.bot.send_message(
            ADMIN_ID,
            f"<b>📥 NEW ACCOUNT FOR SALE</b>\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"<b>🆔 Account ID:</b> <code>#{acc_id}</code>\n"
            f"<b>👤 Seller:</b> {seller_name} (<code>{user.id}</code>)\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n"
            f"<b>🌍 Country:</b> {flag} {country_name}\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"Approve to list it in the marketplace, or Reject to remove it.",
            parse_mode="HTML",
            reply_markup=admin_kb
        )

        # Channel notification
        await send_to_channel(update.message.bot, TRADES_CHANNEL,
            f"💰 <b>NEW ACCOUNT SUBMITTED FOR SALE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Account ID: <code>#{acc_id}</code>\n"
            f"🌍 Country: {flag} {country_name}\n"
            f"📱 Phone: <code>{mask_phone(phone)}</code>\n"
            f"👤 Seller: {seller_name} (<code>{user.id}</code>)\n"
            f"📊 Status: 🔄 Pending Review\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        await update.message.reply_text(
            f"<b>✅ Account Submitted!</b>\n\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n"
            f"<b>🌍 Country:</b> {flag} {country_name}\n\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"Your account is <b>pending admin review</b>.\n"
            f"You'll be notified once it's approved and listed. <b>💰</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Sell sign-in error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ <b>Login Failed</b>\n\n<code>{e}</code>\n\n"
            f"Please try again.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END


async def sell_approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ✅ Approve on a sell submission — asks for price."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return ConversationHandler.END
    acc_id = int(query.data.split("sell_approve_")[1])
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
    if not acc:
        await query.answer("Not found or already processed.", show_alert=True)
        return ConversationHandler.END
    ctx.user_data["sell_approve_acc_id"] = acc_id
    await query.answer()
    await query.edit_message_text(
        f"✅ Approving account <code>#{acc_id}</code>\n\n"
        f"📱 Phone: <code>{acc['phone']}</code>\n\n"
        f"Enter the listing price in USD (e.g. <code>5.00</code>):",
        parse_mode="HTML"
    )
    return SELL_APPROVE_PRICE


async def sell_approve_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sent the price after approving a sell submission."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    acc_id = ctx.user_data.get("sell_approve_acc_id")
    if not acc_id:
        await update.message.reply_text("❌ No pending approval. Use the Approve button.")
        return ConversationHandler.END
    try:
        price = float(update.message.text.strip().replace("$", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a number like <code>5.00</code>", parse_mode="HTML")
        return SELL_APPROVE_PRICE
    with get_db() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=%s", (acc_id,)).fetchone()
        if not acc:
            await update.message.reply_text(f"❌ Account #{acc_id} not found.")
            return ConversationHandler.END
        conn.execute(
            "UPDATE accounts SET price=%s, status='available' WHERE id=%s",
            (price, acc_id)
        )
    ctx.user_data.pop("sell_approve_acc_id", None)
    flag, country_name = phone_to_country(acc["phone"] or "")
    await update.message.reply_text(
        f"✅ Account <code>#{acc_id}</code> approved and listed at <b>{fmt(price)}</b>",
        parse_mode="HTML"
    )
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"✅ <b>ACCOUNT APPROVED &amp; LISTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country: {flag} {country_name}\n"
        f"📱 Phone: <code>{mask_phone(acc['phone'] or '')}</code>\n"
        f"💵 Price: <b>{fmt(price)}</b>\n"
        f"📊 Status: 🟢 Available\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    return ConversationHandler.END


async def sell_reject_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ❌ Reject on a sell submission."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return
    acc_id = int(query.data.split("sell_reject_")[1])
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
        if not acc:
            await query.answer("Not found or already processed.", show_alert=True)
            return
        conn.execute("DELETE FROM accounts WHERE id=%s", (acc_id,))
    await query.edit_message_text(
        f"❌ Account <code>#{acc_id}</code> rejected and removed.",
        parse_mode="HTML"
    )
    await query.answer("❌ Rejected.")
    # Channel notification
    flag, country_name = phone_to_country(acc["phone"] or "")
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"❌ <b>ACCOUNT REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country: {flag} {country_name}\n"
        f"📊 Status: ❌ Rejected\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


async def sell_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel sell conversation."""
    client = ctx.user_data.get("sell_client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    await update.message.reply_text("❌ Sell cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── WALLET / REFER / WITHDRAW ─────────────────────────────────────────────────
async def show_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user.id, user.username or "")
    await query.edit_message_text(
        f"<b>📊 MY WALLET</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💼 Available Balance</b>\n"
        f"<b>💲 {fmt(get_balance(user.id))}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>👥 Referrals:</b>       <b>{get_referral_count(user.id)}</b>\n"
        f"<b>🤝 Referral Earned:</b> <b>{fmt(get_referral_earnings(user.id))}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰  Deposit",  callback_data="menu_deposit"),
             InlineKeyboardButton("💸  Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]]))

async def refer_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ref_link = f"https://t.me/{ctx.bot.username}?start=ref_{user.id}"
    await query.edit_message_text(
        f"<b>👥 REFER &amp; EARN</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"Invite friends and earn <b>{int(REFERRAL_COMMISSION*100)}% commission</b>\non every deposit — <b>forever!</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📊 Your Stats</b>\n"
        f"<b>👥 Total Referrals:</b> <b>{get_referral_count(user.id)}</b>\n"
        f"<b>💰 Total Earned:</b>    <b>{fmt(get_referral_earnings(user.id))}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>🔗 Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"📤 <b>Share this link. When they deposit, you get 2% instantly!</b>",
        parse_mode="HTML", reply_markup=back_keyboard())

async def my_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the user's purchase history."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        with get_db() as conn:
            orders = conn.execute(
                """SELECT a.id, a.phone, a.price, a.created_at
                   FROM accounts a
                   WHERE a.buyer_id = %s
                   ORDER BY a.created_at DESC
                   LIMIT 20""",
                (user_id,)
            ).fetchall()
            # Fetch country dial codes once for all lookups below
            dial_rows = conn.execute(
                "SELECT country_code, country_name, dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
            ).fetchall()
    except Exception as e:
        logger.error(f"my_orders DB error: {e}")
        await query.edit_message_text(
            "❌ Could not load orders. Please try again.",
            reply_markup=back_keyboard())
        return

    if not orders:
        await query.edit_message_text(
            "<b>📦 MY ORDERS</b>\n"
            "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
            "😔 <b>You haven't purchased any accounts yet.</b>\n\n"
            "Tap <b>🛒 Buy Account</b> to browse the marketplace!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒  Buy Account", callback_data="menu_buy")],
                [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]
            ]))
        return

    def _country_from_phone(phone: str) -> tuple:
        """Resolve country using already-fetched dial_rows, then built-in map."""
        try:
            p = (phone or "").strip().lstrip("+")
            for r in dial_rows:
                if r["dial_code"] and p.startswith(r["dial_code"]):
                    return country_flag(r["country_code"]), r["country_name"]
            for dial, code, name in _BUILTIN_DIAL_MAP:
                if p.startswith(dial):
                    return country_flag(code), name
        except Exception:
            pass
        return "🌍", "Unknown"

    lines = [
        "<b>📦 MY ORDERS</b>",
        "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n",
    ]
    for i, o in enumerate(orders, start=1):
        flag, country_name = _country_from_phone(o["phone"] or "")
        try:
            date_str = o["created_at"].strftime("%d %b %Y") if o["created_at"] else "—"
        except Exception:
            date_str = "—"
        try:
            price_str = f"{fmt(float(o['price']))}"
        except Exception:
            price_str = "—"
        lines.append(
            f"<b>{i}.</b> 🔑 <b>Account #{o['id']}</b>\n"
            f"   {flag} {country_name}\n"
            f"   📱 <code>{mask_phone(o['phone'] or '')}</code>\n"
            f"   💵 <b>{price_str}</b>  •  🗓 {date_str}\n"
        )

    lines.append("<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>")
    lines.append(f"<b>Total purchases: {len(orders)}</b>")

    # Trim to Telegram's 4096 char limit just in case
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>... (showing latest entries)</i>"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒  Buy More",     callback_data="menu_buy")],
            [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]
        ]))

async def withdraw_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 — Ask for UPI ID or QR code."""
    query = update.callback_query
    await query.answer()
    balance = get_balance(query.from_user.id)

    # ── Minimum withdrawal check ($2) ─────────────────────────────────────
    MIN_WITHDRAWAL_USD = 2.0
    if balance < MIN_WITHDRAWAL_USD:
        min_inr = round(MIN_WITHDRAWAL_USD * USD_TO_INR)
        await query.edit_message_text(
            f"<b>💸 WITHDRAW</b>\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
            f"<b>❌ Minimum Withdrawal Not Met</b>\n\n"
            f"<b>💰 Your Balance:</b>     <b>{fmt(balance)}</b>\n"
            f"<b>📌 Minimum Required:</b> <b>₹{min_inr} ($2.00)</b>\n\n"
            f"Please recharge your wallet first.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵  Recharge Wallet", callback_data="menu_deposit")],
                [InlineKeyboardButton("🔙  Back to Menu",    callback_data="menu_back")],
            ])
        )
        return ConversationHandler.END
    # ──────────────────────────────────────────────────────────────────────
    await query.edit_message_text(
        f"<b>💸 WITHDRAW</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💰 Your Balance: {fmt(balance)}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📲 Step 1 of 2</b>\n\n"
        f"Send your <b>UPI ID</b> or a <b>QR code photo</b> to receive payment.\n\n"
        f"📌 <b>UPI example:</b> <code>yourname@upi</code>\n"
        f"📌 Or send a QR code image\n\n"        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"Type /cancel to go back.",
        parse_mode="HTML")
    return WITHDRAW_UPI

async def withdraw_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Got UPI/QR, now ask for amount."""
    user = update.effective_user
    ensure_user(user.id, user.username or "")

    # Accept either text (UPI ID) or photo (QR code)
    if update.message.photo:
        # Store the file_id of the largest photo
        ctx.user_data["wd_upi"] = update.message.photo[-1].file_id
        ctx.user_data["wd_upi_type"] = "qr"
        upi_display = "QR Code received ✅"
    elif update.message.text:
        upi_text = update.message.text.strip()
        ctx.user_data["wd_upi"] = upi_text
        ctx.user_data["wd_upi_type"] = "upi"
        upi_display = f"`{upi_text}`"
    else:
        await update.message.reply_text(
            "<b>❌ Please send your UPI ID as text or a QR code as a photo.</b>",
            parse_mode="HTML")
        return WITHDRAW_UPI

    balance = get_balance(user.id)
    balance_inr = round(balance * USD_TO_INR)
    await update.message.reply_text(
        f"<b>✅ Payment details received:</b> {upi_display}\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💰 Step 2 of 2</b>\n\n"
        f"<b>Your current balance: {fmt(balance)}</b>\n\n"
        f"How much do you want to withdraw? <b>(Enter amount in ₹)</b>\n"
        f"📌 <b>Example:</b> <code>500</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"Type /cancel to go back.",
        parse_mode="HTML")
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Got amount (in INR), validate balance and submit request."""
    user = update.effective_user
    try:
        amount_inr = float(update.message.text.strip())
        if amount_inr <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>❌ Invalid amount.</b> Enter a positive number in ₹ like <code>500</code>.",
            parse_mode="HTML")
        return WITHDRAW_AMOUNT

    # Convert INR → USD for internal balance comparison and storage
    amount_usd = round(amount_inr / USD_TO_INR, 2)
    balance    = get_balance(user.id)

    # ── Minimum withdrawal check ($2) ─────────────────────────────────────
    MIN_WITHDRAWAL_USD = 2.0
    if amount_usd < MIN_WITHDRAWAL_USD:
        min_inr = round(MIN_WITHDRAWAL_USD * USD_TO_INR)
        await update.message.reply_text(
            f"<b>❌ Minimum Withdrawal is ₹{min_inr} ($2.00)</b>\n\n"
            f"You entered <b>₹{amount_inr:.0f} (${amount_usd:.2f})</b> which is below the minimum.\n\n"
            f"Please enter at least <b>₹{min_inr}</b>:",
            parse_mode="HTML")
        return WITHDRAW_AMOUNT
    # ──────────────────────────────────────────────────────────────────────

    if balance < amount_usd:
        await update.message.reply_text(
            f"<b>❌ Insufficient Balance</b>\n\n"
            f"<b>💰 Your balance: {fmt(balance)}</b>\n"
            f"<b>💸 Requested:    ₹{amount_inr:.0f} (${amount_usd:.2f})</b>\n\n"
            f"You can only withdraw up to <b>{fmt(balance)}</b>.\n"
            f"Please enter a lower amount:",
            parse_mode="HTML")
        return WITHDRAW_AMOUNT

    upi_val  = ctx.user_data.get("wd_upi", "")
    upi_type = ctx.user_data.get("wd_upi_type", "upi")

    # Deduct balance (USD) and record withdrawal
    with get_db() as conn:
        wd_id = conn.execute(
            "INSERT INTO withdrawals (user_id, amount, upi_id) VALUES (%s,%s,%s) RETURNING id",
            (user.id, amount_usd, upi_val if upi_type == "upi" else "[QR Code]")
        ).fetchone()["id"]
        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (amount_usd, user.id)
        )

    new_balance = get_balance(user.id)

    await update.message.reply_text(
        f"<b>✅ Withdrawal Request Submitted!</b>\n\n"
        f"<b>💸 Amount: ₹{amount_inr:.0f} (${amount_usd:.2f})</b>\n"
        f"<b>🆔 Reference ID:</b> <code>{wd_id}</code>\n"
        f"<b>💰 Remaining Balance: {fmt(new_balance)}</b>\n\n"
        f"⏳ <b>Admin will review and process your withdrawal shortly.</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())

    # Inline buttons for admin
    wd_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}"),
         InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{wd_id}")]
    ])

    upi_line = (
        f"<b>📲 UPI ID:</b> <code>{upi_val}</code>" if upi_type == "upi"
        else "<b>📲 Payment:</b> QR Code (see below)"
    )

    admin_text = (
        f"<b>💸 NEW WITHDRAWAL REQUEST</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>👤 User:</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
        f"<b>💵 Amount: {fmt(amount)}</b>\n"
        f"{upi_line}\n"
        f"<b>🆔 Withdrawal ID:</b> <code>{wd_id}</code>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"
    )

    # Channel gets NO username, NO UPI — only chat ID, amount, status
    channel_text = (
        f"<b>💸 WITHDRAWAL REQUEST</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>🆔 User ID:</b> <code>{user.id}</code>\n"
        f"<b>💵 Amount: {fmt(amount)}</b>\n"
        f"<b>🔖 Withdrawal ID:</b> <code>{wd_id}</code>\n"
        f"<b>📊 Status: ⏳ Pending</b>"
    )

    if upi_type == "qr":
        await ctx.bot.send_photo(ADMIN_ID, photo=upi_val, caption=admin_text,
                                 parse_mode="HTML", reply_markup=wd_kb)
    else:
        await ctx.bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=wd_kb)

    await send_to_channel(ctx.bot, TRADES_CHANNEL, channel_text)
    return ConversationHandler.END

async def wd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin approves a withdrawal — works as both text command and inline button."""
    # Handle both callback query and text command
    if update.callback_query:
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True); return
        wd_id = int(query.data.split("_")[2])
    else:
        if update.effective_user.id != ADMIN_ID: return
        try:
            wd_id = int(update.message.text.split("_")[2])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /wd_approve_<id>"); return

    with get_db() as conn:
        wd = conn.execute("SELECT * FROM withdrawals WHERE id=%s", (wd_id,)).fetchone()
        if not wd:
            if update.callback_query: await update.callback_query.answer("Not found.", show_alert=True)
            else: await update.message.reply_text("❌ Not found.")
            return
        if wd["status"] != "pending":
            if update.callback_query: await update.callback_query.answer("Already processed.", show_alert=True)
            else: await update.message.reply_text("⚠️ Already processed.")
            return
        conn.execute("UPDATE withdrawals SET status='approved' WHERE id=%s", (wd_id,))

    if update.callback_query:
        await update.callback_query.edit_message_caption(
            caption=f"<b>✅ Withdrawal #{wd_id} — APPROVED</b>\n<b>💵 {fmt(wd['amount'])}</b> paid to <code>{wd['user_id']}</code>",
            parse_mode="HTML") if update.callback_query.message.caption else None
        try:
            await update.callback_query.edit_message_text(
                f"<b>✅ Withdrawal #{wd_id} — APPROVED</b>\n<b>💵 {fmt(wd['amount'])}</b> paid to <code>{wd['user_id']}</code>",
                parse_mode="HTML")
        except Exception:
            pass
        await update.callback_query.answer("✅ Approved!")
    else:
        await update.message.reply_text(
            f"✅ Withdrawal #{wd_id} approved! <b>{fmt(wd['amount'])}</b> paid to <code>{wd['user_id']}</code>.",
            parse_mode="HTML")

    await ctx.bot.send_message(
        wd["user_id"],
        f"<b>🎉 Withdrawal Approved!</b>\n\n"
        f"<b>💸 {fmt(wd['amount'])}</b> has been processed.\n"
        f"<b>🆔 Ref:</b> <code>{wd_id}</code>\n\n"
        f"<b>Thank you for using TG Market! 🛒</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"✅ <b>WITHDRAWAL APPROVED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Withdrawal ID: <code>{wd_id}</code>\n"
        f"🆔 User ID: <code>{wd['user_id']}</code>\n"
        f"💵 Amount: <b>{fmt(wd['amount'])}</b>\n"
        f"📊 Status: <b>✅ Approved & Paid</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 @OtpSellerStore_Bot"
    )

async def wd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin rejects a withdrawal — works as both text command and inline button."""
    if update.callback_query:
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True); return
        wd_id = int(query.data.split("_")[2])
    else:
        if update.effective_user.id != ADMIN_ID: return
        try:
            wd_id = int(update.message.text.split("_")[2])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /wd_reject_<id>"); return

    with get_db() as conn:
        wd = conn.execute("SELECT * FROM withdrawals WHERE id=%s", (wd_id,)).fetchone()
        if not wd or wd["status"] != "pending":
            if update.callback_query: await update.callback_query.answer("Not found or already processed.", show_alert=True)
            else: await update.message.reply_text("❌ Not found or already processed.")
            return
        conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=%s", (wd_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (wd["amount"], wd["user_id"]))

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                f"<b>❌ Withdrawal #{wd_id} — REJECTED</b> — balance refunded.",
                parse_mode="HTML")
        except Exception:
            pass
        await update.callback_query.answer("❌ Rejected.")
    else:
        await update.message.reply_text(
            f"❌ Withdrawal #{wd_id} rejected. <b>{fmt(wd['amount'])}</b> refunded.",
            parse_mode="HTML")

    await ctx.bot.send_message(
        wd["user_id"],
        f"<b>❌ Withdrawal Rejected</b>\n\n"
        f"Your withdrawal of <b>{fmt(wd['amount'])}</b> (ID: <code>{wd_id}</code>) was not approved.\n"
        f"<b>💰 {fmt(wd['amount'])}</b> has been refunded to your balance.\n\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"❌ <b>WITHDRAWAL REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Withdrawal ID: <code>{wd_id}</code>\n"
        f"🆔 User ID: <code>{wd['user_id']}</code>\n"
        f"💵 Amount: <b>{fmt(wd['amount'])}</b>\n"
        f"📊 Status: <b>❌ Rejected — Refunded</b>"
    )

# ── Admin: get file ID from a photo ──────────────────────────────────────────
async def admin_getfileid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sends a photo → bot replies with its file_id.
    Use this to get the PAYMENT_QR_FILE_ID value."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.photo:
        await update.message.reply_text(
            "📸 Send your QR code photo directly to the bot (no command needed).\n\n"
            "Just send the image and I'll reply with the file ID.")
        return
    file_id = update.message.photo[-1].file_id
    await update.message.reply_text(
        f"✅ <b>File ID:</b>\n\n<code>{file_id}</code>\n\n"
        f"Copy this value and set it as <code>PAYMENT_QR_FILE_ID</code> in your environment variables.",
        parse_mode="HTML")


# ── Admin: Broadcast ─────────────────────────────────────────────────────────
async def admin_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a message to all bot users.
    Usage: /broadcast Your message here (multiline supported)
    Supports HTML formatting. The message is sent with a 🛒 Buy Now button.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    # Use raw message text so newlines and formatting are preserved exactly.
    # Strip the /broadcast command prefix (handles /broadcast and /broadcast@botname).
    raw = update.message.text or ""
    # Find where the command ends (first whitespace after the command word)
    if " " in raw or "\n" in raw:
        # Split on the first whitespace character (space or newline)
        import re as _re
        match = _re.match(r"^/\S+\s+(.*)", raw, _re.DOTALL)
        text = match.group(1).strip() if match else ""
    else:
        text = ""

    if not text:
        await update.message.reply_text(
            "<b>📢 Broadcast Usage:</b>\n\n"
            "<code>/broadcast Your message here</code>\n\n"
            "Newlines and formatting are preserved exactly as you type.\n\n"
            "<b>Example:</b>\n"
            "<code>/broadcast ✅ NEW ACCOUNTS ADDED!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "� India — $2.00\n"
            "� Pakistan — $1.50\n\n"
            "Purchase now‼️</code>",
            parse_mode="HTML")
        return

    # Fetch all user IDs
    with get_db() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()

    sent = 0
    failed = 0
    for u in users:
        try:
            await ctx.bot.send_message(
                u["user_id"],
                text,
                parse_mode="HTML"
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"<b>✅ Broadcast Complete</b>\n\n"
        f"<b>📨 Sent:</b> <code>{sent}</code>\n"
        f"<b>❌ Failed:</b> <code>{failed}</code> (blocked / deleted)",
        parse_mode="HTML")


# ── ADMIN PANEL ───────────────────────────────────────────────────────────────
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Central admin panel — only visible to admin."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return
    await query.answer()

    with get_db() as conn:
        total_users    = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        total_balance  = conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users").fetchone()["s"]
        avail_accounts = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE status='available'").fetchone()["c"]
        pending_deps   = conn.execute("SELECT COUNT(*) AS c FROM deposits WHERE status='pending'").fetchone()["c"]
        pending_wds    = conn.execute("SELECT COUNT(*) AS c FROM withdrawals WHERE status='pending'").fetchone()["c"]
        pending_sells  = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE status='pending_review'").fetchone()["c"]

    await query.edit_message_text(
        f"<b>⚙️ ADMIN PANEL</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"👥 <b>Total Users:</b> <b>{total_users}</b>\n"
        f"🏦 <b>Total Wallet Funds:</b> <b>{fmt(float(total_balance))}</b>\n"
        f"📦 <b>Available Accounts:</b> <b>{avail_accounts}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"⏳ <b>Pending Deposits:</b> <b>{pending_deps}</b>\n"
        f"💸 <b>Pending Withdrawals:</b> <b>{pending_wds}</b>\n"
        f"🔄 <b>Pending Sell Reviews:</b> <b>{pending_sells}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Account",      callback_data="ap_add_account"),
             InlineKeyboardButton("📦 All Accounts",     callback_data="ap_list_accounts")],
            [InlineKeyboardButton("🔄 Pending Sells",    callback_data="ap_pending_sells"),
             InlineKeyboardButton("💰 Country Prices",   callback_data="ap_prices_menu")],
            [InlineKeyboardButton("📥 Pending Deposits", callback_data="ap_pending_deps"),
             InlineKeyboardButton("💸 Pending Withdrawals", callback_data="ap_pending_wds")],
            [InlineKeyboardButton("👥 User Stats",       callback_data="ap_user_stats"),
             InlineKeyboardButton("📢 Broadcast",        callback_data="ap_broadcast_prompt")],
            [InlineKeyboardButton("🔙 Back to Menu",     callback_data="menu_back")],
        ])
    )

async def ap_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Add Account (redirects to /login_account flow)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()
    await query.edit_message_text(
        "<b>➕ ADD ACCOUNT</b>\n\n"
        "Use the command below to add a new account by logging in via phone + OTP:\n\n"
        "<code>/login_account</code>\n\n"
        "The bot will guide you through:\n"
        "1️⃣ Enter phone number\n"
        "2️⃣ Enter OTP\n"
        "3️⃣ Enter price\n\n"
        "The account will be added and listed instantly.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")]
        ])
    )

async def ap_list_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → All Accounts, filterable by server, with Edit Price + Change Server buttons."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()

    # callback_data is ap_list_accounts  OR  ap_list_accounts_1 / ap_list_accounts_2
    data          = query.data
    server_filter = None
    if data.startswith("ap_list_accounts_"):
        try:
            server_filter = int(data.split("ap_list_accounts_")[1])
        except ValueError:
            server_filter = None

    with get_db() as conn:
        if server_filter:
            rows = conn.execute(
                "SELECT id, phone, price, status, buyer_id, server FROM accounts "
                "WHERE server=%s ORDER BY id DESC LIMIT 30",
                (server_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, phone, price, status, buyer_id, server FROM accounts ORDER BY id DESC LIMIT 30"
            ).fetchall()

    if not rows:
        await query.edit_message_text(
            "📦 No accounts yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    icons  = {"available": "🟢", "sold": "✅", "pending_review": "🔄"}
    slabel = {1: "🔵S1", 2: "🟢S2"}
    lines  = [f"<b>📦 Accounts (latest 30)</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    buttons = []

    # Server filter row at the top
    buttons.append([
        InlineKeyboardButton("📋 All",          callback_data="ap_list_accounts"),
        InlineKeyboardButton("🔵 Server 1",     callback_data="ap_list_accounts_1"),
        InlineKeyboardButton("🟢 Server 2",     callback_data="ap_list_accounts_2"),
    ])

    for r in rows:
        flag, _ = phone_to_country(r["phone"] or "")
        icon    = icons.get(r["status"], "⚪")
        srv     = slabel.get(r["server"], f"S{r['server']}")
        line    = (f"{icon} <b>#{r['id']}</b> [{srv}] {flag} <code>{mask_phone(r['phone'] or '')}</code>"
                   f" — <b>{fmt(r['price'])}</b> [{r['status']}]")
        if r["buyer_id"]:
            line += f" → <code>{r['buyer_id']}</code>"
        lines.append(line)
        # Edit Price + Change Server buttons for available accounts
        if r["status"] == "available":
            other_srv   = 2 if r["server"] == 1 else 1
            other_label = "→ S2" if other_srv == 2 else "→ S1"
            buttons.append([
                InlineKeyboardButton(
                    f"✏️ #{r['id']} Price ({fmt(r['price'])})",
                    callback_data=f"acc_editprice_{r['id']}"
                ),
                InlineKeyboardButton(
                    f"📡 {other_label}",
                    callback_data=f"acc_chgsrv_{r['id']}_{other_srv}"
                ),
            ])

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n\n<i>... truncated</i>"

    buttons.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")])
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def acc_chgsrv_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps 📡 → S1 / → S2 to move account between servers instantly."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    parts  = query.data.split("acc_chgsrv_")[1].split("_")
    acc_id = int(parts[0])
    new_srv = int(parts[1])
    with get_db() as conn:
        acc = conn.execute("SELECT id, status FROM accounts WHERE id=%s", (acc_id,)).fetchone()
        if not acc or acc["status"] != "available":
            await query.answer("Account not found or not available.", show_alert=True); return
        conn.execute("UPDATE accounts SET server=%s WHERE id=%s", (new_srv, acc_id))
    label = "🔵 Server 1" if new_srv == 1 else "🟢 Server 2"
    await query.answer(f"✅ #{acc_id} moved to {label}", show_alert=True)
    # Refresh the list
    await ap_list_accounts(update, ctx)

# ── Admin: edit account price ─────────────────────────────────────────────────
ACC_EDIT_PRICE = 20   # conversation state

async def acc_editprice_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ✏️ Edit Price on an account."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()
    acc_id = int(query.data.split("acc_editprice_")[1])
    with get_db() as conn:
        acc = conn.execute(
            "SELECT id, phone, price FROM accounts WHERE id=%s AND status='available'", (acc_id,)
        ).fetchone()
    if not acc:
        await query.answer("Account not found or no longer available.", show_alert=True); return
    ctx.user_data["acc_edit_id"] = acc_id
    flag, country = phone_to_country(acc["phone"] or "")
    await query.edit_message_text(
        f"<b>✏️ EDIT ACCOUNT PRICE</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>🆔 Account:</b> <code>#{acc_id}</code>\n"
        f"<b>🌍 Country:</b> {flag} {country}\n"
        f"<b>📱 Phone:</b> <code>{mask_phone(acc['phone'] or '')}</code>\n"
        f"<b>💵 Current Price:</b> {fmt(acc['price'])}\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"Enter the new price in <b>USD</b> (e.g. <code>5.00</code>):\n\n"
        f"Send /apcancel to go back.",
        parse_mode="HTML"
    )
    return ACC_EDIT_PRICE

async def acc_editprice_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sent new price for an account."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    acc_id = ctx.user_data.get("acc_edit_id")
    if not acc_id:
        await update.message.reply_text("❌ No account selected. Use the Edit Price button.")
        return ConversationHandler.END
    try:
        new_price = float(update.message.text.strip().replace("$", ""))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid price. Enter a positive number like <code>5.00</code>",
            parse_mode="HTML")
        return ACC_EDIT_PRICE

    with get_db() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=%s", (acc_id,)).fetchone()
        if not acc:
            await update.message.reply_text(f"❌ Account #{acc_id} not found.")
            return ConversationHandler.END
        old_price = acc["price"]
        conn.execute("UPDATE accounts SET price=%s WHERE id=%s", (new_price, acc_id))

    ctx.user_data.pop("acc_edit_id", None)
    flag, country = phone_to_country(acc["phone"] or "")
    await update.message.reply_text(
        f"<b>✅ Price Updated!</b>\n\n"
        f"<b>🆔 Account:</b> <code>#{acc_id}</code>\n"
        f"<b>🌍</b> {flag} {country}\n"
        f"<b>📱</b> <code>{mask_phone(acc['phone'] or '')}</code>\n"
        f"<b>💵 Old Price:</b> {fmt(old_price)}\n"
        f"<b>💵 New Price:</b> {fmt(new_price)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Back to Accounts", callback_data="ap_list_accounts")],
            [InlineKeyboardButton("🔙 Back to Panel",    callback_data="admin_panel")],
        ])
    )
    return ConversationHandler.END

async def ap_pending_sells(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Pending sell submissions."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, phone FROM accounts WHERE status='pending_review' ORDER BY id DESC"
        ).fetchall()

    if not rows:
        await query.edit_message_text(
            "✅ No pending sell submissions.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    buttons = []
    for r in rows:
        flag, _ = phone_to_country(r["phone"] or "")
        buttons.append([
            InlineKeyboardButton(f"✅ Approve #{r['id']} {flag}", callback_data=f"sell_approve_{r['id']}"),
            InlineKeyboardButton(f"❌ Reject #{r['id']}",         callback_data=f"sell_reject_{r['id']}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")])

    await query.edit_message_text(
        f"<b>🔄 Pending Sell Reviews ({len(rows)})</b>\n\n"
        "Approve to set a price and list, or reject to remove.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def ap_prices_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Country Prices (re-uses existing price panel)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()

    text = (
        "<b>💰 Country Price Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\n"
        "Tap a country to edit or delete it.\n"
        "Tap ➕ Add Country to add a new one."
    )
    # Add back-to-panel button at top of keyboard
    kb = _prices_panel_keyboard(rows)
    # Inject a "Back to Panel" row before the last row
    rows_list = list(kb.inline_keyboard)
    rows_list.insert(0, [InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")])
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows_list)
    )

async def ap_pending_deps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Pending deposits with approve/reject buttons."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()

    with get_db() as conn:
        deps = conn.execute(
            "SELECT d.id, d.user_id, d.amount, u.username "
            "FROM deposits d JOIN users u ON d.user_id=u.user_id "
            "WHERE d.status='pending' ORDER BY d.id DESC"
        ).fetchall()

    if not deps:
        await query.edit_message_text(
            "✅ No pending deposits.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    buttons = []
    for d in deps:
        label = f"#{d['id']} @{d['username'] or d['user_id']} — {fmt(d['amount'])}"
        buttons.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"dep_approve_{d['id']}"),
            InlineKeyboardButton(f"❌ Reject",  callback_data=f"dep_reject_{d['id']}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")])

    await query.edit_message_text(
        f"<b>📥 Pending Deposits ({len(deps)})</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def ap_pending_wds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Pending withdrawals with approve/reject buttons."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()

    with get_db() as conn:
        wds = conn.execute(
            "SELECT id, user_id, amount, upi_id FROM withdrawals WHERE status='pending' ORDER BY id DESC"
        ).fetchall()

    if not wds:
        await query.edit_message_text(
            "✅ No pending withdrawals.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return

    buttons = []
    for w in wds:
        label = f"#{w['id']} <code>{w['user_id']}</code> — {fmt(w['amount'])}"
        buttons.append([
            InlineKeyboardButton(f"✅ Approve #{w['id']}", callback_data=f"wd_approve_{w['id']}"),
            InlineKeyboardButton(f"❌ Reject #{w['id']}",  callback_data=f"wd_reject_{w['id']}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")])

    lines = [f"<b>💸 Pending Withdrawals ({len(wds)})</b>\n"]
    for w in wds:
        lines.append(f"<b>#{w['id']}</b> — User <code>{w['user_id']}</code> — <b>{fmt(w['amount'])}</b>\n   UPI: <code>{w['upi_id'] or '—'}</code>")

    await query.edit_message_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def ap_user_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → User stats (inline version)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer("⏳ Loading stats...")

    with get_db() as conn:
        users      = conn.execute("SELECT user_id, balance, created_at FROM users").fetchall()
        dep_count  = conn.execute("SELECT COUNT(*) AS c FROM deposits WHERE status='approved'").fetchone()["c"]
        sell_count = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE status='sold'").fetchone()["c"]
        wd_count   = conn.execute("SELECT COUNT(*) AS c FROM withdrawals WHERE status='approved'").fetchone()["c"]
        total_bal  = conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users").fetchone()["s"]

    total  = len(users)
    funded = sum(1 for u in users if float(u["balance"]) > 0)
    from datetime import datetime, timezone, timedelta
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    new_7d = sum(1 for u in users if u["created_at"] and
                 (u["created_at"].replace(tzinfo=timezone.utc) if u["created_at"].tzinfo is None else u["created_at"]) >= week_ago)

    await query.edit_message_text(
        f"<b>👥 USER STATISTICS</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"👤 Total Users:        <b>{total}</b>\n"
        f"💰 Users with Balance: <b>{funded}</b>\n"
        f"🆕 Joined Last 7d:     <b>{new_7d}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"✅ Approved Deposits:  <b>{dep_count}</b>\n"
        f"🛒 Accounts Sold:      <b>{sell_count}</b>\n"
        f"💸 Withdrawals Paid:   <b>{wd_count}</b>\n"
        f"🏦 Total Wallet Funds: <b>{fmt(float(total_bal))}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")]
        ])
    )

async def ap_broadcast_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel → Broadcast prompt."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    await query.answer()
    await query.edit_message_text(
        "<b>📢 BROADCAST</b>\n\n"
        "Use the command below to send a message to all users:\n\n"
        "<code>/broadcast Your message here</code>\n\n"
        "Supports HTML formatting and multiple lines.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_panel")]
        ])
    )

# ── Flask + Main ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    deposit_conv = ConversationHandler(
        name="deposit",
        entry_points=[CallbackQueryHandler(deposit_start, pattern="^menu_deposit$")],
        states={
            DEPOSIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount),
                CallbackQueryHandler(deposit_ipaid_cb,  pattern="^dep_ipaid$"),
                CallbackQueryHandler(deposit_qrnw_cb,   pattern="^dep_qrnw$"),
                CallbackQueryHandler(deposit_cancel_cb, pattern="^dep_cancel$"),
            ],
            DEPOSIT_UTR:        [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_utr)],
            DEPOSIT_PROOF:      [MessageHandler(filters.PHOTO, deposit_proof)],
            DEPOSIT_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
        allow_reentry=True,
    )
    withdraw_conv = ConversationHandler(
        name="withdraw",
        entry_points=[CallbackQueryHandler(withdraw_menu, pattern="^menu_withdraw$")],
        states={
            WITHDRAW_UPI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi),
                MessageHandler(filters.PHOTO, withdraw_upi),
            ],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    login_conv = ConversationHandler(
        name="login",
        entry_points=[CommandHandler("login_account", admin_login)],
        states={
            ADMIN_PHONE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            ADMIN_OTP:          [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            ADMIN_2FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
            ADMIN_ADD_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, set_price)],
            ADMIN_SERVER_SELECT:[CallbackQueryHandler(set_server, pattern=r"^addacc_server_[12]$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("login_account", admin_login),
        ],
        per_message=False,
        allow_reentry=True,
    )
    sell_conv = ConversationHandler(
        name="sell",
        entry_points=[CallbackQueryHandler(sell_menu, pattern="^menu_sell$")],
        states={
            SELL_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_get_phone)],
            SELL_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_get_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", sell_cancel),
            CallbackQueryHandler(menu_back, pattern="^menu_back$"),
        ],
        per_message=False,
    )
    apanel_conv = ConversationHandler(
        name="apanel",
        entry_points=[
            CommandHandler("adminprices", admin_prices_panel),
            CallbackQueryHandler(ap_add,  pattern="^ap_add$"),
        ],
        states={
            APANEL_ADD_WAITING:  [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), ap_add_save)],
            APANEL_EDIT_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), ap_edit_save)],
        },
        fallbacks=[CommandHandler("apcancel", ap_cancel)],
        per_message=False,
        allow_reentry=True,
    )
    sell_approve_conv = ConversationHandler(
        name="sell_approve",
        entry_points=[CallbackQueryHandler(sell_approve_cb, pattern=r"^sell_approve_\d+$")],
        states={
            SELL_APPROVE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), sell_approve_price)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
        allow_reentry=True,
    )
    acc_editprice_conv = ConversationHandler(
        name="acc_editprice",
        entry_points=[CallbackQueryHandler(acc_editprice_start, pattern=r"^acc_editprice_\d+$")],
        states={
            ACC_EDIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), acc_editprice_save)
            ],
        },
        fallbacks=[CommandHandler("apcancel", ap_cancel)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CallbackQueryHandler(check_joined_cb, pattern="^check_joined$"))
    # ── Admin Panel ──────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(admin_panel,          pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(ap_add_account,       pattern="^ap_add_account$"))
    app.add_handler(CallbackQueryHandler(ap_pending_sells,     pattern="^ap_pending_sells$"))
    app.add_handler(CallbackQueryHandler(ap_prices_menu,       pattern="^ap_prices_menu$"))
    app.add_handler(CallbackQueryHandler(ap_pending_deps,      pattern="^ap_pending_deps$"))
    app.add_handler(CallbackQueryHandler(ap_pending_wds,       pattern="^ap_pending_wds$"))
    app.add_handler(CallbackQueryHandler(ap_user_stats,        pattern="^ap_user_stats$"))
    app.add_handler(CallbackQueryHandler(ap_broadcast_prompt,  pattern="^ap_broadcast_prompt$"))
    # ─────────────────────────────────────────────────────────────────────────
    app.add_handler(deposit_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(login_conv)
    app.add_handler(sell_conv)
    app.add_handler(sell_approve_conv)
    app.add_handler(acc_editprice_conv)
    app.add_handler(apanel_conv)
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("accounts",    admin_accounts))
    app.add_handler(CommandHandler("users",       admin_users))
    app.add_handler(CommandHandler("pending",     admin_pending))
    app.add_handler(CommandHandler("credit",      admin_credit))
    app.add_handler(CommandHandler("deduct",      admin_deduct))
    # Deposit inline approve/reject buttons
    app.add_handler(CallbackQueryHandler(dep_approve_cb, pattern=r"^dep_approve_\d+$"))
    app.add_handler(CallbackQueryHandler(dep_reject_cb,  pattern=r"^dep_reject_\d+$"))
    # Withdrawal inline approve/reject buttons
    app.add_handler(CallbackQueryHandler(wd_approve, pattern=r"^wd_approve_\d+$"))
    app.add_handler(CallbackQueryHandler(wd_reject,  pattern=r"^wd_reject_\d+$"))
    # Sell inline reject button (approve is handled by sell_approve_conv above)
    app.add_handler(CallbackQueryHandler(sell_reject_cb, pattern=r"^sell_reject_\d+$"))
    app.add_handler(CommandHandler("add_sell",    admin_add_sell))
    app.add_handler(CommandHandler("prices",      cmd_prices))
    app.add_handler(CommandHandler("broadcast",   admin_broadcast))
    app.add_handler(MessageHandler(filters.Regex(r"^/wd_approve_\d+$") & filters.User(ADMIN_ID), wd_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/wd_reject_\d+$")  & filters.User(ADMIN_ID), wd_reject))
    # Admin panel inline button handlers (outside conversation for view/del/back/close)
    app.add_handler(CallbackQueryHandler(ap_view,       pattern=r"^ap_view_"))
    app.add_handler(CallbackQueryHandler(ap_edit,       pattern=r"^ap_edit_"))
    app.add_handler(CallbackQueryHandler(ap_del,        pattern=r"^ap_del_[A-Z]"))
    app.add_handler(CallbackQueryHandler(ap_del_confirm,pattern=r"^ap_delconfirm_"))
    app.add_handler(CallbackQueryHandler(ap_back,       pattern="^ap_back$"))
    app.add_handler(CallbackQueryHandler(ap_close,      pattern="^ap_close$"))
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\d+$") & filters.User(ADMIN_ID), admin_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/reject_\d+$")  & filters.User(ADMIN_ID), admin_reject))
    app.add_handler(MessageHandler(filters.Regex(r"^/del_\d+$")     & filters.User(ADMIN_ID), admin_delete))
    app.add_handler(CallbackQueryHandler(buy_menu,      pattern="^menu_buy$"))
    app.add_handler(CallbackQueryHandler(buy_server,    pattern=r"^buy_server_[12]$"))
    app.add_handler(CallbackQueryHandler(buy_server,    pattern=r"^buy_spage_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(buy_country,   pattern=r"^buycountry_\d+_"))
    app.add_handler(CallbackQueryHandler(view_account,  pattern=r"^view_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_buy,   pattern=r"^confirm_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(getotp_cb,     pattern=r"^getotp_\d+$"))
    app.add_handler(CallbackQueryHandler(acc_chgsrv_cb, pattern=r"^acc_chgsrv_\d+_[12]$"))
    app.add_handler(CallbackQueryHandler(ap_list_accounts, pattern="^ap_list_accounts$"))
    app.add_handler(CallbackQueryHandler(ap_list_accounts, pattern=r"^ap_list_accounts_[12]$"))
    app.add_handler(CallbackQueryHandler(show_balance,  pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(refer_menu,    pattern="^menu_refer$"))
    app.add_handler(CallbackQueryHandler(my_orders,     pattern="^menu_orders$"))
    app.add_handler(CallbackQueryHandler(menu_back,     pattern="^menu_back$"))
    return app

def main():
    global ptb_app
    init_db()
    ptb_app = build_app()

    import asyncio
    import signal
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    # ── Health-check server so Render sees an open port ───────────────────────
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(),
        daemon=True,
    ).start()
    logger.info(f"✅ Health-check server on port {PORT}")

    async def run():
        logger.info("🚀 Starting bot in polling mode...")
        await ptb_app.initialize()
        await ptb_app.bot.delete_webhook(drop_pending_updates=True)
        await ptb_app.start()
        await ptb_app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            # These two settings keep the connection alive on Render
            read_timeout=30,
            write_timeout=30,
            connect_timeout=30,
            pool_timeout=30,
        )
        logger.info("✅ Bot is running.")
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows
        await stop_event.wait()
        logger.info("🛑 Shutting down...")
        await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()

    asyncio.run(run())

if __name__ == "__main__":
    main()
