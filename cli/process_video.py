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

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

ENGINE_VERSION = "REAI-Scroll-v5"
WEBP_MAX_DIM = 16383
FRAME_GLOBS = ("frame_*.webp", "frame_*.jpg", "atlas_*.webp", "atlas_*.jpg")


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #
def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fail(msg: str, code: int = 1) -> None:
    print(f"[-] {msg}", file=sys.stderr)
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
                        print(f"\r    {label}: {frame}/{total_out} | ETA {fmt_time(eta)}   ",
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
            print(f"[!] --max-frames={args.max_frames}: fps reduzido de {fps} para {max_fps}.")
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

    print("=" * 58)
    print(f"🚀 {ENGINE_VERSION} - Passe único (split)")
    print("=" * 58)
    print(f"[*] Vídeo: {video}")
    print(f"[*] Origem: {info['codec']} {info['width']}x{info['height']} @ {info['fps']:.2f}fps, "
          f"{info['duration']:.1f}s")
    print(f"[*] Alvo: {fps}fps | {fmt.upper()} q{args.quality} | Interpolação: {interp} "
          f"| Variantes: " + ", ".join(f"{v['name']} {v['width']}x{v['height']}" for v in variants))
    print(f"[*] Frames por variante: ~{expected}" + (f" (atlas {atlas[0]}x{atlas[1]})" if atlas else ""))
    if expected > 300:
        print("[!] Muitos frames (>300): usa --max-frames para páginas mais leves.")

    total_out = math.ceil(expected / (atlas[0] * atlas[1])) if atlas else expected

    def make_cmd(use_hw: bool) -> list[str]:
        return build_command(video, variants, out_dir, fps, interp, atlas, fmt, args.quality,
                             args.effort, args.start, args.end, use_hw)

    use_hw = args.hwaccel == "auto"
    if args.dry_run:
        cmd = make_cmd(use_hw)
        print("\n[dry-run] Comando:\n  " + " ".join(f'"{c}"' if (" " in c or "[" in c) else c for c in cmd))
        return

    for v in variants:
        folder = out_dir / v["name"]
        folder.mkdir(parents=True, exist_ok=True)
        removed = clean_old_outputs(folder)
        if removed:
            print(f"[*] {removed} ficheiros antigos removidos de {folder.name}/")

    t_start = time.monotonic()
    print("\n[*] A exportar...")
    try:
        try:
            run_ffmpeg(make_cmd(use_hw), total_out, "Exportar", args.stall_timeout)
        except FFmpegError as exc:
            if use_hw and not exc.stalled:
                print(f"\n[!] Falhou com hwaccel, a repetir sem aceleração por hardware...\n    {exc}")
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
        print(f"[+] {v['name']}: {len(files)} ficheiros, {human_size(size)}")

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

    print("\n" + "=" * 58)
    print(f"✨ Concluído em {fmt_time(time.monotonic() - t_start)}")
    print(f"✨ Manifest: {manifest_path}")
    for name, v in manifest_variants.items():
        print(f"✨ {name}: {v['frameCount']} frames ({human_size(v['totalBytes'])})")
    print("=" * 58)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{ENGINE_VERSION} - vídeo -> frames para animações por scroll",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", required=True, help="Vídeo de entrada (.mp4, .mov, .webm...)")
    p.add_argument("-o", "--output", default="assets/frames", help="Diretório de saída")
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
    return p


def main() -> None:
    setup_console()
    args = build_parser().parse_args()

    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        fail(f"Não encontrado no PATH: {', '.join(missing)}. "
             "Instala com: winget install Gyan.FFmpeg (e reabre o terminal).")
    if args.fps < 1:
        fail("--fps tem de ser >= 1.")

    try:
        process_video(args)
    except KeyboardInterrupt:
        print("\n[!] Interrompido pelo utilizador.")
        sys.exit(130)


if __name__ == "__main__":
    main()