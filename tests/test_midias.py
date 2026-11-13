import subprocess
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from app.config import get_settings
from app.models import ReturnMedia
from app.services import media, media_backup, video_compress


class _DB:
    """Só o que as rotinas usam: a lista de mídias pendentes, a soma já copiada e commit."""

    def __init__(self, rows, used=0):
        self.rows, self.used, self.commits = rows, used, 0

    def scalars(self, stmt):
        return SimpleNamespace(all=lambda: self.rows)

    def scalar(self, stmt):
        return self.used

    def commit(self):
        self.commits += 1


def _phone_video(path, seconds=2):
    # vídeo "de celular": 1080x1920 em pé, bitrate alto
    subprocess.run([video_compress.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size=1080x1920:rate=30:duration={seconds}", "-c:v", "libx264", "-b:v", "20M",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)


def test_video_is_compressed_and_original_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_storage", lambda: media.LocalStorage(tmp_path))
    monkeypatch.setattr(video_compress, "get_storage", lambda: media.LocalStorage(tmp_path))
    folder = tmp_path / "shopee" / "PEDIDO1"
    folder.mkdir(parents=True)
    _phone_video(folder / "abcd1234_video.mov")
    before = (folder / "abcd1234_video.mov").stat().st_size
    m = ReturnMedia(id=uuid.uuid4(), kind="video", storage_path="shopee/PEDIDO1/abcd1234_video.mov",
                    content_type="video/quicktime", size_bytes=before)
    db = _DB([m])
    out = video_compress.compress_pending(db)
    assert out["comprimidos"] == 1 and out["falhas"] == 0
    assert m.storage_path == "shopee/PEDIDO1/abcd1234_video.mp4" and m.content_type == "video/mp4"
    assert m.original_size_bytes == before and m.size_bytes < before / 3 and m.compressed_at
    assert (folder / "abcd1234_video.mp4").exists() and not (folder / "abcd1234_video.mov").exists()
    assert not list(folder.glob("*.comprimindo.*"))


def test_truncated_output_keeps_original(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    _phone_video(src, seconds=4)
    original = src.read_bytes()
    real = video_compress.duration
    # o comprimido sai com 2 s a menos que o original
    monkeypatch.setattr(video_compress, "duration", lambda p: real(p) - (2 if p.name == "saida.mp4" else 0))
    m = ReturnMedia(id=uuid.uuid4(), kind="video", storage_path="v.mp4", size_bytes=len(original))
    monkeypatch.setattr(video_compress, "get_storage", lambda: media.LocalStorage(tmp_path))
    try:
        video_compress.compress_file(src, tmp_path / "saida.mp4", 720, 28)
        raise AssertionError("deveria recusar")
    except ValueError as exc:
        assert "duração não confere" in str(exc)
    assert not (tmp_path / "saida.mp4").exists() and src.read_bytes() == original


def test_broken_video_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setattr(video_compress, "get_storage", lambda: media.LocalStorage(tmp_path))
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "v.mp4").write_bytes(b"isso nao e video" * 1000)
    m = ReturnMedia(id=uuid.uuid4(), kind="video", storage_path="x/v.mp4", size_bytes=16000)
    out = video_compress.compress_pending(_DB([m]))
    assert out["falhas"] == 1 and m.compressed_at is None and (tmp_path / "x" / "v.mp4").exists()


def _settings(**kw):
    return get_settings().model_copy(update=dict(supabase_url="https://proj.supabase.co", supabase_service_key="segredo",
                                                 media_backup_bucket="midias", media_backup_max_gb=1.0, **kw))


def test_backup_uploads_to_same_path_and_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(media_backup, "get_storage", lambda: media.LocalStorage(tmp_path))
    (tmp_path / "tiktok" / "586").mkdir(parents=True)
    (tmp_path / "tiktok" / "586" / "a1_foto.jpg").write_bytes(b"jpg")
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers["authorization"], request.headers["x-upsert"], request.content))
        return httpx.Response(200, json={"Key": "ok"})

    m = ReturnMedia(id=uuid.uuid4(), kind="photo", storage_path="tiktok/586/a1_foto.jpg", content_type="image/jpeg", size_bytes=3)
    db = _DB([m])
    out = media_backup.run_backup(db, _settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out == {"copiados": 1, "falhas": 0, "cheio": False}
    assert calls == [("https://proj.supabase.co/storage/v1/object/midias/tiktok/586/a1_foto.jpg", "Bearer segredo", "true", b"jpg")]
    assert m.backup_at is not None


def test_backup_stops_when_free_plan_is_full_and_without_key():
    m = ReturnMedia(id=uuid.uuid4(), kind="photo", storage_path="a/b.jpg", size_bytes=200 * 1024**2)
    db = _DB([m], used=900 * 1024**2)
    out = media_backup.run_backup(db, _settings(), client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert out["cheio"] and out["copiados"] == 0 and m.backup_at is None
    assert "skipped" in media_backup.run_backup(db, _settings().model_copy(update={"supabase_service_key": ""}))


def test_backup_status_levels():
    st = media_backup.BackupStatus(True, False, total=10, copied=8, waiting=2, used_bytes=1, limit_bytes=100, last=datetime.now(UTC))
    assert st.level == "ok"
    assert media_backup.BackupStatus(False, False, 0, 0, 0, 0, 100, None).level == "off"
    assert media_backup.BackupStatus(True, False, 10, 5, 0, 96, 100, None).level == "bad"
