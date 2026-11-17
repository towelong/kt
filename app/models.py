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
        description=(
            "本地记账余额（USD）：开卡为 0，首充/充值成功加；卡交易首次扣款状态扣、失败/退款退"
        ),
    )
    applied_at: datetime | None = Field(
        default=None,
        description="开卡成功时间（取平台 webhook 的 completeTime/createdAt，按 UTC 存）",
    )
    used_count: int = Field(
        default=0,
        ge=0,
        description="已使用次数：卡交易首次扣款 +1、失败/退款 -1，最低 0；到 3 停用",
    )
    enabled: bool = Field(
        default=False,
        index=True,
        description="是否可用于交易；首充成功、三要素齐全且次数没用完才为 true",
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
    applied_amount: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=18,
        decimal_places=2,
        description=(
            "本交易已实际作用到卡余额的金额（扣款/回款都记正数）；"
            "AUTH_FAILED 退回时按它加回，避免用被后续状态覆盖的 actual_amount"
        ),
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
        description="本交易是否已应用使用次数；PROCESSING/AUTH_SUCCESS/SETTLED 谁先到谁计一次",
    )
    balance_applied: bool = Field(
        default=False,
        description="本交易是否已应用余额；PROCESSING/AUTH_SUCCESS/SETTLED 谁先到谁扣一次",
    )


class Task(TimestampMixin, table=True):
    """任务状态机：开卡和订阅下单共用，所有状态、结果和重试计数都在这一行。

    调度不在表里：改状态时用 `crud.save_task_with_outbox` 往 `task_outbox` 写一条
    “什么时候再跑”的意图，worker 真正开始执行时才 ack 掉；`attempts` 既是重试计数，
    也是消息令牌（旧令牌的消息会被丢弃）。

    session 只以 AES-GCM 密文存在 payload_ciphertext 里，建单成功或进入终态时清空；
    提交失败（取报价/校验/建单）保留密文，按 submit_attempts 退避重试。
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
            "任务状态：pending / processing / waiting_card / waiting_quote / "
            "waiting_action / succeeded / failed / unknown；"
            "waiting_card 只属于订阅任务等卡，补卡任务用 processing 等 webhook/重试"
        ),
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description="已排定的执行次数，同时是消息令牌：旧令牌的消息直接丢弃",
    )
    submit_attempts: int = Field(
        default=0,
        ge=0,
        description=(
            "失败重试次数：BYOC 取报价/校验/建单失败、补卡未完成都累加；达到各自上限才置为失败"
        ),
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
    callback_delivered: bool = Field(
        default=False,
        description="回调是否已被接收（2xx）；成功后不再重试",
    )


class TaskOutbox(TimestampMixin, table=True):
    """待投递给 arq 的调度意图，和业务状态在同一个事务里写。

    publisher 每几秒扫一次到期行、投递成 arq 的延迟消息；worker 真正开始执行时
    才删掉对应行（ack）。没 ack 的行会被反复重投，靠稳定的 `_job_id` 在 arq 侧去重，
    所以 Redis 丢消息也能自愈。
    """

    __tablename__ = "task_outbox"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    task_id: int = Field(
        foreign_key="task.id",
        index=True,
        description="关联的任务（task.id）",
    )
    kind: str = Field(
        max_length=16,
        description="投递类型：run（执行任务）/ callback（投递回调）",
    )
    run_at: datetime = Field(index=True, description="最早可以投递的时间（UTC）")
    token: int | None = Field(
        default=None,
        description="run 消息的令牌，等于调度时的 task.attempts；callback 为空",
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
