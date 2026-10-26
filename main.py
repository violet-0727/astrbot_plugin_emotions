"""她自己的情绪（astrbot_plugin_emotions）

独立的情绪插件：全局基调 + 对每个用户的态度，都会随时间衰减。
不依赖、不修改任何其它插件。

* 全局：心情 / 精力 / 耐心 / 黏人 —— 决定整体说话基调
* 用户：好感 / 耐心 —— 决定对这个人什么态度
  心情低 + 关系远 → 冷淡；心情低 + 关系近 → 黏人（这是「像人」的关键）
* 注入走临时内容块（mark_as_temp），不改 system_prompt
"""

from __future__ import annotations

import os
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .state import EmotionState
from .tools import EmotionShiftTool

PLUGIN_NAME = "astrbot_plugin_emotions"

RESIDUAL_TEXT = {
    "mood": {True: "心情好了一点", False: "心情被拉低了一点"},
    "patience": {True: "耐心回来了", False: "耐心被磨掉了一点"},
    "warmth": {True: "对这个人更亲近了一点", False: "对这个人有点疏远"},
    "cling": {True: "有点想他", False: "没那么黏了"},
    "energy": {True: "精神好了点", False: "有点累了"},
}

NEG_WORDS = ("滚", "闭嘴", "别说了", "没用一个", "去死", "恶心")
POS_WORDS = ("可爱", "喜欢", "厉害", "好乖", "棒", "真好", "抱抱", "想你", "谢谢")
API_PREFIX = f"/{PLUGIN_NAME}"

try:
    from quart import request
except Exception:  # pragma: no cover
    request = None


@register(
    PLUGIN_NAME,
    "若剑清",
    "她自己的情绪：全局基调 + 对每个人的态度，会随时间慢慢平复。",
    "0.3.2",
    "",
)
class EmotionsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        self.state = EmotionState(os.path.join(data_dir, "state.json"))

        self.enabled = self._bool("enabled", True)
        self.inject_enabled = self._bool("inject_enabled", True)
        self.track_users = self._bool("track_users", True)
        self._seen: set[str] = set()
        self._cool: dict[str, float] = {}
        self._recent: dict[str, list[float]] = {}

        self.tools: list[Any] = [EmotionShiftTool(state=self.state)]
        self.context.add_llm_tools(*self.tools)
        self._register_web_apis()

        snap = self.state.snapshot()
        logger.info(
            "[Emotions] 她自己的情绪加载完成：整体=%s，记录的用户 %d 个",
            snap.get("overall"),
            len(snap.get("users") or []),
        )

    # ---------------- 基础 ----------------

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    async def _json_body(self) -> dict:
        if request is None:
            return {}
        try:
            payload = await request.get_json(silent=True)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    def _cool_ok(self, key: str, seconds: float) -> bool:
        now = time.time()
        if now - float(self._cool.get(key) or 0) < seconds:
            return False
        self._cool[key] = now
        return True

    # ---------------- 面板 API ----------------

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("[Emotions] 当前 AstrBot 版本不支持插件 Web API，跳过面板注册")
            return
        routes = [
            ("/state", self.api_state, ["GET"], "当前情绪"),
            ("/shift", self.api_shift, ["POST"], "手动调节"),
            ("/reset", self.api_reset, ["POST"], "重置"),
            ("/user/save", self.api_user_save, ["POST"], "直接设定某人的数值"),
        ]
        for path, handler, methods, desc in routes:
            try:
                register(f"{API_PREFIX}{path}", handler, methods, desc)
            except Exception as exc:
                logger.warning("[Emotions] 注册面板 API 失败 %s: %s", path, exc)

    async def api_state(self):
        snap = self.state.snapshot()
        snap["ok"] = True
        snap["ts"] = time.time()
        return snap

    async def api_shift(self):
        payload = await self._json_body()
        scope = str(payload.get("scope") or "global").strip().lower()
        dim = str(payload.get("dim") or "mood").strip().lower()
        qq = str(payload.get("qq") or "").strip()
        try:
            delta = float(payload.get("delta") or 0)
        except Exception:
            delta = 0.0
        if not delta:
            return {"ok": False, "error": "delta 不能为 0"}
        result = self.state.shift(scope, dim, delta, str(payload.get("reason") or "面板手动调整"), qq)
        result["state"] = self.state.snapshot()
        return result

    async def api_user_save(self):
        payload = await self._json_body()
        qq = str(payload.get("qq") or "").strip()
        if not qq:
            return {"ok": False, "error": "缺少 qq"}
        row = self.state.set_user(
            qq,
            warmth=payload.get("warmth"),
            bond=payload.get("bond"),
            patience=payload.get("patience"),
        )
        return {"ok": bool(row), "user": row, "state": self.state.snapshot()}

    async def api_reset(self):
        payload = await self._json_body()
        what = str(payload.get("what") or "global").strip().lower()
        now = time.time()
        if what in ("global", "all"):
            from .state import GLOBAL_BASE

            self.state.data["global"] = dict(GLOBAL_BASE)
            self.state.data["global"]["updated_at"] = now
        if what == "all":
            self.state.data["users"] = {}
        if what != "all":
            self.state.data.setdefault("events", []).append(
                {"t": now, "scope": "system", "qq": "", "dim": what, "delta": 0, "reason": "重置"}
            )
        self.state.save()
        return {"ok": True, "state": self.state.snapshot()}

    # ---------------- 记录说话的人 ----------------

    def _auto_triggers(self, qq: str, text: str) -> None:
        """不依赖模型的事件触发：被说难听话、被夸、连发、久未联系。"""
        now = time.time()
        try:
            # 连发消息
            history = self._recent.setdefault(qq, [])
            history.append(now)
            self._recent[qq] = [t for t in history if now - t < 90]
            if len(self._recent[qq]) >= 5 and self._cool_ok("spam:" + qq, 600):
                self.state.shift("global", "patience", -0.06, "被连着发消息")

            # 难听话
            if any(word in text for word in NEG_WORDS) and self._cool_ok("neg:" + qq, 900):
                self.state.shift("global", "patience", -0.06, "被说了难听的话")
                self.state.shift("global", "mood", -0.04, "被说了难听的话")

            # 好话
            if any(word in text for word in POS_WORDS) and self._cool_ok("pos:" + qq, 300):
                self.state.shift("global", "mood", 0.07, "被夸或被谢")
                self.state.shift("user", "warmth", 0.04, "被夸或被谢", qq)
                self.state.shift("user", "bond", 0.02, "被认真对待", qq)

            # 久未联系（超过 6 小时）
            row = self.state.data["users"].get(qq) or {}
            last = float(row.get("last_seen") or 0)
            if last and now - last > 6 * 3600 and self._cool_ok("absence:" + qq, 21600):
                self.state.shift("global", "mood", -0.04, "很久没说话")
                self.state.shift("global", "cling", 0.10, "有点想他")
        except Exception as exc:
            logger.debug("[Emotions] 自动触发失败: %s", exc)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_sender(self, event: AstrMessageEvent):
        if not self.track_users:
            return
        try:
            qq = str(event.get_sender_id() or "").strip()
            name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
            if not qq:
                return
            text = str(getattr(event, "message_str", "") or "")
            if text:
                self._auto_triggers(qq, text)
            row = self.state.data["users"].get(qq)
            if row is not None and row.get("name") == name and qq in self._seen:
                row["last_seen"] = time.time()
                row["talks"] = int(row.get("talks") or 0) + 1
                return
            self._seen.add(qq)
            self.state.touch_user(qq, name)
        except Exception as exc:
            logger.debug("[Emotions] 记录用户失败: %s", exc)

    # ---------------- 注入 ----------------

    @filter.on_llm_request()
    async def inject_emotion(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled or not self.inject_enabled:
            return
        qq = ""
        name = ""
        try:
            qq = str(event.get_sender_id() or "").strip()
            name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
        except Exception:
            pass

        try:
            att = self.state.attitude(qq, name)
        except Exception as exc:
            logger.debug("[Emotions] 取情绪失败: %s", exc)
            return

        regretted = False
        residual: list[dict] = []
        try:
            regretted = self.state.maintain()
            residual = self.state.consume_residual()
        except Exception:
            pass

        lines = ["【此刻的状态】", f"整体：{att['phrase']}"]
        who = "这个人" if name else "对方"
        lines.append(f"对{who}：{att['tag']} —— {att['hint']}")
        if att.get("extra"):
            lines.append("另外：" + "、".join(att["extra"]))
        if att.get("special"):
            lines.append("他是不一样的：你对他比对别人松，但这件事你不会说出口")
            lines.append("有些心事只跟他说，说的时候也是半遮半掩")
            lines.append("再往下的你会害羞地岔开，不会顺着走")
            lines.append("对他不用收着，语气可以软一点，也不用急着结束话题")
        elif att.get("can_share_daily"):
            lines.append("跟他说点日常私事是可以的，但不会主动摊开")
        lines.append("这次说话：" + att["behavior"])
        residual_text: list[str] = []
        seen_dims: set[str] = set()
        for item in residual[:3]:
            dim_key = str(item.get("dim") or "")
            if dim_key in seen_dims:
                continue
            seen_dims.add(dim_key)
            up = float(item.get("delta") or 0) > 0
            residual_text.append(RESIDUAL_TEXT.get(dim_key, {True: "心里有点变化", False: "心里有点波动"})[up])
            if len(seen_dims) >= 2:
                break
        if residual_text:
            lines.append("你还在想刚才的事：" + "、".join(residual_text) + "，还没完全过去")
        if regretted:
            lines.append("刚才那下有点冲，心里有点过意不去")
        lines.append("不要说出这些，也别解释自己的情绪，用语气、话的长短和态度表现。")

        block = "\n".join(lines)
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            parts.append(TextPart(text=block).mark_as_temp())
        except Exception as exc:
            logger.debug("[Emotions] 注入失败: %s", exc)

    # ---------------- 命令 ----------------

    @filter.command("情绪")
    async def emotion_command(self, event: AstrMessageEvent):
        """看看她现在的情绪：情绪"""
        snap = self.state.snapshot()
        glob = snap["global"]
        users = snap.get("users") or []
        lines = [
            f"整体：{snap['overall']}（心情 {glob['mood']:+.2f}｜精力 {glob['energy']:.2f}"
            f"｜耐心 {glob['patience']:.2f}｜黏人 {glob['cling']:.2f}）"
        ]
        for user in users[:3]:
            lines.append(f"· {user['name']}：{user['tag']}（好感 {user['warmth']:+.2f}）")
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        try:
            self.state.save()
        except Exception:
            pass
        for tool in getattr(self, "tools", []):
            try:
                self.context.provider_manager.llm_tools.remove_func(tool.name)
            except Exception:
                pass
        logger.info("[Emotions] 她自己的情绪已停止。")
