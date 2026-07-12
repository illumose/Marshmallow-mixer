import subprocess
import requests
import uuid
from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

def get_audio_duration(file_path: str) -> float:
    """Helper function to get the exact duration of an audio file in seconds."""
    cmd = [
        "ffprobe", 
        "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    return float(result.stdout.strip())

@app.post("/mix")
def mix_audio(payload: dict):
    # Generate a unique string for this specific API call
    job_id = str(uuid.uuid4())
    
    # Create dynamic file paths
    voice_path = f"/tmp/voice_{job_id}.mp3"
    music_path = f"/tmp/music_{job_id}.mp3"
    out_path = f"/tmp/mixed_{job_id}.mp3"

    # 1. Download the files
    with open(voice_path, "wb") as f:
        f.write(requests.get(payload.get("voice_url")).content)
        
    with open(music_path, "wb") as f:
        f.write(requests.get(payload.get("music_url")).content)
        
    # 2. Calculate Timings dynamically
    delay_seconds = 7.0     # The initial pause before the voice starts
    tail_seconds = 4.0      # How much extra music to play after voice ends
    fade_duration = 3.0     # How long the fade out should take (e.g., last 3 seconds)
    
    voice_duration = get_audio_duration(voice_path)
    total_duration = delay_seconds + voice_duration + tail_seconds
    fade_start = total_duration - fade_duration
        
    # 3. Run the system-level FFmpeg command
    cmd = [
        "ffmpeg", 
        "-i", voice_path, 
        "-stream_loop", "-1", "-i", music_path, 
        "-filter_complex", 
        # Add the delay to the start AND 4 seconds of silence to the end of the voice track
        f"[0:a]aresample=44100,adelay={int(delay_seconds*1000)}|{int(delay_seconds*1000)},apad=pad_dur={tail_seconds}[voice];"
        # Set music volume to 10% AND fade it out at the exact calculated time
        f"[1:a]aresample=44100,volume=0.10,afade=t=out:st={fade_start}:d={fade_duration}[bg];"
        # Mix them together (duration=first now cuts off when the 4-second padding finishes)
        "[voice][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=44100[aout]",
        "-map", "[aout]", "-y", out_path
    ]
    subprocess.run(cmd, check=True)
    
    # 4. Return the unique file
    return FileResponse(out_path, media_type="audio/mpeg")
