"""
Chaos testing endpoints.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.services import chaos

router = APIRouter(prefix="/chaos", tags=["Chaos"])


@router.post("/run")
async def run_chaos(payload: Dict[str, int]) -> Dict[str, Any]:
    try:
        events = int(payload.get("events", 100))
        duplicates = int(payload.get("duplicates", 20))
        out_of_order = int(payload.get("out_of_order", 15))
        conflicts = int(payload.get("conflicts", 10))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    result = chaos.run_chaos(events=events, duplicates=duplicates, out_of_order=out_of_order, conflicts=conflicts)
    return result


@router.get("/status")
async def get_chaos_status():
    return chaos.get_status()
