# mpv-discord-karaoke

This is a simple discord bot to run a karaoke queue via a discord server. Users queue yt-dlp supported links or direct media links, which are then played via mpv on the host machine.

## Setup:
1. Install `mpv`, `ffprobe`, and `yt-dlp`.
2. Create `config.json` and override desired parameters from `default_config.json` You'll need to set the token, guild id, and bot channel id. You will also need to set the operator role(s) which are given permissions to manage the karaoke queue. You can also set the limit for the number of songs a non-operator user can queue at once.

## Usage
- With operator role, initialize a queue with `/initialize queuename`. In future versions, you will be able to create multiple queues and switch between them, but support for multiple queues is currently broken.
    - (Not currently useful) List all queues on the server with `/listqueues`
- Add songs to the queue with `/addsong`. You must specify a url, and can optionally add fields for lyrics urls, ping additional collaborators on the song, and add notes.
- List the current songs in the queue with `/listsongs`. You can use the `include_old=True` parameter to list already-played songs too. Use the song position/index from this command to use other commands which modify the queue
- Swap a song at a specified index in the queue with a new one without losing your position in the queue via `/swapsong`. Unless you are an operator, you can only swap your own songs.
- Remove a song from the queue with `/removesong <index>`. Unless you are an operator, you can only remove your own songs. If wish to change to a different song without losing your place in the queue, try `/swapsong` instead.
- Operators can use `/setposition <index>` to stop playback and resume the queue from a specified index. You can use this to soft-reset in the event of an error, to rewind an accidentally skipped song, or skip a song



## Notes
- Data is stored in an sqlite db and the state can be (hopefully) restored in the event the bot crashes. Simply re-`/initialize` the same queue name.
- The goal is to eventually support multiple queues. Trying to use multiple queues now will result in undefined behavior
- Some niconico links aren't streamable with mpv (and can only be downloaded as a file with yt-dlp), and there doesn't seem to be much that can be done to fix this
- Special thanks to https://github.com/qwunchy/karaok for writing the original version of the bot!
