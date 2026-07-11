import subprocess
import requests
import uuid  # <-- Import the UUID library
from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

@app.post("/mix")
def mix_audio(payload: dict):
    # Generate a unique string for this specific API call
    job_id = str(uuid.uuid4())
    
    # Create dynamic file paths so concurrent requests never overlap
    voice_path = f"/tmp/voice_{job_id}.mp3"
    music_path = f"/tmp/music_{job_id}.mp3"
    out_path = f"/tmp/mixed_{job_id}.mp3"

    # 1. Download the files
    with open(voice_path, "wb") as f:
        f.write(requests.get(payload.get("voice_url")).content)
        
    with open(music_path, "wb") as f:
        f.write(requests.get(payload.get("music_url")).content)
        
    # 2. Run the system-level FFmpeg command
    cmd = [
        "ffmpeg", 
        "-i", voice_path, 
        "-stream_loop", "-1", "-i", music_path, 
        "-filter_complex", 
        "[0:a]aresample=44100,adelay=7000|7000[voice];[1:a]aresample=44100,volume=0.10[bg];[voice][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=44100[aout]",
        "-map", "[aout]", "-y", out_path
    ]
    subprocess.run(cmd, check=True)
    
    # 3. Return the unique file
    return FileResponse(out_path, media_type="audio/mpeg")
