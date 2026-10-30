import os
import re
import discord
from discord.ext import commands
from google import genai
from github import Github

# Initialize APIs from environment variables
github_client = Github(os.getenv("GITHUB_TOKEN"))
repo = github_client.get_repo(os.getenv("GITHUB_REPO"))
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Setup Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

MEMORY_DIR = "memory"

def get_repo_memory_context():
    """Fetches all .md files strictly from the memory/ directory."""
    context_text = ""
    try:
        contents = repo.get_contents(MEMORY_DIR)
        while contents:
            file_content = contents.pop(0)
            if file_content.type == "dir":
                contents.extend(repo.get_contents(file_content.path))
            elif file_content.name.endswith(".md"):
                decoded_text = file_content.decoded_content.decode("utf-8")
                # Remove the 'memory/' prefix from display path for cleaner prompts
                clean_path = file_content.path.replace(f"{MEMORY_DIR}/", "")
                context_text += f"\n--- FILE: {clean_path} ---\n{decoded_text}\n"
    except Exception as e:
        print(f"Error fetching memory directory: {e}")
    
    return context_text

def save_to_memory(filename, new_content):
    """Creates or updates a Markdown file strictly inside the memory/ directory."""
    # Ensure filename ends with .md
    if not filename.endswith(".md"):
        filename += ".md"
    
    # Clean user path and enforce memory/ folder prefix
    clean_filename = os.path.basename(filename)
    target_path = f"{MEMORY_DIR}/{clean_filename}"
    commit_message = f"Update {clean_filename} via Discord Memory Bot"
    
    try:
        # Check if file exists to append content
        existing_file = repo.get_contents(target_path)
        existing_text = existing_file.decoded_content.decode("utf-8")
        updated_content = f"{existing_text.strip()}\n\n{new_content.strip()}"
        repo.update_file(existing_file.path, commit_message, updated_content, existing_file.sha)
        return f"Updated `{clean_filename}` in memory repository."
    except Exception:
        # Create new file if it doesn't exist
        repo.create_file(target_path, commit_message, new_content.strip())
        return f"Created `{clean_filename}` in memory repository."

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}!")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, (discord.Thread, discord.TextChannel)):
        async with message.channel.typing():
            user_text = message.content.strip()

            # Command format: "save: domacnost | Washing machine model XYZ"
            # Or default fallback: "save: Washing machine model XYZ" (saves to memory/notes.md)
            if user_text.lower().startswith(("save:", "zapis:", "zapíš:")):
                try:
                    payload = re.sub(r"^(save:|zapis:|zapíš:)", "", user_text, flags=re.IGNORECASE).strip()
                    if "|" in payload:
                        target_file, note = payload.split("|", 1)
                    else:
                        target_file, note = "notes.md", payload
                    
                    result_msg = save_to_memory(target_file.strip(), note.strip())
                    await message.channel.send(f"✅ {result_msg}")
                    return
                except Exception as e:
                    await message.channel.send(f"❌ Failed to save: {str(e)}")
                    return

            # Default: Query memory context and respond
            memory_context = get_repo_memory_context()
            system_instruction = (
                "You are an intelligent personal memory assistant. "
                "Use the provided notes from the user's private memory repository to answer queries accurately."
            )
            prompt = f"MEMORY CONTEXT:\n{memory_context}\n\nUSER QUESTION:\n{user_text}"

            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"system_instruction": system_instruction}
            )

            await message.channel.send(response.text)

bot.run(os.getenv("DISCORD_TOKEN"))
