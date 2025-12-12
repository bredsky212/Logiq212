import asyncio
import types

import pytest

from utils.security import is_protected_member, get_or_bootstrap_security
from utils.feature_permissions import FeaturePermissionManager
from database.models import FeatureKey


class FakePermissions:
    def __init__(self, administrator=False, manage_guild=False):
        self.administrator = administrator
        self.manage_guild = manage_guild


class FakeRole:
    def __init__(self, role_id: int, permissions: FakePermissions, position: int = 0):
        self.id = role_id
        self.permissions = permissions
        self.position = position


class FakeGuild:
    def __init__(self, guild_id: int, roles, owner):
        self.id = guild_id
        self.roles = roles
        self.owner = owner

    def get_role(self, role_id: int):
        for r in self.roles:
            if r.id == role_id:
                return r
        return None


class FakeMember:
    def __init__(self, guild: FakeGuild, roles, guild_permissions: FakePermissions, is_owner=False):
        self.guild = guild
        self.roles = roles
        self.guild_permissions = guild_permissions
        self.id = 123
        if is_owner:
            guild.owner = self


class FakeDB:
    def __init__(self, security=None):
        self.security = security

    async def get_guild_security(self, guild_id: int):
        return self.security

    async def upsert_guild_security(self, guild_id: int, payload):
        self.security = payload
        return payload

    async def get_feature_permission(self, guild_id: int, feature_key: str):
        return None


@pytest.mark.asyncio
async def test_is_protected_member_bootstrap_detects_admin_roles():
    admin_role = FakeRole(1, FakePermissions(administrator=True), position=10)
    regular_role = FakeRole(2, FakePermissions(administrator=False), position=1)
    guild = FakeGuild(42, [admin_role, regular_role], owner=None)
    member = FakeMember(guild, [admin_role], FakePermissions())
    db = FakeDB()

    security = await get_or_bootstrap_security(db, guild)
    assert admin_role.id in security.get("protected_role_ids", [])
    assert await is_protected_member(db, guild, member) is True


@pytest.mark.asyncio
async def test_sensitive_feature_blocked_until_security_initialized():
    guild = FakeGuild(42, [], owner=None)
    member = FakeMember(guild, [], FakePermissions())
    db = FakeDB(security={"guild_id": 42, "protected_role_ids": [], "initialized": False})
    mgr = FeaturePermissionManager(db)  # type: ignore[arg-type]

    allowed = await mgr.check(member, FeatureKey.MOD_BAN, base_check=lambda m: True)
    assert allowed is False

    db.security["initialized"] = True
    allowed_after = await mgr.check(member, FeatureKey.MOD_BAN, base_check=lambda m: True)
    assert allowed_after is True
