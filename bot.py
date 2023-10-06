import json
import asyncio
from pathlib import Path
from typing import TypedDict, Optional
import subprocess
import discord
from discord import app_commands
from discord.ext import tasks
import db
import sqlite3
import datetime

class Config(TypedDict):
    guild_id: str
    token: int
    queue_manager_roles: list

def get_config() -> Config:
    """
    Reads configuration from config file
    """
    DEFAULT_CONFIG = Path(__file__).parent.joinpath("default_config.json")
    USER_CONFIG = Path(__file__).parent.joinpath("config.json")
    with open(DEFAULT_CONFIG) as f:
        config = json.load(f)
    if USER_CONFIG.is_file():
        with open(USER_CONFIG) as f:
            user_config = json.load(f)
        for k,v in user_config.items():
            config[k] = v
    return config


# read the config file and initialize the db
config = get_config()
conn = db.set_up_database()
current_queue = ""
current_position = 0
max_position = 0

def get_current_and_max_position(queuename: str) -> (int, int):
    """Finds the current and max position of the active queue"""
    query = "SELECT currentpos, maxpos FROM queues WHERE name = ?;"
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (queuename,))
        return cursor.fetchone()

def increment_cur_pos(queuename: str):
    """Increment the current position of the active queue"""
    query= "UPDATE queues SET currentpos = ? WHERE name = ?;"
    curpos, _ = get_current_and_max_position(queuename)
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (curpos+1, current_queue))
        cursor.close()

def is_karaoke_operator(user):
    """Determine if a user is authorized to manage the bot"""
    return not set([int(id) for id in config["operator_roles"]]).intersection(set([role.id for role in user.roles])) == set()

# setting up the bot
class aclient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.synced = False

    async def on_ready(self):
        await self.wait_until_ready()
        self.botchannel = client.get_channel(int(config["channel_id"]))
        if not self.synced:
            tree.copy_global_to(guild=discord.Object(id=config["guild_id"]))
            await tree.sync(guild=discord.Object(id=config["guild_id"]))
            self.synced = True

client = aclient()
tree = app_commands.CommandTree(client)

@tasks.loop(seconds = 5)
async def playbackLoop():
    """The main loop to check for a new song and play it"""
    # There is no active queue
    if current_queue == "":
        return
    
    curr_index, max_index = get_current_and_max_position(current_queue)
    if max_index <= curr_index:
        return # There are no more songs in the queue
    
    # Fetch the current song and play it
    with conn:
        cursor = conn.cursor()
        query = "SELECT * FROM songs WHERE position = ?;"
        cursor.execute(query, (curr_index,))
        row = cursor.fetchone()
        column_names = [description[0] for description in cursor.description]
        if row:
            # Create a dictionary using column names and row values
            current_song = {column_names[i]: row[i] for i in range(len(column_names))}
        else:
            print("Some kind of error ocurred fetching the next song")
        
        # If the song was revoked, skip it
        if current_song["is_revoked"]:
            increment_cur_pos(current_queue)
            return
        notification_message = f"<@{str(current_song['discord_user_id'])}>, it is now your turn to sing {current_song['title']}"
        if current_song["collaborators"]:
            notification_message+=f" with {current_song['collaborators']}"
        if current_song["lyrics_url"]:
            notification_message+=f"\nLyrics: {current_song['lyrics_url']}"
        await client.botchannel.send(notification_message)
        # Now try to play it
        try:
            process = subprocess.Popen(["mpv","-fs","-pause","--ytdl-raw-options=format-sort=res:1080",current_song["url"]])
            while process.poll() is None:
                await asyncio.sleep(1)
        except Exception as e:
            print("Unable to launch mpv and play current song")
        finished_at = datetime.datetime.now()

        # Now update the db such that the song is completed
        query= "UPDATE songs SET completed_time = ? WHERE position = ?;"
        with conn:
            cursor = conn.cursor()
            cursor.execute(query, (finished_at, current_song["position"]))
            cursor.close()
        increment_cur_pos(current_queue)

    


# command to initialize a queue
@tree.command(name="set-queue", description="Creates a new empty karaoke queue or switches to queue of specified name")
async def initialize(interaction: discord.Interaction, queue_name: str):
    global current_queue
    """Creates or switches to specific karaoke queue by name"""
    if not is_karaoke_operator(interaction.user):
        await interaction.response.send_message(f'Cannot set queue, permission denied')
        return
    writequery = "INSERT INTO queues (name, currentpos, maxpos, discord_guild_id, time_created) VALUES (?,?,?,?,?)"
    created_at = datetime.datetime.now()
        # check if queue exists
    result = get_current_and_max_position(queue_name)
    if result:
        current_position, max_position = result
        await interaction.response.send_message(f"Fetched queue {queue_name} with current current_position {current_position} and max current_position {max_position}")
    else:
        with conn:
            cursor = conn.cursor()
            cursor.execute(writequery, (queue_name, 0, 0, config["guild_id"], created_at))
            await interaction.response.send_message(f'New queue {queue_name} created!')
    current_queue = queue_name
    if not playbackLoop.is_running():
        playbackLoop.start()
    

@tree.command(name="listqueues", description="Lists existing queues")
async def listqueues(interaction: discord.Interaction):
    """Lists all existing queues"""
    query = "SELECT name, time_created FROM queues"
    with conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        cursor.close()
    await interaction.response.send_message("Current queues: \n" + '\n'.join(["{0}, created on {1}".format(row[0], row[1]) for row in rows]))

@tree.command(name="addsong", description="Add a song to the queue")
async def addsong(interaction: discord.Interaction, song_url: str, lyrics_url: Optional[str], collaborators: Optional[str], notes: Optional[str]):
    """Adds a song to the queue"""
    global current_queue
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return
    with conn:
        cursor = conn.cursor()
    
        # First, ensure the user is allowed to queue
        if not is_karaoke_operator(interaction.user):
            cursor.execute("SELECT COUNT(*) FROM songs WHERE is_revoked = FALSE AND completed_time IS NULL;")
            
            currently_queued_by_user = cursor.fetchone()[0]
            if currently_queued_by_user >= int(config["max_queued_per_user"]):
                await interaction.response.send_message(f"You currently already have {currently_queued_by_user} songs queued. Either swap an existing one or wait until you go next before queuing again")
                cursor.close()
                return
        # Try getting metadata with yt-dlp (for video sites)
        await interaction.response.defer() # sometimes takes more than 3 seconds
        try:
            dl_output: str = subprocess.run(["yt-dlp", "--no-playlist", "--print", '{"title":%(title)j,"duration":"%(duration)j"}', song_url], text=True, capture_output=True, encoding="utf8").stdout
            video_metadata = json.loads(dl_output)
            if video_metadata["duration"] == "NA":
                raise Exception("Could not fetch video duration from yt-dlp")
        except Exception as e: #try with ffprobe (if file directly)
            try:
                ffprobe_output = json.loads(str(subprocess.run(["ffprobe", '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', song_url], text=True, capture_output=True, encoding="utf8").stdout))
                tags = {tag.lower():v for tag,v in ffprobe_output.get('format', {}).get('tags', {}).items()}
                video_metadata = {"title": tags["title"], "duration": int(float(ffprobe_output.get('format', {})["duration"]))}

            except Exception as e:
                await interaction.followup.send("There was an error parsing the specified URL. Please double-check it")
                return
        query = "INSERT INTO songs (url, title, duration, added_time, lyrics_url, notes, position, collaborators, completed_time, is_revoked, discord_user_id, discord_guild_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        created_at = datetime.datetime.now()
        try:
            _, max_position = get_current_and_max_position(current_queue)
            cursor.execute(query, (song_url, video_metadata['title'], int(video_metadata['duration']), created_at, lyrics_url, notes, max_position, collaborators, None, False, interaction.user.id, config["guild_id"]))

        except Exception as e:
            await interaction.followup.send("There was an error adding the song to the database. Is it a duplicate?")
            return
        max_position+=1
        # save the new position 
        updatequery = "UPDATE queues SET maxpos = ? WHERE name = ?;"
        cursor.execute(updatequery, (max_position, current_queue))
        cursor.close()
    await interaction.followup.send(f"Added song {video_metadata['title']}\n{song_url}")



client.run(config["token"])