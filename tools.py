"""给模型用的工具：调整情绪。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

DIM_LABEL = {
    "mood": "心情",
    "energy": "精力",
    "patience": "耐心",
    "cling": "黏人",
    "warmth": "对这个人的好感",
}

DESC = (
    "调整你此刻的情绪。global 是整体基调（心情/精力/耐心/黏人），"
    "user 是你对当前说话这个人的态度（好感/耐心）。"
    "只在真的被触动时调，一次只调一点：被夸、被认真对待 → mood 或 warmth 加；"
    "被冷落、被打断、被戳 → mood 或 patience 减。日常寒暄不要调。"
    "遇到真正的大事（被拉黑、被说很重的话、被认真表白）才把 spike 设为 true，"
    "平时不要用。"
)


@dataclass
class EmotionShiftTool(FunctionTool):
    state: Any = field(repr=False, default=None)
    name: str = "emotion_shift"
    description: str = DESC
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "global 整体情绪 / user 对当前这个人",
                    "enum": ["global", "user"],
                },
                "dim": {
                    "type": "string",
                    "description": "global 可选 mood/energy/patience/cling；user 可选 warmth/patience",
                },
                "delta": {
                    "type": "number",
                    "description": "改变多少，通常 ±0.05 ~ ±0.15，正数上升，负数下降",
                },
                "reason": {"type": "string", "description": "为什么变，一句话"},
                "spike": {
                    "type": "boolean",
                    "description": "是不是重大事件（默认 false）。true 时允许一次变化更大",
                },
            },
            "required": ["scope", "dim", "delta"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        scope: str = "global",
        dim: str = "mood",
        delta: float = 0.0,
        reason: str = "",
        spike: bool = False,
    ) -> str:
        scope = str(scope or "global").strip().lower()
        if scope not in ("global", "user"):
            scope = "global"
        try:
            delta = float(delta)
        except Exception:
            return "错误：delta 必须是数字"
        if abs(delta) < 0.001:
            return "没有变化。"
        qq = ""
        if scope == "user":
            try:
                qq = str(event.get_sender_id() or "").strip()
            except Exception:
                qq = ""
            if not qq:
                return "错误：拿不到当前用户，无法调整对其态度。"

        result = self.state.shift(scope, dim, delta, reason, qq, spike=bool(spike))
        if not result.get("ok"):
            return "调整失败：%s" % result.get("error", "未知原因")
        event_row = result.get("event", {})
        return "已调整%s %s %+.2f" % (
            "整体" if scope == "global" else "对该用户",
            DIM_LABEL.get(event_row.get("dim"), event_row.get("dim", "")),
            float(event_row.get("delta") or 0),
        )
