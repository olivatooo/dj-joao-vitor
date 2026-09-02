# dj-joao-vitor

YouTube music bot for [Spacebar](https://docs.spacebar.chat/) instances.

## Why this isn't just a discord.py bot

Spacebar is close enough to Discord's API that discord.py drives commands, but
`discord.http.Route.BASE` alone is *not* enough — it only redirects REST. Three
shims in `bot.py` are load-bearing, all three verified against a live instance:

- **`DiscordWebSocket.DEFAULT_GATEWAY`** must be overridden too. It's a
  hardcoded constant, so a bot with only `Route.BASE` set connects to the real
  `gateway.discord.gg` and sends your instance token to Discord, which bounces
  it as a confusing `4004 Authentication failed`.
- **`compress=False`** on the gateway. Spacebar doesn't implement
  `zlib-stream`; discord.py requests it by default and the socket dies.
- **Voice channel defaults.** Spacebar omits `bitrate` and `user_limit` and
  sends `video_quality_mode: null`; discord.py indexes all three.

Voice is a bigger exception. Spacebar's voice gateway hard-rejects Discord's UDP
voice path:

```ts
// server/src/webrtc/opcodes/SelectProtocol.ts
if (data.protocol !== "webrtc")
    return this.close(4000, "only webrtc protocol supported currently");
```

So `@discordjs/voice` and discord.py's `VoiceClient` cannot connect — they send
`protocol: "udp"` and get closed. `sbvoice.py` is a `discord.VoiceProtocol` that
speaks the WebRTC variant instead, using aiortc:

1. op 0 IDENTIFY / op 2 READY as usual.
2. Build a real WebRTC offer, then reduce it to the audio m-section's `a=` lines
   — the server does `SDPInfo.parse("m=audio\n" + sdp)`, so a full SDP breaks it.
3. op 4 SESSION_DESCRIPTION comes back as `m=audio <port> ICE/SDP` plus a handful
   of attributes; rebuild a valid SDP answer from it for aiortc.
4. After DTLS is up, op 12 VIDEO announces our RTP SSRC. **This is what actually
   publishes the track** — without it the SFU creates no incoming stream and
   nobody hears anything. op 5 SPEAKING only lights up the green ring.

Opus payload type must be 111: the SFU uses `payload_type === 111` as its
"client is Chromium" probe and configures header extensions off it.

Audio is ffmpeg → raw s16le 48kHz stereo → one long-lived aiortc track, so
changing songs never renegotiates.

## Server-side setup

### 1. Turn voice on (it ships disabled)

Voice is an optional dependency in Spacebar — with no `WRTC_LIBRARY` set the
gateway loads no media server and every voice connect fails. Pick an SFU
(pion is the recommended one) and, in the **server** repo:

```sh
npm install @spacebarchat/pion-webrtc --no-save
```

`.env`:

```sh
WRTC_LIBRARY=@spacebarchat/pion-webrtc
WRTC_WS_PORT=3004
```

Then run the Go SFU next to it (`curl https://checkip.amazonaws.com` for the IP;
`127.0.0.1` is fine for a local-only instance):

```sh
cd pion-sfu && go run . -port <udp port> -ip <public ip>
```

Medooze and mediasoup work too — `@spacebarchat/medooze-webrtc` /
`mediasoup-spacebar-wrtc`, plus `WRTC_PUBLIC_IP` instead of the separate SFU
process. All three emit the same `m=audio <port> ICE/SDP` answer, so this bot
does not care which you pick.

Point the voice region at the gateway, in the `config` table:

```
regions_available_0_endpoint = voice.example.com   # default: localhost:3004
```

If that endpoint is plain `ws://`, run the bot with `SPACEBAR_VOICE_INSECURE=1`.

### 2. Make a bot account

Spacebar has no developer portal yet, so create the application over the API
with a **user** token (grab it from your client):

```sh
U=https://your.instance/api/v9
T='<your user token>'

APP=$(curl -s -X POST $U/applications -H "Authorization: $T" \
  -H 'Content-Type: application/json' -d '{"name":"dj-joao-vitor"}' | jq -r .id)

curl -s -X POST $U/applications/$APP/bot -H "Authorization: $T" | jq -r .token
```

### 3. Invite it

```sh
curl -s -X POST "$U/oauth2/authorize?client_id=$APP" -H "Authorization: $T" \
  -H 'Content-Type: application/json' \
  -d '{"guild_id":"<guild id>","permissions":"8","authorize":true}'
```

## Run

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export SPACEBAR_API=https://your.instance/api      # no /v9
export SPACEBAR_TOKEN=<bot token>
.venv/bin/python bot.py
```

Needs `ffmpeg` on PATH.

| Variable | Purpose |
| --- | --- |
| `SPACEBAR_API` | Instance API root, e.g. `https://chat.plan-b4.com/api` |
| `SPACEBAR_TOKEN` | Bot token |
| `SPACEBAR_VOICE` | Voice gateway override, e.g. `wss://voice.plan-b4.com` |
| `SPACEBAR_VOICE_INSECURE` | Use `ws://` when `SPACEBAR_VOICE` has no scheme |
| `SPACEBAR_NO_VERIFY` | Skip TLS verification (self-signed voice gateway) |

`SPACEBAR_VOICE` is worth setting: the endpoint in `VOICE_SERVER_UPDATE` comes
from the `regions_available_0_endpoint` config row, which on most instances is
still `localhost:3004`.

## Docker / Kubernetes

```sh
docker build -t dj-joao-vitor:latest .
kubectl create secret generic dj-joao-vitor --from-literal=token='<bot token>'
kubectl apply -f k8s.yaml
```

`.github/workflows/docker.yml` builds and pushes to Docker Hub on every push to
`main` (tags `latest` and the commit sha). It needs two repo secrets:
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (an access token, not your password)
— until those exist the workflow will fail on the login step. Then point
`k8s.yaml`'s `image:` at `<user>/dj-joao-vitor:latest`.

Nothing is exposed, and that is not an omission — the bot never listens. The
gateway and voice WebSockets are outbound, and the media flow is outbound too
because the SFU is ice-lite: it only ever replies to the source address of our
STUN and RTP, so conntrack carries the UDP return path. No `Service`, no
`hostNetwork`, no port range to open.

Egress it does need: the API and gateway over TCP, the voice gateway over TCP,
the SFU's UDP media port on `WRTC_PUBLIC_IP`, and HTTPS to YouTube. The
Deployment pins `replicas: 1` with `strategy: Recreate` — a rolling update
would briefly run a second bot that also answers `!play`.

Commands: `!play <search or url>`, `!skip`, `!queue`, `!leave`.

`python test_sdp.py` checks the SDP translation round-trips into aiortc.

## Not built

Volume control, pause/resume, playlists, seeking, reconnect-on-voice-server-move,
and receiving audio (the track is `sendonly`). All small additions on top of
`PCMTrack`; none needed to play music.

No liveness probe either: nothing listens, so it would have to be an exec probe,
and the failure it would catch (voice WebSocket dies while the process lives) is
better fixed by reconnecting than by restarting the pod.

The image has not been built here — no Docker daemon was available in the
environment it was written in.
