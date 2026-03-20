import discord
from discord import app_commands
from discord.ext import commands, tasks
from quart import Quart, request, session, jsonify, render_template_string
import aiosqlite
import os
import random
import time
import asyncio
import datetime
import re
import gc  

try:
    import resource 
except ImportError:
    resource = None

# NEW GOOGLE SDK
from google import genai
from google.genai import types

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
QUERY_TIMESTAMPS = [] 
DB_PATH = "yoai.db"
DB_CONN = None

# 🔴 ANTI-SPAM SHIELD: Prevents Discord Cloudflare Bans
LAST_ALERT_TIME = 0.0

CONFIG_CACHE = {}
CHANNEL_BUFFERS = {}
CHANNEL_TIMERS = {}

GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

FLASK_SECRET = os.environ.get("FLASK_SECRET", "yoai_persistent_secret_key_123")
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Database Setup --------------------
async def init_db():
    global DB_CONN
    if DB_CONN is None:
        DB_CONN = await aiosqlite.connect(DB_PATH)
        
    await DB_CONN.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    await DB_CONN.execute("CREATE TABLE IF NOT EXISTS allowed_channels (guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id))")
    await DB_CONN.execute("""CREATE TABLE IF NOT EXISTS message_history (
        channel_id INTEGER, message_id INTEGER PRIMARY KEY, author_id INTEGER,
        content TEXT, timestamp INTEGER
    )""")
    await DB_CONN.execute("""CREATE TABLE IF NOT EXISTS system_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        user TEXT,
        trace TEXT
    )""")
    
    defaults = {
        'system_prompt': 'You are YoAI, a highly intelligent assistant.',
        'current_model': 'gemini-2.5-flash-lite',
        'global_personality': 'default',
        'status_type': 'watching',
        'status_text': 'over the Matrix',
        'response_delay': '0',
        'engine_status': 'online',
        'safety_hate': 'BLOCK_NONE',
        'safety_harassment': 'BLOCK_NONE',
        'safety_explicit': 'BLOCK_NONE',
        'safety_dangerous': 'BLOCK_NONE'
    }
    
    for k, v in defaults.items():
        await DB_CONN.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
        
    await DB_CONN.commit()
    
    async with DB_CONN.execute("SELECT key, value FROM config") as cursor:
        async for row in cursor:
            CONFIG_CACHE[row[0]] = row[1]

def get_config(key: str, default: str) -> str:
    return CONFIG_CACHE.get(key, default)

async def set_config(key: str, value: str):
    CONFIG_CACHE[key] = value
    await DB_CONN.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    await DB_CONN.commit()

async def log_system_error(user: str, trace: str):
    try:
        await DB_CONN.execute("INSERT INTO system_errors (timestamp, user, trace) VALUES (?, ?, ?)", (time.time(), user, trace))
        await DB_CONN.execute("DELETE FROM system_errors WHERE id NOT IN (SELECT id FROM system_errors ORDER BY id DESC LIMIT 50)")
        await DB_CONN.commit()
    except Exception as e:
        print(f"CRITICAL DB LOGGING ERROR: {e}")

# -------------------- Smart Cluster Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.key_objects = []
        self.key_mapping = {}
        self.all_keys = []
        
        for i, k in enumerate(keys):
            k = k.strip()
            if not k: continue
            
            name = f"Node {i+1}"
            actual_key = k
            
            if ":" in k and not k.startswith("AIza"):
                parts = k.split(":", 1)
                name = parts[0].strip()
                actual_key = parts[1].strip()
            
            self.key_objects.append({
                'index': i + 1,
                'name': name,
                'key': actual_key
            })
            
            self.key_mapping[actual_key] = f"{name} ({actual_key[:8]}...)"
            self.all_keys.append(actual_key)
            
        self.key_cooldowns = {k: 0.0 for k in self.all_keys}
        self.key_usage = {k: [] for k in self.all_keys} 
        self.current_key_idx = 0 
        self.dead_keys = set()
        self.lock = asyncio.Lock()
        
    def get_dynamic_safety(self):
        return [
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=getattr(types.HarmBlockThreshold, get_config('safety_hate', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_harassment', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_explicit', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_dangerous', 'BLOCK_NONE'))),
        ]
    
    async def get_stats(self) -> dict:
        async with self.lock:
            now = time.time()
            total = len(self.all_keys)
            dead = len(self.dead_keys)
            cooldown = sum(1 for k in self.all_keys if k not in self.dead_keys and self.key_cooldowns[k] > now)
            active = total - dead - cooldown
            return {"total": total, "active": active, "cooldown": cooldown, "dead": dead}
            
    async def run_diagnostics(self) -> list:
        results = []
        now = time.time()
        for obj in self.key_objects:
            key = obj['key']
            masked_key = f"{key[:8]}•••••••••••••••••••••••••••••{key[-4:]}"
            
            try:
                client = genai.Client(api_key=key)
                await asyncio.wait_for(client.aio.models.generate_content(model='gemini-2.5-flash-lite', contents="ping"), timeout=15.0)
                
                async with self.lock:
                    if key in self.dead_keys: self.dead_keys.remove(key)
                    self.key_cooldowns[key] = 0.0
                    self.key_usage[key] = []
                
                results.append({
                    "index": obj['index'],
                    "name": obj['name'],
                    "masked_key": masked_key,
                    "status": "ONLINE",
                    "detail": "Healthy & Ready",
                    "unlock_time": 0,
                    "color": "#10b981"
                })
            except Exception as e:
                error_msg = str(e).lower()
                async with self.lock:
                    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg or "timeout" in error_msg:
                        cooldown_time = 60.0
                        try:
                            match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg)
                            if match:
                                cooldown_time = float(match.group(1)) + 2.0
                        except: pass
                            
                        unlock_epoch = now + cooldown_time
                        self.key_cooldowns[key] = unlock_epoch
                        self.key_usage[key] = []
                        
                        results.append({
                            "index": obj['index'],
                            "name": obj['name'],
                            "masked_key": masked_key,
                            "status": "COOLDOWN",
                            "detail": f"Rate Limited or Timeout",
                            "unlock_time": unlock_epoch, 
                            "color": "#f59e0b"
                        })
                    else:
                        self.dead_keys.add(key)
                        results.append({
                            "index": obj['index'],
                            "name": obj['name'],
                            "masked_key": masked_key,
                            "status": "DEAD",
                            "detail": "Invalid / Forbidden / Deleted",
                            "unlock_time": 0,
                            "color": "#ef4444"
                        })
        return results

    async def generate_with_fallback(self, target_model: str, contents: list, system_instruction: str = None) -> str:
        fallback_models = [target_model, 'gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-2.5-pro']
        models_to_try = list(dict.fromkeys(fallback_models)) 
        last_error = None
        dynamic_safety = self.get_dynamic_safety()
        
        for model_name in models_to_try:
            async with self.lock:
                now = time.time()
                available_keys = []
                
                for k in self.all_keys:
                    self.key_usage[k] = [ts for ts in self.key_usage[k] if now - ts < 60.0]
                    if k not in self.dead_keys and self.key_cooldowns[k] <= now and len(self.key_usage[k]) < 14:
                        available_keys.append(k)
                
                if not available_keys: 
                    raise Exception("Cascade Failure: All keys are exhausted (RPM Limit) or dead. No healthy nodes left.")
                
                selected_keys = []
                for _ in range(len(self.all_keys)):
                    k = self.all_keys[self.current_key_idx % len(self.all_keys)]
                    self.current_key_idx = (self.current_key_idx + 1) % len(self.all_keys)
                    if k in available_keys:
                        selected_keys.append(k)
            
            for key in selected_keys:
                try:
                    async with self.lock:
                        self.key_usage[key].append(time.time())

                    client = genai.Client(api_key=key)
                    config = types.GenerateContentConfig(
                        system_instruction=system_instruction if system_instruction else None,
                        safety_settings=dynamic_safety
                    )
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(model=model_name, contents=contents, config=config),
                        timeout=30.0
                    )
                    return response.text
                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()
                    display_name = self.key_mapping.get(key, "Unknown Key")
                    print(f"⚠️ [Model: {model_name}] [{display_name}] Failed: {e}", flush=True)
                    
                    async with self.lock:
                        if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg or "timeout" in error_msg:
                            cooldown_time = 60.0
                            try:
                                match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg)
                                if match:
                                    cooldown_time = float(match.group(1)) + 2.0
                            except: pass
                            self.key_cooldowns[key] = time.time() + cooldown_time
                            self.key_usage[key] = []
                        elif "400" in error_msg or "403" in error_msg or "permission" in error_msg or "invalid" in error_msg:
                            self.dead_keys.add(key)
                    continue
                    
        raise Exception(f"Cascade Failure Details: {str(last_error)}")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Background Memory Compression --------------------
async def background_summarize(channel_id, oldest):
    texts = [f"User ID {aid}: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
    if not texts: return
    
    try:
        summary_text = await key_manager.generate_with_fallback('gemini-2.5-flash-lite', [f"Summarize concisely:\n{chr(10).join(texts)}"])
    except:
        summary_text = "[Summary unavailable]"
        
    oldest_ids = [mid for mid, _, _, _ in oldest]
    timestamp = oldest[0][3]
    
    try:
        await DB_CONN.execute(f"DELETE FROM message_history WHERE message_id IN ({','.join('?'*len(oldest_ids))})", oldest_ids)
        await DB_CONN.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)", (channel_id, -1, summary_text, timestamp))
        await DB_CONN.commit()
    except Exception as e:
        print(f"Compression DB Error: {e}")

# -------------------- Helper Functions --------------------
async def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    if guild_id is None: return True
    async with DB_CONN.execute("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)) as cursor:
        result = await cursor.fetchone()
    return result is not None

async def toggle_channel(guild_id: int, channel_id: int, enable: bool):
    if enable:
        await DB_CONN.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
    else:
        await DB_CONN.execute("DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
    await DB_CONN.commit()

async def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    await DB_CONN.execute("INSERT OR REPLACE INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                (channel_id, message_id, author_id, content, timestamp))
    await DB_CONN.commit()
    
    async with DB_CONN.execute("SELECT COUNT(*) FROM message_history WHERE channel_id=?", (channel_id,)) as cursor:
        count = (await cursor.fetchone())[0]
        
    if count > 15:
        async with DB_CONN.execute("SELECT message_id, author_id, content, timestamp FROM message_history WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 10", (channel_id,)) as cursor:
            oldest = await cursor.fetchall()
        if oldest:
            bot.loop.create_task(background_summarize(channel_id, oldest))

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
        print(f"[SYS] Matrix Sync bypassed on boot to prevent Cloudflare 1015 Bans. Use !sync to register commands.")
        await init_db()
        bot.loop.create_task(app.run_task(host="0.0.0.0", port=PORT))

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
    print("[SYS] Initiating Auto-Optimization...")
    try:
        seven_days_ago = int(time.time()) - 604800
        await DB_CONN.execute("DELETE FROM message_history WHERE timestamp < ?", (seven_days_ago,))
        await DB_CONN.commit()
            
        collected = gc.collect()
        print(f"[SYS] Optimization Complete. Cleared {collected} dead objects from RAM.")
    except Exception as e:
        print(f"[SYS] Optimization Error: {e}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not status_loop.is_running(): status_loop.start()
    if not optimize_db.is_running(): optimize_db.start()

@bot.command(name="sync")
async def sync_cmds(ctx):
    if ctx.author.id != 1285791141266063475: return
    await bot.tree.sync()
    await ctx.send("✅ Slash commands manually synchronized to Discord API.")

# -------------------- The AI Generator --------------------
async def generate_ai_response(channel: discord.abc.Messageable, user_message: str, author: discord.User, image_parts: list = None) -> str:
    global TOTAL_QUERIES, QUERY_TIMESTAMPS
    TOTAL_QUERIES += 1
    QUERY_TIMESTAMPS.append(time.time())

    guild = getattr(channel, 'guild', None)
    channel_id = channel.id

    async with DB_CONN.execute("SELECT author_id, content FROM message_history WHERE channel_id=? ORDER BY timestamp DESC, message_id DESC LIMIT 10", (channel_id,)) as cursor:
        history = await cursor.fetchall()
            
    history.reverse() 
    
    context_lines = []
    current_chars = 0
    
    for aid, cnt in reversed(history):
        if aid == 0:
            line = f"[System Summary]: {cnt}\n"
        else:
            user_obj = guild.get_member(aid) if guild else bot.get_user(aid)
            raw_name = user_obj.display_name if user_obj else f"User_{aid}"
            safe_name = clean_discord_name(raw_name)
            line = f"{safe_name}: {cnt}\n"
            
        if current_chars + len(line) > 3000:
            break
        context_lines.insert(0, line)
        current_chars += len(line)
        
    context_str = "[SYSTEM: Below is the recent chat history for context]\n"
    context_str += "".join(context_lines)
    context_str += "[SYSTEM: End of history.]\n\n"
    
    current_safe_name = clean_discord_name(author.display_name)
    context_str += f"Reply directly to {current_safe_name}'s new message: {user_message}"

    payload = [context_str]
    if image_parts:
        payload.extend(image_parts)

    system = get_config('system_prompt', 'You are YoAI.')
    personality = get_config('global_personality', 'default')
    if personality != "default":
        system += f"\n\n[GLOBAL PERSONALITY OVERRIDE]: You must strictly follow this persona for ALL users: {personality}"

    system += "\n\nCRITICAL DIRECTIVE: You are participating in a live Discord chat room. Respond DIRECTLY and NATURALLY to the user. DO NOT output a chat transcript. DO NOT write dialogue for other users. DO NOT prefix your response with 'YoAI:'."

    target_model = get_config('current_model', 'gemini-2.5-flash-lite')
    
    return await key_manager.generate_with_fallback(target_model, payload, system)

# -------------------- Slash Commands --------------------

class InfoView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(discord.ui.Button(label="Open Web Dashboard", style=discord.ButtonStyle.link, url="https://yoai.onrender.com"))

@bot.tree.command(name="info", description="Bot statistics and control panel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    current_model = get_config('current_model', 'gemini-2.5-flash-lite')
    
    stats = await key_manager.get_stats()
    key_health = f"{stats['active']} Active | {stats['cooldown']} CD | {stats['dead']} Dead"
    
    embed = discord.Embed(title="🏎️ YoAI | Apex Engine 4.4", color=0xff2a2a, description="Advanced Asynchronous Matrix System")
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Engine", value=f"`{current_model}`", inline=True)
    embed.add_field(name="Cluster Health", value=f"`{key_health}`", inline=True)
    embed.add_field(name="Total AI Queries", value=str(TOTAL_QUERIES), inline=True)
    embed.add_field(name="Architect", value="**mr_yaen (Yathin)**", inline=True)
    embed.set_footer(text="Smart Load Balancer & Janitor Protocol Active")
    
    await interaction.response.send_message(embed=embed, view=InfoView())

@bot.tree.command(name="invite", description="Get the bot's invite link (WARNING: Leaves the server if used inside one!)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def invite_cmd(interaction: discord.Interaction):
    client_id = bot.user.id
    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions=0&scope=bot%20applications.commands"
    
    if interaction.guild:
        await interaction.response.send_message(f"👋 You used `/invite` inside a server. As requested, I am leaving this Matrix sector immediately!\n\n🔗 **Bring me back (or to another server):**\n{invite_url}")
        try:
            await interaction.guild.leave()
        except Exception as e:
            print(f"Failed to leave guild: {e}")
    else:
        await interaction.response.send_message(f"🔗 **Add YoAI to your server (No Admin Perms Required):**\n{invite_url}")

@bot.tree.command(name="toggle", description="[ADMIN] Toggle the YoAI Engine ON or OFF globally.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def toggle_cmd(interaction: discord.Interaction):
    if interaction.user.id != 1285791141266063475:
        return await interaction.response.send_message("⛔ **Access Denied:** Only Master Admin Yaen can use the global kill switch.", ephemeral=True)
        
    current_status = get_config('engine_status', 'online')
    new_status = 'offline' if current_status == 'online' else 'online'
    await set_config('engine_status', new_status)
    
    if new_status == 'offline':
        await interaction.response.send_message("🛑 **Engine Offline:** YoAI has been put to sleep. It will completely ignore all messages until toggled back on.", ephemeral=False)
    else:
        await interaction.response.send_message("✅ **Engine Online:** YoAI is now awake and processing messages.", ephemeral=False)
    await status_loop()

@bot.tree.command(name="time", description="Set a global artificial delay for YoAI's responses (in seconds).")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def time_cmd(interaction: discord.Interaction, seconds: int):
    seconds = max(0, seconds)
    await set_config('response_delay', str(seconds))
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
    await set_config('current_model', model_name.value)
    await interaction.response.send_message(f"🧠 **Model Switched:** YoAI is now powered by `{model_name.name}`.")

@bot.tree.command(name="personality", description="Set a GLOBAL custom personality prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def personality(interaction: discord.Interaction, prompt: str):
    if prompt.strip().lower() == "default":
        await set_config('global_personality', 'default')
        await interaction.response.send_message("🌍 **Global Personality Reset:** YoAI has returned to default settings.")
    else:
        await set_config('global_personality', prompt.strip())
        await interaction.response.send_message(f"🌍 **Global Personality Updated:** YoAI will now act like this for everyone:\n`{prompt.strip()}`")

@bot.tree.command(name="clear", description="Wipe YoAI's memory for the current channel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def clear_cmd(interaction: discord.Interaction):
    await DB_CONN.execute("DELETE FROM message_history WHERE channel_id=?", (interaction.channel_id,))
    await DB_CONN.commit()
    await interaction.response.send_message("🧹 **Memory Wiped.** I have forgotten all recent context in this channel.")

@bot.tree.command(name="memory", description="Ask YoAI to analyze and summarize what it remembers about this channel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def memory_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    channel_id = interaction.channel_id
    
    async with DB_CONN.execute("SELECT author_id, content FROM message_history WHERE channel_id=? ORDER BY timestamp ASC", (channel_id,)) as cursor:
        history = await cursor.fetchall()
        
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
        analysis = await key_manager.generate_with_fallback(target_model, [analysis_prompt], sys_inst)
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

@bot.tree.command(name="setchannel", description="Allow YoAI to automatically read & reply to ALL messages here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def setchannel(interaction: discord.Interaction):
    await toggle_channel(interaction.guild_id, interaction.channel.id, True)
    await interaction.response.send_message(f"⚙️ **Activated:** YoAI System is now automatically listening to {interaction.channel.mention}")

@bot.tree.command(name="unsetchannel", description="Stop YoAI from automatically replying in this channel.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def unsetchannel(interaction: discord.Interaction):
    await toggle_channel(interaction.guild_id, interaction.channel.id, False)
    await interaction.response.send_message(f"❌ **Deactivated:** YoAI System is no longer automatically listening to {interaction.channel.mention}")

# -------------------- Clean On-Message Routing (No Double Execution) --------------------

async def process_channel_buffer(channel_id):
    global LAST_ALERT_TIME
    
    # 🔴 0.5s HYPER-DEBOUNCER
    await asyncio.sleep(0.5) 
    if channel_id not in CHANNEL_BUFFERS: return
    
    data = CHANNEL_BUFFERS.pop(channel_id)
    if channel_id in CHANNEL_TIMERS:
        del CHANNEL_TIMERS[channel_id]
        
    channel = data['channel']
    author = data['author']
    msg_obj = data['message']
    combined_content = "\n".join(data['content'])
    image_parts = data['attachments']
    
    try:
        await add_message_to_history(channel_id, msg_obj.id, author.id, combined_content or "[Sent an Image]", int(msg_obj.created_at.timestamp()))
        
        delay = float(get_config('response_delay', '0'))
        
        async with channel.typing():
            if delay > 0:
                await asyncio.sleep(delay)
                
            response = await generate_ai_response(channel, combined_content, author, image_parts)
            for i in range(0, len(response), 2000):
                await msg_obj.reply(response[i:i+2000], mention_author=False)
                
    except Exception as e:
        error_msg_str = str(e)
        await log_system_error(str(author), error_msg_str)
        
        # 🔴 THE "SAFE" PUBLIC ERROR: Protected by the 15-second anti-ban shield.
        now = time.time()
        if now - LAST_ALERT_TIME > 15.0:
            LAST_ALERT_TIME = now
            try:
                error_public = "There is an error.\nThe issue is sent to master admin yaen. The issue will be fixed soon, wait until yaen beats it up."
                await msg_obj.reply(error_public, mention_author=False)
            except discord.Forbidden:
                pass 
            
            try:
                app_info = await bot.application_info()
                admin_user = app_info.owner
                error_dm = (
                    f"⚠️ **YoAI System Alert: Critical Failure** ⚠️\n"
                    f"**Triggered By:** {author} (`{author.id}`)\n"
                    f"**Location:** {channel.mention if hasattr(channel, 'mention') else 'DMs'}\n"
                    f"**Error Trace:**\n```\n{error_msg_str}\n```"
                )
                await admin_user.send(error_dm)
            except Exception as dm_error:
                print(f"Failed to DM admin: {dm_error}")
            
    finally:
        if 'data' in locals(): del data
        if 'image_parts' in locals(): del image_parts
        if 'msg_obj' in locals(): del msg_obj
        if 'channel' in locals(): del channel
        if 'author' in locals(): del author
        if 'combined_content' in locals(): del combined_content

@bot.event
async def on_message(message: discord.Message):
    if not bot.user or message.author == bot.user: return
    
    engine_status = get_config('engine_status', 'online')
    if engine_status == 'offline': return

    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions or f'<@{bot.user.id}>' in message.content or f'<@!{bot.user.id}>' in message.content
    is_allowed = True if is_dm else await is_channel_allowed(message.guild.id, message.channel.id)

    if is_dm or is_mentioned or is_allowed:
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        if not clean_content and not message.attachments: 
            clean_content = "Hello! You pinged me?"

        channel_id = message.channel.id
        
        # 🔴 ONLY ADD TO BUFFER (Removed the bug that caused double API calls)
        if channel_id not in CHANNEL_BUFFERS:
            CHANNEL_BUFFERS[channel_id] = {
                'content': [],
                'attachments': [],
                'author': message.author,
                'channel': message.channel,
                'message': message
            }
            
        if clean_content:
            CHANNEL_BUFFERS[channel_id]['content'].append(clean_content)
            
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    if att.size > 4 * 1024 * 1024:
                        await message.reply("⚠️ Image too large. Please compress it under 4MB to save Engine Memory.", mention_author=False)
                        continue
                        
                    img_bytes = await att.read()
                    CHANNEL_BUFFERS[channel_id]['attachments'].append(types.Part.from_bytes(data=img_bytes, mime_type=att.content_type))
                    
        CHANNEL_BUFFERS[channel_id]['message'] = message 
        
        if channel_id in CHANNEL_TIMERS:
            CHANNEL_TIMERS[channel_id].cancel()
            
        CHANNEL_TIMERS[channel_id] = bot.loop.create_task(process_channel_buffer(channel_id))

    await bot.process_commands(message)

# -------------------- Quart Web Dashboard (ANIMATED GLASS UI) --------------------
app = Quart(__name__)
app.secret_key = FLASK_SECRET

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
            --glass: rgba(10, 10, 10, 0.4);
            --glass-border: rgba(255, 255, 255, 0.1);
            --text-main: #f3f4f6;
            --accent: #ff2a2a;
            --accent-glow: rgba(255, 42, 42, 0.5);
            --danger: #ef4444;
            --success: #10b981;
            --warning: #f59e0b;
        }
        body { 
            margin: 0; font-family: 'Space Grotesk', sans-serif; color: var(--text-main); 
            height: 100vh; overflow: hidden; display: flex; background-color: var(--bg-deep);
        }
        
        /* THE FLUID BACKGROUND ENGINE */
        #live-bg {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2;
            background: linear-gradient(135deg, #000000, #0a0000, #050000, #180000);
            background-size: 400% 400%;
            animation: liquidFlow 20s ease-in-out infinite;
        }
        .orb {
            position: fixed; border-radius: 50%; filter: blur(100px); z-index: -1;
            animation: float 25s infinite ease-in-out alternate;
        }
        .orb-1 { width: 55vw; height: 55vw; background: rgba(255, 42, 42, 0.09); top: -10%; left: -10%; }
        .orb-2 { width: 65vw; height: 65vw; background: rgba(200, 0, 0, 0.07); bottom: -20%; right: -10%; animation-delay: -5s; }
        
        @keyframes liquidFlow { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        @keyframes float { 0% { transform: translate(0, 0) scale(1); } 100% { transform: translate(5vw, 10vh) scale(1.1); } }

        /* APPLE-STYLE GLASSMORPHISM */
        .glass { 
            background: var(--glass); 
            backdrop-filter: blur(25px) saturate(120%); 
            -webkit-backdrop-filter: blur(25px) saturate(120%); 
            border: 1px solid var(--glass-border); 
            border-radius: 12px; 
            box-shadow: 0 15px 40px rgba(0,0,0,0.8); 
            position: relative;
            overflow: hidden;
        }
        .glass::before {
            content: ""; position: absolute; top: 0; left: 0; right: 0; height: 1px;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
            pointer-events: none;
        }

        .accent-text { color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 2px; text-shadow: 0 0 15px var(--accent-glow); }
        
        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; border-left: 4px solid var(--accent); overflow-y: auto; }
        .nav-tab { padding: 12px 15px; border-radius: 8px; cursor: pointer; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); font-weight: bold; text-transform: uppercase; letter-spacing: 1px; font-size: 0.9rem; border: 1px solid transparent; }
        .nav-tab:hover { background: rgba(255,255,255,0.05); transform: translateX(5px); }
        .nav-tab.active { background: rgba(255, 42, 42, 0.15); border-color: rgba(255, 42, 42, 0.4); color: #fff; box-shadow: 0 0 20px rgba(255, 42, 42, 0.2); }

        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; perspective: 1000px; }
        
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: auto; flex-direction: row; flex-wrap: wrap; padding: 15px; margin: 0; justify-content: center; gap: 8px; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 16px 16px 0 0; border-top: 1px solid var(--glass-border); border-left: none; backdrop-filter: blur(30px); }
            .nav-tab { padding: 8px 10px; font-size: 0.7rem; flex: 1 1 30%; text-align: center; }
            .nav-tab:hover { transform: none; }
            #content { padding: 25px; padding-bottom: 180px; margin: 0; }
            .hide-mobile { display: none; }
        }

        h1, h2 { margin-top: 0; }
        
        .card { 
            padding: 25px; margin-bottom: 25px; 
            transition: all 0.4s cubic-bezier(0.25, 0.8, 0.25, 1); 
        }
        .card:hover { 
            transform: translateY(-4px); 
            box-shadow: 0 20px 50px rgba(0,0,0,0.9), 0 0 20px rgba(255,42,42,0.1); 
            border-color: rgba(255,255,255,0.2); 
        }
        
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-label { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 2px; opacity: 0.5; }
        .stat-value { font-size: 3.5rem; font-weight: 300; margin-top: 10px; color: #fff; text-shadow: 0 0 20px rgba(255,255,255,0.1); }
        .stat-small { font-size: 1rem; opacity: 0.5; margin-top: 5px;}

        /* 🔴 NEW: SUPERCAR GAUGES (COCKPIT) 🔴 */
        .dash-meters { display: flex; justify-content: space-around; flex-wrap: wrap; gap: 30px; margin-bottom: 30px; }
        .meter-box {
            position: relative; width: 280px; height: 160px; display: flex; flex-direction: column; align-items: center;
            background: linear-gradient(180deg, rgba(20,0,0,0.8) 0%, rgba(0,0,0,0.9) 100%);
            border: 2px solid rgba(255,42,42,0.2); border-radius: 140px 140px 15px 15px;
            box-shadow: inset 0 20px 30px rgba(255,42,42,0.05), 0 15px 30px rgba(0,0,0,0.8);
            overflow: hidden;
        }
        .meter-track {
            position: absolute; top: 15px; width: 250px; height: 125px;
            border: 4px dashed rgba(255,42,42,0.4); border-radius: 125px 125px 0 0;
            border-bottom: none;
        }
        .meter-needle {
            position: absolute; bottom: 10px; left: 50%; width: 4px; height: 110px;
            background: linear-gradient(to top, transparent 10%, #ff2a2a 90%);
            transform-origin: bottom center; transform: translateX(-50%) rotate(-90deg);
            transition: transform 0.6s cubic-bezier(0.34, 1.56, 0.64, 1);
            box-shadow: 0 0 15px #ff2a2a; z-index: 2;
        }
        .meter-needle::after {
            content: ''; position: absolute; top: 0; left: -3px; width: 10px; height: 10px;
            background: #fff; border-radius: 50%; box-shadow: 0 0 10px #fff;
        }
        .meter-center {
            position: absolute; bottom: 0; left: 50%; transform: translateX(-50%);
            width: 30px; height: 30px; background: #111; border: 3px solid #ff2a2a;
            border-radius: 50%; z-index: 3; box-shadow: 0 0 10px #ff2a2a;
        }
        .meter-data {
            position: absolute; bottom: 20px; text-align: center; z-index: 4;
            background: rgba(0,0,0,0.7); padding: 2px 10px; border-radius: 6px; border: 1px solid rgba(255,42,42,0.3);
        }
        .meter-val { font-size: 1.8rem; font-weight: bold; color: #fff; font-family: monospace; text-shadow: 0 0 10px #ff2a2a; line-height: 1; }
        .meter-title { font-size: 0.7rem; text-transform: uppercase; color: #aaa; letter-spacing: 2px; }
        
        /* LIVE TERMINAL */
        #logs-container, #error-logs-container { max-height: 250px; overflow-y: auto; padding: 15px; background: rgba(0,0,0,0.6); border: 1px solid rgba(255,42,42,0.2); border-radius: 8px; box-shadow: inset 0 0 20px rgba(0,0,0,1); scroll-behavior: smooth; }
        pre { color: #ff5555; font-family: 'Courier New', Courier, monospace; font-size: 0.95rem; line-height: 1.6; text-shadow: 0 0 8px rgba(255,42,42,0.4); margin: 0; white-space: pre-wrap; word-wrap: break-word; }
        .cursor { display: inline-block; width: 10px; height: 1.2em; background-color: var(--accent); vertical-align: middle; animation: blink 1s step-end infinite; box-shadow: 0 0 10px var(--accent); }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

        label { display: block; margin-bottom: 8px; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px; opacity: 0.7; }
        input[type="text"], input[type="password"], textarea, select { 
            width: 100%; box-sizing: border-box; padding: 14px; margin-bottom: 20px; border-radius: 8px; 
            border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.6); color: white; 
            outline: none; font-family: 'Space Grotesk'; font-size: 1rem; transition: all 0.3s ease;
        }
        input:focus, textarea:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 20px var(--accent-glow); background: rgba(0,0,0,0.8); }
        textarea { resize: vertical; min-height: 120px; }
        
        button { padding: 15px 25px; border-radius: 8px; border: 1px solid rgba(255,42,42,0.4); background: rgba(255,42,42,0.15); color: #fff; font-weight: 700; text-transform: uppercase; cursor: pointer; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); letter-spacing: 1px; }
        button:hover { background: var(--accent); box-shadow: 0 0 30px var(--accent-glow); transform: translateY(-2px); }
        button:disabled { opacity: 0.5; cursor: not-allowed; background: transparent; border-color: #555; transform: none; box-shadow: none; }
        
        .btn-danger { background: rgba(239, 68, 68, 0.1); border: 1px solid var(--danger); color: var(--danger); }
        .btn-danger:hover { background: var(--danger); color: #fff; box-shadow: 0 0 30px rgba(239, 68, 68, 0.6); }
        .btn-success { background: rgba(16, 185, 129, 0.1); border: 1px solid var(--success); color: var(--success); }
        .btn-success:hover { background: var(--success); color: #fff; box-shadow: 0 0 30px rgba(16, 185, 129, 0.6); }
        
        .key-row { display: flex; justify-content: space-between; padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.05); transition: background 0.3s; }
        .key-row:hover { background: rgba(255,255,255,0.02); }
        .badge { padding: 6px 12px; border-radius: 6px; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; transition: all 0.5s ease; }

        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.95); display: flex; justify-content: center; align-items: center; z-index: 1000; backdrop-filter: blur(20px); }
        .login-box { padding: 50px; text-align: center; width: 340px; border-top: 4px solid var(--accent); }
        
        /* FLUID TAB ANIMATIONS */
        .hidden { display: none !important; }
        .visible-flex { display: flex !important; }
        .visible-block { display: block !important; }
        .animate-in { animation: slideFadeUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
        @keyframes slideFadeUp { from { opacity: 0; transform: translateY(30px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }

        /* 🔴 CREDITS FANCY CSS 🔴 */
        .glitch-name {
            font-size: 2.8rem; font-weight: 700; color: #fff; text-shadow: 0 0 20px var(--accent);
            position: relative; display: inline-block; letter-spacing: 2px; margin-bottom: 10px;
        }
        .fancy-name {
            font-size: 2rem; font-weight: bold; color: #cbd5e1;
            text-shadow: 0 0 15px rgba(255,255,255,0.4); font-style: italic; letter-spacing: 1px;
        }
        .feature-list { list-style: none; padding: 0; margin: 0; }
        .feature-list li {
            padding: 14px 20px; margin-bottom: 10px; background: rgba(255,255,255,0.03); 
            border-radius: 8px; border-left: 3px solid var(--accent); font-size: 1.05rem;
            opacity: 0; transform: translateX(-20px);
            animation: slideRightIn 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            animation-delay: var(--anim-delay);
        }
        @keyframes slideRightIn { to { opacity: 1; transform: translateX(0); } }
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
            
            <div class="nav-tab active" id="tab-cockpit" onclick="switchTab('cockpit')">Cockpit</div>
            <div class="nav-tab" id="tab-telemetry" onclick="switchTab('telemetry')">Telemetry</div>
            <div class="nav-tab" id="tab-diagnostics" onclick="switchTab('diagnostics')">Cluster Health</div>
            <div class="nav-tab" id="tab-tracker" onclick="switchTab('tracker')">Key Tracker</div>
            <div class="nav-tab" id="tab-trash" onclick="switchTab('trash')">System Janitor</div>
            <div class="nav-tab" id="tab-safety" onclick="switchTab('safety')">Safety Center</div>
            <div class="nav-tab" id="tab-errors" onclick="switchTab('errors')">Error Logs</div>
            <div class="nav-tab" id="tab-customization" onclick="switchTab('customization')">Customization</div>
            <div class="nav-tab" id="tab-admin" onclick="switchTab('admin')">Admin Panel</div>
            <div class="nav-tab" id="tab-credits" onclick="switchTab('credits')">Credits & Features</div>
            
            <div class="hide-mobile" style="margin-top: auto;">
                <button class="btn-danger" style="width: 100%; padding: 10px; font-size: 0.8rem;" onclick="logout()">Kill Switch</button>
            </div>
        </div>

        <div id="content">
            
            <div id="section-cockpit" class="visible-block animate-in">
                <h1 class="accent-text">Engine Cockpit</h1>
                <div class="card glass">
                    <div class="dash-meters">
                        
                        <div class="meter-box">
                            <div class="meter-track"></div>
                            <div class="meter-needle" id="needle-rpm"></div>
                            <div class="meter-center"></div>
                            <div class="meter-data">
                                <div class="meter-val" id="val-rpm">0</div>
                                <div class="meter-title">RPM</div>
                            </div>
                        </div>

                        <div class="meter-box">
                            <div class="meter-track"></div>
                            <div class="meter-needle" id="needle-rlpd"></div>
                            <div class="meter-center"></div>
                            <div class="meter-data">
                                <div class="meter-val" id="val-rlpd">0</div>
                                <div class="meter-title">RLPD</div>
                            </div>
                        </div>

                    </div>
                </div>
            </div>

            <div id="section-telemetry" class="hidden">
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
                <h1 class="accent-text">Cluster Health</h1>
                <div class="card glass">
                    <p style="opacity: 0.7; margin-bottom: 20px; line-height: 1.6;">Execute a high-performance ping across the load-balanced API array. Verifies the RPM and health of all connected Google nodes.</p>
                    <button id="diag-btn" onclick="runDiagnostics()">Initiate Deep Scan</button>
                    <div id="diag-results" style="margin-top: 30px; display: flex; flex-direction: column; background: rgba(0,0,0,0.5); border-radius: 8px; border: 1px solid var(--glass-border);"></div>
                </div>
            </div>

            <div id="section-tracker" class="hidden">
                <h1 class="accent-text">Key Tracker</h1>
                <div class="card glass">
                    <p style="opacity: 0.7; margin-bottom: 20px; line-height: 1.6;">Track the exact status of your custom-named API keys. Format in Render: <code>Name:Key, Backup:Key2</code></p>
                    <button id="tracker-btn" onclick="runDiagnostics()">Scan Named Keys</button>
                    <div id="tracker-results" style="margin-top: 30px; display: flex; flex-direction: column; background: rgba(0,0,0,0.5); border-radius: 8px; border: 1px solid var(--glass-border);"></div>
                </div>
            </div>

            <div id="section-trash" class="hidden">
                <h1 class="accent-text">System Janitor & Trash Analyzer</h1>
                <div class="stat-grid" style="margin-bottom: 25px;">
                    <div class="card glass stat-box">
                        <div class="stat-label">Allocated RAM</div>
                        <div class="stat-value" id="sys-ram">-</div>
                        <div class="stat-small">Megabytes</div>
                    </div>
                    <div class="card glass stat-box">
                        <div class="stat-label">SQLite Size</div>
                        <div class="stat-value" id="sys-db">-</div>
                        <div class="stat-small">Kilobytes</div>
                    </div>
                </div>
                
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 15px;">Manual Memory Overrides</h2>
                    <p style="font-size: 0.85rem; opacity: 0.6; margin-bottom: 25px;">Use these controls to forcefully purge dead objects from Python's memory buffer or defragment the SQLite database. Useful for preventing Render OOM crashes.</p>
                    
                    <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                        <button class="btn-success" onclick="forceSysGC()" id="btn-gc" style="flex: 1;">Force RAM Flush (GC)</button>
                        <button onclick="forceSysVacuum()" id="btn-vacuum" style="flex: 1;">Defragment DB (Vacuum)</button>
                    </div>
                    <div id="sys-trash-logs" style="margin-top: 20px; font-family: monospace; font-size: 0.85rem; color: var(--accent);"></div>
                </div>
            </div>

            <div id="section-safety" class="hidden">
                <h1 class="accent-text">Safety & Behavior Control</h1>
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 20px;">Google AI Harms Filters</h2>
                    <p style="font-size: 0.85rem; opacity: 0.6; margin-bottom: 20px;">Dynamically control the strictness of the AI's internal censorship filters. Note: "BLOCK_NONE" completely removes restrictions.</p>
                    
                    <div class="stat-grid" style="gap: 15px;">
                        <div>
                            <label>Hate Speech</label>
                            <select id="safety-hate">
                                <option value="BLOCK_NONE">Block None (Uncensored)</option>
                                <option value="BLOCK_ONLY_HIGH">Block Only High</option>
                                <option value="BLOCK_MEDIUM_AND_ABOVE">Block Medium & Above</option>
                                <option value="BLOCK_LOW_AND_ABOVE">Block Low & Above (Strict)</option>
                            </select>
                        </div>
                        <div>
                            <label>Harassment</label>
                            <select id="safety-harass">
                                <option value="BLOCK_NONE">Block None (Uncensored)</option>
                                <option value="BLOCK_ONLY_HIGH">Block Only High</option>
                                <option value="BLOCK_MEDIUM_AND_ABOVE">Block Medium & Above</option>
                                <option value="BLOCK_LOW_AND_ABOVE">Block Low & Above (Strict)</option>
                            </select>
                        </div>
                        <div>
                            <label>Sexually Explicit</label>
                            <select id="safety-explicit">
                                <option value="BLOCK_NONE">Block None (Uncensored)</option>
                                <option value="BLOCK_ONLY_HIGH">Block Only High</option>
                                <option value="BLOCK_MEDIUM_AND_ABOVE">Block Medium & Above</option>
                                <option value="BLOCK_LOW_AND_ABOVE">Block Low & Above (Strict)</option>
                            </select>
                        </div>
                        <div>
                            <label>Dangerous Content</label>
                            <select id="safety-danger">
                                <option value="BLOCK_NONE">Block None (Uncensored)</option>
                                <option value="BLOCK_ONLY_HIGH">Block Only High</option>
                                <option value="BLOCK_MEDIUM_AND_ABOVE">Block Medium & Above</option>
                                <option value="BLOCK_LOW_AND_ABOVE">Block Low & Above (Strict)</option>
                            </select>
                        </div>
                    </div>
                    <button onclick="saveSafetyConfig()" style="margin-top: 15px;">Deploy Safety Filters</button>
                    <span id="save-safety-status" style="margin-left: 15px; color: var(--success); display: none; font-weight: bold; text-shadow: 0 0 10px var(--success);">✅ Filters Synced!</span>
                </div>
            </div>

            <div id="section-errors" class="hidden">
                <h1 class="accent-text">System Error Logs</h1>
                <div class="card glass">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                        <p style="opacity: 0.7; margin: 0;">Persistent crash and exception telemetry securely stored in the database.</p>
                        <button class="btn-danger" style="padding: 8px 15px; font-size: 0.8rem;" onclick="clearErrors()">Purge Logs</button>
                    </div>
                    <div id="error-logs-container" style="max-height: 500px;">
                        <div id="error-content"></div>
                    </div>
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
                    <span id="save-cust-status" style="margin-left: 15px; color: var(--success); display: none; font-weight: bold; text-shadow: 0 0 10px var(--success);">✅ Synced!</span>
                </div>
            </div>

            <div id="section-admin" class="hidden">
                <h1 class="accent-text">Admin Control Panel</h1>
                <div class="card glass">
                    <h2 style="font-size: 1.2rem; margin-bottom: 20px;">Core Matrix Directives</h2>
                    <p style="font-size: 0.85rem; opacity: 0.6; margin-bottom: 20px;">This prompt is persistent and permanently locked into the database until you change it here.</p>
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
                    <span id="save-status" style="margin-left: 15px; color: var(--success); display: none; font-weight: bold; text-shadow: 0 0 10px var(--success);">✅ Config Secured!</span>
                </div>
                <div class="card glass" style="border-color: rgba(239, 68, 68, 0.3);">
                    <h2 style="font-size: 1.2rem; color: var(--danger); margin-bottom: 10px;">Danger Zone</h2>
                    <p style="font-size: 0.9rem; opacity: 0.7; margin-bottom: 20px;">Executing a hard reset will permanently incinerate the entire SQLite conversation history across all environments.</p>
                    <button class="btn-danger" onclick="nukeMemory()">Incinerate Memory Bank</button>
                </div>
            </div>
            
            <div id="section-credits" class="hidden">
                <h1 class="accent-text">Apex Architecture & Credits</h1>
                
                <div class="card glass" style="text-align: center; padding: 40px 20px;">
                    <h2 style="font-size: 0.9rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 4px; margin-bottom: 10px;">Master Architect & Developer</h2>
                    <div class="glitch-name">𝕸r_𝖄aen (Yathin)</div>
                    
                    <h2 style="font-size: 0.9rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 4px; margin-top: 50px; margin-bottom: 10px;">Lead Testing Partner</h2>
                    <div class="fancy-name">✨ ℜhys ✨</div>
                </div>

                <div class="card glass">
                    <h2 style="font-size: 1.2rem; border-bottom: 1px solid var(--glass-border); padding-bottom: 15px; margin-bottom: 20px;">Complete Feature List</h2>
                    <ul class="feature-list">
                        <li style="--anim-delay: 0.1s">⚡ <strong>Zero-Delay Direct Async Event Routing:</strong> Instantaneous 0.5s debouncer to catch messages perfectly without double-executing API calls.</li>
                        <li style="--anim-delay: 0.2s">⚖️ <strong>Round-Robin API Load Balancer:</strong> Flawlessly rotates Gemini keys to multiply RPM limits.</li>
                        <li style="--anim-delay: 0.3s">⏱️ <strong>Internal Stopwatch Tracking:</strong> Pre-emptively benches keys before Google triggers a Rate Limit.</li>
                        <li style="--anim-delay: 0.4s">🏎️ <strong>Supercar UI Gauges:</strong> Live CSS Tachometers visualizing RPM and RLPD in the Cockpit.</li>
                        <li style="--anim-delay: 0.5s">🗄️ <strong>Isolated aiosqlite Pipelines:</strong> Eliminates global "Database Locked" crashes using strict 15s connection timeouts.</li>
                        <li style="--anim-delay: 0.6s">🗑️ <strong>System Janitor (Trash Analyzer):</strong> Manual GC and SQLite Vacuum tools to prevent Render RAM spikes.</li>
                        <li style="--anim-delay: 0.7s">🛡️ <strong>Dynamic Harms Filter Center:</strong> Live frontend control over Google's censors (Default: Unshackled).</li>
                        <li style="--anim-delay: 0.8s">🧠 <strong>Background Auto-Summarizer:</strong> Seamlessly compresses context history.</li>
                        <li style="--anim-delay: 0.9s">💻 <strong>Live Typewriter Terminal:</strong> Animated backend signal logging.</li>
                        <li style="--anim-delay: 1.0s">🔐 <strong>Master Admin Lock:</strong> Core commands reject unauthorized users silently.</li>
                        <li style="--anim-delay: 1.1s">📸 <strong>Multi-Modal Vision Guard:</strong> Automatically rejects huge images (>4MB) from crashing the server.</li>
                        <li style="--anim-delay: 1.2s">🚨 <strong>Anti-Freeze API Shield:</strong> Forces a hard 30-second timeout on Google requests to prevent indefinite bot hangs.</li>
                        <li style="--anim-delay: 1.3s">🌐 <strong>Universal Add Command (/invite):</strong> Secure OAuth2 generation that automatically expels the bot if triggered inside an existing guild.</li>
                        <li style="--anim-delay: 1.4s">🛡️ <strong>Anti-Spam Discord Guard:</strong> Forces a 15-second cooldown on public crash alerts and Admin DMs to guarantee zero Cloudflare IP bans.</li>
                    </ul>
                </div>
            </div>
            
        </div>
    </div>

    <script>
        const bootSequence = [
            "[SYS] Apex Engine Initializing...",
            "[SYS] Matrix Connection Established.",
            "[SYS] CSS Liquid Glass Animations: MOUNTED.",
            "[SYS] Dynamic Safety Overrides: ONLINE.",
            "[SYS] Supercar Gauge UI: ACTIVE.",
            "[SYS] Memory Garbage Collector: SECURED.",
            "[SYS] Standing by for incoming signals."
        ];
        
        let bootLine = 0;
        let lastQueryCount = 0;
        let configLoaded = false; 
        
        let cooldownIntervals = {};

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
                    if(callback) setTimeout(callback, 50);
                }
            }, 30); 
        }

        function runBootSequence() {
            if (bootLine < bootSequence.length) {
                typeTerminalLine(bootSequence[bootLine], runBootSequence);
                bootLine++;
            }
        }

        function toggleUI(showDash) {
            document.getElementById('login-overlay').className = showDash ? 'glass hidden' : 'glass visible-flex';
            document.getElementById('dashboard-view').className = showDash ? 'visible-flex' : 'hidden';
            if(showDash) {
                document.getElementById('dashboard-view').style.display = 'flex';
                setTimeout(runBootSequence, 600); 
            } else {
                document.getElementById('dashboard-view').style.display = 'none';
            }
        }

        function switchTab(tab) {
            ['cockpit', 'telemetry', 'diagnostics', 'tracker', 'trash', 'safety', 'errors', 'customization', 'admin', 'credits'].forEach(t => {
                document.getElementById('tab-' + t).classList.remove('active');
                let section = document.getElementById('section-' + t);
                section.classList.replace('visible-block', 'hidden');
                section.classList.remove('animate-in'); 
            });
            
            document.getElementById('tab-' + tab).classList.add('active');
            let activeSection = document.getElementById('section-' + tab);
            activeSection.classList.replace('hidden', 'visible-block');
            
            void activeSection.offsetWidth; 
            activeSection.classList.add('animate-in');

            if((tab === 'admin' || tab === 'safety' || tab === 'customization') && !configLoaded) {
                fetchConfig();
            }
            if(tab === 'errors') fetchErrors();
            if(tab === 'trash') fetchSysInfo();
            if((tab === 'diagnostics' || tab === 'tracker') && document.getElementById('diag-results').innerHTML === "") {
                document.getElementById('diag-results').innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.3;'>Awaiting scan initialization...</div>";
                document.getElementById('tracker-results').innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.3;'>Awaiting scan initialization...</div>";
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
            else { 
                const err = document.getElementById('err');
                err.style.display = 'block';
                err.animate([{transform: 'translateX(-5px)'}, {transform: 'translateX(5px)'}, {transform: 'translateX(0)'}], {duration: 300});
            }
        }
        
        function updateGauges(rpm, total_queries) {
            const needleRpm = document.getElementById('needle-rpm');
            const valRpm = document.getElementById('val-rpm');
            if(needleRpm && valRpm) {
                valRpm.innerText = rpm;
                let rpmDeg = -90 + (Math.min(rpm, 100) / 100) * 180;
                needleRpm.style.transform = `translateX(-50%) rotate(${rpmDeg}deg)`;
            }
            
            const needleRlpd = document.getElementById('needle-rlpd');
            const valRlpd = document.getElementById('val-rlpd');
            if(needleRlpd && valRlpd) {
                valRlpd.innerText = total_queries;
                let maxRlpd = Math.max(1500, total_queries + 500); 
                let rlpdDeg = -90 + (total_queries / maxRlpd) * 180;
                needleRlpd.style.transform = `translateX(-50%) rotate(${rlpdDeg}deg)`;
            }
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
                
                updateGauges(data.rpm, data.total_queries);
                
                if (lastQueryCount !== 0 && data.total_queries > lastQueryCount) {
                    const time = new Date().toLocaleTimeString('en-US', { hour12: false });
                    const diff = data.total_queries - lastQueryCount;
                    document.getElementById('log-content').innerHTML += `[${time}] SIGNAL RECEIVED. Processing +${diff} data packets.<br>`;
                    document.getElementById('logs-container').scrollTop = document.getElementById('logs-container').scrollHeight;
                }
                lastQueryCount = data.total_queries;
                
                if(document.getElementById('tab-errors').classList.contains('active')) fetchErrors();
                if(document.getElementById('tab-trash').classList.contains('active')) fetchSysInfo();
                
            } catch (e) { console.log("Stats fetch error."); }
        }
        
        async function fetchSysInfo() {
            try {
                const res = await fetch('/api/sys_info');
                if (res.ok) {
                    const data = await res.json();
                    document.getElementById('sys-ram').innerText = data.mem_mb !== null ? data.mem_mb : "N/A";
                    document.getElementById('sys-db').innerText = data.db_kb;
                }
            } catch (e) { console.log(e); }
        }
        
        async function forceSysGC() {
            const btn = document.getElementById('btn-gc');
            btn.disabled = true;
            btn.innerText = "Flushing RAM...";
            try {
                const res = await fetch('/api/sys_gc', { method: 'POST' });
                const data = await res.json();
                const log = document.getElementById('sys-trash-logs');
                const time = new Date().toLocaleTimeString('en-US', { hour12: false });
                log.innerHTML = `[${time}] RAM Flush Complete. Annihilated ${data.collected} dead memory objects.`;
                fetchSysInfo();
            } catch(e) {}
            btn.innerText = "Force RAM Flush (GC)";
            btn.disabled = false;
        }
        
        async function forceSysVacuum() {
            const btn = document.getElementById('btn-vacuum');
            btn.disabled = true;
            btn.innerText = "Vacuuming...";
            try {
                await fetch('/api/sys_vacuum', { method: 'POST' });
                const log = document.getElementById('sys-trash-logs');
                const time = new Date().toLocaleTimeString('en-US', { hour12: false });
                log.innerHTML = `[${time}] Database Vacuum Complete. SQLite sectors defragmented.`;
                fetchSysInfo();
            } catch(e) {}
            btn.innerText = "Defragment DB (Vacuum)";
            btn.disabled = false;
        }

        async function fetchErrors() {
            try {
                const res = await fetch('/api/errors');
                if (res.ok) {
                    const data = await res.json();
                    const container = document.getElementById('error-content');
                    
                    if (data.errors.length === 0) {
                        container.innerHTML = "<div style='text-align: center; color: var(--success); opacity: 0.7; padding: 20px;'>[SYS] No critical errors detected. System operating nominally.</div>";
                        return;
                    }
                    
                    let html = '';
                    data.errors.forEach(e => {
                        html += `
                        <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid rgba(255,0,0,0.2);">
                            <div style="color: var(--danger); font-weight: bold; font-family: monospace; font-size: 0.9rem;">[${e.time}] TRIGGERED BY: ${e.user}</div>
                            <pre style="color: #ffb3b3; font-size: 0.85rem; margin-top: 5px; white-space: pre-wrap;">${e.trace}</pre>
                        </div>`;
                    });
                    container.innerHTML = html;
                }
            } catch (e) { console.log("Error fetch failed"); }
        }
        
        async function clearErrors() {
            if(confirm("Purge all error logs permanently?")) {
                await fetch('/api/clear_errors', { method: 'POST' });
                fetchErrors();
            }
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
                    
                    document.getElementById('safety-hate').value = data.safety_hate;
                    document.getElementById('safety-harass').value = data.safety_harassment;
                    document.getElementById('safety-explicit').value = data.safety_explicit;
                    document.getElementById('safety-danger').value = data.safety_dangerous;
                    
                    configLoaded = true; 
                }
            } catch (e) { console.log("Config fetch error."); }
        }

        function startCooldownTimer(prefix, index, unlockTime) {
            const detailId = `detail-${prefix}-${index}`;
            const badgeId = `badge-${prefix}-${index}`;
            
            if (cooldownIntervals[`${prefix}-${index}`]) {
                clearInterval(cooldownIntervals[`${prefix}-${index}`]);
            }
            
            const timer = setInterval(() => {
                const detailEl = document.getElementById(detailId);
                const badgeEl = document.getElementById(badgeId);
                
                if (!detailEl || !badgeEl) {
                    clearInterval(timer);
                    return;
                }
                
                const nowSec = Date.now() / 1000;
                const timeLeft = Math.ceil(unlockTime - nowSec);
                
                if (timeLeft <= 0) {
                    clearInterval(timer);
                    detailEl.innerText = "Healthy & Ready";
                    badgeEl.innerText = "ONLINE";
                    badgeEl.style.background = "#10b98115";
                    badgeEl.style.color = "#10b981";
                    badgeEl.style.borderColor = "#10b98140";
                } else {
                    detailEl.innerText = `Rate Limited (Auto-Retry in ${timeLeft}s)`;
                }
            }, 1000);
            
            cooldownIntervals[`${prefix}-${index}`] = timer;
        }

        async function runDiagnostics() {
            const btn1 = document.getElementById('diag-btn');
            const btn2 = document.getElementById('tracker-btn');
            if(btn1) btn1.disabled = true;
            if(btn2) btn2.disabled = true;
            if(btn1) btn1.innerText = "Scanning...";
            if(btn2) btn2.innerText = "Scanning...";
            
            Object.values(cooldownIntervals).forEach(clearInterval);
            cooldownIntervals = {};
            
            const resDiv1 = document.getElementById('diag-results');
            const resDiv2 = document.getElementById('tracker-results');
            
            resDiv1.innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.6;'>Sending packets to Google...</div>";
            resDiv2.innerHTML = "<div style='padding: 20px; text-align: center; opacity: 0.6;'>Sending packets to Google...</div>";

            try {
                const res = await fetch('/api/diagnostics', { method: 'POST' });
                if (!res.ok) throw new Error('Request failed');
                const data = await res.json();
                
                resDiv1.innerHTML = "";
                resDiv2.innerHTML = "";
                
                data.results.forEach((node) => {
                    const row1 = document.createElement('div');
                    row1.className = 'key-row';
                    row1.innerHTML = `
                        <div>
                            <div class="key-name">Node ${node.index}: ${node.masked_key}</div>
                            <div id="detail-diag-${node.index}" style="font-size: 0.8rem; opacity: 0.5; margin-top: 5px;">${node.detail}</div>
                        </div>
                        <div id="badge-diag-${node.index}" class="badge" style="background: ${node.color}15; color: ${node.color}; border: 1px solid ${node.color}40;">
                            ${node.status}
                        </div>
                    `;
                    resDiv1.appendChild(row1);
                    
                    const row2 = document.createElement('div');
                    row2.className = 'key-row';
                    row2.innerHTML = `
                        <div>
                            <div class="key-name" style="color: #fff;">[ ${node.name} ]</div>
                            <div style="font-family: monospace; font-size: 0.85rem; color: #cbd5e1; margin-top: 5px;">${node.masked_key}</div>
                            <div id="detail-trk-${node.index}" style="font-size: 0.8rem; opacity: 0.5; margin-top: 5px;">${node.detail}</div>
                        </div>
                        <div id="badge-trk-${node.index}" class="badge" style="background: ${node.color}15; color: ${node.color}; border: 1px solid ${node.color}40;">
                            ${node.status}
                        </div>
                    `;
                    resDiv2.appendChild(row2);
                    
                    if (node.status === "COOLDOWN" && node.unlock_time > 0) {
                        startCooldownTimer('diag', node.index, node.unlock_time);
                        startCooldownTimer('trk', node.index, node.unlock_time);
                    }
                });
            } catch (e) {
                resDiv1.innerHTML = `<div style='padding: 20px; text-align: center; color: var(--danger);'>Diagnostic scan failed.</div>`;
                resDiv2.innerHTML = `<div style='padding: 20px; text-align: center; color: var(--danger);'>Diagnostic scan failed.</div>`;
            }
            if(btn1) { btn1.innerText = "Initiate Deep Scan"; btn1.disabled = false; }
            if(btn2) { btn2.innerText = "Scan Named Keys"; btn2.disabled = false; }
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
                setTimeout(() => status.style.display = 'none', 2500);
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
                setTimeout(() => status.style.display = 'none', 2500);
            }
        }
        
        async function saveSafetyConfig() {
            const payload = {
                safety_hate: document.getElementById('safety-hate').value,
                safety_harassment: document.getElementById('safety-harass').value,
                safety_explicit: document.getElementById('safety-explicit').value,
                safety_dangerous: document.getElementById('safety-danger').value
            };
            const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (res.ok) {
                const status = document.getElementById('save-safety-status');
                status.style.display = 'inline';
                setTimeout(() => status.style.display = 'none', 2500);
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
            document.getElementById('log-content').innerHTML = ''; 
            bootLine = 0;
            configLoaded = false;
            
            Object.values(cooldownIntervals).forEach(clearInterval);
            cooldownIntervals = {};
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

@app.route('/')
async def index(): 
    return await render_template_string(HTML_TEMPLATE)

@app.route('/login', methods=['POST'])
async def login():
    data = await request.get_json()
    if data and data.get('password') == "mr_yaen":
        session['logged_in'] = True
        return jsonify(success=True)
    return jsonify(success=False), 401

@app.route('/logout', methods=['POST'])
async def logout():
    session.pop('logged_in', None)
    return jsonify(success=True)

@app.route('/api/stats')
async def api_stats():
    global QUERY_TIMESTAMPS
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
    
    try: db_size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)
    except: db_size_kb = 0
    
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        async with db.execute("SELECT COUNT(*) FROM message_history") as cursor:
            rows = (await cursor.fetchone())[0]
            
    current_model = get_config('current_model', 'gemini-2.5-flash-lite')
    
    now = time.time()
    QUERY_TIMESTAMPS[:] = [ts for ts in QUERY_TIMESTAMPS if now - ts < 60.0]
    current_rpm = len(QUERY_TIMESTAMPS)
        
    return jsonify({"uptime": uptime_str, "total_queries": TOTAL_QUERIES, "active_memory_rows": rows, "db_size": db_size_kb, "current_model": current_model, "rpm": current_rpm})

@app.route('/api/sys_info')
async def api_sys_info():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    mem_mb = None
    if resource:
        mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mem_mb = round(mem_kb / 1024.0, 2)
    
    try: db_size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)
    except: db_size_kb = 0
    return jsonify(mem_mb=mem_mb, db_kb=db_size_kb)

@app.route('/api/sys_gc', methods=['POST'])
async def api_sys_gc():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    collected = gc.collect()
    return jsonify(success=True, collected=collected)

@app.route('/api/sys_vacuum', methods=['POST'])
async def api_sys_vacuum():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    try:
        async with aiosqlite.connect(DB_PATH, isolation_level=None, timeout=15.0) as db_vac:
            await db_vac.execute("VACUUM")
        return jsonify(success=True)
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

@app.route('/api/diagnostics', methods=['POST'])
async def api_diagnostics():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    results = await key_manager.run_diagnostics()
    return jsonify(success=True, results=results)

@app.route('/api/config', methods=['GET', 'POST'])
async def api_config():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    
    if request.method == 'GET':
        return jsonify({
            "system_prompt": get_config('system_prompt', 'You are YoAI, a highly intelligent assistant.'),
            "current_model": get_config('current_model', 'gemini-2.5-flash-lite'),
            "status_type": get_config('status_type', 'watching'),
            "status_text": get_config('status_text', 'over the Matrix'),
            "safety_hate": get_config('safety_hate', 'BLOCK_NONE'),
            "safety_harassment": get_config('safety_harassment', 'BLOCK_NONE'),
            "safety_explicit": get_config('safety_explicit', 'BLOCK_NONE'),
            "safety_dangerous": get_config('safety_dangerous', 'BLOCK_NONE')
        })
    
    if request.method == 'POST':
        data = await request.get_json()
        for k in data:
            await set_config(k, data[k])
        return jsonify(success=True)

@app.route('/api/errors')
async def api_errors():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        async with db.execute("SELECT timestamp, user, trace FROM system_errors ORDER BY timestamp DESC") as cursor:
            rows = await cursor.fetchall()
    
    errors = [{"time": datetime.datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d %H:%M:%S'), "user": r[1], "trace": r[2]} for r in rows]
    return jsonify(errors=errors)

@app.route('/api/clear_errors', methods=['POST'])
async def api_clear_errors():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("DELETE FROM system_errors")
        await db.commit()
    return jsonify(success=True)

@app.route('/api/nuke', methods=['POST'])
async def api_nuke():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("DELETE FROM message_history")
        await db.commit()
    return jsonify(success=True)

if __name__ == "__main__":
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
