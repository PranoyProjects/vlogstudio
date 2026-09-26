# 🎬 VlogStudio v2 — Premium Driving Vlog Editor

> A fully local, free, AI-powered driving vlog editor. No subscriptions. No cloud. Runs on your computer.

---

## 🌟 What It Does

VlogStudio turns your raw driving footage into a polished travel vlog with:

- 🗺️ **Map overlays** — drop your Google Maps screen recording on top of your video, drag to position, set show/hide timers
- 🎨 **Color grading** — brightness, contrast, saturation, temperature, vignette, LUT presets (.cube files)
- ✨ **Effects** — glitch, shake, cinematic black bars, transitions between clips
- 🎙️ **Auto captions** — Whisper AI transcribes your voice, word-by-word sync, TikTok/Classic/Minimal/Bounce styles
- 📝 **SRT export** — download subtitle file for YouTube
- ✨ **3D floating text** — cinematic text overlays with show/hide timing
- 🎥 **GoPro Mode** — fisheye lens, stabilization, Protune color, superview, boomerang, timelapse, burst photos, night mode
- 📹 **Live Camera** — real-time GoPro filters via webcam, record and snapshot
- 🗺️ **Route Animator** — animated car/truck moving along real road on map (3 styles: Road/Satellite/Dark)
- ⚡ **AI Highlight Reel** — auto-picks best moments from long footage
- 🖼️ **Thumbnail Generator** — extracts 9 best frames
- 📑 **Auto Chapter Markers** — generates YouTube chapter timestamps from transcript
- 🌤️ **Weather Overlay** — shows live weather at your location on the video
- 📍 **GPS Speed Overlay** — upload GPX file from phone, shows speed on video
- 🎵 **Background music** — mix in music with volume control
- ✂️ **Trim** — cut start/end of video
- 📐 **Export formats** — 16:9 YouTube / 9:16 Reels / 1:1 Square
- 🔗 **Share links** — create shareable project links with timestamp comments

---

## 🖥️ Requirements

| Tool | Version |
|------|---------|
| Python | 3.10+ |
| FFmpeg | Any recent |
| pip packages | See below |

**Hardware:** No GPU needed. CPU-only encoding via x264.

---

## 🐧 Ubuntu / Linux Setup

```bash
# 1. Install FFmpeg
sudo apt-get install ffmpeg

# 2. Clone the repo
git clone https://github.com/PranoyProjects/vlogstudio.git
cd vlogstudio

# 3. Install Python dependencies
pip3 install fastapi "uvicorn[standard]" python-multipart openai-whisper --break-system-packages

# 4. Create required folders
mkdir -p uploads outputs luts db static

# 5. Run
uvicorn main:app --host 0.0.0.0 --port 8000

# 6. Open browser
# http://localhost:8000
```

> ⚠️ Step 3 downloads ~2GB (PyTorch for Whisper). Use good WiFi. Takes 10–30 min first time.

---

## 🪟 Windows Setup

### Step 1 — Install Python
- Go to https://python.org/downloads
- Download Python 3.11+
- ⚠️ **Check "Add Python to PATH"** on the first screen
- Click Install Now

### Step 2 — Install FFmpeg
Open Command Prompt and run:
```
winget install ffmpeg
```
Verify: `ffmpeg -version`

### Step 3 — Clone VlogStudio
```
git clone https://github.com/PranoyProjects/vlogstudio.git
cd vlogstudio
```

### Step 4 — Install dependencies
```
pip install fastapi "uvicorn[standard]" python-multipart openai-whisper
```
⚠️ Downloads ~2GB. Leave it running.

### Step 5 — Create folders
```
mkdir uploads outputs luts db static
```

### Step 6 — Run
```
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Step 7 — Open browser
```
http://localhost:8000
```

---

## 🍎 Mac Setup

```bash
# 1. Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install Python + FFmpeg
brew install python ffmpeg

# 3. Clone
git clone https://github.com/PranoyProjects/vlogstudio.git
cd vlogstudio

# 4. Install dependencies
pip3 install fastapi "uvicorn[standard]" python-multipart openai-whisper

# 5. Create folders
mkdir -p uploads outputs luts db static

# 6. Run
uvicorn main:app --host 0.0.0.0 --port 8000

# 7. Open
# http://localhost:8000
```

---

## ▶️ Starting the App (Every Time)

```bash
cd vlogstudio
uvicorn main:app --host 0.0.0.0 --port 8000
```
Then open `http://localhost:8000`

To stop: press `Ctrl+C`

---

## 🗺️ Pages

| URL | What it is |
|-----|-----------|
| `localhost:8000` | Main editor |
| `localhost:8000/route` | Route Animator |
| `localhost:8000/gopro` | GoPro Mode (standalone) |
| `localhost:8000/share/<token>` | Shared project link |

---

## 🎬 How to Make a Driving Vlog

1. **Upload** your driving video (dashcam or phone)
2. **Grade** tab — adjust colors or pick a preset
3. **FX** tab — add watermark, weather overlay, effects
4. **Overlay** tab — add map recording, set timer for when it shows
5. **GoPro** tab — add fisheye or stabilize if needed
6. **CC panel** (right) — click Auto-Transcribe for captions
7. **Export panel** (right) — pick format (16:9/9:16/1:1), click Export Video
8. Download the final MP4

---

## 🎥 GoPro Mode

**Post Processing:**
- Upload any video
- Pick a preset (Protune / Night / GoPro Look / Flat)
- Adjust fisheye, horizon level, superview
- Toggle stabilize, boomerang, wind noise fix
- Set timelapse speed or burst photo count
- Click Apply GoPro & Export

**Live Camera:**
- Click Start Camera (allow camera access)
- Pick a live filter (Protune, Night, Vivid, Mono, Warm, Cool)
- Toggle grid overlay, mirror flip
- Click Record → Stop → file auto-downloads as .webm
- Take snapshots with 📸 button

---

## 🗺️ Route Animator

1. Go to `localhost:8000/route`
2. Enter Source and Destination (autocomplete powered by OpenStreetMap)
3. Pick map style: Road / Satellite / Dark
4. Pick vehicle: Car / SUV / Bus / Bike / Flight / Ship
5. Adjust route color, animation speed, duration
6. Click Animate Route
7. Record & Export → download as video
8. Use it as map overlay in VlogStudio

---

## ⚡ Export Speed (No GPU — CPU only)

| Resolution | Speed |
|-----------|-------|
| 480p | Real-time or faster |
| 720p | ~2× slower than video length |
| 1080p | ~5× slower than video length |
| 4K | Not recommended |

Example: 10-minute 1080p video → ~50 minutes to export on CPU.

---

## 🔒 Security

VlogStudio has these protections built in:
- Rate limiting on all endpoints
- File type validation (video/audio only)
- 500MB max upload size
- Path traversal prevention
- Input sanitization for FFmpeg commands
- Secure HTTP headers (XSS, clickjacking protection)
- CORS locked to localhost only
- Auto file cleanup after 24 hours
- SQL injection prevention (parameterized queries)

Safe for local use. Not intended for public internet deployment without adding HTTPS + authentication.

---

## 🛠️ Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python + FastAPI |
| Video processing | FFmpeg |
| AI captions | OpenAI Whisper |
| Map/Route | Leaflet.js + OpenStreetMap + OSRM |
| Weather | Open-Meteo API (free) |
| Frontend | Vanilla HTML/CSS/JS |
| Database | SQLite (shares + comments) |

All free. No paid APIs. No accounts needed.

---

## 📁 Project Structure

```
vlogstudio/
├── main.py              # FastAPI backend — all routes + processing
├── templates/
│   ├── index.html       # Main editor UI
│   ├── route_animator.html  # Route animation page
│   ├── gopro.html       # GoPro mode standalone page
│   └── share.html       # Shared project viewer
├── static/
│   ├── favicon.svg      # App icon
│   ├── favicon-32.png   # Browser tab icon
│   └── favicon-192.png  # Mobile home screen icon
├── uploads/             # Uploaded videos (auto-cleaned after 24h)
├── outputs/             # Exported videos, thumbnails, SRT files
├── luts/                # Uploaded .cube LUT files
├── db/
│   └── vlogstudio.db    # SQLite — share links + comments
└── requirements.txt
```

---

## 🔧 Troubleshooting

**Port already in use:**
```bash
pkill -f uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000
```

**Static folder missing:**
```bash
mkdir -p uploads outputs luts db static
```

**Whisper not transcribing:**
```bash
pip3 install openai-whisper --break-system-packages
```

**Stabilization not working:**
FFmpeg needs vidstab plugin:
```bash
sudo apt-get install ffmpeg  # Ubuntu already includes it
```

**Export too slow:**
Use Low quality (CRF 28) for drafts, High (CRF 18) for final.

---

## 🚀 Future Ideas

- [ ] GPU acceleration (NVIDIA NVENC)
- [ ] Ghibli/AI style transfer (Stable Diffusion)
- [ ] Multi-camera sync
- [ ] Beat sync (cut to music)
- [ ] Face blur (OpenCV)
- [ ] Sky replacement
- [ ] Mobile app wrapper
- [ ] Cloud sync (optional)
- [ ] YouTube direct upload
- [ ] Custom caption fonts

---

## 👨‍💻 Built By

**Sai Pranoy Kumar** — Data Engineer, Hyderabad  
GitHub: [@PranoyProjects](https://github.com/PranoyProjects)

Built with Claude AI assistance.

---

## 📄 License

MIT License — free to use, modify, and share.
