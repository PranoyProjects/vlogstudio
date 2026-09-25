# VlogStudio 🎬

Superior driving vlog editor. Built by combining the best from:
- **nikhil-reddy05/auto-captions** → Whisper word-level timestamps
- **sebetancurch/auto-caption** → Styled animated ASS subtitle burn-in  
- **Fijekronk/easyEdit** → FastAPI + FFmpeg processing pipeline

## Features

- 🗺️ **Map overlay** — drag & drop positioning, resize, opacity control
- 🎙️ **Auto CC** — Whisper AI transcribes voice, syncs word-by-word
- ✨ **3D floating text** — simulated 3D with shadow depth (extruded look)
- ✂️ **Trim** — cut start/end of video
- 🎵 **Background music** — mix with volume control
- 💬 **Caption styles** — TikTok word-highlight, Classic, Minimal
- 🚀 **One-click export** — FFmpeg encodes final MP4

## Requirements

- Ubuntu (or any Linux/Mac)
- Python 3.8+
- FFmpeg

## Setup (Ubuntu)

```bash
# 1. Install FFmpeg
sudo apt-get install ffmpeg

# 2. Clone or download this folder

# 3. Run the startup script
chmod +x start.sh
./start.sh

# 4. Open browser
# http://localhost:8000
```

## Manual setup

```bash
pip3 install fastapi uvicorn[standard] python-multipart faster-whisper --break-system-packages
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Usage

1. Upload your main driving video
2. Upload your map screen recording (optional)
3. Drag the map overlay to position it
4. Click "Auto-Transcribe Voice" for captions
5. Add 3D floating text (optional)
6. Click Export

## Share with your friend

**Option 1: ZIP and share**
```bash
zip -r vlogstudio.zip vlogstudio/
```
Send the ZIP. They run `./start.sh`.

**Option 2: GitHub**
```bash
git init && git add . && git commit -m "VlogStudio"
# Push to GitHub, friend clones and runs start.sh
```

## Architecture

```
Browser (HTML/JS)
    ↕ REST API
FastAPI (main.py)
    ↕
FFmpeg    ← video processing, overlay, captions
Whisper   ← speech-to-text transcription
```
