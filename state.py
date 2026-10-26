"""情绪状态 v0.2：负向偏差、尖峰、余温、意愿、措辞池。

设计
----
* 全局：mood / energy / patience / cling —— 整体说话基调
* 用户：warmth / patience —— 对这个人的态度
* 负向偏差：坏事的推力更大、衰减更慢（记仇比记好久）
* 尖峰：重大事件可以一次推很大，绕过惯性上限
* 余温：一次事件之后几轮还在想，以叙述形式注入
* 意愿：把情绪折算成「这次想不想说话」，直接指导回复长短
"""

from __future__ import annotations

import json
import os
import random
import time

GLOBAL_BASE = {"mood": 0.10, "energy": 0.70, "patience": 0.70, "cling": 0.30}
USER_BASE = {"warmth": 0.0, "patience": 0.70, "bond": 0.0}

# 冷启动：聊得越多，好感/关系自然往上走（不依赖任何其它插件）
WARMTH_TARGET_DIV = 300.0   # talks / 300 → 上限 0.6
WARMTH_TARGET_CAP = 0.6
BOND_TARGET_DIV = 250.0     # talks / 250 → 上限 0.8
BOND_TARGET_CAP = 0.8
GROW_STEP = 0.02            # 每条消息最多长这么多，慢一点更真

# 关系分层
BOND_FRIEND = 0.30          # 能说日常私事
BOND_SPECIAL = 0.55         # 能说心事，且是「例外」

HALF_LIFE = {"mood": 6.0, "energy": 12.0, "patience": 1.0, "cling": 4.0,
             "warmth": 168.0, "bond": 1440.0}

LIMITS = {
    "mood": (-1.0, 1.0),
    "energy": (0.0, 1.0),
    "patience": (0.0, 1.0),
    "cling": (0.0, 1.0),
    "warmth": (-1.0, 1.0),
    "bond": (0.0, 1.0),
}

STEP = {"mood": 0.15, "energy": 0.10, "patience": 0.20, "cling": 0.15,
        "warmth": 0.08, "bond": 0.05}

NEG_PUSH = 1.6      # 负向推力放大
NEG_SLOW = 1.5      # 负向衰减变慢
SPIKE_PUSH = 2.7    # 尖峰倍率

PHRASES = {
    "high": ["今天心情不错", "心情挺好，有点想说话", "今天看什么都顺眼"],
    "mid": ["心情一般", "没什么特别的", "还行吧"],
    "low": ["有点闷", "提不起劲", "心里堵着点什么", "不太想说话"],
    "verylow": ["很低落", "什么都提不起劲", "闷得慌"],
}

BEHAVIOR = {
    "quiet": "这次可以只回一两个字，或者说完一句就停，别主动扩写",
    "short": "话比平时短，但语气不用冷，别催对方走",
    "normal": "正常回应，语气自然，可以软一点",
    "open": "可以多说一点，语气轻快些，也可以主动问一句",
}


def _clamp(dim: str, value: float) -> float:
    low, high = LIMITS[dim]
    return max(low, min(high, float(value)))


def _decay_dim(dim: str, value: float, base: float, seconds: float) -> float:
    half = HALF_LIFE[dim]
    if value < base:  # 低于基线：忘得慢一点
        half *= NEG_SLOW
    factor = 0.5 ** (seconds / (half * 3600.0))
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
        self.data: dict = {"global": {}, "users": {}, "events": [], "residuals": []}
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
            "residuals": raw.get("residuals") if isinstance(raw.get("residuals"), list) else [],
            "pending_regret": raw.get("pending_regret"),
        }
        self.data["global"]["updated_at"] = float(glob.get("updated_at") or time.time())

        users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
        for qq, row in users.items():
            if not isinstance(row, dict):
                continue
            self.data["users"][str(qq)] = {
                "name": str(row.get("name") or "")[:40],
                "warmth": _clamp("warmth", row.get("warmth", USER_BASE["warmth"])),
                "bond": _clamp("bond", row.get("bond", USER_BASE["bond"])),
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
        return {
            dim: _clamp(dim, _decay_dim(dim, glob.get(dim, base), base, elapsed))
            for dim, base in bases.items()
        }

    def _user_now(self, qq: str, now: float) -> dict:
        row = self.data["users"].get(qq) or {}
        elapsed = now - float(row.get("updated_at") or now)
        return {
            dim: _clamp(dim, _decay_dim(dim, row.get(dim, base), base, elapsed))
            for dim, base in USER_BASE.items()
        }

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        self.data["global"].update(self._global_now(now))
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
                "bond": USER_BASE["bond"],
                "last_seen": now,
                "updated_at": now,
                "talks": 0,
            }
            self.data["users"][qq] = row
        if name:
            row["name"] = str(name)[:40]
        row["last_seen"] = now
        row["talks"] = int(row.get("talks") or 0) + 1
        talks = row["talks"]
        # 聊得多，好感和关系慢慢往上走；只向上靠近，不因为一句不顺就掉
        warmth_target = min(WARMTH_TARGET_CAP, talks / WARMTH_TARGET_DIV)
        if float(row.get("warmth") or 0.0) < warmth_target:
            row["warmth"] = _clamp("warmth", float(row.get("warmth") or 0.0) + GROW_STEP)
        bond_target = min(BOND_TARGET_CAP, talks / BOND_TARGET_DIV)
        if float(row.get("bond") or 0.0) < bond_target:
            row["bond"] = _clamp("bond", float(row.get("bond") or 0.0) + GROW_STEP)
        self.save()
        return row

    # ---------------- 调节 ----------------

    def shift(
        self,
        scope: str,
        dim: str,
        delta: float,
        reason: str = "",
        qq: str = "",
        spike: bool = False,
    ) -> dict:
        now = time.time()
        self.tick(now)
        if dim not in LIMITS:
            return {"ok": False, "error": "未知维度 %s" % dim}

        delta = float(delta)
        if delta < 0:
            delta *= NEG_PUSH  # 坏事推得动
        cap = STEP[dim] * (SPIKE_PUSH if spike else 1.0)
        delta = max(-cap, min(cap, delta))

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
            "spike": bool(spike),
        }
        events = self.data.setdefault("events", [])
        events.append(event)
        self.data["events"] = events[-120:]

        # 余温：变化够大时，接下来几轮还会想起
        if abs(delta) >= 0.10:
            residuals = self.data.setdefault("residuals", [])
            residuals.append(
                {
                    "t": now,
                    "dim": dim,
                    "delta": round(delta, 3),
                    "left": 4 if abs(delta) >= 0.2 else 3,
                    "reason": str(reason or "")[:80],
                    "qq": qq if scope == "user" else "",
                }
            )
            self.data["residuals"] = residuals[-6:]
        self.save()
        return {"ok": True, "event": event}

    def set_user(self, qq: str, **fields) -> dict | None:
        """后台直接设定某个人的数值（面板用）。"""
        qq = str(qq or "").strip()
        if not qq:
            return None
        row = self.data["users"].get(qq)
        if row is None:
            self.touch_user(qq)
            row = self.data["users"][qq]
        for key in ("warmth", "bond", "patience"):
            if key in fields and fields[key] is not None:
                try:
                    row[key] = _clamp(key, float(fields[key]))
                except Exception:
                    pass
        row["updated_at"] = time.time()
        self.save()
        return dict(row)

    # ---------------- 余温 ----------------

    def consume_residual(self) -> list[dict]:
        """每次注入前调用：返回还没消化的，并把计数减一。"""
        now = time.time()
        out = []
        keep = []
        for item in self.data.get("residuals", []):
            if int(item.get("left") or 0) > 0 and now - float(item.get("t") or 0) < 6 * 3600:
                out.append(item)
                item["left"] = int(item["left"]) - 1
                if item["left"] > 0:
                    keep.append(item)
        self.data["residuals"] = keep
        return out

    # ---------------- 后悔 ----------------

    def maintain(self) -> bool:
        """调用一次：到了该后悔的点就补一点心情，返回是否刚后悔。"""
        now = time.time()
        pending = self.data.get("pending_regret")
        if pending and now >= float(pending.get("at") or 0):
            self.data["global"]["mood"] = _clamp(
                "mood", self.data["global"].get("mood", 0.1) + float(pending.get("delta") or 0.07)
            )
            self.data["global"]["updated_at"] = now
            self.data["pending_regret"] = None
            self.save()
            return True
        if not pending:
            glob = self._global_now(now)
            if glob["patience"] < 0.20:
                self.data["pending_regret"] = {"at": now + 300, "delta": 0.07}
        return False

    # ---------------- 意愿与态度 ----------------

    def willingness(self, att: dict) -> float:
        mood = (att.get("mood", 0) + 1) / 2
        warmth = (att.get("warmth", 0) + 1) / 2
        value = 0.55 * mood + 0.25 * att.get("patience", 0.7) + 0.20 * warmth
        if att.get("energy", 0.7) < 0.30:
            value *= 0.8
        return max(0.0, min(1.0, value))

    @staticmethod
    def behavior_key(willing: float) -> str:
        if willing < 0.35:
            return "quiet"
        if willing < 0.52:
            return "short"
        if willing < 0.70:
            return "normal"
        return "open"

    def attitude(self, qq: str = "", name: str = "") -> dict:
        now = time.time()
        glob = self._global_now(now)
        user = self._user_now(str(qq), now) if qq else {"warmth": 0.0, "patience": glob["patience"]}
        mood, energy = glob["mood"], glob["energy"]
        warmth = user["warmth"]
        patience = min(glob["patience"], user.get("patience", 0.7))

        bond = float(user.get("bond") or 0.0) if qq else 0.0
        close = warmth >= 0.12 or bond >= BOND_FRIEND
        far = warmth <= -0.05
        special = bond >= BOND_SPECIAL
        extra = []
        if mood <= -0.25:
            if close:
                tag, hint = "闷着，想找人陪", "心情低，但对这个人有依赖：会想说话、会找他，只是嘴上不承认"
            elif far:
                tag, hint = "低气压，不爱搭理", "心情低，对这个人也没好感：回得很短，能不接就不接"
            else:
                tag, hint = "闷，不太想说话", "心情低，对这个人只是认识：话少、回得敷衍，但不会翻脸"
        elif mood >= 0.35:
            if close:
                tag, hint = "心情好，话多", "心情好，又对这个人亲近：愿意多说、会主动找话、语气轻快"
            else:
                tag, hint = "心情不错", "心情好，但对这个人保持距离：礼貌、话不多、不主动"
        else:
            if close:
                tag, hint = "平常，偏亲近", "心情一般，对这个人熟悉：正常聊天，语气随意"
            else:
                tag, hint = "平常", "心情一般，对这个人没什么特别：正常回应，不多说"

        if energy < 0.30:
            extra.append("困")
            hint += "；很困，句子会更短更慢"
        if patience < 0.25:
            extra.append("没耐心")
            hint += "；耐心快没了，别被连着问同一件事"

        out = {
            "tag": tag,
            "extra": extra,
            "hint": hint,
            "mood": round(mood, 3),
            "energy": round(energy, 3),
            "patience": round(patience, 3),
            "cling": round(glob["cling"], 3),
            "warmth": round(warmth, 3),
            "bond": round(bond, 3),
            "close": close,
            "special": special,
            "can_share_daily": bond >= BOND_FRIEND,
            "can_share_heart": bond >= BOND_SPECIAL,
            "name": name,
        }
        willing = self.willingness(out)
        out["willing"] = round(willing, 3)
        out["behavior"] = BEHAVIOR[self.behavior_key(willing)]
        out["phrase"] = self.pick_phrase(mood)
        return out

    @staticmethod
    def pick_phrase(mood: float) -> str:
        if mood >= 0.5:
            band = "high"
        elif mood > -0.2:
            band = "mid"
        elif mood > -0.5:
            band = "low"
        else:
            band = "verylow"
        return random.choice(PHRASES[band])

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
                    "bond": att["bond"],
                    "special": att["special"],
                    "patience": att["patience"],
                    "willing": att["willing"],
                    "tag": att["tag"],
                    "talks": int(row.get("talks") or 0),
                    "last_seen": float(row.get("last_seen") or 0),
                }
            )
        users.sort(key=lambda u: (-u["warmth"], -u["last_seen"]))
        guest = self.attitude("", "")
        return {
            "global": {k: round(v, 3) for k, v in glob.items()},
            "overall": self.overall_label(glob),
            "willing": guest["willing"],
            "phrase": self.pick_phrase(glob["mood"]),
            "users": users,
            "events": self.data.get("events", [])[-40:],
            "residuals": self.data.get("residuals", []),
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
