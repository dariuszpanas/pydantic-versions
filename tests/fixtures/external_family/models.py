from __future__ import annotations

from pydantic import BaseModel


class ExternalConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
