import json
import datetime
import asyncio
from pathlib import Path
from typing import TypedDict, Optional
import subprocess

import discord
from discord import app_commands
from discord.ext import tasks
import db

class Config(TypedDict):
    guild_id: str
    token: int
    queue_manager_roles: list


def get_config() -> Config:
    """
    Reads configuration from config file
    """
    default_config_path = Path(__file__).parent.joinpath("default_config.json")
    user_config_path = Path(__file__).parent.joinpath("config.json")
    with open(default_config_path) as f:
        global_config = json.load(f)
    if user_config_path.is_file():
        with open(user_config_path) as f:
            user_config = json.load(f)
        for k, v in user_config.items():
            global_config[k] = v
    return global_config


# read the config file and initialize the db
config = get_config()
conn = db.set_up_database()
current_queue = ""


def get_song_metadata(song_url: str) -> Optional[dict]:
    """
    This obtains a dictionary containing, currently, the title and duration of a queued song.
    It first tries yt-dlp, and upon failure to find the title and duration, tries ffmpeg (for direct links to media files)
    """
    try:
        downloader = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--print",
                '{"title":%(title)j,"duration":"%(duration)j"}',
                song_url,
            ],
            text=True,
            capture_output=True,
            encoding="utf8",
            check=True,
        )
        dl_output = downloader.stdout
        video_metadata = json.loads(dl_output)
        if video_metadata["duration"] == "NA" or video_metadata["title"] == "NA":
            # aise Exception("Could not fetch video duration from yt-dlp")
            raise Exception("yt-dlp failure", downloader.stderr)
        print(video_metadata)
        video_metadata["duration"] = int(
            float(video_metadata["duration"])
        )  # some sties like niconico return decimal durations
        return video_metadata
    except Exception as ytdlp_failure:  # try with ffprobe (if file directly)
        try:
            ffprober = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    song_url,
                ],
                text=True,
                capture_output=True,
                encoding="utf8",
                check=True,
            )
            ffprobe_output = json.loads(str(ffprober.stdout))
            print(ffprober.stdout)
            # sometimes, the tags are part of the stream block and sometimes part of the format block. Here we check both and merge the result
            tags = {
                tag.lower(): v
                for d in [
                    ffprobe_output.get("format", {}).get("tags", {}),
                    ffprobe_output.get("streams", [{}])[0].get("tags", {}),
                ]
                for tag, v in d.items()
            }
            video_metadata = {
                "title": tags["title"],
                "duration": int(float(ffprobe_output.get("format", {})["duration"])),
            }
            return video_metadata

        except Exception as ffprobe_failure:
            # await interaction.followup.send("There was an error parsing the specified URL. Please double-check it")
            print(
                f"Error fetching metadata of song {song_url}:",
                ytdlp_failure,
                ffprobe_failure,
            )
            return None


def get_current_and_max_position(queuename: str) -> (int, int):
    """Finds the current and max position of the active queue"""
    query = "SELECT currentpos, maxpos FROM queues WHERE name = ?;"
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (queuename,))
        return cursor.fetchone()


def increment_cur_pos(queuename: str):
    """Increment the current position of the active queue"""
    query = "UPDATE queues SET currentpos = ? WHERE name = ?;"
    curpos, _ = get_current_and_max_position(queuename)
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (curpos + 1, current_queue))
        cursor.close()


def is_karaoke_operator(user):
    """Determine if a user is authorized to manage the bot"""
    return (
        not set([int(id) for id in config["operator_roles"]]).intersection(
            set([role.id for role in user.roles])
        )
        == set()
    )


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


@tasks.loop(seconds=5)
async def playback_loop():
    """The main loop to check for a new song and play it"""
    # There is no active queue
    if current_queue == "":
        return

    # There are no more songs in the queue
    curr_index, max_index = get_current_and_max_position(current_queue)
    if max_index <= curr_index:
        return

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
            notification_message += f" with {current_song['collaborators']}"
        if current_song["lyrics_url"]:
            notification_message += f"\nLyrics: {current_song['lyrics_url']}"
        await client.botchannel.send(notification_message)
        # Now try to play it
        try:
            process = subprocess.Popen(
                [
                    "mpv",
                    "-fs",
                    "-pause",
                    "--ytdl-raw-options=format-sort=res:1080",
                    current_song["url"],
                ]
            )
            while process.poll() is None:
                await asyncio.sleep(1)
        except Exception as mpv_error:
            print("Unable to launch mpv and play current song", mpv_error)
        finished_at = datetime.datetime.now()

        # Now update the db such that the song is completed
        query = "UPDATE songs SET completed_time = ? WHERE position = ?;"
        with conn:
            cursor = conn.cursor()
            cursor.execute(query, (finished_at, current_song["position"]))
            cursor.close()
        increment_cur_pos(current_queue)

class EmbedPages():
    def __init__(self, fields: list[tuple[str, str]], max_page_chars: int, max_fields: int = 15):
        self.embed_pages = []
        curr_chars = 0
        curr_embed = discord.Embed(title="Currently queued songs")

        for [name, value] in fields:
            # start a new page if we are over the character limit or field limit
            if curr_chars + len(name) + len(value) > max_page_chars or len(curr_embed.fields) >= max_fields:
                self.embed_pages.append(curr_embed)
                curr_embed = discord.Embed(title="Currently queued songs")
                curr_chars = 0
            curr_embed.add_field(name=name, value=value, inline=False)
            curr_chars += len(name) + len(value)

        if curr_chars > 0:
            self.embed_pages.append(curr_embed)

        self.current_page = 0
        self.num_pages = len(self.embed_pages)

        for i in range(self.num_pages):
            self.embed_pages[i].set_footer(text=f"Page {i+1}/{self.num_pages}")

    def get_current_page(self) -> discord.Embed:
        return self.embed_pages[self.current_page]
    
    def next_page(self) -> discord.Embed:
        self.current_page = (self.current_page + 1) % self.num_pages

    def previous_page(self) -> discord.Embed:
        self.current_page = (self.current_page - 1) % self.num_pages


class PaginatedOutput(discord.ui.View):
    def __init__(
            self,
            pages: EmbedPages,
            user: discord.User,
            timeout: Optional[float] = None
        ):
        self.pages = pages
        self.user = user
        super().__init__(timeout=timeout)

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user.id
    
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.blurple)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pages.previous_page()
        await interaction.response.edit_message(embed=self.pages.get_current_page())

    @discord.ui.button(label="Next", style=discord.ButtonStyle.blurple)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pages.next_page()
        await interaction.response.edit_message(embed=self.pages.get_current_page())


# command to initialize a queue
@tree.command(
    name="initialize",
    description="Creates a new empty karaoke queue or switches to queue of specified name",
)
async def initialize(interaction: discord.Interaction, queue_name: str):
    """Creates or switches to specific karaoke queue by name"""
    global current_queue
    if not is_karaoke_operator(interaction.user):
        await interaction.response.send_message("Cannot set queue, permission denied")
        return
    writequery = "INSERT INTO queues (name, currentpos, maxpos, discord_guild_id, time_created) VALUES (?,?,?,?,?)"
    created_at = datetime.datetime.now()
    # check if queue exists
    result = get_current_and_max_position(queue_name)
    if result:
        current_position, max_position = result
        await interaction.response.send_message(
            f"Fetched queue {queue_name} with current current_position {current_position} and max current_position {max_position}"
        )
    else:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                writequery, (queue_name, 0, 0, config["guild_id"], created_at)
            )
            await interaction.response.send_message(f"New queue {queue_name} created!")
    current_queue = queue_name
    if not playback_loop.is_running():
        playback_loop.start()


@tree.command(name="listqueues", description="Lists existing queues")
async def listqueues(interaction: discord.Interaction):
    """Lists all existing queues"""
    query = "SELECT name, time_created FROM queues"
    with conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        cursor.close()
    await interaction.response.send_message(
        "Current queues: \n"
        + "\n".join(["{0}, created on {1}".format(row[0], row[1]) for row in rows])
    )


@tree.command(name="addsong", description="Add a song to the queue")
async def addsong(
    interaction: discord.Interaction,
    song_url: str,
    lyrics_url: Optional[str],
    collaborators: Optional[str],
    notes: Optional[str],
):
    """Adds a song to the queue"""
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return
    with conn:
        cursor = conn.cursor()

        # First, ensure the user is allowed to queue
        if not is_karaoke_operator(interaction.user):
            cursor.execute(
                "SELECT COUNT(*) FROM songs WHERE is_revoked = FALSE AND completed_time IS NULL and discord_user_id = ?;",
                (interaction.user.id,),
            )

            currently_queued_by_user = cursor.fetchone()[0]
            if currently_queued_by_user >= int(config["max_queued_per_user"]):
                await interaction.response.send_message(
                    f"You currently already have {currently_queued_by_user} songs queued. Either swap an existing one or wait until you go next before queuing again"
                )
                cursor.close()
                return
        # Try getting metadata with yt-dlp (for video sites)
        await interaction.response.defer()  # sometimes takes more than 3 seconds
        video_metadata = get_song_metadata(song_url)
        if not video_metadata:
            await interaction.followup.send(
                "There was an error fetching the song's metadata. Check the URL."
            )
            return

        query = "INSERT INTO songs (url, title, duration, added_time, lyrics_url, notes, position, collaborators, completed_time, is_revoked, discord_user_id, discord_guild_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        created_at = datetime.datetime.now()
        try:
            _, max_position = get_current_and_max_position(current_queue)
            cursor.execute(
                query,
                (
                    song_url,
                    video_metadata["title"],
                    int(video_metadata["duration"]),
                    created_at,
                    lyrics_url,
                    notes,
                    max_position,
                    collaborators,
                    None,
                    False,
                    interaction.user.id,
                    config["guild_id"],
                ),
            )

        except Exception as database_error:
            await interaction.followup.send(
                "There was an error adding the song to the database. Is it a duplicate?"
            )
            print(f"Error adding song {song_url} to database", database_error)
            return
        max_position += 1
        # save the new position
        updatequery = "UPDATE queues SET maxpos = ? WHERE name = ?;"
        cursor.execute(updatequery, (max_position, current_queue))
        cursor.close()
    await interaction.followup.send(f"Added song {video_metadata['title']}\n{song_url}")


@tree.command(
    name="swapsong",
    description="Swap your song with a specified index with a new one while keeping place in the queue",
)
async def swapsong(
    interaction: discord.Interaction,
    position: int,
    song_url: str,
    lyrics_url: Optional[str],
    collaborators: Optional[str],
    notes: Optional[str],
):
    """Swap your song with a specified index with a new one while keeping place in the queue"""
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return
    # ensure user was the creator of the entry
    query = "SELECT discord_user_id FROM songs WHERE position = ?;"
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (position,))
        userofsong = cursor.fetchone()[0]
        if not interaction.user.id == userofsong:
            await interaction.response.send_message(
                "You only have permission to remove your own songs"
            )
            return
        # Try getting metadata with yt-dlp (for video sites)
        await interaction.response.defer()  # sometimes takes more than 3 seconds
        video_metadata = get_song_metadata(song_url)
        if not video_metadata:
            await interaction.followup.send(
                "There was an error fetching the song's metadata. Check the URL."
            )
            return
        query = "UPDATE songs SET url = ?, title = ?, duration = ?, lyrics_url = ?, notes = ?, collaborators = ?, is_revoked = ?, discord_user_id = ?, discord_guild_id = ? WHERE position = ?;"
        try:
            cursor.execute(
                query,
                (
                    song_url,
                    video_metadata["title"],
                    int(video_metadata["duration"]),
                    lyrics_url,
                    notes,
                    collaborators,
                    False,
                    interaction.user.id,
                    config["guild_id"],
                    position,
                ),
            )
        except Exception as database_error:
            await interaction.followup.send(
                "There was an error adding the song to the database. Is it a duplicate?"
            )
            print(f"Error adding song {song_url} to database", database_error)
            return

    await interaction.followup.send(
        f"Swapped position {position} with {video_metadata['title']}\n{song_url}"
    )


# command to manually set the current position
@tree.command(
    name="setposition",
    description="Stops the current playback and sets the current position to a specified value",
)
async def setposition(interaction: discord.Interaction, new_position: int):
    """Stops the current playback and sets the current position to a specified value"""
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return
    if not is_karaoke_operator(interaction.user):
        await interaction.response.send_message(
            "Cannot set position, permission denied"
        )
        return
    _, max_position = get_current_and_max_position(current_queue)
    if new_position > max_position:
        await interaction.response.send_message(
            f"Cannot set position to {new_position}, it exceeds max position of {max_position}"
        )
        return
    # terminate the current playback loop and restart it at new position
    playback_loop.cancel()
    with conn:
        cursor = conn.cursor()
        updatequery = "UPDATE queues SET currentpos = ? WHERE name = ?;"
        cursor.execute(updatequery, (new_position, current_queue))
        cursor.close()
    while playback_loop.is_running():
        await asyncio.sleep(1)
    playback_loop.start()
    await interaction.response.send_message(f"Set position to {new_position}")


@tree.command(name="listsongs", description="Lists currently queued songs")
async def listsongs(interaction: discord.Interaction, include_old: Optional[bool]):
    """Lists queued songs"""
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return
    curpos, _ = get_current_and_max_position(current_queue)
    with conn:
        cursor = conn.cursor()
        if include_old:
            query = "SELECT * FROM songs WHERE is_revoked = FALSE ORDER BY position;"
            cursor.execute(query)
        else:
            query = "SELECT * FROM songs WHERE position >= ? AND is_revoked = FALSE ORDER BY position;"
            cursor.execute(query, (curpos,))
        rows = cursor.fetchall()
        column_names = [description[0] for description in cursor.description]
        # Create a list of dictionaries
        result_list = []
        for row in rows:
            song_dict = {column_names[i]: row[i] for i in range(len(column_names))}
            result_list.append(song_dict)
        cursor.close()

    discord_user_ids = set(int(song["discord_user_id"]) for song in result_list)
    missing_members = []
    nicknames = {}

    # get members from cache if possible or add to missing_members
    for user_id in discord_user_ids:
        member = interaction.guild.get_member(user_id)
        if member is not None:
            nicknames[user_id] = member.nick if member.nick is not None else member.name
        else:
            missing_members.append(user_id)

    # query missing members
    if len(missing_members) > 0:
        for member in await interaction.guild.query_members(user_ids=missing_members):
            nicknames[member.id] = (
                member.nick if member.nick is not None else member.name
            )

    if not result_list:
        await interaction.response.send_message("There are no songs currently queued")
        return
    
    fields = []
    for song in result_list:
        user_id = int(song["discord_user_id"])
        # if the user is not in the guild, use their id instead
        nickname = nicknames[user_id] if user_id in nicknames else "<@{0}>".format(user_id)
        field_title = f"{song['position']:0>2}. {nickname}"
        field_value = f"{song['title']} with {song['collaborators']}" if song["collaborators"] else song['title']
        fields.append((field_title, field_value))

    pages = EmbedPages(fields, 500)
    await interaction.response.send_message(embed=pages.get_current_page(), view=PaginatedOutput(pages, interaction.user))


# command to mark a song as revoked
@tree.command(name="removesong", description="Removes song at specified index")
async def removesong(interaction: discord.Interaction, position: int):
    """Removes song at specified index"""
    if current_queue == "":
        await interaction.response.send_message("No queues are currently active.")
        return

    # ensure user is either admin or was the creator of the entry
    if not is_karaoke_operator(interaction.user):
        query = "SELECT discord_user_id FROM songs WHERE position = ?;"
        with conn:
            cursor = conn.cursor()
            cursor.execute(query, (position,))
            userofsong = cursor.fetchone()[0]
            cursor.close()
        if not interaction.user.id == userofsong:
            await interaction.response.send_message(
                "You only have permission to remove your own songs"
            )
            return

    with conn:
        cursor = conn.cursor()
        updatequery = "UPDATE songs SET is_revoked = TRUE WHERE position = ?;"
        cursor.execute(updatequery, (position,))
        cursor.close()
    await interaction.response.send_message(f"Removed song at {position}")


client.run(config["token"])
