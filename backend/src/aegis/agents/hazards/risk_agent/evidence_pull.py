"""研判证据的**拉取腿**：证据没随请求推送时，按数据 API 地址自己取。

推送腿是进程内装配的正常路径（`AssessRequest.evidence`）；本模块服务于外部实现
**出进程部署**时的场景，因此只依赖 stdlib（`urllib`），不引入 HTTP 客户端依赖。

口径：
- 未配置 `AEGIS_EXT_DATA_API_URL` → 返回 `None`（没有拉取腿，由调用方诚实拒绝）；
- 任一步失败（网络、HTTP 状态、非法 JSON、不合契约）→ 返回 `None` 并记 warning，
  **绝不返回半份证据**——半份证据会一路走到"数据不足"，让配置错误看起来像数据问题。
- 响应必须整体通过 `AssessEvidence.model_validate`：契约从严，不在拉取层做字段裁剪。
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aegis.domain.assess import REGION_PATTERN, WINDOW_PATTERN, AssessEvidence

log = logging.getLogger("aegis.agents.hazards.evidence_pull")

#: 环境变量名（新名；旧名 `AEGIS_EXT_DATA_API_*` 仍兼容读取）。
DATA_API_URL_ENV = "AEGIS_HAZARD_DATA_API_URL"
DATA_API_TOKEN_ENV = "AEGIS_HAZARD_DATA_API_TOKEN"
_LEGACY_DATA_API_URL_ENV = "AEGIS_EXT_DATA_API_URL"
_LEGACY_DATA_API_TOKEN_ENV = "AEGIS_EXT_DATA_API_TOKEN"
PULL_TIMEOUT_SECONDS = 2.0


def pull_evidence(region: str, window: str) -> AssessEvidence | None:
    """按数据 API 拉取证据包；未配置或失败返回 `None`。"""
    import re

    if not re.fullmatch(REGION_PATTERN, region or "") or not re.fullmatch(WINDOW_PATTERN, window or ""):
        return None
    base = os.environ.get(DATA_API_URL_ENV, "").strip() or os.environ.get(_LEGACY_DATA_API_URL_ENV, "").strip()
    if not base:
        return None
    window_minutes = int(window[:-1]) * (60 if window.endswith("h") else 1)
    url = f"{base.rstrip('/')}/api/v1/assess/evidence?{urlencode({'region_code': region, 'window_minutes': window_minutes})}"
    headers = {"Accept": "application/json"}
    token = os.environ.get(DATA_API_TOKEN_ENV, "").strip() or os.environ.get(_LEGACY_DATA_API_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=PULL_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        evidence = AssessEvidence.model_validate(payload)
    except Exception as exc:  # 拉取腿的任何失败都等价于"没有证据"，原因写日志供运维定位
        log.warning("证据拉取失败", extra={"url": url, "err": f"{type(exc).__name__}: {exc}"[:200]})
        return None
    if evidence.region_code != region:
        log.warning("证据拉取区域不一致", extra={"expected": region, "got": evidence.region_code})
        return None
    return evidence


__all__ = ["DATA_API_TOKEN_ENV", "DATA_API_URL_ENV", "pull_evidence"]
