import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import time
import threading
import os
import tempfile
from urllib.parse import urlparse
from datetime import datetime

# ================= Configuration =================
BOT_TOKEN = "8430154368:AAFdGqechsFJEclZjGMzbx9t9JVIdL3I05E"
ADMIN_ID = 8725461956
ADMIN_CHANNEL_ID = "@thelitegiveaway"  # Withdrawal requests go ONLY to this public channel

bot = telebot.TeleBot(BOT_TOKEN)

# Auto-detect Bot Username
try:
    BOT_USERNAME = bot.get_me().username
except Exception as e:
    print(f"Error fetching bot username: {e}")
    BOT_USERNAME = "YourBotUsername"

# ================= Database Setup =================
DB_PATH = os.path.abspath("bot_database.db")
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "database_backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.RLock()


def backup_database(label="auto"):
    """Create a consistent SQLite backup without interrupting bot operations."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"bot_database_{label}_{stamp}.db")
    try:
        with db_lock:
            conn.commit()
            backup_conn = sqlite3.connect(backup_path)
            try:
                conn.backup(backup_conn)
                backup_conn.commit()
            finally:
                backup_conn.close()
        print(f"Database backup created: {backup_path}")
        return backup_path
    except Exception as exc:
        print(f"Database backup failed: {exc}")
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
        except OSError:
            pass
        return None


def automatic_backup_loop():
    """Create a backup and send it to the admin every two hours.

    The first backup is sent shortly after startup. Local copies are also
    retained in BACKUP_DIR on the server.
    """
    while True:
        backup_path = backup_database("auto")
        if backup_path:
            try:
                with open(backup_path, "rb") as backup_file:
                    bot.send_document(
                        ADMIN_ID,
                        backup_file,
                        caption=(
                            "🗄️ Automatic database backup\n"
                            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            "Keep this .db file safe. To restore it after moving servers, "
                            "open Admin Panel → 📥 Load Database and upload this file."
                        )
                    )
                print(f"Database backup sent to admin {ADMIN_ID}: {backup_path}")
            except Exception as exc:
                print(f"Could not send database backup to admin: {exc}")
        time.sleep(2 * 60 * 60)

# Users Table
cursor.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    total_invites INTEGER DEFAULT 0,
    total_withdrawals INTEGER DEFAULT 0
)''')

# History Table
cursor.execute('''CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    reason TEXT,
    date TEXT
)''')

# Channels Table (For Force Sub)
cursor.execute('''CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    link TEXT
)''')
conn.commit()

# ================= Helper Functions =================
def add_user(user_id):
    with db_lock:
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        created = cursor.rowcount == 1
        conn.commit()
        return created

def add_history(user_id, amount, reason):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        cursor.execute(
            "INSERT INTO history (user_id, amount, reason, date) VALUES (?, ?, ?, ?)",
            (user_id, amount, reason, date_str)
        )
        conn.commit()

# Public channels are mandatory for all users, including the admin, once configured.
# If no channel has been configured yet, access stays open so the admin can set up the first one.
def check_force_sub(user_id):
    with db_lock:
        cursor.execute("SELECT channel_id FROM channels")
        channels = cursor.fetchall()

    if not channels:
        return True

    for (ch_id,) in channels:
        try:
            status = bot.get_chat_member(ch_id, user_id).status
            if status not in ("creator", "administrator", "member", "restricted"):
                return False
            if status == "restricted":
                member = bot.get_chat_member(ch_id, user_id)
                if not getattr(member, "is_member", False):
                    return False
        except Exception as exc:
            print(f"Force-sub check failed for {ch_id}, user {user_id}: {exc}")
            return False
    return True

def require_force_sub(chat_id, user_id):
    if check_force_sub(user_id):
        return True
    bot.send_message(
        chat_id,
        "⚠️ **Action Required!**\n\nTo use this bot, join every official channel below, then tap '✅ I Have Joined'.",
        reply_markup=force_sub_keyboard(),
        parse_mode="Markdown"
    )
    return False

def get_welcome_text(user_name):
    return f"""👋 **Welcome to the API Bot, {user_name}!** 🤖

Get API access instantly. 
💰 Refer your friends to earn credits and unlock premium API access!

👇 Select an option from the menu below to get started:"""

# ================= Keyboards =================
def main_menu(user_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("💸 Invite & Earn"),
        KeyboardButton("👀 Profile"),
        KeyboardButton("📊 Bot Status"),
        KeyboardButton("📜 Invite History"),
        KeyboardButton("🔑 Get API"),
        KeyboardButton("⚠️ Help & Support")
    )
    if user_id == ADMIN_ID:
        markup.add(KeyboardButton("🛠 Admin Panel"))
    return markup

def force_sub_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    cursor.execute("SELECT channel_id, link FROM channels")
    for ch_id, link in cursor.fetchall():
        markup.add(InlineKeyboardButton("📢 Join Channel", url=link))
    markup.add(InlineKeyboardButton("✅ I Have Joined", callback_data="check_joined"))
    return markup

# ================= Handlers =================

@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    args = message.text.split()
    
    is_new = add_user(user_id)
    
    # Referral reward is given only once, for a genuinely new user and an existing referrer.
    if is_new and len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id:
            with db_lock:
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
                referrer_exists = cursor.fetchone() is not None
                if referrer_exists:
                    cursor.execute(
                        "UPDATE users SET balance = balance + 1, total_invites = total_invites + 1 WHERE user_id = ?",
                        (referrer_id,)
                    )
                    referred_username = f"@{message.from_user.username}" if message.from_user.username else "No username"
                    referred_name = message.from_user.first_name or "New user"
                    reason = f"Referral: {referred_name} ({referred_username}) joined"
                    cursor.execute(
                        "INSERT INTO history (user_id, amount, reason, date) VALUES (?, ?, ?, ?)",
                        (referrer_id, 1, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    conn.commit()
            if referrer_exists:
                try:
                    referred_username = f"@{message.from_user.username}" if message.from_user.username else "No username set"
                    bot.send_message(
                        referrer_id,
                        f"🎉 New referral joined!\n\n👤 Name: {message.from_user.first_name or 'New user'}\n🔗 Username: {referred_username}\n🆔 User ID: {user_id}\n\n💰 Referral reward: +1 credit"
                    )
                except Exception:
                    pass

    # Strict Check: Agar join nahi hai, to yahi block ho jayega
    if not require_force_sub(message.chat.id, user_id):
        return

    # Agar join hai, to welcome message
    bot.send_message(message.chat.id, get_welcome_text(message.from_user.first_name), reply_markup=main_menu(user_id), parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: call.data == "check_joined")
def check_joined_callback(call):
    if check_force_sub(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Subscription verified!")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, get_welcome_text(call.from_user.first_name), reply_markup=main_menu(call.from_user.id), parse_mode="Markdown")
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined all channels yet! Please join first.", show_alert=True)


@bot.message_handler(func=lambda message: message.text == "💸 Invite & Earn")
def invite_earn(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    
    ref_link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    text = f"""💎 Per Refer Reward
━━━━━━━━━━━━━━━

💰 Earn 1 credit Per Referral

━━━━━━━━━━━━━━━

🔗 Your Referral Link:
{ref_link}

━━━━━━━━━━━━━━━

✅ Share With Friends & Earn Easily!"""

    markup = InlineKeyboardMarkup()
    share_url = f"https://t.me/share/url?url={ref_link}"
    markup.add(
        InlineKeyboardButton("🔗 Share Link", url=share_url),
        InlineKeyboardButton("👥 My Invites", callback_data="my_invites")
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="back_to_menu"))
    
    bot.send_message(message.chat.id, text, reply_markup=markup, disable_web_page_preview=True)


@bot.message_handler(func=lambda message: message.text == "👀 Profile")
def profile(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    user_id = message.from_user.id
    cursor.execute("SELECT balance, total_invites, total_withdrawals FROM users WHERE user_id = ?", (user_id,))
    data = cursor.fetchone()
    if data:
        bal, inv, withdr = data
        text = f"""👀 Profile

🔗 User ID: `{user_id}`
👉 Total Invite: {inv}
💰 Current Balance: {bal}
💵 Total Withdrawal: {withdr}"""
        bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(func=lambda message: message.text == "📊 Bot Status")
def bot_status(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    msg = bot.send_message(message.chat.id, "Processing... ⏳")
    time.sleep(1) 
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    server_time = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    
    text = f"""📊 Bot Status Panel
━━━━━━━━━━━━━━━

🤖 Status ▸ Online
⚡ Performance ▸ Excellent
📅 Server Time ▸ {server_time}
👥 Total Users ▸ {total_users}

━━━━━━━━━━━━━━━

✅ All Services Are Running Good."""
    bot.edit_message_text(text, chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(func=lambda message: message.text == "📜 Invite History")
def invite_history(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    user_id = message.from_user.id
    
    cursor.execute("SELECT amount, reason, date FROM history WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    rows = cursor.fetchall()
    
    if not rows:
        bot.send_message(message.chat.id, "No history found.")
        return
        
    text = f"⭐ Star History ({len(rows)} recent records)\n\n"
    for amt, reason, date in rows:
        sign = "+" if amt > 0 else ""
        text += f"⭐ {sign}{amt} — {reason}\n📅 {date}\n\n"
        
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda message: message.text == "🔑 Get API")
def get_api(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    user_id = message.from_user.id
    
    add_user(user_id)
    with db_lock:
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    balance = row[0] if row else 0
    
    if balance < 15:
        text = f"""⚠️ Minimum Balance Required
━━━━━━━━━━━━━━━

💰 Your Balance: {balance} credit 
✅ Minimum Required: 15 credit 

👀 Make More Refers To Earn More Balance & Withdraw credit"""
        bot.send_message(message.chat.id, text)
    else:
        msg = bot.send_message(message.chat.id, f"Your balance is {balance} credit.\nCost: 2 credit = 1 Day API.\n\nType the number of credits you want to withdraw (Minimum 15):")
        bot.register_next_step_handler(msg, process_api_withdrawal)


def process_api_withdrawal(message):
    # Verify force-sub again at withdrawal time.
    if not require_force_sub(message.chat.id, message.from_user.id):
        return

    user_id = message.from_user.id
    try:
        amount = int((message.text or "").strip())
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "❌ Invalid number. Tap '🔑 Get API' and try again.")
        return

    if amount < 16 or amount % 2 != 0:
        bot.send_message(
            message.chat.id,
            "❌ Enter an even number of credits. Minimum valid withdrawal is 16 credits (2 credits = 1 day)."
        )
        return

    add_user(user_id)
    days = amount // 2
    username = f"@{message.from_user.username}" if message.from_user.username else "No username set"
    first_name = message.from_user.first_name or "Unknown"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Deduct credits and record the request first. If the channel post fails,
    # automatically refund the credits and remove this withdrawal history entry.
    with db_lock:
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        balance = row[0] if row else 0
        if amount > balance:
            bot.send_message(message.chat.id, f"❌ Not enough credits. Your balance is {balance}.")
            return

        cursor.execute(
            "UPDATE users SET balance = balance - ?, total_withdrawals = total_withdrawals + ? "
            "WHERE user_id = ? AND balance >= ?",
            (amount, amount, user_id, amount)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            bot.send_message(message.chat.id, "❌ Balance changed. Please try again.")
            return

        cursor.execute(
            "INSERT INTO history (user_id, amount, reason, date) VALUES (?, ?, ?, ?)",
            (user_id, -amount, f"Withdraw request ({days} Day API)", now)
        )
        history_id = cursor.lastrowid
        conn.commit()

    # Plain text avoids Markdown parse failures caused by names/usernames.
    admin_text = (
        "🆕 NEW API WITHDRAWAL REQUEST\n\n"
        f"👤 Name: {first_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 User ID: {user_id}\n"
        f"💰 Credits: {amount}\n"
        f"📅 Requested API: {days} days\n"
        f"🕒 Time: {now}\n\n"
        "⚙️ Action: Please process this request manually."
    )

    try:
        sent = bot.send_message(ADMIN_CHANNEL_ID, admin_text)
        if not sent:
            raise RuntimeError("Telegram did not confirm the channel message")
    except Exception as exc:
        print(f"Withdrawal channel notification failed for user {user_id}: {exc}")
        # Refund the user because the admin channel did not receive the request.
        with db_lock:
            cursor.execute(
                "UPDATE users SET balance = balance + ?, total_withdrawals = total_withdrawals - ? "
                "WHERE user_id = ?",
                (amount, amount, user_id)
            )
            cursor.execute("DELETE FROM history WHERE id = ?", (history_id,))
            conn.commit()
        bot.send_message(
            message.chat.id,
            "⚠️ Your withdrawal request could not be sent to the admin channel, so your credits have been refunded. "
            "Please contact support or try again later."
        )
        return

    bot.send_message(
        message.chat.id,
        f"✅ Withdrawal request sent to the admin channel!\n"
        f"📅 Requested API: {days} days\n"
        f"💰 Credits used: {amount}\n\n"
        "The admin will process it manually."
    )


@bot.message_handler(func=lambda message: message.text == "⚠️ Help & Support")
def help_support(message):
    if not require_force_sub(message.chat.id, message.from_user.id): return
    text = """⚠️ Help & Support

Kisi bhi problem ke liye contact karo:

👀 Support: @Aditya_dark0

Hum jald se jald reply karenge! ⚡"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Contact Support", url="https://t.me/Aditya_dark0"))
    bot.send_message(message.chat.id, text, reply_markup=markup)


# ================= Callbacks (Back & My Invites) =================
@bot.callback_query_handler(func=lambda call: call.data in ["back_to_menu", "my_invites"])
def callback_handler(call):
    if not require_force_sub(call.message.chat.id, call.from_user.id):
        return
        
    if call.data == "back_to_menu":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, get_welcome_text(call.from_user.first_name), reply_markup=main_menu(call.from_user.id), parse_mode="Markdown")
    elif call.data == "my_invites":
        invite_history(call.message)
        bot.answer_callback_query(call.id)


# ================= Admin Panel =================
@bot.message_handler(func=lambda message: message.text == "🛠 Admin Panel")
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    if not require_force_sub(message.chat.id, message.from_user.id): return
    
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("➕ Add Force Sub Channel", callback_data="admin_add_channel"),
        InlineKeyboardButton("⭐ Add Referral Credits", callback_data="admin_add_referral"),
        InlineKeyboardButton("📥 Load Database", callback_data="admin_load_database"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )
    bot.send_message(message.chat.id, "🛠 Admin Panel:\nSelect an option below:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_callbacks(call):
    if call.from_user.id != ADMIN_ID: return
    if not require_force_sub(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id, "Join the required channels first.", show_alert=True)
        return
    
    if call.data == "admin_add_channel":
        msg = bot.send_message(call.message.chat.id, "Send the Public Channel Username or Link.\n\nExample:\n`@my_channel`\nOR\n`https://t.me/my_channel`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_add_channel)
        
    elif call.data == "admin_add_referral":
        msg = bot.send_message(
            call.message.chat.id,
            "⭐ Add Referral Credits\n\nSend the user's Telegram ID:"
        )
        bot.register_next_step_handler(msg, process_referral_user_id)

    elif call.data == "admin_load_database":
        msg = bot.send_message(
            call.message.chat.id,
            "📥 Send the SQLite database file (.db, .sqlite, or .sqlite3).\n\n"
            "⚠️ The uploaded database will REPLACE the currently active bot database.\n"
            "A backup of the current database will be created first.\n"
            "The file must contain the users, history, and channels tables with the required columns."
        )
        bot.register_next_step_handler(msg, process_load_database_file)

    elif call.data == "admin_broadcast":
        msg = bot.send_message(call.message.chat.id, "Send the message you want to broadcast to all users:")
        bot.register_next_step_handler(msg, process_broadcast)


def process_referral_user_id(message):
    if message.from_user.id != ADMIN_ID:
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        bot.send_message(message.chat.id, "❌ Invalid user ID. Please open Admin Panel and try again.")
        return
    target_id = int(raw_id)
    with db_lock:
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,))
        exists = cursor.fetchone() is not None
    if not exists:
        bot.send_message(message.chat.id, "❌ This user is not registered with the bot. Ask them to press /start first.")
        return
    msg = bot.send_message(message.chat.id, f"User ID: {target_id}\nNow send the number of referral credits to add (positive whole number):")
    bot.register_next_step_handler(msg, lambda m: process_referral_credit_amount(m, target_id))


def process_referral_credit_amount(message, target_id):
    if message.from_user.id != ADMIN_ID:
        return
    raw_amount = (message.text or "").strip()
    if not raw_amount.isdigit() or int(raw_amount) < 1:
        bot.send_message(message.chat.id, "❌ Enter a positive whole number. No credits were added.")
        return
    amount = int(raw_amount)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reason = f"Admin-added referral credits (+{amount}); manually credited by admin"
    try:
        with db_lock:
            cursor.execute(
                "UPDATE users SET balance = balance + ?, total_invites = total_invites + ? WHERE user_id = ?",
                (amount, amount, target_id)
            )
            if cursor.rowcount != 1:
                conn.rollback()
                bot.send_message(message.chat.id, "❌ User not found. No credits were added.")
                return
            cursor.execute(
                "INSERT INTO history (user_id, amount, reason, date) VALUES (?, ?, ?, ?)",
                (target_id, amount, reason, now)
            )
            conn.commit()
        bot.send_message(message.chat.id, f"✅ Added {amount} referral credits to user {target_id}.")
        try:
            bot.send_message(
                target_id,
                f"🎉 Referral credits added!\n\n⭐ Credits added by admin: +{amount}\n💰 Your balance has been updated.\n📜 Check Invite History for details."
            )
        except Exception:
            pass
    except Exception as exc:
        with db_lock:
            conn.rollback()
        bot.send_message(message.chat.id, f"❌ Could not add credits: {exc}")

def process_add_channel(message):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Please send a public channel username or t.me link.")
        return
    raw = message.text.strip()
    username = None

    if raw.startswith("@"):
        username = raw
    else:
        candidate = raw if raw.startswith(("https://", "http://")) else "https://" + raw
        parsed = urlparse(candidate)
        host = (parsed.netloc or "").lower().removeprefix("www.")
        path_parts = [part for part in parsed.path.split("/") if part]
        if host in ("t.me", "telegram.me") and path_parts and not path_parts[0].startswith(("+", "joinchat")):
            name = path_parts[0].split("?")[0]
            if name.replace("_", "").isalnum():
                username = "@" + name

    if not username:
        bot.send_message(message.chat.id, "❌ Invalid format. Send a public channel username (e.g. @mychannel) or https://t.me/mychannel.")
        return

    try:
        chat = bot.get_chat(username)
        if chat.type != "channel" or not getattr(chat, "username", None):
            bot.send_message(message.chat.id, "❌ Please provide a PUBLIC channel with a username.")
            return
        username = "@" + chat.username
        link = f"https://t.me/{chat.username}"
        # Bot must be able to check membership; Telegram normally requires bot admin rights for reliable checks.
        bot.get_chat_member(chat.id, bot.get_me().id)
        with db_lock:
            cursor.execute("INSERT OR REPLACE INTO channels (channel_id, link) VALUES (?, ?)", (str(chat.id), link))
            conn.commit()
        bot.send_message(message.chat.id, f"✅ Successfully added/updated public channel!\n\n📢 Channel: {username}\n🔗 Link: {link}\n\n⚠️ Every user, including admin, must join this channel to use bot commands.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Bot cannot verify this channel. Make sure it is public and add the bot as an administrator, then try again.\n\nError: {e}")


def process_load_database_file(message):
    """Validate an admin-uploaded SQLite database, back up current data, then load it."""
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    document = getattr(message, "document", None)
    if not document:
        bot.send_message(message.chat.id, "❌ Please send the database as a document file (.db, .sqlite, or .sqlite3).")
        return

    filename = (document.file_name or "").lower()
    if not filename.endswith((".db", ".sqlite", ".sqlite3")):
        bot.send_message(message.chat.id, "❌ Unsupported file type. Send a .db, .sqlite, or .sqlite3 file.")
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        bot.send_message(message.chat.id, "❌ Database file is too large. Telegram Bot API downloads support files up to 20 MB here.")
        return

    temp_path = None
    source_conn = None
    try:
        file_info = bot.get_file(document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        fd, temp_path = tempfile.mkstemp(prefix="uploaded_database_", suffix=".db", dir=os.path.dirname(DB_PATH))
        with os.fdopen(fd, "wb") as output:
            output.write(file_bytes)

        source_conn = sqlite3.connect(temp_path)
        source_conn.execute("PRAGMA query_only = ON")
        integrity = source_conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("SQLite integrity check failed; file may be damaged.")

        required_columns = {
            "users": {"user_id", "balance", "total_invites", "total_withdrawals"},
            "history": {"id", "user_id", "amount", "reason", "date"},
            "channels": {"channel_id", "link"},
        }
        tables = {
            row[0] for row in source_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, needed in required_columns.items():
            if table not in tables:
                raise ValueError(f"Required table '{table}' is missing.")
            columns = {
                row[1] for row in source_conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing = needed - columns
            if missing:
                raise ValueError(f"Table '{table}' is missing columns: {', '.join(sorted(missing))}.")

        current_backup = backup_database("before_load")
        if not current_backup:
            raise RuntimeError("Could not back up the current database, so no data was replaced.")

        # Copy uploaded DB contents into the active connection; the bot continues using this DB.
        with db_lock:
            conn.commit()
            source_conn.backup(conn)
            conn.commit()

        bot.send_message(
            message.chat.id,
            "✅ Database loaded successfully. The bot is now using the uploaded database, not the previous data.\n\n"
            f"🛡 Previous database backup: {os.path.basename(current_backup)}\n"
            "Automatic backups will continue every 2 hours."
        )
    except Exception as exc:
        print(f"Database load failed: {exc}")
        bot.send_message(message.chat.id, f"❌ Database was not loaded. Current data was kept if loading had not started.\nReason: {exc}")
    finally:
        if source_conn is not None:
            try:
                source_conn.close()
            except Exception:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

def process_broadcast(message):
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    
    success = 0
    bot.send_message(message.chat.id, f"Starting broadcast to {len(users)} users...")
    
    for (uid,) in users:
        try:
            bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            time.sleep(0.05) 
        except:
            pass
            
    bot.send_message(message.chat.id, f"✅ Broadcast finished. Sent to {success}/{len(users)} users.")


# Catch-all for any other text messages to ensure Force Sub is respected
@bot.message_handler(func=lambda message: True)
def block_unsubbed_users(message):
    if not require_force_sub(message.chat.id, message.from_user.id):
        return

# ================= Start Polling =================
if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_NEW_BOT_TOKEN_HERE":
    raise SystemExit("Set BOT_TOKEN to your NEW token from @BotFather before running the bot.")

# Send the first backup after startup, then repeat every two hours.
threading.Thread(target=automatic_backup_loop, name="database-backup", daemon=True).start()

print(f"Bot is running as @{BOT_USERNAME}...")
bot.infinity_polling(skip_pending=True)