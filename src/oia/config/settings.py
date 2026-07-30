"""Loads .env (secrets) and config.yaml (everything else) into a validated Settings object.

Resolution walks up from the current working directory looking for a
`config/config.yaml`, so `oia` works whether invoked from the project root
or a subdirectory - the same convention pyproject.toml/git use.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(RuntimeError):
    """Raised for missing/invalid configuration - always carries an actionable message."""


class ReportRules(BaseModel):
    schema_patterns: list[str] = Field(default_factory=list)
    name_prefixes: list[str] = Field(default_factory=list)
    name_suffixes: list[str] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)


class SchemasConfig(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model: str = "claude-sonnet-5"
    effort: str = "medium"
    max_tokens: int = 4096


class GraphConfig(BaseModel):
    sqlite_path: str = "data/oia.sqlite"


class Settings(BaseModel):
    # Oracle connection (from .env only - never from config.yaml)
    oracle_user: str
    oracle_password: str
    oracle_dsn: str

    # Anthropic (from .env only; optional - only required for `oia ask`)
    anthropic_api_key: str | None = None

    # From config.yaml
    dictionary_scope: str = "all"
    schemas: SchemasConfig = Field(default_factory=SchemasConfig)
    report_rules: ReportRules = Field(default_factory=ReportRules)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    logging_level: str = "INFO"

    project_root: Path

    @property
    def sqlite_path(self) -> Path:
        p = Path(self.graph.sqlite_path)
        return p if p.is_absolute() else self.project_root / p


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "config.yaml").exists():
            return candidate
    return current


def get_settings(project_root: Path | None = None) -> Settings:
    root = find_project_root(project_root)

    load_dotenv(root / ".env", override=False)

    config_path = root / "config" / "config.yaml"
    raw: dict = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    missing = [
        name
        for name in ("ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN")
        if not os.environ.get(name)
    ]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Copy {root / '.env.example'} to {root / '.env'} and fill in real values."
        )

    return Settings(
        oracle_user=os.environ["ORACLE_USER"],
        oracle_password=os.environ["ORACLE_PASSWORD"],
        oracle_dsn=os.environ["ORACLE_DSN"],
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        dictionary_scope=raw.get("dictionary_scope", "all"),
        schemas=SchemasConfig(**raw.get("schemas", {})),
        report_rules=ReportRules(**raw.get("report_rules", {})),
        agent=AgentConfig(**raw.get("agent", {})),
        graph=GraphConfig(**raw.get("graph", {})),
        logging_level=raw.get("logging", {}).get("level", "INFO"),
        project_root=root,
    )
