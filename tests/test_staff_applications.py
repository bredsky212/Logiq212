"""
Tests for staff application DB helpers
"""

import pytest
from datetime import datetime

from database.db_manager import DatabaseManager
from database.models import (
    StaffApplicationTemplate,
    StaffApplicationField,
    StaffApplication,
    StaffApplicationAnswer,
)


@pytest.fixture
async def db_manager():
    db = DatabaseManager("mongodb://localhost:27017", "Logiq_staff_test", pool_size=5)
    await db.connect()
    # Clean collections
    await db.staff_application_templates.delete_many({})
    await db.staff_applications.delete_many({})
    await db.staff_app_config.delete_many({})
    yield db
    await db.disconnect()


@pytest.mark.asyncio
async def test_staff_template_crud(db_manager: DatabaseManager):
    fields = [
        StaffApplicationField(key="motivation", label="Motivation", style="paragraph"),
        StaffApplicationField(key="experience", label="Experience", style="paragraph"),
    ]
    template = StaffApplicationTemplate(
        guild_id=123,
        template_id="",
        name="Design Team",
        description="Apply here",
        team_role_id=None,
        apply_channel_id=111,
        review_channel_id=222,
        fields=fields,
        created_by_id=999,
    )

    template_id = await db_manager.create_staff_template(template.to_dict())
    assert template_id

    stored = await db_manager.get_staff_template(123, template_id)
    assert stored is not None
    assert stored["name"] == "Design Team"

    templates = await db_manager.list_staff_templates(123)
    assert len(templates) == 1

    updated = await db_manager.set_staff_template_active(123, template_id, False)
    assert updated

    stored = await db_manager.get_staff_template(123, template_id)
    assert stored["is_active"] is False


@pytest.mark.asyncio
async def test_staff_application_crud(db_manager: DatabaseManager):
    application = StaffApplication(
        guild_id=321,
        template_id="tpl1",
        application_id="",
        applicant_id=555,
        team_role_id=None,
        answers=[StaffApplicationAnswer(key="motivation", label="Motivation", value="I love design")],
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        review_channel_id=222,
        review_message_id=0,
    )

    app_id = await db_manager.create_staff_application(application.to_dict())
    assert app_id

    stored = await db_manager.get_staff_application(321, app_id)
    assert stored is not None
    assert stored["status"] == "pending"

    await db_manager.update_staff_application(321, app_id, {"status": "accepted"})
    updated = await db_manager.get_staff_application(321, app_id)
    assert updated["status"] == "accepted"

    queried = await db_manager.query_staff_applications(321, status="accepted")
    assert len(queried) == 1
