"""discord.py VoiceProtocol that speaks Spacebar's WebRTC voice protocol.

Spacebar's voice gateway rejects anything but `protocol: "webrtc"` in
SELECT_PROTOCOL (server/src/webrtc/opcodes/SelectProtocol.ts), so discord.py's
built-in UDP+xsalsa20 voice client cannot connect. The SDP it exchanges is also
Discord's truncated dialect, not real SDP, so we translate at both ends:

  offer  -> strip everything except the audio m-section's `a=` lines
  answer <- pull ip/port/ufrag/pwd/fingerprint out and rebuild a valid SDP

Track publishing is out-of-band: after DTLS comes up we announce our RTP SSRC
with op 12 (VIDEO), which is what makes the SFU forward our audio.
"""

import asyncio
import fractions
import logging
import os
import random
import re
import time

import aiohttp
import discord
from av import AudioFrame
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960  # 20ms
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2
SILENCE = b"\x00" * FRAME_BYTES
OPUS_PAYLOAD_TYPE = 111  # must be 111: the SFU keys "is chromium" off this

IDENTIFY, SELECT_PROTOCOL, READY, HEARTBEAT = 0, 1, 2, 3
SESSION_DESCRIPTION, SPEAKING, HELLO, VIDEO = 4, 5, 8, 12

# The SFU reads header extension ids out of our offer. We never actually send
# these extensions, but it wants them declared or it configures id -1.
OFFER_EXTMAPS = (
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc"
    "-extensions-01\r\n"
)


def _to_offer_body(sdp: str) -> str:
    """Reduce a real SDP offer to the audio attribute lines Spacebar parses."""
    out = []
    in_audio = False
    for line in sdp.splitlines():
        if line.startswith("m="):
            in_audio = line.startswith("m=audio")
        elif in_audio and not line.startswith("a=extmap:"):
            out.append(line)
    return OFFER_EXTMAPS + "\r\n".join(out) + "\r\n"


def _from_answer_body(body: str) -> RTCSessionDescription:
    """Rebuild a valid SDP answer from Spacebar's `m=audio <port> ICE/SDP` blob."""

    def grab(pattern):
        m = re.search(pattern, body, re.M)
        if not m:
            raise ValueError(f"no {pattern!r} in voice answer:\n{body}")
        return m.groups()

    (ip,) = grab(r"^c=IN IP4 (\S+)")
    (port,) = grab(r"^m=audio (\d+)")
    (ufrag,) = grab(r"^a=ice-ufrag:(\S+)")
    (pwd,) = grab(r"^a=ice-pwd:(\S+)")
    algo, fingerprint = grab(r"^a=fingerprint:(\S+) (\S+)")

    # ponytail: we rebuild the host candidate from c=/m= instead of parsing
    # a=candidate, whose field order differs between Spacebar's SFU backends.
    lines = [
        "v=0",
        f"o=- {random.getrandbits(32)} 1 IN IP4 {ip}",
        "s=-",
        "t=0 0",
        f"m=audio {port} UDP/TLS/RTP/SAVPF {OPUS_PAYLOAD_TYPE}",
        f"c=IN IP4 {ip}",
        "a=mid:0",
        "a=recvonly",
        f"a=rtcp:{port} IN IP4 {ip}",
        "a=rtcp-mux",
        f"a=rtpmap:{OPUS_PAYLOAD_TYPE} opus/48000/2",
        f"a=fmtp:{OPUS_PAYLOAD_TYPE} minptime=10;useinbandfec=1;usedtx=1",
        "a=ice-lite",
        f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{pwd}",
        f"a=fingerprint:{algo} {fingerprint}",
        "a=setup:passive",
        f"a=candidate:0 1 UDP 2130706431 {ip} {port} typ host",
        "a=end-of-candidates",
    ]
    return RTCSessionDescription(sdp="\r\n".join(lines) + "\r\n", type="answer")


class PCMTrack(MediaStreamTrack):
    """A never-ending 48kHz stereo track fed by a swappable ffmpeg process.

    Emitting silence rather than ending the track means we can change songs
    without renegotiating, and keeps our RTP timestamps monotonic.
    """

    kind = "audio"

    def __init__(self):
        super().__init__()
        self.proc: asyncio.subprocess.Process | None = None
        self.on_finish = None
        self._pts = 0
        self._epoch = None

    async def play(self, args: list[str]):
        await self.stop_source()
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )

    async def stop_source(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()

    @property
    def playing(self) -> bool:
        return self.proc is not None

    async def _pcm(self) -> bytes:
        if self.proc is None:
            return SILENCE
        try:
            return await self.proc.stdout.readexactly(FRAME_BYTES)
        except asyncio.IncompleteReadError as e:
            await self.stop_source()
            if self.on_finish:
                self.on_finish()
            return e.partial.ljust(FRAME_BYTES, b"\x00")

    async def recv(self) -> AudioFrame:
        if self._epoch is None:
            self._epoch = time.monotonic()
        delay = self._epoch + self._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        frame = AudioFrame(format="s16", layout="stereo", samples=FRAME_SAMPLES)
        frame.planes[0].update(await self._pcm())
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += FRAME_SAMPLES
        return frame


class SpacebarVoiceClient(discord.VoiceProtocol):
    def __init__(self, client, channel):
        super().__init__(client, channel)
        self.track = PCMTrack()
        self.session_id = None
        self.token = None
        self.endpoint = None
        self.server_id = None
        self.ssrc = None
        self.pc = None
        self._ws = None
        self._task = None
        self._connected = asyncio.Event()

    # -- discord.py plumbing ------------------------------------------------

    async def on_voice_state_update(self, data):
        self.session_id = data["session_id"]

    async def on_voice_server_update(self, data):
        self.token = data["token"]
        self.endpoint = data["endpoint"]
        self.server_id = data.get("guild_id") or data.get("channel_id")
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._run())

    async def connect(self, *, timeout, reconnect, self_deaf=False, self_mute=False):
        await self.channel.guild.change_voice_state(
            channel=self.channel, self_deaf=self_deaf, self_mute=self_mute
        )
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def disconnect(self, *, force=False):
        if self._task:
            self._task.cancel()
        await self.track.stop_source()
        if self.pc:
            await self.pc.close()
        await self.channel.guild.change_voice_state(channel=None)
        self.cleanup()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # -- voice gateway ------------------------------------------------------

    async def _send(self, op, d):
        await self._ws.send_json({"op": op, "d": d})

    async def _heartbeat(self, interval):
        while True:
            await asyncio.sleep(interval / 1000)
            await self._send(HEARTBEAT, random.getrandbits(31))

    async def _run(self):
        # The endpoint in VOICE_SERVER_UPDATE comes from the instance's
        # `regions_available_0_endpoint` config row, which is often still
        # localhost:3004. SPACEBAR_VOICE overrides it without touching the db.
        endpoint = os.getenv("SPACEBAR_VOICE") or self.endpoint
        if "://" not in endpoint:
            scheme = "ws" if os.getenv("SPACEBAR_VOICE_INSECURE") else "wss"
            endpoint = f"{scheme}://{endpoint}"
        url = f"{endpoint}/?v=7"

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, ssl=not os.getenv("SPACEBAR_NO_VERIFY")) as ws:
                self._ws = ws
                hb = None
                try:
                    async for msg in ws:
                        payload = msg.json()
                        op, d = payload["op"], payload.get("d")
                        if op == HELLO:
                            hb = asyncio.create_task(
                                self._heartbeat(d["heartbeat_interval"])
                            )
                            await self._send(
                                IDENTIFY,
                                {
                                    "server_id": self.server_id,
                                    "user_id": str(self.client.user.id),
                                    "session_id": self.session_id,
                                    "token": self.token,
                                    "video": False,
                                },
                            )
                        elif op == READY:
                            await self._offer()
                        elif op == SESSION_DESCRIPTION:
                            await self._answer(d["sdp"])
                        else:
                            log.debug("voice op %s: %s", op, d)
                finally:
                    if hb:
                        hb.cancel()
                    self._connected.clear()

    async def _offer(self):
        # No STUN: the SFU is ice-lite and only ever uses our source address,
        # so srflx candidates buy nothing and aiortc's default (Google STUN)
        # is just a startup stall behind a restrictive egress policy.
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTrack(self.track)
        for transceiver in self.pc.getTransceivers():
            transceiver.direction = "sendonly"

        await self.pc.setLocalDescription(await self.pc.createOffer())
        sdp = self.pc.localDescription.sdp
        self.ssrc = int(re.search(r"^a=ssrc:(\d+)", sdp, re.M).group(1))

        body = _to_offer_body(sdp)
        await self._send(
            SELECT_PROTOCOL,
            {
                "protocol": "webrtc",
                "data": body,
                "sdp": body,
                "codecs": [
                    {
                        "name": "opus",
                        "type": "audio",
                        "priority": 1000,
                        "payload_type": OPUS_PAYLOAD_TYPE,
                    }
                ],
            },
        )

    async def _answer(self, body):
        await self.pc.setRemoteDescription(_from_answer_body(body))
        while self.pc.connectionState not in ("connected", "failed", "closed"):
            await asyncio.sleep(0.1)
        if self.pc.connectionState != "connected":
            raise RuntimeError(f"webrtc {self.pc.connectionState}")

        # Publishing is what actually wires our SSRC into the SFU's router.
        await self._send(
            VIDEO,
            {"audio_ssrc": self.ssrc, "video_ssrc": 0, "rtx_ssrc": 0, "streams": []},
        )
        await self._send(
            SPEAKING, {"speaking": 1, "delay": 0, "ssrc": self.ssrc}
        )
        self._connected.set()
        log.info("voice connected, ssrc=%s", self.ssrc)
