"""测试公共设施：临时存储、契约构造、结构化日志。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.contractguard.service import ContractGuardService
from app.contractguard.storage import LocalStore

logger = logging.getLogger("contractguard.tests")


def log_case(title: str, *, input_contract: object | None = None,
             expectation: object | None = None, rationale: object | None = None,
             verdict: object | None = None) -> None:
    """统一打印：输入契约 -> 判定依据 -> 最终结论。"""
    logger.info("=" * 72)
    logger.info("测试用例: %s", title)
    if input_contract is not None:
        logger.info("输入契约:\n%s", json.dumps(input_contract, ensure_ascii=False, indent=2, default=str))
    if expectation is not None:
        logger.info("消费者期望:\n%s", json.dumps(expectation, ensure_ascii=False, indent=2, default=str))
    if rationale is not None:
        logger.info("判定依据: %s", rationale)
    if verdict is not None:
        logger.info("最终结论:\n%s", json.dumps(verdict, ensure_ascii=False, indent=2, default=str))


def _obj_field(name, type="string", *, required=False, nullable=False,
               default=... , enum=None, properties=None, item=None):
    f: dict = {"type": type}
    if nullable:
        f["nullable"] = True
    if default is not ...:
        f["default"] = default
    if enum is not None:
        f["enum"] = enum
    if properties is not None:
        f["properties"] = properties
    if item is not None:
        f["items"] = item
    return name, required, f


def make_contract(endpoints: dict) -> dict:
    """用紧凑结构构造类 OpenAPI 契约。

    endpoints: {
      "/users": {
        "GET": {"query": {...}, "responses": {200: schema}, "request": schema},
      }
    }
    """
    paths: dict = {}
    for path, ops in endpoints.items():
        item: dict = {}
        for method, spec in ops.items():
            op: dict = {}
            if "request" in spec and spec["request"] is not None:
                op["requestBody"] = {
                    "required": spec.get("request_required", True),
                    "content": {"application/json": {"schema": spec["request"]}},
                }
            if spec.get("query"):
                op["parameters"] = [
                    {"name": name, "in": "query",
                     "required": bool(q.get("required", False)),
                     "schema": q["schema"]}
                    for name, q in spec["query"].items()
                ]
            op["responses"] = {
                str(status): {"content": {"application/json": {"schema": schema}}}
                if schema is not None else {}
                for status, schema in spec.get("responses", {}).items()
            }
            item[method.lower()] = op
        paths[path] = item
    return {"paths": paths}


def object_schema(*fields, required=None) -> dict:
    """fields: list of (name, required?, schema_dict)"""
    props: dict = {}
    req: list[str] = []
    for entry in fields:
        name = entry[0]
        is_required = entry[1] if len(entry) > 1 else False
        schema = entry[2]
        props[name] = schema
        if is_required:
            req.append(name)
    out = {"type": "object", "properties": props}
    if req or required:
        out["required"] = req + (required or [])
    return out


@pytest.fixture
def tmp_store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def service(tmp_store: LocalStore) -> ContractGuardService:
    return ContractGuardService(tmp_store)
