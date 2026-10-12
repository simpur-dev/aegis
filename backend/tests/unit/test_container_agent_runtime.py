"""`AEGIS_AGENT_RUNTIME` 三态与互斥：real 与 mock 是同一订阅面上的两副身子，不许同时挂。

容器起的不是"对象列表"而是【谁在应答 agent_in】——所以这里的断言都落在互斥事实上：
开了 real 就没有 mock（哪怕调用方按 dev 习惯传了 with_mock_agents=True），
off 就两副都不起，而 real 模式下 LLM/解析器缺位时内环自己降级、装配点不塞替身。
总线用内存后端，全程离线：这四副真内环只被构造与注册，没发过一条请求，也就没花一分钱。
"""

from __future__ import annotations

from typing import Any

from aegis.container import create_container


async def boot(settings: Any, **start_kw: Any) -> Any:
    container = create_container(settings, with_simulator=False)
    await container.start(**start_kw)
    return container


def typed(agents: Any) -> set[str]:
    return {agent.agent_type.value for agent in agents}


async def test_real内环拿到的是容器那一份共享记忆门面(settings: Any) -> None:
    """§4.3 的闭环靠的就是这一行注入：装配点漏了 `memory=`，`services/memory.py` 写得再全
    也只是死代码——assess 的读写腿与 feedback 的教训写入全部走 `if self.memory is not None`，
    于是"共享记忆"这句话只在测试里成立（2026-10-12 对盘实测就是这个状态）。"""
    container = await boot(settings.model_copy(update={"agent_runtime": "real"}))
    try:
        assert container.memory is not None, "容器上连门面都没有"
        by_type = {agent.agent_type.value: agent for agent in container.real_agents}
        assert by_type["assess"].memory is container.memory, "研判读不到历史、也写不回共享命名空间"
        assert by_type["feedback"].memory is container.memory, "效果验证的教训没地方落"
        assert not hasattr(by_type["plan"], "memory"), "plan 不是记忆消费者（§3.3 只读历史）：别把它顺手接上凑数"
    finally:
        await container.shutdown()


async def test_real模式压下mock且四副真内环上总线(settings: Any) -> None:
    container = await boot(settings.model_copy(update={"agent_runtime": "real"}), with_mock_agents=True)
    try:
        assert container.mock_agents == [], "real 与 mock 同挂 agent_in 会抢答同一批请求"
        assert len(container.real_agents) == 5
        assert typed(container.real_agents) == {"perceive", "assess", "plan", "execute", "feedback"}
        assert "real" in container.agent_runtime_state and "mock" in container.agent_runtime_state
    finally:
        await container.shutdown()


async def test_real模式不写压制话术当调用方本就没要mock(settings: Any) -> None:
    container = await boot(settings.model_copy(update={"agent_runtime": "real"}))
    try:
        assert container.agent_runtime_state == "real"
        assert container.mock_agents == [] and len(container.real_agents) == 5
    finally:
        await container.shutdown()


async def test_mock是默认且行为与升级前一致(settings: Any) -> None:
    assert settings.agent_runtime == "mock", "默认值改了就是改了启动形态，得有人负责"
    container = await boot(settings, with_mock_agents=True)
    try:
        assert container.mock_agents, "默认仍是联调替身"
        assert container.real_agents == []
        assert container.agent_runtime_state == "mock"
    finally:
        await container.shutdown()


async def test_off模式两副都不起(settings: Any) -> None:
    container = await boot(settings.model_copy(update={"agent_runtime": "off"}), with_mock_agents=True)
    try:
        assert container.mock_agents == [] and container.real_agents == []
        assert container.agent_runtime_state == "off"
    finally:
        await container.shutdown()


async def test_停止后不留悬挂的智能体(settings: Any) -> None:
    container = await boot(settings.model_copy(update={"agent_runtime": "real"}))
    assert len(container.real_agents) == 5
    await container.shutdown()
    assert container.real_agents == [], "实例清单没清就等于心跳还在总线上飘"
    await container.shutdown()  # 重复 stop 必须幂等：shutdown 路径上会被叫两次
