"""事件分发：策略 + 注册表 + 模板方法，路由里不出现 eventType 分支。

- `EventHandler` 固定「类型校验 → process」流程，子类只写业务。
- 幂等由分发器按 eventId 处理：一次事件只跑一轮处理器，全部成功后才记已处理。
- 注册按 eventType（精确）或 eventCategory（大类）匹配，两者都命中时都会执行；
  都没命中才走兜底处理器，没有兜底就只记 warning 并当作成功。
- `EventStore` 用协议隔离，默认进程内有界缓存；以后落库只换实现，处理器不用动。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from app.integrations.ailink.errors import EventDispatchError
from app.integrations.ailink.events import WebhookEvent

logger = logging.getLogger(__name__)


class EventStore(Protocol):
    """幂等存储：至少一次投递，处理器必须挡住同一 eventId 的重复执行。"""

    async def seen(self, event_id: str) -> bool: ...

    async def mark(self, event_id: str) -> None: ...


class MemoryEventStore:
    """进程内有界缓存。多副本部署时必须换成共享存储（库/Redis），否则挡不住跨进程重复。"""

    def __init__(self, *, max_entries: int = 10_000, ttl_seconds: float = 24 * 3600) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        while self._entries:
            event_id, marked_at = next(iter(self._entries.items()))
            if now - marked_at < self._ttl_seconds:
                break
            self._entries.pop(event_id, None)

    async def seen(self, event_id: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        return event_id in self._entries

    async def mark(self, event_id: str) -> None:
        now = time.monotonic()
        self._prune(now)
        self._entries[event_id] = now
        self._entries.move_to_end(event_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


@dataclass(frozen=True, slots=True)
class EventContext:
    event_id: str
    event_type: str
    event_category: str
    merchant_id: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchReport:
    event_id: str
    event_type: str
    handler_names: tuple[str, ...]


E = TypeVar("E", bound=WebhookEvent)


class EventHandler(ABC, Generic[E]):
    """模板方法：类型校验 + 公共日志在 handle()，业务只实现 process()。

    幂等由分发器按 eventId 统一处理（一次事件只跑一轮处理器）；但一次投递里
    只要有一个处理器失败，整轮会重跑，所以处理器自身的业务写入也要按业务键幂等。
    """

    event_model: ClassVar[type[WebhookEvent]]
    event_types: ClassVar[tuple[str, ...]] = ()
    categories: ClassVar[tuple[str, ...]] = ()

    @property
    def name(self) -> str:
        return type(self).__name__

    async def handle(self, event: WebhookEvent, ctx: EventContext) -> None:
        if not isinstance(event, self.event_model):
            raise TypeError(
                f"{self.name} 只处理 {self.event_model.__name__}，收到 {type(event).__name__}"
            )
        await self.process(event, ctx)

    @abstractmethod
    async def process(self, event: E, ctx: EventContext) -> None: ...


HandlerClass = type[EventHandler[Any]]
HandlerDecorator = Callable[[HandlerClass], HandlerClass]


class EventDispatcher:
    def __init__(self, *, store: EventStore | None = None) -> None:
        self._store = store or MemoryEventStore()
        self._by_type: dict[str, list[EventHandler[Any]]] = {}
        self._by_category: dict[str, list[EventHandler[Any]]] = {}
        self._fallback: list[EventHandler[Any]] = []

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def handler_count(self) -> int:
        return len(self.iter_handlers())

    def iter_handlers(self) -> list[EventHandler[Any]]:
        ordered: list[EventHandler[Any]] = []
        groups = (*self._by_type.values(), *self._by_category.values(), self._fallback)
        for group in groups:
            for handler in group:
                if all(handler is not seen for seen in ordered):
                    ordered.append(handler)
        return ordered

    def register(self, handler_cls: HandlerClass) -> HandlerClass:
        """用作类装饰器：`@dispatcher.register`。"""
        handler = handler_cls()
        if not handler.event_types and not handler.categories:
            self._fallback.append(handler)
            return handler_cls
        for event_type in handler.event_types:
            self._by_type.setdefault(event_type, []).append(handler)
        for category in handler.categories:
            self._by_category.setdefault(category, []).append(handler)
        return handler_cls

    def resolve(self, event: WebhookEvent) -> list[EventHandler[Any]]:
        handlers: list[EventHandler[Any]] = []
        for handler in (
            *self._by_type.get(event.event_type, ()),
            *self._by_category.get(event.event_category, ()),
        ):
            if handler not in handlers:
                handlers.append(handler)
        if handlers:
            return handlers
        return list(self._fallback)

    async def dispatch(
        self,
        event: WebhookEvent,
        *,
        received_at: datetime | None = None,
    ) -> DispatchReport:
        ctx = EventContext(
            event_id=event.event_id,
            event_type=event.event_type,
            event_category=event.event_category,
            merchant_id=event.merchant_id,
            received_at=received_at or datetime.now(UTC),
        )
        handlers = self.resolve(event)
        if await self._store.seen(event.event_id):
            logger.info(
                "VCC Webhook 重复投递，跳过：event=%s type=%s",
                ctx.event_id,
                ctx.event_type,
            )
            return DispatchReport(
                event_id=ctx.event_id,
                event_type=ctx.event_type,
                handler_names=(),
            )
        if not handlers:
            logger.warning(
                "VCC Webhook 没有匹配的处理器，按成功应答：event=%s type=%s category=%s",
                ctx.event_id,
                ctx.event_type,
                ctx.event_category,
            )
            return DispatchReport(
                event_id=ctx.event_id,
                event_type=ctx.event_type,
                handler_names=(),
            )

        results = await asyncio.gather(
            *(self._run(handler, event, ctx) for handler in handlers),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        names = tuple(handler.name for handler in handlers)
        if failures:
            logger.error(
                "VCC Webhook 处理器失败，等待平台重试：event=%s type=%s handlers=%s failures=%d",
                ctx.event_id,
                ctx.event_type,
                ",".join(names),
                len(failures),
            )
            raise EventDispatchError(
                f"{len(failures)} 个处理器失败",
                event_id=ctx.event_id,
                event_type=ctx.event_type,
                failures=failures,
            )

        await self._store.mark(event.event_id)
        return DispatchReport(
            event_id=ctx.event_id,
            event_type=ctx.event_type,
            handler_names=names,
        )

    async def _run(
        self,
        handler: EventHandler[Any],
        event: WebhookEvent,
        ctx: EventContext,
    ) -> None:
        started = time.perf_counter()
        try:
            await handler.handle(event, ctx)
        finally:
            logger.debug(
                "VCC Webhook handler=%s event=%s 耗时 %.1fms",
                handler.name,
                ctx.event_id,
                (time.perf_counter() - started) * 1000,
            )


# 全局注册表：handler 模块用 ``@dispatcher.register`` 在导入时挂上来
dispatcher = EventDispatcher()
