# kt

FastAPI + SQLModel 服务骨架，同一套代码跑 SQLite 与 MySQL。

## 目录

```
app/
├── __init__.py
├── main.py          # 应用入口、lifespan（建表）、路由挂载
├── config.py        # 配置，全部来自环境变量 / .env
├── db.py            # engine、会话、SQLite 特有参数、安全 URL
├── dependencies.py  # SessionDep、分页依赖
├── models.py        # 数据表模型 + 请求/响应模型
├── crud.py          # 数据访问（唯一碰 Session 的层）
├── routers/
│   ├── items.py
│   └── users.py
└── internal/
    └── admin.py     # 运维接口：库信息、手工建表
tests/
└── test_smoke.py
```

调用方向固定为 `routers → crud → models/db`：路由只管 HTTP 与状态码，`crud` 只管数据，`internal` 放不属于某个资源的逻辑。

## 跑起来

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```

- 文档：http://127.0.0.1:8000/docs
- 健康检查：`GET /health`
- 当前库自检：`GET /internal/admin/db-info`

## 切到 MySQL

```bash
DATABASE_URL='mysql+pymysql://用户名:密码@127.0.0.1:3306/kt?charset=utf8mb4' uv run uvicorn app.main:app --reload
```

库要先建好（`CREATE DATABASE kt CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;`），表由启动时的 `create_all` 建。测试：`uv run pytest`。

## 双库约定

- 差异只在 `app/db.py`：SQLite 放 `check_same_thread=False` 并开 `PRAGMA foreign_keys=ON`；MySQL 用 `pool_pre_ping` + `pool_recycle`。
- 字符串字段一律给 `max_length`（MySQL 要求长度）；不用 `JSON` 列，结构体存 `Text` 自己序列化。
- 时间统一用带时区的 UTC（`datetime.now(UTC)`），SQLModel 的 `UTCDateTime` 会在 MySQL 上落成无偏移的 UTC。
- 分页只用 `offset/limit`，不写 `ILIKE`、不用 `INSERT OR REPLACE`、不依赖大小写不敏感比较。
- 唯一冲突捕获 `IntegrityError` 转 409，不判断数据库错误码。
- 事务边界在 `crud` 里，一个函数一次 `commit`；批量写入失败要 `rollback`。

## 接下来

- `create_all` 只够开发用，上线前换 Alembic（同一套迁移要能在两种库上跑）。
- 鉴权：在 `dependencies.py` 加 `CurrentUser` 依赖，`internal/admin.py` 必须挂上管理员校验。
