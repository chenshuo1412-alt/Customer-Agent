"""Hard draft-only limits. There is intentionally no buyer-send status."""

from __future__ import annotations

ALLOWED_DRAFT_STATUSES = frozenset(
    {
        "pending_generation",
        "ready_for_review",
        "invalidated",
        "needs_human_review",
        "skipped_already_replied",
        "webhook_held",
        "webhook_transport_failed",
        "webhook_transport_accepted_awaiting_receipt",
    }
)

# Statuses that must never be introduced. A webhook HTTP 200 is not one of these.
FORBIDDEN_DRAFT_STATUSES = frozenset(
    {
        "sent",
        "sent_to_buyer",
        "completed",
        "delivered",
        "auto_replied",
    }
)

CANNED_DRAFT_TEXT = "【模拟草稿】这是本地示例，不是真实客服回复，也没有发送给买家。"
PENDING_REASON = "未配置草稿生成器，待生成。没有伪造客户回复。"
MODEL_UNVERIFIED_REASON = "独立模型通道未验证，本版本不调用云 API，也不伪造回复。"
DISCLAIMER = "未连接真实店铺。未向买家发送。Webhook 投递成功也不等于回复草稿已返回。"


class SafetyError(RuntimeError):
    pass


def assert_draft_status(status: str) -> str:
    if status in FORBIDDEN_DRAFT_STATUSES or status not in ALLOWED_DRAFT_STATUSES:
        raise SafetyError(f"draft status refused: {status}")
    return status
