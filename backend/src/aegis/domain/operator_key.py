"""按人令牌：席位的**可验证凭据**，与声明身份分开。

与 `domain/operator.py` 的席位是两种东西，别混：
- **席位**（`X-Aegis-Operator`）是"我说是谁"——任何人都能声明自己是 `admin`，P0–P3 一直如此；
- **按人令牌**（`X-Aegis-Operator-Key`）是"我能证明我是谁"——只有持证人能签发的那把钥匙。

落库只存 SHA-256 摘要：库被读走也拿不到可用凭据。明文只在签发那一次响应里出现，
之后任何读路径都不返回它——连摘要也不返回（摘要被离线爆破并不等于能反查明文，
但没必要把靶子递出去）。

为什么单独一个模块而不是塞进 `Operator`：席位的字段是"谁在班上、什么角色"，
令牌的字段是"哪把钥匙、何时用过、还锁着没有"。两者的生命周期与失效条件都不同，
混在一个模型里就会出现"改角色顺手把令牌删了"这种说不清的行为。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: 明文令牌前缀：便于人工识别与批量吊销，也让日志里的凭据一眼可辨（而不必真的记下来）。
KEY_PREFIX = "aegis_op_"

#: 摘要的形状：SHA-256 的小写 hex。库里用它做 CHECK 约束，防止有人塞进明文。
KEY_HASH_PATTERN = r"^[0-9a-f]{64}$"

#: `key_id` 的形状（与 `control_actions.action_id` 同一族命名法）。
KEY_ID_PATTERN = r"^ok_[0-9a-f]{12,24}$"


class OperatorKey(BaseModel):
    """一把按人令牌的**元数据**。

    注意这里没有 `key_hash` 字段：读接口只给这把钥匙的"存在、归属、何时用过"，
    摘要与明文都不在这条路上（`persistence/operator_keys.py` 里读路径显式剔除）。
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(pattern=KEY_ID_PATTERN)
    operator_id: str
    active: bool = True
    created_at: str | None = None
    last_used_at: str | None = None
