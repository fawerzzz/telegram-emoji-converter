from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


STATIC_LIMIT_BYTES = 128 * 1024
VIDEO_LIMIT_BYTES = 256 * 1024
CANVAS_SIZE = 100
TGS_LIMIT_BYTES = 3 * 1024 * 1024


class ConversionError(RuntimeError):
    pass


@dataclass(slots=True)
class ConvertedSticker:
    path: Path
    sticker_format: str


def _ensure_limit(path: Path, limit: int, label: str) -> None:
    size = path.stat().st_size
    if size > limit:
        raise ConversionError(f"{label} слишком тяжелый: {size // 1024} КБ, лимит {limit // 1024} КБ")


def _contain_on_canvas(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    image.thumbnail((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    x = (CANVAS_SIZE - image.width) // 2
    y = (CANVAS_SIZE - image.height) // 2
    canvas.alpha_composite(image, (x, y))
    return canvas


async def convert_static_to_webp(source: Path, output_dir: Path) -> ConvertedSticker:
    output = output_dir / f"{source.stem}_emoji.webp"

    with Image.open(source) as image:
        canvas = _contain_on_canvas(image)
        for quality in range(95, 35, -10):
            canvas.save(output, "WEBP", quality=quality, method=6, lossless=False)
            if output.stat().st_size <= STATIC_LIMIT_BYTES:
                return ConvertedSticker(path=output, sticker_format="static")

        canvas.save(output, "WEBP", quality=30, method=6, lossless=False)

    _ensure_limit(output, STATIC_LIMIT_BYTES, "Статический эмодзи")
    return ConvertedSticker(path=output, sticker_format="static")


async def _run_process(args: list[str], error_label: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"Утилита {args[0]} не найдена в системе. Проверьте установку.") from exc

    _, stderr = await process.communicate()
    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace").strip()
        raise ConversionError(f"{error_label}: {details[-1200:]}")


async def convert_video_to_webm(source: Path, output_dir: Path) -> ConvertedSticker:
    output = output_dir / f"{source.stem}_emoji.webm"
    vf = (
        "scale=100:100:force_original_aspect_ratio=decrease,"
        "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
        "fps=30"
    )

    await _run_process(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-t", "3", "-an", "-vf", vf,
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", "150k", "-crf", "40",
            "-deadline", "good", "-cpu-used", "4",
            "-auto-alt-ref", "0",
            str(output)
        ],
        error_label="FFmpeg видео-конвертация провалилась"
    )

    if output.stat().st_size > VIDEO_LIMIT_BYTES:
        await _run_process(
            [
                "ffmpeg", "-y", "-i", str(source),
                "-t", "3", "-an", "-vf", vf,
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", "90k", "-crf", "50",
                "-deadline", "good", "-cpu-used", "5",
                "-auto-alt-ref", "0",
                str(output)
            ],
            error_label="FFmpeg повторное сжатие провалилась"
        )

    _ensure_limit(output, VIDEO_LIMIT_BYTES, "Видео-эмодзи")
    return ConvertedSticker(path=output, sticker_format="video")


def _normalize_tgs_json(source: Path, output_dir: Path) -> Path:
    normalized = output_dir / f"{source.stem}_100.tgs"
    with gzip.open(source, "rt", encoding="utf-8") as file:
        data = json.load(file)

    original_w = int(data.get("w") or CANVAS_SIZE)
    original_h = int(data.get("h") or CANVAS_SIZE)

    layers = data.get("layers")
    if not isinstance(layers, list):
        layers = []
        data["layers"] = layers

    scale_x = (CANVAS_SIZE / float(original_w)) * 100.0 if original_w else 100.0
    scale_y = (CANVAS_SIZE / float(original_h)) * 100.0 if original_h else scale_x

    root_layers = [
        layer
        for layer in layers
        if isinstance(layer, dict) and "parent" not in layer
    ]
    has_unindexed_root_layers = any("ind" not in layer for layer in root_layers)

    if has_unindexed_root_layers:
        for layer in root_layers:
            _scale_layer_transform(layer, scale_x, scale_y)

        data["w"] = CANVAS_SIZE
        data["h"] = CANVAS_SIZE

        with gzip.open(normalized, "wt", encoding="utf-8") as file:
            json.dump(data, file, separators=(",", ":"))

        return normalized

    scaler_ind = 9999
    existing_inds = set()
    for layer in layers:
        if isinstance(layer, dict) and "ind" in layer:
            try:
                existing_inds.add(int(layer["ind"]))
            except (TypeError, ValueError):
                continue

    while scaler_ind in existing_inds:
        scaler_ind += 1

    null_layer = {
        "ddd": 0,
        "ind": scaler_ind,
        "ty": 3,
        "nm": "Global Null Scaler",
        "sr": 1,
        "ks": {
            "o": {"a": 0, "k": 100},
            "r": {"a": 0, "k": 0},
            "p": {"a": 0, "k": [0, 0, 0]},
            "a": {"a": 0, "k": [0, 0, 0]},
            "s": {"a": 0, "k": [scale_x, scale_y, 100]},
        },
        "ao": 0,
        "ip": 0,
        "op": int(data.get("op") or 0),
        "st": 0,
        "bm": 0,
    }

    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if "parent" not in layer and layer.get("ind") != scaler_ind:
            layer["parent"] = scaler_ind

    layers.append(null_layer)

    data["w"] = CANVAS_SIZE
    data["h"] = CANVAS_SIZE

    with gzip.open(normalized, "wt", encoding="utf-8") as file:
        json.dump(data, file, separators=(",", ":"))

    return normalized


def _scale_layer_transform(layer: dict, scale_x: float, scale_y: float) -> None:
    ks = layer.setdefault("ks", {})
    scale = ks.get("s")
    if not isinstance(scale, dict):
        ks["s"] = {"a": 0, "k": [scale_x, scale_y]}
        return

    value = scale.get("k")
    if scale.get("a") == 1 and isinstance(value, list):
        for keyframe in value:
            if not isinstance(keyframe, dict):
                continue
            if "s" in keyframe:
                keyframe["s"] = _multiply_scale_vector(keyframe["s"], scale_x, scale_y)
            if "e" in keyframe:
                keyframe["e"] = _multiply_scale_vector(keyframe["e"], scale_x, scale_y)
        return

    scale["k"] = _multiply_scale_vector(value, scale_x, scale_y)


def _multiply_scale_vector(value: object, scale_x: float, scale_y: float) -> object:
    if not isinstance(value, list) or len(value) < 2:
        return [scale_x, scale_y]

    result = list(value)
    if isinstance(result[0], (int, float)):
        result[0] = result[0] * scale_x / 100.0
    if isinstance(result[1], (int, float)):
        result[1] = result[1] * scale_y / 100.0
    return result


def _load_tgs_json(source: Path) -> dict:
    with gzip.open(source, "rt", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ConversionError("TGS содержит некорректный JSON")
    return data


def _tgs_has_raster_assets(source: Path) -> bool:
    data = _load_tgs_json(source)
    assets = data.get("assets") or []
    layers = data.get("layers") or []

    if any(isinstance(layer, dict) and layer.get("ty") == 2 for layer in layers):
        return True

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        path = str(asset.get("p") or "")
        if asset.get("e") == 1 or path.startswith("data:image/"):
            return True
        if path.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
            return True

    return False


def _find_tool(name: str) -> str | None:
    tool = shutil.which(name)
    if tool:
        return tool

    local_tool = Path(sys.executable).parent / name
    if local_tool.exists():
        return str(local_tool)

    return None


def _prepare_cairo_env() -> None:
    homebrew_lib = Path("/opt/homebrew/lib")
    if not homebrew_lib.exists():
        return

    current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    paths = [path for path in current.split(":") if path]
    if str(homebrew_lib) not in paths:
        paths.insert(0, str(homebrew_lib))
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(paths)


def _tgs_frame_plan(source: Path) -> tuple[int, list[int]]:
    data = _load_tgs_json(source)
    source_fps = float(data.get("fr") or 30)
    output_fps = min(30, max(1, int(round(source_fps))))
    ip = int(data.get("ip") or 0)
    op = int(data.get("op") or ip + (output_fps * 3))
    duration = min(3.0, max(1.0 / output_fps, (op - ip) / source_fps))
    frame_count = max(1, min(90, int(math.ceil(duration * output_fps))))

    frames = []
    for frame_index in range(frame_count):
        source_frame = ip + int(round(frame_index * source_fps / output_fps))
        frames.append(min(source_frame, op - 1))

    return output_fps, frames


def _is_frame_by_frame_tgs(source: Path) -> bool:
    data = _load_tgs_json(source)
    layers = data.get("layers") or []
    if not layers:
        return False

    timed_layers = [
        layer
        for layer in layers
        if isinstance(layer, dict)
        and isinstance(layer.get("ip"), (int, float))
        and isinstance(layer.get("op"), (int, float))
    ]
    return len(timed_layers) >= 2 and not any(
        isinstance(layer, dict) and "ind" in layer
        for layer in layers
    )


def _write_tgs_frame_snapshot(source: Path, output: Path, frame: int) -> None:
    data = _load_tgs_json(source)
    layers = data.get("layers") or []
    active_layers = []

    for layer in layers:
        if not isinstance(layer, dict):
            continue

        ip = float(layer.get("ip", data.get("ip", 0)))
        op = float(layer.get("op", data.get("op", ip + 1)))
        if ip <= frame < op:
            copied_layer = json.loads(json.dumps(layer))
            copied_layer["ip"] = 0
            copied_layer["op"] = 1
            copied_layer["st"] = 0
            active_layers.append(copied_layer)

    data["layers"] = active_layers
    data["ip"] = 0
    data["op"] = 1
    data["fr"] = 1

    with gzip.open(output, "wt", encoding="utf-8") as file:
        json.dump(data, file, separators=(",", ":"))


async def _render_tgs_to_png_sequence(source: Path, output_dir: Path) -> tuple[int, Path]:
    renderer = _find_tool("lottie_convert.py") or _find_tool("lottie_convert")
    if renderer is None:
        raise ConversionError(
            "Этот TGS похож на пиксельный/растровый animated sticker. Telegram отклоняет его "
            "как animated custom emoji с ошибкой wrong file type. Для fallback-рендера нужен "
            "lottie_convert.py: установите зависимости командой pip install -r requirements.txt"
        )

    _prepare_cairo_env()
    try:
        import cairosvg
    except (ImportError, OSError) as exc:
        raise ConversionError(
            "Для рендера пиксельного TGS нужен CairoSVG и системная библиотека cairo. "
            "Выполните: brew install cairo && pip install -r requirements.txt"
        ) from exc

    fps, frames = _tgs_frame_plan(source)
    frames_dir = output_dir / f"{source.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_by_frame = _is_frame_by_frame_tgs(source)

    for index, source_frame in enumerate(frames):
        svg_path = frames_dir / f"frame_{index:04d}.svg"
        png_path = frames_dir / f"frame_{index:04d}.png"
        render_source = source
        render_args = [
            renderer,
            "--output-format", "svg",
            "--width", str(CANVAS_SIZE),
            "--height", str(CANVAS_SIZE),
        ]

        if frame_by_frame:
            render_source = frames_dir / f"source_{index:04d}.tgs"
            _write_tgs_frame_snapshot(source, render_source, source_frame)
        else:
            render_args.extend(["--frame", str(source_frame)])

        render_args.extend([str(render_source), str(svg_path)])
        await _run_process(
            render_args,
            error_label="Lottie-рендеринг SVG-кадра провалился",
        )
        await asyncio.to_thread(
            cairosvg.svg2png,
            url=str(svg_path),
            write_to=str(png_path),
            output_width=CANVAS_SIZE,
            output_height=CANVAS_SIZE,
        )

    return fps, frames_dir


async def _convert_png_sequence_to_webm(frames_dir: Path, output: Path, fps: int) -> None:
    await _run_process(
        [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%04d.png"),
            "-t", "3",
            "-an",
            "-vf", "fps=30,scale=100:100",
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", "150k",
            "-crf", "40",
            "-deadline", "good",
            "-cpu-used", "4",
            "-auto-alt-ref", "0",
            str(output),
        ],
        error_label="FFmpeg сборка WEBM из кадров провалилась",
    )


async def convert_tgs_to_webm(source: Path, output_dir: Path) -> ConvertedSticker:
    output = output_dir / f"{source.stem}_emoji.webm"
    fps, frames_dir = await _render_tgs_to_png_sequence(source, output_dir)
    await _convert_png_sequence_to_webm(frames_dir, output, fps)

    if output.stat().st_size > VIDEO_LIMIT_BYTES:
        await _run_process(
            [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", str(frames_dir / "frame_%04d.png"),
                "-t", "3",
                "-an",
                "-vf", "fps=24,scale=100:100",
                "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p",
                "-b:v", "90k",
                "-crf", "50",
                "-deadline", "good",
                "-cpu-used", "5",
                "-auto-alt-ref", "0",
                str(output),
            ],
            error_label="FFmpeg повторное сжатие TGS WEBM провалилось",
        )

    _ensure_limit(output, VIDEO_LIMIT_BYTES, "Видео-эмодзи")
    return ConvertedSticker(path=output, sticker_format="video")


async def convert_tgs_to_animated(source: Path, output_dir: Path) -> ConvertedSticker:
    _ensure_limit(source, TGS_LIMIT_BYTES, "TGS-файл")
    return ConvertedSticker(path=source, sticker_format="animated")


async def convert_sticker_file(source: Path, output_dir: Path, *, is_video: bool, is_animated: bool) -> ConvertedSticker:
    if is_video:
        return await convert_video_to_webm(source, output_dir)
    if is_animated:
        return await convert_tgs_to_animated(source, output_dir)
    return await convert_static_to_webp(source, output_dir)
