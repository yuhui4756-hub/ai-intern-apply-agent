from __future__ import annotations

from pathlib import Path

from ..config import recordings_dir


ALLOWED_RECORDING_EXTENSIONS = {".mp3", ".m4a", ".wav", ".mp4", ".aac", ".flac", ".ogg"}
TRANSCRIPTION_MODELS = ["base", "small"]


def transcription_model_dir() -> Path:
    path = recordings_dir().parent / "whisper_models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transcribe_recording(file_path: str, model_size: str = "base") -> dict[str, str]:
    path = Path(file_path)
    if not path.exists():
        raise ValueError("没有找到本地录音文件。")
    if model_size not in TRANSCRIPTION_MODELS:
        raise ValueError("转写模型无效。")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ValueError("本地转写依赖未安装，请运行 pip install -r requirements.txt。") from exc

    try:
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=str(transcription_model_dir()),
        )
        segments, info = model.transcribe(str(path), language="zh", vad_filter=True)
        transcript = "\n".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"本地转写失败：{message[:220]}") from exc
    if not transcript:
        raise ValueError("没有从录音中识别到可用文本，请确认录音内容和音频格式。")
    return {"transcript": transcript[:60000], "language": str(getattr(info, "language", "") or "zh")}
