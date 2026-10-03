"""调用覆盖（Call Coverage）引擎。

上传一份 **NDJSON** 调用记录（每行一次调用），针对选中的文档侧（基线或候选）
把每次调用归并到 OpenAPI operation，并按 operation 的响应声明建立“覆盖格”：

* 路径先匹配**精确路径**，再匹配**唯一模板**；多个模板同时命中记为**未归属**；
* 每个 operation 的覆盖格由响应状态键 × JSON 媒体类型组成，``content`` 为空的
  响应单列一个“无实体”格；非 JSON 媒体类型不建格（排除）；
* 实际状态码依次匹配精确键（``200``）、范围键（``2XX``）、``default``；
* 仅当**请求参数、请求体、响应体**的所有*可判定*约束都通过时，该格才记为已
  覆盖；未匹配 / 校验失败 / 无法判定逐行给出原因码与说明。

引擎只读已存储的文档包，结论全部现场推导，不依赖比对报告。
导出文档不含时间戳，保证字节级确定性。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .endpointrequest import find_operation, gather_parameters, is_required
from .instance import validate
from .schemagraph import SchemaGraph, SchemaNode
from .structuralreplay import (_media_schema_node, _request_body_site,
                               _response_site, _responses)

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head",
                "options", "trace")

COVERED = "covered"
UNMATCHED = "unmatched"
INVALID = "invalid"
UNDETERMINED = "undetermined"

OUTCOMES = (COVERED, UNMATCHED, INVALID, UNDETERMINED)


# ---------------------------------------------------------------- media types
def _split_media(media_type: Any) -> Optional[str]:
    """Lower-cased media type without parameters (``a/b; charset=…`` → ``a/b``)."""
    if not isinstance(media_type, str) or not media_type.strip():
        return None
    return media_type.split(";", 1)[0].strip().lower()


def is_json_media(media_type: Any) -> bool:
    mt = _split_media(media_type)
    if mt is None:
        return False
    return mt == "application/json" or mt.endswith("+json")


_RANGE_KEY = re.compile(r"^([1-5])XX$", re.IGNORECASE)


def status_key_kind(key: str) -> Optional[str]:
    if key == "default":
        return "default"
    if key.isdigit() and 100 <= int(key) <= 599:
        return "exact"
    if _RANGE_KEY.match(key):
        return "range"
    return None


def _status_sort_key(key: str) -> tuple[int, Any]:
    """Deterministic ordering: exact codes, then 1XX..5XX, then default."""
    kind = status_key_kind(key)
    if kind == "exact":
        return (0, int(key))
    if kind == "range":
        return (1, int(key[0]))
    return (2, 0)


def _media_map(content: Any) -> dict[str, str]:
    """``{normalized media type: original declared key}`` (first spelling wins)."""
    out: dict[str, str] = {}
    if isinstance(content, dict):
        for original in sorted(content):
            norm = _split_media(original)
            if norm and norm not in out:
                out[norm] = original
    return out


# ----------------------------------------------------------------------- index
@dataclass
class Cell:
    status_key: str              # declared key, normalized string form
    status_kind: str             # exact | range | default
    media_type: Optional[str]    # declared JSON media (original spelling)
    has_content: bool
    node: Any = None             # SchemaNode for JSON media (may be None)

    @property
    def key(self) -> str:
        return f"{self.status_key}|{self.media_type or '∅'}"


@dataclass
class OperationIndex:
    method: str
    path: str                    # template as declared
    operation_id: Optional[str]
    exact: bool
    regex: Optional[re.Pattern]
    placeholders: list[str]
    cells: list[Cell] = field(default_factory=list)
    # status_key -> non-JSON media types declared there (excluded from grid)
    excluded_media: dict[str, list[str]] = field(default_factory=dict)


def _template_regex(template: str) -> tuple[re.Pattern, list[str]]:
    placeholders = re.findall(r"\{([^}]+)\}", template)
    parts: list[str] = []
    last = 0
    for m in re.finditer(r"\{([^}]+)\}", template):
        parts.append(re.escape(template[last:m.start()]))
        parts.append("([^/]+)")
        last = m.end()
    parts.append(re.escape(template[last:]))
    return re.compile("^" + "".join(parts) + "$"), placeholders


def _build_cells(graph: SchemaGraph, path: str, method: str
                 ) -> tuple[list[Cell], dict[str, list[str]]]:
    found = find_operation(graph, path, method)
    if found is None or found[0] is None:
        return [], {}
    op = found[0]
    raw_responses = op.get("responses")
    if not isinstance(raw_responses, dict):
        return [], {}
    deref = _responses(graph, path, method)  # str key -> dereferenced dict

    cells: list[Cell] = []
    excluded: dict[str, list[str]] = {}
    for raw_key in sorted(raw_responses,
                          key=lambda k: _status_sort_key(str(k))):
        key = str(raw_key)
        kind = status_key_kind(key)
        if kind is None:
            continue
        resp = deref.get(key)
        if not isinstance(resp, dict):
            continue
        media_map = _media_map(resp.get("content"))
        json_mts = [orig for norm, orig in media_map.items()
                    if norm == "application/json" or norm.endswith("+json")]
        if not media_map:
            # response without content: dedicated no-entity cell
            cells.append(Cell(status_key=key, status_kind=kind,
                              media_type=None, has_content=False))
            continue
        if not json_mts:
            # only non-JSON media types: excluded from the grid
            excluded[key] = sorted(media_map.values())
            continue
        site = _response_site(graph, path, method, raw_key)
        for original_mt in json_mts:
            node = _media_schema_node(graph, site, original_mt)
            cells.append(Cell(status_key=key, status_kind=kind,
                              media_type=original_mt, has_content=True,
                              node=node))
    return cells, excluded


def build_index(graph: SchemaGraph) -> list[OperationIndex]:
    """One index entry per declared operation, deterministically ordered."""
    root = graph.files[graph.entry]
    paths = root.get("paths")
    entries: list[OperationIndex] = []
    if isinstance(paths, dict):
        for template in sorted(paths):
            item = paths[template]
            if not isinstance(item, dict):
                continue
            for method in sorted(m for m in HTTP_METHODS
                                 if isinstance(item.get(m), dict)):
                op = item[method]
                rx, ph = (None, []) if "{" not in template else \
                    _template_regex(template)
                idx = OperationIndex(
                    method=method.upper(),
                    path=template,
                    operation_id=op.get("operationId")
                    if isinstance(op.get("operationId"), str) else None,
                    exact="{" not in template,
                    regex=rx,
                    placeholders=ph)
                idx.cells, idx.excluded_media = _build_cells(
                    graph, template, method)
                entries.append(idx)
    return entries


# ----------------------------------------------------------------- reasons
def _reason(code: str, message: str, **extra) -> dict:
    d = {"code": code, "message": message}
    d.update(extra)
    return d


# ------------------------------------------------- undecidability / coercion
def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def undecidable_reasons(node: Any, value: Any, depth: int = 0) -> list[str]:
    """Reasons why ``value`` cannot be judged against ``node`` (empty = decidable).

    Only schema fragments actually touched by the *present* value are inspected:
    an unsupported keyword on an absent optional property does not block a
    verdict. Conservative on branches: every branch that validates the value
    must itself be decidable.
    """
    if node is None:
        return []
    if node.is_cycle:
        return ["schema 存在循环引用，约束无法判定"]
    if node.invalid:
        return [f"schema 不可判定：{node.invalid}"]
    if node.unsupported:
        kws = sorted({kw for kw, _loc in node.unsupported})
        return [f"schema 含未支持的关键字 {kws}，约束无法判定"]
    if depth >= 8:
        return []

    vt = _json_type(value)
    if vt == "object":
        for name, child in node.properties.items():
            if name in value:
                reasons = undecidable_reasons(child, value[name], depth + 1)
                if reasons:
                    return [f"属性 {name!r}: {r}" for r in reasons]
        if isinstance(node.additional, SchemaNode):
            for k in sorted(set(value) - set(node.properties)):
                reasons = undecidable_reasons(node.additional, value[k],
                                              depth + 1)
                if reasons:
                    return [f"附加属性 {k!r}: {r}" for r in reasons]
        branches = node.one_of or node.any_of
        if branches:
            matching = [b for b in branches if not validate(b, value)]
            if matching:
                for b in matching:
                    reasons = undecidable_reasons(b, value, depth + 1)
                    if reasons:
                        kind = "oneOf" if node.one_of else "anyOf"
                        return [f"{kind} 匹配分支: {r}" for r in reasons]
    elif vt == "array" and node.items is not None:
        for i, item in enumerate(value[:20]):
            reasons = undecidable_reasons(node.items, item, depth + 1)
            if reasons:
                return [f"数组元素 [{i}]: {r}" for r in reasons]
    return []


def _coerce_scalar(node: Any, value: Any) -> Any:
    """Best-effort wire coercion for query/header/cookie/path scalar strings."""
    if not isinstance(value, str) or node is None or node.type is None:
        return value
    types = set(node.type)
    if value == "true" and "boolean" in types:
        return True
    if value == "false" and "boolean" in types:
        return False
    if "integer" in types:
        try:
            return int(value)
        except ValueError:
            pass
    if "number" in types:
        try:
            return float(value)
        except ValueError:
            pass
    return value


def _check_value(node: Any, raw_value: Any, label: str
                 ) -> tuple[Optional[str], list[str]]:
    """Return ``(undecidable_message_or_None, validation_errors)``."""
    if node is None:
        return None, []  # no declared schema => unconstrained
    value = _coerce_scalar(node, raw_value)
    und = undecidable_reasons(node, value)
    if und:
        return f"{label}：{und[0]}", []
    errors = validate(node, value)
    return None, [f"{label}: {e}" for e in errors]


# =============================================================== per-call row
@dataclass
class RowResult:
    line: int
    outcome: str
    reasons: list[dict] = field(default_factory=list)
    method: Optional[str] = None
    request_path: Optional[str] = None
    operation: Optional[dict] = None
    status: Optional[int] = None
    status_key: Optional[str] = None
    media_type: Optional[str] = None
    cell: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "outcome": self.outcome,
            "reasons": self.reasons,
            "method": self.method,
            "path": self.request_path,
            "operation": self.operation,
            "status": self.status,
            "status_key": self.status_key,
            "media_type": self.media_type,
            "cell": self.cell,
        }


class CoverageEngine:
    def __init__(self, graph: SchemaGraph, *, side: str = "",
                 analysis_id: str = "", source: Optional[dict] = None):
        self.graph = graph
        self.side = side
        self.analysis_id = analysis_id
        self.source = source or {}
        self.index = build_index(graph)
        self._exact: dict[tuple[str, str], OperationIndex] = {
            (op.path, op.method): op for op in self.index if op.exact}
        self._templates = [op for op in self.index if not op.exact]
        self._covered_by: dict[tuple[str, str], list[dict]] = {}

    # ------------------------------------------------------------- matching
    def match(self, path: str, method: str
              ) -> tuple[Optional[OperationIndex], Optional[dict],
                         Optional[re.Match]]:
        """Exact first, then unique template; ambiguous templates reported."""
        op = self._exact.get((path, method.upper()))
        if op is not None:
            return op, None, None
        hits: list[tuple[OperationIndex, re.Match]] = []
        for cand in self._templates:
            if cand.method != method.upper() or cand.regex is None:
                continue
            m = cand.regex.match(path)
            if m:
                hits.append((cand, m))
        if len(hits) == 1:
            return hits[0][0], None, hits[0][1]
        if len(hits) > 1:
            templates = sorted(f"{c.method} {c.path}" for c, _ in hits)
            return None, _reason(
                "ambiguous_template",
                f"路径 {path} 同时匹配多个模板: {', '.join(templates)}",
                templates=templates), None
        return None, None, None

    @staticmethod
    def _status_key(op: OperationIndex, status: int) -> Optional[str]:
        keys = {c.status_key for c in op.cells} | \
            set(op.excluded_media)
        if str(status) in keys:
            return str(status)
        rng = f"{status // 100}XX"
        if rng in keys:
            return rng
        if "default" in keys:
            return "default"
        return None

    # ------------------------------------------------------------- one line
    def evaluate_line(self, line_no: int, obj: Any) -> RowResult:
        if not isinstance(obj, dict):
            return RowResult(line_no, UNMATCHED, [_reason(
                "malformed_json", "该行不是 JSON 对象")])

        raw_method = obj.get("method")
        raw_path = obj.get("path")
        method = raw_method.upper() if isinstance(raw_method, str) else None
        row = RowResult(line_no, UNMATCHED, method=method,
                        request_path=raw_path if isinstance(raw_path, str)
                        else None)

        structural = self._structural_errors(obj, raw_method, raw_path,
                                             method)
        if structural:
            row.reasons = structural
            return row
        response = obj["response"]
        row.status = int(response["status"])

        # --------------------------------------------------- match operation
        concrete = raw_path.split("?", 1)[0]
        op, amb_reason, m = self.match(concrete, method)
        if amb_reason is not None:
            row.reasons = [amb_reason]
            return row
        if op is None:
            row.reasons = [_reason(
                "path_not_matched",
                f"{method} {raw_path} 不匹配文档中的任何 operation")]
            return row
        row.operation = {"method": op.method, "path": op.path}

        path_values: dict[str, str] = {}
        if m is not None:
            path_values = dict(zip(op.placeholders, m.groups()))

        invalid: list[dict] = []
        undetermined: list[dict] = []

        # ------------------------------------------------- response → cell
        resp_mt_raw = response.get("media_type")
        resp_mt_norm = _split_media(resp_mt_raw)
        # a missing key means "no entity"; an explicit null IS a JSON body
        entity_present = "body" in response

        key = self._status_key(op, row.status)
        target_cell: Optional[Cell] = None
        declared_keys = sorted(
            {c.status_key for c in op.cells} | set(op.excluded_media),
            key=_status_sort_key)
        if key is None:
            row.reasons = [_reason(
                "status_not_declared",
                f"状态码 {row.status} 不匹配任何精确/范围/default 响应声明"
                f"（声明: {', '.join(declared_keys) or '无'}）")]
            return row
        row.status_key = key
        if key in op.excluded_media and not any(
                c.status_key == key for c in op.cells):
            row.reasons = [_reason(
                "status_media_not_json",
                f"状态码 {row.status}（键 {key}）仅声明了非 JSON 媒体类型 "
                f"{op.excluded_media[key]}，不纳入覆盖格")]
            return row
        key_cells = [c for c in op.cells if c.status_key == key]
        json_cells = [c for c in key_cells if c.has_content]
        no_content_cell = next((c for c in key_cells if not c.has_content),
                               None)

        if not json_cells:
            # the declaration for this status is a no-content response
            target_cell = no_content_cell
            if entity_present or resp_mt_norm is not None:
                invalid.append(_reason(
                    "response_entity_mismatch",
                    f"状态码 {row.status} 声明为无 content 响应（无实体），"
                    "但调用返回了实体"))
        elif not resp_mt_norm:
            if not entity_present:
                invalid.append(_reason(
                    "response_entity_expected",
                    f"响应状态 {row.status} 声明了 JSON 媒体类型，"
                    "但调用未返回实体（缺省 body 表示无实体）"))
            else:
                invalid.append(_reason(
                    "response_media_missing",
                    f"响应状态 {row.status} 声明了 JSON 实体且调用携带 body，"
                    "但未给出 response.media_type"))
        elif not is_json_media(resp_mt_norm):
            row.reasons = [_reason(
                "response_media_not_json",
                f"响应媒体类型 {resp_mt_raw} 非 JSON，不纳入覆盖格")]
            return row
        else:
            declared = {_split_media(c.media_type): c for c in json_cells}
            if resp_mt_norm not in declared:
                invalid.append(_reason(
                    "response_media_not_declared",
                    f"状态码 {row.status} 未声明媒体类型 {resp_mt_raw}，"
                    f"已声明: {sorted(declared)}"))
            elif not entity_present:
                invalid.append(_reason(
                    "response_entity_expected",
                    f"响应状态 {row.status} 的 {resp_mt_raw} 格要求 JSON 实体，"
                    "但调用未返回 body（缺省 body 表示无实体）"))
            else:
                target_cell = declared[resp_mt_norm]
                row.media_type = target_cell.media_type

        # ------------------------------------------------- request params
        found = find_operation(self.graph, op.path, method.lower())
        op_dict, _item, path_params = found
        params = gather_parameters(self.graph, op_dict, path_params,
                                   op.path, method.lower())

        for name in sorted(path_values):
            raw_value = path_values[name]
            node = params[name][1] if name in params else None
            und, errors = _check_value(
                node, raw_value, f"路径参数 {name!r}={raw_value!r}")
            if und:
                undetermined.append(_reason("parameter_schema_undecidable",
                                            und, parameter=name,
                                            location="path"))
            elif errors:
                invalid.append(_reason("parameter_invalid",
                                       "; ".join(errors[:2]),
                                       parameter=name, location="path"))

        provided = {
            "query": obj.get("query") or {},
            "header": obj.get("headers") or {},
            "cookie": obj.get("cookies") or {},
        }
        header_lower = {str(k).lower(): k
                        for k in provided["header"] if isinstance(k, str)}
        labels = {"query": "查询参数", "header": "请求头参数",
                  "cookie": "Cookie 参数"}
        for pname in sorted(params):
            param, node = params[pname]
            loc = param.get("in")
            if loc not in labels:
                continue
            bag = provided[loc]
            if loc == "header":
                actual_key = header_lower.get(pname.lower())
                present = actual_key is not None
                value = bag.get(actual_key) if present else None
            else:
                present = pname in bag
                value = bag.get(pname)
            if not present:
                if is_required(param):
                    invalid.append(_reason(
                        "missing_required_parameter",
                        f"缺少必填{labels[loc]} {pname!r}",
                        parameter=pname, location=loc))
                continue
            und, errors = _check_value(
                node, value, f"{labels[loc]} {pname!r}={value!r}")
            if und:
                undetermined.append(_reason(
                    "parameter_schema_undecidable", und,
                    parameter=pname, location=loc))
            elif errors:
                invalid.append(_reason("parameter_invalid",
                                       "; ".join(errors[:2]),
                                       parameter=pname, location=loc))

        # ------------------------------------------------- request body
        request = obj.get("request")
        req_entity = isinstance(request, dict) and "body" in request
        body_site = _request_body_site(self.graph, op.path, method.lower())
        body_decl = body_site[2] if body_site else None
        if req_entity:
            req_mt_norm = _split_media(request["media_type"])
            if body_decl is None:
                invalid.append(_reason(
                    "request_body_undeclared",
                    "operation 未声明 requestBody，但调用携带了请求体"))
            elif not is_json_media(req_mt_norm):
                undetermined.append(_reason(
                    "request_media_not_json",
                    f"请求体媒体类型 {request['media_type']} 非 JSON，"
                    "无法按 JSON 模式判定请求体约束"))
            else:
                media_map = _media_map(body_decl.get("content"))
                if req_mt_norm not in media_map:
                    invalid.append(_reason(
                        "request_media_not_declared",
                        f"requestBody 未声明媒体类型 {request['media_type']}，"
                        f"已声明: {sorted(media_map) or '无'}"))
                else:
                    original_mt = media_map[req_mt_norm]
                    node = _media_schema_node(self.graph, body_site,
                                              original_mt)
                    und = undecidable_reasons(node, request["body"]) \
                        if node is not None else []
                    if und:
                        undetermined.append(_reason(
                            "request_body_schema_undecidable",
                            f"请求体：{und[0]}", media_type=original_mt))
                    elif node is not None:
                        errors = validate(node, request["body"])
                        if errors:
                            invalid.append(_reason(
                                "request_body_invalid",
                                "请求体不满足 schema: "
                                + "; ".join(errors[:3]),
                                media_type=original_mt))
        elif body_decl is not None and body_decl.get("required"):
            media_map = _media_map(body_decl.get("content"))
            invalid.append(_reason(
                "missing_required_request_body",
                "缺少必填请求体（已声明媒体类型 "
                f"{sorted(media_map) or '无'}）"))

        # ------------------------------------------------- response body
        if target_cell is not None and target_cell.has_content and \
                resp_mt_norm and entity_present:
            node = target_cell.node
            und = undecidable_reasons(node, response["body"]) \
                if node is not None else []
            if und:
                undetermined.append(_reason(
                    "response_body_schema_undecidable",
                    f"响应体：{und[0]}",
                    media_type=target_cell.media_type,
                    status_key=target_cell.status_key))
            elif node is not None:
                errors = validate(node, response["body"])
                if errors:
                    invalid.append(_reason(
                        "response_body_invalid",
                        f"{row.status} 响应体不满足 "
                        f"{target_cell.media_type} schema: "
                        + "; ".join(errors[:3]),
                        media_type=target_cell.media_type,
                        status_key=target_cell.status_key))

        # ------------------------------------------------- verdict
        if undetermined:
            row.outcome = UNDETERMINED
            row.reasons = undetermined + invalid
            return row
        if invalid or target_cell is None:
            row.outcome = INVALID if invalid else UNMATCHED
            row.reasons = invalid
            return row
        row.outcome = COVERED
        row.cell = target_cell.key
        cell_id = (op.path + "|" + op.method, target_cell.key)
        self._covered_by.setdefault(cell_id, []).append(
            {"line": line_no, "status": row.status})
        return row

    @staticmethod
    def _structural_errors(obj, raw_method, raw_path, method) -> list[dict]:
        """Validate the NDJSON record shape itself."""
        errs: list[dict] = []
        if not isinstance(raw_method, str) or not raw_method.strip():
            errs.append(_reason("missing_method", "缺少 method"))
        elif method.lower() not in HTTP_METHODS:
            errs.append(_reason("invalid_method",
                                f"不支持的 HTTP 方法 {raw_method!r}"))
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            errs.append(_reason("missing_path",
                                "缺少以 / 开头的请求路径 path"))
        for field_name in ("query", "headers", "cookies"):
            v = obj.get(field_name, {})
            if v is not None and not isinstance(v, dict):
                errs.append(_reason("malformed_field",
                                    f"{field_name} 必须是对象"))
        request = obj.get("request")
        if request is not None:
            if not isinstance(request, dict):
                errs.append(_reason("malformed_field",
                                    "request 必须是对象"))
            elif not isinstance(request.get("media_type"), str):
                errs.append(_reason("malformed_field",
                                    "request 缺少字符串 media_type"))
        response = obj.get("response")
        if not isinstance(response, dict):
            errs.append(_reason("missing_response", "缺少 response 对象"))
            return errs
        raw_status = response.get("status")
        if isinstance(raw_status, bool) or not isinstance(
                raw_status, (int, str)):
            errs.append(_reason("missing_status",
                                "response 缺少数字 status"))
        else:
            try:
                status = int(raw_status)
            except (ValueError, TypeError):
                status = None
            if status is None or not 100 <= status <= 599:
                errs.append(_reason(
                    "invalid_status",
                    f"response.status {raw_status!r} 不是 100-599 的状态码"))
        if "media_type" in response and not isinstance(
                response.get("media_type"), str):
            errs.append(_reason("malformed_field",
                                "response.media_type 必须是字符串"))
        return errs

    # ------------------------------------------------------------- document
    def build_document(self, rows: list[RowResult]) -> dict:
        counts = {k: 0 for k in OUTCOMES}
        for r in rows:
            counts[r.outcome] += 1

        operations: list[dict] = []
        cells_total = cells_covered = 0
        for op in self.index:
            cells_doc = []
            gaps = []
            for cell in op.cells:
                cell_id = (op.path + "|" + op.method, cell.key)
                by = sorted(self._covered_by.get(cell_id, []),
                            key=lambda x: x["line"])
                covered = bool(by)
                cells_total += 1
                if covered:
                    cells_covered += 1
                else:
                    gaps.append(cell.key)
                cells_doc.append({
                    "status_key": cell.status_key,
                    "status_kind": cell.status_kind,
                    "media_type": cell.media_type,
                    "has_content": cell.has_content,
                    "covered": covered,
                    "covered_by": by,
                })
            operations.append({
                "method": op.method,
                "path": op.path,
                "operation_id": op.operation_id,
                "applicable": bool(op.cells),
                "cells_total": len(op.cells),
                "cells_covered": sum(1 for c in cells_doc if c["covered"]),
                "cells": cells_doc,
                "excluded_media": [
                    {"status_key": k, "media_types": op.excluded_media[k]}
                    for k in sorted(op.excluded_media,
                                    key=_status_sort_key)],
                "gaps": gaps,
            })

        return {
            "schema_version": "1.0",
            "side": self.side,
            "analysis_id": self.analysis_id,
            "source": self.source,
            "summary": {
                "calls_total": len(rows),
                "covered": counts[COVERED],
                "unmatched": counts[UNMATCHED],
                "invalid": counts[INVALID],
                "undetermined": counts[UNDETERMINED],
                "operations_total": len(self.index),
                "operations_applicable": sum(
                    1 for op in self.index if op.cells),
                "cells_total": cells_total,
                "cells_covered": cells_covered,
                "gaps_total": cells_total - cells_covered,
            },
            "operations": operations,
            "rows": [r.to_dict() for r in rows],
        }


# ==================================================================== public
class NDJSONError(ValueError):
    pass


def compute_coverage(graph: SchemaGraph, raw: bytes, *, side: str,
                     analysis_id: str, filename: str) -> dict:
    """Parse NDJSON bytes and build the deterministic coverage document."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NDJSONError(f"NDJSON 不是合法 UTF-8 文本: {exc}") from exc

    rows: list[RowResult] = []
    engine = CoverageEngine(graph, side=side, analysis_id=analysis_id)
    non_empty = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        non_empty += 1
        try:
            obj = json.loads(stripped)
        except Exception as exc:
            rows.append(RowResult(
                line_no, UNMATCHED,
                [_reason("malformed_json", f"JSON 解析失败: {exc}")]))
            continue
        rows.append(engine.evaluate_line(line_no, obj))

    if non_empty == 0:
        raise NDJSONError("NDJSON 不包含任何调用记录（空文件）")

    engine.source = {
        "filename": filename,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "non_empty_lines": non_empty,
    }
    return engine.build_document(rows)
