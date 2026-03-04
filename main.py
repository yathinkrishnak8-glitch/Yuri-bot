import discord
import os
import google.generativeai as genai
from keep_alive import keep_alive

# 1. Setup Gemini AI
genai.configure(api_key=os.environ.get("GEMINI_TOKEN"))

# 2. Give the AI a personality for your server
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction="You are a cool, casual Discord bot named Yuri. You play Free Fire Max, enjoy chatting about anime like Attack on Titan and Bleach, and find the philosophy of weapons fascinating. Keep your answers short and fun for a Discord chat."
)

# 3. Setup Discord Intents (so it can read messages)
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    await client.change_presence(activity=discord.Game(name="Free Fire Max"))

@client.event
async def on_message(message):
    # Stop the bot from replying to itself
    if message.author == client.user:
        return

    # Only reply if someone @mentions the bot
    if client.user in message.mentions:
        # Clean up the message so the AI doesn't read its own ID number
        user_message = message.content.replace(f'<@{client.user.id}>', '').strip()
        
        if not user_message:
            await message.channel.send("What's up? You mentioned me!")
            return

        try:
            # Shows the "Yuri is typing..." animation in Discord
            async with message.channel.typing():
                response = model.generate_content(user_message)
                # Send the AI's reply back to the server
                await message.reply(response.text)
        except Exception as e:
            await message.reply("Whoops, my brain glitched for a second. Try again!")
            print(e)

keep_alive()
client.run(os.environ.get('TOKEN'))
