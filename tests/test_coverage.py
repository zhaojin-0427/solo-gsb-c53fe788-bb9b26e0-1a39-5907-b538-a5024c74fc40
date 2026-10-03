"""Tests for the call-coverage engine and HTTP API."""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def analysis_id(client):
    def zip_bytes(folder):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(folder):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, folder))
        return buf.getvalue()

    r = client.post(
        "/api/analyses",
        files={
            "baseline": ("b.zip", zip_bytes("examples/baseline"),
                         "application/zip"),
            "candidate": ("c.zip", zip_bytes("examples/candidate"),
                          "application/zip"),
        })
    assert r.status_code == 201, r.text
    return r.json()["analysis_id"]


def _ndjson(rows) -> bytes:
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


BASELINE_CALLS = [
    # GET /pets 200 application/json array<Pet> — fully covered
    {"method": "get", "path": "/pets",
     "query": {"status": "available", "limit": 10},
     "headers": {}, "cookies": {},
     "response": {"status": 200, "media_type": "application/json",
                  "body": [{"id": 1, "name": "Rex", "status": "available"}]}},
    # GET /pets/7 — unique template match
    {"method": "get", "path": "/pets/7",
     "response": {"status": 200, "media_type": "application/json",
                  "body": {"id": 7, "name": "A", "status": "sold"}}},
    # 404 declared without content -> no-entity cell
    {"method": "get", "path": "/pets/7",
     "response": {"status": 404}},
    # path parameter fails schema (integer required)
    {"method": "get", "path": "/pets/oops",
     "response": {"status": 200}},
    # unknown path -> unmatched
    {"method": "get", "path": "/nope",
     "response": {"status": 200}},
    # response body violates schema -> invalid
    {"method": "get", "path": "/pets",
     "response": {"status": 200, "media_type": "application/json",
                  "body": [{"id": "not-int"}]}},
    # POST /pets creates a pet, vendor-independent application/json body
    {"method": "post", "path": "/pets",
     "request": {"media_type": "application/json",
                 "body": {"name": "Doggo"}},
     "response": {"status": 201, "media_type": "application/json",
                  "body": {"id": 9, "name": "Doggo", "status": "pending"}}},
    # POST /pets missing required request body -> invalid
    {"method": "post", "path": "/pets",
     "response": {"status": 400}},
]


@pytest.fixture(scope="module")
def coverage_id(client, analysis_id):
    r = client.post(
        f"/api/analyses/{analysis_id}/coverage/baseline",
        files={"calls": ("calls.ndjson", _ndjson(BASELINE_CALLS),
                         "application/x-ndjson")})
    assert r.status_code == 201, r.text
    return analysis_id


# --------------------------------------------------------------------- API
def test_coverage_upload_summary(client, coverage_id):
    d = client.get(
        f"/api/analyses/{coverage_id}/coverage/baseline").json()
    s = d["summary"]
    assert s["calls_total"] == len(BASELINE_CALLS)
    assert s["covered"] == 4          # rows 1,2,3,7
    assert s["cells_covered"] >= 4
    assert s["unmatched"] == 1        # /nope
    assert s["invalid"] == 3          # bad path param, bad body, missing body
    assert s["undetermined"] == 0
    assert s["cells_total"] > s["cells_covered"]
    assert s["gaps_total"] == s["cells_total"] - s["cells_covered"]


def test_coverage_row_reasons(client, coverage_id):
    d = client.get(
        f"/api/analyses/{coverage_id}/coverage/baseline").json()
    by_line = {r["line"]: r for r in d["rows"]}
    assert by_line[1]["outcome"] == "covered"
    assert by_line[1]["cell"] == "200|application/json"
    assert by_line[2]["operation"] == {"method": "GET",
                                        "path": "/pets/{petId}"}
    assert by_line[3]["cell"] == "404|∅"
    bad_path = by_line[4]
    assert bad_path["outcome"] == "invalid"
    assert {x["code"] for x in bad_path["reasons"]} >= {
        "parameter_invalid", "response_entity_expected"}
    assert by_line[5]["outcome"] == "unmatched"
    assert by_line[5]["reasons"][0]["code"] == "path_not_matched"
    assert by_line[6]["reasons"][0]["code"] == "response_body_invalid"
    assert by_line[7]["outcome"] == "covered"
    assert by_line[8]["reasons"][0]["code"] == "missing_required_request_body"


def test_coverage_grid_shape(client, coverage_id):
    d = client.get(
        f"/api/analyses/{coverage_id}/coverage/baseline").json()
    get_pet = next(o for o in d["operations"]
                   if o["method"] == "GET" and o["path"] == "/pets/{petId}")
    keys = {(c["status_key"], c["media_type"], c["has_content"])
            for c in get_pet["cells"]}
    assert ("200", "application/json", True) in keys
    assert ("404", None, False) in keys
    c404 = next(c for c in get_pet["cells"] if c["status_key"] == "404")
    assert c404["covered"] is True and c404["covered_by"][0]["line"] == 3
    # /legacy/echo is in the baseline but this traffic never hits it: gap
    legacy = next(o for o in d["operations"] if o["path"] == "/legacy/echo")
    assert legacy["cells_covered"] == 0
    assert legacy["gaps"]


def test_coverage_listing(client, coverage_id):
    d = client.get(f"/api/analyses/{coverage_id}/coverage").json()
    assert any(x["side"] == "baseline" for x in d["items"])


def test_coverage_missing_side_404(client, coverage_id):
    r = client.get(f"/api/analyses/{coverage_id}/coverage/candidate")
    assert r.status_code == 404


def test_coverage_bad_side_400(client, coverage_id):
    r = client.post(
        f"/api/analyses/{coverage_id}/coverage/sideways",
        files={"calls": ("c.ndjson", b'{"x":1}\n', "text/plain")})
    assert r.status_code == 400


def test_coverage_empty_file_400(client, coverage_id):
    r = client.post(
        f"/api/analyses/{coverage_id}/coverage/candidate",
        files={"calls": ("c.ndjson", b"  \n", "text/plain")})
    assert r.status_code == 400


def test_coverage_unknown_analysis_404(client):
    r = client.post(
        "/api/analyses/nope/coverage/baseline",
        files={"calls": ("c.ndjson", b"{}\n", "text/plain")})
    assert r.status_code == 404


def test_coverage_export_deterministic(client, coverage_id):
    url = f"/api/analyses/{coverage_id}/coverage/baseline/export"
    a = client.get(url)
    b = client.get(url)
    assert a.status_code == 200
    assert a.content == b.content
    import hashlib
    assert a.headers["x-coverage-fingerprint"] == \
        hashlib.sha256(a.content).hexdigest()
    assert json.loads(a.content)["side"] == "baseline"


def test_coverage_replace_then_delete(client, analysis_id):
    # upload to candidate side, replace, verify idempotent bytes, then delete
    calls = [{"method": "get", "path": "/pets",
              "response": {"status": 200, "media_type": "application/json",
                           "body": []}}]
    payload = _ndjson(calls)
    url = f"/api/analyses/{analysis_id}/coverage/candidate"
    r1 = client.post(url, files={"calls": ("a.ndjson", payload,
                                           "application/x-ndjson")})
    assert r1.status_code == 201
    r2 = client.post(url, files={"calls": ("b.ndjson", payload,
                                           "application/x-ndjson")})
    assert r2.status_code == 201
    exp1 = client.get(url + "/export").content
    exp2 = client.get(url + "/export").content
    assert exp1 == exp2
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404
    assert client.delete(url).status_code == 404


# ----------------------------------------------------------------- engine
def _engine(spec):
    from app.services.schemagraph import SchemaGraph
    return SchemaGraph({"openapi.json": spec}, "openapi.json")


def _run(spec, rows, side="baseline"):
    from app.services.coverage import compute_coverage
    return compute_coverage(_engine(spec), _ndjson(rows), side=side,
                            analysis_id="t", filename="c.ndjson")


def test_exact_path_precedes_template():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {
                "/x/1": {"get": {"responses": {"200": {"description": "e"}}}},
                "/x/{a}": {"get": {"responses": {
                    "200": {"description": "t"}}}}}}
    doc = _run(spec, [{"method": "get", "path": "/x/1",
                       "response": {"status": 200}}])
    r = doc["rows"][0]
    assert r["outcome"] == "covered"
    assert r["operation"]["path"] == "/x/1"


def test_ambiguous_templates_unattributed():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {
                "/x/{a}": {"get": {"responses": {"200": {"description": "1"}}}},
                "/x/{b}": {"get": {"responses": {"200": {"description": "2"}}}}}}
    doc = _run(spec, [{"method": "get", "path": "/x/1",
                       "response": {"status": 200}}])
    r = doc["rows"][0]
    assert r["outcome"] == "unmatched"
    assert r["reasons"][0]["code"] == "ambiguous_template"
    assert set(r["reasons"][0]["templates"]) == {
        "GET /x/{a}", "GET /x/{b}"}


def test_status_exact_range_default_order():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/p": {"get": {"responses": {
                "201": {"description": "exact"},
                "2XX": {"description": "range"},
                "default": {"description": "d"}}}}}}
    doc = _run(spec, [
        {"method": "get", "path": "/p", "response": {"status": 201}},
        {"method": "get", "path": "/p", "response": {"status": 204}},
        {"method": "get", "path": "/p", "response": {"status": 500}},
        {"method": "get", "path": "/p", "response": {"status": 403}},
    ])
    keys = [r["status_key"] for r in doc["rows"]]
    assert keys == ["201", "2XX", "default", "default"]
    assert all(r["outcome"] == "covered" for r in doc["rows"])


def test_non_json_media_excluded_and_zero_denominator():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/p": {"get": {"responses": {
                "200": {"description": "ok", "content": {
                    "text/plain": {"schema": {"type": "string"}}}}}}}}}
    doc = _run(spec, [
        {"method": "get", "path": "/p",
         "response": {"status": 200, "media_type": "text/plain",
                      "body": "hi"}},
        {"method": "get", "path": "/p", "response": {"status": 200}},
    ])
    op = doc["operations"][0]
    assert op["applicable"] is False
    assert op["cells_total"] == 0
    assert op["excluded_media"][0]["media_types"] == ["text/plain"]
    assert all(r["outcome"] == "unmatched" for r in doc["rows"])
    codes = {r["reasons"][0]["code"] for r in doc["rows"]}
    assert codes == {"status_media_not_json"}


def test_undetermined_pattern_schema():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/p": {"get": {"responses": {"200": {
                "description": "ok", "content": {"application/json": {
                    "schema": {"type": "string", "pattern": "^a$"}}}}}}}}}
    doc = _run(spec, [{"method": "get", "path": "/p",
                       "response": {"status": 200,
                                    "media_type": "application/json",
                                    "body": "abc"}}])
    r = doc["rows"][0]
    assert r["outcome"] == "undetermined"
    assert r["reasons"][0]["code"] == "response_body_schema_undecidable"
    # an undetermined row never covers its cell
    assert doc["summary"]["cells_covered"] == 0


def test_request_parameter_and_body_constraints():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/p": {"post": {
                "parameters": [{"name": "q", "in": "query", "required": True,
                                "schema": {"type": "integer",
                                           "minimum": 1}}],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["a"],
                        "properties": {"a": {"type": "string"}}}}}},
                "responses": {"200": {"description": "ok"}}}}}}
    # missing required query param -> invalid
    doc1 = _run(spec, [{"method": "post", "path": "/p",
                        "request": {"media_type": "application/json",
                                    "body": {"a": "x"}},
                        "response": {"status": 200}}])
    assert doc1["rows"][0]["outcome"] == "invalid"
    assert doc1["rows"][0]["reasons"][0]["code"] == \
        "missing_required_parameter"
    # everything passes (wire-string coercion for the query integer)
    doc2 = _run(spec, [{"method": "post", "path": "/p",
                        "query": {"q": "5"},
                        "request": {"media_type": "application/json",
                                    "body": {"a": "x"}},
                        "response": {"status": 200}}])
    assert doc2["rows"][0]["outcome"] == "covered"


def test_malformed_and_nonobject_lines():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {}}
    from app.services.coverage import compute_coverage
    raw = b'{"method":"get","path":"/p"}\nnot json at all\n[1,2]\n'
    doc = compute_coverage(_engine(spec), raw, side="baseline",
                           analysis_id="t", filename="c.ndjson")
    outcomes = {(r["line"], r["outcome"]) for r in doc["rows"]}
    assert (2, "unmatched") in outcomes   # malformed JSON
    assert (3, "unmatched") in outcomes   # valid JSON but not an object
    assert doc["rows"][0]["reasons"][0]["code"] == "missing_response"


def test_deterministic_bytes():
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
            "paths": {"/p": {"get": {"responses": {"200": {
                "description": "ok"}}}}}}
    calls = [{"method": "get", "path": "/p",
              "response": {"status": 200}}]
    from app.services.coverage import compute_coverage
    g = _engine(spec)
    a = compute_coverage(g, _ndjson(calls), side="baseline",
                         analysis_id="t", filename="c.ndjson")
    b = compute_coverage(g, _ndjson(calls), side="baseline",
                         analysis_id="t", filename="c.ndjson")
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
        json.dumps(b, sort_keys=True, ensure_ascii=False)
