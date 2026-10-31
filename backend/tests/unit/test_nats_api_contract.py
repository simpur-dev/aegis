"""NATS 接口契约守卫：不连服务器，也能挡住"参数名漂移"这类只有上线才爆的 bug。

做法：对 nats-py 的真实签名做反射断言。若某天库升级改了参数名，
本测试立即失败，而不是等生产环境连上 NATS 才炸。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aegis.bus.nats_bus import (
    _STREAM_MAX_AGE_SECONDS,
    _STREAMS,
    ACK_WAIT_MARGIN_SECONDS,
    DEFAULT_ACK_WAIT_SECONDS,
    DEFAULT_MAX_DELIVER,
    NatsBus,
)


def _params(func: Any) -> set[str]:
    return set(inspect.signature(func).parameters)


class TestNatsApiContract:
    def test_connect_accepts_our_kwargs(self) -> None:
        import nats.aio.client as nats_client

        available = _params(nats_client.Client.connect)
        for used in ("servers", "error_cb", "allow_reconnect", "max_reconnect_attempts"):
            assert used in available, f"nats.connect 不再接受参数 {used}"

    def test_publish_accepts_subject_and_payload(self) -> None:
        from nats.js import JetStreamContext

        available = _params(JetStreamContext.publish)
        assert {"subject", "payload"} <= available

    def test_subscribe_accepts_our_kwargs(self) -> None:
        from nats.js import JetStreamContext

        available = _params(JetStreamContext.subscribe)
        for used in ("subject", "stream", "queue", "durable", "cb", "manual_ack"):
            assert used in available, f"js.subscribe 不再接受参数 {used}（曾用错 durable_name/deliver_group）"

    def test_core_subscribe_accepts_queue_and_cb(self) -> None:
        import nats.aio.client as nats_client

        available = _params(nats_client.Client.subscribe)
        assert {"subject", "queue", "cb"} <= available

    def test_stream_config_fields(self) -> None:
        from nats.js import api as js_api

        fields = set(js_api.StreamConfig.__dataclass_fields__)
        for used in ("name", "subjects", "duplicate_window", "max_msgs"):
            assert used in fields, f"StreamConfig 缺少字段 {used}"

    def test_add_stream_takes_config_positionally(self) -> None:
        from nats.js import JetStreamContext

        parameters = list(inspect.signature(JetStreamContext.add_stream).parameters)
        assert parameters[1] == "config", "add_stream 首参应为 StreamConfig"

    def test_every_callback_handed_to_nats_is_a_coroutine(self) -> None:
        """nats-py 在 connect()/subscribe 里就校验回调必须是协程，同步函数直接抛 InvalidCallbackTypeError。

        这条是今晚被真实 NATS 服务器打出来的：替身客户端从不校验，所以"连不上总线"
        只在接上真服务的那一刻发生，而那时降级路径会把它盖成一行状态。
        """
        source = Path(inspect.getsourcefile(NatsBus) or "").read_text(encoding="utf-8")
        handed = set(re.findall(r"\b\w*cb=(?:self\.)?([A-Za-z_]\w*)", source))
        assert handed, "nats_bus.py 不再向 nats 传回调：这条守卫要看住的东西已经消失，请连同它一起处理"
        for name in sorted(handed):
            target = getattr(NatsBus, name, None)
            if target is None:
                # 方法内的局部闭包（`async def _cb`）：只能按定义处判定它是不是协程。
                assert re.search(rf"\n\s*async def {name}\(", source), f"{name} 不是协程回调"
                continue
            assert inspect.iscoroutinefunction(target), f"NatsBus.{name} 必须是协程，否则真实连接当场失败"

    def test_duplicate_window_reaches_the_server_as_seconds_scaled_to_nanoseconds(self) -> None:
        """nats-py 把 `duplicate_window` 当**秒**再乘 1e9 上线；传纳秒会被乘第二次。

        今晚被真实 JetStream 打出来：120e9 变成 1.2e20，超出 int64，服务端对建流请求
        直接回 `code=400 err_code=10025 invalid JSON`——而旧代码把这个错咽在 debug 里。
        """
        from nats.js import api as js_api

        from aegis.bus.nats_bus import DUPLICATE_WINDOW_SECONDS

        wire = js_api.StreamConfig(name="X", subjects=["a.>"], duplicate_window=DUPLICATE_WINDOW_SECONDS).as_dict()["duplicate_window"]
        assert wire == DUPLICATE_WINDOW_SECONDS * 1_000_000_000
        assert wire <= 2**63 - 1, "换算后超出 int64：整个建流请求会被服务端判为 invalid JSON"

    async def test_stream_creation_failure_fails_loudly_instead_of_reporting_connected(self, monkeypatch) -> None:
        import nats as nats_package
        from nats.js.errors import APIError

        from aegis.errors import BusNotReadyError

        closed: list[bool] = []

        class _Js:
            async def add_stream(self, _config: Any) -> Any:
                raise APIError(code=400, err_code=10025, description="invalid JSON")

        class _Client:
            def jetstream(self) -> Any:
                return _Js()

            async def close(self) -> None:
                closed.append(True)

        async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
            return _Client()

        monkeypatch.setattr(nats_package, "connect", fake_connect)
        bus = NatsBus("nats://127.0.0.1:4222", stream_prefix="AEGIST")

        with pytest.raises(BusNotReadyError, match="创建失败"):
            await bus.connect()

        assert bus.connected is False, "建流失败却把总线标成可用，等于让平台带着一台发不出消息的总线继续跑"
        assert closed == [True], "失败路径要把已建立的连接收掉，别留半死状态"

    async def test_existing_stream_is_upgraded_instead_of_failing(self, monkeypatch) -> None:
        """既存流的 subject 集合变了（新增本体收件箱）时，连接应把流升级到新配置而不是拒启。"""
        import nats as nats_package
        from nats.js.errors import APIError

        from aegis.bus.nats_bus import _STREAMS

        updated: list[Any] = []

        class _Js:
            async def add_stream(self, _config: Any) -> Any:
                raise APIError(code=400, err_code=10058, description="stream name already in use")

            async def update_stream(self, config: Any) -> Any:
                updated.append(config)
                return config

        class _Client:
            def jetstream(self) -> Any:
                return _Js()

            async def close(self) -> None:
                return None

        async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
            return _Client()

        monkeypatch.setattr(nats_package, "connect", fake_connect)
        bus = NatsBus(stream_prefix="AEGIST")
        await bus.connect()
        try:
            assert bus.connected is True
            assert len(updated) == len(_STREAMS), "每个流都要按新配置对账，而不是只处理报错的那一个"
            agent_stream = next(config for config in updated if config.name == "AEGIST_AGENT")
            assert "agent.*.in.>" in agent_stream.subjects
        finally:
            await bus.close()

    async def test_durable_subscription_names_the_consumer_once(self, monkeypatch) -> None:
        """nats-py 把 `queue` 当消费者名：queue 与 durable 都给且不等会被当场拒绝。

        真实服务器上表现为 `cannot create queue subscription 'aegis-gateway' to consumer
        'cg_gateway_perceive'`——网关的持久订阅一条都建不起来，而替身总线照单全收。
        """
        import nats as nats_package
        from nats.js.errors import NotFoundError

        captured: dict[str, Any] = {}
        created: list[Any] = []

        class _Sub:
            async def drain(self) -> None:
                return None

        class _Js:
            async def add_stream(self, config: Any) -> Any:
                return config

            async def consumer_info(self, _stream: str, _consumer: str) -> Any:
                raise NotFoundError

            async def add_consumer(self, _stream: str, config: Any = None, **_kwargs: Any) -> Any:
                created.append(config)
                return config

            async def subscribe(self, subject: str, **kwargs: Any) -> Any:
                captured.update(kwargs)
                captured["subject"] = subject
                return _Sub()

        class _Client:
            def jetstream(self) -> Any:
                return _Js()

            def new_inbox(self) -> str:
                return "_INBOX.contract"

            async def close(self) -> None:
                return None

            async def subscribe(self, *_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("durable 订阅不该退到核心 subscribe")

        async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
            return _Client()

        monkeypatch.setattr(nats_package, "connect", fake_connect)
        bus = NatsBus(stream_prefix="AEGIST")
        await bus.connect()

        async def _sink(_raw: bytes) -> None:
            return None

        await bus._subscribe_raw("agent.perceive.out", _sink, queue="cg_gateway", durable="cg_perceive")

        assert captured["subject"] == "agent.perceive.out"
        assert captured["stream"] == "AEGIST_AGENT"
        assert captured["queue"] == captured["durable"] == "cg_perceive", "队列组名必须沿用消费者名，否则 nats-py 直接拒绝"
        assert created[0].ack_wait == DEFAULT_ACK_WAIT_SECONDS, "新建消费者必须带上平台回执窗口，而不是服务端默认 30s"
        assert created[0].max_deliver == DEFAULT_MAX_DELIVER, "毒消息必须有有限重投上限"


class TestStreamRouting:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("data.rg01.rain_10min", "AEGIS_DATA"),
            ("agent.assess.in", "AEGIS_AGENT"),
            ("agent.assess.in.assess.real01", "AEGIS_AGENT"),
            ("agent.assess.out", "AEGIS_AGENT"),
            ("agent.plan.hb", "AEGIS_AGENT"),
            ("workflow.wfi_0001.started", "AEGIS_WF"),
            ("ops.feedback.status", "AEGIS_OPS"),
            ("platform.alert.1.540121", "AEGIS_OPS"),
            ("reply.gw00000001.inbox", "AEGIS_REPLY"),
            ("other.thing.here", None),
        ],
    )
    def test_stream_for(self, subject: str, expected: str | None) -> None:
        assert NatsBus(stream_prefix="AEGIS").stream_for(subject) == expected

    def test_durable_without_stream_is_refused(self) -> None:
        bus = NatsBus()
        assert bus.stream_for("nobody.owns.this") is None

    async def test_operations_require_connection(self) -> None:
        from aegis.domain.messages import make_event
        from aegis.errors import BusNotReadyError

        bus = NatsBus()
        message = make_event(source="perceive.t01", action="perceive.anomaly", payload={}, trace_id="trc_" + "0" * 16)
        with pytest.raises(BusNotReadyError):
            await bus.publish("data.rg01.rain", message)


async def _sink(_raw: bytes) -> None:
    return None


class _Sub:
    async def drain(self) -> None:
        return None


def _install_fake_js(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: Any | None = None,
    add_error: Exception | None = None,
    **bus_kwargs: Any,
) -> SimpleNamespace:
    """只实现"建流 + 消费者对账 + 绑定"所需接口的 JetStream 替身。"""
    import nats as nats_package
    from nats.js.errors import NotFoundError

    streams: list[Any] = []
    consumers: list[Any] = []
    bound: dict[str, Any] = {}

    class _Js:
        async def add_stream(self, config: Any) -> Any:
            streams.append(config)
            return config

        async def consumer_info(self, _stream: str, _consumer: str) -> Any:
            if existing is None:
                raise NotFoundError
            return SimpleNamespace(config=existing)

        async def add_consumer(self, _stream: str, config: Any = None, **_kwargs: Any) -> Any:
            if add_error is not None:
                raise add_error
            consumers.append(config)
            return config

        async def subscribe(self, subject: str, **kwargs: Any) -> Any:
            bound.update(kwargs)
            bound["subject"] = subject
            return _Sub()

    class _Client:
        def jetstream(self) -> Any:
            return _Js()

        def new_inbox(self) -> str:
            return "_INBOX.fake"

        async def close(self) -> None:
            return None

    async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return _Client()

    monkeypatch.setattr(nats_package, "connect", fake_connect)
    return SimpleNamespace(bus=NatsBus(stream_prefix="AEGIST", **bus_kwargs), streams=streams, consumers=consumers, bound=bound)


class TestJetStreamRetentionAndRedelivery:
    def test_every_stream_has_a_bounded_retention(self) -> None:
        assert set(_STREAM_MAX_AGE_SECONDS) == {suffix for suffix, _patterns in _STREAMS}, "每个流都必须显式给保留期，缺一个就是无限保留"
        assert all(age > 0 for age in _STREAM_MAX_AGE_SECONDS.values())
        for suffix in ("AGENT", "REPLY"):
            assert _STREAM_MAX_AGE_SECONDS[suffix] <= 24 * 3600, f"{suffix} 流留得太久：过期请求重放会让智能体执行早已超时的动作"

    async def test_streams_are_created_with_their_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install_fake_js(monkeypatch)
        await fake.bus.connect()
        try:
            ages = {config.name.removeprefix("AEGIST_"): config.max_age for config in fake.streams}
            assert ages == _STREAM_MAX_AGE_SECONDS
        finally:
            await fake.bus.close()

    async def test_new_consumer_is_created_with_the_platform_ack_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install_fake_js(monkeypatch, ack_wait_seconds=71.5, max_deliver=3)
        await fake.bus.connect()
        try:
            await fake.bus._subscribe_raw("agent.perceive.out", _sink, queue="cg", durable="d_perceive")
        finally:
            await fake.bus.close()

        assert len(fake.consumers) == 1
        config = fake.consumers[0]
        assert config.ack_wait == pytest.approx(71.5)
        assert config.max_deliver == 3
        assert config.deliver_group == "d_perceive"
        assert config.filter_subject == "agent.perceive.out"
        assert config.deliver_subject == "_INBOX.fake", "push 消费者必须带投递 subject"
        assert fake.bound["config"] is config, "绑定必须用对账后的配置，而不是让 nats-py 自行取服务端默认"

    async def test_queue_less_durable_gets_no_delivery_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """带组却没按队列订阅（或反之）会被 nats-py 当场拒绝——真机上刚踩过。"""
        fake = _install_fake_js(monkeypatch)
        await fake.bus.connect()
        try:
            await fake.bus._subscribe_raw("agent.perceive.out", _sink, durable="d_perceive")
        finally:
            await fake.bus.close()

        assert fake.consumers[0].deliver_group is None
        assert fake.bound["queue"] == ""

    async def test_existing_consumer_is_upgraded_without_moving_its_delivery_subject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nats.js import api as js_api

        existing = js_api.ConsumerConfig(
            durable_name="d_perceive",
            deliver_group="d_perceive",
            deliver_subject="_INBOX.legacy",
            filter_subject="agent.perceive.out",
            ack_wait=30.0,
            max_deliver=-1,
        )
        fake = _install_fake_js(monkeypatch, existing=existing, ack_wait_seconds=71.5, max_deliver=3)
        await fake.bus.connect()
        try:
            await fake.bus._subscribe_raw("agent.perceive.out", _sink, queue="cg", durable="d_perceive")
        finally:
            await fake.bus.close()

        assert len(fake.consumers) == 1
        upgraded = fake.consumers[0]
        assert upgraded.ack_wait == pytest.approx(71.5)
        assert upgraded.max_deliver == 3
        assert upgraded.deliver_subject == "_INBOX.legacy", "换投递 subject 会把已绑在它上面的其他实例踢下线"
        assert fake.bound["config"].ack_wait == pytest.approx(71.5)

    async def test_matching_consumer_is_not_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nats.js import api as js_api

        existing = js_api.ConsumerConfig(
            durable_name="d_perceive",
            deliver_group="d_perceive",
            deliver_subject="_INBOX.legacy",
            filter_subject="agent.perceive.out",
            ack_wait=71.5,
            max_deliver=3,
        )
        fake = _install_fake_js(monkeypatch, existing=existing, ack_wait_seconds=71.5, max_deliver=3)
        await fake.bus.connect()
        try:
            await fake.bus._subscribe_raw("agent.perceive.out", _sink, queue="cg", durable="d_perceive")
        finally:
            await fake.bus.close()

        assert fake.consumers == [], "配置已对齐时不应每次连接都重写服务端消费者"

    async def test_upgrade_failure_is_reported_instead_of_binding_the_old_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nats.js import api as js_api

        from aegis.errors import BusNotReadyError

        existing = js_api.ConsumerConfig(
            durable_name="d_perceive",
            deliver_group="d_perceive",
            deliver_subject="_INBOX.legacy",
            filter_subject="agent.perceive.out",
            ack_wait=30.0,
            max_deliver=-1,
        )
        fake = _install_fake_js(
            monkeypatch, existing=existing, add_error=RuntimeError("update rejected"), ack_wait_seconds=71.5, max_deliver=3
        )
        await fake.bus.connect()
        try:
            with pytest.raises(BusNotReadyError, match="升级失败"):
                await fake.bus._subscribe_raw("agent.perceive.out", _sink, queue="cg", durable="d_perceive")
        finally:
            await fake.bus.close()

        assert fake.bound == {}, "升级失败时不得回落到旧回执窗口继续绑定"

    def test_ack_wait_covers_the_agent_transaction_budget(self) -> None:
        from aegis.config import Settings
        from aegis.integrations.channels import build_transport

        settings = Settings(env="test", bus_backend="nats", sla_agent_txn_ms=42_000)
        bus = build_transport(settings)
        assert isinstance(bus, NatsBus)
        assert bus.ack_wait_seconds == pytest.approx(42.0 + ACK_WAIT_MARGIN_SECONDS)
        assert bus.ack_wait_seconds > settings.sla_agent_txn_ms / 1000.0, "回执窗口短于事务预算＝服务端会在智能体仍在处理时重投"

    def test_defaults_cover_the_default_transaction_budget(self) -> None:
        from aegis.config import Settings

        assert Settings(env="test").sla_agent_txn_ms / 1000.0 < DEFAULT_ACK_WAIT_SECONDS
        assert 0 < DEFAULT_MAX_DELIVER < 100
