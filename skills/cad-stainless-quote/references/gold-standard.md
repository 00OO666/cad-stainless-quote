# 人工清单金标准导入与审计

人工报价表只能作为“候选金标准”，不能因为是人工制作就默认正确。导入器保留原始证据、按统一公式复算，并把所有导入行保持为 `REVIEW` 或 `BLOCK`；它从不自动产生 `PASS`。

## 支持范围

- 文件格式：`.xls`、`.xlsx`、`.xlsm`。
- 标准 17 字段：`序号、名称、MT编号、材料、平面图位置、对应立面、对应节点、展开规格、宽、长度、数量、工程量、单位、计价方式、单价、金额、备注`。
- 兼容旧式两行表头：其中 `颜色` 可解释为 MT 编号，`展开宽/长/件数` 分别映射为宽、长度、构件数量，外层 `数量` 映射为工程量。
- 自动识别一行或两行复合表头，并兼容常见字段名，例如 `材料名称/名称`、`位置（图号）`、`材料代号`、`材料特征描述`、`宽/展开尺寸`、`高/长`、`计量/数量`、`计算方式` 和 `合计/金额`。
- `材料代号/材料编号` 单独保存在 `source_material_code`。只有明确符合 MT 语法的值才可派生 MT；其他项目编码不得伪装成 MT，缺失 MT 的行保持阻断待审。
- 每个导入行保留工作表、Excel 行号、单元格坐标、原始值、数值格式、公式（`.xlsx/.xlsm` 可得时）和字段到单元格的映射。
- `.xlsx/.xlsm` 同时保留公式、缓存显示值、普通/数组公式类型及数组范围。没有缓存值且无法可靠复算时，必须产生 `REVIEW/BLOCK`，不能把公式文本冒充数值。
- 工作表内的嵌入图片和 `DISPIMG` 图片单元格会记录锚点、证据类别与所在行；当前只建立图片证据索引，不对图片内容做 OCR。
- 对 `.xlsx/.xlsm` 可另行只读导出图片原始字节。导出器直接解析 OOXML 关系，普通嵌入图按 drawing 锚点定位，WPS `DISPIMG` 按公式 ID 关联 `cellimages.xml`；图片使用内容哈希命名，不改写源工作簿。
- 工作表的逻辑边界按实际有值的单元格、合并区域和图片锚点计算，忽略仅由远端空白样式造成的夸大 OOXML dimension。

## 复算规则

- 面积：`宽(mm) × 长度(mm) × 数量 ÷ 1,000,000`。
- 延米：`长度(mm) × 数量 ÷ 1,000`。
- 件/套：`数量`。
- 金额（仅原表同时有工程量、单价、金额，且金额公式只引用这两个字段时）：`工程量 × 单价`。若原表金额公式还引用安装费、税费或其他价格分量，保留公式和缓存值并标记 `AMOUNT_NOT_CALCULABLE`，不得按材料单价误判为金额错误。

工程量比较使用原单元格的显示精度。例如 `0.000` 的显示量级是 `0.001`，允许的舍入差为半个显示量级 `0.0005`。这样不会因为 Excel 浮点存储或临界舍入方式产生假异常。超过该容差但不超过复算值 1% 的行记为 `QUANTITY_ROUNDING_DEVIATION`；更大的偏差记为 `QUANTITY_FORMULA_MISMATCH` 并阻断。

## Python 与独立命令用法

```python
from cadquote.gold import import_gold_workbook
from cadquote.gold_images import export_gold_image_assets

result = import_gold_workbook("人工不锈钢清单.xls")
result.write_json("runs/example/gold-import.json")
print(result.summary.model_dump())

# XLSX/XLSM：原始图片写入 assets/，同目录生成 manifest.json。
image_source = "人工不锈钢清单.xlsx"
image_gold = import_gold_workbook(image_source)
images = export_gold_image_assets(
    image_source,
    "runs/example/gold-images",
    gold_result=image_gold,
)
print(images.asset_count, images.issues)
```

通过 SKILL 启动器导入：

```powershell
& .\scripts\run.ps1 gold-import "人工不锈钢清单.xls" `
  --out "runs\example\gold-import.json"

# 导入清单的同时导出图片证据
& .\scripts\run.ps1 gold-import "人工不锈钢清单.xlsx" `
  --out "runs\example\gold-import.json" `
  --image-assets-dir "runs\example\gold-images"

# 也可独立执行图片资产导出
& .\scripts\run.ps1 gold-image-export "人工不锈钢清单.xlsx" `
  --out "runs\example\gold-images"
```

图片 manifest 每个证据项至少包含 `sheet、cell、row、category、formula_id、sha256、package_path、export_relative_path`。同一原始图片被多个单元格引用时，manifest 保留每个引用，但内容相同的文件只落盘一次。所有路径均为包内路径或相对导出路径；绝对源路径不会写入 manifest。

旧 `.xls` 不是 OOXML ZIP 包。当前图片导出器不会猜测或静默忽略其 OLE/BIFF 图片，而是在 manifest 中写入 `LEGACY_XLS_IMAGE_EXPORT_UNSUPPORTED` 阻断问题；清单行数据仍可按既有 `xlrd` 路径导入。

## 主 CLI 行为

- 位置参数 `workbook`；
- 必填的 `--out`；
- 可选 `--image-assets-dir`：只读导出 XLSX/XLSM 图片并写 `manifest.json`；
- 输出终端摘要：行数、`REVIEW/BLOCK` 数量、问题总数和输出路径；MT/单位分布及逐类偏差保存在完整输出 JSON 中；
- 进程退出码：读取/解析异常或没有识别到报价行时非零；零行仍会生成 `row_count = 0` 的审计 JSON 供诊断。行级偏差会保存在输出 JSON 中，不能被自动提升为 PASS。

不要在 CLI 层改变审核状态，也不要把导入结果直接覆盖自动识别结果。金标准与预测结果应保存在不同文件中，再由评估阶段按稳定行 ID/业务键比较。

## 私有回归数据政策

真实客户清单、由其派生的统计指纹，以及对应的断言都不得提交到公开源码仓库。组织可以在仓库外维护经合法授权的私有测试套件，并调用同一个导入器执行回归；公开贡献只能使用合成数据或明确允许再分发的数据。
