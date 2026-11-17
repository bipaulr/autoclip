"""Reframe and render (plan prompt 7), with the caption/zoom/SFX layer from prompt 8.

Output: 1080x1920 H.264 (preset veryfast), AAC 48 kHz, loudness normalised to -14 LUFS.
Layouts:
  talking_head      crop follows the main face, EMA-smoothed, reset at every shot cut so the crop
                    never pans across a cut
  gameplay_facecam  facecam (found once) stacked on top 35%, gameplay bottom 65%
  gameplay          full frame centred over a blurred fill
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from autoclip.integrations.transcriber import Word
from autoclip.pipeline import sfx
from autoclip.pipeline.style import PRESETS, Preset, build_ass, flash_filter, merge_amounts, zoom_expression

W, H = 1080, 1920
TOP_H = 672  # 35% of 1920
LAYOUTS = ("talking_head", "gameplay_facecam", "gameplay")


# Encoding Instagram's Reels processor accepts reliably: strict profile/level, capped bitrate,
# fixed 2 s keyframes, 44.1 kHz AAC, no metadata. (Some uncapped CRF clips were refused.)
SAFE_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-level", "4.1",
                   "-maxrate", "8M", "-bufsize", "16M", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                   "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                   "-map_metadata", "-1", "-movflags", "+faststart"]


def safe_reencode(src: Path, dst: Path, run: Callable | None = None) -> Path:
    """Re-encodes a finished clip with SAFE_VIDEO_ARGS (used when a platform refuses the original)."""
    (run or run_ffmpeg)(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                         *SAFE_VIDEO_ARGS, str(dst)])
    return dst


def trim_clip(src: Path, dst: Path, seconds: float, run: Callable | None = None) -> Path:
    """Keeps the first `seconds` of a finished clip (hook first), re-encoded with SAFE_VIDEO_ARGS."""
    (run or run_ffmpeg)(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                         "-t", f"{seconds:.3f}", "-af", f"afade=t=out:st={max(0.0, seconds - 0.6):.3f}:d=0.6",
                         *SAFE_VIDEO_ARGS, str(dst)])
    return dst


class RenderError(Exception):
    pass


@dataclass
class Face:
    cx: float  # centre, 0-1 of frame width
    cy: float
    w: float  # size, 0-1 of frame width/height
    h: float


@dataclass
class Sample:
    t: float  # seconds from the clip start
    faces: list[Face] = field(default_factory=list)


class FaceDetector(Protocol):
    def detect(self, frame) -> list[Face]: ...


class NoFaceDetector:
    """Used when no detector can load: every clip gets the safe full-frame layout."""

    def detect(self, frame) -> list[Face]:
        return []


YUNET_MODEL = os.environ.get("FACE_MODEL", "/app/models/face_detection_yunet_2023mar.onnx")


class YuNetFaceDetector:
    """OpenCV's built-in DNN face detector (YuNet). The ~230 KB model is downloaded at image build."""

    def __init__(self, model_path: str = YUNET_MODEL, score: float = 0.7):
        import cv2
        if not os.path.exists(model_path):
            raise FileNotFoundError(model_path)
        self.det = cv2.FaceDetectorYN.create(model_path, "", (320, 320), score, 0.3, 50)

    def detect(self, frame) -> list[Face]:
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(frame)
        if faces is None:
            return []
        return [Face((f[0] + f[2] / 2) / w, (f[1] + f[3] / 2) / h, f[2] / w, f[3] / h) for f in faces]


class HaarFaceDetector:
    """Classic OpenCV detector, if the cascade file ships with this OpenCV build."""

    def __init__(self):
        import cv2
        self.cv2 = cv2
        path = getattr(getattr(cv2, "data", None), "haarcascades", "") + "haarcascade_frontalface_default.xml"
        self.cascade = cv2.CascadeClassifier(path)
        if self.cascade.empty():  # OpenCV 5 no longer bundles the cascades
            raise FileNotFoundError(path)

    def detect(self, frame) -> list[Face]:
        h, w = frame.shape[:2]
        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        found = self.cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(w // 40, w // 40))
        return [Face((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h) for x, y, fw, fh in found]


class MediaPipeFaceDetector:
    def __init__(self):
        import mediapipe as mp
        self.det = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)

    def detect(self, frame) -> list[Face]:
        import cv2
        res = self.det.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        out = []
        for d in res.detections or []:
            b = d.location_data.relative_bounding_box
            out.append(Face(b.xmin + b.width / 2, b.ymin + b.height / 2, b.width, b.height))
        return out


_detector: FaceDetector | None = None


def default_detector() -> FaceDetector:
    """First detector that loads: YuNet, MediaPipe (legacy API), Haar, else none."""
    global _detector
    if _detector is None:
        for make in (YuNetFaceDetector, MediaPipeFaceDetector, HaarFaceDetector):
            try:
                _detector = make()
                break
            except Exception:  # noqa: BLE001 - missing model or removed API: try the next one
                continue
        else:
            _detector = NoFaceDetector()
    return _detector


def run_ffmpeg(args: list[str], cwd: Path | None = None, timeout: int = 1800) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"{args[0]} failed to run ({type(e).__name__})") from None
    if p.returncode != 0:
        raise RenderError(f"{args[0]} exited {p.returncode}: {p.stderr.strip()[-400:]}")
    return p.stdout


def probe(path: Path, run: Callable = run_ffmpeg) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
               "-of", "json", str(path)])
    d = json.loads(out)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"width": int(v.get("width", 0)), "height": int(v.get("height", 0)),
            "duration": float(d.get("format", {}).get("duration", 0)),
            "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def sample_frames(video: Path, start: float, end: float, detector: FaceDetector, fps: float = 2.0) -> list[Sample]:
    import cv2
    cap = cv2.VideoCapture(str(video))
    out, t = [], start
    try:
        while t < end:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok:
                out.append(Sample(round(t - start, 2), detector.detect(frame)))
            t += 1 / fps
    finally:
        cap.release()
    return out


def steady_faces(samples: list[Sample], min_share: float = 0.6, grid: float = 0.1) -> list[Face]:
    """Faces that stay in the same spot for most of the clip: a streamer's facecam overlay.
    Faces in the content being watched move around and come and go, so they do not qualify."""
    if not samples:
        return []
    cells: dict[tuple[int, int], list[Face]] = {}
    for s in samples:
        seen = set()
        for f in s.faces:
            key = (int(f.cx / grid), int(f.cy / grid))
            if key not in seen and f.w < 0.25:
                cells.setdefault(key, []).append(f)
                seen.add(key)
    best = max(cells.values(), key=len, default=[])
    return best if len(best) >= min_share * len(samples) else []


def choose_layout(samples: list[Sample], prefer_crop: bool = False) -> str:
    """A face parked in one spot (a facecam) wins; else big faces -> talking head; else full frame.
    prefer_crop (story style): any face on screen often enough -> a full-screen crop that follows it."""
    if not samples:
        return "gameplay"
    cam = steady_faces(samples)
    if cam and sorted(f.w for f in cam)[len(cam) // 2] < 0.18:
        return "gameplay_facecam"
    with_face = [s for s in samples if s.faces]
    share = len(with_face) / len(samples)
    if prefer_crop and share >= 0.3:
        return "talking_head"
    if share < 0.4:
        return "gameplay"
    biggest = [max(s.faces, key=lambda f: f.w) for s in with_face]
    size = sorted(f.w for f in biggest)[len(biggest) // 2]
    if size >= 0.12:
        return "talking_head"
    xs = sorted(f.cx for f in biggest)
    spread = xs[int(len(xs) * 0.9) - 1 if len(xs) > 1 else 0] - xs[int(len(xs) * 0.1)]
    return "gameplay_facecam" if spread < 0.1 else "gameplay"


CROP_FPS = 30  # crop positions per second: one per output frame, so moves glide instead of stepping
DEAD_ZONE = 0.06  # of the source width: a face moving less than this does not move the frame
FOLLOW = 0.07  # share of the remaining distance covered per frame (~0.5 s to settle)


def _face_track(samples: list[Sample], cuts: list[float]) -> list[tuple[float, float, int]]:
    """(t, face centre x 0..1, shot) per sample. Stays with the same person: the face nearest the one
    followed so far, not whichever face is biggest in this frame (two people would flip the crop)."""
    out, last, shot = [], None, -1
    for s in samples:
        cur = sum(1 for c in cuts if c <= s.t)
        if cur != shot:
            last, shot = None, cur
        if s.faces:
            pick = (max(s.faces, key=lambda f: f.w) if last is None else
                    min(s.faces, key=lambda f: abs(f.cx - last) - 0.5 * f.w))
            last = pick.cx
        out.append((s.t, last if last is not None else 0.5, cur))
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2]


def track_crop(samples: list[Sample], cuts: list[float], src_w: int, src_h: int
               ) -> list[tuple[float, int]]:
    """Crop x positions (clip time, pixels) for every output frame. Noise in face detection is removed
    with a rolling median and a dead zone; the frame then glides toward the target, and jumps only at
    a shot cut (a pan across a cut looks wrong)."""
    cw = min(src_w, int(src_h * 9 / 16))
    track = _face_track(samples, sorted(cuts))
    if not track:
        return [(0.0, (src_w - cw) // 2 // 2 * 2)]
    # 1. rolling median within each shot (5 samples = 2.5 s at 2 fps)
    steady = []
    for i, (t, cx, shot) in enumerate(track):
        near = [c for tt, c, sh in track[max(0, i - 2):i + 3] if sh == shot]
        steady.append((t, _median(near), shot))
    # 2. dead zone: the target only moves when the face really moved
    targets, hold, shot = [], None, -1
    for t, cx, sh in steady:
        if sh != shot or hold is None or abs(cx - hold) > DEAD_ZONE:
            hold = cx if sh != shot or hold is None else cx - DEAD_ZONE / 2 * (1 if cx > hold else -1)
        shot = sh
        targets.append((t, hold, sh))
    # 3. glide frame by frame toward the target; jump at cuts
    end = targets[-1][0] + 0.5
    pts, x, k, cur_shot = [], None, 0, None
    for f in range(int(end * CROP_FPS) + 1):
        t = f / CROP_FPS
        while k + 1 < len(targets) and targets[k + 1][0] <= t:
            k += 1
        _, target, sh = targets[k]
        goal = min(max(target * src_w - cw / 2, 0), src_w - cw)
        x = goal if x is None or sh != cur_shot else x + (goal - x) * FOLLOW
        cur_shot = sh
        xi = int(round(x))
        pts.append((round(t, 3), xi - xi % 2))
    return pts


def crop_commands(points: list[tuple[float, int]], min_move: int = 2) -> str:
    """ffmpeg sendcmd script that moves the crop over time. A nested if() expression hits ffmpeg's
    nesting limit on clips longer than about 50 seconds, so the path is sent as timed commands."""
    lines, last = [], None
    for t, x in points:
        if last is None or abs(x - last) >= min_move:
            lines.append(f"{t:.3f} crop@cam x {x};")
            last = x
    return "\n".join(lines) + "\n"


def facecam_box(samples: list[Sample], src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """A 16:10 box around where the facecam face usually sits."""
    faces = steady_faces(samples) or [max(s.faces, key=lambda f: f.w) for s in samples if s.faces]
    if not faces:
        return 0, 0, src_w // 4, src_h // 4
    cx = sorted(f.cx for f in faces)[len(faces) // 2] * src_w
    cy = sorted(f.cy for f in faces)[len(faces) // 2] * src_h
    fw = sorted(f.w for f in faces)[len(faces) // 2] * src_w
    bw = int(min(src_w, max(fw * 3.2, src_w * 0.18)))
    bh = int(min(src_h, bw * TOP_H / W))
    x = int(min(max(cx - bw / 2, 0), src_w - bw))
    y = int(min(max(cy - bh / 2, 0), src_h - bh))
    return x - x % 2, y - y % 2, bw - bw % 2, bh - bh % 2


COLD_OPEN_S = 1.5
PAUSE_MIN = 0.45  # silences longer than this are cut out (top clips have no dead air)
PAUSE_KEEP = 0.12  # left on each side of a cut, so words do not sound clipped
PROTECT = (1.0, 2.0)  # seconds before/after a loud moment that are never cut (screams, laughs)


def keep_spans(words: list[tuple[float, float]], duration: float, protect: list[float] = ()
               ) -> list[tuple[float, float]]:
    """The parts of the clip to keep: everything except long silences between words. Silences near a
    loud moment stay (a reaction often has no words). Returns [] when there is nothing worth cutting."""
    if not words:
        return []
    words = sorted(words)
    gaps = [(0.0, words[0][0])] + [(a[1], b[0]) for a, b in zip(words, words[1:])]
    gaps.append((max(e for _, e in words), duration))
    cuts = []
    for g0, g1 in gaps:
        if g1 - g0 <= PAUSE_MIN:
            continue
        if any(g0 < p + PROTECT[1] and g1 > p - PROTECT[0] for p in protect):
            continue
        a = 0.0 if g0 == 0.0 else g0 + PAUSE_KEEP
        b = duration if g1 >= duration else g1 - PAUSE_KEEP
        if b - a > 0.05:
            cuts.append((a, b))
    if sum(b - a for a, b in cuts) < 0.5:
        return []
    spans, t = [], 0.0
    for a, b in cuts:
        if a > t:
            spans.append((round(t, 3), round(a, 3)))
        t = b
    if t < duration:
        spans.append((round(t, 3), round(duration, 3)))
    return spans


def map_time(t: float, spans: list[tuple[float, float]]) -> float:
    """Where a moment of the uncut clip lands after the cuts."""
    out = 0.0
    for a, b in spans:
        if t < a:
            return out
        if t <= b:
            return out + (t - a)
        out += b - a
    return out


def jump_cut(out_dir: Path, spans: list[tuple[float, float]], run: Callable) -> None:
    """Keeps only the given spans of clip.mp4 (captions and sound effects are already in it, so they
    stay in sync).

    Each span is trimmed and the pieces are joined with concat. A select/aselect filter with rewritten
    timestamps did the same on ffmpeg 7, but on ffmpeg 8 the audio kept its full length, out of sync."""
    parts = [f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{k}];"
             f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{k}];" for k, (a, b) in enumerate(spans)]
    inputs = "".join(f"[v{k}][a{k}]" for k in range(len(spans)))
    graph = "".join(parts) + f"{inputs}concat=n={len(spans)}:v=1:a=1[v][a]"
    run(["ffmpeg", "-y", "-v", "error", "-i", "clip.mp4", "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         *SAFE_VIDEO_ARGS, "clip_cut.mp4"], cwd=out_dir)
    if (out_dir / "clip_cut.mp4").exists():
        (out_dir / "clip_cut.mp4").replace(out_dir / "clip.mp4")


def big_moment(peaks: list[float], duration: float) -> float | None:
    """The clip's payoff: the last signal peak with room for the cold-open teaser around it."""
    fits = [p for p in peaks if 2.5 <= p <= duration - COLD_OPEN_S]
    return max(fits) if fits else None


def cold_open(out_dir: Path, at: float, run: Callable) -> None:
    """Puts 1.5 s of the big moment in front of the clip, so it opens on the reaction."""
    a = max(0.0, at - 0.4)
    b = a + COLD_OPEN_S
    graph = (f"[0:v]split[v1][v2];[0:a]asplit[a1][a2];"
             f"[v1]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[tv];[a1]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[ta];"
             f"[v2]setpts=PTS-STARTPTS[fv];[a2]asetpts=PTS-STARTPTS[fa];"
             f"[tv][ta][fv][fa]concat=n=2:v=1:a=1[v][a]")
    run(["ffmpeg", "-y", "-v", "error", "-i", "clip.mp4", "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         *SAFE_VIDEO_ARGS, "clip_cold.mp4"], cwd=out_dir)
    if (out_dir / "clip_cold.mp4").exists():
        (out_dir / "clip_cold.mp4").replace(out_dir / "clip.mp4")


@dataclass
class RenderJob:
    source: Path
    start: float
    end: float
    out_dir: Path
    words: list[Word] = field(default_factory=list)
    hook_headline: str = ""
    context_line: str = ""
    preset: str = "clean_white"
    layout: str | None = None  # None = auto
    peaks: list[float] = field(default_factory=list)  # source-video seconds of signal peaks
    cuts: list[float] = field(default_factory=list)  # source-video seconds of shot cuts
    caption_words: int = 3  # caption density chosen by the bandit
    emphasis: list[str] = field(default_factory=list)  # key words the story style colours
    reaction_tag: str = ""  # story style: shown like *SHOCKED* on the big moment
    sfx_level: str = "off"  # story style sound effects: off, light or full (see sfx.py)


@dataclass
class RenderResult:
    path: Path
    layout: str
    preset: str
    duration: float
    frames: list[Path]


def build_filter(layout: str, src_w: int, src_h: int, samples: list[Sample], cuts: list[float],
                 preset: Preset, peaks: list[float], has_audio: bool, duration: float,
                 sfx_cues: list[tuple[float, float]] | None = None) -> tuple[str, str]:
    """Returns the filter_complex string and the crop commands. sfx_cues: (clip time, gain) for the
    sound files given as inputs 1..n; without them a soft swish marks the first three peaks."""
    cmds = ""
    if layout == "talking_head":
        cw = min(src_w, int(src_h * 9 / 16)) // 2 * 2
        points = track_crop(samples, cuts, src_w, src_h)
        cmds = crop_commands(points)
        base = (f"[0:v]sendcmd=f=crop.cmd,crop@cam=w={cw}:h={src_h - src_h % 2}:x={points[0][1]}:y=0,"
                f"scale={W}:{H},setsar=1[base]")
    elif layout == "gameplay_facecam":
        fx, fy, fw, fh = facecam_box(samples, src_w, src_h)
        gh = src_h - src_h % 2
        gw = min(src_w, int(gh * W / (H - TOP_H))) // 2 * 2
        base = (f"[0:v]split[a][b];[a]crop={fw}:{fh}:{fx}:{fy},scale={W}:{TOP_H},setsar=1[top];"
                f"[b]crop={gw}:{gh}:(iw-{gw})/2:0,scale={W}:{H - TOP_H},setsar=1[bot];[top][bot]vstack[base]")
    else:
        base = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"boxblur=20:2[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    z = zoom_expression(peaks, preset.zoom)
    video_in = "[base]"
    if preset.story and peaks:
        base += f";[base]{flash_filter(peaks)}[flashed]"
        video_in = "[flashed]"
    video = (f"{base};{video_in}scale=w='trunc({W}*({z})/2)*2':h='trunc({H}*({z})/2)*2':eval=frame,"
             f"crop={W}:{H}:(iw-{W})/2:(ih-{H})/2,subtitles=captions.ass,format=yuv420p[v]")
    sfx = peaks[:3]
    if has_audio:
        # Twitch drops audio packets where it muted music, so a clip can contain no audio at all.
        # Fill gaps with silence and mix over a silent bed, so there is always a full-length track.
        audio = (f"[0:a]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo[src];"
                 f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.2f}[bed];"
                 f"[src][bed]amix=inputs=2:duration=longest:normalize=0[main]")
    else:
        audio = f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.2f}[main]"
    mix = ["[main]"]
    for i, (t, gain) in enumerate(sfx_cues or []):
        ms = int(max(0.0, t) * 1000)
        audio += (f";[{i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain:.2f},"
                  f"adelay={ms}|{ms}[x{i}]")
        mix.append(f"[x{i}]")
    for i, p in enumerate([] if sfx_cues is not None else sfx):
        ms = int(max(0.0, p) * 1000)
        audio += (f";anoisesrc=d=0.45:c=pink:a=0.35:r=48000,highpass=f=900,lowpass=f=6000,"
                  f"afade=t=in:d=0.12,afade=t=out:st=0.2:d=0.25,aformat=channel_layouts=stereo,"
                  f"adelay={ms}|{ms}[s{i}]")
        mix.append(f"[s{i}]")
    if len(mix) > 1:
        audio += f";{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[mixed]"
        last = "[mixed]"
    else:
        last = "[main]"
    audio += f";{last}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    return f"{video};{audio}", cmds


def render(job: RenderJob, *, detector: FaceDetector | None = None, run: Callable = run_ffmpeg,
           sampler: Callable = sample_frames) -> RenderResult:
    if job.layout and job.layout not in LAYOUTS:
        raise RenderError(f"unknown layout '{job.layout}'")
    preset = PRESETS.get(job.preset) or PRESETS["clean_white"]
    duration = round(job.end - job.start, 3)
    if duration <= 0:
        raise RenderError("clip has no duration")
    job.out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(job.source, run)
    if not info["width"]:
        raise RenderError("source has no video stream")

    try:
        samples = sampler(job.source, job.start, job.end, detector or default_detector())
    except Exception:  # noqa: BLE001 - face sampling only chooses the layout; never fail a clip over it
        samples = []
    layout = job.layout or choose_layout(samples, prefer_crop=preset.story)
    cuts = [c - job.start for c in job.cuts if job.start < c < job.end]
    peaks = [p - job.start for p in job.peaks if job.start <= p < job.end]
    big = big_moment(peaks, duration) if preset.story else None

    (job.out_dir / "captions.ass").write_text(
        build_ass(job.words, preset, clip_start=job.start, duration=duration,
                  hook_headline=job.hook_headline, context_line=job.context_line,
                  max_words=job.caption_words, emphasis=job.emphasis, reaction_tag=job.reaction_tag,
                  tag_at=big), encoding="utf-8")
    cues = []
    if preset.story and job.sfx_level in sfx.MAX_CUES:
        clip_words = [Word(w.w, w.start - job.start, w.end - job.start)
                      for w in merge_amounts([w for w in job.words if job.start <= w.start < job.end])]
        keys = {re.sub(r"[^\w]", "", x).lower() for e in job.emphasis for x in e.split()}
        cues = sfx.plan(job.sfx_level, clip_words, emphasis=keys, cuts=cuts, peaks=peaks, big=big,
                        duration=duration)
    graph, cmds = build_filter(layout, info["width"], info["height"], samples, cuts, preset, peaks,
                            info["has_audio"], duration,
                            [(c.t, c.gain) for c in cues] if preset.story and job.sfx_level != "off" else None)
    sfx_inputs = [a for c in cues for a in ("-i", str(sfx.sound_file(c.name, run)))]
    if cmds:
        (job.out_dir / "crop.cmd").write_text(cmds, encoding="utf-8")
    out = job.out_dir / "clip.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{job.start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(job.source.resolve()), *sfx_inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         *SAFE_VIDEO_ARGS, "-t", f"{duration:.3f}", "clip.mp4"], cwd=job.out_dir)
    clip_words = [w for w in job.words if job.start <= w.start < job.end]
    spans = keep_spans([(w.start - job.start, w.end - job.start) for w in clip_words], duration,
                       protect=peaks + ([big] if big is not None else []))
    if spans:
        jump_cut(job.out_dir, spans, run)
        duration = round(sum(b - a for a, b in spans), 3)
        big = map_time(big, spans) if big is not None else None
    if big is not None:
        cold_open(job.out_dir, big, run)
        duration += COLD_OPEN_S

    frames = []
    for k, frac in enumerate((0.15, 0.5, 0.85)):
        f = job.out_dir / f"frame_{k + 1}.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{duration * frac:.2f}", "-i", "clip.mp4", "-frames:v", "1",
             f.name], cwd=job.out_dir)
        frames.append(f)
    return RenderResult(out, layout, preset.name, probe(out, run)["duration"], frames)
