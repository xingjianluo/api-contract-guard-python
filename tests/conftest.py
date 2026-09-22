"""pytest 公共夹具。

单测日志必须打印：输入契约、判定依据、最终结论，
因此提供统一的记录工具，失败时随断言输出一起呈现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_store(tmp_path: Path) -> Path:
    store = tmp_path / "guard-data"
    store.mkdir()
    return store


def dump(label: str, payload: object) -> str:
    """把输入契约 / 报告渲染为稳定 JSON 文本，供日志与断言使用。"""
    return f"===== {label} =====\n{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)}\n"
