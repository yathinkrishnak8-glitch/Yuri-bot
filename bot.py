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
import re

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
        c.execute("CREATE TABLE IF NOT EXISTS allowed_channels (guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id))")
        c.execute("""CREATE TABLE IF NOT EXISTS message_history (
            channel_id INTEGER, message_id INTEGER PRIMARY KEY, author_id INTEGER,
            content TEXT, timestamp INTEGER
        )""")
        # Base Configs
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a highly intelligent assistant.')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('current_model', 'gemini-2.5-flash-lite')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('global_personality', 'default')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('status_type', 'watching')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('status_text', 'over the Matrix')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('response_delay', '0')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('engine_status', 'online')")
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

# -------------------- Smart Cluster Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.all_keys = [k.strip() for k in keys if k.strip()]
        self.key_cooldowns = {k: 0.0 for k in self.all_keys}
        self.dead_keys = set()
        self.lock = threading.Lock()
        
        self.unrestricted_safety = [
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
        ]
    
    def get_stats(self) -> dict:
        with self.lock:
            now = time.time()
            total = len(self.all_keys)
            dead = len(self.dead_keys)
            cooldown = sum(1 for k in self.all_keys if k not in self.dead_keys and self.key_cooldowns[k] > now)
            active = total - dead - cooldown
            return {"total": total, "active": active, "cooldown": cooldown, "dead": dead}
            
    def run_diagnostics(self) -> list:
        results = []
        for key in self.all_keys:
            masked_key = f"{key[:8]}••••••••{key[-4:]}"
            try:
                client = genai.Client(api_key=key)
                client.models.generate_content(model='gemini-2.5-flash-lite', contents="ping")
                with self.lock:
                    if key in self.dead_keys: self.dead_keys.remove(key)
                    self.key_cooldowns[key] = 0.0
                results.append({"key": masked_key, "status": "ONLINE", "detail": "Healthy", "color": "#10b981"})
            except Exception as e:
                error_msg = str(e).lower()
                with self.lock:
                    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                        self.key_cooldowns[key] = time.time() + 60.0
                        results.append({"key": masked_key, "status": "COOLDOWN", "detail": "Rate Limit", "color": "#f59e0b"})
                    else:
                        self.dead_keys.add(key)
                        results.append({"key": masked_key, "status": "DEAD", "detail": "Invalid", "color": "#ef4444"})
        return results

    def generate_with_fallback(self, target_model: str, contents: list, system_instruction: str = None) -> str:
        fallback_models = [target_model, 'gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-2.5-pro']
        models_to_try = list(dict.fromkeys(fallback_models)) 
        last_error = None
        for model_name in models_to_try:
            with self.lock:
                now = time.time()
                available_keys = [k for k in self.all_keys if k not in self.dead_keys and self.key_cooldowns[k] <= now]
            if not available_keys: continue 
            random.shuffle(available_keys)
            for key in available_keys:
                try:
                    client = genai.Client(api_key=key)
                    config = types.GenerateContentConfig(system_instruction=system_instruction if system_instruction else None, safety_settings=self.unrestricted_safety)
                    response = client.models.generate_content(model=model_name, contents=contents, config=config)
                    return response.text
                except Exception as e:
                    last_error = e
                    with self.lock:
                        if "429" in str(e) or "quota" in str(e).lower(): self.key_cooldowns[key] = time.time() + 60.0
                        elif "400" in str(e) or "403" in str(e): self.dead_keys.add(key)
                    continue
        raise last_error or Exception("Cascade failure.")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Helper Functions --------------------
def get_global_personality() -> str: return get_config('global_personality', 'default')
def set_global_personality(preset: str): set_config('global_personality', preset)

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
        if enable: c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
        else: c.execute("DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        conn.commit()
        conn.close()

def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)", (channel_id, message_id, author_id, content, timestamp))
        c.execute("SELECT COUNT(*) FROM message_history WHERE channel_id=?", (channel_id,))
        count = c.fetchone()[0]
        if count > 20:
            c.execute("SELECT message_id, author_id, content, timestamp FROM message_history WHERE channel_id=? ORDER BY timestamp ASC LIMIT 10", (channel_id,))
            oldest = c.fetchall()
            if oldest:
                texts = [f"User {aid}: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
                summary_text = key_manager.generate_with_fallback('gemini-2.5-flash-lite', [f"Summarize:\n{chr(10).join(texts)}"]) if texts else "[Summary unavailable]"
                c.execute(f"DELETE FROM message_history WHERE message_id IN ({','.join('?'*len(oldest))})", [m[0] for m in oldest])
                c.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)", (channel_id, -1, summary_text, oldest[0][3])) 
        conn.commit()
        conn.close()

def clean_discord_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c.isspace()).strip()
    return cleaned if cleaned else "User"

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

class YoAIBot(commands.Bot):
    def __init__(self): super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self): await self.tree.sync()

bot = YoAIBot()

@tasks.loop(seconds=60)
async def status_loop():
    s_type = get_config('status_type', 'watching')
    s_text = get_config('status_text', 'over the Matrix')
    a_map = {'playing': discord.ActivityType.playing, 'listening': discord.ActivityType.listening, 'competing': discord.ActivityType.competing, 'streaming': discord.ActivityType.streaming}
    a_type = a_map.get(s_type, discord.ActivityType.watching)
    engine_status = get_config('engine_status', 'online')
    if engine_status == 'offline': await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="[OFFLINE]"), status=discord.Status.dnd)
    else: await bot.change_presence(activity=discord.Activity(type=a_type, name=s_text), status=discord.Status.online)

@tasks.loop(hours=24)
async def optimize_db():
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        seven_days_ago = int(time.time()) - 604800
        conn.execute("DELETE FROM message_history WHERE timestamp < ?", (seven_days_ago,))
        conn.commit(); conn.close()
        conn_vac = sqlite3.connect(DB_PATH, isolation_level=None)
        conn_vac.execute("VACUUM"); conn_vac.close()

@bot.event
async def on_ready():
    if not status_loop.is_running(): status_loop.start()
    if not optimize_db.is_running(): optimize_db.start()

async def generate_ai_response(channel: discord.abc.Messageable, user_message: str, author: discord.User, image_parts: list = None) -> str:
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1
    guild = getattr(channel, 'guild', None)
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT author_id, content FROM message_history WHERE channel_id=? ORDER BY timestamp ASC LIMIT 20", (channel.id,))
        history = c.fetchall(); conn.close()
    context_str = "[SYSTEM: History]\n"
    for aid, cnt in history:
        u_obj = guild.get_member(aid) if guild else bot.get_user(aid)
        name = clean_discord_name(u_obj.display_name) if u_obj else f"User_{aid}"
        context_str += f"{name}: {cnt}\n"
    current_name = clean_discord_name(author.display_name)
    context_str += f"\nReply to {current_name}: {user_message}"
    system = get_config('system_prompt', 'You are YoAI.')
    personality = get_global_personality()
    if personality != "default": system += f"\n\n[PERSONALITY]: {personality}"
    system += "\n\nDIRECTIVE: Respond naturally. No chat logs."
    target_model = get_config('current_model', 'gemini-2.5-flash-lite')
    return key_manager.generate_with_fallback(target_model, [context_str] + (image_parts or []), system)

# -------------------- Slash Commands --------------------
@bot.tree.command(name="toggle", description="Toggle Engine ON/OFF.")
async def toggle_cmd(interaction: discord.Interaction):
    s = 'offline' if get_config('engine_status', 'online') == 'online' else 'online'
    set_config('engine_status', s)
    await interaction.response.send_message(f"✅ Status updated to: {s.upper()}")
    await status_loop()

@bot.tree.command(name="time", description="Set delay (seconds).")
async def time_cmd(interaction: discord.Interaction, seconds: int):
    set_config('response_delay', str(max(0, seconds)))
    await interaction.response.send_message(f"⏱️ Delay set to {seconds}s.")

@bot.tree.command(name="model", description="Switch AI Block.")
@app_commands.choices(model_name=[
    app_commands.Choice(name="Gemini 2.5 Flash Lite (Token Saver)", value="gemini-2.5-flash-lite"),
    app_commands.Choice(name="Gemini 2.5 Flash (Balanced)", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Pro (Highly Intelligent)", value="gemini-2.5-pro"),
    app_commands.Choice(name="Gemini 2.0 Flash", value="gemini-2.0-flash"),
    app_commands.Choice(name="Gemini 2.0 Pro Experimental", value="gemini-2.0-pro-exp")
])
async def model_cmd(interaction: discord.Interaction, model_name: app_commands.Choice[str]):
    set_config('current_model', model_name.value)
    await interaction.response.send_message(f"🧠 Powered by `{model_name.name}`.")

@bot.tree.command(name="personality", description="Set GLOBAL persona.")
async def personality(interaction: discord.Interaction, prompt: str):
    set_global_personality(prompt.strip())
    await interaction.response.send_message(f"🌍 Persona updated.")

@bot.tree.command(name="clear", description="Wipe memory.")
async def clear_cmd(interaction: discord.Interaction):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM message_history WHERE channel_id=?", (interaction.channel_id,)); conn.commit(); conn.close()
    await interaction.response.send_message("🧹 Memory Wiped.")

@bot.tree.command(name="memory", description="Analyze memory.")
async def memory_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send("🧠 Analysis complete.")

@bot.tree.command(name="info", description="Stats.")
async def info(interaction: discord.Interaction):
    stats = key_manager.get_stats()
    await interaction.response.send_message(f"🏎️ Engine Online. Cluster: {stats['active']}/{stats['total']}")

@bot.tree.command(name="setchannel", description="Auto-reply ON.")
async def setchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, True)
    await interaction.response.send_message("⚙️ Activated.")

@bot.tree.command(name="unsetchannel", description="Auto-reply OFF.")
async def unsetchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, False)
    await interaction.response.send_message("❌ Deactivated.")

# -------------------- Messaging --------------------
@bot.event
async def on_message(message: discord.Message):
    if not bot.user or message.author.bot or get_config('engine_status', 'online') == 'offline': return
    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions or f'<@{bot.user.id}>' in message.content
    if is_dm or is_mentioned or is_channel_allowed(getattr(message.guild, 'id', 0), message.channel.id):
        clean = message.content.replace(f'<@{bot.user.id}>', '').strip()
        add_message_to_history(message.channel.id, message.id, message.author.id, clean or "[Image]", int(message.created_at.timestamp()))
        image_parts = []
        for att in message.attachments:
            if att.content_type and att.content_type.startswith('image/'):
                img_bytes = await att.read(); image_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=att.content_type))
        try:
            delay = float(get_config('response_delay', '0'))
            async with message.channel.typing():
                if delay > 0: await asyncio.sleep(delay)
                response = await generate_ai_response(message.channel, clean, message.author, image_parts)
                for i in range(0, len(response), 2000): await message.reply(response[i:i+2000], mention_author=False)
        except: pass

# -------------------- Flask Web Dashboard (SATAN BLACK UI RESTORED) --------------------
flask_app = Flask(__name__); flask_app.secret_key = FLASK_SECRET
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI | Apex Command</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg-deep: #000; --glass: rgba(10, 10, 10, 0.5); --glass-border: rgba(255, 255, 255, 0.05); --text-main: #f3f4f6; --accent: #ff2a2a; --accent-glow: rgba(255, 42, 42, 0.4); --danger: #ef4444; --success: #10b981; }
        body { margin: 0; font-family: 'Space Grotesk', sans-serif; color: var(--text-main); height: 100vh; overflow: hidden; display: flex; background-color: var(--bg-deep); }
        #live-bg { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2; background: linear-gradient(120deg, #000, #0a0000, #050000, #140000); background-size: 300% 300%; animation: liquidFlow 15s ease-in-out infinite; }
        .orb { position: fixed; border-radius: 50%; filter: blur(90px); z-index: -1; animation: float 20s infinite ease-in-out alternate; }
        .orb-1 { width: 50vw; height: 50vw; background: rgba(255, 42, 42, 0.08); top: -10%; left: -10%; }
        .orb-2 { width: 60vw; height: 60vw; background: rgba(150, 0, 0, 0.06); bottom: -20%; right: -10%; }
        @keyframes liquidFlow { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        @keyframes float { 0% { transform: translate(0, 0) scale(1); } 100% { transform: translate(5vw, 10vh) scale(1.2); } }
        .glass { background: var(--glass); backdrop-filter: blur(30px); border: 1px solid var(--glass-border); border-radius: 8px; box-shadow: 0 15px 50px rgba(0,0,0,0.9); }
        .accent-text { color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 2px; text-shadow: 0 0 15px var(--accent-glow); }
        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; border-left: 4px solid var(--accent); }
        .nav-tab { padding: 12px 15px; border-radius: 4px; cursor: pointer; transition: 0.3s; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; font-size: 0.9rem; border: 1px solid transparent; }
        .nav-tab:hover { background: rgba(255,255,255,0.03); }
        .nav-tab.active { background: rgba(255, 42, 42, 0.1); border-color: rgba(255, 42, 42, 0.3); color: #fff; }
        #content { flex-grow: 1; padding: 40px; overflow-y: auto; z-index: 10; }
        @media (max-width: 768px) { body { flex-direction: column; } #nav { width: auto; flex-direction: row; margin: 0; border-left: none; border-bottom: 1px solid var(--glass-border); } #content { padding: 20px; } .hide-mobile { display: none; } }
        .card { padding: 25px; margin-bottom: 25px; }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-value { font-size: 3rem; font-weight: 300; margin-top: 10px; color: #fff; }
        label { display: block; margin-bottom: 8px; font-size: 0.9rem; text-transform: uppercase; opacity: 0.7; }
        input, textarea, select { width: 100%; padding: 14px; margin-bottom: 20px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.05); background: rgba(0,0,0,0.8); color: white; outline: none; font-family: 'Space Grotesk'; }
        button { padding: 15px 25px; border-radius: 4px; border: 1px solid rgba(255,42,42,0.3); background: rgba(255,42,42,0.1); color: #fff; font-weight: 700; text-transform: uppercase; cursor: pointer; transition: 0.3s; }
        button:hover { background: var(--accent); box-shadow: 0 0 25px var(--accent-glow); }
        pre { color: #a1a1aa; font-family: monospace; font-size: 0.95rem; line-height: 1.5; }
        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.95); display: flex; justify-content: center; align-items: center; z-index: 1000; }
        .hidden { display: none !important; }
        .key-row { display: flex; justify-content: space-between; padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.02); }
    </style>
</head>
<body>
    <div id="live-bg"></div><div class="orb orb-1"></div><div class="orb orb-2"></div>
    <div id="login-overlay" class="glass"><div class="glass" style="padding:50px; text-align:center;"><h1 class="accent-text">Ignition</h1><input type="password" id="pwd" placeholder="Code"><button onclick="login()">Engage</button></div></div>
    <div id="dashboard-view" class="hidden" style="width:100%; display:flex; height:100vh;">
        <div id="nav" class="glass">
            <h2 class="accent-text">YoAI</h2>
            <div class="nav-tab active" id="tab-telemetry" onclick="switchTab('telemetry')">Telemetry</div>
            <div class="nav-tab" id="tab-diagnostics" onclick="switchTab('diagnostics')">Cluster Health</div>
            <div class="nav-tab" id="tab-customization" onclick="switchTab('customization')">Customization</div>
            <div class="nav-tab" id="tab-admin" onclick="switchTab('admin')">Admin Panel</div>
        </div>
        <div id="content">
            <div id="section-telemetry" class="visible">
                <h1 class="accent-text">System Telemetry</h1>
                <div class="stat-grid">
                    <div class="card glass stat-box"><div class="stat-value" id="uptime">-</div><label>Engine Uptime</label></div>
                    <div class="card glass stat-box"><div class="stat-value" id="queries">-</div><label>Data Output</label></div>
                    <div class="card glass stat-box"><div class="stat-value" id="db-size">-</div><label>DB Weight (KB)</label></div>
                </div>
                <div class="card glass" style="margin-top:20px;">
                    <h2 style="font-size:1.2rem;">Live Terminal</h2>
                    <pre id="logs">[SYS] Apex Engine Initialized.&#10;[SYS] Safety Overrides Online.&#10;[SYS] Awaiting signals...</pre>
                </div>
            </div>
            <div id="section-diagnostics" class="hidden">
                <h1 class="accent-text">Cluster Diagnostics</h1>
                <div class="card glass">
                    <button id="diag-btn" onclick="runDiagnostics()">Initiate Deep Scan</button>
                    <div id="diag-results" style="margin-top:20px;"></div>
                </div>
            </div>
            <div id="section-customization" class="hidden">
                <h1 class="accent-text">Customization</h1>
                <div class="card glass">
                    <label>Activity Type</label><select id="cust-status-type"><option value="playing">Playing</option><option value="watching">Watching</option><option value="listening">Listening</option><option value="streaming">Streaming</option></select>
                    <label>Description</label><input type="text" id="cust-status-text">
                    <button onclick="saveCustom()">Sync Update</button>
                </div>
            </div>
            <div id="section-admin" class="hidden">
                <h1 class="accent-text">Admin Control Panel</h1>
                <div class="card glass">
                    <label>Global Prompt</label><textarea id="admin-prompt" rows="4"></textarea>
                    <label>Engine Block</label>
                    <select id="admin-model">
                        <option value="gemini-2.5-flash-lite">Gemini 2.5 Flash Lite</option>
                        <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
                        <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
                        <option value="gemini-2.0-flash">Gemini 2.0 Flash</option>
                        <option value="gemini-2.0-pro-exp">Gemini 2.0 Pro Experimental</option>
                    </select>
                    <button onclick="saveAdmin()">Deploy Config</button>
                </div>
                <button class="glass" style="margin-top:20px; color:var(--danger); border-color:var(--danger);" onclick="nuke()">Incinerate Memory</button>
            </div>
        </div>
    </div>
    <script>
        async function login(){ if(document.getElementById('pwd').value === 'mr_yaen'){ document.getElementById('login-overlay').className='hidden'; document.getElementById('dashboard-view').style.display='flex'; fetchStats(); setInterval(fetchStats, 3000); fetchConfig(); } }
        function switchTab(t){ ['telemetry','diagnostics','customization', 'admin'].forEach(s => { document.getElementById('section-'+s).className='hidden'; document.getElementById('tab-'+s).classList.remove('active'); }); document.getElementById('section-'+t).className='visible'; document.getElementById('tab-'+t).classList.add('active'); }
        async function fetchStats(){ const r = await fetch('/api/stats'); const d = await r.json(); document.getElementById('uptime').innerText=d.uptime; document.getElementById('queries').innerText=d.total_queries; document.getElementById('db-size').innerText=d.db_size; }
        async function fetchConfig(){ const r = await fetch('/api/config'); const d = await r.json(); document.getElementById('admin-prompt').value=d.system_prompt; document.getElementById('admin-model').value=d.current_model; document.getElementById('cust-status-type').value=d.status_type; document.getElementById('cust-status-text').value=d.status_text; }
        async function runDiagnostics(){ const b = document.getElementById('diag-btn'); b.disabled=true; const r = await fetch('/api/diagnostics',{method:'POST'}); const d = await r.json(); let h=''; d.results.forEach(n=>{ h+=`<div class='key-row'><span>Node: ${n.key}</span><span style='color:${n.color}'>${n.status}</span></div>`; }); document.getElementById('diag-results').innerHTML=h; b.disabled=false; }
        async function saveCustom(){ const p = {status_type:document.getElementById('cust-status-type').value, status_text:document.getElementById('cust-status-text').value}; await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}); }
        async function saveAdmin(){ const p = {system_prompt:document.getElementById('admin-prompt').value, current_model:document.getElementById('admin-model').value}; await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}); }
        async function nuke(){ if(confirm('Incinerate all history?')){ await fetch('/api/nuke',{method:'POST'}); } }
    </script>
</body>
</html>
"""

@flask_app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)
@flask_app.route('/login', methods=['POST'])
def login(): return jsonify(success=True)
@flask_app.route('/api/stats')
def api_stats():
    u = str(datetime.timedelta(seconds=int(time.time()-START_TIME))).split(".")[0]
    try: s = round(os.path.getsize(DB_PATH)/1024, 1)
    except: s = 0
    return jsonify({"uptime":u, "total_queries":TOTAL_QUERIES, "db_size": s})
@flask_app.route('/api/diagnostics', methods=['POST'])
def api_diag(): return jsonify(success=True, results=key_manager.run_diagnostics())
@flask_app.route('/api/config', methods=['GET','POST'])
def api_conf():
    if request.method=='GET': return jsonify({"system_prompt":get_config('system_prompt',''), "current_model":get_config('current_model',''), "status_type":get_config('status_type',''), "status_text":get_config('status_text','')})
    d = request.get_json()
    for k in d: set_config(k, d[k])
    return jsonify(success=True)
@flask_app.route('/api/nuke', methods=['POST'])
def api_nuke():
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM message_history"); conn.commit(); conn.close()
    return jsonify(success=True)

if __name__ == "__main__":
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False), daemon=True).start()
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
