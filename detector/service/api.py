"""HTTP surface of the detector service.

    POST /detect  {"text": "..."}         -> analysis (see analysis.analyze)
    POST /ocr     multipart files=<img>…  -> one text from all images, in order
    GET  /healthz                         -> loaded models, OCR languages

Internal service: compose publishes no port for it, the bot reaches it on the
compose network. If DETECTOR_API_TOKEN is set, every call except /healthz
must carry it in X-Detector-Token.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import sqlite3
from typing import Literal
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from fastapi.responses import HTMLResponse

from detector.service import analysis, ocr
from detector.service.corpus import CorpusStore, DuplicateSample, CorpusFull
from detector.service.releases import ReleaseRegistry
from detector.service.engine import Registry, default_threads
from detector.service.text import word_count

logging.basicConfig(level=os.environ.get("DETECTOR_LOG_LEVEL", "INFO"))
logger = logging.getLogger("detector.api")

MAX_TEXT_CHARS = 200_000
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
BUSY_WAIT_SECONDS = 30


class DetectRequest(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_CHARS)
    explain: bool = False


class SentenceScore(BaseModel):
    text: str
    ai_probability: float | None


class DetectResponse(BaseModel):
    lang: str
    supported: bool
    ai_probability: float | None
    human_similarity: float | None = None
    authorship_confidence: float | None = None
    score_kind: str = "model_similarity_not_authorship_probability"
    api_contract_version: int = 2
    decision_thresholds: dict[str, float] | None = None
    explanation: dict | None = None
    verdict: str
    sentences: list[SentenceScore]
    model_version: str | None
    word_count: int
    truncated: bool
    chunks: int
    detail: str | None = None


class SampleRequest(DetectRequest):
    consent: Literal[True]
    claimed_label: Literal["human", "ai", "unknown"] = "unknown"
    claimed_model: str = Field(default="", max_length=120)
    genre: Literal["insho", "maqola", "blog", "yangilik", "badiiy", "ilmiy", "rasmiy"] = "insho"
    topic: str = Field(default="", max_length=300)

    @field_validator("consent", mode="before")
    @classmethod
    def explicit_consent(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("explicit boolean consent required")
        return value


class WithdrawalRequest(BaseModel):
    sample_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    withdrawal_token: str = Field(min_length=20, max_length=100)


def create_app(registry: Registry | None = None) -> FastAPI:
    state = {"registry": registry}
    limits = analysis.Limits.from_env()
    detector_root = Path(__file__).resolve().parents[1]
    collection_enabled = os.environ.get("DETECTOR_COLLECTION_ENABLED", "false").lower() in {"1","true","yes"}
    corpus = CorpusStore(Path(os.environ.get("DETECTOR_CORPUS_DB", str(detector_root / "data/feedback/corpus.sqlite3"))))
    manager = ReleaseRegistry(Path(os.environ.get("DETECTOR_MODELS_DIR", str(detector_root / "models"))),
        Path(os.environ.get("DETECTOR_RELEASES_DIR", str(detector_root / "data/model_updates/releases"))),
        threads=default_threads(), languages={v.strip() for v in os.environ.get("DETECTOR_LANGUAGES", "").split(",") if v.strip()} or None)
    # Inference is CPU-bound and each run already uses every intra-op thread;
    # running two at once only doubles peak RAM. Callers queue here instead.
    gate = threading.Semaphore(int(os.environ.get("DETECTOR_MAX_CONCURRENCY", "1")))
    token = os.environ.get("DETECTOR_API_TOKEN") or ""

    def reg() -> Registry:
        if registry is None:
            state["registry"] = manager.get()
        return state["registry"]

    def check_token(x_detector_token: str | None = Header(default=None)) -> None:
        if token and not hmac.compare_digest(x_detector_token or "", token):
            raise HTTPException(status_code=401, detail="bad token")

    @contextmanager
    def slot():
        if not gate.acquire(timeout=BUSY_WAIT_SECONDS):
            raise HTTPException(status_code=503, detail="busy")
        try:
            yield
        finally:
            gate.release()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        r = reg()
        logger.info(
            "detector ready: %s",
            {lang: m.version for lang, m in r.by_lang.items()} or "no models",
        )
        yield

    app = FastAPI(
        title="Testova AI detector", docs_url=None, redoc_url=None, lifespan=lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return Path(__file__).with_name("web.html").read_text(encoding="utf8")

    @app.get("/healthz")
    def healthz() -> dict:
        r = reg()
        return {
            "status": "ok",
            "models": [
                {"name": m.name, "version": m.version, "languages": m.languages}
                for m in r.models
            ],
            "ocr_languages": ocr.tesseract_languages(),
            "collection_enabled": collection_enabled,
            "automatic_learning_enabled": os.environ.get("DETECTOR_AUTO_LEARN_ENABLED", "false").lower() in {"1","true","yes"},
            "feedback_consent_version": "local-training-v1",
            "collection_counts": corpus.stats() if collection_enabled else None,
            "active_release": manager.active_release if registry is None else "injected",
            "api_contract_version": 2,
            "score_kind": "model_similarity_not_authorship_probability",
            "minimum_words_by_language": {lang: max(limits.min_words,getattr(m,"min_words",0)) for lang,m in r.by_lang.items()},
        }

    @app.post("/detect", response_model=DetectResponse, dependencies=[Depends(check_token)])
    def detect(req: DetectRequest) -> dict:
        with slot():
            return analysis.analyze(req.text, reg(), limits, explain=req.explain)

    @app.post("/samples", dependencies=[Depends(check_token)])
    def collect(req: SampleRequest) -> dict:
        if not collection_enabled:
            raise HTTPException(status_code=403, detail="collection disabled")
        with slot():
            result = analysis.analyze(req.text, reg(), limits)
        if result["ai_probability"] is None or result["lang"] not in {"uz","uz-cyrl","ru","en"}:
            raise HTTPException(status_code=422, detail="collection requires a supported 150+ word text")
        try:
            return corpus.add(text=req.text, lang=result["lang"], genre=req.genre, topic=req.topic,
                claimed_label=req.claimed_label, claimed_model=req.claimed_model, analysis=result)
        except DuplicateSample as exc:
            raise HTTPException(status_code=409, detail="duplicate text") from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="collection temporarily unavailable") from exc
        except CorpusFull as exc:
            raise HTTPException(status_code=507, detail="collection limit reached") from exc

    @app.post("/samples/withdraw", dependencies=[Depends(check_token)])
    def withdraw(req: WithdrawalRequest) -> dict:
        # Keep withdrawals available even after new collection has been disabled.
        if not corpus.withdraw(req.sample_id, req.withdrawal_token):
            raise HTTPException(status_code=404, detail="receipt not found")
        return {"status":"withdrawn", "excluded_from_future_training":True,
                "existing_model_weights_automatically_unlearned":False}

    @app.post("/ocr", dependencies=[Depends(check_token)])
    def run_ocr(files: list[UploadFile] = File(...)) -> dict:
        if not files:
            raise HTTPException(status_code=422, detail="no images")
        if len(files) > MAX_IMAGES:
            raise HTTPException(status_code=413, detail=f"at most {MAX_IMAGES} images")
        blobs = []
        for f in files:
            data = f.file.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="image too large")
            blobs.append(data)
        with slot():
            try:
                pages = [ocr.ocr_image(b) for b in blobs]
            except ocr.OcrUnavailable as e:
                logger.error("ocr unavailable: %s", e)
                raise HTTPException(status_code=503, detail="ocr unavailable") from e
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
        result = ocr.combine(pages)
        result["word_count"] = word_count(result["text"])
        return result

    return app


app = create_app()
