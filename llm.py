"""OpenRouter client with a hard spend cap and a per-call cost ledger."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

KEY_FILE = Path.home() / ".config/secrets/openrouter_api_key"


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, budget: float, ledger: Path):
        self.budget = budget
        self.ledger = ledger
        self.spent = self.total_from_ledger()  # resumed runs count earlier spend
        self.c = httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1",
            headers={"Authorization": f"Bearer {KEY_FILE.read_text().strip()}",
                     "X-Title": "Librarians of Babel"},
            timeout=180,
        )

    def total_from_ledger(self) -> float:
        if not self.ledger.exists():
            return 0.0
        return sum(json.loads(l).get("cost", 0) for l in self.ledger.read_text().splitlines() if l.strip())

    async def raw(self, model: str, messages: list, max_tokens: int, tools=None, json_mode=False, reasoning: dict | None = None) -> dict:
        if self.spent >= self.budget:
            raise BudgetExceeded(f"spent ${self.spent:.3f} of ${self.budget:.2f}")
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        if reasoning is not None:
            body["reasoning"] = reasoning
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(5):
            try:
                r = await self.c.post("/chat/completions", json=body)
            except httpx.TransportError:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{model} {r.status_code}: {r.text[:300]}")
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"{model}: {d['error']}")
            u = d.get("usage") or {}
            cost = float(u.get("cost") or 0)
            self.spent += cost
            with self.ledger.open("a") as f:
                f.write(json.dumps({"t": time.time(), "model": model, "in": u.get("prompt_tokens"),
                                    "out": u.get("completion_tokens"), "cost": cost}) + "\n")
            return d["choices"][0]["message"]
        raise RuntimeError(f"{model}: retries exhausted")

    async def chat(self, model: str, messages: list, max_tokens: int = 1000, json_mode=False, reasoning=None) -> str:
        msg = await self.raw(model, messages, max_tokens, json_mode=json_mode, reasoning=reasoning)
        return msg.get("content") or ""
