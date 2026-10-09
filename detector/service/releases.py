"""Immutable versioned bundles; reload on pointer change, keep last good model on failure."""
from __future__ import annotations
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from detector.service.engine import Registry

logger = logging.getLogger(__name__)
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def validate_bundle(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf8"))
    if manifest.get("gate_passed") is not True:
        raise ValueError("release has not passed acceptance gates")
    if not manifest.get("artifacts"):
        raise ValueError("release has no checked artifacts")
    checked = set()
    for row in manifest["artifacts"]:
        path = (folder / row["file"]).resolve()
        if path in checked or not path.is_relative_to(folder / "models") or file_hash(path) != row["sha256"]:
            raise ValueError("release artifact path or checksum mismatch")
        checked.add(path)
    if checked != {p.resolve() for p in (folder/"models").rglob("*") if p.is_file()}:
        raise ValueError("release contains unlisted artifacts")
    return manifest

def set_active(root, name):
    root = Path(root).resolve()
    if not NAME.fullmatch(name):
        raise ValueError("invalid release name")
    manifest = validate_bundle(root / name)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "active.json"
    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf8") as f:
        json.dump({"release": name, "manifest_sha256": file_hash(root / name / "manifest.json")}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return manifest

class ReleaseRegistry:
    def __init__(self, baseline, releases, threads=None, languages=None):
        self.baseline, self.releases = Path(baseline), Path(releases)
        self.threads, self.languages = threads, languages
        self.lock = threading.RLock()
        self.registry = None
        self.fingerprint = None
        self.failed_fingerprint = None
        self.active_release = "baseline"

    def get(self):
        with self.lock:
            pointer = self.releases / "active.json"
            try:
                raw = pointer.read_bytes()
            except FileNotFoundError:
                raw = b""
            signature = hashlib.sha256(raw).hexdigest()
            if self.registry is not None and signature in (self.fingerprint, self.failed_fingerprint):
                return self.registry
            try:
                root, release = self.baseline, "baseline"
                if raw:
                    if len(raw) > 4096:
                        raise ValueError("oversized release pointer")
                    selected = json.loads(raw)
                    release = selected["release"]
                    if not NAME.fullmatch(release):
                        raise ValueError("invalid release pointer")
                    folder = self.releases / release
                    if file_hash(folder / "manifest.json") != selected["manifest_sha256"]:
                        raise ValueError("release manifest changed")
                    validate_bundle(folder)
                    root = folder / "models"
                candidate = Registry.from_dir(root, self.threads, self.languages)
                required = self.languages or {"uz", "uz-cyrl", "ru", "en"}
                if raw and not required.issubset(candidate.by_lang):
                    raise ValueError("release is missing requested models")
                self.registry, self.fingerprint, self.active_release = candidate, signature, release
                self.failed_fingerprint = None
            except (OSError, ValueError, KeyError, TypeError):
                logger.exception("invalid detector release; retaining last good registry")
                self.failed_fingerprint = signature
                if self.registry is None:
                    self.registry = Registry.from_dir(self.baseline, self.threads, self.languages)
            return self.registry
