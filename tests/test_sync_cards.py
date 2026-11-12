"""平台卡同步脚本：导入、幂等更新、停用状态、dry-run。"""

from decimal import Decimal

from app import crud
from app.tools.sync_cards import parse_balance, plan_from_remark, sync_cards
from tests.factories import make_card

CARD_ID = "VC_SYNC_1"


def platform_card(**overrides):
    item = {
        "cardId": CARD_ID,
        "cardNoMask": "486532****1298",
        "cardType": "PREPAID",
        "cardStatus": "ACTIVE",
        "balance": "25.50",
        "batchNo": "BATCH_SYNC",
        "remark": "stock-plus",
        "createTime": "2026-06-25 14:00:00",
    }
    item.update(overrides)
    return item


class FakeClient:
    def __init__(self, items):
        self.items = items

    async def query_cards(self, *, page_num=1, page_size=20):
        if page_num > 1:
            return {"total": len(self.items), "list": []}
        return {"total": len(self.items), "list": self.items}


async def test_imports_platform_card_without_secrets(db_session):
    report = await sync_cards(db_session, FakeClient([platform_card()]))

    assert report.imported == 1
    card = crud.get_card_by_platform_id(db_session, CARD_ID)
    assert card is not None
    assert card.plan == "plus"
    assert card.balance == Decimal("25.50")
    assert card.batch_no == "BATCH_SYNC"
    assert card.apply_status == "SUCCESS"
    assert card.applied_at is not None
    # 平台列表只有脱敏卡号，导入的卡不能参与订阅选卡
    assert card.card_no is None
    assert card.enabled is False
    assert card.request_no == f"import_{CARD_ID}"


async def test_sync_is_idempotent(db_session):
    await sync_cards(db_session, FakeClient([platform_card()]))
    report = await sync_cards(
        db_session,
        FakeClient([platform_card(balance="30.00")]),
    )

    assert report.imported == 0
    assert report.updated == 1
    card = crud.get_card_by_platform_id(db_session, CARD_ID)
    assert card is not None
    assert card.balance == Decimal("30.00")
    assert len(crud.list_api_keys(db_session)) == 0


async def test_cancelled_status_disables_existing_card(db_session):
    card = make_card(db_session, plan="plus", card_id=CARD_ID)
    assert card.enabled is True

    await sync_cards(db_session, FakeClient([platform_card(cardStatus="CANCELLED")]))

    db_session.refresh(card)
    assert card.enabled is False


async def test_dry_run_does_not_write(db_session):
    report = await sync_cards(db_session, FakeClient([platform_card()]), dry_run=True)

    assert report.imported == 1
    assert crud.get_card_by_platform_id(db_session, CARD_ID) is None


def test_plan_from_remark():
    assert plan_from_remark("stock-plus") == "plus"
    assert plan_from_remark("PRO100 backup") == "pro100"
    assert plan_from_remark("") == "plus"
    assert plan_from_remark(None) == "plus"


def test_parse_balance():
    assert parse_balance("25.5") == Decimal("25.50")
    assert parse_balance(None) == Decimal("0.00")
    assert parse_balance("bad") == Decimal("0.00")
