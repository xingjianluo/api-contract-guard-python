"""采集与归一化测试。

每个用例都会通过日志打印：输入契约、判定依据、最终结论
（pytest 以 --log-cli-level=INFO 输出到控制台）。
"""

from __future__ import annotations

import json
import logging

import pytest

from contract_guard.collector import ContractCollector
from contract_guard.errors import (
    ContractGuardError,
    ContractParseError,
    IncompleteContractError,
    UnsupportedContractError,
)

logger = logging.getLogger("contract_tests.collector")


def valid_contract() -> dict:
    """一份字段声明顺序刻意打乱、枚举无序的合法契约。"""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/users/{id}": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "integer"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id", "name"],
                                        "properties": {
                                            "role": {
                                                "type": "string",
                                                "enum": ["admin", "user", "guest"],
                                            },
                                            "name": {"type": "string"},
                                            "id": {"type": "integer"},
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/users": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "tags": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "name": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                }
            },
        },
    }


def test_collect_normalizes_order_and_is_deterministic() -> None:
    """字段顺序、枚举顺序被归一化；不同入口（dict / 文本 / 字节）指纹一致。"""
    collector = ContractCollector()
    raw_a = valid_contract()

    raw_b = json.loads(json.dumps(raw_a))
    # 刻意以不同插入顺序重建 properties，并打乱枚举（不改变语义）
    schema_b = raw_b["paths"]["/users/{id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    old_props = schema_b["properties"]
    schema_b["properties"] = {
        key: old_props[key] for key in ["role", "id", "name"]
    }
    schema_b["properties"]["role"]["enum"] = ["guest", "admin", "user"]

    snap_a = collector.collect(raw_a, service="svc", version="1.0.0")
    snap_b = collector.collect(raw_b, service="svc", version="1.0.0")
    snap_text = collector.collect(json.dumps(raw_a), service="svc", version="1.0.0")
    snap_bytes = collector.collect(json.dumps(raw_a).encode(), service="svc", version="1.0.0")

    endpoint_names = [(e.method, e.path) for e in snap_a.endpoints]
    logger.info("输入契约(重排前后两份) + 采集端点顺序: %s", endpoint_names)
    logger.info("判定依据: endpoints 按(path,method)排序, 字段按名排序, 枚举按稳定规则排序")
    logger.info(
        "最终结论: 指纹一致 -> %s | %s | %s | %s",
        snap_a.fingerprint,
        snap_b.fingerprint,
        snap_text.fingerprint,
        snap_bytes.fingerprint,
    )

    # 字典序："/users" 是 "/users/{id}" 的前缀，故在前
    assert endpoint_names == [("POST", "/users"), ("GET", "/users/{id}")]
    success_200 = next(
        r for r in snap_a.endpoints[1].responses if r.status == "200"
    )
    resp_fields = success_200.fields
    assert [r.status for r in snap_a.endpoints[1].responses] == ["200"]
    assert [f.name for f in resp_fields] == ["id", "name", "role"]
    assert tuple(resp_fields[2].enum) == ("admin", "guest", "user")
    assert snap_a.fingerprint == snap_b.fingerprint == snap_text.fingerprint == snap_bytes.fingerprint


def test_collect_nullable_default_null_semantics() -> None:
    """has_default 与 nullable 能区分“未声明 default”和“default: null”。"""
    collector = ContractCollector()
    raw = {
        "paths": {
            "/x": {
                "get": {
                    "parameters": [
                        {"name": "a", "in": "query", "schema": {"type": "string"}},
                        {"name": "b", "in": "query",
                         "schema": {"type": "string", "nullable": True, "default": None}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    }
    snap = collector.collect(raw, service="svc", version="1")
    a, b = snap.endpoints[0].request_params
    logger.info("判定依据: 未声明 default -> has_default=False; default:null -> True")
    logger.info("最终结论: a.has_default=%s b.has_default=%s b.nullable=%s",
                a.has_default, b.has_default, b.nullable)
    assert a.has_default is False and a.default is None
    assert b.has_default is True and b.default is None and b.nullable is True


@pytest.mark.parametrize(
    "mutate, expected_code",
    [
        # 无法解析
        (lambda r: r.__setitem__("paths", "not-an-object"), "contract_parse_error"),
        # 不完整：缺 paths
        (lambda r: r.pop("paths"), "incomplete_contract"),
        # 不完整：字段缺 type
        (lambda r: r["paths"]["/users/{id}"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]["properties"]["id"].pop("type"),
         "incomplete_contract"),
        # 不完整：数组缺 items
        (lambda r: r["paths"]["/users"]["post"]["requestBody"]["content"][
            "application/json"]["schema"]["properties"]["tags"].pop("items"),
         "incomplete_contract"),
        # 不完整：required 引用了未定义字段
        (lambda r: r["paths"]["/users/{id}"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]["required"].append("ghost"),
         "incomplete_contract"),
        # 不完整：path 参数显式 required=false
        (lambda r: r["paths"]["/users/{id}"]["get"]["parameters"][0].__setitem__(
            "required", False),
         "incomplete_contract"),
        # 不支持：未知类型
        (lambda r: r["paths"]["/users/{id}"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]["properties"]["id"].__setitem__("type", "uuid"),
         "unsupported_contract"),
        # 不支持：未知参数位置
        (lambda r: r["paths"]["/users/{id}"]["get"]["parameters"][0].__setitem__("in", "body"),
         "unsupported_contract"),
        # 不完整：重复参数
        (lambda r: r["paths"]["/users/{id}"]["get"]["parameters"].append(
            {"name": "id", "in": "path", "schema": {"type": "integer"}}),
         "incomplete_contract"),
        # 不完整：缺少成功响应
        (lambda r: r["paths"]["/users/{id}"]["get"].__setitem__("responses", {"404": {}}),
         "incomplete_contract"),
    ],
)
def test_malformed_contracts_have_distinguishable_reasons(mutate, expected_code) -> None:
    raw = valid_contract()
    mutate(raw)
    logger.info("输入畸形契约: %s", json.dumps(raw, ensure_ascii=False, sort_keys=True))
    collector = ContractCollector()
    with pytest.raises(ContractGuardError) as exc_info:
        collector.collect(raw, service="svc", version="1")
    err = exc_info.value
    logger.info("判定依据: 归一化采集器必须显式拒绝而不是静默跳过")
    logger.info("最终结论: code=%s message=%s", err.code, err.message)
    assert err.code == expected_code
    assert err.code in {"contract_parse_error", "incomplete_contract", "unsupported_contract"}
