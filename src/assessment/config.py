import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def read_mapping(name: str, fallback: dict[str, str]) -> dict[str, str]:
    raw = json.loads(os.environ.get(name, json.dumps(fallback)))
    if (
        not isinstance(raw, dict)
        or not raw
        or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in raw.items()
        )
    ):
        raise ValueError(f"Invalid {name} mapping")
    return raw


@dataclass(frozen=True)
class Settings:
    roles: dict[str, str] = field(
        default_factory=lambda: {
            digest("dev-admin-token"): "admin",
            digest("dev-viewer-token"): "viewer",
        }
    )
    tenants: dict[str, str] = field(
        default_factory=lambda: {digest("dev-tenant-key"): "tenant-demo"}
    )
    downstream: str = "http://127.0.0.1:8001/mcp"
    provider: str = "http://127.0.0.1:8001"
    secondary_provider: str | None = None
    database: Path = Path("runtime/quota.sqlite3")
    limit: int = 50_000
    window: float = 60
    primary_timeout: float = 3
    secondary_timeout: float = 5
    mcp_timeout: float = 5

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        roles = read_mapping(
            "DEV_ROLE_TOKENS", {"dev-admin-token": "admin", "dev-viewer-token": "viewer"}
        )
        tenants = read_mapping("TENANT_KEY_DIGESTS", {digest("dev-tenant-key"): "tenant-demo"})
        if any(not re.fullmatch(r"[0-9a-f]{64}", key) for key in tenants):
            raise ValueError("Tenant key map requires lowercase SHA-256 digests")
        result = cls(
            roles={digest(token): role for token, role in roles.items()},
            tenants=tenants,
            downstream=os.getenv("DOWNSTREAM_URL", "http://127.0.0.1:8001/mcp"),
            provider=os.getenv("PROVIDER_URL", "http://127.0.0.1:8001"),
            secondary_provider=os.getenv("SECONDARY_PROVIDER_URL"),
            database=Path(os.getenv("QUOTA_DB", "runtime/quota.sqlite3")),
            limit=int(os.getenv("TOKEN_LIMIT", "50000")),
            window=float(os.getenv("WINDOW_SECONDS", "60")),
            primary_timeout=float(os.getenv("PRIMARY_TIMEOUT", "3")),
            secondary_timeout=float(os.getenv("SECONDARY_TIMEOUT", "5")),
            mcp_timeout=float(os.getenv("MCP_TIMEOUT", "5")),
        )
        limits = (
            result.limit,
            result.window,
            result.primary_timeout,
            result.secondary_timeout,
            result.mcp_timeout,
        )
        if any(not math.isfinite(value) or value <= 0 for value in limits):
            raise ValueError("Limits and timeouts must be finite and positive")
        if not set(result.roles.values()) <= {"admin", "viewer"}:
            raise ValueError("Unknown development role")
        return result
