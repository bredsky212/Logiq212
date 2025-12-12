"""
Tests for feature permission manager and denial logging
"""

import pytest

from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.models import FeatureKey


class DummyPerms:
    def __init__(self, admin=False, manage_messages=False, moderate_members=False, ban_members=False, kick_members=False, manage_channels=False, manage_nicknames=False):
        self.administrator = admin
        self.manage_messages = manage_messages
        self.moderate_members = moderate_members
        self.ban_members = ban_members
        self.kick_members = kick_members
        self.manage_channels = manage_channels
        self.manage_nicknames = manage_nicknames


class DummyRole:
    def __init__(self, role_id):
        self.id = role_id


class DummyGuild:
    def __init__(self, owner):
        self.owner = owner


class DummyMember:
    def __init__(self, guild, roles, perms):
        self.guild = guild
        self.roles = roles
        self.guild_permissions = perms

    def __eq__(self, other):
        return isinstance(other, DummyMember) and self is other


class FakeDB:
    def __init__(self):
        self.docs = {}

    async def get_feature_permission(self, guild_id, feature_key):
        return self.docs.get((guild_id, feature_key))

    async def add_doc(self, guild_id, feature_key, allowed, denied):
        self.docs[(guild_id, feature_key)] = {
            "guild_id": guild_id,
            "feature_key": feature_key,
            "allowed_roles": allowed,
            "denied_roles": denied,
        }


@pytest.mark.asyncio
async def test_admin_bypass_and_base_check():
    db = FakeDB()
    mgr = FeaturePermissionManager(db)

    guild = DummyGuild(owner=None)
    admin = DummyMember(guild, [], DummyPerms(admin=True))
    non_admin = DummyMember(guild, [], DummyPerms())

    allowed_admin = await mgr.check(admin, FeatureKey.MOD_WARN, base_check=lambda m: False)
    assert allowed_admin  # admin bypass

    allowed_non_admin = await mgr.check(non_admin, FeatureKey.MOD_WARN, base_check=lambda m: False)
    assert not allowed_non_admin  # base check fails


@pytest.mark.asyncio
async def test_allowed_and_denied_roles():
    db = FakeDB()
    mgr = FeaturePermissionManager(db)
    guild = DummyGuild(owner=None)
    role_allowed = DummyRole(1)
    role_denied = DummyRole(2)
    member = DummyMember(guild, [role_allowed], DummyPerms(moderate_members=True))
    member_denied = DummyMember(guild, [role_denied], DummyPerms(moderate_members=True))

    await db.add_doc(guild_id=123, feature_key=FeatureKey.MOD_TIMEOUT.value, allowed=[1], denied=[])

    assert await mgr.check(member, FeatureKey.MOD_TIMEOUT, base_check=lambda m: True)

    await db.add_doc(guild_id=123, feature_key=FeatureKey.MOD_TIMEOUT.value, allowed=[1], denied=[2])
    assert not await mgr.check(member_denied, FeatureKey.MOD_TIMEOUT, base_check=lambda m: True)


def test_denial_logger_throttles():
    dl = DenialLogger(window_seconds=1)
    assert dl.should_log(1, 2, "cmd", "feature")
    assert not dl.should_log(1, 2, "cmd", "feature")
