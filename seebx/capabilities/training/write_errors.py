from __future__ import annotations

"""Stable HTTP mapping for failures from the LifeSwitch Training writer."""

from fastapi import HTTPException

from seebx.adapters.lifeswitch_training_writes_postgres import TrainingWriterError


def training_writer_http_error(error: TrainingWriterError) -> HTTPException:
    if error.no_result:
        return HTTPException(status_code=500, detail=error.detail)
    if error.sqlstate in {"22023", "23514"}:
        return HTTPException(status_code=400, detail=error.detail)
    if error.sqlstate in {"22001", "22P02", "23502", "23503"}:
        return HTTPException(status_code=400, detail="invalid training write request")
    if error.sqlstate == "P0002":
        return HTTPException(status_code=404, detail=error.detail)
    if error.sqlstate == "23505":
        return HTTPException(status_code=409, detail=error.detail)
    if error.sqlstate == "28000":
        return HTTPException(status_code=403, detail="training write not authorized")
    if error.sqlstate in {"40001", "40P01"}:
        return HTTPException(
            status_code=503,
            detail="training write should be retried",
            headers={"Retry-After": "1"},
        )
    if error.sqlstate in {"42501", "42883"}:
        return HTTPException(
            status_code=503,
            detail="training write temporarily unavailable",
        )
    return HTTPException(status_code=500, detail="training write failed")
