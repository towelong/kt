# AGENTS.md

本文件是本仓库的工程约定，改代码前先读，冲突时以本文件为准。

## 技术栈

- Python ≥ 3.11，依赖与虚拟环境统一用 `uv`（`.venv` 在仓库根，不要用系统 python 装包）。
- FastAPI + SQLModel（`sqlmodel>=0.0.48`，依赖它的 `UTCDateTime` 语义）+ Alembic。
- 数据库同一套代码支持两种：SQLite（开发默认）与 MySQL（生产），驱动 `pymysql`，连接串只来自环境变量 `DATABASE_URL`。

## 目录与分层职责

```
app/
├── main.py          # 应用入口、lifespan、路由挂载
├── config.py        # Settings、DATABASE_URL 校验/脱敏
├── db.py            # engine、get_session、双库差异
├── dependencies.py  # SessionDep、PageDep 等可复用依赖
├── models.py        # 表模型 + Create/Update/Public 模型 + naming_convention
├── crud.py          # 数据访问，唯一直接使用 Session 的层
├── migrations.py    # 以代码方式调用 Alembic
├── integrations/    # 外部服务对接（每个 vendor 一个子包）
│   └── ailink/      # AiLink VCC 客户端、事件模型、Webhook 验签与分发
├── services/        # 业务编排：跨 crud 组合、决策、调用 integrations
├── jobs/            # 定时任务：进程内 asyncio + 数据库租约
├── routers/         # 只做 HTTP：参数、状态码、错误翻译
└── internal/        # 不属于某个资源的内部/运维逻辑
migrations/          # Alembic 环境与版本脚本，必须入库
tests/
```

调用方向固定为 `routers → crud → models/db`，不允许反向：

- 路由里不写 SQL、不直接 `session.add/commit`、不拼查询。
- `crud` 里不出现 `HTTPException`、`Request`、状态码。业务异常用 `crud.NotFoundError` / `crud.ConflictError`，由路由翻译成 404 / 409。
- 跨资源的复用逻辑放 `dependencies.py`，不要放进某个 router 供别的 router import。
- 运维/维护类接口放 `internal/`，不塞进业务 router。
- 外部 HTTP 服务统一封装在 `app/integrations/<vendor>/`：签名、重试、错误翻译、事件模型都在里面，路由只做「解析请求 → 调集成层 → 翻译响应」。
- 集成层的密钥只从 `Settings` 取，类型用 `SecretStr`；日志和异常里不出现明文、不打印请求/响应正文。
- 业务编排放 `app/services/`，只调 `crud` 读写，不写 SQL；Webhook 处理器和定时任务都经 services 落库。
- 定时任务放 `app/jobs/`：必须能从配置关掉、必须用 `job_lease` 抢锁、异常只记日志不退出循环。

## 命名与日志

- 表名、列名、SQLModel 字段统一 `snake_case`；平台返回的 `camelCase` 只存在于 `app/integrations` 的模型里，写入数据库前完成转换。
- 业务失败用 `logger.error`：余额不足、开卡失败/首充失败、交易授权失败、敏感字段解密失败、找不到本地卡。日志带 `plan` / `request_no` / `card_id` / `transaction_id` 这类业务键，便于定位。
- 卡三要素（完整卡号、有效期、CVV）和 3DS 验证码禁止出现在日志、异常信息、接口响应里；只允许出现在 `card` 表对应列。需要展示时给脱敏卡号。
- 对外接口密钥存 `api_key` 表，只保留 sha256 和前缀；完整 key 只在生成命令里输出一次，日志最多出现前缀。

## 双库硬约束

数据库差异只允许出现在 `app/db.py` 和 `migrations/env.py`。业务代码里不出现 `if sqlite` / `if mysql`。

- 字符串字段必须给 `max_length`（MySQL 要求长度）；不用 `JSON` 列，结构体存 `Text` 自己序列化。
- 时间统一存带时区的 UTC：`datetime.now(UTC)`，默认值放 Python 侧（`default_factory`），不用 `server_default=func.now()`。
- 不用 `ILIKE`、`INSERT OR REPLACE`、`AUTOINCREMENT`、`PRAGMA`、`ON CONFLICT` 等方言写法；模糊查询用 `like`。
- 不依赖大小写不敏感的比较（SQLite 与 MySQL 排序规则不同）。
- 分页统一 `offset/limit`，`limit` 在 `crud` 里再夹一次上限（`MAX_LIMIT = 100`）。
- 唯一约束冲突一律 catch `IntegrityError` → `ConflictError` → 409，不判断数据库错误码/错误文本。
- 事务边界在 `crud`：一个函数一次 `commit`，失败 `rollback`；不要在多个 crud 调用之间留半截事务。
- SQLite 侧：`check_same_thread=False`、连接时 `PRAGMA foreign_keys=ON`；MySQL 侧：`pool_pre_ping` + `pool_recycle`。
- 日志、接口返回的连接串必须脱敏，用 `config.mask_url()`，绝不打印密码。

## 模型与迁移

- 表模型写在 `app/models.py`，继承 `TimestampMixin`（`created_at`/`updated_at`），主键 `id: int | None = Field(default=None, primary_key=True)`。
- 约束/索引名依赖 `SQLModel.metadata.naming_convention`，不要删；新增约束时保证名字在 MySQL 的 64 字符上限内。
- 禁止 `SQLModel.metadata.create_all()`，建表与改表只走 Alembic。`tests` 里也不要另建表，迁移本身就是建表路径。
- 已执行过的迁移脚本不允许修改（改了要 `downgrade` 后重来）；修补用新迁移。
- `migrations/env.py` 的 `render_item` 负责把 `AutoString`/`UTCDateTime` 落成 `sa.String(...)`/`sa.DateTime(timezone=True)`，迁移脚本里只出现标准 `sa.*` 类型。
- SQLite 用 `render_as_batch=True`，MySQL 关闭；`autogenerate` 之后要 `ruff format migrations/versions`。

## 迁移流程

```bash
uv run alembic revision --autogenerate -m "add card remark"  # 生成，比对模型 vs 当前库
uv run ruff format migrations/versions                     # 格式化生成的脚本
# 打开脚本人工核对：重命名会被识别成删列+加列，删列/改类型要补数据迁移
uv run alembic upgrade head                                # 执行
uv run alembic downgrade -1                                # 回退一步；base 为清空
uv run alembic check                                       # 只比对不写入，CI 用
```

- `autogenerate` 需要连得上 `DATABASE_URL`，且库已到 head。
- 删列/改类型优先分两次发布：先让代码停止读写，再发删列迁移。
- 种子/数据迁移手写 `op.execute`，不要塞进启动逻辑。
- 启动时默认自动 `upgrade head`（`AUTO_MIGRATE=true`）；多进程/多副本部署必须置 `false`，在发布流程里单独执行迁移。

## 测试与质量门

提交前必须全绿：

```bash
uv run ruff check .
uv run ruff format --check app migrations tests
uv run pytest
```

- 测试用临时 SQLite（`tests` 里已有的 fixture 模式），不许写进项目的 `app.db`。
- 新增接口至少覆盖：正常路径、404/409 分支、分页边界。
- 改了模型就跑 `alembic check`（`tests/test_migrations.py` 已包含），确保迁移和模型一致。
- 涉及双库差异的改动，用 `uv run alembic upgrade head --sql` 配 MySQL URL 看一遍生成的 DDL。

## 命令速查

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run python -c "import sys; print(sys.executable)"   # 确认解释器
```

## 禁止事项

- 不要用 `pip install` 改环境，依赖用 `uv add` / `uv remove`。
- 不要在路由/模型里写数据库方言判断或原始 SQL 字符串拼接。
- 不要把密码、令牌写进代码、`.env.example`、日志、接口响应。
- 不要绕过 `crud` 从路由直接拿 `Session` 操作数据。
- `internal/admin.py` 目前没有鉴权，只允许本机使用；上线前必须先挂管理员依赖，而不是依赖“路径不公开”。
