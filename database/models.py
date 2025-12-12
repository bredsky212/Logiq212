"""
Data models for Logiq
Defines structure for database documents
"""

from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from datetime import datetime


@dataclass
class User:
    """User model"""
    user_id: int
    guild_id: int
    xp: int = 0
    level: int = 0
    balance: int = 1000
    inventory: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    last_message: Optional[float] = None
    last_daily: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "user_id": self.user_id,
            "guild_id": self.guild_id,
            "xp": self.xp,
            "level": self.level,
            "balance": self.balance,
            "inventory": self.inventory,
            "warnings": self.warnings,
            "created_at": self.created_at,
            "last_message": self.last_message,
            "last_daily": self.last_daily
        }


@dataclass
class Guild:
    """Guild configuration model"""
    guild_id: int
    prefix: str = "/"
    log_channel: Optional[int] = None
    welcome_channel: Optional[int] = None
    verified_role: Optional[int] = None
    modules: Dict[str, bool] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "guild_id": self.guild_id,
            "prefix": self.prefix,
            "log_channel": self.log_channel,
            "welcome_channel": self.welcome_channel,
            "verified_role": self.verified_role,
            "modules": self.modules,
            "created_at": self.created_at
        }


@dataclass
class Warning:
    """Warning model"""
    moderator_id: int
    reason: str
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "moderator_id": self.moderator_id,
            "reason": self.reason,
            "timestamp": self.timestamp
        }


@dataclass
class Report:
    """User report model"""
    guild_id: int
    reporter_id: int
    reported_user_id: int
    category: str
    reason: str
    message_link: Optional[str] = None
    message_id: Optional[int] = None
    channel_id: Optional[int] = None
    status: str = "open"
    created_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None
    resolved_by_id: Optional[int] = None
    moderation_action: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "guild_id": self.guild_id,
            "reporter_id": self.reporter_id,
            "reported_user_id": self.reported_user_id,
            "category": self.category,
            "reason": self.reason,
            "message_link": self.message_link,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by_id": self.resolved_by_id,
            "moderation_action": self.moderation_action
        }


@dataclass
class Ticket:
    """Support ticket model"""
    ticket_id: str
    guild_id: int
    user_id: int
    channel_id: int
    category: str
    status: str = "open"  # open, closed, resolved
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    closed_at: Optional[float] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "ticket_id": self.ticket_id,
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "channel_id": self.channel_id,
            "category": self.category,
            "status": self.status,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "messages": self.messages
        }


@dataclass
class ShopItem:
    """Shop item model"""
    item_id: str
    guild_id: int
    name: str
    description: str
    price: int
    role_id: Optional[int] = None
    stock: int = -1  # -1 = unlimited
    purchasable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "item_id": self.item_id,
            "guild_id": self.guild_id,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "role_id": self.role_id,
            "stock": self.stock,
            "purchasable": self.purchasable
        }


@dataclass
class Reminder:
    """Reminder model"""
    reminder_id: str
    user_id: int
    guild_id: int
    channel_id: int
    message: str
    remind_at: float
    completed: bool = False
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "reminder_id": self.reminder_id,
            "user_id": self.user_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message": self.message,
            "remind_at": self.remind_at,
            "completed": self.completed,
            "created_at": self.created_at
        }


@dataclass
class AnalyticsEvent:
    """Analytics event model"""
    event_type: str
    guild_id: int
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "type": self.event_type,
            "guild_id": self.guild_id,
            "timestamp": self.timestamp,
            **self.data
        }


@dataclass
class StaffApplicationField:
    """Field configuration for staff applications"""
    key: str
    label: str
    style: str  # "short" | "paragraph"
    required: bool = True
    max_length: int = 1000
    placeholder: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "key": self.key,
            "label": self.label,
            "style": self.style,
            "required": self.required,
            "max_length": self.max_length,
            "placeholder": self.placeholder,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationField":
        """Create from dictionary"""
        return cls(
            key=data["key"],
            label=data["label"],
            style=data.get("style", "paragraph"),
            required=data.get("required", True),
            max_length=data.get("max_length", 1000),
            placeholder=data.get("placeholder"),
        )


@dataclass
class StaffApplicationTemplate:
    """Template describing a staff application form"""
    guild_id: int
    template_id: str
    name: str
    description: str
    team_role_id: Optional[int]
    apply_channel_id: int
    review_channel_id: int
    fields: List[StaffApplicationField]
    created_by_id: int
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "guild_id": self.guild_id,
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "team_role_id": self.team_role_id,
            "apply_channel_id": self.apply_channel_id,
            "review_channel_id": self.review_channel_id,
            "fields": [f.to_dict() for f in self.fields],
            "created_by_id": self.created_by_id,
            "created_at": self.created_at,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationTemplate":
        """Create from dictionary"""
        return cls(
            guild_id=data["guild_id"],
            template_id=str(data["template_id"]),
            name=data["name"],
            description=data.get("description", ""),
            team_role_id=data.get("team_role_id"),
            apply_channel_id=data["apply_channel_id"],
            review_channel_id=data["review_channel_id"],
            fields=[StaffApplicationField.from_dict(f) for f in data.get("fields", [])],
            created_by_id=data["created_by_id"],
            created_at=data.get("created_at", datetime.utcnow()),
            is_active=data.get("is_active", True),
        )


@dataclass
class StaffApplicationAnswer:
    """Answer to a staff application field"""
    key: str
    label: str
    value: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationAnswer":
        """Create from dictionary"""
        return cls(
            key=data["key"],
            label=data["label"],
            value=data["value"],
        )


@dataclass
class StaffApplication:
    """Staff application submission"""
    guild_id: int
    template_id: str
    application_id: str
    applicant_id: int
    team_role_id: Optional[int]
    answers: List[StaffApplicationAnswer]
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    reviewed_by_id: Optional[int] = None
    review_notes: Optional[str] = None
    review_channel_id: int = 0
    review_message_id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "guild_id": self.guild_id,
            "template_id": self.template_id,
            "application_id": self.application_id,
            "applicant_id": self.applicant_id,
            "team_role_id": self.team_role_id,
            "answers": [a.to_dict() for a in self.answers],
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_by_id": self.reviewed_by_id,
            "review_notes": self.review_notes,
            "review_channel_id": self.review_channel_id,
            "review_message_id": self.review_message_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplication":
        """Create from dictionary"""
        return cls(
            guild_id=data["guild_id"],
            template_id=str(data["template_id"]),
            application_id=str(data["application_id"]),
            applicant_id=data["applicant_id"],
            team_role_id=data.get("team_role_id"),
            answers=[StaffApplicationAnswer.from_dict(a) for a in data.get("answers", [])],
            status=data.get("status", "pending"),
            created_at=data.get("created_at", datetime.utcnow()),
            updated_at=data.get("updated_at", datetime.utcnow()),
            reviewed_by_id=data.get("reviewed_by_id"),
            review_notes=data.get("review_notes"),
            review_channel_id=data.get("review_channel_id", 0),
            review_message_id=data.get("review_message_id", 0),
        )


class FeatureKey(str, Enum):
    """Feature keys used for permission gating"""
    # Moderation
    MOD_VC_SUSPEND = "mod.vc_suspend"
    MOD_VC_UNSUSPEND = "mod.vc_unsuspend"
    MOD_WARN = "mod.warn"
    MOD_WARNINGS = "mod.warnings"
    MOD_TIMEOUT = "mod.timeout"
    MOD_BAN = "mod.ban"
    MOD_KICK = "mod.kick"
    MOD_CLEAR = "mod.clear"
    MOD_SLOWMODE = "mod.slowmode"
    MOD_LOCK = "mod.lock"
    MOD_NICKNAME = "mod.nickname"
    REPORT_CREATE = "report.create"

    # Tickets
    TICKETS_CREATE = "tickets.create"
    TICKETS_CLOSE = "tickets.close"
    TICKETS_ADMIN = "tickets.admin"

    # Staff applications
    STAFFAPP_TEMPLATE_MANAGE = "staffapp.template.manage"
    STAFFAPP_REVIEW = "staffapp.review"

    # Reports
    REPORT_VIEW = "report.view"
    REPORT_MANAGE = "report.manage"

    # Permissions management
    PERMS_MANAGE = "perms.manage"

    # Verification
    VERIFY_CONFIG = "verify.config"

    # Games
    GAMES_PANEL_MANAGE = "games.panel.manage"

    # Roles
    ROLES_MENU_MANAGE = "roles.menu.manage"
    ROLES_FORCE_ASSIGN = "roles.force.assign"

    # Economy
    ECONOMY_ADMIN_ADJUST = "economy.admin.adjust"

    # Leveling
    LEVELING_ADMIN_SET = "leveling.admin.set"
    LEVELING_ADMIN_RESET = "leveling.admin.reset"

    # Giveaways
    GIVEAWAY_CREATE = "giveaway.create"
    GIVEAWAY_MANAGE = "giveaway.manage"

    # Music
    MUSIC_DJ_BASIC = "music.dj.basic"
    MUSIC_DJ_VOLUME = "music.dj.volume"

    # Social alerts
    ALERTS_MANAGE = "alerts.manage"
    ALERTS_VIEW = "alerts.view"

    # Temporary voice
    TEMPVOICE_SETUP = "tempvoice.setup"
    TEMPVOICE_OWNER_POWER = "tempvoice.owner.power"

    # Utility
    UTILITY_POLL = "utility.poll"

    # Analytics
    ANALYTICS_VIEW = "analytics.view"


@dataclass
class FeaturePermission:
    """Per-feature role allow/deny configuration"""
    guild_id: int
    feature_key: str
    allowed_roles: List[int] = field(default_factory=list)
    denied_roles: List[int] = field(default_factory=list)
    updated_by: Optional[int] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "feature_key": self.feature_key,
            "allowed_roles": self.allowed_roles,
            "denied_roles": self.denied_roles,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }


@dataclass
class FeaturePermissionAudit:
    """Audit log entry for feature permission changes"""
    guild_id: int
    feature_key: str
    changed_by: int
    change_type: str  # allow | deny | clear | reset
    role_id: Optional[int]
    old_doc: Dict[str, Any]
    new_doc: Dict[str, Any]
    at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "feature_key": self.feature_key,
            "changed_by": self.changed_by,
            "change_type": self.change_type,
            "role_id": self.role_id,
            "old_doc": self.old_doc,
            "new_doc": self.new_doc,
            "at": self.at,
        }


@dataclass
class GuildSecurityConfig:
    """Security configuration for a guild."""
    guild_id: int
    protected_role_ids: List[int] = field(default_factory=list)
    initialized: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "protected_role_ids": self.protected_role_ids,
            "initialized": self.initialized,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Suspension:
    """Voice/chat suspension record (timeout)"""
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    duration_seconds: int
    started_at: datetime
    ends_at: datetime
    type: str = "timeout"
    active: bool = True
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "moderator_id": self.moderator_id,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "type": self.type,
            "active": self.active,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }
