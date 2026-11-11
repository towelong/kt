"""默认事件处理器：导入本包即完成注册（`@dispatcher.register` 写在各自模块里）。

业务逻辑从这里接手：现在只做结构化日志 + 幂等，落库/发内部事件加在 process() 里。
"""

from app.integrations.ailink.dispatch import dispatcher
from app.integrations.ailink.handlers import (  # noqa: F401  导入即注册
    budget_operation,
    card_3ds,
    card_issue,
    card_operation,
    card_transaction,
    webhook_test,
)

__all__ = ["dispatcher"]
