"""Filesystem layout for ``~/.inhere-box/``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from inhere_box.environment import env_get


def default_root(home: Path | None = None) -> Path:
    """Use the new install root, retaining an existing legacy installation."""
    home = home if home is not None else Path.home()
    current = home / ".inhere-box"
    legacy = home / ".octop"
    return legacy if not current.exists() and legacy.is_dir() else current


def _existing_name(parent: Path, name: str, legacy: str) -> Path:
    current = parent / name
    previous = parent / legacy
    return previous if not current.exists() and previous.is_file() else current


@dataclass(frozen=True)
class PathLayout:
    root: Path

    @classmethod
    def from_env(cls) -> PathLayout:
        """Resolve install root from ``INHERE_BOX_HOME`` or ``~/.inhere-box``."""
        raw = env_get("INHERE_BOX_HOME", "").strip()
        if raw:
            return cls(Path(raw).expanduser())
        return cls(default_root())

    @property
    def wizard_password_file(self) -> Path:
        """Initial wizard password file: ``~/.inhere-box/inhere-box-login.txt`` (or ``$INHERE_BOX_HOME/inhere-box-login.txt``)."""
        return _existing_name(self.root, "inhere-box-login.txt", "octop-login.txt")

    @property
    def db(self) -> Path:
        return _existing_name(self.root, "inhere-box.db", "octop.db")

    @property
    def logs_dir(self) -> Path:
        """Structured runtime logs: ``~/.inhere-box/logs/``."""
        return self.root / "logs"

    @property
    def log(self) -> Path:
        return self.logs_dir / "inhere-box.log"

    def ensure_logs_dir(self) -> Path:
        """Create the logs directory and return it."""
        out = self.logs_dir
        out.mkdir(parents=True, exist_ok=True)
        return out

    def ensure_log(self) -> Path:
        """Create the logs directory and return ``inhere-box.log``."""
        self.ensure_logs_dir()
        return self.log

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def agentcomputer_identity(self) -> Path:
        """Installation identity outside database backups and agent workspaces."""
        return self.root / "agentcomputer-control" / "identity.json"

    @property
    def users_dir(self) -> Path:
        return self.root / "users"

    def user_dir(self, username: str) -> Path:
        return self.users_dir / username

    @property
    def agents_dir(self) -> Path:
        """Global agents directory: ~/.inhere-box/agents/"""
        return self.root / "agents"

    @property
    def expert_market_dir(self) -> Path:
        """Cached SkillHub expert templates: ``~/.inhere-box/expert_market/``."""
        return self.root / "expert_market"

    @property
    def published_experts_dir(self) -> Path:
        """User-published expert snapshots: ``~/.inhere-box/published_experts/``."""
        return self.root / "published_experts"

    @property
    def skill_packages_dir(self) -> Path:
        """Global skill package content: ``~/.inhere-box/skill-packages/``."""
        return self.root / "skill-packages"

    @property
    def knowledge_dir(self) -> Path:
        """Global knowledge base files: ``~/.inhere-box/knowledge/``."""
        return self.root / "knowledge"

    def agent_workspace(self, agent_id: str) -> Path:
        """Global agent workspace: ~/.inhere-box/agents/<agent_id>/"""
        return self.agents_dir / agent_id

    def ensure_agent_workspace(self, agent_id: str) -> Path:
        """Global agent workspace, mkdir -p."""
        out = self.agent_workspace(agent_id)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def tool_guard_rules_dir(self) -> Path:
        """User-editable command guard rules: ``~/.inhere-box/security/tool_guard/``."""
        return self.root / "security" / "tool_guard"

    @property
    def tool_guard_rules_file(self) -> Path:
        return self.tool_guard_rules_dir / "dangerous_shell_commands.yaml"

    @property
    def backups_dir(self) -> Path:
        """Stored system backup archives: ``~/.inhere-box/backups/``."""
        return self.root / "backups"

    def ensure_backups_dir(self) -> Path:
        out = self.backups_dir
        out.mkdir(parents=True, exist_ok=True)
        return out

    def backup_file(self, filename: str) -> Path:
        """Resolve a backup archive path under :attr:`backups_dir` (basename only)."""
        return self.backups_dir / Path(filename).name

    @property
    def user_avatars_dir(self) -> Path:
        """Uploaded user portraits: ``~/.inhere-box/avatars/users/``."""
        return self.root / "avatars" / "users"

    @property
    def role_avatars_dir(self) -> Path:
        """Uploaded role portraits: ``~/.inhere-box/avatars/roles/``."""
        return self.root / "avatars" / "roles"

    @property
    def ssl_dir(self) -> Path:
        """TLS certificates and ACME account keys: ``~/.inhere-box/ssl/``."""
        return self.root / "ssl"

    def ensure_ssl_dir(self) -> Path:
        out = self.ssl_dir
        out.mkdir(parents=True, exist_ok=True)
        return out

    @property
    def connector_cli_dir(self) -> Path:
        """Per-instance CLI config roots: ``~/.inhere-box/connector-cli/``."""
        return self.root / "connector-cli"

    def connector_cli_instance_dir(self, kind: str, instance_key: str) -> Path:
        """Isolated config dir for one connector CLI instance."""
        safe_kind = Path(kind).name
        safe_key = (
            "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in instance_key)[:80]
            or "default"
        )
        return self.connector_cli_dir / safe_kind / safe_key

    def ensure_connector_cli_instance_dir(self, kind: str, instance_key: str) -> Path:
        out = self.connector_cli_instance_dir(kind, instance_key)
        out.mkdir(parents=True, exist_ok=True)
        return out
