FROM python:3.11-slim

# Install the FFmpeg OS package
RUN apt-get update && apt-get install -y ffmpeg

# Set up the Python environment
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Start the server
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
