import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from flask import Flask, request, session, jsonify, render_template_string
import threading
import sqlite3
import os
import random
import time
import asyncio
import datetime
import json
import secrets
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
DB_LOCK = threading.Lock()
DB_PATH = "yoai.db"
executor = ThreadPoolExecutor(max_workers=4)  # For async DB ops

# Load Gemini API keys from environment variable (comma-separated)
GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

# Flask secret key (random per run, but can be overridden via env for production)
FLASK_SECRET = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# Render provides PORT env var
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Token Retrieval --------------------
def get_discord_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("Discord token not found. Please set DISCORD_BOT_TOKEN or DISCORD_TOKEN.")
    return token

# -------------------- Async Database Helpers --------------------
async def run_db(query: str, *args, fetch_one=False, fetch_all=False, commit=False):
    """Run database operations in a thread to avoid blocking."""
    def _sync_db():
        with DB_LOCK:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute(query, args)
            if commit:
                conn.commit()
            if fetch_one:
                result = c.fetchone()
            elif fetch_all:
                result = c.fetchall()
            else:
                result = None
            conn.close()
            return result
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _sync_db)

# -------------------- Database Setup (sync for startup) --------------------
def init_db():
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS user_personality (user_id INTEGER PRIMARY KEY, preset TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS allowed_channels (guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id))")
        c.execute("""CREATE TABLE IF NOT EXISTS message_history (
            channel_id INTEGER, message_id INTEGER PRIMARY KEY, author_id INTEGER,
            content TEXT, timestamp INTEGER
        )""")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a helpful AI assistant.')")
        conn.commit()
        conn.close()
init_db()

# -------------------- Gemini Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
    
    def count(self) -> int:
        return len(self.keys)
    
    def generate_with_fallback(self, model_name: str, contents: Any, system_instruction: Optional[str] = None, max_retries: Optional[int] = None) -> str:
        if max_retries is None:
            max_retries = len(self.keys)
        shuffled_keys = random.sample(self.keys, len(self.keys))
        last_error = None
        for attempt, key in enumerate(shuffled_keys[:max_retries]):
            try:
                genai.configure(api_key=key)
                model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
                response = model.generate_content(contents)
                return response.text
            except Exception as e:
                last_error = e
                print(f"Key {key[:8]}... failed (attempt {attempt+1}/{max_retries}): {e}")
                continue
        raise last_error or Exception("All Gemini keys failed")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Helper Functions (Async) --------------------
async def get_system_prompt() -> str:
    result = await run_db("SELECT value FROM config WHERE key='system_prompt'", fetch_one=True)
    return result[0] if result else "You are YoAI, a helpful AI assistant."

async def set_system_prompt(prompt: str):
    await run_db("INSERT OR REPLACE INTO config (key, value) VALUES ('system_prompt', ?)", prompt, commit=True)

async def get_user_personality(user_id: int) -> str:
    result = await run_db("SELECT preset FROM user_personality WHERE user_id=?", user_id, fetch_one=True)
    return result[0] if result else "default"

async def set_user_personality(user_id: int, preset: str):
    await run_db("INSERT OR REPLACE INTO user_personality (user_id, preset) VALUES (?, ?)", user_id, preset, commit=True)

async def is_channel_allowed(guild_id: Optional[int], channel_id: int) -> bool:
    if guild_id is None:
        return True
    result = await run_db("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", guild_id, channel_id, fetch_one=True)
    return result is not None

async def add_allowed_channel(guild_id: int, channel_id: int):
    await run_db("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", guild_id, channel_id, commit=True)

async def remove_allowed_channel(guild_id: int, channel_id: int):
    await run_db("DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?", guild_id, channel_id, commit=True)

async def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    # Insert new message
    await run_db("INSERT OR REPLACE INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                 channel_id, message_id, author_id, content, timestamp, commit=True)
    # Count and compress if needed
    count_row = await run_db("SELECT COUNT(*) FROM message_history WHERE channel_id=?", channel_id, fetch_one=True)
    count = count_row[0]
    if count > 20:
        # Fetch oldest 10
        oldest = await run_db("""SELECT message_id, author_id, content, timestamp FROM message_history 
                                  WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 10""",
                              channel_id, fetch_all=True)
        if oldest:
            texts = [f"User {aid}: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
            if texts:
                summary_text = await asyncio.to_thread(summarize_with_gemini_sync, "\n".join(texts))
                oldest_ids = [mid for mid, _, _, _ in oldest]
                placeholders = ','.join('?' * len(oldest_ids))
                await run_db(f"DELETE FROM message_history WHERE message_id IN ({placeholders})", *oldest_ids, commit=True)
                summary_timestamp = oldest[0][3]
                await run_db("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)",
                             channel_id, -1, summary_text, summary_timestamp, commit=True)

async def get_channel_history(channel_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    rows = await run_db("""SELECT author_id, content, timestamp FROM message_history 
                           WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT ?""",
                        channel_id, limit, fetch_all=True)
    return [{"author_id": row[0], "content": row[1], "timestamp": row[2]} for row in rows]

def summarize_with_gemini_sync(text: str) -> str:
    try:
        return key_manager.generate_with_fallback(
            model_name='gemini-1.5-flash',
            contents=f"Summarize the following conversation concisely, preserving key points:\n{text}",
            max_retries=key_manager.count()
        )
    except Exception as e:
        print(f"Summarization error: {e}")
        return "[Summary unavailable]"

async def generate_ai_response(channel_id: int, user_message: str, author_id: int) -> str:
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1
    history = await get_channel_history(channel_id, limit=20)
    context = ""
    for msg in history:
        if msg["author_id"] == 0:
            context += f"[Summary]: {msg['content']}\n"
        else:
            context += f"User {msg['author_id']}: {msg['content']}\n"
    context += f"User {author_id}: {user_message}\nYoAI:"
    system = await get_system_prompt()
    personality = await get_user_personality(author_id)
    if personality == "hacker":
        system += " Respond like a hacker, using leetspeak and tech jargon."
    elif personality == "tsundere":
        system += " Respond like a tsundere anime character, with a mix of harshness and hidden kindness."
    try:
        response_text = await asyncio.to_thread(
            key_manager.generate_with_fallback,
            'gemini-1.5-flash',
            context,
            system,
            key_manager.count()
        )
        return response_text
    except Exception as e:
        print(f"AI generation error: {e}")
        return "I'm having trouble thinking right now. Please try again later."

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        # Sync commands globally
        await self.tree.sync()
        print(f"Synced commands for {self.user}")

bot = YoAIBot()

# -------------------- Global Error Handler for Slash Commands --------------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Try to respond if not already responded
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
    except:
        pass
    print(f"Command error: {error}")

# -------------------- Slash Commands --------------------
@bot.tree.command(name="core", description="Override the global system prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def core(interaction: discord.Interaction, directive: str):
    try:
        await set_system_prompt(directive)
        await interaction.response.send_message(f"System prompt updated to:\n{directive}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to update prompt: {e}", ephemeral=True)

@bot.tree.command(name="personality", description="Choose your interaction style")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(preset=[
    app_commands.Choice(name="Default", value="default"),
    app_commands.Choice(name="Hacker", value="hacker"),
    app_commands.Choice(name="Tsundere", value="tsundere"),
])
async def personality(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    try:
        await set_user_personality(interaction.user.id, preset.value)
        await interaction.response.send_message(f"Personality set to **{preset.name}**", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to set personality: {e}", ephemeral=True)

@bot.tree.command(name="hack", description="Prank a user with a fake hacking sequence")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=False)  # defer publicly
    try:
        fake_searches = [
            "how to become a meme lord", "why is my cat ignoring me", "secret discord admin powers",
            "what does 'sus' really mean", "how to fake being productive", "is water wet?",
            "how to train your dragon irl", "anime waifu tier list", "how to hack (jk)"
        ]
        searches = random.sample(fake_searches, k=3)
        msg = await interaction.followup.send(f"`Initiating hack on {user.display_name}...`")
        await asyncio.sleep(1)
        await msg.edit(content=f"`Bypassing firewalls... [█░░░░░░░░░] 10%`")
        await asyncio.sleep(1)
        await msg.edit(content=f"`Cracking passwords... [███░░░░░░░] 30%`")
        await asyncio.sleep(1)
        await msg.edit(content=f"`Accessing search history... [██████░░░░] 60%`")
        await asyncio.sleep(1)
        await msg.edit(content=f"`Downloading data... [█████████░] 90%`")
        await asyncio.sleep(1)
        await msg.edit(content=f"`Hack complete! Leaked search history for {user.display_name}:`\n" +
                       "\n".join([f"- {s}" for s in searches]))
    except Exception as e:
        await interaction.followup.send(f"Hack failed: {e}", ephemeral=True)

@bot.tree.command(name="info", description="Bot statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    try:
        uptime_seconds = int(time.time() - START_TIME)
        uptime_str = str(datetime.timedelta(seconds=uptime_seconds))
        # Get memory rows
        rows_result = await run_db("SELECT COUNT(*) FROM message_history", fetch_one=True)
        rows = rows_result[0] if rows_result else 0
        embed = discord.Embed(title="YoAI System Info", color=0x00ff00)
        embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Uptime", value=uptime_str, inline=True)
        embed.add_field(name="Active Gemini Keys", value=key_manager.count(), inline=True)
        embed.add_field(name="Total Queries", value=TOTAL_QUERIES, inline=True)
        embed.add_field(name="Memory Rows", value=rows, inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"Info command failed: {e}", ephemeral=True)

@bot.tree.command(name="setchannel", description="Allow/Disallow bot auto-reply in this channel (Admin only)")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def setchannel(interaction: discord.Interaction, enabled: bool):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in servers.", ephemeral=True)
        return
    try:
        channel = interaction.channel
        if enabled:
            await add_allowed_channel(interaction.guild_id, channel.id)
            await interaction.response.send_message(f"✅ YoAI will now auto-reply in {channel.mention}", ephemeral=True)
        else:
            await remove_allowed_channel(interaction.guild_id, channel.id)
            await interaction.response.send_message(f"❌ YoAI will no longer auto-reply in {channel.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to update channel: {e}", ephemeral=True)

# -------------------- Message Handling --------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    # Store in history
    await add_message_to_history(
        channel_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        content=message.content,
        timestamp=int(message.created_at.timestamp())
    )
    # Check if should reply
    should_reply = False
    if message.guild is None:
        should_reply = True
    else:
        if await is_channel_allowed(message.guild.id, message.channel.id):
            should_reply = True
    if should_reply:
        async with message.channel.typing():
            response = await generate_ai_response(message.channel.id, message.content, message.author.id)
            await message.reply(response)
    await bot.process_commands(message)

# -------------------- Flask Web Dashboard (same HTML as before) --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

# (Insert the same HTML_TEMPLATE with anime theme here - unchanged)
HTML_TEMPLATE = """<!DOCTYPE html> ... (keep the previous HTML) ... """

@flask_app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@flask_app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if data and data.get('password') == "mr_yaen":
        session['logged_in'] = True
        return jsonify(success=True)
    return jsonify(success=False), 401

@flask_app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    return jsonify(success=True)

@flask_app.route('/api/stats')
def api_stats():
    if not session.get('logged_in'):
        return jsonify(error="Unauthorized"), 401
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM message_history")
        rows = c.fetchone()[0]
        conn.close()
    return jsonify({
        "uptime": uptime_str,
        "total_queries": TOTAL_QUERIES,
        "active_memory_rows": rows
    })

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# -------------------- Main --------------------
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(get_discord_token())