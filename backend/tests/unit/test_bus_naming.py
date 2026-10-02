"""JetStream 名字规则：与 nats-py **自己的**校验函数对账，而不是抄它的字符集。

真事故：`AEGIS_BUS_BACKEND=nats` 时应用启动失败，
`ValueError: nats: invalid consumer name: 'd_perceive_perceive.mock01'`——
durable 名里带了 `agent_id`，而 JetStream 消费者名不许有点。内存总线不校验，
所以这条只在部署形态出现（见 `aegis/bus/naming.py` 头注释）。

这里刻意 import 上游的 `_validate_consumer_name`：它改了规则我们就红，
而不是继续用自己的正则"自我证明合法"。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

# nats-py 没把这个校验函数放进公开面，但它就是服务端接受与否的实际判据。
from nats.js.manager import _INVALID_NAME_CHARS, _validate_consumer_name

from aegis.bus.naming import consumer_name

INSTANCE_IDS = (
    "perceive.mock01",  # 点：真事故里的那个
    "assess/edge-01",
    "plan*",
    "reach>1",
    "gateway node",
    "hz:8222",
    "站点-01",  # 非 ASCII：一律替换，名字要能安全进面板与日志
)


class TestAgainstUpstreamValidator:
    @pytest.mark.parametrize("instance_id", INSTANCE_IDS)
    def test_任何实例号拼出来的名字都能过服务端校验(self, instance_id: str) -> None:
        name = consumer_name("d", "perceive", instance_id)
        _validate_consumer_name(name)  # 不抛就是合法；抛了就是这条规则没盖住
        assert name == name.strip()

    def test_上游校验的判据确实比我们要松_所以我们的字符集不是白收紧的(self) -> None:
        # 万一哪天 _INVALID_NAME_CHARS 被清空（等于不校验），这条会提醒我们
        # "名字仍要进监控面板"这个理由还在不在。
        assert {" ", ".", "*", ">"} <= set(_INVALID_NAME_CHARS)


class TestDeterminismAndCollisions:
    def test_已经合法的名字原样保留(self) -> None:
        assert consumer_name("cg_gateway", "assess") == "cg_gateway_assess"
        assert consumer_name("d", "plan", "node-7") == "d_plan_node-7"

    def test_改写过的名字带哈希_不同原名不会撞进同一个消费者(self) -> None:
        # `a.b` 与 `a_b` 若都变成 `a_b`，两者会共享服务端投递状态：比启动失败更难查。
        assert consumer_name("d", "a", "a.b") != consumer_name("d", "a", "a_b")

    def test_同一输入跨调用稳定(self) -> None:
        first = consumer_name("d", "assess", "assess.edge-01")
        assert first == consumer_name("d", "assess", "assess.edge-01")

    def test_空片段被丢掉_全空时给一个非空名字(self) -> None:
        assert consumer_name("d", "", "x") == "d_x"
        # 空名字在 nats-py 里等价于"不做 durable 订阅"，静默降级成无状态订阅不可接受。
        assert consumer_name("", "") == "anon"
        _validate_consumer_name(consumer_name("", ""))

    def test_全是非法字符时仍给出合法且可区分的名字(self) -> None:
        assert consumer_name("...", "///") != consumer_name("...", r"\\\ ")
        _validate_consumer_name(consumer_name("...", "///"))

    def test_超长名字被截断但仍带哈希且合法(self) -> None:
        name = consumer_name("d", "assess", "x" * 300)
        assert len(name) <= 96
        _validate_consumer_name(name)


class TestCallSitesUseTheRule:
    """两个 durable 名的生产点必须真的**解析得到**这条规则。

    只看 `co_names` 里有没有 `consumer_name` 是不够的：漏写 import 时字节码里照样有那个名字，
    运行时才炸 `NameError`——真发生过一次（2026-10-02，`aegis/bus/gateway.py` 里加了调用点
    忘了加 import，内存总线形态的单测全绿，部署形态一起步就是启动失败）。
    """

    @staticmethod
    def _assert_resolves(func: Callable[..., object]) -> None:
        # KeyError 就是"这个模块没 import"；`is` 不成立就是"规则有了第二份真源"。两种都要响。
        assert func.__globals__["consumer_name"] is consumer_name

    def test_智能体订阅的名字规则解析得到(self) -> None:
        from aegis.agents.mock import MockAgent

        self._assert_resolves(MockAgent.start)

    def test_网关订阅的名字规则解析得到(self) -> None:
        from aegis.bus.gateway import AgentGateway

        self._assert_resolves(AgentGateway.start)

    def test_两个订阅点都真的调用了它(self) -> None:
        from aegis.agents.mock import MockAgent
        from aegis.bus.gateway import AgentGateway

        assert "consumer_name" in MockAgent.start.__code__.co_names
        assert "consumer_name" in AgentGateway.start.__code__.co_names
