"""MQTT 推送腿（站端 publish、平台 subscribe）：没装 [iot] extra 时给出一行 mqtt-unavailable 事实。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from aegis.config import Settings, get_settings
from aegis.connectors.mqtt import AiomqttClient, MqttSource  # MQTT 只在此装配层被 import；aiomqtt 本身延迟到建连时导入
from aegis.integrations.state import IntegrationState


def build_mqtt(settings: Settings | None = None) -> tuple[MqttSource | None, IntegrationState]:
    """装配 MQTT 推送腿（站端主动 publish、平台 subscribe）——接入腿里唯一不需要轮询的一条。

    未开启时链路里完全不出现这一层；开了但 `[iot]` extra 没装，则给出 `mqtt-unavailable`
    这一行事实。运维必须看到"这条腿没起来 + 为什么"，而不是"站端一直在发、平台这边查不到数"。
    凭据只进连接参数，绝不进状态面。
    """
    cfg = settings or get_settings()
    if not cfg.mqtt_enabled:
        return None, IntegrationState(name="mqtt", enabled=False, driver="off")

    try:
        import aiomqtt  # noqa: F401  只做能力探测：真正建连发生在后台订阅任务里
    except ImportError as exc:
        return None, IntegrationState(
            name="mqtt",
            enabled=False,
            driver="mqtt-unavailable",
            detail={
                "broker": f"{cfg.mqtt_host}:{cfg.mqtt_port}",
                "reason": f"缺少 aiomqtt（[iot] extra）: {type(exc).__name__}",
            },
        )

    client = AiomqttClient(
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        topic_prefix=cfg.mqtt_topic_prefix,
        username=cfg.mqtt_username,
        password=cfg.mqtt_password,
        qos=cfg.mqtt_qos,
        keepalive_seconds=cfg.mqtt_keepalive_seconds,
        client_id=cfg.mqtt_client_id,
        incoming_queue_limit=cfg.mqtt_buffer_limit,
    )
    detail: dict[str, object] = {**client.status(), "buffer_limit": cfg.mqtt_buffer_limit}
    source = MqttSource(client, prefix=cfg.mqtt_topic_prefix, buffer_limit=cfg.mqtt_buffer_limit)
    return source, IntegrationState(name="mqtt", enabled=True, driver="mqtt", detail=detail)
