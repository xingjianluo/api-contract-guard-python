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
