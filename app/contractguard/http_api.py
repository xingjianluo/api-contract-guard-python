"""HTTP 入口：FastAPI 路由，领域错误统一映射为可区分响应。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    ConsumerNotFoundError,
    ContractGuardError,
    ContractParseError,
    StorageError,
)
from .service import ContractGuardService
from .storage import LocalStore

# 错误类型 -> (HTTP 状态码, 稳定 error code)
_ERROR_STATUS: dict[type[ContractGuardError], tuple[int, str]] = {
    ContractParseError: (422, "contract_parse_error"),
    BaselineConflictError: (409, "baseline_conflict"),
    ConsumerConflictError: (409, "consumer_conflict"),
    BaselineNotFoundError: (404, "baseline_not_found"),
    ConsumerNotFoundError: (404, "consumer_not_found"),
    StorageError: (500, "storage_error"),
}


def create_app(store_root: str = "./.contractguard_data") -> FastAPI:
    app = FastAPI(title="api-contract-guard")
    service = ContractGuardService(LocalStore(store_root))

    @app.exception_handler(ContractGuardError)
    async def _handle_guard_error(_: Request, exc: ContractGuardError) -> JSONResponse:
        status, code = _ERROR_STATUS.get(type(exc), (500, "guard_error"))
        return JSONResponse(
            status_code=status,
            content={
                "error": code,
                "reason": exc.reason,
                "message": exc.message,
            },
        )

    @app.post("/baselines/{version}")
    async def register_baseline(
        version: str, payload: dict[str, Any], overwrite: bool = False
    ):
        snapshot = service.register_baseline(payload, version=version, overwrite=overwrite)
        return {
            "version": snapshot.version,
            "fingerprint": snapshot.fingerprint(),
            "endpoint_count": len(snapshot.endpoints),
        }

    @app.get("/baselines")
    async def list_baselines():
        return {"versions": service.list_baselines()}

    @app.get("/diff/{old_version}/{new_version}")
    async def diff(old_version: str, new_version: str):
        return service.compare(old_version, new_version).to_dict()

    @app.post("/consumers/{consumer}")
    async def register_consumer(
        consumer: str, payload: dict[str, Any], overwrite: bool = False
    ):
        expectation = service.register_consumer(
            payload, consumer=consumer, overwrite=overwrite
        )
        return {
            "consumer": expectation.consumer,
            "version": expectation.version,
            "used_endpoint_count": len(expectation.used_endpoints),
        }

    @app.get("/consumers")
    async def list_consumers():
        return {"consumers": service.list_consumers()}

    @app.get("/gate/{old_version}/{new_version}")
    async def gate(old_version: str, new_version: str):
        return service.gate(old_version, new_version).to_dict()

    return app
