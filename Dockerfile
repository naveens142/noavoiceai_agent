FROM python:3.11-slim

# Install system dependencies for audio and WebRTC
RUN apt-get update && apt-get install -y ffmpeg libavformat58 libavcodec58 libavdevice58 libavfilter7 libavutil56 libswscale5 libswresample3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["uvicorn", "bot_runner:app", "--host", "0.0.0.0", "--port", "7860"]
