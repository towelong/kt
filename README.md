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
├── routers/
│   ├── items.py
│   └── users.py
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

启动时默认自动 `upgrade head`（`AUTO_MIGRATE=true`）。多进程/多副本部署建议关掉，改在发布流程里单独执行 `alembic upgrade head`，避免并发迁移。

## 新增一条迁移

```bash
# 1. 改 app/models.py
# 2. 生成（需要连得上当前 DATABASE_URL）
uv run alembic revision --autogenerate -m "add item sku"
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

- 鉴权：在 `dependencies.py` 加 `CurrentUser` 依赖，`internal/admin.py` 必须挂上管理员校验。
- 种子数据：写成 `migrations/versions` 里的数据迁移，别塞进启动逻辑。
