"""Self-check for the Spacebar <-> real-SDP translation. Run: python test_sdp.py"""

import asyncio

from aiortc import RTCPeerConnection

from sbvoice import PCMTrack, _from_answer_body, _to_offer_body

# Shape produced by spacebarchat/medooze-webrtc MedoozeSignalingDelegate.onOffer
ANSWER = """m=audio 40000 ICE/SDP
a=fingerprint:sha-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89
c=IN IP4 10.0.0.5
a=rtcp:40000
a=ice-ufrag:abcd
a=ice-pwd:0123456789abcdef0123
a=fingerprint:sha-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89
a=candidate:1 1 UDP 1 10.0.0.5 40000 typ host
"""


async def main():
    # ICE really starts dialling the fake candidate below; ignore the fallout.
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: None)
    pc = RTCPeerConnection()
    pc.addTrack(PCMTrack())
    for t in pc.getTransceivers():
        t.direction = "sendonly"
    await pc.setLocalDescription(await pc.createOffer())

    body = _to_offer_body(pc.localDescription.sdp)
    assert "\nm=" not in "\n" + body, "offer body must be attribute lines only"
    assert "\nv=0" not in "\n" + body
    for required in ("a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:", "a=setup:"):
        assert required in body, f"offer body lost {required}\n{body}"
    assert "ssrc-audio-level" in body and "transport-wide-cc" in body

    # The real check: aiortc must accept what we rebuild from Spacebar's answer.
    await pc.setRemoteDescription(_from_answer_body(ANSWER))
    assert pc.signalingState == "stable", pc.signalingState
    assert pc.getTransceivers()[0].direction == "sendonly"
    await pc.close()

    track = PCMTrack()
    frame = await track.recv()  # silent, but must be a well-formed 20ms frame
    assert frame.samples == 960 and frame.sample_rate == 48000
    assert (await track.recv()).pts == 960

    print("ok")


asyncio.run(main())
