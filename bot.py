import os
import asyncio
import sqlite3
import logging
from datetime import datetime, date, timedelta

import discord
from discord import app_commands
from discord.ext import tasks
import requests
import pytz

# ------------- CONFIG -------------

TOKEN = "YOUR_BOT_TOKEN_HERE"

# Default values for you (can be changed with /set_location)
DEFAULT_CITY = "Abu Dhabi"
DEFAULT_COUNTRY = "UAE"
DEFAULT_TIMEZONE = "Asia/Dubai"

# ------------- LOGGING -------------

logging.basicConfig(level=logging.INFO)

# ------------- DATABASE -------------

conn = sqlite3.connect("prayer_bot.db")
cursor = conn.cursor()

# Users table
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    city TEXT,
    country TEXT,
    timezone TEXT
)
""")

# Qada table
cursor.execute("""
CREATE TABLE IF NOT EXISTS qada (
    user_id TEXT,
    prayer TEXT,
    remaining INTEGER,
    PRIMARY KEY (user_id, prayer)
)
""")

# Daily prayers table
cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_prayers (
    user_id TEXT,
    date TEXT,
    prayer TEXT,
    status TEXT,
    message_id TEXT,
    reacted INTEGER,
    PRIMARY KEY (user_id, date, prayer)
)
""")

# Qada log table
cursor.execute("""
CREATE TABLE IF NOT EXISTS qada_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    date TEXT,
    prayer TEXT,
    change INTEGER,
    reason TEXT
)
""")

conn.commit()

PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

# ------------- DISCORD CLIENT -------------

intents = discord.Intents.default()
intents.message_content = False
intents.reactions = True
intents.members = True

class PrayerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.prayer_times = {}  # {user_id: {prayer: "HH:MM"}}

    async def setup_hook(self):
        await self.tree.sync()
        update_prayer_times.start()
        prayer_reminder_loop.start()

bot = PrayerBot()

# ------------- HELPER FUNCTIONS -------------

def get_user_settings(user_id: int):
    cursor.execute("SELECT city, country, timezone FROM users WHERE user_id = ?", (str(user_id),))
    row = cursor.fetchone()
    if row:
        city, country, tz = row
        return {
            "city": city or DEFAULT_CITY,
            "country": country or DEFAULT_COUNTRY,
            "timezone": tz or DEFAULT_TIMEZONE
        }
    else:
        # Insert default
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, city, country, timezone) VALUES (?, ?, ?, ?)",
            (str(user_id), DEFAULT_CITY, DEFAULT_COUNTRY, DEFAULT_TIMEZONE)
        )
        conn.commit()
        return {
            "city": DEFAULT_CITY,
            "country": DEFAULT_COUNTRY,
            "timezone": DEFAULT_TIMEZONE
        }

def init_qada_for_user(user_id: int):
    cursor.execute("SELECT COUNT(*) FROM qada WHERE user_id = ?", (str(user_id),))
    count = cursor.fetchone()[0]
    if count == 0:
        # Your starting numbers
        initial = {
            "Fajr": 2288,
            "Dhuhr": 2288,
            "Asr": 2288,
            "Maghrib": 2288,
            "Isha": 2287
        }
        for prayer, remaining in initial.items():
            cursor.execute(
                "INSERT INTO qada (user_id, prayer, remaining) VALUES (?, ?, ?)",
                (str(user_id), prayer, remaining)
            )
        conn.commit()

def get_qada_for_user(user_id: int):
    init_qada_for_user(user_id)
    cursor.execute("SELECT prayer, remaining FROM qada WHERE user_id = ?", (str(user_id),))
    return dict(cursor.fetchall())

def set_qada_for_user(user_id: int, prayer: str, remaining: int):
    cursor.execute(
        "UPDATE qada SET remaining = ? WHERE user_id = ? AND prayer = ?",
        (remaining, str(user_id), prayer)
    )
    conn.commit()

def log_qada_change(user_id: int, prayer: str, change: int, reason: str):
    cursor.execute(
        "INSERT INTO qada_log (user_id, date, prayer, change, reason) VALUES (?, ?, ?, ?, ?)",
        (str(user_id), date.today().isoformat(), prayer, change, reason)
    )
    conn.commit()

def get_prayer_times_for_user(user_id: int):
    settings = get_user_settings(user_id)
    city = settings["city"]
    country = settings["country"]

    url = "https://api.aladhan.com/v1/timingsByCity"
    params = {
        "city": city,
        "country": country,
        "method": 2
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        timings = data["data"]["timings"]
        # Clean to HH:MM
        result = {
            "Fajr": timings["Fajr"].split(" ")[0],
            "Dhuhr": timings["Dhuhr"].split(" ")[0],
            "Asr": timings["Asr"].split(" ")[0],
            "Maghrib": timings["Maghrib"].split(" ")[0],
            "Isha": timings["Isha"].split(" ")[0],
        }
        return result
    except Exception as e:
        logging.error(f"Error fetching prayer times: {e}")
        return None

def get_tz_for_user(user_id: int):
    settings = get_user_settings(user_id)
    return pytz.timezone(settings["timezone"])

async def ensure_daily_prayers(user_id: int):
    today_str = date.today().isoformat()
    cursor.execute(
        "SELECT COUNT(*) FROM daily_prayers WHERE user_id = ? AND date = ?",
        (str(user_id), today_str)
    )
    count = cursor.fetchone()[0]
    if count == 0:
        for p in PRAYERS:
            cursor.execute("""
                INSERT INTO daily_prayers (user_id, date, prayer, status, message_id, reacted)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (str(user_id), today_str, p, "pending", None, 0))
        conn.commit()

def get_previous_prayer(prayer: str):
    idx = PRAYERS.index(prayer)
    if idx == 0:
        return None
    return PRAYERS[idx - 1]

# ------------- TASKS -------------

@tasks.loop(hours=24)
async def update_prayer_times():
    await bot.wait_until_ready()
    logging.info("Updating prayer times for all known users...")
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    for uid in users:
        times = get_prayer_times_for_user(int(uid))
        if times:
            bot.prayer_times[uid] = times
            logging.info(f"Updated prayer times for {uid}: {times}")

@update_prayer_times.before_loop
async def before_update_prayer_times():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def prayer_reminder_loop():
    await bot.wait_until_ready()
    now = datetime.utcnow()

    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]

    for uid in users:
        user_id = int(uid)
        tz = get_tz_for_user(user_id)
        local_now = now.astimezone(tz)
        current_time_str = local_now.strftime("%H:%M")

        # Ensure we have prayer times
        if uid not in bot.prayer_times:
            times = get_prayer_times_for_user(user_id)
            if times:
                bot.prayer_times[uid] = times

        times = bot.prayer_times.get(uid)
        if not times:
            continue

        # Check if it's time for any prayer
        for prayer, t in times.items():
            if t == current_time_str:
                # Before sending this prayer reminder, handle missed previous prayer
                prev = get_previous_prayer(prayer)
                if prev:
                    await handle_auto_missed_prayer(user_id, prev)

                await send_prayer_reminder(user_id, prayer)

async def handle_auto_missed_prayer(user_id: int, prayer: str):
    today_str = date.today().isoformat()
    cursor.execute("""
        SELECT status, reacted FROM daily_prayers
        WHERE user_id = ? AND date = ? AND prayer = ?
    """, (str(user_id), today_str, prayer))
    row = cursor.fetchone()
    if not row:
        return
    status, reacted = row
    if status == "pending" and reacted == 0:
        # Mark as missed and add qada
        cursor.execute("""
            UPDATE daily_prayers
            SET status = ?, reacted = ?
            WHERE user_id = ? AND date = ? AND prayer = ?
        """, ("missed", 1, str(user_id), today_str, prayer))
        conn.commit()

        qada = get_qada_for_user(user_id)
        remaining = qada.get(prayer, 0) + 1
        set_qada_for_user(user_id, prayer, remaining)
        log_qada_change(user_id, prayer, +1, "missed_prayer_auto")

        user = await bot.fetch_user(user_id)
        try:
            await user.send(f"⏳ You did not respond for **{prayer}** before the next prayer.\n"
                            f"It has been marked as **missed** and **+1 qada** added.")
        except:
            pass

async def send_prayer_reminder(user_id: int, prayer: str):
    await ensure_daily_prayers(user_id)
    today_str = date.today().isoformat()

    user = await bot.fetch_user(user_id)
    try:
        msg = await user.send(f"🕌 **{prayer} time!** Did you pray?")
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

        cursor.execute("""
            UPDATE daily_prayers
            SET message_id = ?
            WHERE user_id = ? AND date = ? AND prayer = ?
        """, (str(msg.id), str(user_id), today_str, prayer))
        conn.commit()
    except Exception as e:
        logging.error(f"Failed to DM user {user_id}: {e}")

# ------------- REACTION HANDLING -------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    # Only care about DMs
    if payload.guild_id is not None:
        return

    user_id = payload.user_id
    emoji = str(payload.emoji)

    # Find which prayer this message belongs to
    cursor.execute("""
        SELECT date, prayer, status, reacted FROM daily_prayers
        WHERE user_id = ? AND message_id = ?
    """, (str(user_id), str(payload.message_id)))
    row = cursor.fetchone()
    if not row:
        return

    d, prayer, status, reacted = row
    if reacted == 1:
        return  # already handled

    user = await bot.fetch_user(user_id)
    today_str = date.today().isoformat()

    if emoji == "👍":
        cursor.execute("""
            UPDATE daily_prayers
            SET status = ?, reacted = ?
            WHERE user_id = ? AND date = ? AND prayer = ?
        """, ("prayed", 1, str(user_id), d, prayer))
        conn.commit()
        try:
            await user.send(f"✅ Marked **{prayer}** as **prayed**. May Allah accept it.")
        except:
            pass

    elif emoji == "👎":
        cursor.execute("""
            UPDATE daily_prayers
            SET status = ?, reacted = ?
            WHERE user_id = ? AND date = ? AND prayer = ?
        """, ("missed", 1, str(user_id), d, prayer))
        conn.commit()

        qada = get_qada_for_user(user_id)
        remaining = qada.get(prayer, 0) + 1
        set_qada_for_user(user_id, prayer, remaining)
        log_qada_change(user_id, prayer, +1, "missed_prayer_manual")

        try:
            await user.send(f"❌ Marked **{prayer}** as **missed** and **+1 qada** added.")
        except:
            pass

# ------------- VIEWS (CONFIRMATION) -------------

class QadaConfirmView(discord.ui.View):
    def __init__(self, user_id: int, prayer: str, amount: int, old_remaining: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.prayer = prayer
        self.amount = amount
        self.old_remaining = old_remaining
        self.value = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return

        new_remaining = self.old_remaining - self.amount
        set_qada_for_user(self.user_id, self.prayer, new_remaining)
        log_qada_change(self.user_id, self.prayer, -self.amount, "manual_done")

        await interaction.response.edit_message(
            content=f"✅ Qada updated: **{self.prayer}** {self.old_remaining} → {new_remaining}",
            view=None
        )
        self.value = True
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="❎ Cancelled. No changes made.",
            view=None
        )
        self.value = False
        self.stop()

# ------------- SLASH COMMANDS -------------

@bot.tree.command(name="set_location", description="Set your city and country for prayer times.")
@app_commands.describe(city="Your city", country="Your country")
async def set_location(interaction: discord.Interaction, city: str, country: str):
    user_id = interaction.user.id
    cursor.execute("""
        INSERT INTO users (user_id, city, country, timezone)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET city=excluded.city, country=excluded.country
    """, (str(user_id), city, country, DEFAULT_TIMEZONE))
    conn.commit()

    await interaction.response.send_message(
        f"📍 Location updated to **{city}, {country}**.\n"
        f"Timezone is currently set to **{DEFAULT_TIMEZONE}**.",
        ephemeral=True
    )

@bot.tree.command(name="qada_remaining", description="Show your remaining qada counts.")
async def qada_remaining(interaction: discord.Interaction):
    user_id = interaction.user.id
    qada = get_qada_for_user(user_id)

    lines = ["📦 **Qada Remaining:**"]
    for p in PRAYERS:
        lines.append(f"- {p}: {qada.get(p, 0)}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="qada_done", description="Log completed qada (strict + confirmation).")
@app_commands.describe(
    prayer="Which prayer's qada you completed",
    amount="How many qada you completed"
)
@app_commands.choices(prayer=[
    app_commands.Choice(name="Fajr", value="Fajr"),
    app_commands.Choice(name="Dhuhr", value="Dhuhr"),
    app_commands.Choice(name="Asr", value="Asr"),
    app_commands.Choice(name="Maghrib", value="Maghrib"),
    app_commands.Choice(name="Isha", value="Isha"),
])
async def qada_done(interaction: discord.Interaction, prayer: app_commands.Choice[str], amount: int):
    user_id = interaction.user.id
    if amount <= 0:
        await interaction.response.send_message("Amount must be greater than 0.", ephemeral=True)
        return

    qada = get_qada_for_user(user_id)
    old_remaining = qada.get(prayer.value, 0)

    if amount > old_remaining:
        await interaction.response.send_message(
            f"❌ You only have **{old_remaining} {prayer.value}** qada remaining. "
            f"You cannot subtract **{amount}**.",
            ephemeral=True
        )
        return

    # Send confirmation in DM
    user = interaction.user
    await interaction.response.send_message(
        "Check your DMs for confirmation.", ephemeral=True
    )

    try:
        view = QadaConfirmView(user_id, prayer.value, amount, old_remaining)
        await user.send(
            f"⚠️ You are about to subtract **{amount} {prayer.value}** qada "
            f"({old_remaining} → {old_remaining - amount}). Confirm?",
            view=view
        )
    except Exception as e:
        logging.error(f"Failed to DM user {user_id} for qada confirmation: {e}")
        await interaction.followup.send("I couldn't DM you. Please enable DMs from this bot.", ephemeral=True)

@bot.tree.command(name="today", description="Show today's prayer status.")
async def today(interaction: discord.Interaction):
    user_id = interaction.user.id
    await ensure_daily_prayers(user_id)
    today_str = date.today().isoformat()

    cursor.execute("""
        SELECT prayer, status FROM daily_prayers
        WHERE user_id = ? AND date = ?
        ORDER BY CASE prayer
            WHEN 'Fajr' THEN 1
            WHEN 'Dhuhr' THEN 2
            WHEN 'Asr' THEN 3
            WHEN 'Maghrib' THEN 4
            WHEN 'Isha' THEN 5
        END
    """, (str(user_id), today_str))
    rows = cursor.fetchall()

    lines = [f"📅 **Today's prayers ({today_str}):**"]
    for prayer, status in rows:
        if status == "pending":
            emoji = "🟡"
        elif status == "prayed":
            emoji = "🟢"
        elif status == "missed":
            emoji = "🔴"
        else:
            emoji = "⚪"
        lines.append(f"{emoji} {prayer}: {status}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ------------- EVENTS -------------

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logging.info("Bot is ready.")

# ------------- RUN -------------

if __name__ == "__main__":
    bot.run(TOKEN)