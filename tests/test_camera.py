"""Câmera da bancada: escolha dos arquivos de gravação, janela do recorte e limpeza."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.models import Return
from app.services import camera


def _make(root: Path, local_starts: list[datetime]) -> None:
    for s in local_starts:
        folder = root / s.strftime("%Y%m%d")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{s.strftime(camera.NAME_FORMAT)}.mp4").write_bytes(b"x")


def test_list_segments_covers_window(tmp_path):
    base = datetime(2026, 10, 22, 14, 30, 0)
    _make(tmp_path, [base + timedelta(minutes=m) for m in range(0, 6)])  # 14:30 ... 14:35
    start = (base + timedelta(minutes=1, seconds=20)).astimezone()
    end = (base + timedelta(minutes=3, seconds=10)).astimezone()
    segs = camera.list_segments(tmp_path, start, end, 60)
    assert [s.path.stem for s in segs] == ["20261022-143100", "20261022-143200", "20261022-143300"]


def test_list_segments_crosses_midnight(tmp_path):
    base = datetime(2026, 10, 22, 23, 59, 0)
    _make(tmp_path, [base, base + timedelta(minutes=1)])
    segs = camera.list_segments(tmp_path, base.astimezone() + timedelta(seconds=30),
                                (base + timedelta(minutes=1, seconds=30)).astimezone(), 60)
    assert len(segs) == 2


def test_clip_window_has_padding():
    ret = Return(inspection_started_at=datetime(2026, 10, 22, 17, 30, tzinfo=UTC),
                 received_at=datetime(2026, 10, 22, 17, 34, tzinfo=UTC))
    start, end = camera.clip_window(ret)
    pad = get_settings().camera_clip_pad_seconds
    assert start == ret.inspection_started_at - timedelta(seconds=pad)
    assert end == ret.received_at + timedelta(seconds=pad)


def test_disabled_without_rtsp_url():
    assert get_settings().camera_rtsp_url == "" and camera.process_pending_clips(None) == {"camera": "desligada"}


def test_cleanup_removes_old_days(tmp_path, monkeypatch):
    monkeypatch.setattr(camera, "recordings_dir", lambda: tmp_path)
    old = datetime.now() - timedelta(days=get_settings().camera_retention_days + 2)
    _make(tmp_path, [old, datetime.now()])
    assert camera.cleanup_recordings() == 1
    assert len(list(tmp_path.iterdir())) == 1
