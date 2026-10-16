"""真端口、真 uvicorn 进程的服务令牌冒烟（§4.8.1）：配了令牌之后，业务面真的全要凭据吗？

为什么需要它而不是只信用例：`httpx.ASGITransport` 绕过真实 HTTP 栈——头的大小写归一、
流式响应的写法都不经它，而 §4.8.1 当初的事故正是"配了令牌"与"真的在拦"两件事脱钩。
这条脚本自己起服务、自己收摊，逐条用真 socket 指认。

它**不在 CI 里**（CI 的对应判据是 `tests/api/test_api.py::TestTokenGateCoversWholeApiSurface`，
那条从路由表取路径逐条验）；这份是"真进程真端口"档的手工取证，改了鉴权面后跑一次。
用法：`uv run python scripts/smoke_service_token.py`（占用 8123 端口，退出前一定收摊）。
"""

import os
import threading
import time

os.environ.update(
    {
        "AEGIS_ENV": "dev",
        "AEGIS_BUS_BACKEND": "memory",
        "AEGIS_STORE_BACKEND": "memory",
        "AEGIS_SIMULATOR_ENABLED": "false",
        "AEGIS_DELIVERY_MODE": "mock",
        "AEGIS_ANALYTICS_BACKEND": "off",
        "AEGIS_MCP_ENABLED": "false",
        "AEGIS_SERVICE_TOKEN": "s3cr3t-token",
    }
)

import httpx
import uvicorn

from aegis.api.app import create_app
from aegis.config import get_settings

BASE = "http://127.0.0.1:8123"
PORT = 8123
failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"{'通过' if ok else '不通过'}｜{name}{('：' + extra) if extra else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    server = uvicorn.Server(uvicorn.Config(create_app(get_settings()), host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    else:
        print("服务没起来，冒烟作废")
        return 2
    try:
        with httpx.Client(base_url=BASE, timeout=20.0) as c:
            check("/healthz 不带凭据也放行（容器探针不该带业务令牌）", c.get("/healthz").status_code == 200)
            check("/metrics 同上", c.get("/metrics").status_code == 200)

            bare = c.get("/api/v1/warnings")
            check("数据读端点不带凭据必 401", bare.status_code == 401, f"HTTP {bare.status_code}")
            detail = bare.json().get("detail", {})
            check("401 说清了要什么头", isinstance(detail, dict) and "X-Aegis-Token" in str(detail.get("message", "")), str(detail)[:120])

            token = {"X-Aegis-Token": "s3cr3t-token"}
            bearer = {"authorization": "Bearer s3cr3t-token"}
            for label, headers in (("X-Aegis-Token", token), ("Authorization: Bearer", bearer)):
                status = c.get("/api/v1/warnings", headers=headers).status_code
                check(f"带对凭据（{label}）放行", status == 200, f"HTTP {status}")
            check(
                "错凭据仍被拒",
                c.get("/api/v1/warnings", headers={"X-Aegis-Token": "wrong"}).status_code == 401,
            )

            # 这一组是本轮补上的缺口：以前只有 11 条路由挂了闸，这几条在"配了令牌"的部署里其实谁都能读写
            for method, path in (
                ("GET", "/api/v1/rules"),
                ("GET", "/api/v1/rules/calibration"),
                ("GET", "/api/v1/events"),
                ("GET", "/api/v1/agents"),
                ("GET", "/api/v1/integrations"),
                ("GET", "/api/v1/workflow/definitions"),
                ("GET", "/api/v1/workflow/node-types"),
                ("GET", "/api/v1/assistant/capabilities"),
                ("POST", "/api/v1/reports"),
            ):
                without = c.request(method, path).status_code
                with_token = c.request(method, path, headers=token).status_code
                check(
                    f"{method} {path} 不带凭据 401 / 带凭据放行",
                    without == 401 and with_token != 401,
                    f"{without} / {with_token}",
                )

            # 事件流：浏览器侧改成 fetch 之后才带得动头，这里验它确实带得动
            with c.stream("GET", "/api/v1/events/stream") as stream:
                check("事件流不带凭据 401", stream.status_code == 401, f"HTTP {stream.status_code}")
            with c.stream("GET", "/api/v1/events/stream", headers=token) as stream:
                got_status = stream.status_code
                first = ""
                if got_status == 200:
                    for line in stream.iter_lines():
                        if line.startswith("data:"):
                            first = line
                            break
                check("事件流带凭据可连且能读到帧", got_status == 200 and first.startswith("data:"), f"HTTP {got_status}｜{first[:60]}")
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    print(f"\n[冒烟] {'全部通过' if not failures else '不通过：' + '、'.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
