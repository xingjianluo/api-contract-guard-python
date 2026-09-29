"""测试用契约构造工具。"""

from __future__ import annotations

from copy import deepcopy


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


def json_response(status: str, properties=None, *, required=None, with_body: bool = True) -> dict:
    """构造单个 OpenAPI 响应条目；with_body=False 表示无内容响应（如 204）。"""
    if not with_body:
        return {"description": f"{status} no body"}
    return {
        "description": f"{status} response",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": required or [],
                    "properties": properties if properties is not None else {},
                }
            }
        },
    }


def contract_with_responses(responses: dict, *, path: str = "/users", method: str = "get",
                            params=None, request_body=None) -> dict:
    """构造带多种响应结果（状态码 -> 响应条目）的契约。

    request_body 可传 (properties_dict, required_list, required_flag)。
    """
    op: dict = {
        "parameters": params or [],
        "responses": deepcopy(responses),
    }
    if request_body is not None:
        body_props, body_required, body_flag = request_body
        op["requestBody"] = {
            "required": body_flag,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": body_required or [],
                        "properties": body_props,
                    }
                }
            },
        }
    return {
        "openapi": "3.0.0",
        "paths": {path: {method: op}},
    }
