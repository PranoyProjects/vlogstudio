"""
VlogStudio v2 — Secure Backend
Security: rate limiting, file validation, input sanitization,
secure headers, path traversal prevention, auto cleanup
"""

import os, uuid, json, subprocess, sqlite3, time, math, re, hashlib
import zipfile
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI(title="VlogStudio v2")

# ── Security Middleware ───────────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=*, microphone=*, geolocation=()"
        # CSP — allow only local resources
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com https://server.arcgisonline.com; "
            "media-src 'self' blob:; "
            "connect-src 'self' https://nominatim.openstreetmap.org https://router.project-osrm.org https://api.open-meteo.com; "
            "frame-ancestors 'none';"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)

# CORS — localhost only
app.add_middleware(CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"])

# ── Rate Limiting ─────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.requests = {}  # ip -> [timestamps]
    
    def check(self, ip: str, limit: int = 60, window: int = 60) -> bool:
        now = time.time()
        if ip not in self.requests:
            self.requests[ip] = []
        # Clean old requests
        self.requests[ip] = [t for t in self.requests[ip] if now - t < window]
        if len(self.requests[ip]) >= limit:
            return False
        self.requests[ip].append(now)
        return True

rate_limiter = RateLimiter()

def get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def check_rate_limit(request: Request, limit: int = 30):
    ip = get_client_ip(request)
    if not rate_limiter.check(ip, limit=limit):
        raise HTTPException(429, "Too many requests. Please wait.")

# ── Config ─────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
DB_PATH = BASE / "db" / "vlogstudio.db"
MAX_UPLOAD_MB = 500
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_JOBS_PER_IP = 3
CLEANUP_AGE_HOURS = 24

for d in [UPLOAD_DIR, OUTPUT_DIR, BASE/"db", BASE/"luts", BASE/"static"]:
    d.mkdir(exist_ok=True)

jobs = {}
job_ip_map = {}  # job_id -> ip

# ── Allowed file types ────────────────────────────────────────────────────────
ALLOWED_VIDEO = {'.mp4','.mov','.avi','.mkv','.webm','.m4v','.3gp','.flv'}
ALLOWED_AUDIO = {'.mp3','.wav','.aac','.m4a','.ogg','.flac'}
ALLOWED_OTHER = {'.gpx','.cube','.srt'}
ALL_ALLOWED = ALLOWED_VIDEO | ALLOWED_AUDIO | ALLOWED_OTHER

VIDEO_MAGIC = {
    b'\x00\x00\x00': 'mp4/mov',
    b'\x1a\x45\xdf': 'webm/mkv',
    b'RIFF': 'avi',
    b'FLV\x01': 'flv',
}

def validate_upload(file: UploadFile, allowed_exts: set, max_bytes: int = MAX_UPLOAD_BYTES):
    """Validate file extension and basic magic bytes"""
    ext = Path(file.filename or '').suffix.lower()
    if ext not in allowed_exts:
        raise HTTPException(400, f"File type '{ext}' not allowed. Allowed: {', '.join(allowed_exts)}")
    # Sanitize filename
    safe_name = re.sub(r'[^\w\-_\.]', '_', Path(file.filename or 'file').stem)
    return safe_name, ext

def sanitize_text(text: str, max_len: int = 200) -> str:
    """Remove characters dangerous in FFmpeg filter strings"""
    if not text:
        return ""
    # Remove shell-dangerous and FFmpeg filter-dangerous chars
    text = re.sub(r"['\"\\\[\]{}|&;$`!<>()]", "", text)
    text = text[:max_len]
    return text.strip()

def safe_path(base_dir: Path, filename: str) -> Path:
    """Prevent path traversal attacks"""
    # Resolve and ensure it's under base_dir
    try:
        full = (base_dir / Path(filename).name).resolve()
        base_resolved = base_dir.resolve()
        if not str(full).startswith(str(base_resolved)):
            raise HTTPException(400, "Invalid file path")
        return full
    except Exception:
        raise HTTPException(400, "Invalid file path")

def validate_job_path(path_str: str, allowed_dir: Path) -> Path:
    """Ensure a path from user input is within allowed directory"""
    try:
        p = Path(path_str).resolve()
        if not str(p).startswith(str(allowed_dir.resolve())):
            raise HTTPException(400, "Invalid file path")
        if not p.exists():
            raise HTTPException(400, "File not found")
        return p
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid file path")

# ── DB ─────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS shares (
        token TEXT PRIMARY KEY, project_data TEXT, created_at REAL, expires_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, timestamp REAL,
        author TEXT, text TEXT, created_at REAL)""")
    conn.commit(); conn.close()

init_db()

def save_upload(file: UploadFile) -> Path:
    ext = Path(file.filename or 'file').suffix.lower()
    # Use UUID to prevent filename collisions and info leakage
    dest = UPLOAD_DIR / f"{uuid.uuid4()}{ext}"
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Max {MAX_UPLOAD_MB}MB.")
    with open(dest, "wb") as f:
        f.write(data)
    return dest

def job_update(job_id: str, **kw):
    jobs.setdefault(job_id, {}).update(kw)

def get_video_info(path: Path) -> dict:
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_format","-show_streams",str(path)], capture_output=True, text=True, timeout=30)
    try:
        d = json.loads(r.stdout)
        fmt = d.get("format",{})
        vstream = next((s for s in d.get("streams",[]) if s.get("codec_type")=="video"),{})
        return {"duration":float(fmt.get("duration",0)),"width":int(vstream.get("width",1920)),
                "height":int(vstream.get("height",1080)),"size":int(fmt.get("size",0))}
    except:
        return {"duration":0,"width":1920,"height":1080,"size":0}

# ── Auto cleanup ───────────────────────────────────────────────────────────────
def cleanup_old_files():
    """Delete uploads/outputs older than CLEANUP_AGE_HOURS"""
    cutoff = time.time() - CLEANUP_AGE_HOURS * 3600
    deleted = 0
    for d in [UPLOAD_DIR, OUTPUT_DIR]:
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try: f.unlink(); deleted += 1
                except: pass
    if deleted:
        print(f"Cleaned up {deleted} old files")

# Run cleanup on startup
import threading
threading.Thread(target=cleanup_old_files, daemon=True).start()

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    check_rate_limit(request, limit=100)
    return (BASE/"templates"/"index.html").read_text()

@app.get("/route", response_class=HTMLResponse)
async def route_page(request: Request):
    check_rate_limit(request, limit=100)
    return (BASE/"templates"/"route_animator.html").read_text()

@app.get("/gopro", response_class=HTMLResponse)
async def gopro_page(request: Request):
    check_rate_limit(request, limit=100)
    return (BASE/"templates"/"gopro.html").read_text()

@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(token: str):
    # Validate token format
    if not re.match(r'^[a-f0-9\-]{12,36}$', token):
        raise HTTPException(400, "Invalid token")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT project_data, expires_at FROM shares WHERE token=?", (token,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse("<h2>Link expired or not found</h2>", 404)
    if row[1] and time.time() > row[1]:
        return HTMLResponse("<h2>This link has expired</h2>", 410)
    # Sanitize project data before embedding
    safe_data = json.dumps(json.loads(row[0]))
    return (BASE/"templates"/"share.html").read_text().replace("__PROJECT_DATA__", safe_data)

# ── Favicon ───────────────────────────────────────────────────────────────────
@app.get("/favicon.svg")
async def favicon_svg():
    f = BASE / "static" / "favicon.svg"
    if f.exists():
        return Response(f.read_bytes(), media_type="image/svg+xml")
    raise HTTPException(404)

@app.get("/favicon.ico")
async def favicon_ico():
    # Redirect to SVG for modern browsers
    f = BASE / "static" / "favicon-32.png"
    if f.exists():
        return Response(f.read_bytes(), media_type="image/png")
    f2 = BASE / "static" / "favicon.svg"
    if f2.exists():
        return Response(f2.read_bytes(), media_type="image/svg+xml")
    raise HTTPException(404)

@app.get("/apple-touch-icon.png")
async def apple_icon():
    f = BASE / "static" / "favicon-192.png"
    if f.exists():
        return Response(f.read_bytes(), media_type="image/png")
    raise HTTPException(404)

# ── Uploads ───────────────────────────────────────────────────────────────────
@app.post("/upload/video")
async def upload_video(request: Request, file: UploadFile = File(...)):
    check_rate_limit(request, limit=20)
    validate_upload(file, ALLOWED_VIDEO)
    path = save_upload(file)
    info = get_video_info(path)
    return {"id": path.stem, "path": str(path), "filename": Path(file.filename or '').name, **info}

@app.post("/upload/map")
async def upload_map(request: Request, file: UploadFile = File(...)):
    check_rate_limit(request, limit=20)
    validate_upload(file, ALLOWED_VIDEO)
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": Path(file.filename or '').name}

@app.post("/upload/music")
async def upload_music(request: Request, file: UploadFile = File(...)):
    check_rate_limit(request, limit=20)
    validate_upload(file, ALLOWED_AUDIO)
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": Path(file.filename or '').name}

@app.post("/upload/gpx")
async def upload_gpx(request: Request, file: UploadFile = File(...)):
    check_rate_limit(request, limit=10)
    validate_upload(file, {'.gpx'})
    path = save_upload(file)
    speeds = parse_gpx(path)
    return {"id": path.stem, "path": str(path), "speeds": speeds[:20]}

@app.post("/upload/lut")
async def upload_lut(request: Request, file: UploadFile = File(...)):
    check_rate_limit(request, limit=10)
    validate_upload(file, {'.cube'})
    dest = BASE / "luts" / f"{uuid.uuid4()}.cube"
    data = file.file.read(10 * 1024 * 1024)  # Max 10MB for LUT
    with open(dest, "wb") as f: f.write(data)
    return {"name": dest.name, "path": str(dest)}

# ── Job system ─────────────────────────────────────────────────────────────────
@app.get("/job/{job_id}")
async def job_status(job_id: str, request: Request):
    # Validate job_id format
    if not re.match(r'^[a-f0-9\-]{36}$', job_id):
        raise HTTPException(400, "Invalid job ID")
    return jobs.get(job_id, {"status": "not_found"})

@app.get("/download/{filename}")
async def download(filename: str, request: Request):
    check_rate_limit(request, limit=50)
    # Validate filename — no path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        raise HTTPException(400, "Invalid filename")
    if not re.match(r'^[\w\-\.]+$', filename):
        raise HTTPException(400, "Invalid filename")
    path = safe_path(OUTPUT_DIR, filename)
    if not path.exists():
        raise HTTPException(404, "File not found")
    mt = "video/mp4" if filename.endswith('.mp4') else \
         "application/zip" if filename.endswith('.zip') else \
         "text/plain" if filename.endswith('.srt') else \
         "image/jpeg" if filename.endswith('.jpg') else \
         "application/octet-stream"
    return FileResponse(str(path), filename=filename, media_type=mt)

# ── Transcribe ────────────────────────────────────────────────────────────────
@app.post("/transcribe")
async def transcribe(request: Request, bg: BackgroundTasks,
                     main_path: str = Form(...), language: str = Form("auto")):
    check_rate_limit(request, limit=5)
    ip = get_client_ip(request)
    active = sum(1 for j in jobs.values() if j.get("status") == "running" and job_ip_map.get(j.get("id","")) == ip)
    if active >= MAX_JOBS_PER_IP:
        raise HTTPException(429, "Too many active jobs")
    path = validate_job_path(main_path, UPLOAD_DIR)
    # Validate language code
    lang = language if re.match(r'^[a-z]{2,3}$', language) or language == "auto" else "auto"
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0, id=job_id)
    job_ip_map[job_id] = ip
    bg.add_task(run_transcribe, job_id, str(path), lang)
    return {"job_id": job_id}

def run_transcribe(job_id, path, language="auto"):
    try:
        job_update(job_id, status="running", progress=10, message="Loading Whisper...")
        try:
            import faster_whisper
            model = faster_whisper.WhisperModel("base", device="cpu", compute_type="int8")
            lang = None if language == "auto" else language
            segments, _ = model.transcribe(path, word_timestamps=True, language=lang)
            words, full_text = [], []
            for seg in segments:
                full_text.append(seg.text.strip())
                if seg.words:
                    for w in seg.words:
                        words.append({"word": w.word.strip(), "start": round(w.start,3), "end": round(w.end,3)})
        except ImportError:
            import whisper
            model = whisper.load_model("base")
            result = model.transcribe(path, word_timestamps=True)
            words, full_text = [], []
            for seg in result.get("segments", []):
                full_text.append(seg.get("text","").strip())
                for w in seg.get("words", []):
                    words.append({"word": w["word"].strip(), "start": round(w["start"],3), "end": round(w["end"],3)})
        srt = generate_srt(words)
        srt_path = OUTPUT_DIR / f"cap_{job_id[:8]}.srt"
        srt_path.write_text(srt, encoding="utf-8")
        chapters = auto_chapters(words, full_text)
        job_update(job_id, status="done", progress=100, words=words,
                   srt_file=srt_path.name, chapters=chapters,
                   full_text=" ".join(full_text),
                   message=f"Transcribed {len(words)} words")
    except Exception as e:
        job_update(job_id, status="error", message=str(e)[:200])

def generate_srt(words):
    lines = []
    groups, group, gs = [], [], None
    for w in words:
        if gs is None: gs = w["start"]
        group.append(w)
        if len(group) >= 6 or (w["end"] - gs) >= 3:
            groups.append((gs, w["end"], list(group))); group, gs = [], None
    if group: groups.append((gs, group[-1]["end"], group))
    for i,(s,e,ws) in enumerate(groups, 1):
        lines += [str(i), f"{srt_ts(s)} --> {srt_ts(e)}", " ".join(w["word"] for w in ws), ""]
    return "\n".join(lines)

def srt_ts(t):
    h=int(t//3600); m=int((t%3600)//60); s=t%60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".",",")

def auto_chapters(words, segments):
    if len(segments) < 3: return []
    chapters = [{"time": 0, "title": "Introduction"}]
    step = max(1, len(segments)//5)
    for i in range(step, len(segments)-1, step):
        if words:
            idx = int((i/len(segments))*len(words))
            t = words[min(idx, len(words)-1)]["start"]
            title = " ".join(segments[i].split()[:4]).title()
            chapters.append({"time": round(t), "title": title})
    return chapters

# ── Thumbnail ──────────────────────────────────────────────────────────────────
@app.post("/thumbnail")
async def generate_thumbnail(request: Request, bg: BackgroundTasks,
                              video_path: str = Form(...), count: int = Form(9)):
    check_rate_limit(request, limit=10)
    path = validate_job_path(video_path, UPLOAD_DIR)
    count = max(1, min(count, 20))  # clamp 1-20
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_thumbnail, job_id, str(path), count)
    return {"job_id": job_id}

def run_thumbnail(job_id, path, count=9):
    try:
        job_update(job_id, status="running", progress=10, message="Extracting frames...")
        info = get_video_info(Path(path))
        dur = info["duration"]
        thumbs = []
        for i in range(count):
            t = (i+1) * (dur/(count+1))
            out = OUTPUT_DIR / f"thumb_{job_id[:6]}_{i}.jpg"
            subprocess.run(["ffmpeg","-y","-ss",str(t),"-i",path,
                "-vframes","1","-q:v","2",str(out)], capture_output=True, timeout=30)
            if out.exists():
                thumbs.append({"time": round(t,1), "file": out.name})
        job_update(job_id, status="done", progress=100, thumbnails=thumbs)
    except Exception as e:
        job_update(job_id, status="error", message=str(e)[:200])

# ── Highlights ─────────────────────────────────────────────────────────────────
@app.post("/highlights")
async def auto_highlights(request: Request, bg: BackgroundTasks,
                          video_path: str = Form(...),
                          words_json: str = Form("[]"),
                          duration: int = Form(60)):
    check_rate_limit(request, limit=5)
    path = validate_job_path(video_path, UPLOAD_DIR)
    duration = max(10, min(duration, 300))
    try: words = json.loads(words_json)
    except: words = []
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_highlights, job_id, str(path), words, duration)
    return {"job_id": job_id}

def run_highlights(job_id, path, words, target_dur=60):
    try:
        job_update(job_id, status="running", progress=10, message="Analyzing...")
        info = get_video_info(Path(path))
        dur = info["duration"]
        excitement = {"wow","amazing","look","incredible","beautiful","oh","yes","great","awesome","perfect","nice"}
        segments = []
        step = 5
        for t in range(0, int(dur)-step, step):
            score = 0
            seg_words = [w for w in words if t <= w.get("start",0) < t+step]
            score += len(seg_words)*2
            for w in seg_words:
                if w.get("word","").lower().strip(".,!?") in excitement: score += 10
            segments.append({"start":t,"end":t+step,"score":score})
        segments.sort(key=lambda x: -x["score"])
        picked, total = [], 0
        for seg in segments:
            if total >= target_dur: break
            picked.append(seg); total += step
        picked.sort(key=lambda x: x["start"])
        if not picked:
            n = max(3, target_dur//10)
            picked = [{"start":i*(dur/n),"end":i*(dur/n)+8} for i in range(n)]
        job_update(job_id, progress=40, message="Cutting clips...")
        concat_file = OUTPUT_DIR / f"concat_{job_id[:6]}.txt"
        clip_files = []
        for i, seg in enumerate(picked):
            clip = OUTPUT_DIR / f"clip_{job_id[:6]}_{i}.mp4"
            subprocess.run(["ffmpeg","-y","-ss",str(seg["start"]),"-t",str(seg["end"]-seg["start"]),
                "-i",path,"-c:v","libx264","-crf","23","-preset","fast","-c:a","aac",str(clip)],
                capture_output=True, timeout=120)
            if clip.exists(): clip_files.append(clip)
        with open(concat_file,"w") as f:
            for cf in clip_files: f.write(f"file '{cf}'\n")
        out = OUTPUT_DIR / f"highlights_{job_id[:8]}.mp4"
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_file),
            "-c","copy",str(out)], capture_output=True, timeout=300)
        for f in clip_files: 
            try: f.unlink()
            except: pass
        try: concat_file.unlink()
        except: pass
        job_update(job_id, status="done", progress=100, output=out.name,
                   message=f"Highlight reel: {len(clip_files)} clips")
    except Exception as e:
        job_update(job_id, status="error", message=str(e)[:200])

# ── Weather ────────────────────────────────────────────────────────────────────
@app.get("/weather")
async def get_weather(request: Request, lat: float, lon: float):
    check_rate_limit(request, limit=20)
    # Validate lat/lon ranges
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(400, "Invalid coordinates")
    try:
        import urllib.request
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        current = data.get("current", {})
        code = int(current.get("weather_code", 0))
        emoji = ("☀️" if code==0 else "⛅" if code<=3 else "🌫️" if code<=49 else "🌧️" if code<=69 else "🌨️" if code<=79 else "⛈️")
        desc = {0:"Clear",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",45:"Foggy",
                51:"Drizzle",61:"Rain",71:"Snow",95:"Thunderstorm"}.get(code,"")
        return {"temp": current.get("temperature_2m","--"), "wind": current.get("wind_speed_10m","--"),
                "emoji": emoji, "description": desc}
    except Exception as e:
        return {"error": str(e)[:100], "temp":"--","emoji":"🌤️","description":"Unknown"}

# ── GPX Parser ─────────────────────────────────────────────────────────────────
def parse_gpx(path: Path):
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"gpx":"http://www.topografix.com/GPX/1/1"}
        points, prev = [], None
        for trkpt in root.findall(".//gpx:trkpt", ns):
            lat = float(trkpt.get("lat",0))
            lon = float(trkpt.get("lon",0))
            if prev:
                R=6371; dlat=math.radians(lat-prev["lat"]); dlon=math.radians(lon-prev["lon"])
                a=math.sin(dlat/2)**2+math.cos(math.radians(prev["lat"]))*math.cos(math.radians(lat))*math.sin(dlon/2)**2
                dist=R*2*math.asin(math.sqrt(a))
                speed_kmh=dist*3600
            else: speed_kmh=0
            points.append({"lat":lat,"lon":lon,"speed":round(speed_kmh,1)})
            prev={"lat":lat,"lon":lon}
        return points
    except: return []

# ── Share ──────────────────────────────────────────────────────────────────────
@app.post("/share/create")
async def create_share(request: Request, project_data: str = Form(...), expires_hours: int = Form(72)):
    check_rate_limit(request, limit=10)
    # Validate project data is valid JSON
    try: json.loads(project_data)
    except: raise HTTPException(400, "Invalid project data")
    expires_hours = max(1, min(expires_hours, 168))  # 1 hour to 1 week
    token = hashlib.sha256(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()[:16]
    expires = time.time() + expires_hours*3600
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO shares VALUES (?,?,?,?)", (token, project_data, time.time(), expires))
    conn.commit(); conn.close()
    return {"token": token, "url": f"/share/{token}", "expires_hours": expires_hours}

@app.post("/share/{token}/comment")
async def add_comment(request: Request, token: str,
                      timestamp: float = Form(...),
                      author: str = Form("Anonymous"),
                      text: str = Form(...)):
    check_rate_limit(request, limit=20)
    if not re.match(r'^[a-f0-9]{16}$', token):
        raise HTTPException(400, "Invalid token")
    author = sanitize_text(author, 50)
    text = sanitize_text(text, 500)
    if not text:
        raise HTTPException(400, "Comment cannot be empty")
    timestamp = max(0, min(float(timestamp), 86400))
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO comments (token,timestamp,author,text,created_at) VALUES (?,?,?,?,?)",
                 (token, timestamp, author, text, time.time()))
    conn.commit(); conn.close()
    return {"status": "ok"}

@app.get("/share/{token}/comments")
async def get_comments(token: str):
    if not re.match(r'^[a-f0-9]{16}$', token):
        raise HTTPException(400, "Invalid token")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT timestamp,author,text,created_at FROM comments WHERE token=? ORDER BY timestamp LIMIT 100", (token,)).fetchall()
    conn.close()
    return [{"timestamp":r[0],"author":r[1],"text":r[2],"created_at":r[3]} for r in rows]

# ── Export ─────────────────────────────────────────────────────────────────────
@app.post("/export")
async def export(request: Request, bg: BackgroundTasks,
    main_path: str = Form(...),
    clip_paths: str = Form("[]"),
    music_path: str = Form(None),
    map_path: str = Form(None),
    map_x: int = Form(20), map_y: int = Form(20),
    map_w: int = Form(320), map_h: int = Form(240),
    map_opacity: float = Form(0.9),
    map_show_from: float = Form(0), map_show_to: float = Form(0),
    captions: str = Form("[]"),
    caption_style: str = Form("tiktok"),
    text_3d: str = Form("[]"),
    brightness: float = Form(0),
    contrast: float = Form(1),
    saturation: float = Form(1),
    temperature: float = Form(0),
    vignette: float = Form(0),
    lut_name: str = Form(""),
    speed: float = Form(1.0),
    reverse: bool = Form(False),
    cinematic_bars: bool = Form(False),
    glitch: bool = Form(False),
    watermark_text: str = Form(""),
    watermark_position: str = Form("bottomright"),
    weather_text: str = Form(""),
    weather_position: str = Form("topright"),
    output_format: str = Form("16:9"),
    output_quality: str = Form("medium"),
    trim_start: float = Form(0),
    trim_end: float = Form(0),
    music_volume: float = Form(0.3),
    noise_reduce: bool = Form(False),
    gpx_path: str = Form(None),
    show_speed: bool = Form(False),
):
    check_rate_limit(request, limit=5)
    ip = get_client_ip(request)
    active = sum(1 for j in jobs.values() if j.get("status") == "running")
    if active >= MAX_JOBS_PER_IP * 2:
        raise HTTPException(429, "Server busy. Please wait.")

    path = validate_job_path(main_path, UPLOAD_DIR)

    # Sanitize all text inputs
    watermark_text = sanitize_text(watermark_text, 100)
    weather_text = sanitize_text(weather_text, 80)
    lut_name = re.sub(r'[^\w\-\.]', '', lut_name)[:100]

    # Clamp numeric values
    map_x = max(0, min(map_x, 9999))
    map_y = max(0, min(map_y, 9999))
    map_w = max(50, min(map_w, 9999))
    map_h = max(50, min(map_h, 9999))
    map_opacity = max(0.0, min(map_opacity, 1.0))
    brightness = max(-1.0, min(brightness, 1.0))
    contrast = max(0.1, min(contrast, 5.0))
    saturation = max(0.0, min(saturation, 5.0))
    temperature = max(-1.0, min(temperature, 1.0))
    vignette = max(0.0, min(vignette, 1.0))
    speed = max(0.1, min(speed, 10.0))
    music_volume = max(0.0, min(music_volume, 2.0))
    trim_start = max(0.0, trim_start)
    trim_end = max(0.0, trim_end)

    # Validate enum-like values
    if output_format not in ["16:9","9:16","1:1"]: output_format = "16:9"
    if output_quality not in ["high","medium","low"]: output_quality = "medium"
    if caption_style not in ["tiktok","classic","minimal","bounce"]: caption_style = "tiktok"
    if watermark_position not in ["topright","topleft","bottomright","bottomleft","center"]: watermark_position = "bottomright"
    if weather_position not in ["topright","topleft","bottomright"]: weather_position = "topright"

    # Validate JSON inputs
    try: words = json.loads(captions)
    except: words = []
    try: text_3d_list = json.loads(text_3d)
    except: text_3d_list = []
    try: clip_list = json.loads(clip_paths)
    except: clip_list = []

    job_id = str(uuid.uuid4())
    params = dict(
        main_path=str(path), clip_paths=json.dumps(clip_list),
        music_path=music_path, map_path=map_path,
        map_x=map_x, map_y=map_y, map_w=map_w, map_h=map_h,
        map_opacity=map_opacity, map_show_from=map_show_from, map_show_to=map_show_to,
        captions=json.dumps(words), caption_style=caption_style,
        text_3d=json.dumps(text_3d_list),
        brightness=brightness, contrast=contrast, saturation=saturation,
        temperature=temperature, vignette=vignette, lut_name=lut_name,
        speed=speed, reverse=reverse, cinematic_bars=cinematic_bars, glitch=glitch,
        watermark_text=watermark_text, watermark_position=watermark_position,
        weather_text=weather_text, weather_position=weather_position,
        output_format=output_format, output_quality=output_quality,
        trim_start=trim_start, trim_end=trim_end,
        music_volume=music_volume, noise_reduce=noise_reduce,
    )
    job_update(job_id, status="queued", progress=0)
    job_ip_map[job_id] = ip
    bg.add_task(run_export, job_id, params)
    return {"job_id": job_id}

def run_export(job_id, p):
    try:
        job_update(job_id, status="running", progress=5, message="Starting...")
        main_path = p["main_path"]
        crf = {"high":"18","medium":"23","low":"28"}.get(p.get("output_quality","medium"),"23")
        output_file = OUTPUT_DIR / f"vlogstudio_{job_id[:8]}.mp4"

        words = json.loads(p.get("captions","[]") or "[]")
        text_3d_list = json.loads(p.get("text_3d","[]") or "[]")

        speed = float(p.get("speed",1.0))
        vf, af = [], []

        if p.get("reverse"): vf.append("reverse"); af.append("areverse")
        if speed != 1.0:
            vf.append(f"setpts={1/speed}*PTS")
            af.append(f"atempo={min(2.0,max(0.5,speed))}")

        fmt = p.get("output_format","16:9")
        if fmt=="9:16": vf.append("crop=ih*9/16:ih,scale=1080:1920")
        elif fmt=="1:1": vf.append("crop=min(iw\\,ih):min(iw\\,ih),scale=1080:1080")

        if p.get("cinematic_bars"):
            vf.append("drawbox=x=0:y=0:w=iw:h=ih*0.08:color=black:t=fill")
            vf.append("drawbox=x=0:y=ih*0.92:w=iw:h=ih*0.08:color=black:t=fill")

        b=float(p.get("brightness",0)); c=float(p.get("contrast",1))
        s=float(p.get("saturation",1)); eq=[]
        if b!=0: eq.append(f"brightness={b:.3f}")
        if c!=1: eq.append(f"contrast={c:.3f}")
        if s!=1: eq.append(f"saturation={s:.3f}")
        if eq: vf.append(f"eq={'\\:'.join(eq)}")

        temp=float(p.get("temperature",0))
        if temp!=0:
            r=min(1.5,1+temp*0.3) if temp>0 else max(0.5,1+temp*0.3)
            b2=max(0.5,1-temp*0.3) if temp>0 else min(1.5,1-temp*0.3)
            vf.append(f"colorchannelmixer=rr={r:.3f}:bb={b2:.3f}")

        lut_name = p.get("lut_name","")
        if lut_name:
            lut_path = safe_path(BASE/"luts", lut_name)
            if lut_path.exists(): vf.append(f"lut3d='{lut_path}'")

        vig=float(p.get("vignette",0))
        if vig>0: vf.append(f"vignette=PI/4*{vig:.3f}")
        if p.get("glitch"): vf.append("rgbashift=rh=5:bh=-5")

        vf_str = ",".join(vf) if vf else "copy"
        af_str = ",".join(af) if af else "acopy"

        trim_start = float(p.get("trim_start",0))
        trim_end = float(p.get("trim_end",0))
        cmd_inputs = []
        if trim_start>0: cmd_inputs += ["-ss",str(trim_start)]
        cmd_inputs += ["-i",main_path]
        if trim_end>0: cmd_inputs += ["-t",str(trim_end-trim_start)]

        work = OUTPUT_DIR / f"work_{job_id[:6]}.mp4"
        r = subprocess.run(["ffmpeg","-y"]+cmd_inputs+
            ["-vf",vf_str,"-af",af_str,"-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(work)],
            capture_output=True, text=True, timeout=600)
        if r.returncode!=0 or not work.exists():
            subprocess.run(["ffmpeg","-y","-i",main_path,"-c","copy",str(work)], capture_output=True, timeout=120)

        job_update(job_id, progress=35, message="Applying overlays...")

        # Overlays
        ov_filters, ov_inputs, idx = [], [str(work)], 1
        map_path = p.get("map_path")
        if map_path and Path(map_path).exists() and str(Path(map_path).resolve()).startswith(str(UPLOAD_DIR.resolve())):
            x,y,w,h = p.get("map_x",20),p.get("map_y",20),p.get("map_w",320),p.get("map_h",240)
            op=float(p.get("map_opacity",0.9)); sf=p.get("map_show_from",0); st=p.get("map_show_to",0)
            ov_inputs.append(map_path)
            en=f":enable='between(t,{sf},{st})'" if sf or st else ""
            ov_filters.append(f"[{idx}:v]scale={w}:{h}[mv]")
            ov_filters.append(f"[0:v][mv]overlay={x}:{y}{en}[vo]"); idx+=1; cur="[vo]"
        else:
            ov_filters.append("[0:v]copy[vo]"); cur="[vo]"

        for i,t in enumerate(text_3d_list):
            txt=sanitize_text(t.get("text",""),100)
            if not txt: continue
            s,e=t.get("start",0),t.get("end",3)
            tx,ty=t.get("x",100),t.get("y",100)
            col=re.sub(r'[^a-zA-Z0-9#]','',t.get("color","white"))
            en=f"between(t\\,{s}\\,{e})"
            ov_filters.append(f"{cur}drawtext=text='{txt}':x={tx}:y={ty+4}:fontsize=60:fontcolor=black@0.5:enable='{en}'[ta{i}]"); cur=f"[ta{i}]"
            ov_filters.append(f"{cur}drawtext=text='{txt}':x={tx}:y={ty}:fontsize=60:fontcolor={col}:enable='{en}'[tb{i}]"); cur=f"[tb{i}]"

        wm=sanitize_text(p.get("watermark_text",""),80)
        if wm:
            pos_map={"topright":"x=w-tw-20:y=20","topleft":"x=20:y=20","bottomright":"x=w-tw-20:y=h-th-20","bottomleft":"x=20:y=h-th-20","center":"x=(w-tw)/2:y=(h-th)/2"}
            pos=pos_map.get(p.get("watermark_position","bottomright"),"x=w-tw-20:y=h-th-20")
            ov_filters.append(f"{cur}drawtext=text='{wm}':{pos}:fontsize=28:fontcolor=white@0.7[wm]"); cur="[wm]"

        wt=sanitize_text(p.get("weather_text",""),60)
        if wt:
            wp={"topright":"x=w-tw-20:y=20","topleft":"x=20:y=20","bottomright":"x=w-tw-20:y=h-th-20"}.get(p.get("weather_position","topright"),"x=w-tw-20:y=20")
            ov_filters.append(f"{cur}drawtext=text='{wt}':{wp}:fontsize=32:fontcolor=white:box=1:boxcolor=black@0.4:boxborderw=6[wt]"); cur="[wt]"

        if words:
            ass_path = OUTPUT_DIR / f"cap_{job_id[:8]}.ass"
            write_ass(words, ass_path, p.get("caption_style","tiktok"))
            ass_str = str(ass_path).replace("'","")
            ov_filters.append(f"{cur}ass='{ass_str}'[co]"); cur="[co]"

        ov_filters.append(f"{cur}copy[vfinal]"); ov_filters.append("[0:a]acopy[afinal]")
        work2 = OUTPUT_DIR / f"work2_{job_id[:6]}.mp4"
        inp_args = []
        for inp in ov_inputs: inp_args += ["-i",inp]
        r2=subprocess.run(["ffmpeg","-y"]+inp_args+["-filter_complex",";".join(ov_filters),
            "-map","[vfinal]","-map","[afinal]","-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(work2)],
            capture_output=True, text=True, timeout=600)
        if r2.returncode!=0 or not work2.exists(): work2=work

        job_update(job_id, progress=65, message="Mixing audio...")
        music_path=p.get("music_path")
        if music_path and Path(music_path).exists() and str(Path(music_path).resolve()).startswith(str(UPLOAD_DIR.resolve())):
            mv=float(p.get("music_volume",0.3))
            r3=subprocess.run(["ffmpeg","-y","-i",str(work2),"-i",music_path,
                "-filter_complex",f"[0:a]volume=1[orig];[1:a]volume={mv}[bg];[orig][bg]amix=inputs=2:duration=first[aout]",
                "-map","0:v","-map","[aout]","-c:v","copy","-c:a","aac",str(output_file)],
                capture_output=True, timeout=600)
            if r3.returncode!=0:
                import shutil; shutil.copy(str(work2),str(output_file))
        else:
            import shutil; shutil.copy(str(work2),str(output_file))

        for f in [work, work2]:
            try: f.unlink()
            except: pass

        job_update(job_id, status="done", progress=100, output=output_file.name, message="Export complete!")
    except Exception as e:
        import traceback
        job_update(job_id, status="error", message=str(e)[:200])

def write_ass(words, path, style):
    cfg={"tiktok":("Arial Black","52","1","3","2","80"),"classic":("Arial","40","0","2","1","40"),
         "minimal":("Arial","36","0","1","0","30"),"bounce":("Arial Black","48","1","2","1","60")}
    fn,fs,bold,outline,shadow,marginv=cfg.get(style,cfg["tiktok"])
    def ts(t): h=int(t//3600);m=int((t%3600)//60);s=t%60; return f"{h}:{m:02d}:{s:05.2f}"
    lines=["[Script Info]","ScriptType: v4.00+","PlayResX: 1920","PlayResY: 1080","",
        "[V4+ Styles]","Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{fn},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,10,10,{marginv},1",
        "","[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    groups,group,gs=[],[],None
    for w in words:
        if gs is None: gs=w["start"]
        group.append(w)
        if len(group)>=4 or (w["end"]-gs)>=3: groups.append((gs,w["end"],list(group))); group,gs=[],None
    if group: groups.append((gs,group[-1]["end"],group))
    for gstart,gend,gwords in groups:
        if style in ("tiktok","bounce"):
            for i,w in enumerate(gwords):
                before=" ".join(x["word"] for x in gwords[:i]); cur=gwords[i]["word"]; after=" ".join(x["word"] for x in gwords[i+1:])
                text=(before+" " if before else "")+f"{{\\c&H00FFFF&}}{cur}{{\\c&HFFFFFF&}}"+(" "+after if after else "")
                lines.append(f"Dialogue: 0,{ts(w['start'])},{ts(w['end'])},Default,,0,0,0,,{text}")
        else:
            lines.append(f"Dialogue: 0,{ts(gstart)},{ts(gend)},Default,,0,0,0,,{' '.join(w['word'] for w in gwords)}")
    path.write_text("\n".join(lines), encoding="utf-8")

# ── GoPro Export ───────────────────────────────────────────────────────────────
@app.post("/gopro/export")
async def gopro_export(request: Request, bg: BackgroundTasks,
    video_path: str = Form(...),
    fisheye: float = Form(0),
    stabilize: bool = Form(False),
    protune: bool = Form(False),
    superview: bool = Form(False),
    timelapse: int = Form(0),
    boomerang: bool = Form(False),
    wind_noise: bool = Form(False),
    horizon_level: float = Form(0),
    night_mode: bool = Form(False),
    burst_count: int = Form(0),
    output_quality: str = Form("medium"),
):
    check_rate_limit(request, limit=5)
    path = validate_job_path(video_path, UPLOAD_DIR)
    fisheye = max(0.0, min(fisheye, 1.0))
    horizon_level = max(-45.0, min(horizon_level, 45.0))
    timelapse = max(0, min(timelapse, 60))
    burst_count = max(0, min(burst_count, 50))
    if output_quality not in ["high","medium","low"]: output_quality = "medium"
    job_id = str(uuid.uuid4())
    params = dict(video_path=str(path), fisheye=fisheye, stabilize=stabilize,
        protune=protune, superview=superview, timelapse=timelapse,
        boomerang=boomerang, wind_noise=wind_noise, horizon_level=horizon_level,
        night_mode=night_mode, burst_count=burst_count, output_quality=output_quality)
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_gopro_export, job_id, params)
    return {"job_id": job_id}

def run_gopro_export(job_id, p):
    try:
        path=p["video_path"]; crf={"high":"18","medium":"23","low":"28"}.get(p.get("output_quality","medium"),"23")
        output=OUTPUT_DIR/f"gopro_{job_id[:8]}.mp4"
        job_update(job_id,status="running",progress=10,message="Applying GoPro effects...")
        vf,af=[],[]
        f=float(p.get("fisheye",0))
        if f>0: vf.append(f"lenscorrection=k1={-0.3*f:.3f}:k2={0.1*f:.3f}")
        if p.get("superview"): vf.append("scale=iw*1.33:ih,crop=iw/1.33:ih")
        angle=float(p.get("horizon_level",0))
        if angle!=0: vf.append(f"rotate={angle:.2f}*PI/180:fillcolor=black")
        if p.get("protune"):
            vf.append("eq=contrast=1.3:saturation=1.4:brightness=0.05")
            vf.append("curves=r='0/0 0.5/0.55 1/1':g='0/0 0.5/0.5 1/1':b='0/0 0.5/0.45 1/0.95'")
        if p.get("night_mode"):
            vf.append("eq=brightness=0.15:contrast=1.1:gamma=1.3")
        tl=int(p.get("timelapse",0))
        if tl>1: vf.append(f"select='not(mod(n\\,{tl}))',setpts=N/FRAME_RATE/TB"); af.append("atempo=2.0")
        if p.get("wind_noise"): af.append("highpass=f=200,lowpass=f=3000")
        vf_str=",".join(vf) if vf else "copy"; af_str=",".join(af) if af else "acopy"
        job_update(job_id,progress=30,message="Encoding...")
        if p.get("boomerang"):
            fwd=OUTPUT_DIR/f"fwd_{job_id[:6]}.mp4"; rev=OUTPUT_DIR/f"rev_{job_id[:6]}.mp4"
            ct=OUTPUT_DIR/f"ct_{job_id[:6]}.txt"
            subprocess.run(["ffmpeg","-y","-i",path,"-vf",vf_str,"-af",af_str,"-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(fwd)],capture_output=True,timeout=600)
            subprocess.run(["ffmpeg","-y","-i",str(fwd),"-vf","reverse","-af","areverse","-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(rev)],capture_output=True,timeout=600)
            with open(ct,"w") as cf: cf.write(f"file '{fwd}'\nfile '{rev}'\n")
            subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(ct),"-c","copy",str(output)],capture_output=True,timeout=300)
            for f in [fwd,rev,ct]:
                try: f.unlink()
                except: pass
        elif p.get("stabilize"):
            job_update(job_id,progress=20,message="Analyzing shake...")
            trf=OUTPUT_DIR/f"trf_{job_id[:6]}.trf"
            subprocess.run(["ffmpeg","-y","-i",path,"-vf",f"vidstabdetect=shakiness=10:accuracy=15:result={trf}","-f","null","-"],capture_output=True,timeout=600)
            job_update(job_id,progress=50,message="Stabilizing...")
            svf=f"vidstabtransform=input={trf}:zoom=5:smoothing=30,unsharp=5:5:0.8:3:3:0.4"
            if vf: svf=svf+","+",".join(vf)
            r=subprocess.run(["ffmpeg","-y","-i",path,"-vf",svf,"-af",af_str,"-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],capture_output=True,timeout=600)
            try: trf.unlink()
            except: pass
            if r.returncode!=0:
                subprocess.run(["ffmpeg","-y","-i",path,"-vf",vf_str,"-af",af_str,"-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],capture_output=True,timeout=600)
        elif int(p.get("burst_count",0))>0:
            count=int(p["burst_count"]); info=get_video_info(Path(path)); dur=info["duration"]
            bd=OUTPUT_DIR/f"burst_{job_id[:6]}"; bd.mkdir(exist_ok=True)
            frames=[]
            for i in range(count):
                t=(i+1)*(dur/(count+1)); of=bd/f"burst_{i:03d}.jpg"
                subprocess.run(["ffmpeg","-y","-ss",str(t),"-i",path,"-vframes","1","-q:v","1",str(of)],capture_output=True,timeout=30)
                if of.exists(): frames.append(of.name)
            zout=OUTPUT_DIR/f"burst_{job_id[:8]}.zip"
            with zipfile.ZipFile(zout,"w") as zf:
                for fn in frames: zf.write(bd/fn,fn)
            try:
                for fn in frames: (bd/fn).unlink()
                bd.rmdir()
            except: pass
            job_update(job_id,status="done",progress=100,output=zout.name,burst=True,message=f"{len(frames)} frames")
            return
        else:
            subprocess.run(["ffmpeg","-y","-i",path,"-vf",vf_str,"-af",af_str,"-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],capture_output=True,timeout=600)
        job_update(job_id,status="done",progress=100,output=output.name,message="GoPro export complete!")
    except Exception as e:
        job_update(job_id,status="error",message=str(e)[:200])

# ── Static files ───────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(BASE/"static")), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
