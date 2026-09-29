"""测试用契约构造工具。"""

from __future__ import annotations

from copy import deepcopy


def _json_response(fields, *, required=None, description="ok"):
    """构造一个 application/json 响应节点；fields 为 None 表示无响应体。"""
    node: dict = {"description": description}
    if fields is not None:
        node["content"] = {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": list(required) if required is not None
                    else (["id"] if "id" in fields else []),
                    "properties": fields,
                }
            }
        }
    return node


def response_contract(responses, *, path="/users", method="get",
                      params=None, body=None, body_required=False):
    """构造声明了多种响应结果的契约。

    responses: {状态码: 字段dict 或 None(无响应体)}
    body:      请求体字段 dict（None 表示无请求体）
    """
    contract = {
        "openapi": "3.0.0",
        "paths": {
            path: {
                method: {
                    "parameters": params or [],
                    "responses": {
                        code: _json_response(fields)
                        for code, fields in responses.items()
                    },
                }
            }
        },
    }
    op = contract["paths"][path][method]
    if body is not None:
        op["requestBody"] = {
            "required": body_required,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": [],
                        "properties": body,
                    }
                }
            },
        }
    return contract


def minimal_contract(version: str = "1.0.0", *, response_fields=None, params=None,
                     body_fields=None, path: str = "/users", method: str = "get") -> dict:
    contract = {
        "openapi": "3.0.0",
        "paths": {
            path: {
                method: {
                    "parameters": params or [],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"] if response_fields is None else [],
                                        "properties": response_fields
                                        if response_fields is not None
                                        else {"id": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    op = contract["paths"][path][method]
    if body_fields is not None:
        op["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": [],
                        "properties": body_fields,
                    }
                }
            }
        }
    return deepcopy(contract)
