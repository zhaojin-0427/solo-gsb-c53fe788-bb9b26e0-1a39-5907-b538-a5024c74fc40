# OpenAPI 契约演进工作台

在服务端升级场景下，对两份 **OpenAPI 3.x** 文档（基线 / 候选）做契约兼容性比对：

- **请求模式不可收窄**：升级后的服务端必须继续接受旧客户端能发送的一切；
- **响应模式不可放宽**：升级后的服务端不得返回旧客户端无法理解的新形态。

对每个端点、每个媒体类型给出 `兼容 / 破坏性 / 无法判定` 判定，破坏性结果定位到
**JSON Pointer**、解释命中的规则，并生成一份**只在单侧校验通过的最小 JSON 反例**，
可直接在页面上**重放**（对两侧模式重新校验）。端点被删除（`ENDPOINT_REMOVED`）
时，反例是一份**最小 JSON 请求**（方法、已代入具体值的路径、必填 query/header/
cookie 参数、适用时的最小请求体）：重放按两份文档核对**端点存在性**与**基线请求
约束**，明确展示“基线可接受、候选无此端点”。参数可选变必填 / 新增必填、参数或
requestBody 被删除（`PARAMETER_MADE_REQUIRED` / `PARAMETER_ADDED_REQUIRED` /
`PARAMETER_REMOVED`）、响应状态码删除（`STATUS_REMOVED`）、媒体类型删除
（`MEDIA_REMOVED`）等**结构性 finding** 没有可双侧校验的根 schema，其反例是一份
**可编辑的结构化描述符**，重放按 finding 语义分别核对基线是否声明/接受该参数、
请求体或响应、候选是否缺失或收紧，而不再因缺少根 schema 指针而失败。

## 能力一览

- 解析内部与**本地跨文件** `$ref`（ZIP 文档包内任意相对路径）；外部
  `http(s)://` 引用不会被抓取，按“无法解析”报告；
- 展开并合并 `allOf`，识别 `oneOf` / `anyOf` 分支（按结构指纹配对）；
- 比较 `required`、`nullable`（OAS 3.0）/ `type: null`（3.1）、枚举、
  `type`、数值与长度边界、`multipleOf`、数组 `items`、
  `additionalProperties` 等；
- **循环引用安全终止**（自引用、相互引用均不会死循环，统计中可见终止次数）；
- `pattern`、`not`、`if/then/else`、`contains`、`prefixItems` 等
  **未支持的关键字一律标记为“无法判定”**，绝不静默忽略；
- 结果可按严重度（破坏性 / 无法判定 / 兼容）筛选，按端点分组或扁平浏览；
- **调用覆盖视图**：选基线或候选文档上传 **NDJSON** 调用记录，先精确路径、
  再唯一模板归并 operation（多模板命中记未归属），按“状态键 × JSON 媒体类型”
  组成覆盖格，仅当请求参数 / 请求体 / 响应体的可判定约束全部通过才覆盖该格；
  页面展示已覆盖格/总格、零分母“无适用格”与逐行原因，结果随分析持久化并
  确定性导出 JSON；
- **确定性 JSON 导出**（键排序、无多余空白），响应头携带 SHA-256 指纹；
- PostgreSQL 持久化分析记录与原始文档包（用于反例重放）；数据库不可用时
  自动降级为进程内存储；
- Docker Compose 一键启动（Web + PostgreSQL）。

## 一键启动（Docker Compose）

需要本机已安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后访问：

- 工作台页面：<http://localhost:8000/>
- API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>

`docker compose down` 停止；加 `-v` 同时删除数据库卷。

### 上传内容

- 单个 `.yaml / .yml / .json` OpenAPI 文档；
- 或一个 `.zip` 文档包：包内可含多份相互引用的 YAML/JSON 文件，
  根文档通过 `openapi` 字段自动识别（歧义时优先 `openapi.*` / `index.*`）。

仓库 `examples/baseline` 与 `examples/candidate` 是一份刻意包含各类变化的示例
（跨文件引用、`allOf`、`oneOf`、循环引用、枚举收窄/放宽、nullable、
`pattern` 未支持关键字等），可将两个目录分别打成 ZIP 后上传。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：启动 PostgreSQL 后通过环境变量指向它；不配置则使用内存存储
export DATABASE_URL="postgresql://openapi:openapi@localhost:5432/openapi_contract"

uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pytest -q
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/analyses` | multipart 上传 `baseline`、`candidate`，创建分析 |
| `GET` | `/api/analyses` | 历史分析列表 |
| `GET` | `/api/analyses/{id}` | 获取完整报告 |
| `GET` | `/api/analyses/{id}/export?severity=breaking,undetermined` | 确定性 JSON 导出（可按严重度筛选） |
| `POST` | `/api/analyses/{id}/findings/{fid}/replay` | 对反例（可在 body 中用 `{"instance": ...}` 覆盖）做双侧校验重放 |
| `DELETE` | `/api/analyses/{id}` | 删除分析 |
| `POST` | `/api/analyses/{id}/coverage/{side}` | multipart 上传 `calls`（NDJSON），计算并保存 `side=baseline|candidate` 的调用覆盖 |
| `GET` | `/api/analyses/{id}/coverage` | 列出已上传覆盖的文档侧 |
| `GET` | `/api/analyses/{id}/coverage/{side}` | 获取某一侧的覆盖结果 |
| `GET` | `/api/analyses/{id}/coverage/{side}/export` | 确定性导出覆盖 JSON（`X-Coverage-Fingerprint` 头携带 SHA-256） |
| `DELETE` | `/api/analyses/{id}/coverage/{side}` | 删除某一侧的覆盖结果 |
| `GET` | `/api/health` | 服务与存储状态 |

导出响应头：

- `Content-Disposition: attachment; filename="contract-report-<id>-<severity>.json"`
- `X-Report-Fingerprint: <sha256>`（与导出字节一一对应，可用于校验确定性）

## 调用覆盖（Call Coverage）

在比对结果下方的“**3. 调用覆盖**”面板，选择**基线**或**候选**文档侧，
上传一份 **NDJSON** 调用记录（每行一次独立调用），工作台现场依据所选文档
重新推导 operation 与 schema 约束，给出每个 operation 的覆盖格与逐行原因；
结果随分析保存（PostgreSQL 或内存降级存储），可重复上传覆盖、可确定性导出。

### NDJSON 每行格式

顶层字段：`method`、`path`（具体请求路径，不含 query string）、`query`、
`headers`、`cookies`（均为对象，可省略）；`request` / `response` 为对象，
含 `media_type` 与 JSON `body`，`response` 还必须含整数 `status`。
**缺省 `body` 表示该请求/响应无实体**（HTTP 无 content）。

仓库自带可直接上传基线的示例：[`examples/calls-baseline.ndjson`](examples/calls-baseline.ndjson)。

```json
{"method":"get","path":"/pets","query":{"status":"available","limit":10},"headers":{},"cookies":{},"response":{"status":200,"media_type":"application/json","body":[{"id":1,"name":"Rex","status":"available"}]}}
{"method":"get","path":"/pets/7","response":{"status":404}}
{"method":"post","path":"/pets","request":{"media_type":"application/json","body":{"name":"Doggo"}},"response":{"status":201,"media_type":"application/json","body":{"id":9,"name":"Doggo"}}}
```

### 归并与建格规则

1. **路径归并**：先匹配**精确路径**（如 `/pets`），未命中再匹配**唯一**路径
   模板（如 `/pets/{petId}`，正则 `[^/]+` 提取路径参数）；同一具体路径同时
   命中多个模板时记为**未归属**（`ambiguous_template`），不计入任何 operation；
   无任何命中记 `path_not_matched`。
2. **覆盖格**：每个 operation 的格由响应状态键 × JSON 媒体类型组成：
   - 状态键：响应实际 `status` 依次匹配精确值（`200`）→ 范围键
     （`2XX`，按百位归类）→ `default`；都不匹配记 `status_not_declared`；
   - `content` 为空的响应单列一个“**无实体**”格（媒体类型显示 `∅`）；
   - 仅 `application/json` 与 `*+json` 建格；`text/plain` 等**非 JSON
     媒体排除**（operation 下列出“非 JSON 媒体（不建格）”）；整个 operation
     没有可建格时标记“**无适用格**”，分母为零、不计入覆盖率。
3. **覆盖判定**：仅当一次调用在**请求参数、请求体、响应体**三个位置的所有
   **可判定**约束均通过时，对应格才标记为已覆盖：
   - query/header/cookie/path 参数按声明 schema 校验（传输层字符串会按
     `integer/number/boolean` 尝试还原）；缺失必填参数记校验失败；
   - 请求体按 `request.media_type` 对应媒体的 schema 校验；声明必填 requestBody
     但调用无实体记校验失败；请求媒体非 JSON 记**无法判定**；
   - 响应体按格媒体的 schema 校验；声明 JSON 实体但调用无实体（或缺
     `media_type`）记校验失败；无实体格携带实体同样记校验失败；
   - schema 含未支持关键字（`pattern` 等）、`$ref` 不可解析或循环引用，
     且该片段被本次载荷实际触达时，记**无法判定**（不覆盖格，也不谎称通过）；
     未触达的可选属性不影响判定。
4. **逐行原因**：每行给出 `covered / invalid / undetermined / unmatched`
   结论与原因码（如 `missing_required_parameter`、`request_body_invalid`、
   `parameter_schema_undecidable`、`ambiguous_template`、
   `status_media_not_json` 等）及中文说明，页面可按结论筛选、按路径/原因搜索。

### HTTP 调用示例

上传（multipart 字段名固定为 `calls`）：

```bash
curl -s -X POST "http://localhost:8000/api/analyses/$AID/coverage/baseline" \
  -F "calls=@examples/calls-baseline.ndjson;type=application/x-ndjson"
```

返回 `201`（节选）：

```json
{
  "schema_version": "1.0",
  "side": "baseline",
  "analysis_id": "f4f8e10573d05dfb",
  "source": {"filename": "calls-baseline.ndjson", "sha256": "…", "non_empty_lines": 9},
  "summary": {
    "calls_total": 9, "covered": 6, "unmatched": 1, "invalid": 2,
    "undetermined": 0, "operations_total": 5, "operations_applicable": 5,
    "cells_total": 7, "cells_covered": 6, "gaps_total": 1
  },
  "operations": [
    {"method": "GET", "path": "/pets/{petId}", "operation_id": "getPet",
     "applicable": true, "cells_total": 2, "cells_covered": 2,
     "cells": [
       {"status_key": "200", "status_kind": "exact",
        "media_type": "application/json", "has_content": true,
        "covered": true, "covered_by": [{"line": 2, "status": 200}]},
       {"status_key": "404", "status_kind": "exact",
        "media_type": null, "has_content": false,
        "covered": true, "covered_by": [{"line": 3, "status": 404}]}
     ],
     "excluded_media": [], "gaps": []}
  ],
  "rows": [
    {"line": 1, "outcome": "covered", "method": "GET", "path": "/pets",
     "operation": {"method": "GET", "path": "/pets"},
     "status": 200, "status_key": "200",
     "media_type": "application/json", "cell": "200|application/json",
     "reasons": []},
    {"line": 8, "outcome": "invalid", "method": "GET", "path": "/pets/999",
     "operation": {"method": "GET", "path": "/pets/{petId}"},
     "status": 200, "status_key": "200", "reasons": [
       {"code": "parameter_invalid",
        "message": "路径参数 'petId'='999'：… 类型 string 不被允许 ['integer']",
        "parameter": "petId", "location": "path"},
       {"code": "response_body_invalid",
        "message": "200 响应体不满足 application/json schema: …",
        "media_type": "application/json", "status_key": "200"}]}
  ]
}
```

查询 / 导出 / 删除：

```bash
curl -s "http://localhost:8000/api/analyses/$AID/coverage"
curl -s "http://localhost:8000/api/analyses/$AID/coverage/baseline"
curl -s -D - "http://localhost:8000/api/analyses/$AID/coverage/baseline/export" \
  -o call-coverage-baseline.json   # X-Coverage-Fingerprint: <sha256>
curl -s -X DELETE "http://localhost:8000/api/analyses/$AID/coverage/baseline"
```

导出字节确定（键排序、无多余空白、无时间戳）：同一份 NDJSON 与文档反复导出
字节一致，`X-Coverage-Fingerprint` 为导出字节的 SHA-256。


## 判定规则（方向：基线 → 候选）

### 请求侧（不可收窄）

新增必填参数/属性、可选变必填、枚举收窄、类型收窄、数值/长度下界抬高或上界压低、
新增 `multipleOf`、`additionalProperties` 变为 `false`、删除参数/媒体类型/端点等
→ **破坏性**。

### 响应侧（不可放宽）

枚举新增取值、类型放宽、允许 `null`、`oneOf` 新增分支、删除必有字段、
数值/长度边界放宽、删除 `additionalProperties:false` 等 → **破坏性**。

### 无法判定

存在未支持的关键字（`pattern`、`not`、`if/then/else`、`contains` …）、
`$ref` 无法解析（含外部引用）、`format` 语义变化、多重非空类型数组、
`oneOf` 分支无法可靠配对、模式自相矛盾等 → **无法判定**，并给出对应 JSON Pointer。

每条破坏性结果包含：

- `rule` / `rule_title` / `rule_detail`：命中规则与解释；
- `pointer.baseline` / `pointer.candidate`：定位（跨文件时另附 `file`）；
- `witness.instance`：最小 JSON 反例；
- `witness.self_check`：生成时对双侧模式的自检结果；
- 重放接口再次独立校验，并给出失败原因列表。

### 端点删除（ENDPOINT_REMOVED）的反例与重放

候选整体移除某个 operation 时，候选侧**不存在可校验的 schema**，因此该结果不
走模式重放，而是生成并核对一份**端点级最小请求**：

- `witness.instance` 形如
  `{"method": "POST", "path": "/shops/5/orders/abc", "query": {...},
  "header": {...}, "cookie": {...}, "body": {...}}`：路径模板占位符已被满足
  基线约束的具体值替换；只包含**必填**参数（可选参数按旧客户端惯例省略）；
  存在**必填**请求体时附最小 JSON `body`，可选请求体则省略并在 `caveats` 说明。
- 重放分别检查两份文档中端点是否存在（候选保留了 path item 但删掉该 method 时
  会明确区分），并把请求重新对照基线 operation 的路径/查询/请求头/Cookie 参数
  与请求体 schema 校验；结论展示“**基线可接受、候选无此端点**”。
- 当基线请求无法**可靠构造**（必填参数的 `$ref` 无法解析、只有
  `application/octet-stream` 等无法综合的请求体媒体类型、最小实例自校验失败
  等），破坏性结论**保留**，但 `witness.verifiable=false` 并给出
  `unverifiable_reason`：页面标记“反例不可验证”，重放不会伪称单侧通过；
  仍可在页面编辑器中手写请求后再次重放。
- 表单类媒体类型（`application/x-www-form-urlencoded` /
  `multipart/form-data`）按结构化 schema 校验 body，并提示未重放实际线缆编码。

反例均可在结果抽屉的 JSON 编辑器中修改后重放；模式级（参数 / 请求体 / 响应中的
schema 变化）反例走双侧 schema 校验，端点级反例走上文的存在性 + 基线约束核对。

### 结构性 finding 的反例与重放

以下 finding 在候选侧（或基线侧）根本不存在可校验的媒体 schema，旧版本会错误地
落入模式重放并报“重放信息缺少根 schema 指针”；现在它们生成**结构化描述符**
反例，重放从存储的两份文档**重新推导**声明与约束：

| 规则 | 反例描述符（可编辑） | 重放核对 |
| --- | --- | --- |
| `PARAMETER_MADE_REQUIRED`（参数） | `{method, path, parameter, in, value}` | 基线声明该参数且**可选**；候选把它收紧为**必填** |
| `PARAMETER_ADDED_REQUIRED`（参数） | 同上 | 基线**未声明**该参数；候选声明且**必填** |
| `PARAMETER_REMOVED`（参数） | 同上 | 基线声明并按参数 schema **接受** `value`；候选**无此参数** |
| `PARAMETER_*`（requestBody） | `{method, path, media_type, body}` | 基线 requestBody 的声明/必填与 `body` 的 schema 校验；候选删除 requestBody 或将其收紧为必填 |
| `STATUS_REMOVED` | `{method, path, status_code, media_type, body}` | 基线声明该状态码（有 schema 时校验 `body`）；候选**无此状态码** |
| `MEDIA_REMOVED` | `{method, path, location, status_code?, media_type, body}` | 基线声明并接受/返回该媒体类型且 `body` 通过其 schema；候选**无此媒体类型** |

抽屉里分别展示“基线核对 / 候选核对”两张卡片（声明情况、`required`、媒体类型、
schema 错误）以及携带值/载荷的独立校验；只有当两侧事实都与 finding 语义一致时
才给出“**反例可验证**”，否则保留破坏性结论并标记“反例不可验证”，可在编辑器中
修改描述符后再次重放。跨文件 `$ref` 的 requestBody / 响应会先解析到实际定义位置
再取 schema。

## 目录结构

```
app/
  api/routes.py          # FastAPI 路由（上传、分析、导出、重放、历史）
  services/
    loader.py            # 单文件 / ZIP 包加载与根文档识别
    pointer.py           # JSON Pointer (RFC 6901) 工具
    resolver.py          # 本地/跨文件 $ref 解析（外部引用拒绝抓取）
    schemagraph.py       # 规范化 Schema 图：$ref、allOf 合并、循环终止
    instance.py          # 最小实例生成 + 模式校验（反例重放基础）
    endpointrequest.py   # 端点级最小 JSON 请求构造与基线请求约束核对
    structuralreplay.py  # 结构性 finding（参数/请求体/状态码/媒体类型增删）的结构化反例与语义重放
    comparator.py        # 兼容性比较引擎与最小反例综合
    coverage.py          # 调用覆盖：NDJSON 归并、覆盖格、约束核对与确定性导出
    replay.py            # 双侧重放校验（模式级 + 端点级）
    rules.py             # 规则目录与严重度
  analysis.py            # 编排：加载→比较→序列化→存储
  serialization.py       # 确定性 JSON 导出
  db.py / store.py       # PostgreSQL 持久化 + 内存降级
  static/                # 原生 JS 前端（无框架、无构建步骤）
examples/                # 基线 / 候选示例文档包、调用记录 NDJSON 示例
tests/                   # pytest（引擎/循环/API + 调用覆盖共 70 项）
```

## 说明与边界

- 比对基于 OpenAPI 3.x 的 Schema 约束语义；`format` 默认不做强制校验，
  因此 format 变化只判“无法判定”而非破坏；
- 反例综合在复杂组合约束下是**尽力而为**：报告的 `self_check` 会如实标注
  是否严格“仅单侧通过”；不满足时可在页面编辑器里手动修改实例后再次重放；
- 不抓取任何网络资源，所有分析均在本地/容器内完成。
