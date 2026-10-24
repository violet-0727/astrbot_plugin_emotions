"""情绪状态：全局基调 + 每个用户单独的态度。

设计
----
* 全局：mood / energy / patience / cling —— 决定「整体的说话基调」
* 用户：warmth / patience —— 决定「对这个人什么态度」
* 两者组合出态度标签：难过时对生人冷淡，对亲近的人反而黏
* 所有维度都会随时间衰减回基线，避免一次吵架永久低气压
"""

from __future__ import annotations

import json
import math
import os
import time

GLOBAL_BASE = {"mood": 0.10, "energy": 0.70, "patience": 0.70, "cling": 0.30}
USER_BASE = {"warmth": 0.0, "patience": 0.70}

# 半衰期（小时）
HALF_LIFE = {"mood": 6.0, "energy": 12.0, "patience": 1.0, "cling": 4.0, "warmth": 168.0}

LIMITS = {
    "mood": (-1.0, 1.0),
    "energy": (0.0, 1.0),
    "patience": (0.0, 1.0),
    "cling": (0.0, 1.0),
    "warmth": (-1.0, 1.0),
}

# 单次调节上限（惯性：一句话改变不了太多）
STEP = {"mood": 0.15, "energy": 0.10, "patience": 0.20, "cling": 0.15, "warmth": 0.08}


def _clamp(dim: str, value: float) -> float:
    low, high = LIMITS[dim]
    return max(low, min(high, float(value)))


def _decay(value: float, base: float, half_life_hours: float, seconds: float) -> float:
    if seconds <= 0:
        return value
    factor = 0.5 ** (seconds / (half_life_hours * 3600.0))
    return base + (value - base) * factor


def _energy_base(now: float) -> float:
    hour = time.localtime(now).tm_hour
    if 0 <= hour < 6:
        return 0.30
    if 7 <= hour < 11:
        return 0.80
    if 14 <= hour < 17:
        return 0.60
    return 0.70


class EmotionState:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.data: dict = {"global": {}, "users": {}, "events": []}
        self.load()

    # ---------------- 持久化 ----------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        glob = raw.get("global") if isinstance(raw.get("global"), dict) else {}
        self.data = {
            "global": {k: _clamp(k, glob.get(k, v)) for k, v in GLOBAL_BASE.items()},
            "users": {},
            "events": raw.get("events") if isinstance(raw.get("events"), list) else [],
        }
        self.data["global"]["updated_at"] = float(glob.get("updated_at") or time.time())

        users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
        for qq, row in users.items():
            if not isinstance(row, dict):
                continue
            self.data["users"][str(qq)] = {
                "name": str(row.get("name") or "")[:40],
                "warmth": _clamp("warmth", row.get("warmth", USER_BASE["warmth"])),
                "patience": _clamp("patience", row.get("patience", USER_BASE["patience"])),
                "last_seen": float(row.get("last_seen") or 0),
                "updated_at": float(row.get("updated_at") or 0),
                "talks": int(row.get("talks") or 0),
            }

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---------------- 衰减 ----------------

    def _global_now(self, now: float) -> dict:
        glob = self.data["global"]
        elapsed = now - float(glob.get("updated_at") or now)
        bases = dict(GLOBAL_BASE)
        bases["energy"] = _energy_base(now)
        out = {}
        for dim, base in bases.items():
            out[dim] = _clamp(dim, _decay(glob.get(dim, base), base, HALF_LIFE[dim], elapsed))
        return out

    def _user_now(self, qq: str, now: float) -> dict:
        row = self.data["users"].get(qq) or {}
        elapsed = now - float(row.get("updated_at") or now)
        out = {}
        for dim, base in USER_BASE.items():
            out[dim] = _clamp(dim, _decay(row.get(dim, base), base, HALF_LIFE[dim], elapsed))
        return out

    def tick(self, now: float | None = None) -> None:
        """把衰减写回（顺带保证 updated_at 前进）。"""
        now = now or time.time()
        glob = self._global_now(now)
        self.data["global"].update(glob)
        self.data["global"]["updated_at"] = now
        for qq, row in self.data["users"].items():
            row.update(self._user_now(qq, now))
            row["updated_at"] = now

    # ---------------- 用户 ----------------

    def touch_user(self, qq: str, name: str = "") -> dict:
        qq = str(qq).strip()
        if not qq:
            return {}
        now = time.time()
        row = self.data["users"].get(qq)
        if row is None:
            row = {
                "name": str(name or "")[:40],
                "warmth": USER_BASE["warmth"],
                "patience": USER_BASE["patience"],
                "last_seen": now,
                "updated_at": now,
                "talks": 0,
            }
            self.data["users"][qq] = row
        if name:
            row["name"] = str(name)[:40]
        row["last_seen"] = now
        row["talks"] = int(row.get("talks") or 0) + 1
        self.save()
        return row

    # ---------------- 调节 ----------------

    def shift(self, scope: str, dim: str, delta: float, reason: str = "", qq: str = "") -> dict:
        now = time.time()
        self.tick(now)
        if dim not in LIMITS:
            return {"ok": False, "error": "未知维度 %s" % dim}
        delta = max(-STEP[dim], min(STEP[dim], float(delta)))
        if scope == "user":
            if not qq:
                return {"ok": False, "error": "缺少用户"}
            row = self.data["users"].get(qq)
            if row is None:
                self.touch_user(qq)
                row = self.data["users"][qq]
            if dim not in ("warmth", "patience", "cling"):
                dim = "warmth"
            row[dim] = _clamp(dim, row.get(dim, USER_BASE.get(dim, 0.0)) + delta)
            row["updated_at"] = now
        else:
            if dim not in GLOBAL_BASE:
                dim = "mood"
            self.data["global"][dim] = _clamp(
                dim, self.data["global"].get(dim, GLOBAL_BASE[dim]) + delta
            )
            self.data["global"]["updated_at"] = now

        event = {
            "t": now,
            "scope": scope,
            "qq": qq if scope == "user" else "",
            "dim": dim,
            "delta": round(delta, 3),
            "reason": str(reason or "")[:120],
        }
        events = self.data.setdefault("events", [])
        events.append(event)
        self.data["events"] = events[-120:]
        self.save()
        return {"ok": True, "event": event}

    # ---------------- 态度 ----------------

    def attitude(self, qq: str = "", name: str = "") -> dict:
        now = time.time()
        glob = self._global_now(now)
        user = self._user_now(str(qq), now) if qq else {"warmth": 0.0, "patience": glob["patience"]}
        mood = glob["mood"]
        energy = glob["energy"]
        warmth = user["warmth"]
        patience = min(glob["patience"], user.get("patience", 0.7))

        close = warmth >= 0.12
        far = warmth <= -0.05
        tags = []
        hint = ""

        if mood <= -0.25:
            if close:
                tag = "闷着，想找人陪"
                hint = "心情低，但对这个人有依赖：会想说话、会找他，只是嘴上不承认"
            elif far:
                tag = "低气压，不爱搭理"
                hint = "心情低，对这个人也没好感：回得很短，能不接就不接"
            else:
                tag = "闷，不太想说话"
                hint = "心情低，对这个人只是认识：话少、回得敷衍，但不会翻脸"
        elif mood >= 0.35:
            if close:
                tag = "心情好，话多"
                hint = "心情好，又对这个人亲近：愿意多说、会主动找话、语气轻快"
            else:
                tag = "心情不错"
                hint = "心情好，但对这个人保持距离：礼貌、话不多，不主动"
        else:
            if close:
                tag = "平常，偏亲近"
                hint = "心情一般，对这个人熟悉：正常聊天，语气随意"
            else:
                tag = "平常"
                hint = "心情一般，对这个人没什么特别：正常回应，不多说"

        if energy < 0.30:
            tags.append("困")
            hint += "；很困，句子会更短更慢"
        if patience < 0.25:
            tags.append("没耐心")
            hint += "；耐心快没了，别被连着问同一件事"

        return {
            "tag": tag,
            "extra": tags,
            "hint": hint,
            "mood": round(mood, 3),
            "energy": round(energy, 3),
            "patience": round(patience, 3),
            "cling": round(glob["cling"], 3),
            "warmth": round(warmth, 3),
            "close": close,
            "name": name,
        }

    # ---------------- 快照 ----------------

    def snapshot(self) -> dict:
        now = time.time()
        glob = self._global_now(now)
        users = []
        for qq, row in self.data["users"].items():
            att = self.attitude(qq, row.get("name") or "")
            users.append(
                {
                    "qq": qq,
                    "name": row.get("name") or qq,
                    "warmth": att["warmth"],
                    "patience": round(att["patience"], 3),
                    "talks": int(row.get("talks") or 0),
                    "last_seen": float(row.get("last_seen") or 0),
                    "tag": att["tag"],
                }
            )
        users.sort(key=lambda u: (-u["warmth"], -u["last_seen"]))
        return {
            "global": {k: round(v, 3) for k, v in glob.items()},
            "overall": self.overall_label(glob),
            "users": users,
            "events": self.data.get("events", [])[-40:],
        }

    @staticmethod
    def overall_label(glob: dict) -> str:
        mood = glob.get("mood", 0)
        if mood >= 0.5:
            return "心情很好"
        if mood >= 0.2:
            return "还不错"
        if mood > -0.2:
            return "平常"
        if mood > -0.5:
            return "有点闷"
        return "很低落"
