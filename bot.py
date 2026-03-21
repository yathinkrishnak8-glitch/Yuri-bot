import discord
from discord import app_commands
from discord.ext import commands, tasks
from quart import Quart, request, session, jsonify, render_template_string
import asyncpg
import os
import random
import time
import asyncio
import datetime
import re
import gc  
import requests

# NEW GOOGLE SDK
from google import genai
from google.genai import types

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
QUERY_TIMESTAMPS = [] 

# Cloud PostgreSQL Pool
DB_POOL = None
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("⚠️ WARNING: DATABASE_URL is missing! You must add your Supabase URL in Render.")

# ASYNC TRAFFIC CONTROLLERS (Cloudflare 1015 Shields)
ALERT_LOCK = asyncio.Lock()
LAST_ALERT_TIME = 0.0

CONFIG_CACHE = {}
CHANNEL_BUFFERS = {}
CHANNEL_TIMERS = {}

GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

FLASK_SECRET = os.environ.get("FLASK_SECRET", "yoai_persistent_secret_key_123")
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Cloud Database Setup (Supabase) --------------------
async def init_db():
    global DB_POOL
    if DB_POOL is None:
        # Connect to Supabase
        DB_POOL = await asyncpg.create_pool(DATABASE_URL)
        
    async with DB_POOL.acquire() as conn:
        # Build PostgreSQL Tables natively
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS allowed_channels (guild_id BIGINT, channel_id BIGINT, PRIMARY KEY (guild_id, channel_id));
            CREATE TABLE IF NOT EXISTS message_history (
                channel_id BIGINT, message_id BIGINT PRIMARY KEY, author_id BIGINT,
                content TEXT, timestamp BIGINT
            );
            CREATE TABLE IF NOT EXISTS system_errors (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION,
                user_name TEXT,
                trace TEXT
            );
        """)
        
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
            await conn.execute("INSERT INTO config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", k, v)
            
        rows = await conn.fetch("SELECT key, value FROM config")
        for row in rows:
            CONFIG_CACHE[row['key']] = row['value']

def get_config(key: str, default: str) -> str:
    return CONFIG_CACHE.get(key, default)

async def set_config(key: str, value: str):
    CONFIG_CACHE[key] = value
    async with DB_POOL.acquire() as conn:
        await conn.execute("INSERT INTO config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)

async def log_system_error(user: str, trace: str):
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("INSERT INTO system_errors (timestamp, user_name, trace) VALUES ($1, $2, $3)", time.time(), user, trace)
            await conn.execute("DELETE FROM system_errors WHERE id NOT IN (SELECT id FROM system_errors ORDER BY id DESC LIMIT 50)")
    except Exception as e:
        print(f"CRITICAL DB LOGGING ERROR: {e}")

# -------------------- Smart Cluster Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.key_objects = [{'index': i+1, 'name': k.split(":", 1)[0].strip() if ":" in k and not k.startswith("AIza") else f"Node {i+1}", 'key': k.split(":", 1)[1].strip() if ":" in k and not k.startswith("AIza") else k.strip()} for i, k in enumerate(keys) if k.strip()]
        self.key_mapping = {obj['key']: f"{obj['name']} ({obj['key'][:8]}...)" for obj in self.key_objects}
        self.all_keys = [obj['key'] for obj in self.key_objects]
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
                results.append({"index": obj['index'], "name": obj['name'], "masked_key": masked_key, "status": "ONLINE", "detail": "Healthy & Ready", "unlock_time": 0, "color": "#10b981"})
            except Exception as e:
                error_msg = str(e).lower()
                async with self.lock:
                    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg or "timeout" in error_msg:
                        cooldown_time = float(re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg).group(1)) + 2.0 if re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg) else 60.0
                        unlock_epoch = now + cooldown_time
                        self.key_cooldowns[key] = unlock_epoch
                        self.key_usage[key] = []
                        results.append({"index": obj['index'], "name": obj['name'], "masked_key": masked_key, "status": "COOLDOWN", "detail": f"Rate Limited or Timeout", "unlock_time": unlock_epoch, "color": "#f59e0b"})
                    else:
                        self.dead_keys.add(key)
                        results.append({"index": obj['index'], "name": obj['name'], "masked_key": masked_key, "status": "DEAD", "detail": "Invalid Key", "unlock_time": 0, "color": "#ef4444"})
        return results

    async def generate_with_fallback(self, target_model: str, contents: list, system_instruction: str = None) -> str:
        fallback_models = list(dict.fromkeys([target_model, 'gemini-2.5-flash-lite', 'gemini-2.5-flash']))
        last_error = None
        dynamic_safety = self.get_dynamic_safety()
        
        for model_name in fallback_models:
            async with self.lock:
                now = time.time()
                available_keys = []
                for k in self.all_keys:
                    self.key_usage[k] = [ts for ts in self.key_usage[k] if now - ts < 60.0]
                    if k not in self.dead_keys and self.key_cooldowns[k] <= now and len(self.key_usage[k]) < 14:
                        available_keys.append(k)
                if not available_keys: raise Exception("Cascade Failure: All keys are exhausted (RPM Limit) or dead.")
                selected_keys = []
                for _ in range(len(self.all_keys)):
                    k = self.all_keys[self.current_key_idx % len(self.all_keys)]
                    self.current_key_idx = (self.current_key_idx + 1) % len(self.all_keys)
                    if k in available_keys: selected_keys.append(k)
            
            for key in selected_keys:
                try:
                    async with self.lock: self.key_usage[key].append(time.time())
                    client = genai.Client(api_key=key)
                    config = types.GenerateContentConfig(system_instruction=system_instruction, safety_settings=dynamic_safety)
                    response = await asyncio.wait_for(client.aio.models.generate_content(model=model_name, contents=contents, config=config), timeout=30.0)
                    return response.text
                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()
                    async with self.lock:
                        if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg or "timeout" in error_msg:
                            self.key_cooldowns[key] = time.time() + (float(re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg).group(1)) + 2.0 if re.search(r'retry in (\d+(?:\.\d+)?)s', error_msg) else 60.0)
                            self.key_usage[key] = []
                        elif "400" in error_msg or "403" in error_msg or "invalid" in error_msg:
                            self.dead_keys.add(key)
                    continue
        raise Exception(f"Cascade Failure Details: {str(last_error)}")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Background Memory Compression --------------------
async def background_summarize(channel_id, oldest):
    texts = [f"User ID {row['author_id']}: {row['content']}" for row in oldest if row['author_id'] != 0]
    if not texts: return
    
    try:
        summary_text = await key_manager.generate_with_fallback('gemini-2.5-flash-lite', [f"Summarize concisely:\n{chr(10).join(texts)}"])
    except:
        summary_text = "[Summary unavailable]"
        
    oldest_ids = [row['message_id'] for row in oldest]
    timestamp = oldest[0]['timestamp']
    fake_msg_id = -int(time.time() * 1000) # Prevents Postgres ID Conflicts
    
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("DELETE FROM message_history WHERE message_id = ANY($1::bigint[])", oldest_ids)
            await conn.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES ($1, $2, 0, $3, $4)", channel_id, fake_msg_id, summary_text, timestamp)
    except Exception as e:
        print(f"Compression DB Error: {e}")

# -------------------- Helper Functions --------------------
async def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    if guild_id is None: return True
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_channels WHERE guild_id=$1 AND channel_id=$2", guild_id, channel_id)
        return row is not None

async def toggle_channel(guild_id: int, channel_id: int, enable: bool):
    async with DB_POOL.acquire() as conn:
        if enable:
            await conn.execute("INSERT INTO allowed_channels (guild_id, channel_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, channel_id)
        else:
            await conn.execute("DELETE FROM allowed_channels WHERE guild_id=$1 AND channel_id=$2", guild_id, channel_id)

async def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) 
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (message_id) DO UPDATE SET content = EXCLUDED.content
        """, channel_id, message_id, author_id, content, timestamp)
        count = await conn.fetchval("SELECT COUNT(*) FROM message_history WHERE channel_id=$1", channel_id)
        
    if count > 15:
        async with DB_POOL.acquire() as conn:
            oldest = await conn.fetch("SELECT message_id, author_id, content, timestamp FROM message_history WHERE channel_id=$1 ORDER BY timestamp ASC, message_id ASC LIMIT 10", channel_id)
        if oldest:
            bot.loop.create_task(background_summarize(channel_id, oldest))

def clean_discord_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c.isspace()).strip()
    return cleaned if cleaned else "User"

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        print(f"[SYS] Engine online. Boot Auto-Sync disabled. Use !sync to register slash commands.")
        await init_db()

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
    print("[SYS] Initiating Postgres Maintenance...")
    try:
        seven_days_ago = int(time.time()) - 604800
        async with DB_POOL.acquire() as conn:
            await conn.execute("DELETE FROM message_history WHERE timestamp < $1", seven_days_ago)
        gc.collect()
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

# -------------------- Slash Commands --------------------
class InfoView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(discord.ui.Button(label="Open Web Dashboard", style=discord.ButtonStyle.link, url="https://yoai.onrender.com"))

@bot.tree.command(name="info", description="Bot statistics and control panel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime = str(datetime.timedelta(seconds=int(time.time() - START_TIME))).split(".")[0]
    current_model = get_config('current_model', 'gemini-2.5-flash-lite')
    stats = await key_manager.get_stats()
    key_health = f"{stats['active']} Active | {stats['cooldown']} CD | {stats['dead']} Dead"
    
    embed = discord.Embed(title="🏎️ YoAI | Apex Engine 7.0 (Postgres)", color=0xff2a2a, description="Cloud-Brain Asynchronous Matrix System")
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Active Engine", value=f"`{current_model}`", inline=True)
    embed.add_field(name="Cluster Health", value=f"`{key_health}`", inline=True)
    embed.add_field(name="Total AI Queries", value=str(TOTAL_QUERIES), inline=True)
    embed.add_field(name="Architect", value="**mr_yaen (Yathin)**", inline=True)
    
    await interaction.response.send_message(embed=embed, view=InfoView())

@bot.tree.command(name="invite", description="Get the bot's invite link")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def invite_cmd(interaction: discord.Interaction):
    invite_url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=0&scope=bot%20applications.commands"
    if interaction.guild:
        await interaction.response.send_message(f"👋 Leaving Matrix sector. Invite: {invite_url}")
        await interaction.guild.leave()
    else:
        await interaction.response.send_message(f"🔗 Invite: {invite_url}")

@bot.tree.command(name="toggle", description="[ADMIN] Toggle the Engine ON/OFF.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def toggle_cmd(interaction: discord.Interaction):
    if interaction.user.id != 1285791141266063475: return await interaction.response.send_message("⛔ Access Denied.", ephemeral=True)
    new_status = 'offline' if get_config('engine_status', 'online') == 'online' else 'online'
    await set_config('engine_status', new_status)
    await interaction.response.send_message(f"⚙️ Engine is now **{new_status.upper()}**.")
    await status_loop()

@bot.tree.command(name="time", description="Set artificial delay for responses.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def time_cmd(interaction: discord.Interaction, seconds: int):
    seconds = max(0, seconds)
    await set_config('response_delay', str(seconds))
    await interaction.response.send_message(f"⏱️ Delay set to {seconds} seconds.")

@bot.tree.command(name="model", description="Change AI model.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(model_name=[
    app_commands.Choice(name="Gemini 2.5 Flash Lite", value="gemini-2.5-flash-lite"),
    app_commands.Choice(name="Gemini 2.5 Flash", value="gemini-2.5-flash"),
    app_commands.Choice(name="Gemini 2.5 Pro", value="gemini-2.5-pro")
])
async def model_cmd(interaction: discord.Interaction, model_name: app_commands.Choice[str]):
    await set_config('current_model', model_name.value)
    await interaction.response.send_message(f"🧠 Model switched to `{model_name.name}`.")

@bot.tree.command(name="personality", description="Set custom personality")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def personality(interaction: discord.Interaction, prompt: str):
    await set_config('global_personality', prompt.strip())
    await interaction.response.send_message(f"🌍 Personality Updated.")

@bot.tree.command(name="clear", description="Wipe memory for current channel.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def clear_cmd(interaction: discord.Interaction):
    async with DB_POOL.acquire() as conn:
        await conn.execute("DELETE FROM message_history WHERE channel_id=$1", interaction.channel_id)
    await interaction.response.send_message("🧹 Memory Wiped.")

@bot.tree.command(name="memory", description="Analyze chat memory.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def memory_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    async with DB_POOL.acquire() as conn:
        history = await conn.fetch("SELECT author_id, content FROM message_history WHERE channel_id=$1 ORDER BY timestamp ASC", interaction.channel_id)
    if not history: return await interaction.followup.send("🧠 Memory Bank empty.")
    
    ctx_str = "\n".join([f"{'System' if r['author_id']==0 else 'User'}: {r['content']}" for r in history])
    try:
        sys_inst = "Analyze this chat memory. Give a brief, analytical overview."
        analysis = await key_manager.generate_with_fallback('gemini-2.5-flash-lite', [ctx_str], sys_inst)
        await interaction.followup.send(f"🧠 **Analysis:**\n{analysis}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Extraction failed: {e}")

@bot.tree.command(name="hack", description="Prank hacking sequence")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    searches = random.sample(["how to pretend i know python", "anime waifu tier list", "free robux legit no virus"], k=3)
    msg = await interaction.followup.send(f"💻 `Attacking {user.display_name}'s mainframe...`")
    await asyncio.sleep(1.5)
    await msg.edit(content=f"**[CLASSIFIED LEAK]**\n" + "\n".join([f"- `{s}`" for s in searches]))

@bot.tree.command(name="setchannel", description="Auto-reply to all messages here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def setchannel(interaction: discord.Interaction):
    await toggle_channel(interaction.guild_id, interaction.channel.id, True)
    await interaction.response.send_message(f"⚙️ Activated in {interaction.channel.mention}")

@bot.tree.command(name="unsetchannel", description="Stop auto-replying here.")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def unsetchannel(interaction: discord.Interaction):
    await toggle_channel(interaction.guild_id, interaction.channel.id, False)
    await interaction.response.send_message(f"❌ Deactivated in {interaction.channel.mention}")

# -------------------- Event Router --------------------
async def generate_ai_response(channel, user_message, author, image_parts=None):
    global TOTAL_QUERIES, QUERY_TIMESTAMPS
    TOTAL_QUERIES += 1
    QUERY_TIMESTAMPS.append(time.time())

    async with DB_POOL.acquire() as conn:
        history = await conn.fetch("SELECT author_id, content FROM message_history WHERE channel_id=$1 ORDER BY timestamp DESC, message_id DESC LIMIT 10", channel.id)
            
    history = list(history)
    history.reverse() 
    
    ctx_lines = []
    chars = 0
    for row in reversed(history):
        line = f"{'System' if row['author_id']==0 else 'User'}: {row['content']}\n"
        if chars + len(line) > 3000: break
        ctx_lines.insert(0, line)
        chars += len(line)
        
    ctx_str = "[SYSTEM: Recent history]\n" + "".join(ctx_lines) + f"\nReply to new message: {user_message}"

    payload = [ctx_str]
    if image_parts: payload.extend(image_parts)

    system = get_config('system_prompt', 'You are YoAI.')
    personality = get_config('global_personality', 'default')
    if personality != "default": system += f"\n[GLOBAL PERSONALITY]: {personality}"
    system += "\nCRITICAL: Respond DIRECTLY to the user. DO NOT output a chat transcript."

    return await key_manager.generate_with_fallback(get_config('current_model', 'gemini-2.5-flash-lite'), payload, system)

async def process_channel_buffer(channel_id):
    global LAST_ALERT_TIME
    await asyncio.sleep(1.5) # The Debouncer
    if channel_id not in CHANNEL_BUFFERS: return
    data = CHANNEL_BUFFERS.pop(channel_id)
    if channel_id in CHANNEL_TIMERS: del CHANNEL_TIMERS[channel_id]
        
    try:
        combined_content = "\n".join(data['content'])
        await add_message_to_history(channel_id, data['message'].id, data['author'].id, combined_content or "[Image]", int(time.time()))
        
        delay = float(get_config('response_delay', '0'))
        if delay > 0: await asyncio.sleep(delay)
            
        response = await generate_ai_response(data['channel'], combined_content, data['author'], data['attachments'])
        for i in range(0, len(response), 2000):
            await data['message'].reply(response[i:i+2000], mention_author=False)
            await asyncio.sleep(1.0) 
            
    except Exception as e:
        await log_system_error(str(data['author']), str(e))
        async with ALERT_LOCK:
            now = time.time()
            if now - LAST_ALERT_TIME > 15.0:
                LAST_ALERT_TIME = now
                try: await data['message'].reply("There is an error. Yaen is notified.", mention_author=False)
                except: pass
                try:
                    app_info = await bot.application_info()
                    await app_info.owner.send(f"⚠️ **Error Trace:**\n```\n{e}\n```")
                except: pass

@bot.event
async def on_message(message: discord.Message):
    if not bot.user or message.author == bot.user: return
    if get_config('engine_status', 'online') == 'offline': return

    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions
    is_allowed = True if is_dm else await is_channel_allowed(message.guild.id, message.channel.id)

    if is_dm or is_mentioned or is_allowed:
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').strip()
        if not clean_content and not message.attachments: clean_content = "Hello!"
        
        channel_id = message.channel.id
        if channel_id not in CHANNEL_BUFFERS:
            CHANNEL_BUFFERS[channel_id] = {'content': [], 'attachments': [], 'author': message.author, 'channel': message.channel, 'message': message}
            
        if clean_content: CHANNEL_BUFFERS[channel_id]['content'].append(clean_content)
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    if att.size > 4 * 1024 * 1024: continue
                    CHANNEL_BUFFERS[channel_id]['attachments'].append(types.Part.from_bytes(data=await att.read(), mime_type=att.content_type))
                    
        if channel_id in CHANNEL_TIMERS: CHANNEL_TIMERS[channel_id].cancel()
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
    <title>YoAI | Supabase Command</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg-deep: #000; --glass: rgba(10, 10, 10, 0.4); --glass-border: rgba(255, 255, 255, 0.1); --text-main: #f3f4f6; --accent: #ff2a2a; --danger: #ef4444; --success: #10b981; }
        body { margin: 0; font-family: 'Space Grotesk'; color: var(--text-main); height: 100vh; overflow: hidden; display: flex; background: var(--bg-deep); }
        #live-bg { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2; background: linear-gradient(135deg, #000, #0a0000, #050000, #180000); background-size: 400% 400%; animation: flow 20s ease-in-out infinite; }
        .orb { position: fixed; border-radius: 50%; filter: blur(100px); z-index: -1; animation: float 25s infinite ease-in-out alternate; }
        .orb-1 { width: 55vw; height: 55vw; background: rgba(255, 42, 42, 0.09); top: -10%; left: -10%; }
        @keyframes flow { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        @keyframes float { 100% { transform: translate(5vw, 10vh) scale(1.1); } }
        .glass { background: var(--glass); backdrop-filter: blur(25px); border: 1px solid var(--glass-border); border-radius: 12px; box-shadow: 0 15px 40px rgba(0,0,0,0.8); }
        .accent-text { color: var(--accent); font-weight: 700; text-transform: uppercase; }
        #nav { width: 260px; padding: 25px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; border-left: 4px solid var(--accent); }
        .nav-tab { padding: 12px; border-radius: 8px; cursor: pointer; font-weight: bold; text-transform: uppercase; transition: 0.3s; }
        .nav-tab:hover { background: rgba(255,255,255,0.05); transform: translateX(5px); }
        .nav-tab.active { background: rgba(255, 42, 42, 0.15); border-left: 3px solid var(--accent); color: #fff; }
        #content { flex-grow: 1; padding: 40px; overflow-y: auto; z-index: 10; }
        .card { padding: 25px; margin-bottom: 25px; }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; }
        .stat-box { text-align: center; padding: 30px 20px; }
        .stat-value { font-size: 3rem; font-weight: 300; color: #fff; margin-top: 10px; }
        .dash-meters { display: flex; justify-content: space-around; flex-wrap: wrap; gap: 30px; margin-bottom: 30px; }
        .meter-box { position: relative; width: 280px; height: 160px; display: flex; flex-direction: column; align-items: center; background: #050505; border: 2px solid rgba(255,42,42,0.2); border-radius: 140px 140px 15px 15px; overflow: hidden; }
        .meter-needle { position: absolute; bottom: 10px; left: 50%; width: 4px; height: 110px; background: var(--accent); transform-origin: bottom center; transform: translateX(-50%) rotate(-90deg); transition: 1s cubic-bezier(0.34, 1.56, 0.64, 1); box-shadow: 0 0 15px var(--accent); }
        .meter-data { position: absolute; bottom: 20px; text-align: center; }
        .meter-val { font-size: 1.8rem; font-weight: bold; }
        button { padding: 15px 25px; border-radius: 8px; border: 1px solid var(--accent); background: rgba(255,42,42,0.15); color: #fff; font-weight: 700; text-transform: uppercase; cursor: pointer; transition: 0.3s; width: 100%; margin-top:10px; }
        button:hover { background: var(--accent); transform: translateY(-2px); }
        input, select, textarea { width: 100%; box-sizing: border-box; padding: 14px; margin-bottom: 20px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.6); color: white; outline: none; font-family: 'Space Grotesk'; }
        .hidden { display: none !important; }
        .visible-block { display: block !important; }
        #login-overlay { position: fixed; top:0; left:0; width:100%; height:100%; background: rgba(0,0,0,0.95); display: flex; justify-content: center; align-items: center; z-index: 1000; }
        .login-box { padding: 50px; text-align: center; width: 340px; border-top: 4px solid var(--accent); }
        .glitch-name { font-size: 2.8rem; font-weight: 700; color: #fff; text-shadow: 0 0 20px var(--accent); margin-bottom: 10px; }
        .fancy-name { font-size: 2rem; font-weight: bold; color: #cbd5e1; font-style: italic; }
    </style>
</head>
<body>
    <div id="live-bg"></div><div class="orb orb-1"></div>
    <div id="login-overlay" class="glass">
        <div class="login-box glass">
            <h1 class="accent-text" style="font-size:2.2rem; margin-bottom:5px;">Apex Command</h1>
            <p style="font-size:0.75rem; letter-spacing:3px; opacity:0.4;">Ignition Protocol</p>
            <input type="password" id="pwd" placeholder="Ignition Code">
            <button onclick="login()">Engage Engine</button>
            <p id="err" class="hidden" style="color:var(--danger); margin-top:20px;">Ignition Failed.</p>
        </div>
    </div>
    <div id="dashboard-view" class="hidden" style="width:100%; height:100%;">
        <div id="nav" class="glass">
            <div><h2 class="accent-text" style="margin:0;">YoAI</h2><div style="opacity:0.4; font-size:0.75rem;">Postgres Engine</div></div>
            <div class="nav-tab active" id="tab-cockpit" onclick="switchTab('cockpit')">Cockpit</div>
            <div class="nav-tab" id="tab-telemetry" onclick="switchTab('telemetry')">Telemetry</div>
            <div class="nav-tab" id="tab-diag" onclick="switchTab('diag')">Cluster Health</div>
            <div class="nav-tab" id="tab-admin" onclick="switchTab('admin')">Admin Config</div>
            <div class="nav-tab" id="tab-credits" onclick="switchTab('credits')">Architecture</div>
            <button style="border-color:var(--danger); color:var(--danger); background:transparent; margin-top:auto;" onclick="logout()">Kill Switch</button>
        </div>
        <div id="content">
            <div id="section-cockpit" class="visible-block">
                <h1 class="accent-text">Supabase Cockpit</h1>
                <div class="card glass">
                    <div class="dash-meters">
                        <div class="meter-box">
                            <div class="meter-needle" id="needle-rpm"></div>
                            <div class="meter-data"><div class="meter-val" id="val-rpm">0</div><div style="font-size:0.7rem; color:#aaa;">RPM</div></div>
                        </div>
                        <div class="meter-box">
                            <div class="meter-needle" id="needle-rlpd"></div>
                            <div class="meter-data"><div class="meter-val" id="val-rlpd">0</div><div style="font-size:0.7rem; color:#aaa;">RLPD</div></div>
                        </div>
                    </div>
                </div>
            </div>
            <div id="section-telemetry" class="hidden">
                <h1 class="accent-text">Postgres Telemetry</h1>
                <div class="stat-grid">
                    <div class="card glass stat-box"><div style="opacity:0.5">Uptime</div><div class="stat-value" id="uptime">-</div></div>
                    <div class="card glass stat-box"><div style="opacity:0.5">DB Rows</div><div class="stat-value" id="memory">-</div></div>
                    <div class="card glass stat-box"><div style="opacity:0.5">DB Weight (KB)</div><div class="stat-value" id="db-size">-</div></div>
                </div>
            </div>
            <div id="section-diag" class="hidden">
                <h1 class="accent-text">Cluster Health</h1>
                <div class="card glass">
                    <button id="diag-btn" onclick="runDiag()">Initiate Scan</button>
                    <div id="diag-results" style="margin-top:30px;"></div>
                </div>
            </div>
            <div id="section-admin" class="hidden">
                <h1 class="accent-text">Matrix Directives</h1>
                <div class="card glass">
                    <label>System Prompt</label><textarea id="admin-prompt"></textarea>
                    <label>AI Model</label>
                    <select id="admin-model">
                        <option value="gemini-2.5-flash-lite">Gemini 2.5 Flash Lite</option>
                        <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
                        <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
                    </select>
                    <button onclick="saveConfig()">Deploy Config to Supabase</button>
                </div>
                <div class="card glass" style="border-color:rgba(239,68,68,0.3);">
                    <button style="border-color:var(--danger); color:var(--danger); background:transparent;" onclick="nukeMemory()">Incinerate Cloud Memory</button>
                </div>
            </div>
            <div id="section-credits" class="hidden">
                <h1 class="accent-text">Cloud Architecture</h1>
                <div class="card glass" style="text-align:center; padding:40px;">
                    <div class="glitch-name">𝕸r_𝖄aen (Yathin)</div>
                    <p style="opacity:0.6; margin-bottom:40px;">Master Architect & Developer</p>
                    <div class="fancy-name">✨ ℜhys ✨</div>
                    <p style="opacity:0.6;">Lead Testing Partner</p>
                </div>
            </div>
        </div>
    </div>
    <script>
        function toggleUI(show) {
            document.getElementById('login-overlay').className = show ? 'hidden' : 'glass';
            document.getElementById('dashboard-view').className = show ? 'visible-block' : 'hidden';
            if(show) { document.getElementById('dashboard-view').style.display = 'flex'; }
        }
        function switchTab(t) {
            ['cockpit','telemetry','diag','admin','credits'].forEach(id => {
                document.getElementById('tab-'+id).classList.remove('active');
                document.getElementById('section-'+id).classList.replace('visible-block', 'hidden');
            });
            document.getElementById('tab-'+t).classList.add('active');
            document.getElementById('section-'+t).classList.replace('hidden', 'visible-block');
            if(t === 'admin') fetchConfig();
        }
        async function login() {
            const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:document.getElementById('pwd').value})});
            if(r.ok) { toggleUI(true); fetchStats(); setInterval(fetchStats, 3000); fetchConfig(); }
            else { document.getElementById('err').classList.remove('hidden'); }
        }
        async function logout() { await fetch('/logout', {method:'POST'}); toggleUI(false); document.getElementById('pwd').value=''; }
        
        function updateGauges(rpm, total) {
            document.getElementById('val-rpm').innerText = rpm;
            document.getElementById('needle-rpm').style.transform = `translateX(-50%) rotate(${-90 + (Math.min(rpm, 100)/100)*180}deg)`;
            document.getElementById('val-rlpd').innerText = total;
            document.getElementById('needle-rlpd').style.transform = `translateX(-50%) rotate(${-90 + (Math.min(total, 2000)/2000)*180}deg)`;
        }
        async function fetchStats() {
            try {
                if(document.getElementById('dashboard-view').classList.contains('hidden')) return;
                const r = await fetch('/api/stats');
                if(!r.ok) throw new Error('Unauth');
                const d = await r.json();
                document.getElementById('uptime').innerText = d.uptime;
                document.getElementById('memory').innerText = d.rows;
                document.getElementById('db-size').innerText = d.db_size;
                updateGauges(d.rpm, d.total);
            } catch(e) {}
        }
        async function runDiag() {
            document.getElementById('diag-btn').innerText = 'Scanning...';
            document.getElementById('diag-results').innerHTML = 'Sending packets to Google...';
            const r = await fetch('/api/diag', {method:'POST'});
            const d = await r.json();
            document.getElementById('diag-results').innerHTML = d.results.map(n => `<div style="padding:15px; border-bottom:1px solid #333; display:flex; justify-content:space-between;"><div><b>[${n.name}]</b> ${n.masked_key}<br><small style="opacity:0.5">${n.detail}</small></div><div style="color:${n.color}; font-weight:bold">${n.status}</div></div>`).join('');
            document.getElementById('diag-btn').innerText = 'Initiate Scan';
        }
        async function fetchConfig() {
            const r = await fetch('/api/config');
            if(r.ok) {
                const d = await r.json();
                document.getElementById('admin-prompt').value = d.system_prompt;
                document.getElementById('admin-model').value = d.current_model;
            }
        }
        async function saveConfig() {
            await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({system_prompt:document.getElementById('admin-prompt').value, current_model:document.getElementById('admin-model').value})});
            alert('Config Deployed to Supabase!');
        }
        async function nukeMemory() {
            if(confirm("Incinerate all Cloud Postgres Memory?")) { await fetch('/api/nuke', {method:'POST'}); alert("Wiped."); fetchStats(); }
        }
        window.onload = async () => { const r = await fetch('/api/stats'); if(r.ok) { toggleUI(true); fetchStats(); setInterval(fetchStats, 3000); fetchConfig(); } };
    </script>
</body>
</html>
"""

@app.route('/')
async def index(): return await render_template_string(HTML_TEMPLATE)

@app.route('/login', methods=['POST'])
async def login():
    if (await request.get_json()).get('password') == "mr_yaen":
        session['logged_in'] = True
        return jsonify(success=True)
    return jsonify(success=False), 401

@app.route('/logout', methods=['POST'])
async def logout():
    session.pop('logged_in', None)
    return jsonify(success=True)

@app.route('/api/stats')
async def api_stats():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    now = time.time()
    QUERY_TIMESTAMPS[:] = [ts for ts in QUERY_TIMESTAMPS if now - ts < 60.0]
    
    rows, db_size = 0, 0
    if DB_POOL:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetchval("SELECT COUNT(*) FROM message_history")
            try: db_size = round((await conn.fetchval("SELECT pg_database_size(current_database())")) / 1024, 1)
            except: pass
            
    return jsonify({"uptime": str(datetime.timedelta(seconds=int(time.time() - START_TIME))).split(".")[0], "total": TOTAL_QUERIES, "rows": rows, "db_size": db_size, "rpm": len(QUERY_TIMESTAMPS)})

@app.route('/api/diag', methods=['POST'])
async def api_diag():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    return jsonify(success=True, results=await key_manager.run_diagnostics())

@app.route('/api/config', methods=['GET', 'POST'])
async def api_config():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    if request.method == 'GET':
        return jsonify({
            "system_prompt": get_config('system_prompt', 'You are YoAI, a highly intelligent assistant.'),
            "current_model": get_config('current_model', 'gemini-2.5-flash-lite')
        })
    data = await request.get_json()
    for k, v in data.items(): await set_config(k, v)
    return jsonify(success=True)

@app.route('/api/nuke', methods=['POST'])
async def api_nuke():
    if not session.get('logged_in'): return jsonify(error="Unauthorized"), 401
    async with DB_POOL.acquire() as conn: await conn.execute("DELETE FROM message_history")
    return jsonify(success=True)

# -------------------- Survival Startup Logic --------------------
async def main():
    if not DATABASE_URL:
        print("❌ FATAL: You did not add DATABASE_URL to Render Environment Variables. The bot will crash.")
        return

    # 1. Immediate Port Bind (Keeps Render Happy)
    print(f"[BOOT] Binding Dash to Port {PORT}...")
    asyncio.create_task(app.run_task(host="0.0.0.0", port=PORT))
    
    # 2. Wait 10s to let old Cloudflare limits expire
    print("[BOOT] Cloudflare evasion cooldown (10s)...")
    await asyncio.sleep(10)
    
    # 3. Connect to Supabase
    print("[BOOT] Connecting to Supabase Cloud Brain...")
    await init_db()
    
    # 4. Engine Start
    print("[BOOT] Igniting Discord Connection...")
    while True:
        try:
            await bot.start(os.environ.get("DISCORD_BOT_TOKEN"))
        except Exception as e:
            if "1015" in str(e) or "429" in str(e):
                print("🛑 [BANNED] Waiting 60s for Cloudflare ban to lift...")
                await asyncio.sleep(60)
            else:
                print(f"⚠️ [CRASH] Restarting in 10s. Error: {e}")
                await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
