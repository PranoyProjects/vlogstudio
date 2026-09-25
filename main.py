"""
VlogStudio - Superior Driving Vlog Editor
"""

import os, uuid, json, subprocess, threading, time
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="VlogStudio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

jobs = {}

def save_upload(file: UploadFile) -> Path:
    ext = Path(file.filename).suffix
    dest = UPLOAD_DIR / f"{uuid.uuid4()}{ext}"
    with open(dest, "wb") as f:
        f.write(file.file.read())
    return dest

def job_update(job_id: str, **kwargs):
    jobs.setdefault(job_id, {}).update(kwargs)

@app.get("/", response_class=HTMLResponse)
async def root():
    return (BASE / "templates" / "index.html").read_text()

@app.get("/route", response_class=HTMLResponse)
async def route_animator():
    return (BASE / "templates" / "route_animator.html").read_text()

@app.post("/upload/main")
async def upload_main(file: UploadFile = File(...)):
    path = save_upload(file)
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True
    )
    duration = 0
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except:
        pass
    return {"id": path.stem, "path": str(path), "filename": file.filename, "duration": duration}

@app.post("/upload/map")
async def upload_map(file: UploadFile = File(...)):
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": file.filename}

@app.post("/upload/music")
async def upload_music(file: UploadFile = File(...)):
    path = save_upload(file)
    return {"id": path.stem, "path": str(path), "filename": file.filename}

@app.post("/transcribe")
async def transcribe(background_tasks: BackgroundTasks, main_path: str = Form(...)):
    job_id = str(uuid.uuid4())
    job_update(job_id, status="queued", type="transcribe", progress=0)
    background_tasks.add_task(run_transcribe, job_id, main_path)
    return {"job_id": job_id}

@app.post("/export")
async def export(background_tasks: BackgroundTasks,
                 main_path: str = Form(...),
                 map_path: str = Form(None),
                 music_path: str = Form(None),
                 map_x: int = Form(20), map_y: int = Form(20),
                 map_w: int = Form(320), map_h: int = Form(240),
                 map_opacity: float = Form(0.9),
                 captions: str = Form(None),
                 caption_style: str = Form("tiktok"),
                 text_3d: str = Form(None),
                 music_volume: float = Form(0.3),
                 trim_start: float = Form(0),
                 trim_end: float = Form(0),
                 output_quality: str = Form("high")):
    job_id = str(uuid.uuid4())
    params = dict(
        main_path=main_path, map_path=map_path, music_path=music_path,
        map_x=map_x, map_y=map_y, map_w=map_w, map_h=map_h, map_opacity=map_opacity,
        captions=captions, caption_style=caption_style, text_3d=text_3d,
        music_volume=music_volume, trim_start=trim_start, trim_end=trim_end,
        output_quality=output_quality
    )
    job_update(job_id, status="queued", type="export", progress=0, params=params)
    background_tasks.add_task(run_export, job_id, params)
    return {"job_id": job_id}

@app.get("/job/{job_id}")
async def job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})

@app.get("/download/{filename}")
async def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(str(path), filename=filename, media_type="video/mp4")

# ── Transcribe ────────────────────────────────────────────────────────────────
def run_transcribe(job_id: str, main_path: str):
    try:
        job_update(job_id, status="running", progress=10, message="Loading Whisper model...")
        try:
            import faster_whisper
            model = faster_whisper.WhisperModel("base", device="cpu", compute_type="int8")
            job_update(job_id, progress=30, message="Transcribing...")
            segments, _ = model.transcribe(main_path, word_timestamps=True)
            words = []
            for seg in segments:
                if seg.words:
                    for w in seg.words:
                        words.append({"word": w.word.strip(), "start": round(w.start,3), "end": round(w.end,3)})
        except ImportError:
            import whisper
            job_update(job_id, progress=20, message="Loading Whisper (openai)...")
            model = whisper.load_model("base")
            job_update(job_id, progress=40, message="Transcribing...")
            result = model.transcribe(main_path, word_timestamps=True)
            words = []
            for seg in result.get("segments", []):
                for w in seg.get("words", []):
                    words.append({"word": w["word"].strip(), "start": round(w["start"],3), "end": round(w["end"],3)})
        job_update(job_id, status="done", progress=100, words=words,
                   message=f"Transcribed {len(words)} words")
    except Exception as e:
        job_update(job_id, status="error", message=str(e))

# ── Export ────────────────────────────────────────────────────────────────────
def run_export(job_id: str, params: dict):
    try:
        job_update(job_id, status="running", progress=5, message="Starting export...")
        main_path = params["main_path"]
        output_file = OUTPUT_DIR / f"vlogstudio_{job_id[:8]}.mp4"
        quality = params.get("output_quality", "medium")
        crf = {"high": "18", "medium": "23", "low": "28"}.get(quality, "23")

        trim_start = float(params.get("trim_start") or 0)
        trim_end = float(params.get("trim_end") or 0)

        # Parse optional data
        words = []
        if params.get("captions"):
            try: words = json.loads(params["captions"])
            except: pass

        text_3d_list = []
        if params.get("text_3d"):
            try: text_3d_list = json.loads(params["text_3d"])
            except: pass

        has_map = bool(params.get("map_path") and Path(params["map_path"]).exists())
        has_music = bool(params.get("music_path") and Path(params["music_path"]).exists())
        has_captions = len(words) > 0
        has_3d = len(text_3d_list) > 0
        has_trim = trim_start > 0 or trim_end > 0

        job_update(job_id, progress=15, message="Building pipeline...")

        # ── Build filter graph step by step ──────────────────────────────────
        inputs = ["-i", main_path]
        input_idx = 1
        filters = []
        vid = "[0:v]"
        aud = "[0:a]"

        # Trim
        if has_trim:
            tf = f"[0:v]trim=start={trim_start}"
            if trim_end > 0: tf += f":end={trim_end}"
            tf += ",setpts=PTS-STARTPTS[trimv]"
            filters.append(tf)
            af = f"[0:a]atrim=start={trim_start}"
            if trim_end > 0: af += f":end={trim_end}"
            af += ",asetpts=PTS-STARTPTS[trima]"
            filters.append(af)
            vid = "[trimv]"
            aud = "[trima]"

        # Map overlay
        if has_map:
            inputs += ["-i", params["map_path"]]
            x = params.get("map_x", 20)
            y = params.get("map_y", 20)
            w = params.get("map_w", 320)
            h = params.get("map_h", 240)
            op = float(params.get("map_opacity", 0.9))
            filters.append(f"[{input_idx}:v]scale={w}:{h}[mapscaled]")
            filters.append(f"{vid}[mapscaled]overlay={x}:{y}[mapout]")
            vid = "[mapout]"
            input_idx += 1
            job_update(job_id, progress=30, message="Map overlay applied...")

        # 3D text overlays
        for i, t in enumerate(text_3d_list):
            raw = t.get("text", "")
            text = raw.replace("'", "").replace(":", " ").replace("\\", "")
            start = t.get("start", 0)
            end = t.get("end", 3)
            tx = t.get("x", 100)
            ty = t.get("y", 100)
            color = t.get("color", "white").replace("#", "0x") if t.get("color","").startswith("#") else t.get("color","white")
            font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            enable = f"between(t\\,{start}\\,{end})"
            # Shadow
            filters.append(
                f"{vid}drawtext=text='{text}':x={tx+4}:y={ty+4}:fontsize=60:"
                f"fontcolor=black@0.6:enable='{enable}'[t3ds{i}]"
            )
            vid = f"[t3ds{i}]"
            # Outline
            filters.append(
                f"{vid}drawtext=text='{text}':x={tx+2}:y={ty+2}:fontsize=60:"
                f"fontcolor=gray@0.8:enable='{enable}'[t3do{i}]"
            )
            vid = f"[t3do{i}]"
            # Main
            filters.append(
                f"{vid}drawtext=text='{text}':x={tx}:y={ty}:fontsize=60:"
                f"fontcolor={color}:fontfile='{font}':enable='{enable}'[t3dm{i}]"
            )
            vid = f"[t3dm{i}]"

        job_update(job_id, progress=50, message="Applying text overlays...")

        # Captions via ASS
        if has_captions:
            ass_path = OUTPUT_DIR / f"cap_{job_id[:8]}.ass"
            write_ass_captions(words, ass_path, params.get("caption_style","tiktok"))
            ass_str = str(ass_path).replace("'", "")
            filters.append(f"{vid}ass='{ass_str}'[capout]")
            vid = "[capout]"
            job_update(job_id, progress=60, message="Captions applied...")

        filters.append(f"{vid}copy[vout]")

        # Audio
        if has_music:
            inputs += ["-i", params["music_path"]]
            mv = float(params.get("music_volume", 0.3))
            filters.append(f"{aud}volume=1.0[orig]")
            filters.append(f"[{input_idx}:a]volume={mv}[bg]")
            filters.append(f"[orig][bg]amix=inputs=2:duration=first[aout]")
            aud_out = "[aout]"
            input_idx += 1
        else:
            filters.append(f"{aud}acopy[aout]")
            aud_out = "[aout]"

        filter_graph = ";".join(filters)
        job_update(job_id, progress=70, message="Encoding video...")

        cmd = (["ffmpeg", "-y"] + inputs +
               ["-filter_complex", filter_graph,
                "-map", "[vout]", "-map", aud_out,
                "-c:v", "libx264", "-crf", crf, "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                str(output_file)])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            # Log error and try bare minimum fallback
            err = result.stderr[-300:] if result.stderr else ""
            job_update(job_id, progress=75, message=f"Trying fallback... Error was: {err[:80]}")
            run_bare_export(job_id, params, output_file, crf)
            return

        job_update(job_id, status="done", progress=100,
                   output=output_file.name, message="Export complete!")

    except Exception as e:
        job_update(job_id, status="error", message=str(e))


def run_bare_export(job_id: str, params: dict, output_file: Path, crf: str):
    """Absolute minimal fallback — just map overlay, no filters"""
    try:
        main_path = params["main_path"]
        has_map = bool(params.get("map_path") and Path(params["map_path"]).exists())

        if has_map:
            x = params.get("map_x", 20)
            y = params.get("map_y", 20)
            w = params.get("map_w", 320)
            h = params.get("map_h", 240)
            cmd = [
                "ffmpeg", "-y",
                "-i", main_path,
                "-i", params["map_path"],
                "-filter_complex",
                f"[1:v]scale={w}:{h}[ov];[0:v][ov]overlay={x}:{y}[vout];[0:a]acopy[aout]",
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-crf", crf, "-preset", "fast",
                "-c:a", "aac", str(output_file)
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", main_path,
                "-c:v", "libx264", "-crf", crf, "-preset", "fast",
                "-c:a", "aac", str(output_file)
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            job_update(job_id, status="done", progress=100,
                       output=output_file.name, message="Export complete!")
        else:
            job_update(job_id, status="error", message=result.stderr[-300:])
    except Exception as e:
        job_update(job_id, status="error", message=str(e))


def write_ass_captions(words: list, path: Path, style: str):
    styles = {
        "tiktok": ("Arial Black", "52", "1", "3", "2", "80"),
        "classic": ("Arial", "40", "0", "2", "1", "40"),
        "minimal": ("Arial", "36", "0", "1", "0", "30"),
    }
    fn, fs, bold, outline, shadow, marginv = styles.get(style, styles["tiktok"])

    def ts(t):
        h=int(t//3600); m=int((t%3600)//60); s=t%60
        return f"{h}:{m:02d}:{s:05.2f}"

    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{fn},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,10,10,{marginv},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]

    # Group into blocks of 4 words
    groups, group, gs = [], [], None
    for w in words:
        if gs is None: gs = w["start"]
        group.append(w)
        if len(group) >= 4 or (w["end"] - gs) >= 3.0:
            groups.append((gs, w["end"], list(group)))
            group, gs = [], None
    if group: groups.append((gs, group[-1]["end"], group))

    for gstart, gend, gwords in groups:
        if style == "tiktok":
            for i, w in enumerate(gwords):
                before = " ".join(x["word"] for x in gwords[:i])
                cur = gwords[i]["word"]
                after = " ".join(x["word"] for x in gwords[i+1:])
                text = (before + " " if before else "") + \
                       f"{{\\c&H00FFFF&}}{cur}{{\\c&HFFFFFF&}}" + \
                       (" " + after if after else "")
                lines.append(f"Dialogue: 0,{ts(w['start'])},{ts(w['end'])},Default,,0,0,0,,{text}")
        else:
            text = " ".join(w["word"] for w in gwords)
            lines.append(f"Dialogue: 0,{ts(gstart)},{ts(gend)},Default,,0,0,0,,{text}")

    path.write_text("\n".join(lines), encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
