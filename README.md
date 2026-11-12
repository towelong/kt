# kt

FastAPI + SQLModel 服务骨架，同一套代码跑 SQLite 与 MySQL，迁移用 Alembic。

工程约定（分层、双库限制、迁移流程、质量门）见 [AGENTS.md](AGENTS.md)。

## 目录

```
app/
├── __init__.py
├── main.py          # 应用入口、lifespan（启动时升级数据库）、路由挂载
├── config.py        # 配置，全部来自环境变量 / .env
├── db.py            # engine、会话、SQLite 特有参数
├── dependencies.py  # SessionDep、分页依赖
├── models.py        # 数据表模型 + 请求/响应模型 + 命名约定
├── crud.py          # 数据访问（唯一碰 Session 的层）
├── migrations.py    # 以代码方式调用 Alembic
├── integrations/    # 外部服务对接
│   └── ailink/      # AiLink VCC 客户端、事件模型、Webhook 验签与分发
├── services/        # 业务编排：卡事件落库、卡库存策略
├── jobs/            # 定时任务：卡库存扫描
├── routers/
│   ├── subscriptions.py  # POST /api/subscriptions：订阅任务投递
│   └── webhooks.py       # POST /webhooks/ailink：VCC 事件统一入口
└── internal/
    └── admin.py     # 运维接口：库信息、迁移状态、手工迁移
migrations/
├── env.py           # 连接串与元数据都取自应用
├── script.py.mako
└── versions/        # 迁移脚本，入库
alembic.ini
tests/
├── test_smoke.py
└── test_migrations.py
```

调用方向固定为 `routers → crud → models/db`：路由只管 HTTP 与状态码，`crud` 只管数据，`internal` 放不属于某个资源的逻辑。

## 跑起来

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head     # 建表/升级，等价于启动时自动跑的那一步
uv run uvicorn app.main:app --reload
```

- 文档：http://127.0.0.1:8000/docs
- 健康检查：`GET /health`
- 当前库自检：`GET /internal/admin/db-info`
- 迁移是否跑齐：`GET /internal/admin/migration-state`（`current` 应等于 `head`）

启动时默认**不会**自动迁移（`AUTO_MIGRATE` 默认 `false`）。开发环境用 `.env` 里的 `AUTO_MIGRATE=true` 或手动 `alembic upgrade head`；生产/多副本一律在发布流程里单独跑迁移，避免并发。

## AiLink VCC 对接

配置 `AILINK_API_KEY` / `AILINK_API_SECRET` 后可以调开放 API；Webhook 端点还需要 `AILINK_WEBHOOK_SECRET`。
`AILINK_WEBHOOK_SECRET` 同时用于验签和卡三要素解密，不会出现在日志里。

```python
from decimal import Decimal

from app.integrations.ailink import AiLinkClient, new_request_no
from app.integrations.ailink.models import CardApplyRequest

async with AiLinkClient.from_settings() as client:
    bins = await client.list_card_bins()          # 开卡配额看 bins.quota
    balance = await client.get_account_balance()  # USD / USDT
    await client.convert_balance("USDT", "USD", Decimal("100.00"))
    await client.apply_cards(
        CardApplyRequest(
            request_no=new_request_no("kaika_"),
            card_type="PREPAID",
            card_bin_id=bins.bins[0].bin_id,
            recharge_amount=Decimal("100.00"),
        )
    )
```

- 同步响应只代表受理：申请卡返回 `SUCCESS` 时 `initialFundingStatus` 仍可能是 `PENDING`，首充结果以 Webhook 为准。
- 500 / 超时都是“结果未知”，保留 `requestId` 和原 `requestNo` 找平台核对，不要换号重提。

### Webhook

VCC 后台填 `https://<域名>/webhooks/ailink`，所有事件都推这里，按 `eventType` 分发：

```
POST /webhooks/ailink  →  验签 → parse_event → dispatcher → 纯文本 ok
```

- 成功必须返回 `2xx` + 正文 `ok`；处理器抛异常返回 500，平台按 1m/5m/15m/30m/1h/3h 重试。
- 幂等键是 `eventId`，默认用进程内有界缓存；多副本部署请把 `EventStore` 换成共享存储。
- 新增事件类型：在 `app/integrations/ailink/handlers/` 加一个 `@dispatcher.register` 的处理器即可，路由和分发器不用改。
- 处理器拿到的 `event.data` 已类型化；卡号/有效期/CVV 只在 `event.data.decrypt_cards(secret)` 里显式解密，`repr` 不输出明文。

```python
from app.integrations.ailink.dispatch import EventContext, EventHandler
from app.integrations.ailink.events import CardTransactionEvent
from app.integrations.ailink.handlers import dispatcher

@dispatcher.register
class SettledToLedger(EventHandler[CardTransactionEvent]):
    event_model = CardTransactionEvent
    event_types = ("CARD_TRANSACTION.SETTLED",)

    async def process(self, event: CardTransactionEvent, ctx: EventContext):
        ...
```

### 卡库存与计数

`CARD_STOCK_ENABLED=true` 后，lifespan 里会起一个进程内任务（`app/jobs/card_stock.py`），每 `CARD_STOCK_INTERVAL_SECONDS` 扫一轮：

| 套餐 | 单价 | 首充（3 次） |
| --- | --- | --- |
| plus | 16 | 48 |
| pro100 | 90 | 270 |
| pro200 | 150 | 450 |
| pro500 | 470 | 1410 |

- 可用卡 = `enabled`、`used_count < 3`，且余额不低于该套餐单价（plus 16 / pro100 90 / pro200 150 / pro500 470），不够就补新卡。
- 缺卡时先查账户余额：USD 够直接开；不够按 `CARD_STOCK_USDT_USD_RATE`（默认 0.997）把缺口折成 USDT 兑换，兑换后重新查余额；两边都不够打 `error` 日志并跳过本轮。
- 卡 BIN 只选 `issuingCountry=US` 且 BIN 以 `5378` 开头的配置；配额用尽或没有匹配 BIN 时打 error。
- 开卡前先写 `card` 占位行（`request_no` 唯一，含套餐名），超时后下一轮不会重复提交；三要素等开卡 Webhook 补齐后才 `enabled`。
- 交易 Webhook 按 `transaction_id` 落 `card_transaction`：`PROCESSING` 先 +1 并按 `actual_amount` 扣余额；`AUTH_SUCCESS` / `SETTLED` 只推进状态、次数和余额都不再变；`AUTH_FAILED` 次数 -1 并退回已扣金额；退款/撤销（独立交易）-1 并加回；`CORRECTION` 照常扣减修正金额、不计次数。同一交易多阶段按差值计算，重试和无序推送都不会重复计。
- 次数到 3 自动停用，退款把次数降回来会重新启用。
- 卡操作事件落 `card_operation`：充值加余额、转出减余额，`eventId` 唯一约束保证重投不重复加钱；首充成功/失败同步更新 `initial_funding_status`。
- 多副本部署靠 `job_lease` 抢锁，同一时间只有一个实例真正扫描。

平台已有卡可以一次性同步进本地 `card` 表（按 `cardId` 幂等，不会重复插入）：

```bash
uv run python -m app.tools.sync_cards --dry-run   # 先看会导入/更新哪些
uv run python -m app.tools.sync_cards
```

列表只返回脱敏卡号，所以导入的卡不写 `card_no`/`expiry`/`cvv`、`enabled=false`，只用来对账余额和状态，不会被订阅选卡拿去交易；已有卡只更新余额、批次和停用状态。套餐从备注里推断，认不出就是 `unknown`。

想直接看表效果，可以往当前 `DATABASE_URL`（默认 `app.db`）灌一份演示数据：

```bash
uv run python -m tests.seed_app_db
```

会写入 4 张卡（plus/pro100/pro200/pro500）和 7 条交易流水（授权、清算、失败、退款、处理中），按 `request_no` / `transaction_id` 去重，重复执行不会翻倍，也不会删已有数据。单元测试本身仍跑在临时库，不碰 `app.db`。

## 订阅开通（BYOC）

关掉 `SUBSCRIPTION_WORKER_ENABLED` 时接口只入库不执行；打开前必须配 `BYOC_API_KEY` 和 `TASK_PAYLOAD_KEY`。

```
POST /api/subscriptions
{"plan": "plus", "session": {...完整 Session...}, "callback_url": "https://merchant/cb"}
→ 202 {"task_id": 1, "request_id": "order-2026...", "status": "pending"}

GET /api/subscriptions/{task_id}   # 兜底查询，不返回 session
```

### 鉴权

两个接口都要带 `X-API-Key`；key 只存 sha256 和前 11 位前缀，完整值只在创建时打印一次。

```bash
uv run python -m app.tools.api_keys create --name partner-a --expires-in-days 365
uv run python -m app.tools.api_keys list
uv run python -m app.tools.api_keys revoke --prefix kt_a1b2c3d4
```

```bash
curl -X POST http://127.0.0.1:8000/api/subscriptions \
  -H "X-API-Key: kt_xxx" -H "Content-Type: application/json" \
  -d '{"plan":"plus","session":{...},"callback_url":"https://merchant/cb"}'
```

- 缺 key / 错 key → 401；已吊销或过期 → 403。
- `GET` 只能查自己 key 提交的任务，别的 key 一律 404，不暴露任务是否存在。
- 卡库存、补卡任务和 VCC Webhook 不用这套鉴权。

worker（`app/jobs/task_worker.py`）执行顺序：

1. 抢任务（条件 UPDATE，多副本只跑一次）。
2. 按内部套餐选卡并锁定：`enabled`、`used_count < 3`、三要素齐全、未过期、未被别的任务锁住，且卡余额不低于套餐单价；余额不够就投递补卡任务。
3. 没有卡 → `waiting_card` 并投递 `open_card` 任务，补卡后再试。
4. `GET /quotes/{byoc_plan_type}?billing_country=...` 取 `quote_id`，过期就重新报价；金额和币种原样保存供回调/对账，不参与校验。
5. 组单 → `POST /orders/validate` → `POST /orders`，同一个 `request_id`、同一份 JSON。
6. 只认 BYOC 的订单状态：`completed + PAID` 成功，`failed/cancelled` 失败，`requires_action` 等 3DS；超时按原 `request_id` 找回，不换号重提。
7. validate 超时/网络异常释放卡锁、任务 30 秒后重试；建单后结果未知保留卡锁等 `recover` 核对，成功/失败释放；终态清空 session 密文并回调。

套餐映射（BYOC 的名字和内部不一样）：

| 内部 | BYOC `plan_type` | billing_country |
| --- | --- | --- |
| plus | plus | ph |
| pro100 | pro5 | eg |
| pro200 | pro20 | ph |
| pro500 | promax | eg |

回调 body 拍平后按 key 升序拼 `k=v&...`，末尾拼 `BYOC_CALLBACK_SECRET` 做 MD5 hex，放在 `Kt-Secret` 头里；失败按 1m/5m/15m/1h 重试。session 和卡三要素只存在于加密 payload 和内存里，接口、日志、回调都不出现。

## 新增一条迁移

```bash
# 1. 改 app/models.py
# 2. 生成（需要连得上当前 DATABASE_URL）
uv run alembic revision --autogenerate -m "add card remark"
uv run ruff format migrations/versions
# 3. 一定要打开 migrations/versions/xxx.py 检查：autogenerate 不认重命名，
#    删列/改类型要自己补数据迁移
uv run alembic upgrade head
```

回滚一条：`uv run alembic downgrade -1`；清空重来：`uv run alembic downgrade base`。

`tests/test_migrations.py` 里的 `command.check()` 会在模型改了却没生成迁移时失败，也可以手动 `uv run alembic check`。

## 切到 MySQL

```bash
DATABASE_URL='mysql+pymysql://用户名:<CRED>@127.0.0.1:3306/kt?charset=utf8mb4' \
  uv run alembic upgrade head
```

库要先建好（`CREATE DATABASE kt CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;`）。迁移脚本由 SQLite 生成也能直接跑在 MySQL 上，因为：

- `migrations/env.py` 用 `render_item` 把 SQLModel 的 `AutoString` / `UTCDateTime` 落成标准 `sa.String(length=...)` / `sa.DateTime(timezone=True)`，脚本不依赖 sqlmodel 内部类型。
- 所有约束和索引都有确定名字（`app/models.py` 里的 `naming_convention`），SQLite batch 模式重建表和 MySQL `DROP INDEX` 都靠它。
- SQLite 上 `render_as_batch=True`，MySQL 上关闭。

不上服务器也能看 MySQL 会执行什么：

```bash
DATABASE_URL='mysql+pymysql://用户:<CRED>@127.0.0.1/kt' uv run alembic upgrade head --sql
```

## 双库约定

- 差异只在 `app/db.py`：SQLite 放 `check_same_thread=False` 并开 `PRAGMA foreign_keys=ON`；MySQL 用 `pool_pre_ping` + `pool_recycle`。
- 字符串字段一律给 `max_length`（MySQL 要求长度）；不用 `JSON` 列，结构体存 `Text` 自己序列化。
- 时间统一用带时区的 UTC（`datetime.now(UTC)`），SQLModel 的 `UTCDateTime` 会在 MySQL 上落成无偏移的 UTC。
- 分页只用 `offset/limit`，不写 `ILIKE`、不用 `INSERT OR REPLACE`、不依赖大小写不敏感比较。
- 唯一冲突捕获 `IntegrityError` 转 409，不判断数据库错误码。
- 事务边界在 `crud` 里，一个函数一次 `commit`；批量写入失败要 `rollback`。

## 测试

```bash
uv run pytest      # 每次用临时 SQLite，不碰 app.db
uv run ruff check .
```

## 接下来

- `internal/admin.py` 还没有鉴权，上线前必须加管理员校验。
- 多副本部署时把限流配额按副本数拆分，或换数据库跨副本计数。
- 卡余额目前按 webhook 记账，后续可以加定时对账。

## 容器与 CI

`Dockerfile` 用 `uv sync --frozen --no-dev` 构建，运行镜像基于 `python:3.12-slim`，非 root，暴露 8000，带 `/health` 健康检查。

`.github/workflows/docker.yml` 的行为：

- PR 只跑质量门（ruff + pytest），不推镜像。
- 推 `main` 或 `v*` tag 时构建并推送到私有 registry；镜像只打短 sha tag（7 位），例如 `registry.example.com/team/kt:8866e92`。
- 构建缓存用 GitHub Actions cache，不依赖 registry。

仓库 Secrets 需要配：

| Secret | 说明 |
| --- | --- |
| `REGISTRY_HOST` | 私有 registry 域名，例如 `registry.example.com` |
| `REGISTRY_NAMESPACE` | 命名空间/项目，例如 `team` |
| `REGISTRY_USERNAME` | registry 用户名 |
| `REGISTRY_PASSWORD` | registry 密码或 token |

镜像地址是 `REGISTRY_HOST/REGISTRY_NAMESPACE/kt`。生产默认不自动迁移（`AUTO_MIGRATE=false`），发布流程里单独跑 `alembic upgrade head`。

## 命令速查

### 开发

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
uv run python -c "import sys; print(sys.executable)"   # 确认解释器
```

### 迁移

```bash
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "add card remark"
uv run ruff format migrations/versions
uv run alembic downgrade -1
uv run alembic downgrade base
uv run alembic check
DATABASE_URL='mysql+pymysql://用户:密码@主机:3306/kt?charset=utf8mb4' uv run alembic upgrade head --sql
```

### 质量门

```bash
uv run ruff check .
uv run ruff format --check app migrations tests
uv run pytest
```

### 容器

```bash
docker build -t registry.example.com/team/kt:local .
docker run --rm -p 8000:8000 --env-file .env registry.example.com/team/kt:local
```

### 演示数据 / 卡同步

```bash
uv run python -m tests.seed_app_db
uv run python -m app.tools.sync_cards --dry-run
uv run python -m app.tools.sync_cards
```

### API Key

```bash
uv run python -m app.tools.api_keys create --name oai123 --expires-in-days 365
uv run python -m app.tools.api_keys list
uv run python -m app.tools.api_keys revoke --prefix kt_a1b2c3d4
```

## 环境变量

全部来自环境变量或 `.env`；带 Secret 的字段用 `SecretStr`，不会出现在 repr 和日志里。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./app.db` | 数据库连接串；MySQL 用 `mysql+pymysql://用户名:密码@主机:3306/库名?charset=utf8mb4` |
| `APP_NAME` | `kt` | 应用名，显示在 OpenAPI 标题上 |
| `DEBUG` | `false` | 打开后 SQLAlchemy 打印 SQL |
| `POOL_SIZE` / `MAX_OVERFLOW` | `5` / `10` | MySQL 连接池大小，SQLite 不用 |
| `POOL_RECYCLE_SECONDS` | `3600` | MySQL 连接回收秒数 |
| `API_PREFIX` | `/api` | 业务路由前缀；Webhook 不受它影响 |
| `CORS_ORIGINS` | `[]` | 允许的跨域来源，JSON 数组 |
| `AUTO_MIGRATE` | `false` | 启动时是否自动 `alembic upgrade head`；开发可设为 `true`，生产保持关闭 |
| `AILINK_BASE_URL` | `https://card.ailinkvcc.com` | VCC 开放 API 域名 |
| `AILINK_API_KEY` | 空 | VCC API Key |
| `AILINK_API_SECRET` | 空 | VCC API Secret，只用于请求签名 |
| `AILINK_WEBHOOK_SECRET` | 空 | Webhook 验签 + 卡三要素解密；不配就不收 Webhook、不解密 |
| `AILINK_TIMEOUT_SECONDS` | `10` | VCC 请求超时 |
| `AILINK_WEBHOOK_MAX_BODY_BYTES` | `1048576` | Webhook 正文上限，超出返回 413 |
| `AILINK_WEBHOOK_DEBUG` | `false` | 排查开关：成功请求也打印 header/body，敏感字段自动打码；生产关闭 |
| `CARD_STOCK_ENABLED` | `false` | 是否启动卡库存扫描任务 |
| `CARD_STOCK_INTERVAL_SECONDS` | `300` | 扫描间隔 |
| `CARD_STOCK_PENDING_MINUTES` | `30` | 有未完成开卡申请时，这段时间内不重复开卡 |
| `CARD_STOCK_USDT_USD_RATE` | `0.997` | USDT→USD 折算率，10 USDT 约 9.97 USD |
| `BYOC_BASE_URL` | `https://wangbohui.com` | BYOC 站点地址，只接受 HTTPS |
| `BYOC_API_KEY` | 空 | BYOC API Key，放在 `X-API-Key` |
| `BYOC_CALLBACK_SECRET` | 空 | 回调签名密钥，拼在 `k=v&...` 后面做 MD5 |
| `TASK_PAYLOAD_KEY` | 空 | 任务 payload 的 AES-256-GCM 密钥；32 字节 base64 或任意字符串（内部 SHA256 派生） |
| `SUBSCRIPTION_WORKER_ENABLED` | `false` | 是否启动订阅任务 worker |
| `SUBSCRIPTION_WORKER_INTERVAL_SECONDS` | `10` | worker 轮询间隔 |
| `SUBSCRIPTION_CALLBACK_MAX_ATTEMPTS` | `5` | 回调最多尝试几次，超过后放弃 |
| `AILINK_RATE_LIMIT_PER_MINUTE` | `450` | AiLink 每分钟请求上限；重试和只读 429 重试也占配额 |
| `BYOC_RATE_LIMIT_PER_MINUTE` | `300` | BYOC 每分钟请求上限；create 重试、recover 也占配额 |
