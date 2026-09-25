#!/bin/bash
# VlogStudio - One-click startup for Ubuntu
echo "================================================"
echo "  VlogStudio - Driving Vlog Editor"
echo "================================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python3 not found. Install with: sudo apt install python3"
    exit 1
fi

# Check FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing FFmpeg..."
    sudo apt-get install -y ffmpeg
fi

# Install Python deps
echo "Installing dependencies..."
pip3 install -r requirements.txt --break-system-packages -q

# Optional: install whisper CLI as fallback
pip3 install openai-whisper --break-system-packages -q 2>/dev/null || true

echo ""
echo "✅ Starting VlogStudio..."
echo "👉 Open your browser at: http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop."
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
