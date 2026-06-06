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
        # Fix sample rates, delay voice by 4000ms (4 seconds), set music to 15%
        "[0:a]aresample=44100,adelay=4000|4000[voice];[1:a]aresample=44100,volume=0.15[bg];[voice][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "[aout]", "-y", "/tmp/mixed.mp3"
    ]
    subprocess.run(cmd, check=True)
    
    # 3. Return the file
    return FileResponse("/tmp/mixed.mp3", media_type="audio/mpeg")
