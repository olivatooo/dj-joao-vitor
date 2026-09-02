"""YouTube music bot for Spacebar instances."""

import asyncio
import functools
import logging
import os
import re
import sys

import discord
import yarl
import yt_dlp
from discord.ext import commands

from sbvoice import SpacebarVoiceClient

API = os.environ.get("SPACEBAR_API", "http://localhost:3001/api").rstrip("/")
discord.http.Route.BASE = f"{API}/v9"

# Route.BASE only redirects REST. The gateway URL is a hardcoded constant, so
# without this the bot cheerfully connects to the real gateway.discord.gg and
# hands your instance token to Discord, which rejects it as 4004.
GATEWAY = os.environ.get("SPACEBAR_GATEWAY") or re.sub(
    r"^http", "ws", API.removesuffix("/api")
)
discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(GATEWAY + "/")

# Spacebar's gateway doesn't implement zlib-stream; discord.py asks for it by
# default and the socket dies mid-handshake.
discord.gateway.DiscordWebSocket.from_client = functools.partial(
    discord.gateway.DiscordWebSocket.from_client, compress=False
)

YDL = yt_dlp.YoutubeDL(
    {"format": "bestaudio/best", "quiet": True, "noplaylist": True, "default_search": "ytsearch"}
)

FFMPEG = "ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -i {} -vn -f s16le -ar 48000 -ac 2 -loglevel error pipe:1"

# Spacebar omits bitrate/user_limit on voice channels and sends a null
# video_quality_mode; discord.py indexes all three unconditionally.
_vocal_update = discord.channel.VocalGuildChannel._update


def _lenient_vocal_update(self, guild, data):
    data.setdefault("bitrate", 64000)
    data.setdefault("user_limit", 0)
    if data.get("video_quality_mode") is None:
        data["video_quality_mode"] = 1
    return _vocal_update(self, guild, data)


discord.channel.VocalGuildChannel._update = _lenient_vocal_update

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
players: dict[int, "Player"] = {}


class Player:
    def __init__(self, vc, text):
        self.vc = vc
        self.text = text
        self.queue: list[dict] = []
        self.current = None
        vc.track.on_finish = lambda: asyncio.get_running_loop().create_task(self.advance())

    async def advance(self):
        self.current = self.queue.pop(0) if self.queue else None
        if self.current is None:
            await self.vc.track.stop_source()
            return
        await self.vc.track.play(FFMPEG.format(self.current["url"]).split())
        await self.text.send(f"\N{MUSICAL NOTE} Now playing: **{self.current['title']}**")

    async def add(self, query):
        info = await asyncio.to_thread(YDL.extract_info, query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        self.queue.append({"title": info["title"], "url": info["url"]})
        if not self.vc.track.playing:
            await self.advance()
        else:
            await self.text.send(f"Queued **{info['title']}** (#{len(self.queue)})")


async def player_for(ctx) -> Player:
    player = players.get(ctx.guild.id)
    if player is None:
        if not ctx.author.voice:
            raise commands.CommandError("You are not in a voice channel.")
        vc = await ctx.author.voice.channel.connect(cls=SpacebarVoiceClient)
        player = players[ctx.guild.id] = Player(vc, ctx.channel)
    return player


@bot.command()
async def play(ctx, *, query):
    async with ctx.typing():
        await (await player_for(ctx)).add(query)


@bot.command()
async def skip(ctx):
    player = players.get(ctx.guild.id)
    if player:
        await player.vc.track.stop_source()
        await player.advance()


@bot.command(name="queue")
async def queue_(ctx):
    player = players.get(ctx.guild.id)
    lines = [f"**Now:** {player.current['title']}"] if player and player.current else []
    lines += [f"{i}. {s['title']}" for i, s in enumerate(player.queue, 1)] if player else []
    await ctx.send("\n".join(lines) or "Nothing playing.")


@bot.command(aliases=["stop"])
async def leave(ctx):
    player = players.pop(ctx.guild.id, None)
    if player:
        await player.vc.disconnect()


@bot.event
async def on_ready():
    logging.info("logged in as %s (%s guilds)", bot.user, len(bot.guilds))


@bot.event
async def on_command_error(ctx, error):
    await ctx.send(f"\N{CROSS MARK} {error}")


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
    token = os.environ.get("SPACEBAR_TOKEN")
    if not token:
        sys.exit("set SPACEBAR_TOKEN (and SPACEBAR_API) first")
    bot.run(token)
