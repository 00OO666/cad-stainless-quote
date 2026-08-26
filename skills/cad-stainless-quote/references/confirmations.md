# 人工确认与恢复运行

首次分析会生成 `<run-dir>/review-pack.json`。它按构件列出候选尺寸、候选跨图关系、来源图纸和实体 ID。审核人只能从其中选择候选，不能把猜测伪装成 CAD 证据。

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

单位决定必选项：

- `㎡`：展开规格、长度、数量；
- `m`：长度、数量；
- `件` 或 `套`：数量。

确认关系边时，边必须真实存在，并且其源图和目标图确实属于该构件；填入任意 ID 不能让项目变为 PASS。任何非空 `selected` 都必须同时填写非空 `reviewer`、带时区的合法 ISO 8601 `reviewed_at` 和非空 `reason`，否则整份确认文件不会被应用，并产生阻断问题。读取历史文件时兼容旧字段 `timestamp`，但写回、模板和新文件统一使用 `reviewed_at`。

旧版扁平格式 `{ "component:id": { "length": "measurement:id" } }` 继续兼容读取和复核，但因为缺少完整审计元数据，会产生 `LEGACY_CONFIRMATIONS_UNAUDITED` 阻断问题，不能得到最终商业 PASS。要解除阻断，必须改写为上述推荐结构并补齐审核信息。

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
