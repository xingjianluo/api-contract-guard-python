"""HTTP 入口。

路由只负责：读取 JSON、调用 :class:`ContractGuardService`、把领域结果
渲染为稳定 JSON。所有 :class:`ContractGuardError` 统一渲染为
``{"error": {"code", "message"}}``，底层异常类型不外泄。
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    ConsumerDependencyError,
    ContractGuardError,
    ContractParseError,
    IncompleteContractError,
    StorageError,
    UnsupportedContractError,
)
from .models import (
    Change,
    DiffReport,
    GateFinding,
    GateReport,
    GateVerdict,
    Severity,
)
from .service import ContractGuardService

STORAGE_ENV = "CONTRACT_GUARD_STORAGE"

_HTTP_STATUS = {
    ContractParseError: 400,
    ConsumerDependencyError: 422,
    IncompleteContractError: 422,
    UnsupportedContractError: 422,
    BaselineConflictError: 409,
    ConsumerConflictError: 409,
    BaselineNotFoundError: 404,
    StorageError: 500,
}


def _status_for(exc: ContractGuardError) -> int:
    for cls, status in _HTTP_STATUS.items():
        if isinstance(exc, cls):
            return status
    return 500


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _change_to_dict(c: Change) -> dict[str, Any]:
    return {
        "severity": c.severity.value,
        "path": c.path,
        "kind": c.kind,
        "reason_code": c.reason_code,
        "message": c.message,
        "old": c.old,
        "new": c.new,
    }


def _diff_to_dict(r: DiffReport) -> dict[str, Any]:
    return {
        "service": r.service,
        "old_version": r.old_version,
        "new_version": r.new_version,
        "old_fingerprint": r.old_fingerprint,
        "new_fingerprint": r.new_fingerprint,
        "has_breaking": r.has_breaking,
        "summary": {sev.value: sum(1 for c in r.changes if c.severity is sev)
                    for sev in Severity},
        "changes": [_change_to_dict(c) for c in r.changes],
    }


def _finding_to_dict(f: GateFinding) -> dict[str, Any]:
    return {
        "affected": f.affected,
        "operations": list(f.operations),
        "reason_code": f.reason_code,
        "message": f.message,
        "affected_consumers": list(f.affected_consumers),
        "change": _change_to_dict(f.change),
    }


def _gate_to_dict(r: GateReport) -> dict[str, Any]:
    return {
        "service": r.service,
        "version": r.version,
        "verdict": r.verdict.value,
        "findings": [_finding_to_dict(f) for f in r.findings],
    }


def create_app(storage_root: str | None = None) -> FastAPI:
    root = storage_root or os.environ.get(STORAGE_ENV, "./.contract_guard_data")
    guard = ContractGuardService(root)
    app = FastAPI(title="API Contract Guard", version="0.1.0")
    app.state.guard = guard

    # ---------------- 错误归一化 ---------------- #

    @app.exception_handler(ContractGuardError)
    async def _guard_error_handler(_request: Request, exc: ContractGuardError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for(exc),
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    async def _read_json_body(request: Request) -> object:
        body = await request.body()
        if not body:
            raise ContractParseError("empty_request_body")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ContractParseError(
                f"invalid_json: {exc.msg} at line {exc.lineno} col {exc.colno}",
                cause=exc,
            ) from exc

    # ---------------- 路由 ---------------- #

    @app.post("/services/{service}/baselines/{version}")
    async def register_baseline(service: str, version: str, request: Request) -> JSONResponse:
        raw = await _read_json_body(request)
        expected = request.query_params.get("expected_fingerprint")
        baseline = guard.register_baseline(
            raw, service=service, version=version,
            expected_fingerprint=expected or None,
        )
        return JSONResponse(
            status_code=201,
            content={
                "service": baseline.service,
                "version": baseline.version,
                "fingerprint": baseline.snapshot.fingerprint,
                "endpoint_count": len(baseline.snapshot.endpoints),
            },
        )

    @app.get("/services/{service}/baselines")
    async def list_baselines(service: str) -> JSONResponse:
        items = guard.list_baselines(service)
        return JSONResponse(content={
            "service": service,
            "baselines": [
                {"version": b.version,
                 "fingerprint": b.snapshot.fingerprint,
                 "created_at": b.created_at}
                for b in items
            ],
        })

    @app.post("/services/{service}/consumers/{consumer}")
    async def register_consumer(service: str, consumer: str, request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not isinstance(body, dict):
            raise IncompleteContractError("consumer_body_must_be_object")
        operations = body.get("operations", [])
        dependencies = body.get("dependencies", [])
        if not isinstance(operations, list):
            raise IncompleteContractError("consumer_operations_must_be_array")
        if not isinstance(dependencies, list):
            raise IncompleteContractError("consumer_dependencies_must_be_array")
        if not operations and not dependencies:
            raise IncompleteContractError(
                "consumer_must_declare_operations_or_dependencies"
            )
        expectation = guard.register_consumer(
            service, consumer, operations, dependencies
        )
        return JSONResponse(status_code=201, content={
            "service": service,
            "consumer": expectation.consumer,
            "operations": list(expectation.operations),
            "dependencies": [
                {
                    "operation": d.operation,
                    "response": d.response,
                    "fields": list(d.fields),
                }
                for d in expectation.dependencies
            ],
        })

    @app.get("/services/{service}/diff/{old_version}/{new_version}")
    async def diff_versions(service: str, old_version: str, new_version: str) -> JSONResponse:
        report = guard.diff_versions(service, old_version, new_version)
        return JSONResponse(content=_diff_to_dict(report))

    @app.get("/services/{service}/release-check/{old_version}/{new_version}")
    async def release_check(service: str, old_version: str, new_version: str) -> JSONResponse:
        report = guard.check_release(service, old_version, new_version)
        return JSONResponse(content=_gate_to_dict(report))

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok"})

    return app



app = create_app()


__all__ = ["create_app", "app", "GateVerdict"]
