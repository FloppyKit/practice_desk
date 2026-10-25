"""Synthetic visit: fake host + fake guest publish TTS into LiveKit room office.
Keys come from env (secrets/livekit.env); never printed."""
import asyncio, os, sys, wave, time
from livekit import api, rtc

URL = "ws://127.0.0.1:7880"; ROOM = "office"
KEY = os.environ["LIVEKIT_API_KEY"]; SEC = os.environ["LIVEKIT_API_SECRET"]
TEST_CLIENT = "574f242f-70e3-40f2-96fa-fa397ddd7d23"

def token(identity, name, attrs):
    t = api.AccessToken(KEY, SEC).with_identity(identity).with_name(name).with_ttl(__import__("datetime").timedelta(hours=1))
    t = t.with_grants(api.VideoGrants(room_join=True, room=ROOM, can_publish=True, can_subscribe=True))
    return t.with_attributes(attrs).to_jwt()

async def occupants():
    lk = api.LiveKitAPI(URL.replace("ws://", "http://"), KEY, SEC)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=ROOM))
        return [p.identity for p in res.participants]
    except Exception:
        return []
    finally:
        await lk.aclose()

async def join(identity, name, attrs, wav_path):
    room = rtc.Room()
    await room.connect(URL, token(identity, name, attrs))
    w = wave.open(wav_path); rate = w.getframerate()
    src = rtc.AudioSource(rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", src)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    return room, w, src, rate

async def pump(w, src, rate):
    n = rate // 100  # 10 ms
    while True:
        data = w.readframes(n)
        if not data:
            break
        if len(data) < n * 2:
            data += bytes(n * 2 - len(data))
        fr = rtc.AudioFrame(data, rate, 1, n)
        await src.capture_frame(fr)

async def main():
    occ = [i for i in await occupants() if not i.startswith("scribe-")]
    if occ:
        print("ABORT: room office not empty:", len(occ), "participant(s)"); sys.exit(2)
    host_attrs = {"scribe_on": "1", "desk_client_id": TEST_CLIENT,
                  "desk_client_email": "matt@spaceandtime.gallery", "desk_client_name": "Test Patient (synthetic)"}
    h = await join("host-synthetic-test", "Synthetic Host (test)", host_attrs, "/t/host.wav")
    g = await join("guest-synthetic-test", "Synthetic Guest (test)", {"scribe_consent": "1"}, "/t/guest.wav")
    print("joined; waiting for scribe", flush=True)
    await asyncio.sleep(12)
    t0 = time.time()
    await asyncio.gather(pump(h[1], h[2], h[3]), pump(g[1], g[2], g[3]))
    print("audio done in", round(time.time() - t0, 1), "s", flush=True)
    await asyncio.sleep(3)
    await g[0].disconnect(); await h[0].disconnect()
    print("left", flush=True)

asyncio.run(main())
