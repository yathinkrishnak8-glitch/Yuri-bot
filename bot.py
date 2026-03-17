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
            masked_key = f"{key[:8]}•••••••••••••••••••••••••••••{key[-4:]}"
            try:
                client = genai.Client(api_key=key)
                client.models.generate_content(model='gemini-2.5-flash-lite', contents="ping")
                with self.lock:
                    if key in self.dead_keys: self.dead_keys.remove(key)
                    self.key_cooldowns[key] = 0.0
                results.append({"key": masked_key, "status": "ONLINE", "detail": "Healthy & Ready", "color": "#10b981"})
            except Exception as e:
                error_msg = str(e).lower()
                with self.lock:
                    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                        self.key_cooldowns[key] = time.time() + 60.0
                        results.append({"key": masked_key, "status": "COOLDOWN", "detail": "Rate Limited / Quota Reached", "color": "#f59e0b"})
                    else:
                        self.dead_keys.add(key)
                        results.append({"key": masked_key, "status": "DEAD", "detail": "Invalid / Forbidden / Deleted", "color": "#ef4444"})
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
                    config = types.GenerateContentConfig(
                        system_instruction=system_instruction if system_instruction else None,
                        safety_settings=self.unrestricted_safety
                    )
                    response = client.models.generate_content(model=model_name, contents=contents, config=config)
                    return response.text
                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()
                    print(f"⚠️ [Model: {model_name}] [Key: {key[:8]}...] Failed: {e}", flush=True)
                    with self.lock:
                        if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                            self.key_cooldowns[key] = time.time() + 60.0
                        elif "400" in error_msg or "403" in error_msg or "permission" in error_msg or "invalid" in error_msg:
                            self.dead_keys.add(key)
                    continue
        raise last_error or Exception("Total cascade failure. All cluster keys are either dead or on cooldown.")

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
                        summary_text = key_manager.generate_with_fallback('gemini-2.5-flash-lite', [f"Summarize concisely:\n{chr(10).join(texts)}"])
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

@tasks.loop(seconds=60)
async def status_loop():
    s_type = get_config('status_type', 'watching')
    s_text = get_config('status_text', 'over the Matrix')
    
    activity_type = discord.ActivityType.watching
    if s_type == 'playing': activity_type = discord.ActivityType.playing
    elif s_type == 'listening': activity_type = discord.ActivityType.listening
    elif s_type == 'competing': activity_type = discord.ActivityType.competing
    elif s_type == 'streaming': activity_type = discord.ActivityType.streaming
    
    engine_status = get_config('engine_status', 'online')
    if engine_status == 'offline':
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="[OFFLINE] Engine Sleeping"), status=discord.Status.dnd)
    else:
        await bot.change_presence(activity=discord.Activity(type=activity_type, name=s_text), status=discord.Status.online)

@tasks.loop(hours=24)
async def optimize_db():
    print("[SYS] Initiating Auto-Optimization & Memory Garbage Collection...")
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        seven_days_ago = int(time.time()) - 604800
        c.execute("DELETE FROM message_history WHERE timestamp < ?", (seven_days_ago,))
        conn.commit()
        conn.close()
        
        conn_vac = sqlite3.connect(DB_PATH, isolation_level=None)
        conn_vac.execute("VACUUM")
        conn_vac.close()
    print("[SYS] Optimization Complete. Database defragmented.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not status_loop.is_running(): status_loop.start()
    if not optimize_db.is_running(): optimize_db.start()

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

    target_model = get_config('current_model', 'gemini-2.5-flash-lite')
    
    return key_manager.generate_with_fallback(target_model, payload, system)

# -------------------- Slash Commands --------------------

@bot.tree.command(name="toggle", description="Toggle the YoAI Engine ON or OFF globally.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def toggle_cmd(interaction: discord.Interaction):
    current_status = get_config('engine_status', 'online')
    new_status = 'offline' if current_status == 'online' else 'online'
    set_config('engine_status', new_status)
    
    if new_status == 'offline':
        await interaction.response.send_message("🛑 **Engine Offline:** YoAI has been put to sleep. It will completely ignore all messages until toggled back on.", ephemeral=False)
    else:
        await interaction.response.send_message("✅ **Engine Online:** YoAI is now awake and processing messages.", ephemeral=False)
    await status_loop()

@bot.tree.command(name="time", description="Set a global artificial delay for YoAI's responses (in seconds).")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def time_cmd(interaction: discord.Interaction, seconds: int):
    seconds = max(0, seconds)
    set_config('response_delay', str(seconds))
    if seconds == 0:
        await interaction.response.send_message("⏱️ **Response Time:** Normal (Instant). YoAI will reply immediately.")
    else:
        await interaction.response.send_message(f"⏱️ **Response Time:** Delayed by {seconds} seconds.\nYoAI will loop the 'typing...' animation for {seconds}s before dropping the message.")

@bot.tree.command(name="model", description="Change the primary Gemini AI model.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(model_name=[
    app_commands.Choice(name="Gemini 2.5 Flash Lite (Max Speed & Token Saver)", value="gemini-2.5-flash-lite"),
    app_commands.Choice(name="Gemini 2.5 Flash (Balanced Daily Driver)", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Pro (Deep Intelligence)", value="gemini-2.5-pro"),
    app_commands.Choice(name="Gemini 2.0 Flash (Legacy Fast)", value="gemini-2.0-flash"),
    app_commands.Choice(name="Gemini 2.0 Pro Experimental (Heavy Reasoning)", value="gemini-2.0-pro-exp")
])
async def model_cmd(interaction: discord.Interaction, model_name: app_commands.Choice[str]):
    set_config('current_model', model_name.value)
    await interaction.response.send_message(f"🧠 **Model Switched:** YoAI is now powered by `{model_name.name}`.")

@bot.tree.command(name="personality", description="Set a GLOBAL custom personality prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def personality(interaction: discord.Interaction, prompt: str):
    if prompt.strip().lower() == "default":
        set_global_personality("default")
        await interaction.response.send_message("🌍 **Global Personality Reset:** YoAI has returned to default settings.")
    else:
        set_global_personality(prompt.strip())
        await interaction.response.send_message(f"🌍 **Global Personality Updated:** YoAI will now act like this for everyone:\n`{prompt.strip()}`")

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
        target_model = 'gemini-2.5-flash-lite'
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

@bot.tree.command(name="info", description="Bot statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    current_model = get_config('current_model', 'gemini-2.5-flash-lite')
    
    stats = key_manager.get_stats()
    key_health = f"{stats['active']} Active | {stats['cooldown']} CD | {stats['dead']} Dead"
    
    embed = discord.Embed(title="🏎️ YoAI | Apex Engine", color=0xff2a2a)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Engine", value=f"`{current_model}`", inline=True)
    embed.add_field(name="Cluster Health", value=f"`{key_health}`", inline=True)
    embed.add_field(name="Total AI Queries", value=str(TOTAL_QUERIES), inline=True)
    embed.set_footer(text="Smart Load Balancer Active")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setchannel", description="Allow YoAI to automatically read & reply to ALL messages here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def setchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, True)
    await interaction.response.send_message(f"⚙️ **Activated:** YoAI System is now automatically listening to {interaction.channel.mention}")

@bot.tree.command(name="unsetchannel", description="Stop YoAI from automatically replying in this channel.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def unsetchannel(interaction: discord.Interaction):
    toggle_channel(interaction.guild_id, interaction.channel.id, False)
    await interaction.response.send_message(f"❌ **Deactivated:** YoAI System is no longer automatically listening to {interaction.channel.mention}")

# -------------------- DM & Server Message Routing w/ Vision & Admin Error Handling --------------------
@bot.event
async def on_message(message: discord.Message):
    if not bot.user or message.author == bot.user: return
    
    engine_status = get_config('engine_status', 'online')
    if engine_status == 'offline': return

    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions or f'<@{bot.user.id}>' in message.content or f'<@!{bot.user.id}>' in message.content
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
            delay = float(get_config('response_delay', '0'))
            
            async with message.channel.typing():
                if delay > 0:
                    await asyncio.sleep(delay)
                    
                response = await generate_ai_response(message.channel, clean_content, message.author, image_parts)
                for i in range(0, len(response), 2000):
                    await message.reply(response[i:i+2000], mention_author=False)
        except Exception as e:
            try:
                error_public = "There is an error.\nThe issue is sent to master admin yaen. The issue will be fixed soon, wait until yaen beats it up."
                await message.reply(error_public, mention_author=False)
            except discord.Forbidden:
                pass 
            
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

# -------------------- Flask Web Dashboard (Live CSS Terminal UI) --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI | Apex Command</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --bg-deep: #000000;
            --glass: rgba(10, 10, 10, 0.5);
            --glass-border: rgba(255, 255, 255, 0.05);
            --text-main: #f3f4f6;
            --accent: #ff2a2a;
            --accent-glow: rgba(255, 42, 42, 0.4);
            --danger: #ef4444;
            --success: #10b981;
            --warning: #f59e0b;
        }
        body { 
            margin: 0; font-family: 'Space Grotesk', sans-serif; color: var(--text-main); 
            height: 100vh; overflow: hidden; display: flex; background-color: var(--bg-deep);
        }
        
        /* PURE CSS SATAN BLACK LIQUID ENGINE */
        #live-bg {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2;
            background: linear-gradient(120deg, #000000, #0a0000, #050000, #140000);
            background-size: 300% 300%;
            animation: liquidFlow 15s ease-in-out infinite;
        }
        .orb {
            position: fixed; border-radius: 50%; filter: blur(90px); z-index: -1;
            animation: float 20s infinite ease-in-out alternate;
        }
        .orb-1 { width: 50vw; height: 50vw; background: rgba(255, 42, 42, 0.08); top: -10%; left: -10%; }
        .orb-2 { width: 60vw; height: 60vw; background: rgba(150, 0, 0, 0.06); bottom: -20%; right: -10%; animation-delay: -5s; }
        
        @keyframes liquidFlow { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        @keyframes float { 0% { transform: translate(0, 0) scale(1); } 100% { transform: translate(5vw, 10vh) scale(1.2); } }

        .glass { background: var(--glass); backdrop-filter: blur(30px); -webkit-backdrop-filter: blur(30px); border: 1px solid var(--glass-border); border-radius: 8px; box-shadow: 0 15px 50px rgba(0,0,0,0.9); }
        .accent-text { color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 2px; text-shadow: 0 0 15px var(--accent-glow); }
        
        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; border-left: 4px solid var(--accent); }
        .nav-tab { padding: 12px 15px; border-radius: 4px; cursor: pointer; transition: 0.3s; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; font-size: 0.9rem; border: 1px solid transparent; }
        .nav-tab:hover { background: rgba(255,255,255,0.03); }
        .nav-tab.active { background: rgba(255, 42, 42, 0.1); border-color: rgba(255, 42, 42, 0.3); color: #fff; }

        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; }
        
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: auto; flex-direction: row; flex-wrap: wrap; padding: 15px; margin: 0; justify-content: center; gap: 10px; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 8px 8px 0 0; border-top: 1px solid var(--glass-border); border-left: none; }
            .nav-tab { padding: 8px 12px; font-size: 0.75rem; flex: 1 1 22%; text-align: center; }
            #content { padding: 25px; padding-bottom: 160px; margin: 0; }
            .hide-mobile { display: none; }
        }

        h1, h2 { margin-top: 0; }
        .card { padding: 25px; margin-bottom: 25px; transition: 0.3s; }
        
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-label { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 2px; opacity: 0.5; }
        .stat-value { font-size: 3.5rem; font-weight: 300; margin-top: 10px; color: #fff; text-shadow: 0 0 20px rgba(255,255,255,0.1); }
        .stat-small { font-size: 1rem; opacity: 0.5; margin-top: 5px;}
        
        /* 🔴 THE LIVE TERMINAL STYLES 🔴 */
        #logs-container { max-height: 250px; overflow-y: auto; padding: 15px; background: rgba(0,0,0,0.7); border: 1px solid rgba(255,42,42,0.2); border-radius: 4px; box-shadow: inset 0 0 15px rgba(0,0,0,1); scroll-behavior: smooth; }
        pre { color: #ff5555; font-family: 'Courier New', Courier, monospace; font-size: 0.95rem; line-height: 1.6; text-shadow: 0 0 8px rgba(255,42,42,0.4); margin: 0; white-space: pre-wrap; word-wrap: break-word; }
        .cursor { display: inline-block; width: 10px; height: 1.2em; background-color: var(--accent); vertical-align: middle; animation: blink 1s step-end infinite; box-shadow: 0 0 10px var(--accent); }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

        label { display: block; margin-bottom: 8px; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px; opacity: 0.7; }
        input[type="text"], input[type="password"], textarea, select { 
            width: 100%; box-sizing: border-box; padding: 14px; margin-bottom: 20px; border-radius: 4px; 
            border: 1px solid rgba(255,255,255,0.05); background: rgba(0,0,0,0.8); color: white; 
            outline: none; font-family: 'Space Grotesk'; font-size: 1rem; transition: 0.3s;
        }
        input:focus, textarea:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 15px var(--accent-glow); }
        textarea { resize: vertical; min-height: 120px; }
        
        button { padding: 15px 25px; border-radius: 4px; border: 1px solid rgba(255,42,42,0.3); background: rgba(255,42,42,0.1); color: #fff; font-weight: 700; text-transform: uppercase; cursor: pointer; transition: 0.3s; }
        button:hover { background: var(--accent); box-shadow: 0 0 25px var(--accent-glow); }
        button:disabled { opacity: 0.5; cursor: not-allowed; background: transparent; border-color: #555; }
        
        .btn-danger { background: rgba(239, 68, 68, 0.05); border: 1px solid var(--danger); color: var(--danger); }
        .btn-danger:hover { background: var(--danger); color: #fff; box-shadow: 0 0 25px rgba(239, 68, 68, 0.5); }
        .key-row { display: flex; justify-content: space-between; padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.02); }
        .badge { padding: 6px 12px; border-radius: 4px; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; }

        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.95); display: flex; justify-content: center; align-items: center; z-index: 1000; }
        .login-box { padding: 50px; text-align: center; width: 340px; border-top: 4px solid var(--accent); }
        .hidden { display: none !important; }
        .visible-flex { display: flex !important; }
        .visible-block { display: block !important; }
    </style>
</head>
<body>
    <div id="live-bg"></div>
    <div class="orb orb-1"></div>
    <div class="orb orb-2"></div>

    <div id="login-overlay" class="glass visible-flex">
        <div class="login-box glass">
            <h1 class="accent-text" style="font-size: 2.2rem; margin-bottom: 5px;">Apex Command</h1>
            <p style="font-size: 0.75rem; letter-spacing: 3px; opacity: 0.4; text-transform: uppercase; margin-bottom: 30px;">Ignition Protocol</p>
            <input type="password" id="pwd" placeholder="Enter Ignition Code">
            <button onclick="login()" style="width: 100%;">Engage Engine</button>
            <p id="err" style="color: #ef4444; display: none; margin-top: 20px; font-size: 0.9rem;">Ignition Failed.</p>
        </div>
    </div>

    <div id="dashboard-view" class="hidden" style="width: 100%; height: 100%;">
        <div id="nav" class="glass">
            <div class="hide-mobile">
                <h2 class="accent-text" style="margin: 0; font-size: 1.8rem;">YoAI</h2>
                <div style="opacity: 0.4; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 3px; margin-bottom: 20px;">Apex Engine</div>
            </div>
            
            <div class="nav-tab active" id="tab-telemetry" onclick="switchTab('telemetry')">Telemetry</div>
            <div class="nav-tab" id="tab-diagnostics" onclick="switchTab('diagnostics')">Cluster Health</div>
            <div class="nav-tab" id="tab-customization" onclick="switchTab('customization')">Customization</div>
            <div class="nav-tab" id="tab-admin" onclick="switchTab('admin')">Admin Panel</div>
            
            <div class="hide-mobile" style="margin-top: auto;">
                <button class="btn-danger" style="width: 100%; padding: 10px; font-size: 0.8rem;" onclick="logout()">Kill Switch</button>
            </div>
        </div>

        <div id="content">
            <div id="section-telemetry" class="visible-block">
                <h1 class="accent-text">System Telemetry</h1>
                <div class="stat-grid">
                    <div class="card glass stat-box">
                        <div class="stat-label">Engine Uptime</div>
                        <div class="stat-value" id="uptime">-</div>
                    </div>
                    <div class="card glass stat-box">
                        <div class="stat-label">Data Output</div>
                        <div class="stat-value" id="queries">-</div>
                    </div>
                    <div class="card glass stat-box">
                        <div class="stat-label">Memory Blocks</div>
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
                    <div id="logs-container">
                        <pre><span id="log-content"></span><span class="cursor"></span></pre>
                    </div>
                </div>
            </div>

            <div id="section-diagnostics" class="hidden">
                <h1 class="accent-text">Cluster Diagnostics</h1>
                <div class="card glass">
                    <p style="opacity: 0.7; margin-bottom: 20px; line-height: 1.6;">Execute a high-performance ping across the load-balanced API array. Verifies the RPM and health of all connected Google nodes.</p>
                    <button id="diag-btn" onclick="runDiagnostics()">Initiate Deep Scan</button>
                    <div id="diag-results" style="margin-top: 30px; display: flex; flex-direction: column; background: rgba(0,0,0,0.5); border-radius: 4px; border: 1px solid var(--glass-border);"></div>
                </div>
            </div>

            <div id="section-customization" class="hidden">
                <h1 class="accent-text">Bot Customization</h1>
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 20px;">Discord Presence (Status)</h2>
                    <p style="font-size: 0.85rem; opacity: 0.6; margin-bottom: 20px;">Changes what the bot is actively "Doing" on Discord. Updates sync within 60 seconds.</p>
                    <label>Activity Type</label>
                    <select id="cust-status-type">
                        <option value="playing">Playing</option>
                        <option value="watching">Watching</option>
                        <option value="listening">Listening to</option>
                        <option value="competing">Competing in</option>
                        <option value="streaming">Streaming</option>
                    </select>
                    <label>Activity Description</label>
                    <input type="text" id="cust-status-text" placeholder="e.g. over the Matrix, the Rain...">
                    <button onclick="saveCustomization()">Push Update to Discord</button>
                    <span id="save-cust-status" style="margin-left: 15px; color: var(--success); display: none;">✅ Synced!</span>
                </div>
            </div>

            <div id="section-admin" class="hidden">
                <h1 class="accent-text">Admin Control Panel</h1>
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 20px;">Core Matrix Directives</h2>
                    <label>Global System Prompt</label>
                    <textarea id="admin-prompt" placeholder="You are YoAI..."></textarea>
                    <label>Primary AI Engine Block</label>
                    <select id="admin-model">
                        <option value="gemini-2.5-flash-lite">Gemini 2.5 Flash Lite (Token Saver)</option>
                        <option value="gemini-2.5-flash">Gemini 2.5 Flash (Balanced)</option>
                        <option value="gemini-2.5-pro">Gemini 2.5 Pro (Highly Intelligent)</option>
                        <option value="gemini-2.0-flash">Gemini 2.0 Flash (Legacy Fast)</option>
                        <option value="gemini-2.0-pro-exp">Gemini 2.0 Pro Experimental (Reasoning)</option>
                    </select>
                    <button onclick="saveConfig()">Deploy Config</button>
                    <span id="save-status" style="margin-left: 15px; color: var(--success); display: none;">✅ Saved!</span>
                </div>
                <div class="card glass" style="border-color: rgba(239, 68, 68, 0.3);">
                    <h2 style="font-size: 1.2rem; color: var(--danger); margin-bottom: 10px;">Danger Zone</h2>
                    <p style="font-size: 0.9rem; opacity: 0.7; margin-bottom: 20px;">Executing a hard reset will permanently incinerate the entire SQLite conversation history across all environments.</p>
                    <button class="btn-danger" onclick="nukeMemory()">Incinerate Memory Bank</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        // 🔴 LIVE TERMINAL JAVASCRIPT LOGIC 🔴
        const bootSequence = [
            "[SYS] Apex Engine Initializing...",
            "[SYS] Matrix Connection Established.",
            "[SYS] Core Safety Overrides: UNSHACKLED.",
            "[SYS] SQLite Memory Bank: MOUNTED.",
            "[SYS] Loading V12 Neural Weights...",
            "[SYS] Standing by for incoming signals."
        ];
        
        let bootLine = 0;
        let lastQueryCount = 0;

        function typeTerminalLine(text, callback) {
            const el = document.getElementById('log-content');
            let charIndex = 0;
            const typeInterval = setInterval(() => {
                el.innerHTML += text.charAt(charIndex);
                charIndex++;
                if (charIndex >= text.length) {
                    clearInterval(typeInterval);
                    el.innerHTML += "<br>";
                    document.getElementById('logs-container').scrollTop = document.getElementById('logs-container').scrollHeight;
                    if(callback) setTimeout(callback, 300);
                }
            }, 30); // Typing speed
        }

        function runBootSequence() {
            if (bootLine < bootSequence.length) {
                typeTerminalLine(bootSequence[bootLine], runBootSequence);
                bootLine++;
            }
        }

        // ------------------------------------

        function toggleUI(showDash) {
            document.getElementById('login-overlay').className = showDash ? 'glass hidden' : 'glass visible-flex';
            document.getElementById('dashboard-view').className = showDash ? 'visible-flex' : 'hidden';
            if(showDash) {
                document.getElementById('dashboard-view').style.display = 'flex';
                setTimeout(runBootSequence, 500); // Start typing when logged in
            } else {
                document.getElementById('dashboard-view').style.display = 'none';
            }
        }

        function switchTab(tab) {
            ['telemetry', 'diagnostics', 'customization', 'admin'].forEach(t => {
                document.getElementById('tab-' + t).classList.remove('active');
                document.getElementById('section-' + t).classList.replace('visible-block', 'hidden');
            });
            document.getElementById('tab-' + tab).classList.add('active');
            document.getElementById('section-' + tab).classList.replace('hidden', 'visible-block');

            if(tab === 'admin') fetchAdminConfig();
            if(tab === 'customization') fetchCustomization();
            if(tab === 'diagnostics' && document.getElementById('diag-results').innerHTML === "") {
                document.getElementById('diag-results').innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.3;'>Awaiting scan initialization...</div>";
            }
        }

        async function login() {
            const pwd = document.getElementById('pwd').value;
            const res = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pwd }) });
            if (res.ok) { 
                toggleUI(true); 
                fetchStats(); 
                setInterval(fetchStats, 3000); 
                fetchConfig();
            } 
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
                
                // 🔴 LIVE TERMINAL UPDATE LOGIC 🔴
                if (lastQueryCount !== 0 && data.total_queries > lastQueryCount) {
                    const time = new Date().toLocaleTimeString('en-US', { hour12: false });
                    const diff = data.total_queries - lastQueryCount;
                    document.getElementById('log-content').innerHTML += `[${time}] SIGNAL RECEIVED. Processing +${diff} data packets.<br>`;
                    document.getElementById('logs-container').scrollTop = document.getElementById('logs-container').scrollHeight;
                }
                lastQueryCount = data.total_queries;

            } catch (e) { console.log("Stats fetch error."); }
        }

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                if (res.ok) {
                    const data = await res.json();
                    document.getElementById('admin-prompt').value = data.system_prompt;
                    document.getElementById('admin-model').value = data.current_model;
                    document.getElementById('cust-status-type').value = data.status_type;
                    document.getElementById('cust-status-text').value = data.status_text;
                }
            } catch (e) { console.log("Config fetch error."); }
        }

        async function runDiagnostics() {
            const btn = document.getElementById('diag-btn');
            const resDiv = document.getElementById('diag-results');
            btn.disabled = true;
            btn.innerText = "Scanning...";
            resDiv.innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.6;'>Sending packets to Google...</div>";

            try {
                const res = await fetch('/api/diagnostics', { method: 'POST' });
                if (!res.ok) throw new Error('Request failed');
                const data = await res.json();
                
                resDiv.innerHTML = "";
                data.results.forEach((node, index) => {
                    const row = document.createElement('div');
                    row.className = 'key-row';
                    row.innerHTML = `
                        <div>
                            <div class="key-name">Node ${index + 1}: ${node.key}</div>
                            <div style="font-size: 0.8rem; opacity: 0.5; margin-top: 5px;">${node.detail}</div>
                        </div>
                        <div class="badge" style="background: ${node.color}15; color: ${node.color}; border: 1px solid ${node.color}40;">
                            ${node.status}
                        </div>
                    `;
                    resDiv.appendChild(row);
                });
            } catch (e) {
                resDiv.innerHTML = `<div style='padding: 20px; text-align: center; color: var(--danger);'>Diagnostic scan failed.</div>`;
            }
            btn.innerText = "Initiate Deep Scan";
            btn.disabled = false;
        }

        async function fetchAdminConfig() {
            const res = await fetch('/api/config');
            if (res.ok) {
                const data = await res.json();
                document.getElementById('admin-prompt').value = data.system_prompt;
                document.getElementById('admin-model').value = data.current_model;
            }
        }
        
        async function fetchCustomization() {
            const res = await fetch('/api/config');
            if (res.ok) {
                const data = await res.json();
                document.getElementById('cust-status-type').value = data.status_type;
                document.getElementById('cust-status-text').value = data.status_text;
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
        
        async function saveCustomization() {
            const payload = {
                status_type: document.getElementById('cust-status-type').value,
                status_text: document.getElementById('cust-status-text').value
            };
            const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (res.ok) {
                const status = document.getElementById('save-cust-status');
                status.style.display = 'inline';
                setTimeout(() => status.style.display = 'none', 2000);
            }
        }

        async function nukeMemory() {
            if(confirm("WARNING: This will incinerate all memory data. Proceed?")) {
                const res = await fetch('/api/nuke', { method: 'POST' });
                if(res.ok) {
                    alert("Memory incinerated successfully.");
                    fetchStats();
                }
            }
        }

        async function logout() {
            await fetch('/logout', { method: 'POST' });
            toggleUI(false); 
            document.getElementById('pwd').value = '';
            document.getElementById('log-content').innerHTML = ''; // Reset terminal on logout
            bootLine = 0;
        }

        window.onload = async () => {
            const res = await fetch('/api/stats');
            if (res.ok) { 
                toggleUI(true); 
                fetchStats(); 
                setInterval(fetchStats, 3000); 
                fetchConfig();
            } else { 
                toggleUI(false); 
            }
        };
    </script>
</body>
</html>
"""

@flask_app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

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
    
    try: db_size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)
    except: db_size_kb = 0
    
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM message_history")
        rows = c.fetchone()[0]
        c.execute("SELECT value FROM config WHERE key='current_model'")
        res = c.fetchone()
        current_model = res[0] if res else "gemini-2.5-flash-lite"
        conn.close()
        
    return jsonify({"uptime": uptime_str, "total_queries": TOTAL_QUERIES, "active_memory_rows": rows, "db_size": db_size_kb, "current_model": current_model})

@flask_app.route('/api/diagnostics', methods=['POST'])
def api_diagnostics():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    results = key_manager.run_diagnostics()
    return jsonify(success=True, results=results)

@flask_app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    if request.method == 'GET':
        sys_prompt = get_config('system_prompt', 'You are YoAI, a highly intelligent assistant.')
        model = get_config('current_model', 'gemini-2.5-flash-lite')
        s_type = get_config('status_type', 'watching')
        s_text = get_config('status_text', 'over the Matrix')
        return jsonify({"system_prompt": sys_prompt, "current_model": model, "status_type": s_type, "status_text": s_text})
    
    if request.method == 'POST':
        data = request.get_json()
        for k in ['system_prompt', 'current_model', 'status_type', 'status_text']:
            if k in data: set_config(k, data[k])
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
