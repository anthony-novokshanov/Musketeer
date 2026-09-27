"""Meta FAIR Contriever (spec §8.6), hybrid mode only. Lazy import: muse mode never loads torch.

facebook/contriever-msmarco, mean pooling over the attention mask, L2-normalized, 768 dims, CPU.
Cosine scores from it are uncalibrated: use them to order candidates, never as a threshold.
"""
import asyncio
import logging
import threading

from app.config import settings

log = logging.getLogger("musketeer.embedder")

DIMS = 768
BATCH = 16
MAX_TOKENS = 512

_model = None
_lock = threading.Lock()


def _load():
    global _model
    with _lock:
        if _model is None:
            from transformers import AutoModel, AutoTokenizer
            _model = (AutoTokenizer.from_pretrained(settings.EMBED_MODEL), AutoModel.from_pretrained(settings.EMBED_MODEL).eval())
    return _model


def warm() -> None:
    _load()
    log.info("Contriever loaded (%s)", settings.EMBED_MODEL)


def embed_sync(texts: list[str]) -> list[list[float]]:
    import torch
    tokenizer, model = _load()
    out: list[list[float]] = []
    with torch.inference_mode():
        for i in range(0, len(texts), BATCH):
            batch = tokenizer(texts[i:i + BATCH], padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
            hidden = model(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            out += torch.nn.functional.normalize(pooled, dim=-1).tolist()
    return out


async def embed(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(embed_sync, texts) if texts else []


def literal(vec: list[float]) -> str:
    """pgvector text form; pass with a ::vector cast."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
