#!/usr/bin/env python3
"""
REAI ScrollSequence CLI v5
--------------------------
Vídeo -> frames (WebP/JPG) desktop + mobile + manifest.json, otimizado para velocidade.

Novidades v5:
 - UM só passe do FFmpeg: o vídeo é descodificado uma vez e dividido (split) em desktop + mobile
 - Aceleração por hardware na descodificação (-hwaccel auto) com fallback automático
 - Padrões rápidos: 720p desktop / 540p mobile, máx. 150 frames, interpolação OFF
   (a suavidade vem do lerp + crossfade do scroll-engine.js v5)
 - --format jpg: encoding muito mais rápido que WebP
 - Poster minúsculo (blur-up) embutido no manifest para ecrã instantâneo
 - Manifest v5 (variants, atlas, poster) + chaves legadas (totalFrames, assets)
 - Frames com 4 dígitos (frame_0001) e ordenação numérica correta
 - Mantém: ffprobe, progresso real, watchdog, pathlib/POSIX, --atlas, --dry-run, Ctrl+C limpo
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

ENGINE_VERSION = "REAI-Scroll-v5"
WEBP_MAX_DIM = 16383
FRAME_GLOBS = ("frame_*.webp", "frame_*.jpg", "atlas_*.webp", "atlas_*.jpg")
ANSI_COLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
}
COLOR_ENABLED = False


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #
def configure_color(mode: str) -> None:
    global COLOR_ENABLED

    if mode == "never" or (mode == "auto" and ("NO_COLOR" in os.environ or not sys.stdout.isatty())):
        COLOR_ENABLED = False
        return

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            kernel32.GetConsoleMode.restype = ctypes.c_int
            kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.SetConsoleMode.restype = ctypes.c_int
            for stream, stream_id in ((sys.stdout, -11), (sys.stderr, -12)):
                if not stream.isatty():
                    continue
                handle = kernel32.GetStdHandle(stream_id)
                console_mode = ctypes.c_uint()
                if not kernel32.GetConsoleMode(handle, ctypes.byref(console_mode)):
                    COLOR_ENABLED = False
                    return
                if not kernel32.SetConsoleMode(handle, console_mode.value | 0x0004):
                    COLOR_ENABLED = False
                    return
        except (AttributeError, OSError):
            COLOR_ENABLED = False
            return

    COLOR_ENABLED = True


def paint(text: str, color: str, *, bold: bool = False) -> str:
    if not COLOR_ENABLED:
        return text
    style = ("\033[1m" if bold else "") + ANSI_COLORS[color]
    return f"{style}{text}\033[0m"


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fail(msg: str, code: int = 1) -> None:
    print(f"{paint('[-]', 'red', bold=True)} {msg}", file=sys.stderr)
    sys.exit(code)


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def parse_rate(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def natural_key(path: Path) -> int:
    m = re.search(r"(\d+)", path.stem)
    return int(m.group(1)) if m else 0


def even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #
def probe_video(video: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,duration,pix_fmt:format=duration",
        "-of", "json", str(video),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        fail("O ffprobe demorou demasiado a analisar o vídeo.")

    if res.returncode != 0:
        fail(f"O ffprobe não conseguiu ler o vídeo:\n{res.stderr.strip()}")
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        fail("Resposta inválida do ffprobe.")

    streams = data.get("streams") or []
    if not streams:
        fail("O ficheiro não contém nenhuma stream de vídeo válida.")

    s = streams[0]
    fps = parse_rate(s.get("avg_frame_rate", "0")) or parse_rate(s.get("r_frame_rate", "0"))
    duration = 0.0
    for raw in (s.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(raw)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    info = {
        "codec": s.get("codec_name", "?"),
        "width": int(s.get("width") or 0),
        "height": int(s.get("height") or 0),
        "fps": fps or 30.0,
        "duration": duration,
        "pix_fmt": s.get("pix_fmt", "?"),
    }
    if not info["width"] or not info["height"]:
        fail("Não foi possível determinar a resolução do vídeo.")
    if info["duration"] <= 0:
        fail("Não foi possível determinar a duração do vídeo.")
    return info


# --------------------------------------------------------------------------- #
# Construção do comando (passe único + split)
# --------------------------------------------------------------------------- #
def resolve_interp(mode: str, src_fps: float, target_fps: int) -> str:
    if mode == "auto":
        return "mci" if src_fps < target_fps * 0.9 else "off"
    if mode != "off" and src_fps >= target_fps:
        return "off"
    return mode


def build_filter_complex(variants: list[dict], fps: int, interp: str,
                         atlas: tuple[int, int] | None) -> str:
    top_h = max(v["height"] for v in variants)

    if interp == "mci":
        head = (f"[0:v]scale=-2:{top_h}:flags=lanczos,"
                f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=obmc:me_mode=bidir:me=epzs:vsbmc=0")
    elif interp == "blend":
        head = f"[0:v]scale=-2:{top_h}:flags=lanczos,minterpolate=fps={fps}:mi_mode=blend"
    else:
        # fps ANTES do scale: menos frames para redimensionar
        head = f"[0:v]fps={fps},scale=-2:{top_h}:flags=lanczos"

    n = len(variants)
    if n > 1:
        head += f",split={n}" + "".join(f"[s{i}]" for i in range(n))
    else:
        head += "[s0]"

    parts = [head]
    for i, v in enumerate(variants):
        chain = []
        if v["height"] != top_h:
            chain.append(f"scale=-2:{v['height']}:flags=lanczos")
        if atlas:
            chain.append(f"tile={atlas[0]}x{atlas[1]}")
        parts.append(f"[s{i}]{','.join(chain) if chain else 'null'}[o{i}]")
    return ";".join(parts)


def codec_args(fmt: str, quality: int, effort: int) -> list[str]:
    if fmt == "jpg":
        qv = max(2, min(31, round(2 + (100 - quality) * 0.29)))
        return ["-c:v", "mjpeg", "-q:v", str(qv), "-pix_fmt", "yuvj420p"]
    return ["-c:v", "libwebp", "-quality", str(quality), "-compression_level", str(effort)]


def build_command(video: Path, variants: list[dict], out_root: Path, fps: int, interp: str,
                  atlas: tuple[int, int] | None, fmt: str, quality: int, effort: int,
                  start: float | None, end: float | None, hwaccel: bool) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats",
           "-progress", "pipe:1"]
    if hwaccel:
        cmd += ["-hwaccel", "auto"]
    if start is not None:
        cmd += ["-ss", f"{start}"]
    if end is not None:
        cmd += ["-to", f"{end}"]
    cmd += ["-i", str(video), "-filter_complex", build_filter_complex(variants, fps, interp, atlas)]

    prefix = "atlas" if atlas else "frame"
    for i, v in enumerate(variants):
        cmd += ["-map", f"[o{i}]", "-an", "-sn"]
        cmd += codec_args(fmt, quality, effort)
        cmd += ["-y", str(out_root / v["name"] / f"{prefix}_%04d.{fmt}")]
    return cmd


# --------------------------------------------------------------------------- #
# Execução com progresso real (sem deadlock)
# --------------------------------------------------------------------------- #
class FFmpegError(RuntimeError):
    def __init__(self, msg: str, stalled: bool = False):
        super().__init__(msg)
        self.stalled = stalled


def run_ffmpeg(cmd: list[str], total_out: int, label: str, stall_timeout: int) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)

    stderr_tail: deque[str] = deque(maxlen=40)

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                stderr_tail.append(line)

    t_err = threading.Thread(target=drain_stderr, daemon=True)
    t_err.start()

    state = {"last": time.monotonic(), "stalled": False, "done": False}

    def watchdog() -> None:
        while not state["done"] and proc.poll() is None:
            if stall_timeout > 0 and time.monotonic() - state["last"] > stall_timeout:
                state["stalled"] = True
                proc.kill()
                return
            time.sleep(1)

    threading.Thread(target=watchdog, daemon=True).start()

    bar = None
    if HAS_TQDM:
        bar = tqdm(total=max(total_out, 1), desc=label, unit="frame",
                   bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
    t0 = time.monotonic()
    last_frame = 0

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("frame="):
                try:
                    frame = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if frame != last_frame:
                    state["last"] = time.monotonic()
                    if bar is not None:
                        bar.total = max(bar.total, frame)
                        bar.update(frame - last_frame)
                    else:
                        elapsed = time.monotonic() - t0
                        eta = (elapsed / frame) * max(0, total_out - frame) if frame else 0
                        print(f"\r    {paint(label, 'cyan')}: {frame}/{total_out} | ETA {fmt_time(eta)}   ",
                              end="", flush=True)
                    last_frame = frame
            elif line.startswith("progress=end"):
                break
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    finally:
        state["done"] = True
        if bar is not None:
            bar.close()
        elif last_frame:
            print()
        t_err.join(timeout=2)

    if state["stalled"]:
        raise FFmpegError(
            f"O FFmpeg ficou {stall_timeout}s sem progresso e foi terminado.\n"
            "    Dica: usa --interp off, reduz --fps/--max-frames, ou aumenta --stall-timeout.",
            stalled=True,
        )
    if proc.returncode != 0:
        tail = "\n    ".join(stderr_tail) or "(sem mensagem de erro)"
        raise FFmpegError(f"O FFmpeg terminou com código {proc.returncode}:\n    {tail}")


# --------------------------------------------------------------------------- #
# Ficheiros auxiliares
# --------------------------------------------------------------------------- #
def clean_old_outputs(folder: Path) -> int:
    removed = 0
    for pattern in FRAME_GLOBS:
        for f in folder.glob(pattern):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def clear_generated_frames(out_dir: Path) -> int:
    removed = 0
    for variant in ("desktop", "mobile"):
        folder = out_dir / variant
        for pattern in FRAME_GLOBS:
            for path in folder.glob(pattern):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed += 1
    manifest = out_dir / "manifest.json"
    if manifest.is_file() or manifest.is_symlink():
        manifest.unlink()
        removed += 1
    preview = out_dir / "preview.html"
    if preview.is_file() or preview.is_symlink():
        preview.unlink()
        removed += 1
    return removed


def create_preview(out_dir: Path) -> Path:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Manifest não encontrado: {manifest_path}. Processa primeiro um vídeo.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    variants = manifest.get("variants")
    if not isinstance(variants, dict) or not variants:
        raise ValueError("O manifest não contém variantes de frames válidas.")

    preview_variants: dict[str, dict] = {}
    for name, variant in variants.items():
        if name not in ("desktop", "mobile") or not isinstance(variant, dict):
            continue
        files = variant.get("files")
        if not isinstance(files, list) or not files:
            continue

        local_files = []
        for file_path in files:
            if not isinstance(file_path, str):
                continue
            filename = file_path.replace("\\", "/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"(?:frame|atlas)_\d+\.(?:webp|jpg)", filename):
                continue
            if (out_dir / name / filename).is_file():
                local_files.append(f"{name}/{quote(filename)}")
        if not local_files:
            continue

        atlas = variant.get("atlas")
        cols = max(1, int(atlas.get("cols", 1))) if isinstance(atlas, dict) else 1
        rows = max(1, int(atlas.get("rows", 1))) if isinstance(atlas, dict) else 1
        frames_per_file = max(1, int(atlas.get("framesPerFile", 1))) if isinstance(atlas, dict) else 1
        frame_count = int(variant.get("frameCount", len(local_files) * frames_per_file))
        preview_variants[name] = {
            "files": local_files,
            "frameCount": max(1, min(frame_count, len(local_files) * frames_per_file)),
            "width": max(1, int(variant.get("width", 1))),
            "height": max(1, int(variant.get("height", 1))),
            "cols": cols,
            "rows": rows,
            "framesPerFile": frames_per_file,
        }
    if not preview_variants:
        raise ValueError("Não foram encontrados frames locais válidos. Verifica as pastas desktop e mobile.")

    safe_data = json.dumps(preview_variants, ensure_ascii=True)
    safe_data = (safe_data.replace("&", "\\u0026").replace("<", "\\u003c")
                 .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    html = """<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pré-visualização REAI Scroll</title>
  <style>
    :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; background: #101114; color: #f3f4f6; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; }
    main { width: min(100%, 960px); }
    header, .controls { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0 0 4px; font-size: 1.25rem; }
    p { margin: 0; color: #a1a1aa; }
    select, button { border: 1px solid #3f3f46; border-radius: 8px; background: #1c1d22; color: inherit; padding: 10px 12px; }
    .stage { min-height: 52vh; margin: 24px 0; display: grid; place-items: center; overflow: hidden; border: 1px solid #303138; border-radius: 14px; background: radial-gradient(#25262c, #151619); }
    #frame { display: block; max-width: 100%; max-height: 68vh; object-fit: contain; }
    #atlas { display: none; width: min(100%, 820px); background-repeat: no-repeat; background-size: 100% 100%; }
    input[type=range] { width: 100%; accent-color: #a78bfa; }
    .controls { justify-content: flex-start; flex-wrap: wrap; }
    .controls input { flex: 1 1 220px; }
    output { min-width: 90px; text-align: right; color: #c4b5fd; font-variant-numeric: tabular-nums; }
    @media (max-width: 560px) { body { padding: 16px; } header { align-items: flex-start; flex-direction: column; } .stage { min-height: 42vh; } #frame { max-height: 58vh; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div><h1>REAI Scroll — pré-visualização</h1><p>Sequência local, sem servidor ou dependências.</p></div>
      <select id="variant" aria-label="Variante da sequência"></select>
    </header>
    <section class="stage" aria-label="Frame atual">
      <img id="frame" alt="Frame atual da sequência">
      <div id="atlas" role="img" aria-label="Frame atual da sequência"></div>
    </section>
    <div class="controls">
      <button id="previous" type="button" aria-label="Frame anterior">←</button>
      <input id="scrubber" type="range" min="0" value="0" aria-label="Percorrer frames">
      <button id="next" type="button" aria-label="Frame seguinte">→</button>
      <output id="counter" for="scrubber"></output>
    </div>
  </main>
  <script>
    const variants = __VARIANTS__;
    const selector = document.querySelector("#variant");
    const scrubber = document.querySelector("#scrubber");
    const image = document.querySelector("#frame");
    const atlas = document.querySelector("#atlas");
    const counter = document.querySelector("#counter");
    let current = 0;

    for (const name of Object.keys(variants)) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name === "desktop" ? "Desktop" : "Mobile";
      selector.append(option);
    }

    function render() {
      const variant = variants[selector.value];
      const frameCount = variant.frameCount;
      current = Math.max(0, Math.min(current, frameCount - 1));
      scrubber.max = String(frameCount - 1);
      scrubber.value = String(current);
      counter.value = `${String(current + 1).padStart(4, "0")} / ${String(frameCount).padStart(4, "0")}`;

      const fileIndex = Math.floor(current / variant.framesPerFile);
      if (variant.framesPerFile > 1) {
        image.style.display = "none";
        atlas.style.display = "block";
        const cell = current % variant.framesPerFile;
        const col = cell % variant.cols;
        const row = Math.floor(cell / variant.cols);
        atlas.style.aspectRatio = `${variant.width * variant.cols} / ${variant.height * variant.rows}`;
        atlas.style.backgroundImage = `url("${variant.files[fileIndex]}")`;
        atlas.style.backgroundSize = `${variant.cols * 100}% ${variant.rows * 100}%`;
        atlas.style.backgroundPosition = `${variant.cols > 1 ? col / (variant.cols - 1) * 100 : 0}% ${variant.rows > 1 ? row / (variant.rows - 1) * 100 : 0}%`;
      } else {
        atlas.style.display = "none";
        image.style.display = "block";
        image.src = variant.files[fileIndex];
      }
    }

    selector.addEventListener("change", () => { current = 0; render(); });
    scrubber.addEventListener("input", () => { current = Number(scrubber.value); render(); });
    document.querySelector("#previous").addEventListener("click", () => { current -= 1; render(); });
    document.querySelector("#next").addEventListener("click", () => { current += 1; render(); });
    document.addEventListener("keydown", event => {
      if (event.key === "ArrowLeft") { current -= 1; render(); }
      if (event.key === "ArrowRight") { current += 1; render(); }
    });
    render();
  </script>
</body>
</html>
""".replace("__VARIANTS__", safe_data)
    preview_path = out_dir / "preview.html"
    preview_path.write_text(html, encoding="utf-8")
    return preview_path


def web_prefix(out_dir: Path, override: str | None) -> str:
    if override is not None:
        return override.strip("/").replace("\\", "/")
    try:
        rel = Path(os.path.relpath(out_dir, Path.cwd()))
        if not str(rel).startswith(".."):
            return rel.as_posix()
    except ValueError:
        pass
    return out_dir.name


def parse_atlas(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        c, r = value.lower().split("x")
        c, r = int(c), int(r)
        if c < 1 or r < 1:
            raise ValueError
        return c, r
    except ValueError:
        fail("--atlas deve ter o formato COLSxROWS, por exemplo 5x5.")
    return None


def make_poster(video: Path, out_dir: Path, start: float | None) -> str | None:
    """Frame minúsculo (32px) embutido no manifest como data URI para blur-up instantâneo."""
    poster = out_dir / "_poster.webp"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start}"]
    cmd += ["-i", str(video), "-frames:v", "1", "-vf", "scale=32:-2:flags=bicubic",
            "-c:v", "libwebp", "-quality", "35", "-y", str(poster)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        if res.returncode != 0 or not poster.exists():
            return None
        data = base64.b64encode(poster.read_bytes()).decode("ascii")
        return f"data:image/webp;base64,{data}"
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            poster.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Pipeline principal
# --------------------------------------------------------------------------- #
def process_video(args: argparse.Namespace) -> None:
    video = Path(args.input).expanduser().resolve()
    if not video.is_file():
        fail(f"Ficheiro de vídeo não encontrado: {video}")

    out_dir = Path(args.output).expanduser().resolve()
    atlas = parse_atlas(args.atlas)
    info = probe_video(video)

    eff_start = args.start or 0.0
    eff_end = min(args.end, info["duration"]) if args.end is not None else info["duration"]
    eff_duration = eff_end - eff_start
    if eff_duration <= 0:
        fail("Intervalo --start/--end inválido (duração efetiva <= 0).")

    fps = args.fps
    if args.max_frames and args.max_frames > 0:
        max_fps = max(1, int(args.max_frames / eff_duration))
        if max_fps < fps:
            print(f"{paint('[!]', 'yellow', bold=True)} --max-frames={args.max_frames}: "
                  f"fps reduzido de {fps} para {max_fps}.")
            fps = max_fps

    interp = "off" if args.no_ai else resolve_interp(args.interp, info["fps"], fps)
    expected = max(1, int(round(eff_duration * fps)))
    fmt = args.format

    requested = [("desktop", args.desktop_height), ("mobile", args.mobile_height)]
    if args.only != "both":
        requested = [r for r in requested if r[0] == args.only]

    variants: list[dict] = []
    for name, target_h in requested:
        h = min(target_h, info["height"])
        h -= h % 2
        w = even(info["width"] * h / info["height"])
        if atlas and (w * atlas[0] > WEBP_MAX_DIM or h * atlas[1] > WEBP_MAX_DIM) and fmt == "webp":
            fail(f"Atlas {atlas[0]}x{atlas[1]} excede o limite do WebP ({WEBP_MAX_DIM}px) em {name}. "
                 "Usa menos colunas/linhas.")
        variants.append({"name": name, "height": h, "width": w})

    print(paint("=" * 58, "magenta"))
    print(paint(f"🚀 {ENGINE_VERSION} - Passe único (split)", "magenta", bold=True))
    print(paint("=" * 58, "magenta"))
    print(f"{paint('[*]', 'blue', bold=True)} Vídeo: {video}")
    print(f"{paint('[*]', 'blue', bold=True)} Origem: {info['codec']} {info['width']}x{info['height']} @ {info['fps']:.2f}fps, "
          f"{info['duration']:.1f}s")
    print(f"{paint('[*]', 'blue', bold=True)} Alvo: {fps}fps | {fmt.upper()} q{args.quality} | Interpolação: {interp} "
          f"| Variantes: " + ", ".join(f"{v['name']} {v['width']}x{v['height']}" for v in variants))
    print(f"{paint('[*]', 'blue', bold=True)} Frames por variante: ~{expected}" +
          (f" (atlas {atlas[0]}x{atlas[1]})" if atlas else ""))
    if expected > 300:
        print(f"{paint('[!]', 'yellow', bold=True)} Muitos frames (>300): usa --max-frames para páginas mais leves.")

    total_out = math.ceil(expected / (atlas[0] * atlas[1])) if atlas else expected

    def make_cmd(use_hw: bool) -> list[str]:
        return build_command(video, variants, out_dir, fps, interp, atlas, fmt, args.quality,
                             args.effort, args.start, args.end, use_hw)

    use_hw = args.hwaccel == "auto"
    if args.dry_run:
        cmd = make_cmd(use_hw)
        print(f"\n{paint('[dry-run]', 'cyan', bold=True)} Comando:\n  " +
              " ".join(f'"{c}"' if (" " in c or "[" in c) else c for c in cmd))
        return

    for v in variants:
        folder = out_dir / v["name"]
        folder.mkdir(parents=True, exist_ok=True)
        removed = clean_old_outputs(folder)
        if removed:
            print(f"{paint('[*]', 'blue', bold=True)} {removed} ficheiros antigos removidos de {folder.name}/")

    t_start = time.monotonic()
    print(f"\n{paint('[*]', 'blue', bold=True)} A exportar...")
    try:
        try:
            run_ffmpeg(make_cmd(use_hw), total_out, "Exportar", args.stall_timeout)
        except FFmpegError as exc:
            if use_hw and not exc.stalled:
                print(f"\n{paint('[!]', 'yellow', bold=True)} Falhou com hwaccel, a repetir "
                      f"sem aceleração por hardware...\n    {exc}")
                for v in variants:
                    clean_old_outputs(out_dir / v["name"])
                run_ffmpeg(make_cmd(False), total_out, "Exportar (CPU)", args.stall_timeout)
            else:
                raise
    except FFmpegError as exc:
        fail(f"Falha na exportação: {exc}")

    prefix = web_prefix(out_dir, args.web_prefix)
    manifest_variants: dict[str, dict] = {}
    for v in variants:
        folder = out_dir / v["name"]
        pattern = f"atlas_*.{fmt}" if atlas else f"frame_*.{fmt}"
        files = sorted(folder.glob(pattern), key=natural_key)
        if not files:
            fail(f"O FFmpeg terminou sem gerar frames em {v['name']}. Verifica o vídeo de entrada.")
        size = sum(p.stat().st_size for p in files)
        print(f"{paint('[+]', 'green', bold=True)} {v['name']}: {len(files)} ficheiros, {human_size(size)}")

        entry: dict = {
            "width": v["width"],
            "height": v["height"],
            "ext": fmt,
            "fileCount": len(files),
            "totalBytes": size,
            "files": [f"{prefix}/{v['name']}/{p.name}" for p in files],
        }
        if atlas:
            per = atlas[0] * atlas[1]
            entry["atlas"] = {"cols": atlas[0], "rows": atlas[1], "framesPerFile": per}
            entry["frameCount"] = min(expected, len(files) * per)
        else:
            entry["frameCount"] = len(files)
        manifest_variants[v["name"]] = entry

    poster = make_poster(video, out_dir, args.start)
    primary = manifest_variants.get("desktop") or next(iter(manifest_variants.values()))

    manifest = {
        "engine": ENGINE_VERSION,
        "version": 5,
        "totalFrames": primary["frameCount"],
        "fps": fps,
        "targetFps": fps,
        "format": fmt,
        "quality": args.quality,
        "interpolation": interp,
        "interpolated": interp != "off",
        "source": {
            "file": video.name,
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 3),
            "duration": round(info["duration"], 3),
            "codec": info["codec"],
        },
        "poster": poster,
        "variants": manifest_variants,
        "assets": {k: v["files"] for k, v in manifest_variants.items()},
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + paint("=" * 58, "green"))
    print(paint(f"✨ Concluído em {fmt_time(time.monotonic() - t_start)}", "green", bold=True))
    print(f"{paint('✨ Manifest:', 'green', bold=True)} {manifest_path}")
    for name, v in manifest_variants.items():
        print(f"{paint('✨', 'green')} {name}: {v['frameCount']} frames ({human_size(v['totalBytes'])})")
    print(paint("=" * 58, "green"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{ENGINE_VERSION} - vídeo -> frames para animações por scroll",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", help="Vídeo de entrada (.mp4, .mov, .webm...)")
    p.add_argument("-o", "--output", default="assets/frames", help="Diretório de saída")
    p.add_argument("--clean-frames", action="store_true",
                   help="Apaga frames gerados, manifest.json e preview.html")
    p.add_argument("--preview", action="store_true",
                   help="Gera uma pré-visualização HTML offline a partir do manifest existente")
    p.add_argument("--fps", type=int, default=30, help="FPS alvo da sequência")
    p.add_argument("--max-frames", type=int, default=150,
                   help="Limite de frames por variante (reduz o fps automaticamente; 0 = sem limite)")
    p.add_argument("--format", choices=["webp", "jpg"], default="webp",
                   help="Formato dos frames (jpg codifica bem mais depressa)")
    p.add_argument("--quality", type=int, default=70, choices=range(1, 101), metavar="1-100")
    p.add_argument("--effort", type=int, default=4, choices=range(0, 7), metavar="0-6",
                   help="Esforço de compressão WebP (0 rápido ... 6 menor e mais lento)")
    p.add_argument("--desktop-height", type=int, default=720, help="Altura da versão desktop")
    p.add_argument("--mobile-height", type=int, default=540, help="Altura da versão mobile")
    p.add_argument("--only", choices=["both", "desktop", "mobile"], default="both")
    p.add_argument("--interp", choices=["off", "auto", "blend", "mci"], default="off",
                   help="Interpolação de movimento (lenta; o motor JS já suaviza com lerp/crossfade)")
    p.add_argument("--no-ai", action="store_true", help="Compatibilidade v3 (= --interp off)")
    p.add_argument("--hwaccel", choices=["auto", "off"], default="auto",
                   help="Descodificação por hardware (fallback automático para CPU)")
    p.add_argument("--start", type=float, default=None, help="Início do corte (segundos)")
    p.add_argument("--end", type=float, default=None, help="Fim do corte (segundos)")
    p.add_argument("--atlas", default=None, metavar="COLSxROWS",
                   help="Atlas de sprites (ex.: 4x4) em vez de um ficheiro por frame")
    p.add_argument("--web-prefix", default=None,
                   help="Prefixo dos URLs no manifest (por omissão, o caminho de saída relativo)")
    p.add_argument("--stall-timeout", type=int, default=180,
                   help="Segundos sem progresso até terminar o FFmpeg (0 = desativado)")
    p.add_argument("--dry-run", action="store_true", help="Mostra o comando sem executar")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                   help="Cores no terminal: auto deteta TTY, always força, never desativa")
    return p


def main() -> None:
    setup_console()
    parser = build_parser()
    args = parser.parse_args()
    configure_color(args.color)

    if args.clean_frames:
        if args.preview:
            parser.error("--preview não pode ser combinado com --clean-frames.")
        out_dir = Path(args.output).expanduser().resolve()
        removed = clear_generated_frames(out_dir)
        print(f"{paint('[OK]', 'green', bold=True)} Limpeza concluída: "
              f"{removed} ficheiro(s) removido(s) de {out_dir}")
        return

    if args.preview:
        out_dir = Path(args.output).expanduser().resolve()
        try:
            preview_path = create_preview(out_dir)
        except (OSError, ValueError) as exc:
            fail(f"Não foi possível criar a pré-visualização: {exc}")
        print(f"{paint('[OK]', 'green', bold=True)} Pré-visualização offline criada: {preview_path}")
        print(f"{paint('[*]', 'blue', bold=True)} Abre este ficheiro no browser; "
              "não é necessário iniciar um servidor.")
        return

    if not args.input:
        parser.error("--input é obrigatório para processar um vídeo.")

    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        fail(f"Não encontrado no PATH: {', '.join(missing)}. "
             "Instala com: winget install Gyan.FFmpeg (e reabre o terminal).")
    if args.fps < 1:
        fail("--fps tem de ser >= 1.")

    try:
        process_video(args)
    except KeyboardInterrupt:
        print(f"\n{paint('[!]', 'yellow', bold=True)} Interrompido pelo utilizador.")
        sys.exit(130)


if __name__ == "__main__":
    main()