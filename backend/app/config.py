from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache" / "browsers"))


@dataclass
class Settings:
    root: Path = ROOT
    api_key: str = ""
    base_url: str = ""
    model: str = "deepseek-flash"
    agent_timeout: int = 600
    recursion_limit: int = 80
    max_tokens: int = 12000

    @property
    def data(self) -> Path:
        return self.root / ".data"

    @classmethod
    def load(cls):
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        return cls(api_key=values.get("ANTHROPIC_API_KEY", ""),
                   base_url=values.get("ANTHROPIC_BASE_URL", ""),
                   model=values.get("ANTHROPIC_MODEL", "deepseek-flash"),
                   agent_timeout=int(values.get("AGENT_TIMEOUT", 600)),
                   recursion_limit=int(values.get("AGENT_RECURSION_LIMIT", 80)),
                   max_tokens=int(values.get("MODEL_MAX_TOKENS", 12000)))

    def redact(self, text: str) -> str:
        return str(text).replace(self.api_key, "[REDACTED]") if self.api_key else str(text)
