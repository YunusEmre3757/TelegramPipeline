"""Single-host process locks, rotating logs and inspectable collector health."""
import asyncio
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc)


@contextmanager
def process_lock(resource):
    """OS-held lock: released even after a crash. Do not delete the lock file."""
    path = Path(str(Path(resource).resolve()) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Başka bir toplayıcı bu kaynağı kullanıyor: {resource}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def setup_logging(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")],
                        force=True)
    logging.getLogger("telethon").setLevel(logging.WARNING)


class Health:
    def __init__(self, path, command):
        self.path = Path(path)
        self.data = {"pid": os.getpid(), "command": command, "status": "starting",
                     "started_at": utc_now().isoformat(), "channels": {}, "resolution_errors": {}}

    def channel(self, cid, **fields):
        record = self.data["channels"].setdefault(str(cid), {})
        record.update(fields)

    def error(self, cid, exc):
        self.channel(cid, status="error", last_error=str(exc),
                     last_error_at=utc_now().isoformat(), waiting_until=None)

    def publish(self, db=None, client=None):
        self.data["heartbeat_at"] = utc_now().isoformat()
        if client is not None:
            self.data["connected"] = client.is_connected()
        if db is not None:
            for record in self.data["channels"].values():
                record.update(messages_24h=0)
            cutoff = (utc_now() - timedelta(hours=24)).isoformat()
            for cid, count in db.execute(
                "SELECT channel_id,COUNT(*) FROM messages "
                "WHERE date_utc>=? GROUP BY channel_id", (cutoff,)
            ):
                if str(cid) in self.data["channels"]:
                    self.channel(cid, messages_24h=count)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    async def loop(self, db, client, interval=30):
        while True:
            self.publish(db, client)
            await asyncio.sleep(interval)


def read_health(path, max_age=120, scan_max_age=3600):
    """Quiet channels are not unhealthy; stale scans and explicit failures are."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        now = utc_now()
        issues = []
        age = (now - datetime.fromisoformat(data["heartbeat_at"])).total_seconds()
        if age > max_age:
            issues.append("Süreç kalp atışı eski")
        if data.get("status") not in {"starting", "running"}:
            issues.append("Toplayıcı çalışmıyor")
        if data.get("connected") is False:
            issues.append("Telegram bağlantısı kapalı")
        if data.get("resolution_errors"):
            issues.append("Çözülemeyen kanal girdileri var")
        if data.get("last_live_error"):
            issues.append("Canlı kayıt hatası: " + data["last_live_error"])
        for cid, record in data.get("channels", {}).items():
            if record.get("status") == "error":
                issues.append(f"{cid}: son tarama başarısız")
            elif record.get("status") == "waiting":
                until = datetime.fromisoformat(record["waiting_until"])
                if now > until + timedelta(seconds=max_age):
                    issues.append(f"{cid}: bekleme süresi aşıldı")
            elif data.get("scan_mode") != "on_reconnect" or record.get("status") != "ok":
                progress = max(record.get("last_progress_at") or data["started_at"],
                               record.get("last_success_at") or data["started_at"],
                               record.get("last_scan_started_at") or data["started_at"])
                if (now - datetime.fromisoformat(progress)).total_seconds() > scan_max_age:
                    issues.append(f"{cid}: tarama ilerlemesi eski")
        data["issues"] = issues
        return data, not issues
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"issues": [f"Sağlık kaydı okunamadı: {exc}"]}, False
