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

def get_repo_memory_context():
    """Recursively fetches all .md files from the memory repository."""
    context_text = ""
    try:
        # We start checking the root of the memory repository
        contents = repo.get_contents("")
        while contents:
            file_content = contents.pop(0)
            if file_content.type == "dir":
                # Recursively fetch contents of subdirectories
                contents.extend(repo.get_contents(file_content.path))
            elif file_content.name.endswith(".md"):
                decoded_text = file_content.decoded_content.decode("utf-8")
                context_text += f"\n--- FILE: {file_content.path} ---\n{decoded_text}\n"
    except Exception as e:
        print(f"Error fetching repository contents: {e}")
    
    return context_text

def save_to_memory(target_path, new_content):
    """Creates or updates a Markdown file at any path (e.g., domacnost/pracka.md)."""
    # Ensure path ends with .md
    if not target_path.endswith(".md"):
        target_path += ".md"
    
    # Clean up leading slashes if any
    target_path = target_path.lstrip("/")
    commit_message = f"Update {target_path} via Discord Memory Bot"
    
    try:
        # Check if file exists to append content
        existing_file = repo.get_contents(target_path)
        existing_text = existing_file.decoded_content.decode("utf-8")
        updated_content = f"{existing_text.strip()}\n\n{new_content.strip()}"
        repo.update_file(existing_file.path, commit_message, updated_content, existing_file.sha)
        return f"Updated `{target_path}` in memory repository."
    except Exception:
        # Create new file (GitHub API automatically creates intermediate directories if needed)
        repo.create_file(target_path, commit_message, new_content.strip())
        return f"Created `{target_path}` in memory repository."

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

            # Command format: "save: domacnost/pracka | Washing machine model XYZ is on the attic"
            # Or default fallback: "save: Washing machine model XYZ" (saves to notes.md)
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
                model="gemini-3.8-flash",
                contents=prompt,
                config={"system_instruction": system_instruction}
            )

            await message.channel.send(response.text)

bot.run(os.getenv("DISCORD_TOKEN"))
