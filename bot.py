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
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a highly intelligent assistant.')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('current_model', 'gemini-2.5-flash')")
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('global_personality', 'default')")
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
def get_global_personality() -> str:
    return get_config('global_personality', 'default')

def set_global_personality(preset: str):
    set_config('global_personality', preset)

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

# ⚙️ AUTO-OPTIMIZATION LOOP (Runs every 24 hours)
@tasks.loop(hours=24)
async def optimize_db():
    print("[SYS] Initiating Auto-Optimization & Memory Garbage Collection...")
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Prune memories older than 7 days (604800 seconds)
        seven_days_ago = int(time.time()) - 604800
        c.execute("DELETE FROM message_history WHERE timestamp < ?", (seven_days_ago,))
        # SQLite Vacuum defragments the DB to save disk space
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
    print("[SYS] Optimization Complete. Database defragmented.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not status_loop.is_running():
        status_loop.start()
    if not optimize_db.is_running():
        optimize_db.start()

# -------------------- The AI Generator --------------------
async def generate_ai_response(channel: discord.abc.Messageable, user_message: str, author: discord.User, image_parts: list = None) -> str:
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1

    guild = getattr(channel, 'guild', None)
    channel_id = channel.id

    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""SELECT author_id, content FROM message_history 
                     WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 20""", (channel_id,))
        history = c.fetchall()
        conn.close()
    
    context_str = "[SYSTEM: Below is the recent chat history for context]\n"
    for aid, cnt in history:
        if aid == 0:
            context_str += f"[System Summary]: {cnt}\n"
        else:
            user_obj = guild.get_member(aid) if guild else bot.get_user(aid)
            raw_name = user_obj.display_name if user_obj else f"User_{aid}"
            safe_name = clean_discord_name(raw_name)
            context_str += f"{safe_name}: {cnt}\n"
            
    current_safe_name = clean_discord_name(author.display_name)
    
    context_str += "[SYSTEM: End of history.]\n\n"
    context_str += f"Reply directly to {current_safe_name}'s new message: {user_message}"

    payload = [context_str]
    if image_parts:
        payload.extend(image_parts)

    system = get_config('system_prompt', 'You are YoAI.')
    
    personality = get_global_personality()
    if personality != "default":
        system += f"\n\n[GLOBAL PERSONALITY OVERRIDE]: You must strictly follow this persona for ALL users: {personality}"

    system += "\n\nCRITICAL DIRECTIVE: You are participating in a live Discord chat room. Respond DIRECTLY and NATURALLY to the user. DO NOT output a chat transcript. DO NOT write dialogue for other users. DO NOT prefix your response with 'YoAI:'."

    target_model = get_config('current_model', 'gemini-2.5-flash')
    
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
    await interaction.response.send_message(f"🧠 **Model Switched:** YoAI is now powered by `{model_name.name}`.")

@bot.tree.command(name="personality", description="Set a GLOBAL custom personality prompt (or type 'default' to reset)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def personality(interaction: discord.Interaction, prompt: str):
    if prompt.strip().lower() == "default":
        set_global_personality("default")
        await interaction.response.send_message("🌍 **Global Personality Reset:** YoAI has returned to default settings.")
    else:
        set_global_personality(prompt.strip())
        await interaction.response.send_message(f"🌍 **Global Personality Updated:** YoAI will now act like this for everyone:\n`{prompt.strip()}`")

# 🧠 NEW: AI MEMORY TRACKING COMMAND
@bot.tree.command(name="memory", description="Ask YoAI to analyze and summarize what it remembers about this channel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def memory_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    channel_id = interaction.channel_id
    
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT author_id, content FROM message_history WHERE channel_id=? ORDER BY timestamp ASC", (channel_id,))
        history = c.fetchall()
        conn.close()
        
    if not history:
        return await interaction.followup.send("🧠 Memory Bank is currently empty for this sector.")
        
    context_str = ""
    for aid, cnt in history:
        if aid == 0:
            context_str += f"[System Summary]: {cnt}\n"
        else:
            user_obj = guild.get_member(aid) if guild else bot.get_user(aid)
            raw_name = user_obj.display_name if user_obj else f"User_{aid}"
            safe_name = clean_discord_name(raw_name)
            context_str += f"{safe_name}: {cnt}\n"
            
    analysis_prompt = f"Analyze the following chat memory bank. Give a concise, analytical overview of the current topics being discussed, the vibe of the channel, and a quick profile of the active users based on their dialogue:\n\n{context_str}"
    
    try:
        target_model = get_config('current_model', 'gemini-2.5-flash')
        sys_inst = "You are a cybernetic memory archivist. Keep your report structured, analytical, and brief."
        analysis = key_manager.generate_with_fallback(target_model, [analysis_prompt], sys_inst)
        await interaction.followup.send(f"🧠 **Memory Bank Analysis Complete:**\n\n{analysis}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Memory extraction failed: {e}")

@bot.tree.command(name="hack", description="Prank a user with a fake hacking sequence")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    fake_searches = ["how to pretend i know python", "why does my code work but i don't know why", "anime waifu tier list"]
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

        image_parts = []
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    img_bytes = await att.read()
                    image_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=att.content_type))

        try:
            async with message.channel.typing():
                response = await generate_ai_response(message.channel, clean_content, message.author, image_parts)
                for i in range(0, len(response), 2000):
                    await message.reply(response[i:i+2000], mention_author=False)
        except Exception as e:
            error_public = "There is an error.\nThe issue is sent to master admin yaen. The issue will be fixed soon, wait until yaen beats it up."
            await message.reply(error_public, mention_author=False)
            
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

# -------------------- Flask Web Dashboard (Admin Panel & Telemetry) --------------------
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
            --glass: rgba(15, 15, 20, 0.85);
            --glass-border: rgba(255, 255, 255, 0.08);
            --text-main: #f3f4f6;
            --gemini-grad: linear-gradient(90deg, #4285f4, #9b72cb, #d96570);
            --accent-glow: rgba(155, 114, 203, 0.3);
            --danger: #ef4444;
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
            backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.8);
        }
        
        .gemini-text {
            background: var(--gemini-grad);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            font-weight: 700; text-transform: uppercase; letter-spacing: 2px;
        }

        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; border-top: 3px solid #9b72cb; }
        .nav-tab { padding: 12px 15px; border-radius: 8px; cursor: pointer; transition: 0.3s; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; font-size: 0.9rem; border: 1px solid transparent; }
        .nav-tab:hover { background: rgba(255,255,255,0.05); }
        .nav-tab.active { background: rgba(155, 114, 203, 0.2); border-color: rgba(155, 114, 203, 0.5); color: #fff; }

        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; }
        
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: 80px; flex-direction: row; padding: 15px 25px; margin: 0; justify-content: space-around; align-items: center; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 20px 20px 0 0; border-top: 1px solid var(--glass-border); }
            .nav-tab { padding: 10px; font-size: 0.8rem; text-align: center; }
            #content { padding: 25px; padding-bottom: 130px; margin: 0; }
            .hide-mobile { display: none; }
        }

        h1, h2 { margin-top: 0; }
        .card { padding: 25px; margin-bottom: 25px; transition: 0.3s; }
        
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-label { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 2px; opacity: 0.6; }
        .stat-value { font-size: 3.5rem; font-weight: 300; margin-top: 10px; color: #fff; }
        .stat-small { font-size: 1rem; opacity: 0.7; margin-top: 5px;}
        
        pre { color: #a1a1aa; font-family: monospace; font-size: 0.95rem; line-height: 1.5; }
        .highlight { color: #4285f4; font-weight: bold; }

        label { display: block; margin-bottom: 8px; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px; opacity: 0.8; }
        input[type="text"], input[type="password"], textarea, select { 
            width: 100%; box-sizing: border-box; padding: 14px; margin-bottom: 20px; border-radius: 6px; 
            border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.6); color: white; 
            outline: none; font-family: 'Space Grotesk'; font-size: 1rem; transition: 0.3s;
        }
        input:focus, textarea:focus, select:focus { border-color: #9b72cb; box-shadow: 0 0 15px var(--accent-glow); }
        textarea { resize: vertical; min-height: 120px; }
        
        button { padding: 15px 25px; border-radius: 6px; border: none; background: var(--text-main); color: #000; font-weight: 700; font-size: 1rem; font-family: 'Space Grotesk'; cursor: pointer; transition: 0.3s; text-transform: uppercase; letter-spacing: 1.5px; }
        button:hover { background: #fff; transform: translateY(-2px); box-shadow: 0 5px 20px rgba(255,255,255,0.2); }
        
        .btn-danger { background: rgba(239, 68, 68, 0.1); border: 1px solid var(--danger); color: var(--danger); }
        .btn-danger:hover { background: var(--danger); color: #fff; box-shadow: 0 5px 20px rgba(239, 68, 68, 0.4); }

        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(5,5,5,0.9); display: flex; justify-content: center; align-items: center; z-index: 1000; }
        .login-box { padding: 50px; text-align: center; width: 340px; border-top: 3px solid #4285f4; }
        
        .hidden { display: none !important; }
        .visible-flex { display: flex !important; }
        .visible-block { display: block !important; }
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
            <button onclick="login()" style="width: 100%;">Enter Matrix</button>
            <p id="err" style="color: #ef4444; display: none; margin-top: 20px; font-size: 0.9rem;">Access Denied.</p>
        </div>
    </div>

    <div id="dashboard-view" class="hidden" style="width: 100%; height: 100%;">
        <div id="nav" class="glass">
            <div class="hide-mobile">
                <h2 class="gemini-text" style="margin: 0;">✨ YoAI</h2>
                <div style="opacity: 0.5; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 20px;">Command Center</div>
            </div>
            
            <div class="nav-tab active" id="tab-telemetry" onclick="switchTab('telemetry')">Telemetry</div>
            <div class="nav-tab" id="tab-admin" onclick="switchTab('admin')">Admin Panel</div>
            
            <div class="hide-mobile" style="margin-top: auto;">
                <button class="btn-danger" style="width: 100%; padding: 10px; font-size: 0.8rem;" onclick="logout()">Logout</button>
            </div>
        </div>

        <div id="content">
            <div id="section-telemetry" class="visible-block">
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
                    <div class="card glass stat-box">
                        <div class="stat-label">DB Weight</div>
                        <div class="stat-value" id="db-size">-</div>
                        <div class="stat-small">Kilobytes</div>
                    </div>
                </div>
                
                <div class="card glass" style="margin-top: 20px;">
                    <h2 style="font-size: 1.2rem; font-weight: 500; border-bottom: 1px solid var(--glass-border); padding-bottom: 15px;">Live Terminal</h2>
                    <pre id="logs">
[SYS] Initializing YoAI Command Center...
[SYS] Master authentication accepted.
[SYS] Auto-Optimization & Memory GC running.
[SYS] Engine: <span id="model-display" class="highlight">Loading...</span>
[SYS] Standing by for incoming data streams...
                    </pre>
                </div>
            </div>

            <div id="section-admin" class="hidden">
                <h1 class="gemini-text">Admin Control Panel</h1>
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 20px;">Core Matrix Directives</h2>
                    
                    <label>Global System Prompt</label>
                    <textarea id="admin-prompt" placeholder="You are YoAI..."></textarea>
                    
                    <label>Primary AI Engine</label>
                    <select id="admin-model">
                        <option value="gemini-2.5-flash">Gemini 2.5 Flash (Fast & Efficient)</option>
                        <option value="gemini-2.5-pro">Gemini 2.5 Pro (Highly Intelligent)</option>
                    </select>
                    
                    <button onclick="saveConfig()">Deploy Config</button>
                    <span id="save-status" style="margin-left: 15px; color: #10b981; display: none;">✅ Saved!</span>
                </div>

                <div class="card glass" style="border-color: rgba(239, 68, 68, 0.3);">
                    <h2 style="font-size: 1.2rem; color: var(--danger); margin-bottom: 10px;">Danger Zone</h2>
                    <p style="font-size: 0.9rem; opacity: 0.8; margin-bottom: 20px;">Executing a Hollow Purple will instantly wipe the entire SQLite conversation history across all Discord servers and DMs.</p>
                    <button class="btn-danger" onclick="nukeMemory()">Execute Hollow Purple (Wipe Memory)</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        function toggleUI(showDash) {
            document.getElementById('login-overlay').className = showDash ? 'glass hidden' : 'glass visible-flex';
            document.getElementById('dashboard-view').className = showDash ? 'visible-flex' : 'hidden';
        }

        function switchTab(tab) {
            document.getElementById('tab-telemetry').classList.remove('active');
            document.getElementById('tab-admin').classList.remove('active');
            document.getElementById('section-telemetry').classList.replace('visible-block', 'hidden');
            document.getElementById('section-admin').classList.replace('visible-block', 'hidden');

            document.getElementById('tab-' + tab).classList.add('active');
            document.getElementById('section-' + tab).classList.replace('hidden', 'visible-block');

            if(tab === 'admin') fetchAdminConfig();
        }

        async function login() {
            const pwd = document.getElementById('pwd').value;
            const res = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pwd }) });
            if (res.ok) { toggleUI(true); fetchStats(); setInterval(fetchStats, 3000); } 
            else { document.getElementById('err').style.display = 'block'; }
        }

        async function fetchStats() {
            try {
                if (document.getElementById('dashboard-view').classList.contains('hidden')) return;
                const res = await fetch('/api/stats');
                if (!res.ok) throw new Error('Unauthorized');
                const data = await res.json();
                document.getElementById('uptime').innerText = data.uptime;
                document.getElementById('queries').innerText = data.total_queries;
                document.getElementById('memory').innerText = data.active_memory_rows;
                document.getElementById('db-size').innerText = data.db_size;
                document.getElementById('model-display').innerText = data.current_model.replace("gemini-", "").toUpperCase();
            } catch (e) { toggleUI(false); }
        }

        async function fetchAdminConfig() {
            const res = await fetch('/api/config');
            if (res.ok) {
                const data = await res.json();
                document.getElementById('admin-prompt').value = data.system_prompt;
                document.getElementById('admin-model').value = data.current_model;
            }
        }

        async function saveConfig() {
            const payload = {
                system_prompt: document.getElementById('admin-prompt').value,
                current_model: document.getElementById('admin-model').value
            };
            const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (res.ok) {
                const status = document.getElementById('save-status');
                status.style.display = 'inline';
                setTimeout(() => status.style.display = 'none', 2000);
                fetchStats(); 
            }
        }

        async function nukeMemory() {
            if(confirm("WARNING: This will permanently delete all chat history data. Proceed?")) {
                const res = await fetch('/api/nuke', { method: 'POST' });
                if(res.ok) {
                    alert("Memory wiped successfully.");
                    fetchStats();
                }
            }
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
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    
    # Calculate DB file size in KB
    try:
        db_size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)
    except:
        db_size_kb = 0
    
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
        "db_size": db_size_kb,
        "current_model": current_model
    })

@flask_app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    if request.method == 'GET':
        sys_prompt = get_config('system_prompt', 'You are YoAI, a highly intelligent assistant.')
        model = get_config('current_model', 'gemini-2.5-flash')
        return jsonify({"system_prompt": sys_prompt, "current_model": model})
    
    if request.method == 'POST':
        data = request.get_json()
        if 'system_prompt' in data: set_config('system_prompt', data['system_prompt'])
        if 'current_model' in data: set_config('current_model', data['current_model'])
        return jsonify(success=True)

@flask_app.route('/api/nuke', methods=['POST'])
def api_nuke():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("DELETE FROM message_history")
        conn.commit()
        conn.close()
        
    return jsonify(success=True)

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
