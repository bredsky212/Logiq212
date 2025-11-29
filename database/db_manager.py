"""
Database Manager for Logiq
Handles async MongoDB operations with connection pooling
"""

import asyncio
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async MongoDB database manager with connection pooling"""

    def __init__(self, uri: str, database_name: str, pool_size: int = 10):
        """
        Initialize database manager

        Args:
            uri: MongoDB connection URI
            database_name: Name of the database
            pool_size: Maximum connection pool size
        """
        self.uri = uri
        self.database_name = database_name
        self.pool_size = pool_size
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self._connected = False

    async def connect(self) -> None:
        """Establish database connection"""
        try:
            self.client = AsyncIOMotorClient(
                self.uri,
                maxPoolSize=self.pool_size,
                minPoolSize=1,
                serverSelectionTimeoutMS=5000
            )
            self.db = self.client[self.database_name]
            # Test connection
            await self.client.admin.command('ping')
            await self._ensure_indexes()
            self._connected = True
            logger.info(f"Connected to MongoDB database: {self.database_name}")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    async def disconnect(self) -> None:
        """Close database connection"""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("Disconnected from MongoDB")

    @property
    def is_connected(self) -> bool:
        """Check if database is connected"""
        return self._connected

    @property
    def reports(self):
        """Access reports collection"""
        return self.db.reports if self.db is not None else None

    async def _ensure_indexes(self) -> None:
        """Ensure required indexes are present"""
        if self.db is None:
            return

        try:
            await self.db.reports.create_index([("guild_id", 1), ("status", 1)])
            await self.db.reports.create_index([("reported_user_id", 1), ("guild_id", 1)])
        except Exception as e:
            logger.warning(f"Failed to ensure report indexes: {e}")

    # User operations
    async def get_user(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get user document"""
        return await self.db.users.find_one({
            "user_id": user_id,
            "guild_id": guild_id
        })

    async def create_user(self, user_id: int, guild_id: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create new user document"""
        user_data = {
            "user_id": user_id,
            "guild_id": guild_id,
            "xp": 0,
            "level": 0,
            "balance": 1000,
            "inventory": [],
            "warnings": [],
            "created_at": asyncio.get_event_loop().time()
        }
        if data:
            user_data.update(data)

        await self.db.users.insert_one(user_data)
        return user_data

    async def update_user(self, user_id: int, guild_id: int, data: Dict[str, Any]) -> bool:
        """Update user document"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$set": data}
        )
        return result.modified_count > 0

    async def increment_user_field(self, user_id: int, guild_id: int, field: str, amount: int = 1) -> bool:
        """Increment a numeric field in user document"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$inc": {field: amount}}
        )
        return result.modified_count > 0

    # Guild operations
    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get guild configuration"""
        return await self.db.guilds.find_one({"guild_id": guild_id})

    async def create_guild(self, guild_id: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create new guild configuration"""
        guild_data = {
            "guild_id": guild_id,
            "prefix": "/",
            "modules": {},
            "log_channel": None,
            "welcome_channel": None,
            "verified_role": None,
            "created_at": asyncio.get_event_loop().time()
        }
        if data:
            guild_data.update(data)

        await self.db.guilds.insert_one(guild_data)
        return guild_data

    async def update_guild(self, guild_id: int, data: Dict[str, Any]) -> bool:
        """Update guild configuration"""
        result = await self.db.guilds.update_one(
            {"guild_id": guild_id},
            {"$set": data}
        )
        return result.modified_count > 0

    # Leveling operations
    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Get XP leaderboard for guild"""
        cursor = self.db.users.find(
            {"guild_id": guild_id}
        ).sort("xp", -1).limit(limit)
        return await cursor.to_list(length=limit)

    # Economy operations
    async def add_balance(self, user_id: int, guild_id: int, amount: int) -> bool:
        """Add to user balance"""
        return await self.increment_user_field(user_id, guild_id, "balance", amount)

    async def remove_balance(self, user_id: int, guild_id: int, amount: int) -> bool:
        """Remove from user balance"""
        user = await self.get_user(user_id, guild_id)
        if user and user.get("balance", 0) >= amount:
            return await self.increment_user_field(user_id, guild_id, "balance", -amount)
        return False

    async def add_item(self, user_id: int, guild_id: int, item: Dict[str, Any]) -> bool:
        """Add item to user inventory"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$push": {"inventory": item}}
        )
        return result.modified_count > 0

    # Moderation operations
    async def add_warning(self, user_id: int, guild_id: int, warning: Dict[str, Any]) -> bool:
        """Add warning to user"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$push": {"warnings": warning}}
        )
        return result.modified_count > 0

    async def get_warnings(self, user_id: int, guild_id: int) -> List[Dict[str, Any]]:
        """Get user warnings"""
        user = await self.get_user(user_id, guild_id)
        return user.get("warnings", []) if user else []

    async def create_report(self, report_data: Dict[str, Any]) -> str:
        """Create user report"""
        result = await self.db.reports.insert_one(report_data)
        return str(result.inserted_id)

    # Tickets operations
    async def create_ticket(self, ticket_data: Dict[str, Any]) -> str:
        """Create support ticket"""
        result = await self.db.tickets.insert_one(ticket_data)
        return str(result.inserted_id)

    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Get ticket by ID"""
        from bson import ObjectId
        return await self.db.tickets.find_one({"_id": ObjectId(ticket_id)})

    async def update_ticket(self, ticket_id: str, data: Dict[str, Any]) -> bool:
        """Update ticket"""
        from bson import ObjectId
        result = await self.db.tickets.update_one(
            {"_id": ObjectId(ticket_id)},
            {"$set": data}
        )
        return result.modified_count > 0

    # Analytics operations
    async def log_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Log analytics event"""
        event = {
            "type": event_type,
            "timestamp": asyncio.get_event_loop().time(),
            **data
        }
        await self.db.analytics.insert_one(event)

    async def get_analytics(
        self,
        guild_id: int,
        event_type: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Get analytics events with filters"""
        query = {"guild_id": guild_id}
        if event_type:
            query["type"] = event_type
        if start_time or end_time:
            query["timestamp"] = {}
            if start_time:
                query["timestamp"]["$gte"] = start_time
            if end_time:
                query["timestamp"]["$lte"] = end_time

        cursor = self.db.analytics.find(query).sort("timestamp", -1)
        return await cursor.to_list(length=1000)

    # Reminder operations
    async def create_reminder(self, reminder_data: Dict[str, Any]) -> str:
        """Create reminder"""
        result = await self.db.reminders.insert_one(reminder_data)
        return str(result.inserted_id)

    async def get_due_reminders(self, current_time: float) -> List[Dict[str, Any]]:
        """Get reminders that are due"""
        cursor = self.db.reminders.find({
            "remind_at": {"$lte": current_time},
            "completed": False
        })
        return await cursor.to_list(length=100)

    async def complete_reminder(self, reminder_id: str) -> bool:
        """Mark reminder as completed"""
        from bson import ObjectId
        result = await self.db.reminders.update_one(
            {"_id": ObjectId(reminder_id)},
            {"$set": {"completed": True}}
        )
        return result.modified_count > 0

    # Shop operations
    async def get_shop_items(self, guild_id: int) -> List[Dict[str, Any]]:
        """Get shop items for guild"""
        cursor = self.db.shop.find({"guild_id": guild_id})
        return await cursor.to_list(length=100)

    async def create_shop_item(self, item_data: Dict[str, Any]) -> str:
        """Create shop item"""
        result = await self.db.shop.insert_one(item_data)
        return str(result.inserted_id)
