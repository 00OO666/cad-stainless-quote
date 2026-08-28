# 人工确认与恢复运行

首次分析会生成 `<run-dir>/review-pack.json`。它按构件列出候选尺寸、候选跨图关系、来源图纸和实体 ID。审核人可以选择一个候选，或用多个真实候选组成可审计公式；不能把游离数字或猜测伪装成 CAD 证据。

确认文件支持推荐结构：

```json
{
  "schema_version": "1.0",
  "components": {
    "component:...": {
      "reviewer": "张三",
      "reviewed_at": "2026-08-26T10:00:00+08:00",
      "reason": "核对 A-E-01 与 DT-01 局部证据图",
      "selected": {
        "plan_to_elevation_edge": "edge:...",
        "elevation_to_detail_edge": "edge:...",
        "unit": "㎡",
        "pricing_method": "按实际展开面积计算",
        "unfolded_spec": "measurement:...",
        "length": "measurement:...",
        "quantity": "measurement:..."
      }
    }
  }
}
```

如果该构件确实没有对应节点/大样，不能因为“系统没找到候选”就自动写成“无”。审核人必须在已选立面上完成负向检索，并提交可审计的 `detail_requirement`：

```json
{
  "schema_version": "1.0",
  "components": {
    "component:...": {
      "reviewer": "张三",
      "reviewed_at": "2026-08-26T10:00:00+08:00",
      "reason": "核对立面索引、构造标注和同图号拆分面板",
      "selected": {
        "plan_to_elevation_edge": "edge:...",
        "detail_requirement": {
          "kind": "not_applicable",
          "basis": "立面已给出全部计量尺寸，且没有节点或大样索引",
          "searched_sheet_ids": ["panel:ELEVATION_ID"],
          "reference_entity_ids": ["panel_entity:OPTIONAL_REFERENCE_ID"]
        },
        "unit": "㎡",
        "pricing_method": "按投影面积计算",
        "unfolded_spec": "measurement:...",
        "length": "measurement:...",
        "quantity": "measurement:..."
      }
    }
  }
}
```

`searched_sheet_ids` 必须至少覆盖当前构件所选立面；可选的 `reference_entity_ids` 必须真实存在且位于已检索图纸。该选择不能与 `elevation_to_detail_edge` 同时出现。验证通过后报价字段写为 `对应节点=无`，展开、长度和数量只能绑定该立面或同图号立面面板中的真实 CAD 候选。缺节点候选、截图空白或模型低置信度都不能自行触发此例外。

单位决定必选项：

- `㎡`：展开规格、长度、数量；
- `m`：长度、数量；
- `件` 或 `套`：数量。

确认关系边时，边必须真实存在，并且其源图和目标图确实属于该构件；填入任意 ID 不能让项目变为 PASS。任何非空 `selected` 都必须同时填写非空 `reviewer`、带时区的合法 ISO 8601 `reviewed_at` 和非空 `reason`，否则整份确认文件不会被应用，并产生阻断问题。读取历史文件时兼容旧字段 `timestamp`，但写回、模板和新文件统一使用 `reviewed_at`。

旧版扁平格式 `{ "component:id": { "length": "measurement:id" } }` 继续兼容读取和复核，但因为缺少完整审计元数据，会产生 `LEGACY_CONFIRMATIONS_UNAUDITED` 阻断问题，不能得到最终商业 PASS。要解除阻断，必须改写为上述推荐结构并补齐审核信息。

## 多段尺寸组合与规格取轴

当人工算法需要把同一构件的多段 CAD 尺寸相加、对称重复，或从宽×高×深规格中选取计量轴时，不能直接填一个计算结果。应使用 `derived_measurement`，为每个符号绑定 review-pack 中真实存在的尺寸候选：

```json
{
  "kind": "derived_measurement",
  "expression": "left+middle+right*2",
  "value_expression": "left+middle+right*2",
  "terms": [
    {"symbol": "left", "candidate_id": "measurement:..."},
    {"symbol": "middle", "candidate_id": "measurement:..."},
    {"symbol": "right", "candidate_id": "measurement:..."}
  ],
  "unit": "mm",
  "basis": "同一已确认立面中的连续尺寸链，right 为左右对称段"
}
```

- `expression` 决定报价表保留的可读规格/公式及段落顺序；
- `value_expression` 决定该字段用于算量的数值，省略时与 `expression` 相同。例如规格显示为 `width*height*depth`、计量取宽时，可写 `expression: "width*height*depth"` 和 `value_expression: "width"`；
- 公式中的尺寸只能用 `terms` 声明的符号。允许 `*2`、`/2` 这类 1–100 的整数重复系数，不允许写入未绑定 CAD 候选的 `+7080` 一类自由尺寸；
- 数量公式只能引用数量候选，尺寸公式只能引用尺寸候选；候选必须属于当前物理构件，并位于选定平面→立面→节点链或同图号拆分面板中；
- 数量确认的 `basis` 必须说明乘数语义：整件重复、左右门梃、包框边数或重复面板等。一个材料引线、一个 MT 文本或一个可见总成均不能自动证明数量为 1；内部边/面乘数若已写进表达式，不得再作为外层数量重复相乘；
- 组合结果会生成新的稳定 measurement ID，保存源候选 ID、图纸 ID、实体 ID、公式、审核原因，并进入 `component_to_dimension` 证据边。

## 不锈钢屏风与艺术玻璃按一条复合构件确认

当已选平面→立面→节点链证明不锈钢框架与艺术玻璃是同一樘屏风时，在 `selected` 中加入
`composite_assembly`。确认文件只引用 review-pack 已存在的材料规格和 CAD 实体，不手填
玻璃编号或名称：

如果局部构件区域已经同时发现屏风不锈钢标注与关联玻璃编号/引线，系统即使尚未收到
本段确认，也必须保留一条 `screen_with_glass` 复合候选。该候选至少为 REVIEW，单价和
金额为空；若同时缺少材料、图链或尺寸等硬证据，整行可以进一步 BLOCK。确认的作用是
审定同构件归属与材料证据，不能以“未确认”为由把候选降成纯不锈钢项目或忽略玻璃。

```json
{
  "composite_assembly": {
    "kind": "single_line_composite",
    "assembly_type": "screen_with_glass",
    "billing_basis": "whole_elevation_projection",
    "required_material_roles": ["glass_infill"],
    "included_materials": [
      {
        "role": "glass_infill",
        "material_spec_id": "material:REPLACE_WITH_REVIEW_PACK_ID"
      }
    ],
    "basis": "不锈钢框架与玻璃属于同一物理屏风，整樘按立面投影面积计量",
    "projection_width_candidate_id": "measurement:REPLACE_WITH_NATIVE_HORIZONTAL_DIMENSION_ID",
    "projection_length_candidate_id": "measurement:REPLACE_WITH_NATIVE_VERTICAL_DIMENSION_ID",
    "projection_component_entity_id": "entity:REPLACE_WITH_MT_BOUND_SCREEN_INSERT_ID",
    "projection_axis_basis": "原生水平/垂直DIMENSION端点覆盖已绑定屏风INSERT外框",
    "evidence_ids": [
      "entity:REPLACE_WITH_SAME_COMPONENT_GLASS_EVIDENCE_ID"
    ]
  }
}
```

恢复运行会从所选 `MaterialSpec` 解析玻璃编号和名称，并生成
`component_to_material` 证据边。必须同时满足：

- 输出只有一条复合屏风记录，不再生成独立玻璃报价行；
- `unit=㎡`，工程量固定为 `width_mm*length_mm*quantity/1000000`，宽、高和数量均绑定
  当前构件；
- 整樘宽、高不能只凭 `WD/HT` 属性或“取同页最大值”放行。`projection_component_entity_id`
  必须是当前 MT occurrence 直接包含或父块链指向的有界 `INSERT`；宽、高候选必须来自原生
  `DIMENSION`，且 `defpoint2/defpoint3` 分别覆盖该 INSERT 外框的水平/垂直跨度。任一证明
  缺失时保持 REVIEW/BLOCK，价格和金额为空；
- 规格中的第三轴/构造深度只用于说明构造，不参与立面投影面积，也不能当成数量；
- 材料栏同时列出主不锈钢与玻璃编号/名称，备注注明含玻璃且不拆分；
- 玻璃材料编号、名称或同构件证据缺失/冲突时保持 REVIEW，单价和金额为空；
- 玻璃材料证据还必须与已审定的屏风 `INSERT` 物理边界一致：证据属于该 INSERT 的直接
  父块，或唯一引线目标/证据点落入该 INSERT 外框的小公差范围；仅仅离 MT 锚点更近
  不能成为 PASS 依据；
- 玻璃证据必须精确含有所选 `GC-GL-*` 编号；“艺术玻璃”等同名文字只能触发 REVIEW
  候选，不能证明某个具体玻璃编号；一个实体同时出现多个玻璃编号同样不构成唯一材料
  证明；即使图纸里的 `GC-GL-*` 尚未出现在材料表，也必须保留未解析复合候选。自动
  name-only 信号保持 REVIEW；若确认文件把它显式声明为某个编号的证明，该无效确认必须
  BLOCK；
- 一个玻璃证据实体只能被一个 `component_id`/报价行认领。确认文件若让同一实体出现在
  不同构件的 `included_materials` 或 `evidence_ids` 中，相关确认必须阻断或退回复核；
- 只有包含材料编号集合及 `whole_elevation_projection` 口径完全一致的复合价格条目才能
  匹配，纯不锈钢价格不能匹配。

## 显示列与实际计价拓扑不一致

默认工程量只使用固定公式：面积为 `width_mm*length_mm*quantity/1000000`，延米为 `length_mm*quantity/1000`，件/套等于 `quantity`。但有些经 CAD 证实的物理构造会出现以下情况：报价表仍显示一个构件，实际包含两条独立计价线；或延米应取“宽”列所示的平面长边，而不是“长度”列。此时不能篡改显示数量来凑结果，也不能把人工工程量直接复制为答案，可在该构件的 `selected` 中增加：

```json
{
  "engineering_quantity": {
    "kind": "engineering_quantity_expression",
    "expression": "length_mm*2/1000",
    "basis": "立面与节点证实同一构件含两条独立 3000 mm 实体线；数量列仍表示一个总成",
    "evidence_ids": ["entity:LEFT_RUN", "entity:RIGHT_RUN"]
  }
}
```

另一种合法形式是 `width_mm*quantity/1000`，但前提是 `width` 已绑定当前构件、且 CAD 证实该轴才是延米计价轴。规则如下：

- 表达式必须引用 CAD 已确认字段，不能是 `7.25` 这类目标值常量。`m` 必须以 `/1000` 结束且分子量纲为长度；`㎡` 必须同时引用 `width_mm`、`length_mm` 并以 `/1000000` 结束；`件/套` 只能引用 `quantity`；
- 分子中的拓扑倍数只允许 1–100 的正整数，不允许任意小数系数或第二个除法。`quantity` 只能作为正向乘法因子出现一次；当显示数量不为 1 时必须实际参与表达式，不能写进公式后再抵消；
- 函数、单元格引用、任意变量、量纲混加、错误换算、非正/非有限/过大数字、过长公式及除零均被阻断。表达式直接返回报价单位下的最终工程量；
- `basis` 与 `evidence_ids` 必填。每个证据 ID 必须是真实 CAD 实体，并位于已确认的平面→立面→节点链上；
- 该表达式只描述计价轴或构件内部拓扑，不替代物理构件发现、尺寸候选确认或数量确认；
- takeoff 会为每个证据实体生成 `component_to_engineering_quantity_evidence` 边，绑定构件、实体、完整表达式、依据、来源图纸、handle 与 bbox。单独执行 `quote` 或直接调用导出器时，必须同时携带表达式和依据完全一致的 PASS 证据边；旧公式的边不能复用；
- Excel 的 17 个业务字段不增加列。系统把安全表达式编译成工程量单元格公式，在工程量批注和“来源追踪”表中保存依据；无此确认时仍使用固定公式；
- 该机制不得用于对答案表做目标拟合。若 CAD 不能证明换轴或内部倍数，项目保持 REVIEW/BLOCK。

## 显式归并物理构件

当 review-pack 中多个候选项经人工核图后确认其实是同一个物理构件，可在保留项的 `selected` 中加入 `merge_component_ids`。值可以是组件 ID 数组，也可以是逗号分隔字符串：

```json
{
  "schema_version": "1.0",
  "components": {
    "component:TARGET": {
      "reviewer": "张三",
      "reviewed_at": "2026-08-26T10:00:00+08:00",
      "reason": "平面与门套立面指向同一根连续收口构件",
      "selected": {
        "merge_component_ids": [
          "component:SOURCE-1",
          "component:SOURCE-2"
        ]
      }
    }
  }
}
```

归并是原子操作，必须满足以下条件：

- 目标和所有来源 ID 都真实存在于本次 review-pack，且 MT 编号一致；
- 一个来源组件只能被一个目标认领；
- 目标与来源的非空房间、构件名称必须一致；
- 不支持嵌套归并；需要时把所有来源 ID 直接列在最终保留目标下；
- 错误或冲突归并不会抑制来源项，也不能让目标项变为 PASS。

成功归并后，来源项从算量输出中抑制，其平面、立面、节点和 occurrence 证据合并到目标项，并生成显式 PASS 的 `occurrence_to_component` 证据边。没有人工归并确认时，系统不得仅因 MT 相同就猜测多个标注属于同一物理构件。

完成确认后运行：

```powershell
& .\scripts\run.ps1 resume "C:\runs\项目名" `
  --confirmations "C:\review\confirmations.json" `
  --price-book "C:\prices\approved-price-book.json"
```

恢复运行复用原有解包、转换和索引，只重做证据选择、算量、价格匹配与报价导出。
