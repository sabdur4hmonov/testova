"""Async HTTP contract for later Telegram wiring, independent of aiogram/config/DB."""
from __future__ import annotations
import httpx

class DetectorGateway:
    def __init__(self, url, token="", timeout=60):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    async def request(self, path, payload=None):
        headers = {"X-Detector-Token": self.token} if self.token else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await (client.get(self.url + path, headers=headers) if payload is None
                              else client.post(self.url + path, json=payload, headers=headers))
            response.raise_for_status()
            return response.json()

    async def detect(self, text, *, explain=True):
        # Scores and labels come from the service; neither changes the bot user record.
        return await self.request("/detect", {"text": text,"explain":explain})

    async def collect(self, text, *, consent, claimed_label="unknown", model="", genre="insho", topic=""):
        if consent is not True:
            raise ValueError("explicit per-text training consent required")
        return await self.request("/samples", {"text": text, "consent": True,
            "claimed_label": claimed_label, "claimed_model": model, "genre": genre, "topic": topic})

    async def withdraw(self, sample_id, withdrawal_token):
        return await self.request("/samples/withdraw", {"sample_id": sample_id, "withdrawal_token": withdrawal_token})

    async def capabilities(self):
        return await self.request("/healthz")

def result_for_bot(result):
    """A translation-friendly presentation contract. No authorship certainty invented."""
    p = result.get("ai_probability")
    return {"language": result["lang"], "verdict": result["verdict"],
            "ai_similarity_percent": None if p is None else round(p * 100, 1),
            "human_similarity_percent": None if p is None else round((1 - p) * 100, 1),
            "authorship_confidence_percent": None,
            "independently_validated": False, "model_version": result.get("model_version"),
            "partial_text": result.get("truncated", False),
            "message_key": "similarity_is_not_authorship_probability", "explanation": result.get("explanation")}
