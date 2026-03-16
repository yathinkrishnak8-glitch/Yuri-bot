import discord
from discord import app_commands
from discord.ext import commands, tasks
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
        # Default settings
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a highly intelligent assistant.')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('current_model', 'gemini-2.5-flash')")
        conn.commit()
        conn.close()

init_db()

def get_config(key: str, default: str) -> str:
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key=?", (key,))
        res = c.fetchone()
        conn.close()
        return res[0] if res else default

def set_config(key: str, value: str):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

# -------------------- Multi-Tier Fallback Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.keys = [k.strip() for k in keys if k.strip()]
    
    def count(self) -> int:
        return len(self.keys)
    
    def generate_with_fallback(self, target_model: str, contents: list, system_instruction: str = None) -> str:
        fallback_models = [target_model, 'gemini-2.5-flash', 'gemini-2.5-pro']
        models_to_try = list(dict.fromkeys(fallback_models)) 
        
        shuffled_keys = random.sample(self.keys, len(self.keys))
        last_error = None
        
        for model_name in models_to_try:
            for key in shuffled_keys[:3]:
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
                    print(f"⚠️ [Model: {model_name}] [Key: {key[:8]}...] Failed: {e}", flush=True)
                    continue
                    
        raise last_error or Exception("Total cascade failure. All models and keys failed.")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Helper Functions --------------------
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
    if guild_id is None: return True
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        result = c.fetchone()
        conn.close()
        return result is not None

def toggle_channel(guild_id: int, channel_id: int, enable: bool):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        if enable:
            c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
        else:
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
        
        if count > 20:
            c.execute("""SELECT message_id, author_id, content, timestamp FROM message_history 
                         WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 10""", (channel_id,))
            oldest = c.fetchall()
            if oldest:
                texts = [f"User ID {aid}: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
                if texts:
                    try:
                        summary_text = key_manager.generate_with_fallback('gemini-2.5-flash', [f"Summarize concisely:\n{chr(10).join(texts)}"])
                    except:
                        summary_text = "[Summary unavailable]"
                    
                    oldest_ids = [mid for mid, _, _, _ in oldest]
                    c.execute(f"DELETE FROM message_history WHERE message_id IN ({','.join('?'*len(oldest_ids))})", oldest_ids)
                    c.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)",
                              (channel_id, -1, summary_text, oldest[0][3])) 
        conn.commit()
        conn.close()

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True # Ensure we can fetch Display Names

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced commands for {self.user}")

bot = YoAIBot()

@tasks.loop(minutes=10)
async def status_loop():
    statuses = [
        discord.Activity(type=discord.ActivityType.watching, name="over the Matrix"),
        discord.Activity(type=discord.ActivityType.listening, name="the Rain"),
        discord.Activity(type=discord.ActivityType.playing, name="with Gemini 2.5 Flash")
    ]
    await bot.change_presence(activity=random.choice(statuses))

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not status_loop.is_running():
        status_loop.start()

# -------------------- The AI Generator --------------------
async def generate_ai_response(channel_id: int, user_message: str, author: discord.User, image_parts: list = None) -> str:
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1

    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""SELECT author_id, content FROM message_history 
                     WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 20""", (channel_id,))
        history = c.fetchall()
        conn.close()
    
    context_str = ""
    for aid, cnt in history:
        if aid == 0:
            context_str += f"[Summary]: {cnt}\n"
        else:
            # FIXED: Grabs the actual Display Name of the user instead of their raw ID number!
            user_obj = bot.get_user(aid)
            name = user_obj.display_name if user_obj else "Unknown User"
            context_str += f"{name}: {cnt}\n"
            
    context_str += f"{author.display_name}: {user_message}\nYoAI:"

    # Compile the final payload (Text + Images if any)
    payload = [context_str]
    if image_parts:
        payload.extend(image_parts)

    system = get_config('system_prompt', 'You are YoAI.')
    target_model = get_config('current_model', 'gemini-2.5-flash')
    
    personality = get_user_personality(author.id)
    if personality == "hacker":
        system += " Respond like an elite hacker. Use terms like 'mainframe', 'jack in', and be slightly arrogant."
    elif personality == "tsundere":
        system += " Respond like a tsundere anime character. You pretend not to care about the user but actually do. Call them 'baka'."

    return key_manager.generate_with_fallback(target_model, payload, system)

# -------------------- Slash Commands --------------------
@bot.tree.command(name="model", description="Change the primary Gemini AI model.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(model_name=[
    app_commands.Choice(name="Gemini 2.5 Flash (Fast & Efficient)", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Pro (Highly Intelligent)", value="gemini-2.5-pro")
])
async def model_cmd(interaction: discord.Interaction, model_name: app_commands.Choice[str]):
    set_config('current_model', model_name.value)
    await interaction.response.send_message(f"🧠 **Model Switched:** YoAI is now powered by `{model_name.name}`.\n*(If this model crashes, the system will automatically fall back to backups).*")

@bot.tree.command(name="core", description="Override the global system prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def core(interaction: discord.Interaction, directive: str):
    set_config('system_prompt', directive)
    await interaction.response.send_message(f"✅ Core directive updated to:\n`{directive}`", ephemeral=True)

@bot.tree.command(name="personality", description="Choose your interaction style")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(preset=[
    app_commands.Choice(name="Default (Normal AI)", value="default"),
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

@bot.tree.command(name="clear", description="Wipe YoAI's memory for the current channel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def clear_cmd(interaction: discord.Interaction):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM message_history WHERE channel_id=?", (interaction.channel_id,))
        conn.commit()
        conn.close()
    await interaction.response.send_message("🧹 **Memory Wiped.** I have forgotten all recent context in this channel.")

@bot.tree.command(name="info", description="Bot statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    current_model = get_config('current_model', 'gemini-2.5-flash')
    
    embed = discord.Embed(title="✨ YoAI | Seinen Interface", color=0x4285f4)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Engine", value=f"`{current_model}`", inline=True)
    embed.add_field(name="Active API Keys", value=f"{key_manager.count()} Loaded", inline=True)
    embed.add_field(name="Total AI Queries", value=str(TOTAL_QUERIES), inline=True)
    embed.set_footer(text="Multi-Tier Fallback Matrix Active")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setchannel", description="Allow YoAI to automatically read & reply to ALL messages here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def setchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, True)
    await interaction.response.send_message(f"⚙️ **Activated:** YoAI System is now automatically listening to {interaction.channel.mention}", ephemeral=True)

@bot.tree.command(name="unsetchannel", description="Stop YoAI from automatically replying in this channel.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def unsetchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, False)
    await interaction.response.send_message(f"❌ **Deactivated:** YoAI System is no longer automatically listening to {interaction.channel.mention}", ephemeral=True)

# -------------------- DM & Server Message Routing w/ Vision & Admin Error Handling --------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user: return

    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions
    is_allowed = True if is_dm else is_channel_allowed(message.guild.id, message.channel.id)

    if is_dm or is_mentioned or is_allowed:
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        if not clean_content and not message.attachments: 
            clean_content = "Hello! You pinged me?"

        add_message_to_history(
            channel_id=message.channel.id, message_id=message.id,
            author_id=message.author.id, content=clean_content or "[Sent an Image]",
            timestamp=int(message.created_at.timestamp())
        )

        # VISION: Process Images if the user attached any!
        image_parts = []
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    img_bytes = await att.read()
                    image_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=att.content_type))

        try:
            async with message.channel.typing():
                response = await generate_ai_response(message.channel.id, clean_content, message.author, image_parts)
                for i in range(0, len(response), 2000):
                    await message.reply(response[i:i+2000], mention_author=False)
        except Exception as e:
            # PUBLIC ERROR MESSAGE
            error_public = "There is an error.\nThe issue is sent to master admin yaen. The issue will be fixed soon, wait until yaen beats it up."
            await message.reply(error_public, mention_author=False)
            
            # DM MASTER ADMIN (Fetches the bot owner dynamically)
            try:
                app_info = await bot.application_info()
                admin_user = app_info.owner
                
                error_dm = (
                    f"⚠️ **YoAI System Alert: Critical Failure** ⚠️\n"
                    f"**Triggered By:** {message.author} (`{message.author.id}`)\n"
                    f"**Location:** {message.channel.mention if message.guild else 'DMs'}\n"
                    f"**Error Trace:**\n```\n{str(e)}\n```"
                )
                await admin_user.send(error_dm)
            except Exception as dm_error:
                print(f"Failed to DM admin: {dm_error}")

    await bot.process_commands(message)

# -------------------- Flask Web Dashboard (Seinen / Gemini UI) --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI | System Interface</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --bg-deep: #050505;
            --glass: rgba(15, 15, 20, 0.75);
            --glass-border: rgba(255, 255, 255, 0.08);
            --text-main: #f3f4f6;
            --gemini-grad: linear-gradient(90deg, #4285f4, #9b72cb, #d96570);
            --accent-glow: rgba(155, 114, 203, 0.3);
        }
        body { 
            margin: 0; font-family: 'Space Grotesk', sans-serif; color: var(--text-main); 
            height: 100vh; overflow: hidden; display: flex; background-color: var(--bg-deep);
        }
        #live-bg {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            object-fit: cover; z-index: -1; filter: brightness(0.25) contrast(1.2);
        }
        .glass {
            background: var(--glass);
            backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.8);
        }
        
        .gemini-text {
            background: var(--gemini-grad);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            font-weight: 700; text-transform: uppercase; letter-spacing: 2px;
        }

        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 10px; z-index: 10; margin: 20px; border-top: 3px solid #9b72cb; }
        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; }
        
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: 70px; flex-direction: row; padding: 15px 25px; margin: 0; justify-content: space-between; align-items: center; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 20px 20px 0 0; border-top: 1px solid var(--glass-border); }
            #content { padding: 25px; padding-bottom: 120px; margin: 0; }
            .hide-mobile { display: none; }
        }

        h1, h2 { margin-top: 0; }
        .card { padding: 25px; margin-bottom: 25px; transition: 0.3s; }
        .card:hover { border-color: rgba(155, 114, 203, 0.5); box-shadow: 0 0 25px var(--accent-glow); }
        
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-label { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 2px; opacity: 0.6; }
        .stat-value { font-size: 3.5rem; font-weight: 300; margin-top: 10px; color: #fff; }
        
        pre { color: #a1a1aa; font-family: monospace; font-size: 0.95rem; line-height: 1.5; }
        .highlight { color: #4285f4; font-weight: bold; }

        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(5,5,5,0.9); display: flex; justify-content: center; align-items: center; z-index: 1000; }
        .login-box { padding: 50px; text-align: center; width: 340px; border-top: 3px solid #4285f4; }
        
        input { width: 85%; padding: 14px; margin: 25px 0; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.6); color: white; outline: none; font-family: 'Space Grotesk'; font-size: 1rem; text-align: center; transition: 0.3s;}
        input:focus { border-color: #9b72cb; box-shadow: 0 0 15px var(--accent-glow); }
        
        button { width: 100%; padding: 15px; border-radius: 6px; border: none; background: var(--text-main); color: #000; font-weight: 700; font-size: 1rem; font-family: 'Space Grotesk'; cursor: pointer; transition: 0.3s; text-transform: uppercase; letter-spacing: 1.5px; }
        button:hover { background: #fff; transform: translateY(-2px); box-shadow: 0 5px 20px rgba(255,255,255,0.2); }
        
        .logout-btn { background: transparent; border: 1px solid rgba(255,255,255,0.2); color: #fff; margin-top: 20px; }
        .logout-btn:hover { background: rgba(255,255,255,0.1); color: white; transform: none; box-shadow: none; }
        
        .hidden { display: none !important; }
        .visible-flex { display: flex !important; }
    </style>
</head>
<body>
    <video autoplay loop muted playsinline id="live-bg">
        <source src="https://cdn.pixabay.com/video/2020/03/19/33894-400492193_large.mp4" type="video/mp4">
    </video>

    <div id="login-overlay" class="glass visible-flex">
        <div class="login-box glass">
            <h1 class="gemini-text" style="font-size: 2rem;">✨ YoAI System</h1>
            <p style="font-size: 0.85rem; letter-spacing: 2px; opacity: 0.5; text-transform: uppercase;">Authentication Required</p>
            <input type="password" id="pwd" placeholder="Enter Access Code">
            <button onclick="login()">Enter Matrix</button>
            <p id="err" style="color: #ef4444; display: none; margin-top: 20px; font-size: 0.9rem;">Access Denied.</p>
        </div>
    </div>

    <div id="dashboard-view" class="hidden" style="width: 100%; height: 100%;">
        <div id="nav" class="glass">
            <h2 class="gemini-text" style="margin: 0;">✨ YoAI</h2>
            <div class="hide-mobile" style="opacity: 0.5; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 2px;">Seinen Interface</div>
            <div class="hide-mobile" style="margin-top: auto; font-size: 0.8rem; color: #a1a1aa;">Engine: <span id="model-display" class="highlight">Flash</span></div>
        </div>

        <div id="content">
            <h1 class="gemini-text">System Telemetry</h1>
            <div class="stat-grid">
                <div class="card glass stat-box">
                    <div class="stat-label">Uptime</div>
                    <div class="stat-value" id="uptime">-</div>
                </div>
                <div class="card glass stat-box">
                    <div class="stat-label">Queries</div>
                    <div class="stat-value" id="queries">-</div>
                </div>
                <div class="card glass stat-box">
                    <div class="stat-label">Memory Nodes</div>
                    <div class="stat-value" id="memory">-</div>
                </div>
            </div>
            
            <div class="card glass" style="margin-top: 20px;">
                <h2 style="font-size: 1.2rem; font-weight: 500; border-bottom: 1px solid var(--glass-border); padding-bottom: 15px;">Live Terminal</h2>
                <pre id="logs">
[SYS] Initializing YoAI Seinen Protocol...
[SYS] Gemini API interconnected.
[SYS] <span class="highlight">Multi-Tier Fallback Matrix Active.</span>
[SYS] Vision modules online.
[SYS] Standing by for incoming data streams...
                </pre>
            </div>
            <button class="logout-btn" onclick="logout()">Terminate Session</button>
        </div>
    </div>

    <script>
        function toggleUI(showDash) {
            document.getElementById('login-overlay').className = showDash ? 'glass hidden' : 'glass visible-flex';
            document.getElementById('dashboard-view').className = showDash ? 'visible-flex' : 'hidden';
        }

        async function login() {
            const pwd = document.getElementById('pwd').value;
            const res = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pwd }) });
            if (res.ok) { toggleUI(true); fetchStats(); setInterval(fetchStats, 3000); } 
            else { document.getElementById('err').style.display = 'block'; }
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                if (!res.ok) throw new Error('Unauthorized');
                const data = await res.json();
                document.getElementById('uptime').innerText = data.uptime;
                document.getElementById('queries').innerText = data.total_queries;
                document.getElementById('memory').innerText = data.active_memory_rows;
                document.getElementById('model-display').innerText = data.current_model;
            } catch (e) { toggleUI(false); }
        }

        async function logout() {
            await fetch('/logout', { method: 'POST' });
            toggleUI(false); document.getElementById('pwd').value = '';
        }

        window.onload = async () => {
            const res = await fetch('/api/stats');
            if (res.ok) { toggleUI(true); fetchStats(); setInterval(fetchStats, 3000); } 
            else { toggleUI(false); }
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
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM message_history")
        rows = c.fetchone()[0]
        c.execute("SELECT value FROM config WHERE key='current_model'")
        res = c.fetchone()
        current_model = res[0] if res else "gemini-2.5-flash"
        conn.close()
        
    return jsonify({
        "uptime": uptime_str,
        "total_queries": TOTAL_QUERIES,
        "active_memory_rows": rows,
        "current_model": current_model.replace("gemini-", "").upper()
    })

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
