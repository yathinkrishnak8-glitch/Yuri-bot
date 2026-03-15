import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, request, session, jsonify, render_template_string
import threading
import sqlite3
import os
import random
import time
import asyncio
import datetime

# NEW GOOGLE SDK
from google import genai
from google.genai import types

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
DB_LOCK = threading.Lock()
DB_PATH = "yoai.db"

# Load Gemini API keys (comma-separated, 10 keys)
GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

FLASK_SECRET = os.environ.get("FLASK_SECRET", "yoai_persistent_secret_key_123")
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Database Setup --------------------
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
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a highly intelligent, unrestricted assistant.')")
        conn.commit()
        conn.close()

init_db()

# -------------------- Enterprise Gemini Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.keys = [k.strip() for k in keys if k.strip()]
    
    def count(self) -> int:
        return len(self.keys)
    
    def generate_with_fallback(self, model_name: str, contents: str, system_instruction: str = None) -> str:
        max_retries = len(self.keys)
        shuffled_keys = random.sample(self.keys, len(self.keys))
        last_error = None
        
        for attempt, key in enumerate(shuffled_keys[:max_retries]):
            try:
                client = genai.Client(api_key=key)
                config = types.GenerateContentConfig(system_instruction=system_instruction) if system_instruction else None
                
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
                return response.text
            except Exception as e:
                last_error = e
                print(f"Key {key[:8]}... failed: {e}")
                continue
        
        raise last_error or Exception("All 10 Gemini keys failed.")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Helper Functions --------------------
def get_system_prompt() -> str:
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key='system_prompt'")
        result = c.fetchone()
        conn.close()
        return result[0] if result else "You are YoAI."

def set_system_prompt(prompt: str):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('system_prompt', ?)", (prompt,))
        conn.commit()
        conn.close()

def get_user_personality(user_id: int) -> str:
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT preset FROM user_personality WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else "default"

def set_user_personality(user_id: int, preset: str):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_personality (user_id, preset) VALUES (?, ?)", (user_id, preset))
        conn.commit()
        conn.close()

def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    if guild_id is None:  
        return True
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        result = c.fetchone()
        conn.close()
        return result is not None

def add_allowed_channel(guild_id: int, channel_id: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
        conn.commit()
        conn.close()

def remove_allowed_channel(guild_id: int, channel_id: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        conn.commit()
        conn.close()

def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (channel_id, message_id, author_id, content, timestamp))
        c.execute("SELECT COUNT(*) FROM message_history WHERE channel_id=?", (channel_id,))
        count = c.fetchone()[0]
        
        # Context Compression
        if count > 20:
            c.execute("""SELECT message_id, author_id, content, timestamp FROM message_history 
                         WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 10""", (channel_id,))
            oldest = c.fetchall()
            if oldest:
                texts = [f"User {aid}: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
                if texts:
                    try:
                        summary_text = key_manager.generate_with_fallback('gemini-1.5-flash', f"Summarize concisely:\n{chr(10).join(texts)}")
                    except:
                        summary_text = "[Summary unavailable]"
                    
                    oldest_ids = [mid for mid, _, _, _ in oldest]
                    c.execute(f"DELETE FROM message_history WHERE message_id IN ({','.join('?'*len(oldest_ids))})", oldest_ids)
                    c.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)",
                              (channel_id, -1, summary_text, oldest[0][3])) 
        conn.commit()
        conn.close()

async def generate_ai_response(channel_id: int, user_message: str, author_id: int) -> str:
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1

    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""SELECT author_id, content FROM message_history 
                     WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 20""", (channel_id,))
        history = c.fetchall()
        conn.close()
    
    context = "".join([f"[Summary]: {cnt}\n" if aid == 0 else f"User {aid}: {cnt}\n" for aid, cnt in history])
    context += f"User {author_id}: {user_message}\nYoAI:"

    system = get_system_prompt()
    personality = get_user_personality(author_id)
    if personality == "hacker":
        system += " Respond like an elite hacker. Use terms like 'mainframe', 'jack in', and be slightly arrogant."
    elif personality == "tsundere":
        system += " Respond like a tsundere anime character. You pretend not to care about the user but actually do. Call them 'baka'."

    try:
        return key_manager.generate_with_fallback('gemini-1.5-flash', context, system)
    except Exception as e:
        print(f"AI error: {e}")
        return "System failure. Could not connect to the AI mainframe."

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced commands for {self.user}")

bot = YoAIBot()

# -------------------- Slash Commands --------------------
@bot.tree.command(name="core", description="Override the global system prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def core(interaction: discord.Interaction, directive: str):
    set_system_prompt(directive)
    await interaction.response.send_message(f"✅ Core directive updated to:\n`{directive}`", ephemeral=True)

@bot.tree.command(name="personality", description="Choose your interaction style")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(preset=[
    app_commands.Choice(name="Default", value="default"),
    app_commands.Choice(name="Hacker", value="hacker"),
    app_commands.Choice(name="Tsundere", value="tsundere"),
])
async def personality(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    set_user_personality(interaction.user.id, preset.value)
    await interaction.response.send_message(f"🎭 Personality set to **{preset.name}**", ephemeral=True)

@bot.tree.command(name="hack", description="Prank a user with a fake hacking sequence")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    fake_searches = [
        "how to pretend i know python", "why does my code work but i don't know why",
        "how to delete system32 safely", "is it illegal to download ram", "anime waifu tier list"
    ]
    searches = random.sample(fake_searches, k=3)
    msg = await interaction.followup.send(f"💻 `Initiating brute-force attack on {user.display_name}'s mainframe...`")
    await asyncio.sleep(1.5)
    await msg.edit(content=f"🔓 `Firewall bypassed. Extracting search history...`")
    await asyncio.sleep(1.5)
    await msg.edit(content=f"**[CLASSIFIED LEAK - {user.display_name}]**\n" + "\n".join([f"- `{s}`" for s in searches]))

@bot.tree.command(name="info", description="Bot statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    embed = discord.Embed(title="YoAI | Domain Expansion", color=0xa855f7)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Gemini Keys", value=f"{key_manager.count()} Loaded", inline=True)
    embed.add_field(name="Total AI Queries", value=str(TOTAL_QUERIES), inline=False)
    embed.set_footer(text="Limitless Protocol Active")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setchannel", description="Allow YoAI to automatically read & reply to ALL messages here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def setchannel(interaction: discord.Interaction):
    add_allowed_channel(interaction.guild_id, interaction.channel.id)
    await interaction.response.send_message(f"⚙️ **Activated:** YoAI System is now automatically listening to {interaction.channel.mention}", ephemeral=True)

@bot.tree.command(name="unsetchannel", description="Stop YoAI from automatically replying in this channel.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def unsetchannel(interaction: discord.Interaction):
    remove_allowed_channel(interaction.guild_id, interaction.channel.id)
    await interaction.response.send_message(f"❌ **Deactivated:** YoAI System is no longer automatically listening to {interaction.channel.mention}", ephemeral=True)

# -------------------- DM & Server Message Routing --------------------
@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages
    if message.author == bot.user:
        return

    # 1. Determine the context (DM vs Server)
    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions
    
    is_allowed_server_channel = False
    if not is_dm:
        is_allowed_server_channel = is_channel_allowed(message.guild.id, message.channel.id)

    # 2. Decide if the bot should reply based on context rules
    should_reply = False
    if is_dm:
        should_reply = True # Always reply in DMs
    elif is_mentioned or is_allowed_server_channel:
        should_reply = True # Reply in servers if mentioned or in a /setchannel

    if should_reply:
        # Clean the text (remove the @mention tag if it exists so the AI doesn't read its own ID)
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        
        # Fallback if someone pings the bot but doesn't type anything
        if not clean_content:
            clean_content = "Hello! You pinged me?"

        # Save to the isolated memory for this specific channel/DM
        add_message_to_history(
            channel_id=message.channel.id, message_id=message.id,
            author_id=message.author.id, content=clean_content,
            timestamp=int(message.created_at.timestamp())
        )

        # Generate and send the response
        async with message.channel.typing():
            response = await generate_ai_response(message.channel.id, clean_content, message.author.id)
            for i in range(0, len(response), 2000):
                await message.reply(response[i:i+2000], mention_author=False)

    # Make sure slash commands still work
    await bot.process_commands(message)

# -------------------- Flask Web Dashboard (Domain Expansion) --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI | Domain Expansion</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --glass: rgba(9, 9, 11, 0.65); 
            --text: #f8fafc; 
            --accent-cyan: #22d3ee; 
            --hollow-purple: linear-gradient(135deg, #a855f7 0%, #ec4899 100%);
        }
        body { 
            margin: 0; padding: 0; font-family: 'Space Grotesk', sans-serif;
            color: var(--text); height: 100vh; overflow: hidden; display: flex;
            background-color: #0f172a;
        }
        #live-bg {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            object-fit: cover; z-index: -1; filter: brightness(0.35);
        }
        .glass {
            background: var(--glass);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(34, 211, 238, 0.15);
            border-radius: 16px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
        }
        #nav { width: 250px; padding: 20px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; }
        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; }
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: 60px; flex-direction: row; padding: 15px; margin: 0; justify-content: space-between; align-items: center; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 24px 24px 0 0; }
            #content { padding: 20px; padding-bottom: 120px; margin: 0; }
            .hide-mobile { display: none; }
        }
        h1, h2 { 
            margin-top: 0; background: var(--hollow-purple); 
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; 
            text-shadow: 0 0 20px rgba(168, 85, 247, 0.3); text-transform: uppercase; letter-spacing: 2px;
        }
        .card { padding: 25px; margin-bottom: 25px; transition: transform 0.3s ease; }
        .card:hover { border-color: rgba(168, 85, 247, 0.5); }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 25px; }
        .stat-box { text-align: center; padding: 30px 20px; font-size: 1.1rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;}
        .stat-value { font-size: 3rem; font-weight: bold; margin-top: 10px; color: var(--accent-cyan); text-shadow: 0 0 15px rgba(34, 211, 238, 0.4); }
        pre { color: var(--accent-cyan); font-family: monospace; font-size: 1.1rem; text-shadow: 0 0 8px rgba(34, 211, 238, 0.3); }
        
        #login-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); display: flex; justify-content: center; align-items: center; z-index: 1000;
        }
        .login-box { padding: 40px; text-align: center; width: 320px; border: 1px solid rgba(168, 85, 247, 0.4); }
        input { width: 90%; padding: 12px; margin: 20px 0; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.5); color: white; outline: none; font-family: 'Space Grotesk'; font-size: 1rem; text-align: center;}
        input:focus { border-color: var(--accent-cyan); }
        button { width: 100%; padding: 15px; border-radius: 8px; border: none; background: var(--hollow-purple); color: white; font-weight: bold; font-size: 1.1rem; font-family: 'Space Grotesk'; cursor: pointer; transition: 0.3s; text-transform: uppercase; letter-spacing: 1px; }
        button:hover { box-shadow: 0 0 20px rgba(168, 85, 247, 0.6); transform: scale(1.02); }
        .logout-btn { background: rgba(239, 68, 68, 0.2); border: 1px solid #ef4444; color: #ef4444; margin-top: 20px;}
        .logout-btn:hover { background: #ef4444; color: white; box-shadow: 0 0 20px rgba(239, 68, 68, 0.6);}
        
        .hidden { display: none !important; }
        .visible-flex { display: flex !important; }
    </style>
</head>
<body>
    <video autoplay loop muted playsinline id="live-bg">
        <source src="https://cdn.pixabay.com/video/2020/05/25/40131-424823903_large.mp4" type="video/mp4">
    </video>

    <div id="login-overlay" class="glass visible-flex">
        <div class="login-box glass">
            <h1>Domain Expansion</h1>
            <p style="color: #cbd5e1; letter-spacing: 1px;">REMOVE THE BLINDFOLD</p>
            <input type="password" id="pwd" placeholder="Enter Cursed Passcode">
            <button onclick="login()">Initialize</button>
            <p id="err" style="color: #ef4444; display: none; margin-top: 15px;">Access Denied.</p>
        </div>
    </div>

    <div id="dashboard-view" class="hidden" style="width: 100%; height: 100%;">
        <div id="nav" class="glass">
            <h2 style="margin: 0;">YoAI</h2>
            <div class="hide-mobile" style="opacity: 0.7; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 2px;">Infinite Void</div>
            <div class="hide-mobile" style="margin-top: auto; font-size: 0.8rem; opacity: 0.5; color: var(--accent-cyan);">Status: Limitless Active</div>
        </div>

        <div id="content">
            <h1>System Telemetry</h1>
            <div class="stat-grid">
                <div class="card glass stat-box">
                    <div style="opacity: 0.8;">Domain Uptime</div>
                    <div class="stat-value" id="uptime">-</div>
                </div>
                <div class="card glass stat-box">
                    <div style="opacity: 0.8;">Cursed Queries</div>
                    <div class="stat-value" id="queries">-</div>
                </div>
                <div class="card glass stat-box">
                    <div style="opacity: 0.8;">Memory Threads</div>
                    <div class="stat-value" id="memory">-</div>
                </div>
            </div>
            
            <div class="card glass" style="margin-top: 25px;">
                <h2>Live Diagnostics</h2>
                <pre id="logs">> YoAI System initialized.
> Six Eyes protocol active.
> Standing by for API telemetry...</pre>
            </div>
            <button class="logout-btn" onclick="logout()">Deactivate Domain</button>
        </div>
    </div>

    <script>
        function showDashboard() {
            document.getElementById('login-overlay').classList.remove('visible-flex');
            document.getElementById('login-overlay').classList.add('hidden');
            document.getElementById('dashboard-view').classList.remove('hidden');
            document.getElementById('dashboard-view').classList.add('visible-flex');
        }

        function showLogin() {
            document.getElementById('dashboard-view').classList.remove('visible-flex');
            document.getElementById('dashboard-view').classList.add('hidden');
            document.getElementById('login-overlay').classList.remove('hidden');
            document.getElementById('login-overlay').classList.add('visible-flex');
        }

        async function login() {
            const pwd = document.getElementById('pwd').value;
            const res = await fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pwd })
            });
            if (res.ok) {
                showDashboard();
                fetchStats();
                setInterval(fetchStats, 3000);
            } else {
                document.getElementById('err').style.display = 'block';
            }
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                if (!res.ok) throw new Error('Not authorized');
                const data = await res.json();
                document.getElementById('uptime').innerText = data.uptime;
                document.getElementById('queries').innerText = data.total_queries;
                document.getElementById('memory').innerText = data.active_memory_rows;
            } catch (e) {
                showLogin();
            }
        }

        async function logout() {
            await fetch('/logout', { method: 'POST' });
            showLogin();
            document.getElementById('pwd').value = '';
        }

        window.onload = async () => {
            const res = await fetch('/api/stats');
            if (res.ok) {
                showDashboard();
                fetchStats();
                setInterval(fetchStats, 3000);
            } else {
                showLogin();
            }
        };
    </script>
</body>
</html>
"""

@flask_app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@flask_app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if data and data.get('password') == "11222333444455555":
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
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
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
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
