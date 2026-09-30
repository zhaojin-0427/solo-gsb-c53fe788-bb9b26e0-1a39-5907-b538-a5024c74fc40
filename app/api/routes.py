from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response
from datetime import datetime, timezone

from ..analysis import analyze, filter_report, from_envelope
from ..config import settings
from ..services.loader import PackageError
from ..services.replay import replay
from ..store import store

router = APIRouter(prefix="/api")

SEVERITY_VALUES = {"breaking", "undetermined", "compatible"}


@router.get("/health")
def health():
    return {
        "status": "ok",
        "storage": store.status(),
        "max_upload_mb": settings.max_upload_mb,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/analyses", status_code=201)
async def create_analysis(baseline: UploadFile, candidate: UploadFile):
    b_bytes = await baseline.read()
    c_bytes = await candidate.read()
    _check_size(baseline.filename, b_bytes)
    _check_size(candidate.filename, c_bytes)
    try:
        result = analyze(baseline.filename or "baseline.yaml", b_bytes,
                         candidate.filename or "candidate.yaml", c_bytes)
    except PackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # defensive: surface parse bugs as 422
        raise HTTPException(status_code=422,
                            detail=f"分析失败: {exc.__class__.__name__}: {exc}")

    created_at = datetime.now(timezone.utc).isoformat()
    store.save(
        analysis_id=result["id"],
        baseline_name=baseline.filename or "baseline",
        candidate_name=candidate.filename or "candidate",
        baseline_meta=result["baseline_meta"],
        candidate_meta=result["candidate_meta"],
        report_doc=result["report_doc"],
        stats=result["stats"],
        created_at=created_at,
        packages=result["packages"])
    body = dict(result["report_doc"])
    body["stored"] = True
    body["storage"] = store.status()
    body["fingerprint"] = result["fingerprint"]
    return body


def _check_size(name, data):
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {name} 超过大小限制 "
                   f"{settings.max_upload_mb}MB")


@router.get("/analyses")
def list_analyses(limit: int = 50):
    return {"items": store.list_analyses(min(max(limit, 1), 200)),
            "storage": store.status()}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    return record["report"]


@router.delete("/analyses/{analysis_id}")
def delete_analysis(analysis_id: str):
    if not store.get(analysis_id):
        raise HTTPException(status_code=404, detail="分析不存在")
    store.delete(analysis_id)
    return {"deleted": analysis_id}


def _parse_severity(severity: str | None) -> set[str]:
    if not severity:
        return set(SEVERITY_VALUES)
    values = {s.strip() for s in severity.split(",") if s.strip()}
    unknown = values - SEVERITY_VALUES
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知严重度筛选: {sorted(unknown)}，"
                   f"可选 breaking/undetermined/compatible")
    return values


@router.get("/analyses/{analysis_id}/export")
def export_analysis(analysis_id: str, severity: str | None = None):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    doc = record["report"]
    sev = _parse_severity(severity)
    if sev != SEVERITY_VALUES:
        doc = filter_report(doc, sev)
    payload = _dumps_deterministic(doc)
    suffix = "all" if sev == SEVERITY_VALUES else "-".join(sorted(sev))
    filename = f"contract-report-{analysis_id}-{suffix}.json"
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"',
                 "X-Report-Fingerprint": _fingerprint(payload)})


@router.post("/analyses/{analysis_id}/findings/{finding_id}/replay")
async def replay_finding(request: Request, analysis_id: str,
                         finding_id: str):
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="分析不存在")
    packages = store.get_packages(analysis_id)
    if not packages:
        raise HTTPException(status_code=409,
                            detail="原始文档包不可用，无法重放")
    finding = None
    for f in record["report"]["findings"]:
        if f["id"] == finding_id:
            finding = f
            break
    if not finding:
        raise HTTPException(status_code=404, detail="结果不存在")

    override = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and "instance" in body:
        override = body["instance"]

    witness = finding.get("witness") or {}
    # replay module expects side descriptors
    witness = dict(witness)
    witness.setdefault("direction",
                       "response" if finding.get("location") == "response"
                       else "request")
    # structural replay dispatches on witness.kind but needs the rule to pick
    # the right semantic checks; backfill context for older stored reports
    witness.setdefault("rule", finding.get("rule"))
    for key in ("location", "media_type", "status_code"):
        witness.setdefault(key, finding.get(key))
    pkg_b = from_envelope(packages["baseline"])
    pkg_c = from_envelope(packages["candidate"])
    result = replay(pkg_b, pkg_c, witness, instance_override=override,
                    rule=finding.get("rule"))
    result["finding_id"] = finding_id
    result["rule"] = finding["rule"]
    result["message"] = finding["message"]
    return result


def _dumps_deterministic(doc) -> str:
    import json
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _fingerprint(payload: str) -> str:
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
