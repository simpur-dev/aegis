"""外部降水取件脚本的**离线**部分：不联网也能验的那些事。

要钉住的：
- 待取清单按 `--files`／`--years` 生成对不对（daily 按年切、annual/monthly 各一个）；
- **幂等**：已在盘且 sha256 一致的文件直接跳过，不重复下载；
- **完整性**：落地写 `sha256sums.json`，`--verify-only` 只认这份清单；
- **失败要说人话**：一条通道失败自动换下一条；全失败时报"所有通道都失败"而不是静默跳过。

取件本体（FTP/HTTP）用替身通道跑，不触网——真取件是运维动作，不是门禁。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.fetch_precip_chmpre import (
    Artifact,
    Channel,
    build_artifacts,
    fetch_all,
    read_manifest,
    verify,
    write_manifest,
)


def _minimal_hdf5() -> bytes:
    """h5py 产出一个能打开的最小 HDF5 文件字节。"""
    import io

    import h5py

    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as handle:
        handle.attrs["aegis_test"] = True
    return buffer.getvalue()


class _FakeChannel(Channel):
    """替身通道：按内容写盘，并记录被取了几次。"""

    name = "fake"

    def __init__(self, payload: bytes | None = None, *, fail: bool = False) -> None:
        # 默认 payload 用**最小合法 HDF5**：skip_existing 的判据是 h5py 结构完整，
        # 替身写出来的"已在盘"文件也必须过这道闸（否则幂等语义测不了）。
        self.payload = payload if payload is not None else _minimal_hdf5()
        self.fail = fail
        self.calls: list[str] = []

    def fetch(self, artifact: Artifact, dest: Path, *, on_progress=None) -> None:
        self.calls.append(artifact.dest_rel)
        if self.fail:
            raise RuntimeError("替身通道故意失败")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        if on_progress is not None:
            on_progress(len(self.payload))


class _FailingChannel(Channel):
    name = "failing"

    def fetch(self, artifact: Artifact, dest: Path, *, on_progress=None) -> None:
        raise RuntimeError("这条通道不可用")


def test_待取清单按分组与年份生成() -> None:
    artifacts = build_artifacts(["annual", "daily"], [1991, 1992, 1993])
    names = {artifact.dest_rel for artifact in artifacts}
    assert names == {
        "annual/CHM_PRE_V2_annual.nc",
        "daily/CHM_PRE_V2_daily_1991.nc",
        "daily/CHM_PRE_V2_daily_1992.nc",
        "daily/CHM_PRE_V2_daily_1993.nc",
    }


def test_未知分组与空清单都报错() -> None:
    with pytest.raises(SystemExit):
        build_artifacts(["yearly"], [])
    with pytest.raises(SystemExit):
        build_artifacts([], [])


def test_取件落盘并写校验和清单(tmp_path: Path) -> None:
    channel = _FakeChannel()
    digests = fetch_all(tmp_path, build_artifacts(["annual"], []), [channel])

    manifest = json.loads((tmp_path / "sha256sums.json").read_text(encoding="utf-8"))
    assert manifest == digests
    assert set(manifest) == {"annual/CHM_PRE_V2_annual.nc"}
    assert len(manifest["annual/CHM_PRE_V2_annual.nc"]) == 64


def test_已在盘的文件不重取(tmp_path: Path) -> None:
    channel = _FakeChannel()
    artifacts = build_artifacts(["annual"], [])
    fetch_all(tmp_path, artifacts, [channel])
    assert channel.calls == ["annual/CHM_PRE_V2_annual.nc"]

    second = _FakeChannel()
    fetch_all(tmp_path, artifacts, [second])
    assert second.calls == [], "幂等：已在盘**且结构完整**的文件不该重取"

    # 截断文件（上次断连的残留）不得按 sha256 记入清单——h5py 打不开就重下
    magic_head = bytes([0x89]) + b"HDF" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
    truncated_bytes = magic_head + b"truncated"
    (tmp_path / "annual" / "CHM_PRE_V2_annual.nc").write_bytes(truncated_bytes)
    third = _FakeChannel()
    fetch_all(tmp_path, artifacts, [third])
    assert third.calls == ["annual/CHM_PRE_V2_annual.nc"], "截断残留必须重取（不能给损坏数据发真值证书）"


def test_force_可以强制重取(tmp_path: Path) -> None:
    fetch_all(tmp_path, build_artifacts(["annual"], []), [_FakeChannel()])
    again = _FakeChannel()
    fetch_all(tmp_path, build_artifacts(["annual"], []), [again], skip_existing=False)
    assert again.calls == ["annual/CHM_PRE_V2_annual.nc"]


def test_一条通道失败自动换下一条(tmp_path: Path) -> None:
    good = _FakeChannel()
    digests = fetch_all(tmp_path, build_artifacts(["annual"], []), [_FailingChannel(), good])
    assert good.calls == ["annual/CHM_PRE_V2_annual.nc"]
    assert digests


def test_所有通道都失败时报错(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="所有通道都失败"):
        fetch_all(tmp_path, build_artifacts(["annual"], []), [_FailingChannel(), _FailingChannel()])


def test_离线校验认清单不认盘(tmp_path: Path) -> None:
    artifacts = build_artifacts(["annual"], [])
    fetch_all(tmp_path, artifacts, [_FakeChannel()])
    assert verify(tmp_path, artifacts) == []

    # 盘上文件被改过 → 校验必须红
    (tmp_path / "annual" / "CHM_PRE_V2_annual.nc").write_bytes(b"tampered")
    problems = verify(tmp_path, artifacts)
    assert problems and "sha256 不一致" in problems[0]


def test_清单缺条目也报错(tmp_path: Path) -> None:
    artifacts = build_artifacts(["annual"], [])
    fetch_all(tmp_path, artifacts, [_FakeChannel()])
    write_manifest(tmp_path, {})
    problems = verify(tmp_path, artifacts)
    assert problems and "不在 sha256sums.json 里" in problems[0]


def test_缺文件也报错(tmp_path: Path) -> None:
    artifacts = build_artifacts(["annual"], [])
    problems = verify(tmp_path, artifacts)
    assert problems and "缺失" in problems[0]


def test_清单读写往返(tmp_path: Path) -> None:
    write_manifest(tmp_path, {"a/x.nc": "0" * 64, "b/y.nc": "1" * 64})
    assert read_manifest(tmp_path) == {"a/x.nc": "0" * 64, "b/y.nc": "1" * 64}
