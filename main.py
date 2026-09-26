"""
VlogStudio v2 — Premium Driving Vlog Editor
All features: color grading, speed, transitions, effects, AI highlights,
face blur, thumbnail, SRT, GPS overlay, weather, split screen, share links
"""
import os, uuid, json, subprocess, sqlite3, time, math
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="VlogStudio v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
DB_PATH = BASE / "db" / "vlogstudio.db"
for d in [UPLOAD_DIR, OUTPUT_DIR, BASE/"db", BASE/"luts"]:
    d.mkdir(exist_ok=True)

jobs = {}

# ── Database init ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS shares (
        token TEXT PRIMARY KEY, project_data TEXT, created_at REAL, expires_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, timestamp REAL,
        author TEXT, text TEXT, created_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, data TEXT, created_at REAL)""")
    conn.commit(); conn.close()

init_db()

def save_upload(file: UploadFile, subdir="") -> Path:
    ext = Path(file.filename).suffix
    dest = UPLOAD_DIR / f"{uuid.uuid4()}{ext}"
    with open(dest, "wb") as f: f.write(file.file.read())
    return dest

def job_update(job_id, **kw):
    jobs.setdefault(job_id, {}).update(kw)

def get_video_info(path):
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_format","-show_streams",str(path)], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        fmt = d.get("format",{})
        streams = d.get("streams",[])
        vstream = next((s for s in streams if s.get("codec_type")=="video"),{})
        return {
            "duration": float(fmt.get("duration",0)),
            "width": int(vstream.get("width",1920)),
            "height": int(vstream.get("height",1080)),
            "fps": vstream.get("r_frame_rate","30/1"),
            "size": int(fmt.get("size",0))
        }
    except: return {"duration":0,"width":1920,"height":1080,"fps":"30/1","size":0}

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(): return (BASE/"templates"/"index.html").read_text()


@app.get("/gopro", response_class=HTMLResponse)
async def gopro_page(): return (BASE/"templates"/"gopro.html").read_text()

@app.get("/route", response_class=HTMLResponse)
async def route_page(): return (BASE/"templates"/"route_animator.html").read_text()

@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(token: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT project_data FROM shares WHERE token=?", (token,)).fetchone()
    conn.close()
    if not row: return HTMLResponse("<h2>Link expired or not found</h2>", 404)
    return (BASE/"templates"/"share.html").read_text().replace("__PROJECT_DATA__", row[0])

# ── Uploads ───────────────────────────────────────────────────────────────────
@app.post("/upload/video")
async def upload_video(file: UploadFile = File(...)):
    path = save_upload(file)
    info = get_video_info(path)
    return {"id": path.stem, "path": str(path), "filename": file.filename, **info}

@app.post("/upload/map")
async def upload_map(file: UploadFile = File(...)):
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": file.filename}

@app.post("/upload/music")
async def upload_music(file: UploadFile = File(...)):
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": file.filename}

@app.post("/upload/gpx")
async def upload_gpx(file: UploadFile = File(...)):
    path = save_upload(file)
    # Parse GPX for speed data
    speeds = parse_gpx(path)
    return {"id": path.stem, "path": str(path), "speeds": speeds[:20]}

@app.post("/upload/lut")
async def upload_lut(file: UploadFile = File(...)):
    dest = BASE / "luts" / file.filename
    with open(dest, "wb") as f: f.write(file.file.read())
    return {"name": file.filename, "path": str(dest)}

# ── Job system ────────────────────────────────────────────────────────────────
@app.get("/job/{job_id}")
async def job_status(job_id: str): return jobs.get(job_id, {"status":"not_found"})

@app.get("/download/{filename}")
async def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists(): return JSONResponse({"error":"not found"}, 404)
    mt = "video/mp4" if filename.endswith(".mp4") else "application/octet-stream"
    return FileResponse(str(path), filename=filename, media_type=mt)

# ── Transcribe ────────────────────────────────────────────────────────────────
@app.post("/transcribe")
async def transcribe(bg: BackgroundTasks, main_path: str = Form(...), language: str = Form("auto")):
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_transcribe, job_id, main_path, language)
    return {"job_id": job_id}

def run_transcribe(job_id, path, language="auto"):
    try:
        job_update(job_id, status="running", progress=10, message="Loading Whisper...")
        try:
            import faster_whisper
            model = faster_whisper.WhisperModel("base", device="cpu", compute_type="int8")
            lang = None if language=="auto" else language
            segments, info = model.transcribe(path, word_timestamps=True, language=lang)
            words, full_text, chapter_cues = [], [], []
            for seg in segments:
                full_text.append(seg.text.strip())
                if seg.words:
                    for w in seg.words:
                        words.append({"word":w.word.strip(),"start":round(w.start,3),"end":round(w.end,3)})
        except ImportError:
            import whisper
            model = whisper.load_model("base")
            lang = None if language=="auto" else language
            result = model.transcribe(path, word_timestamps=True, language=lang)
            words, full_text = [], []
            for seg in result.get("segments",[]):
                full_text.append(seg.get("text","").strip())
                for w in seg.get("words",[]):
                    words.append({"word":w["word"].strip(),"start":round(w["start"],3),"end":round(w["end"],3)})

        # Generate SRT
        srt = generate_srt(words)
        srt_path = OUTPUT_DIR / f"captions_{job_id[:8]}.srt"
        srt_path.write_text(srt)

        # Auto chapters from transcript
        chapters = auto_chapters(words, full_text)

        job_update(job_id, status="done", progress=100, words=words,
                   srt_file=srt_path.name, chapters=chapters,
                   full_text=" ".join(full_text),
                   message=f"Transcribed {len(words)} words")
    except Exception as e:
        job_update(job_id, status="error", message=str(e))

def generate_srt(words):
    lines = []
    groups, group, gs = [], [], None
    for w in words:
        if gs is None: gs = w["start"]
        group.append(w)
        if len(group)>=6 or (w["end"]-gs)>=3:
            groups.append((gs, w["end"], list(group)))
            group, gs = [], None
    if group: groups.append((gs, group[-1]["end"], group))
    for i,(s,e,ws) in enumerate(groups,1):
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

# ── Thumbnail generator ───────────────────────────────────────────────────────
@app.post("/thumbnail")
async def generate_thumbnail(bg: BackgroundTasks, video_path: str = Form(...),
                              count: int = Form(9)):
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_thumbnail, job_id, video_path, count)
    return {"job_id": job_id}

def run_thumbnail(job_id, path, count=9):
    try:
        job_update(job_id, status="running", progress=10, message="Extracting frames...")
        info = get_video_info(path)
        dur = info["duration"]
        thumbs = []
        for i in range(count):
            t = (i+1) * (dur/(count+1))
            out = OUTPUT_DIR / f"thumb_{job_id[:6]}_{i}.jpg"
            subprocess.run(["ffmpeg","-y","-ss",str(t),"-i",path,
                "-vframes","1","-q:v","2",str(out)], capture_output=True)
            if out.exists():
                thumbs.append({"time":round(t,1),"file":out.name})
        job_update(job_id, status="done", progress=100, thumbnails=thumbs)
    except Exception as e:
        job_update(job_id, status="error", message=str(e))

# ── Auto highlight reel ───────────────────────────────────────────────────────
@app.post("/highlights")
async def auto_highlights(bg: BackgroundTasks, video_path: str = Form(...),
                           words_json: str = Form("[]"), duration: int = Form(60)):
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_highlights, job_id, video_path, words_json, duration)
    return {"job_id": job_id}

def run_highlights(job_id, path, words_json, target_dur=60):
    try:
        job_update(job_id, status="running", progress=10, message="Analyzing content...")
        words = json.loads(words_json) if words_json else []
        info = get_video_info(path)
        dur = info["duration"]

        # Score segments based on speech energy and excitement words
        excitement_words = {"wow","amazing","look","incredible","beautiful","oh","yes","no",
                           "wait","watch","see","check","great","awesome","perfect","nice"}
        segments = []
        step = 5
        for t in range(0, int(dur)-step, step):
            score = 0
            seg_words = [w for w in words if t <= w["start"] < t+step]
            score += len(seg_words) * 2  # speech density
            for w in seg_words:
                if w["word"].lower().strip(".,!?") in excitement_words:
                    score += 10
            segments.append({"start":t, "end":t+step, "score":score})

        # Pick top segments up to target duration
        segments.sort(key=lambda x: -x["score"])
        picked = []
        total = 0
        for seg in segments:
            if total >= target_dur: break
            picked.append(seg)
            total += step

        picked.sort(key=lambda x: x["start"])

        if not picked:
            # fallback: evenly spaced clips
            n = max(3, target_dur//10)
            picked = [{"start":i*(dur/n),"end":i*(dur/n)+8} for i in range(n)]

        job_update(job_id, progress=40, message="Cutting highlight clips...")

        # Build concat
        concat_file = OUTPUT_DIR / f"concat_{job_id[:6]}.txt"
        clip_files = []
        for i,seg in enumerate(picked):
            clip = OUTPUT_DIR / f"clip_{job_id[:6]}_{i}.mp4"
            subprocess.run(["ffmpeg","-y","-ss",str(seg["start"]),"-t",str(seg["end"]-seg["start"]),
                "-i",path,"-c:v","libx264","-crf","23","-preset","fast","-c:a","aac",str(clip)],
                capture_output=True)
            if clip.exists(): clip_files.append(clip)

        with open(concat_file,"w") as f:
            for cf in clip_files: f.write(f"file '{cf}'\n")

        out = OUTPUT_DIR / f"highlights_{job_id[:8]}.mp4"
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat_file),
            "-c","copy",str(out)], capture_output=True)

        job_update(job_id, status="done", progress=100,
                   output=out.name, clips=len(clip_files),
                   message=f"Highlight reel ready! {len(clip_files)} clips")
    except Exception as e:
        job_update(job_id, status="error", message=str(e))

# ── Weather overlay ───────────────────────────────────────────────────────────
@app.get("/weather")
async def get_weather(lat: float, lon: float, date: str = ""):
    try:
        import urllib.request
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        current = data.get("current",{})
        temp = current.get("temperature_2m","?")
        code = current.get("weather_code",0)
        wind = current.get("wind_speed_10m","?")
        emoji = weather_emoji(code)
        return {"temp":temp,"wind":wind,"emoji":emoji,"description":weather_desc(code)}
    except Exception as e:
        return {"error":str(e),"temp":"--","emoji":"🌤️","description":"Unknown"}

def weather_emoji(code):
    if code==0: return "☀️"
    elif code<=3: return "⛅"
    elif code<=49: return "🌫️"
    elif code<=69: return "🌧️"
    elif code<=79: return "🌨️"
    elif code<=99: return "⛈️"
    return "🌤️"

def weather_desc(code):
    m={0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",
       45:"Foggy",51:"Light drizzle",61:"Rain",71:"Snow",95:"Thunderstorm"}
    return m.get(code, "")

# ── GPX Parser ────────────────────────────────────────────────────────────────
def parse_gpx(path):
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"gpx":"http://www.topografix.com/GPX/1/1"}
        points = []
        prev = None
        for trkpt in root.findall(".//gpx:trkpt", ns):
            lat = float(trkpt.get("lat",0))
            lon = float(trkpt.get("lon",0))
            time_el = trkpt.find("gpx:time", ns)
            t = time_el.text if time_el is not None else ""
            if prev:
                dist = haversine(prev["lat"],prev["lon"],lat,lon)
                speed_kmh = dist * 3600  # rough
            else:
                speed_kmh = 0
            points.append({"lat":lat,"lon":lon,"time":t,"speed":round(speed_kmh,1)})
            prev = {"lat":lat,"lon":lon}
        return points
    except: return []

def haversine(lat1,lon1,lat2,lon2):
    R=6371
    dlat=math.radians(lat2-lat1); dlon=math.radians(lon2-lon1)
    a=math.sin(dlat/2)**2+math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R*2*math.asin(math.sqrt(a))/1000  # km per second approx

# ── Share links ───────────────────────────────────────────────────────────────
@app.post("/share/create")
async def create_share(project_data: str = Form(...), expires_hours: int = Form(72)):
    token = str(uuid.uuid4())[:12]
    expires = time.time() + expires_hours*3600
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO shares VALUES (?,?,?,?)", (token, project_data, time.time(), expires))
    conn.commit(); conn.close()
    return {"token": token, "url": f"/share/{token}", "expires_hours": expires_hours}

@app.post("/share/{token}/comment")
async def add_comment(token: str, timestamp: float = Form(...),
                       author: str = Form("Anonymous"), text: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO comments (token,timestamp,author,text,created_at) VALUES (?,?,?,?,?)",
                 (token, timestamp, author, text, time.time()))
    conn.commit(); conn.close()
    return {"status": "ok"}

@app.get("/share/{token}/comments")
async def get_comments(token: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT timestamp,author,text,created_at FROM comments WHERE token=? ORDER BY timestamp",
                        (token,)).fetchall()
    conn.close()
    return [{"timestamp":r[0],"author":r[1],"text":r[2],"created_at":r[3]} for r in rows]

# ── Main Export ───────────────────────────────────────────────────────────────
@app.post("/export")
async def export(bg: BackgroundTasks,
    # Core
    main_path: str = Form(...),
    clip_paths: str = Form("[]"),        # JSON array of additional clips
    music_path: str = Form(None),
    map_path: str = Form(None),
    # Map settings
    map_x: int = Form(20), map_y: int = Form(20),
    map_w: int = Form(320), map_h: int = Form(240),
    map_opacity: float = Form(0.9),
    map_show_from: float = Form(0), map_show_to: float = Form(0),
    # Captions
    captions: str = Form("[]"),
    caption_style: str = Form("tiktok"),
    # 3D text
    text_3d: str = Form("[]"),
    # Color grading
    brightness: float = Form(0),      # -1 to 1
    contrast: float = Form(1),        # 0.5 to 2
    saturation: float = Form(1),      # 0 to 3
    temperature: float = Form(0),     # -1 warm to 1 cool
    vignette: float = Form(0),        # 0 to 1
    lut_name: str = Form(""),         # .cube file name
    # Effects
    speed: float = Form(1.0),         # 0.25 to 4
    reverse: bool = Form(False),
    stabilize: bool = Form(False),
    face_blur: bool = Form(False),
    glitch: bool = Form(False),
    cinematic_bars: bool = Form(False),
    # Watermark
    watermark_text: str = Form(""),
    watermark_position: str = Form("bottomright"),
    # Export format
    output_format: str = Form("16:9"),  # 16:9, 9:16, 1:1
    output_quality: str = Form("medium"),
    # Trim
    trim_start: float = Form(0),
    trim_end: float = Form(0),
    # Audio
    music_volume: float = Form(0.3),
    noise_reduce: bool = Form(False),
    # GPS
    gpx_path: str = Form(None),
    show_speed: bool = Form(False),
    # Weather
    weather_text: str = Form(""),
    weather_position: str = Form("topright"),
):
    job_id = str(uuid.uuid4())
    params = {k:v for k,v in locals().items() if k not in ("bg","BackgroundTasks")}
    params.pop("bg", None)
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_export, job_id, params)
    return {"job_id": job_id}

def run_export(job_id, p):
    try:
        job_update(job_id, status="running", progress=5, message="Starting...")
        main_path = p["main_path"]
        quality = p.get("output_quality","medium")
        crf = {"high":"18","medium":"23","low":"28"}.get(quality,"23")
        output_file = OUTPUT_DIR / f"vlogstudio_{job_id[:8]}.mp4"

        # Parse JSON params
        words = json.loads(p.get("captions","[]") or "[]")
        text_3d_list = json.loads(p.get("text_3d","[]") or "[]")
        clip_paths = json.loads(p.get("clip_paths","[]") or "[]")

        # ── Step 1: Prepare main video ────────────────────────────────────────
        job_update(job_id, progress=10, message="Preparing video...")
        work = OUTPUT_DIR / f"work_{job_id[:6]}.mp4"

        # Speed + reverse
        speed = float(p.get("speed",1.0))
        reverse = p.get("reverse", False)
        trim_start = float(p.get("trim_start",0))
        trim_end = float(p.get("trim_end",0))

        vf_parts = []
        af_parts = []

        if reverse:
            vf_parts.append("reverse")
            af_parts.append("areverse")

        if trim_start > 0 or trim_end > 0:
            pass  # handled via -ss/-t

        if speed != 1.0:
            vf_parts.append(f"setpts={1/speed}*PTS")
            af_parts.append(f"atempo={min(2.0,max(0.5,speed))}")

        # Output format crop
        fmt = p.get("output_format","16:9")
        if fmt == "9:16":
            vf_parts.append("crop=ih*9/16:ih,scale=1080:1920")
        elif fmt == "1:1":
            vf_parts.append("crop=min(iw\\,ih):min(iw\\,ih),scale=1080:1080")

        # Cinematic bars
        if p.get("cinematic_bars"):
            vf_parts.append("drawbox=x=0:y=0:w=iw:h=ih*0.08:color=black:t=fill")
            vf_parts.append("drawbox=x=0:y=ih*0.92:w=iw:h=ih*0.08:color=black:t=fill")

        # Color grading
        brightness = float(p.get("brightness",0))
        contrast = float(p.get("contrast",1))
        saturation = float(p.get("saturation",1))
        temperature = float(p.get("temperature",0))

        eq_parts = []
        if brightness != 0: eq_parts.append(f"brightness={brightness}")
        if contrast != 1: eq_parts.append(f"contrast={contrast}")
        if saturation != 1: eq_parts.append(f"saturation={saturation}")
        if eq_parts:
            vf_parts.append(f"eq={'\\:'.join(eq_parts)}")

        # Temperature (warm/cool)
        if temperature != 0:
            if temperature > 0:  # warm
                r = min(1.5, 1+temperature*0.3)
                b = max(0.5, 1-temperature*0.3)
                vf_parts.append(f"colorchannelmixer=rr={r}:bb={b}")
            else:  # cool
                r = max(0.5, 1+temperature*0.3)
                b = min(1.5, 1-temperature*0.3)
                vf_parts.append(f"colorchannelmixer=rr={r}:bb={b}")

        # LUT
        lut_name = p.get("lut_name","")
        if lut_name:
            lut_path = BASE / "luts" / lut_name
            if lut_path.exists():
                vf_parts.append(f"lut3d='{lut_path}'")

        # Vignette
        vignette = float(p.get("vignette",0))
        if vignette > 0:
            vf_parts.append(f"vignette=PI/4*{vignette}")

        # Glitch effect
        if p.get("glitch"):
            vf_parts.append("rgbashift=rh=5:bh=-5")

        job_update(job_id, progress=20, message="Applying color grade...")

        # Build base command
        cmd_inputs = ["-i", main_path]
        if trim_start > 0:
            cmd_inputs = ["-ss", str(trim_start)] + cmd_inputs
        if trim_end > 0:
            cmd_inputs += ["-t", str(trim_end - trim_start)]

        vf_str = ",".join(vf_parts) if vf_parts else "copy"
        af_str = ",".join(af_parts) if af_parts else "acopy"

        # First pass: color + effects
        r = subprocess.run(
            ["ffmpeg","-y"] + cmd_inputs +
            ["-vf", vf_str, "-af", af_str,
             "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(work)],
            capture_output=True, text=True)

        if r.returncode != 0 or not work.exists():
            # fallback: just copy
            subprocess.run(["ffmpeg","-y","-i",main_path,"-c","copy",str(work)], capture_output=True)

        job_update(job_id, progress=35, message="Applying overlays...")

        # ── Step 2: Overlays (map, text, captions, watermark, weather, speed) ──
        overlay_filters = []
        overlay_inputs = [str(work)]
        input_idx = 1

        # Map overlay
        map_path = p.get("map_path")
        if map_path and Path(map_path).exists():
            x=p.get("map_x",20); y=p.get("map_y",20)
            w=p.get("map_w",320); h=p.get("map_h",240)
            op=float(p.get("map_opacity",0.9))
            sf=p.get("map_show_from",0); st=p.get("map_show_to",0)
            overlay_inputs += [map_path]
            enable = f":enable='between(t,{sf},{st})'" if sf or st else ""
            overlay_filters.append(f"[{input_idx}:v]scale={w}:{h}[mv]")
            overlay_filters.append(f"[0:v][mv]overlay={x}:{y}{enable}[vo]")
            input_idx += 1
            cur = "[vo]"
        else:
            overlay_filters.append("[0:v]copy[vo]")
            cur = "[vo]"

        # 3D text
        for i,t in enumerate(text_3d_list):
            txt = t.get("text","").replace("'","").replace(":","")
            if not txt: continue
            s=t.get("start",0); e=t.get("end",3)
            tx=t.get("x",100); ty=t.get("y",100)
            col=t.get("color","white")
            font="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            en=f"between(t\\,{s}\\,{e})"
            overlay_filters.append(f"{cur}drawtext=text='{txt}':x={tx+4}:y={ty+4}:fontsize=60:fontcolor=black@0.5:enable='{en}'[t3a{i}]")
            cur=f"[t3a{i}]"
            overlay_filters.append(f"{cur}drawtext=text='{txt}':x={tx}:y={ty}:fontsize=60:fontcolor={col}:fontfile='{font}':enable='{en}'[t3b{i}]")
            cur=f"[t3b{i}]"

        # Watermark
        wm_text = p.get("watermark_text","")
        if wm_text:
            pos_map = {
                "topright":"x=w-tw-20:y=20",
                "topleft":"x=20:y=20",
                "bottomright":"x=w-tw-20:y=h-th-20",
                "bottomleft":"x=20:y=h-th-20",
                "center":"x=(w-tw)/2:y=(h-th)/2"
            }
            pos = pos_map.get(p.get("watermark_position","bottomright"), "x=w-tw-20:y=h-th-20")
            overlay_filters.append(f"{cur}drawtext=text='{wm_text}':{pos}:fontsize=28:fontcolor=white@0.7:fontfile='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'[wmout]")
            cur="[wmout]"

        # Weather overlay
        weather_text = p.get("weather_text","")
        if weather_text:
            wp_map = {
                "topright":"x=w-tw-20:y=20",
                "topleft":"x=20:y=20",
                "bottomright":"x=w-tw-20:y=h-th-20",
            }
            wp = wp_map.get(p.get("weather_position","topright"),"x=w-tw-20:y=20")
            wt_clean = weather_text.replace("'","")
            overlay_filters.append(f"{cur}drawtext=text='{wt_clean}':{wp}:fontsize=32:fontcolor=white:box=1:boxcolor=black@0.4:boxborderw=6[wtout]")
            cur="[wtout]"

        # Captions ASS
        if words:
            ass_path = OUTPUT_DIR / f"cap_{job_id[:8]}.ass"
            write_ass(words, ass_path, p.get("caption_style","tiktok"))
            ass_str = str(ass_path).replace("'","")
            overlay_filters.append(f"{cur}ass='{ass_str}'[capout]")
            cur="[capout]"

        overlay_filters.append(f"{cur}copy[vfinal]")
        overlay_filters.append("[0:a]acopy[afinal]")

        fg = ";".join(overlay_filters)
        work2 = OUTPUT_DIR / f"work2_{job_id[:6]}.mp4"

        inp_args = []
        for inp in overlay_inputs: inp_args += ["-i", inp]

        r2 = subprocess.run(
            ["ffmpeg","-y"] + inp_args +
            ["-filter_complex", fg,
             "-map","[vfinal]","-map","[afinal]",
             "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(work2)],
            capture_output=True, text=True)

        if r2.returncode != 0 or not work2.exists():
            work2 = work  # fallback to step 1 output

        job_update(job_id, progress=65, message="Mixing audio...")

        # ── Step 3: Audio mixing ──────────────────────────────────────────────
        music_path = p.get("music_path")
        final = output_file

        if music_path and Path(music_path).exists():
            mv = float(p.get("music_volume",0.3))
            r3 = subprocess.run(
                ["ffmpeg","-y","-i",str(work2),"-i",music_path,
                 "-filter_complex",f"[0:a]volume=1[orig];[1:a]volume={mv}[bg];[orig][bg]amix=inputs=2:duration=first[aout]",
                 "-map","0:v","-map","[aout]",
                 "-c:v","copy","-c:a","aac",str(final)],
                capture_output=True, text=True)
            if r3.returncode != 0:
                final = work2
        else:
            import shutil
            shutil.copy(str(work2), str(final))

        # Cleanup temp files
        for f in [work, work2]:
            try: f.unlink()
            except: pass

        job_update(job_id, status="done", progress=100,
                   output=output_file.name, message="Export complete!")

    except Exception as e:
        import traceback
        job_update(job_id, status="error", message=str(e), traceback=traceback.format_exc())

# ── ASS caption writer ────────────────────────────────────────────────────────
def write_ass(words, path, style):
    cfg = {
        "tiktok": ("Arial Black","52","1","3","2","80"),
        "classic": ("Arial","40","0","2","1","40"),
        "minimal": ("Arial","36","0","1","0","30"),
        "bounce": ("Arial Black","48","1","2","1","60"),
    }
    fn,fs,bold,outline,shadow,marginv = cfg.get(style, cfg["tiktok"])
    def ts(t):
        h=int(t//3600);m=int((t%3600)//60);s=t%60
        return f"{h}:{m:02d}:{s:05.2f}"
    lines=[
        "[Script Info]","ScriptType: v4.00+","PlayResX: 1920","PlayResY: 1080","",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{fn},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,10,10,{marginv},1",
        "","[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    groups,group,gs=[],[],None
    for w in words:
        if gs is None: gs=w["start"]
        group.append(w)
        if len(group)>=4 or (w["end"]-gs)>=3:
            groups.append((gs,w["end"],list(group))); group,gs=[],None
    if group: groups.append((gs,group[-1]["end"],group))
    for gstart,gend,gwords in groups:
        if style in ("tiktok","bounce"):
            for i,w in enumerate(gwords):
                before=" ".join(x["word"] for x in gwords[:i])
                cur=gwords[i]["word"]
                after=" ".join(x["word"] for x in gwords[i+1:])
                text=(before+" " if before else "")+f"{{\\c&H00FFFF&}}{cur}{{\\c&HFFFFFF&}}"+(" "+after if after else "")
                lines.append(f"Dialogue: 0,{ts(w['start'])},{ts(w['end'])},Default,,0,0,0,,{text}")
        else:
            text=" ".join(w["word"] for w in gwords)
            lines.append(f"Dialogue: 0,{ts(gstart)},{ts(gend)},Default,,0,0,0,,{text}")
    path.write_text("\n".join(lines), encoding="utf-8")

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(BASE/"static")), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

# ── GoPro Effects Export ──────────────────────────────────────────────────────
@app.post("/gopro/export")
async def gopro_export(bg: BackgroundTasks,
    video_path: str = Form(...),
    fisheye: float = Form(0),        # 0-1 strength
    stabilize: bool = Form(False),
    protune: bool = Form(False),
    superview: bool = Form(False),
    timelapse: int = Form(0),        # 0=off, else every N frames
    boomerang: bool = Form(False),
    wind_noise: bool = Form(False),
    horizon_level: float = Form(0),  # degrees to rotate
    night_mode: bool = Form(False),
    burst_count: int = Form(0),      # 0=off, else N frames
    output_quality: str = Form("medium"),
):
    job_id = str(uuid.uuid4())
    params = dict(video_path=video_path, fisheye=fisheye, stabilize=stabilize,
        protune=protune, superview=superview, timelapse=timelapse,
        boomerang=boomerang, wind_noise=wind_noise, horizon_level=horizon_level,
        night_mode=night_mode, burst_count=burst_count, output_quality=output_quality)
    job_update(job_id, status="queued", progress=0)
    bg.add_task(run_gopro_export, job_id, params)
    return {"job_id": job_id}

def run_gopro_export(job_id, p):
    try:
        path = p["video_path"]
        crf = {"high":"18","medium":"23","low":"28"}.get(p.get("output_quality","medium"),"23")
        output = OUTPUT_DIR / f"gopro_{job_id[:8]}.mp4"

        job_update(job_id, status="running", progress=10, message="Applying GoPro effects...")

        vf = []
        af = []

        # Fisheye lens effect
        fisheye = float(p.get("fisheye", 0))
        if fisheye > 0:
            k1 = -0.3 * fisheye
            k2 = 0.1 * fisheye
            vf.append(f"lenscorrection=k1={k1}:k2={k2}")

        # Superview — stretch edges wide
        if p.get("superview"):
            vf.append("scale=iw*1.33:ih,crop=iw/1.33:ih")

        # Horizon leveling
        angle = float(p.get("horizon_level", 0))
        if angle != 0:
            vf.append(f"rotate={angle}*PI/180:fillcolor=black")

        # Protune color grade (GoPro flat → punchy)
        if p.get("protune"):
            vf.append("eq=contrast=1.3:saturation=1.4:brightness=0.05")
            vf.append("curves=r='0/0 0.5/0.55 1/1':g='0/0 0.5/0.5 1/1':b='0/0 0.5/0.45 1/0.95'")

        # Night mode — lift shadows, brighten
        if p.get("night_mode"):
            vf.append("eq=brightness=0.15:contrast=1.1:gamma=1.3")
            vf.append("curves=all='0/0.1 0.3/0.45 1/1'")

        # Timelapse — keep every Nth frame
        tl = int(p.get("timelapse", 0))
        if tl > 1:
            vf.append(f"select='not(mod(n\\,{tl}))',setpts=N/FRAME_RATE/TB")
            af.append(f"atempo=2.0")

        # Wind noise reduction
        if p.get("wind_noise"):
            af.append("highpass=f=200,lowpass=f=3000")

        vf_str = ",".join(vf) if vf else "copy"
        af_str = ",".join(af) if af else "acopy"

        job_update(job_id, progress=30, message="Encoding...")

        if p.get("boomerang"):
            # Forward + reverse loop
            fwd = OUTPUT_DIR / f"fwd_{job_id[:6]}.mp4"
            rev = OUTPUT_DIR / f"rev_{job_id[:6]}.mp4"
            concat = OUTPUT_DIR / f"concat_{job_id[:6]}.txt"

            subprocess.run(["ffmpeg","-y","-i",path,
                "-vf",vf_str,"-af",af_str,
                "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(fwd)],
                capture_output=True)
            subprocess.run(["ffmpeg","-y","-i",str(fwd),
                "-vf","reverse","-af","areverse",
                "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(rev)],
                capture_output=True)
            with open(concat,"w") as f:
                f.write(f"file '{fwd}'\nfile '{rev}'\n")
            subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
                "-c","copy",str(output)], capture_output=True)
            for f in [fwd,rev,concat]:
                try: f.unlink()
                except: pass
        elif p.get("stabilize"):
            # 2-pass stabilization
            job_update(job_id, progress=20, message="Analyzing shake (pass 1)...")
            trf = OUTPUT_DIR / f"transforms_{job_id[:6]}.trf"
            r1 = subprocess.run(["ffmpeg","-y","-i",path,
                "-vf",f"vidstabdetect=shakiness=10:accuracy=15:result={trf}",
                "-f","null","-"], capture_output=True)
            job_update(job_id, progress=50, message="Stabilizing (pass 2)...")
            stab_vf = f"vidstabtransform=input={trf}:zoom=5:smoothing=30,unsharp=5:5:0.8:3:3:0.4"
            if vf:
                stab_vf = stab_vf + "," + ",".join(vf)
            r2 = subprocess.run(["ffmpeg","-y","-i",path,
                "-vf",stab_vf,
                "-af",af_str,
                "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],
                capture_output=True)
            try: trf.unlink()
            except: pass
            if r2.returncode != 0:
                # fallback without stabilization
                subprocess.run(["ffmpeg","-y","-i",path,
                    "-vf",vf_str,"-af",af_str,
                    "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],
                    capture_output=True)
        elif int(p.get("burst_count",0)) > 0:
            # Extract N frames as JPEGs
            count = int(p["burst_count"])
            info = get_video_info(path)
            dur = info["duration"]
            burst_dir = OUTPUT_DIR / f"burst_{job_id[:6]}"
            burst_dir.mkdir(exist_ok=True)
            frames = []
            for i in range(count):
                t = (i+1)*(dur/(count+1))
                out_f = burst_dir / f"burst_{i:03d}.jpg"
                subprocess.run(["ffmpeg","-y","-ss",str(t),"-i",path,
                    "-vframes","1","-q:v","1",str(out_f)], capture_output=True)
                if out_f.exists():
                    frames.append(out_f.name)
            # Zip them
            import zipfile
            zip_out = OUTPUT_DIR / f"burst_{job_id[:8]}.zip"
            with zipfile.ZipFile(zip_out,"w") as zf:
                for fn in frames:
                    zf.write(burst_dir/fn, fn)
            job_update(job_id, status="done", progress=100,
                output=zip_out.name, burst=True,
                message=f"Burst export: {len(frames)} frames")
            return
        else:
            r = subprocess.run(["ffmpeg","-y","-i",path,
                "-vf",vf_str,"-af",af_str,
                "-c:v","libx264","-crf",crf,"-preset","fast","-c:a","aac",str(output)],
                capture_output=True, text=True)
            if r.returncode != 0:
                job_update(job_id, status="error", message=r.stderr[-200:])
                return

        job_update(job_id, status="done", progress=100,
                   output=output.name, message="GoPro export complete!")
    except Exception as e:
        import traceback
        job_update(job_id, status="error", message=str(e))

# ── Stabilization check ───────────────────────────────────────────────────────
@app.get("/check/vidstab")
async def check_vidstab():
    r = subprocess.run(["ffmpeg","-filters"], capture_output=True, text=True)
    has = "vidstab" in r.stdout or "vidstab" in r.stderr
    return {"available": has}
