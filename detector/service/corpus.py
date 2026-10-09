"""Opt-in local collection. Predictions never become verified training labels."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import unicodedata
import uuid

def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def normalized_hash(text):
    value = unicodedata.normalize("NFKC", text).lower()
    value = re.sub("[ʻʼ‘’\x60]", "'", value)
    return hashlib.sha256(" ".join(value.split()).encode("utf8")).hexdigest()

class DuplicateSample(ValueError):
    pass

class CorpusFull(ValueError):
    pass

class CorpusStore:
    def __init__(self, path: Path, max_samples=10000, max_chars=100_000_000):
        self.path = Path(path)
        self.max_samples, self.max_chars = max_samples, max_chars

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
          id TEXT PRIMARY KEY, text TEXT NOT NULL, content_hash TEXT UNIQUE NOT NULL,
          lang TEXT NOT NULL, genre TEXT NOT NULL, topic TEXT NOT NULL,
          claimed_label TEXT NOT NULL, claimed_model TEXT NOT NULL,
          score REAL, model_version TEXT, created_at TEXT NOT NULL,
          consent_version TEXT NOT NULL, withdrawal_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', verified_label INTEGER,
          verified_model TEXT, evidence_kind TEXT, evidence TEXT, reviewer TEXT,
          reviewed_at TEXT);
        CREATE TABLE IF NOT EXISTS events (
          sample_id TEXT, action TEXT NOT NULL, at TEXT NOT NULL);
        """)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, *, text, lang, genre, topic, claimed_label, claimed_model, analysis):
        if lang not in {"uz", "uz-cyrl", "ru", "en"}:
            raise ValueError("unsupported collection language")
        if claimed_label not in {"human", "ai", "unknown"}:
            raise ValueError("invalid claimed label")
        sid, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count, chars = db.execute("SELECT count(*), coalesce(sum(length(text)),0) FROM samples").fetchone()
            if count >= self.max_samples or chars + len(text) > self.max_chars:
                raise CorpusFull("local collection limit reached")
            try:
                db.execute("""INSERT INTO samples
                  (id,text,content_hash,lang,genre,topic,claimed_label,claimed_model,
                   score,model_version,created_at,consent_version,withdrawal_hash)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    sid, text, normalized_hash(text), lang, genre, topic, claimed_label, claimed_model,
                    analysis.get("ai_probability"), analysis.get("model_version"), utc(),
                    "local-training-v1", hashlib.sha256(token.encode()).hexdigest()))
            except sqlite3.IntegrityError as exc:
                # Never disclose another submitter's receipt or withdrawal capability.
                raise DuplicateSample("duplicate text already collected") from exc
            db.execute("INSERT INTO events VALUES(?,?,?)", (sid, "consented_submission", utc()))
        return {"sample_id": sid, "withdrawal_token": token, "status": "pending",
                "used_for_training": False}

    def withdraw(self, sid, token):
        if not self.path.exists():
            return False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT withdrawal_hash FROM samples WHERE id=?", (sid,)).fetchone()
            if row is None or not hmac.compare_digest(row[0], hashlib.sha256(token.encode()).hexdigest()):
                return False
            db.execute("UPDATE samples SET text='',topic='',claimed_model='',evidence='',reviewer='',status='withdrawn',verified_label=NULL,verified_model=NULL WHERE id=?", (sid,))
            db.execute("INSERT INTO events VALUES(?,?,?)", (sid, "withdrawn", utc()))
        return True

    def review(self, sid, *, label, evidence_kind, evidence, reviewer, rights_confirmed, model="", reject=False):
        if not reviewer.strip() or len(evidence.strip()) < 12:
            raise ValueError("reviewer and specific independent evidence required")
        if not reject:
            if not rights_confirmed:
                raise ValueError("training rights must be confirmed")
            valid = {"human": {"historical_source", "author_confirmation"}, "ai": {"generator_record"}}
            if label not in valid or evidence_kind not in valid[label] or (label == "ai" and not model.strip()):
                raise ValueError("verified authorship source or actual generator record required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM samples WHERE id=?", (sid,)).fetchone()
            if row is None or row[0] == "withdrawn":
                raise ValueError("sample unavailable or withdrawn")
            db.execute("""UPDATE samples SET status=?,verified_label=?,verified_model=?,evidence_kind=?,
                        evidence=?,reviewer=?,reviewed_at=? WHERE id=?""", (
                "rejected" if reject else "approved", None if reject else int(label == "ai"),
                model, evidence_kind, evidence, reviewer, utc(), sid))
            db.execute("INSERT INTO events VALUES(?,?,?)", (sid, "rejected" if reject else "independently_approved", utc()))

    def stats(self):
        if not self.path.exists():
            return {"pending": 0, "approved": 0, "rejected": 0, "withdrawn": 0}
        with self.connection() as db:
            return {**dict.fromkeys(("pending", "approved", "rejected", "withdrawn"), 0),
                    **dict(db.execute("SELECT status,count(*) FROM samples GROUP BY status").fetchall())}

    def pending(self, limit=50):
        if not self.path.exists(): return []
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT id,lang,genre,topic,claimed_label,created_at FROM samples WHERE status='pending' ORDER BY created_at,id LIMIT ?", (max(1,min(1000,limit)),))]

    def show(self, sid):
        if not self.path.exists(): return None
        with self.connection() as db:
            row = db.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
            if row is None: return None
            result = dict(row)
            result.pop("withdrawal_hash", None)
            return result

    def approved(self):
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM samples WHERE status='approved' AND text<>'' ORDER BY id")]
