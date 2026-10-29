import subprocess, wave, os
R=48000
lines=[l.rstrip("\n").split("|",1) for l in open("script.txt") if "|" in l]
host=bytearray(); guest=bytearray()
gap=bytes(int(R*0.5)*2)
for i,(who,text) in enumerate(lines):
    aiff=f"tts/{i:02d}.aiff"; wav=f"tts/{i:02d}.wav"
    subprocess.run(["say","-v","Daniel" if who=="D" else "Samantha","-r","175","-o",aiff,text],check=True)
    subprocess.run(["ffmpeg","-loglevel","error","-y","-i",aiff,"-ac","1","-ar",str(R),"-sample_fmt","s16",wav],check=True)
    with wave.open(wav) as w: pcm=w.readframes(w.getnframes())
    sil=bytes(len(pcm))
    if who=="D": host+=pcm; guest+=sil
    else: guest+=pcm; host+=sil
    host+=gap; guest+=gap
for name,buf in (("host.wav",host),("guest.wav",guest)):
    with wave.open(name,"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(R); w.writeframes(bytes(buf))
print("seconds", len(host)/2/R)
