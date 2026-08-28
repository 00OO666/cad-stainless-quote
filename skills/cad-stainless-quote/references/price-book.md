# 经批准价格库

只有用户提供或明确批准的价格库可以进入金额计算。模板位于
`assets/price-book-template.json`，其 `approved` 默认均为 `false`，防止模板被误当成真实价格。

## 匹配键

每条价格必须同时匹配：

- MT 编号；
- 材料、牌号、厚度、表面处理、加工方式；
- 计价方式和单位；
- 价格条目与整个价格库都已批准。

一个候选都没有或存在多个同等候选时，不自动选价。材料字段为空时，带材料限定的价格条目也不能匹配。

### 单行复合构件

不锈钢屏风与艺术玻璃经 CAD 证明属于同一物理构件时，按
`single_line_composite` 一条报价，不拆成不锈钢价和玻璃价。价格条目除上述主材键外，
还必须显式填写：

- `included_material_codes`：包含材料编号集合，至少包含已确认的玻璃编号；
- `composite_billing_basis=whole_elevation_projection`；
- 单位 `㎡`，计价口径与整樘立面投影一致。

匹配时比较完整的包含材料编号集合和复合计量口径，不能把缺少玻璃的纯不锈钢价格
当作复合屏风价格。报价项缺玻璃编号、名称、同构件材料证据，或价格库没有完全一致的
复合条目时，单价和金额保持空白，状态为 REVIEW。

## JSON 格式

```json
{
  "version": "2026-Q3-v1",
  "approved": true,
  "source": "采购部批准单",
  "entries": [
    {
      "id": "price:mt01-area-304-12",
      "version": "2026-Q3-v1",
      "approved": true,
      "mt_code": "MT-01",
      "material": "古铜色不锈钢",
      "grade": "304",
      "thickness_mm": 1.2,
      "finish": "PVD",
      "process": "折弯",
      "pricing_method": "按实际展开面积计算",
      "unit": "㎡",
      "unit_price": 500,
      "currency": "CNY",
      "tax_included": true,
      "valid_from": "2026-07-01",
      "valid_to": "2026-09-30",
      "source": "采购部批准单第12行",
      "note": null
    }
  ]
}
```

也支持 `.xlsx`，默认读取“价格库”工作表。商业 PASS 的必要列是：

`版本、批准、MT编号、材料、牌号、厚度、表面处理、加工方式、计价方式、单位、单价、币种、含税、价格来源`。

`ID、生效日期、失效日期、备注` 可选。缺少任一必要列时，加载阶段直接报错；这些字段不能作为通配符静默匹配。

复合构件价格可使用可选列 `包含材料编号、复合计量口径`，分别映射到
`included_material_codes` 和 `composite_billing_basis`。一旦任一列有值，该价格条目只参与
复合构件匹配；普通纯不锈钢报价项不得匹配它。反过来，复合构件也不得匹配这两列均为空
的普通价格条目。

`valid_from` 和 `valid_to` 使用 ISO 日期并按报价基准日期校验；未生效、已过期或日期格式无效的条目不能匹配。价格还必须明确币种与含税口径；默认报价币种为 CNY。
