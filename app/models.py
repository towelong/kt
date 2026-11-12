"""领域模型与请求/响应模型。所有字段长度都显式给出，MySQL 才能建表。

每个字段的 `description` 就是它的业务含义，改字段时顺手更新，别让注释和实现漂移。
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

# 约束/索引名必须确定，否则 SQLite 上改表（batch 模式）和 MySQL 上的 drop 都会猜名字。
# 名字也控制在 MySQL 的 64 字符标识符上限内。
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """带时区的 UTC；SQLModel 的 UTCDateTime 负责在 MySQL 上落成无偏移的 UTC 值。"""
    return datetime.now(UTC)


class TimestampMixin(SQLModel):
    created_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
        description="记录创建时间（UTC）",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
        description="记录最后更新时间（UTC），每次写入显式刷新",
    )


# --------------------------------------------------------------------------- 卡库存


class CardPlan(StrEnum):
    """套餐：单价 × 3 次即为首充金额。"""

    PLUS = "plus"
    PRO100 = "pro100"
    PRO200 = "pro200"
    PRO500 = "pro500"


class Card(TimestampMixin, table=True):
    """VCC 卡。

    三个敏感字段（card_no / expiry / cvv）来自开卡 Webhook 解密，只允许出现在本表，
    日志、接口和回调里一律用 card_id 或脱敏卡号代替。
    """

    __tablename__ = "card"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    plan: str = Field(
        max_length=16,
        index=True,
        description="所属套餐：plus / pro100 / pro200 / pro500",
    )
    request_no: str = Field(
        max_length=64,
        unique=True,
        description="开卡请求号，含套餐名；webhook 找不到本地占位行时靠它回填",
    )
    card_id: str | None = Field(
        default=None,
        max_length=64,
        unique=True,
        description="平台业务卡 ID（VC 开头），交易 webhook 用它关联",
    )
    batch_no: str | None = Field(default=None, max_length=64, description="平台开卡批次号")
    card_no: str | None = Field(
        default=None,
        max_length=32,
        description="完整卡号，敏感字段，只允许存本表",
    )
    expiry: str | None = Field(
        default=None,
        max_length=16,
        description="有效期字符串，格式 月/年，例如 11/28",
    )
    cvv: str | None = Field(default=None, max_length=8, description="CVV，敏感字段")
    balance: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=18,
        decimal_places=2,
        description="本地记账余额（USD）：开卡写入首充，清算扣、退款加；只做记录",
    )
    applied_at: datetime | None = Field(
        default=None,
        description="开卡成功时间（取平台 webhook 的 completeTime/createdAt，按 UTC 存）",
    )
    used_count: int = Field(
        default=0,
        ge=0,
        description="已使用次数：授权成功 +1、失败/退款 -1，最低 0；到 3 停用",
    )
    enabled: bool = Field(
        default=False,
        index=True,
        description="是否可用于交易；三要素齐全且次数没用完才为 true",
    )
    apply_status: str = Field(
        default="PENDING",
        max_length=16,
        description="开卡状态：PENDING / SUCCESS / FAILED",
    )
    initial_funding_status: str | None = Field(
        default=None,
        max_length=16,
        description="首充状态：PENDING / SUCCESS / FAILED / SKIPPED，与开卡状态独立",
    )
    reserved_until: datetime | None = Field(
        default=None,
        description="订阅下单锁卡到期的 UTC 时间；空闲或已过期才会被任务重新抢",
    )
    reserved_task_id: str | None = Field(
        default=None,
        max_length=64,
        description="当前持锁任务的 request_id，释放时按它做条件更新",
    )


class CardTransaction(TimestampMixin, table=True):
    """卡片交易流水：同一 transaction_id 会按阶段多次推送，最终状态覆盖在同一行。"""

    __tablename__ = "card_transaction"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    transaction_id: str = Field(
        max_length=64,
        unique=True,
        description="平台交易 ID；退款/撤销是独立交易，用 related_transaction_id 关联原单",
    )
    card_id: str = Field(
        max_length=64,
        index=True,
        description="平台业务卡 ID，对应 card.card_id",
    )
    original_amount: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=18,
        decimal_places=2,
        description="原始授权/交易金额（原币种）",
    )
    original_currency: str | None = Field(
        default=None,
        max_length=8,
        description="原始金额币种，平台字段 originalCurrency，例如 PHP、USD",
    )
    actual_amount: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=18,
        decimal_places=2,
        description="本阶段实际金额，已含手续费；清算扣款、退款加回都用它",
    )
    actual_currency: str | None = Field(
        default=None,
        max_length=8,
        description="实际金额币种，平台字段 actualCurrency",
    )
    merchant_name: str | None = Field(default=None, max_length=255, description="商户名称")
    transaction_time: datetime | None = Field(
        default=None,
        description="平台交易时间（不带时区，按 UTC 存）",
    )
    event_status: str = Field(
        max_length=32,
        index=True,
        description=(
            "交易状态：PROCESSING / AUTH_SUCCESS / AUTH_FAILED / SETTLED / "
            "REFUND_SUCCESS / REVERSE_SUCCESS / CORRECTION"
        ),
    )
    related_transaction_id: str | None = Field(
        default=None,
        max_length=64,
        description="关联的原交易 ID，退款/撤销/修正会带",
    )
    event_id: str | None = Field(
        default=None,
        max_length=128,
        description="最近一次处理该交易的 webhook eventId，排查用",
    )
    remark: str | None = Field(
        default=None,
        max_length=255,
        description="流水备注：授权失败等业务文案，优先取平台返回，没有则本地生成",
    )
    use_counted: bool = Field(
        default=False,
        description="本交易当前是否占用使用次数；按状态目标值做差值，重复推送不重复计",
    )
    balance_applied: bool = Field(
        default=False,
        description="本交易当前是否已影响卡余额；阶段推进时只应用差额",
    )


class JobLease(SQLModel, table=True):
    """多副本定时任务的数据库租约，跨 SQLite/MySQL 都靠条件 UPDATE 抢锁。"""

    __tablename__ = "job_lease"

    name: str = Field(max_length=64, primary_key=True, description="任务名，一个任务一行")
    locked_until: datetime = Field(
        default_factory=utcnow,
        description="租约到期时间；小于当前时间表示没人持锁",
    )
    updated_at: datetime = Field(default_factory=utcnow, description="最后续锁/释放时间（UTC）")


class Task(TimestampMixin, table=True):
    """数据库任务队列：开卡和订阅下单共用，worker 用条件 UPDATE 抢占。

    session 只以 AES-GCM 密文存在 payload_ciphertext 里，订单提交后立即清空。
    """

    __tablename__ = "task"

    id: int | None = Field(default=None, primary_key=True, description="自增主键，也是对外 task_id")
    kind: str = Field(
        max_length=32,
        index=True,
        description="任务类型：open_card（补卡）/ open_subscription（订阅下单）",
    )
    plan: str | None = Field(
        default=None,
        max_length=16,
        index=True,
        description="关联套餐：plus / pro100 / pro200 / pro500",
    )
    request_id: str = Field(
        max_length=96,
        unique=True,
        description="BYOC 订单请求号，也是卡锁持有者标识；重试必须沿用",
    )
    quote_id: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="最近一次报价的 quote_id，下单时原样带上（不限制长度）",
    )
    authorized_amount_minor: int | None = Field(
        default=None,
        description="报价授权金额，单位是分（例如 2000 = 20.00 USD）",
    )
    display_amount: str | None = Field(
        default=None,
        max_length=64,
        description="展示用金额字符串，例如 USD $20.00，回调对账用",
    )
    payload_ciphertext: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="AES-256-GCM 密文，base64url；订单提交后清空",
    )
    status: str = Field(
        default="pending",
        max_length=24,
        index=True,
        description=(
            "任务状态：pending / waiting_card / waiting_quote / processing / "
            "waiting_action / succeeded / failed / unknown"
        ),
    )
    attempts: int = Field(default=0, ge=0, description="已认领执行次数，排查重试风暴用")
    next_run_at: datetime = Field(
        default_factory=utcnow,
        index=True,
        description="下次允许执行时间（UTC），退避和轮询都靠它",
    )
    claimed_until: datetime | None = Field(
        default=None,
        description="本次认领的租约到期时间；过期后可被其它 worker 重新认领",
    )
    card_id: int | None = Field(
        default=None,
        index=True,
        description="本次任务使用的本地卡主键（card.id）；释放卡锁不清空，便于追溯",
    )
    api_key_id: int | None = Field(
        default=None,
        index=True,
        description="提交该任务的 API Key ID；查询时按它做归属校验",
    )
    byoc_task_id: str | None = Field(
        default=None,
        max_length=64,
        description="BYOC 订单 task_id，拿到后用它轮询状态、找回原单",
    )
    payment_status: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "BYOC 订单支付状态：queued / processing / requires_action / "
            "manual_review / completed / failed / cancelled"
        ),
    )
    settlement: str | None = Field(
        default=None,
        max_length=16,
        description="BYOC 结算状态：NONE / NO_CHARGE / UNKNOWN / PAID",
    )
    last_error: str | None = Field(
        default=None,
        max_length=500,
        description="最近一次失败/未知原因，回调里会带上",
    )
    callback_url: str | None = Field(
        default=None,
        max_length=500,
        description="结果回调地址（http/https），为空则不回调",
    )
    callback_attempts: int = Field(default=0, ge=0, description="回调已尝试次数")
    callback_next_at: datetime | None = Field(
        default=None,
        index=True,
        description="下次回调时间（UTC）；为空表示不用再回调",
    )
    callback_delivered: bool = Field(
        default=False,
        description="回调是否已被接收（2xx）；成功后不再重试",
    )


class ApiKey(TimestampMixin, table=True):
    """对外接口密钥：只存哈希和前缀，完整 key 仅在创建时展示一次。"""

    __tablename__ = "api_key"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    name: str = Field(
        max_length=64,
        index=True,
        description="调用方名称，用于识别、吊销和审计",
    )
    key_prefix: str = Field(
        max_length=16,
        unique=True,
        description="完整 key 的前缀（含 kt_），查库和日志展示用",
    )
    key_hash: str = Field(
        max_length=64,
        unique=True,
        description="sha256(完整 key) 的 hex；不存明文，比对用常数时间",
    )
    enabled: bool = Field(default=True, index=True, description="是否启用；吊销后置 false")
    expires_at: datetime | None = Field(default=None, description="过期时间（UTC），为空表示长期")
    last_used_at: datetime | None = Field(default=None, description="最后一次通过鉴权的时间（UTC）")


class CardOperation(TimestampMixin, table=True):
    """卡资金/状态操作事件流水：充值、转出、冻结、销卡等。

    以平台 eventId 做唯一键，保证同一条 webhook 重投不会重复加/减余额。
    """

    __tablename__ = "card_operation"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    event_id: str = Field(
        max_length=128,
        unique=True,
        description="平台 webhook eventId；唯一约束用于幂等，重复投递直接跳过",
    )
    card_id: str = Field(max_length=64, index=True, description="平台业务卡 ID")
    operation_type: str = Field(
        max_length=32,
        description=(
            "操作类型：RECHARGE / WITHDRAW / FREEZE / UNFREEZE / "
            "CANCEL / LIMIT_CHANGE / REMARK_UPDATE"
        ),
    )
    operation_status: str = Field(max_length=16, description="操作状态，例如 SUCCESS / FAILED")
    amount: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=18,
        decimal_places=2,
        description="资金操作金额；非资金操作是 0",
    )
    currency: str | None = Field(default=None, max_length=8, description="金额币种")
    request_no: str | None = Field(default=None, max_length=64, description="本次操作请求号")
    funding_purpose: str | None = Field(
        default=None,
        max_length=32,
        description="资金用途，首充为 INITIAL_FUNDING",
    )
    card_issue_request_no: str | None = Field(
        default=None,
        max_length=64,
        description="首充事件里关联的原开卡请求号",
    )
    operation_time: datetime | None = Field(default=None, description="平台操作时间（按 UTC 存）")
