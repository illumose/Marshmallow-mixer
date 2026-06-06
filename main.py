import subprocess
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

@app.post("/mix")
def mix_audio(payload: dict):
    # 1. Download the files
    with open("/tmp/voice.mp3", "wb") as f:
        f.write(requests.get(payload.get("voice_url")).content)
        
    with open("/tmp/music.mp3", "wb") as f:
        f.write(requests.get(payload.get("music_url")).content)
        
# 2. Run the system-level FFmpeg command
    cmd = [
        "ffmpeg", 
        "-i", "/tmp/voice.mp3", 
        "-stream_loop", "-1", "-i", "/tmp/music.mp3", 
        "-filter_complex", 
        # 1. Apply a clean 7-second delay (7000ms) to the voice. (aresample is a free safety net)
        "[0:a]aresample=44100,adelay=7000|7000[voice];"
        # 2. Set background music to 20% volume
        "[1:a]aresample=44100,volume=0.20[bg];"
        # 3. Mix them together (duration=first ensures it cuts off when the voice ends)
        "[voice][bg]amix=inputs=2:duration=first:dropout_transition=2,aresample=44100[aout]",
        "-map", "[aout]", "-y", "/tmp/mixed.mp3"
    ]
    subprocess.run(cmd, check=True)
    
    # 3. Return the file
    return FileResponse("/tmp/mixed.mp3", media_type="audio/mpeg")
