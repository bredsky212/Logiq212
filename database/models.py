"""
Data models for Logiq
Defines structure for database documents
"""

from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
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
