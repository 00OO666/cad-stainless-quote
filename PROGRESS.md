# 项目进度与接管入口

更新时间：2026-09-09。本文只记录可公开的工程进展，不包含客户图纸、报价表、截图、实体标识、实测尺寸、私有路径或客户级评测明细。

## 当前结论

### Candidate resolution and auditable calculation drafts

Private development now distinguishes a resolved dimension role, a calculable
per-instance result, an aggregate quantity and a fully verified quotation row.
Repeated views of the same source component are retained for traceability but
are not summed as separate physical objects. Proposed corrections are kept
apart from the original reference workbook and its evaluation denominator.

The development delivery adapter produces formula-based calculation drafts
with native CAD evidence in a separate image sheet and row-level navigation.
Image bytes remain unchanged; deduplicated embedding reduces memory use
without discarding evidence references. Verification covers missing, textual,
zero and fractional counts, formula recalculation, image relationships and
immutable inputs. An original PNG and an embedded shape are not by themselves
proof that a crop satisfies the complete evidence contract.

This is a private, known-sample research and delivery update, not a new
production resolver. Customer-specific adapters, drawings, images, dimensions,
results and local accuracy statistics are not published. The installed skill
and production pipeline are unchanged; unresolved physical quantities stay
blank and no new autonomous accuracy claim is made.

### Exact native index scope and semantic-binding research

The native index-direction CLI/API now accepts an explicit, canonical list of
drawing page codes, covering custom sheet families without silently broadening
legacy discovery. Material namespaces are excluded, including non-metal codes.
Same-parent attributes, visibility, unique arrow geometry and viewport mapping
remain separate gates. Synthetic tests cover custom families, canonical exact
matching, material-code rejection and ambiguous or hidden symbols. See
[navigation usage and limits](docs/detail-navigation.md).

Private development now traces visible section paths connected outside an index
block, rather than assuming all reference geometry lives inside that block.
Native plan titles with additional words are retained during research discovery.
Repeated outlines, nested projected boundaries and numerical cross-view agreement
still do not by themselves establish material ownership, local units or quantity.
These research modules and case findings remain private; they are not new
automatic quotation resolvers. The optional index API is implemented in source,
but the installed personal skill is unchanged and no end-to-end accuracy or
human-time-saving percentage is claimed.

### Repeatable development replay and file-access diagnostics

Added an optional [file-boundary audit](devtools/replay-boundary-audit/README.md) for developer replay scripts. It records observable Python file access, restricts data inputs to declared sources, separates fresh outputs from preexisting results, and retains source hashes and denied attempts. Synthetic tests exercise direct opens, descriptors, hard links, child-process attempts, and generated CAD/plot outputs. This is not an operating-system sandbox, a security guarantee, or a blind-accuracy certificate.

Private development work now separates semantic CAD retrieval, repeated calculation, frozen predictions, native drawing crops, and subsequent reference comparison. Field-level uncertainty and per-instance facts are retained across the replay comparison; equal totals alone do not establish repeatability. Duplicate boundaries, unresolved units, hidden title blocks, source-version mismatches, and geometry-versus-measurement roles remain explicit gates.

The case-specific extractors, query configuration, customer artifacts, and local scoring remain private. This release adds developer diagnostics rather than a new production quotation resolver; the installed skill and production pipeline are unchanged. Known-sample replay is not whole-package discovery and does not establish held-out accuracy.

### Dimension-scope regression coverage

Added synthetic tests for adjacent substrate and metal profiles that share a horizontal span but occupy different vertical intervals. The tests cover translation, reflection, vertex and dimension-endpoint reversal, equal-valued dimensions at unrelated origins, and input preservation. The existing binding function passes these cases; this change adds regression coverage rather than introducing a new production resolver.

Development diagnostics now explicitly distinguish hidden symbol branches from drawable geometry and separate external section paths from the reference-symbol envelope. These diagnostics are not a new production feature. Neither a bounding-box intersection nor its absence establishes component ownership or proves that a detail does not exist.

Mixed physical groups and fabrication-profile interpretations remain separate from numeric agreement. Customer evidence stays private; no case measurements, drawings, workbooks, or project accuracy statistics are included. This update does not establish held-out CAD-to-quotation accuracy.

### Development review update

Recent development reviews examined native model-to-paper-space binding, nested insert traversal, and physical identity across repeated views. Cross-view deduplication uses shared source entities, viewport visibility, and component boundaries rather than matching labels or numerical similarity. Competing interpretations remain visible in the audit record.

Counterevidence gates distinguish verified geometry from complete takeoff evidence. Missing detail associations, unresolved assembly-depth semantics, and uncertain end treatments remain under review even when candidate quantities agree with a reference. Original workbook artifacts remain separate from proposed corrections and research annotations; preservation of original values, formulas, and embedded content is a required delivery check.

These findings come from development review, not held-out evaluation. They do not establish autonomous end-to-end accuracy or imply that research procedures have been released as production functionality. This progress note contains no customer artifacts or project-level measurements.

### 前批：逐字段核查覆盖

本次仅更新通用研发状态：逐项核查记录进一步区分检查覆盖、证据闭合和数值一致；每个字段保留已证实值、来源候选或未解决原因。未找到绑定关系不能表述为源图缺失，冲突也不能直接归咎人工。客户级结论、统计、尺寸、图号、图像和特例实现均不在本次公开内容中。

工作簿维护继续采用增量编辑，保留既有值、公式及原图字节，补充逐字段审查快照并检查缺失、零值、输入扰动和重算。已知样本的审图成果不等于独立盲测结果。未调整验收容差、未升级安装版、未发布新生产功能或准确率承诺。本次只公开本文；下面为已有的历史研发摘要。

新增批次已把逐行交付标准应用到多类已有开发构件：按投影、展开、延米、套区分计量，独立读取原生几何与尺寸，生成保留全部目标行的累计公式工作簿。已完成行与仅数字通过、尚未闭合的行分开；未知保持空白，不将困难行从分母删除。原图按原PNG字节嵌入，并执行公式空值/零值/扰动恢复及原生Excel只读重算检查。

本批仍是已知样本审图研究。计价方法、目标清单和部分共用节点采用了人工样本辅助；位置/节点审阅标签不属于独立金标准，不能据此声称新CAD的95%全自动准确率。新增研究发现包括同名但不同的原生图框、远离可见属性文字的块原点、原生尺寸显示精度与几何测量值的区别。它们尚未全部接入生产管线；本批未升级安装版，也未修改现有评分容差。

客户级逐行数据、原图、工作簿、源实体标识与尺寸仅留私有审计和项目知识库。此次公开增量只更新本进度文档；下方单行示范和旧阶段结论为历史，不代表当前只研究过一行。

### 前批：逐行交付要求与首条示范

后续每条统一执行[逐行算量交付标准](docs/row-closeout-standard.md)：原CAD证据、独立计算、全字段比较、原图入Excel及重算验证。逐条累积，难项留证且不从分母删除；审图通过与纯自动通过分开记录。本次是固定执行要求和完整工作队列，不新增业务正确行或准确率声明。

本轮从“继续添加查图诊断”转为完整开发行闭合：对已有样本重新执行原CAD几何计算，完成物理范围、端点角色和局部截面单位审阅，生成含原图的公式算量表并经原生Excel只读重算。现有评价器得出单条经审阅的开发行通过；这是已知样本的审图结果，不是任意新CAD的全自动输出，也不是整包95%证明。客户数字、图纸、截图及标签明细不公开。

纠正进度口径：历史局部诊断中的“完整正确行0”有固定未核验标记，不能解释为全表数值准确率0%。人工参考表还存在字段导入错位及仅在图片中的位置/节点标签；新审阅补充层与原始表、原始导入分开保存，数字标签不改。数字容差通过、整行源图审阅通过、商业价格批准必须分开报告；不把缺价格当作算量错误，也不把缺标签自动算正确。

新增4项合成评价回归，保持数量精确约束、缺参考标签未定、商业状态与算量字段评分分离；公开评价模块28项、私有模块27项通过。原有公开材料代码CLI回归保留，旧私有诊断的字段缺口不代表公开入口缺少该适配。既有全表分母与未完成项保留，未新增盲测项目，95%尚未验证，安装版未升级。

### 前批：原生尺寸范围补全

本次补齐两类原生读图遗漏：尺寸引出点落在同一截面边线内部时，可依据原生线性计量轴绑定目标段；同一模型长图在多个纸面窗口中显示时，新增研究API按窗口模型范围并集保留跨窗口尺寸。前者沿用原容差、冲突和非平行支持门禁；后者要求整个模型连接范围被可见窗口覆盖，拒绝模型空隙、重复计数、隐藏和资源截断。纸面距离不拼接为模型长度，尺寸角色与实物件数仍不自动确认。

已在已有开发图纸复跑并补回真实遗漏。也确认人工一条汇总记录可能跨多个立面图页，单一图号或节点不是完整计量范围；不能把同数值检索当作物理归属。新增26项合成回归；公开804 passed / 2 skipped，开发821 passed / 3 skipped，Ruff通过。没有新增完整正确报价行或95%证明，安装版未升级。范围清单API尚未接入生产报价字段，客户明细和原图只留私有记录。

### 前批闭合条带能力

新增闭合条带截面探查，并接入 `detail-routes --probe-native` 的组内输出。原闭合轮廓先拆成两条表面边线和厚度端部，再以恒定间距、完整条带重建及同视口原生尺寸端点约束检验；不能把闭合周长直接当展开宽度。原始坐标几何 API 与毫米 API 分开，未设单位的图不再阻止几何诊断，但仍不生成毫米计量值。

竞争材料接触、曲线、宽线、隐藏/冻结、裁切、原生形状变化及资源截断保留未定。尺寸支持侧仍只是名义边线假设，未直接标注段、另一侧和厚度端部全部留证；不代表制造下料、沿立面计量范围或实际件数已确定。公开命令已在已知开发资料复跑；私有近景另有尺寸裁切及代理渲染限制，没有新增完整正确报价行或95%证明。新增22项合成测试；本轮公开778 passed / 2 skipped，开发795 passed / 3 skipped，公开Ruff通过。安装版未升级。

### 前批组图与开放边线能力

本地新增断开视口组图、右下标题及唯一完整标题分配。组图必须具有同源、同布局、同图框、相同比例/方向、纸面及模型横向坐标对齐等证据；重复图号、重叠视口和不唯一分配不会自动选中。独立组图导航账本保留原有单视口判断，同时明确新增冲突，不能把两者数量相加当报价行数。

组内原生材料边框→引线→各视口独立逆变换→直线轮廓候选已接通；原生纸面/模型尺寸保留坐标、显示倍率与未定尺寸角色，模型间断开的空隙不作为尺寸。新增可选原CAD视图组图像导出，保留来源哈希、像素尺寸、参与视口和排除的相邻标题。CLI可一次输出导航、组内证据、核对表和图片；不自动确认工程量。

权限恢复后，公开版完整导航/探查/原CAD图像入口已在已有开发图包重跑成功；历史权限失败输出保留。图片导出成功不等于逐张截图验收，抽查仍发现部分边缘材料说明裁切。客户资料和实际统计只保存在私有工作区，不是盲测或95%验收。

新增原生尺寸端点约束的双边线判别：同一视口内，材料引线接触边与另一条边须通过恒定法向间距、相同转折和完整条带重建校验；至少两个非平行方向的原生尺寸必须落在同一条边的顶点/合法投影上，才输出名义边线候选。文字覆盖冲突、倍率变化、竞争边线、被裁切路径和资源截断不放行。保留逐段来源与未直接标注段；原单位不变，不将中线/边线自动当制造下料、工程量或件数。新增合成测试，并已用于原生开发资料；没有新增完整正确报价行，95%仍未验证。安装版未升级。

### 前次已发布能力

本轮最终公开合成回归756 passed / 2 skipped，开发工作区773 passed / 3 skipped；公开Ruff通过。早期公开回归发现索引元数据版本差异，现已通过原生尺寸类型/舍入元数据适配修复，未删除失败断言。回归数量不是业务准确率。

本次补齐原生图框恢复与立面标题反查平面索引：旧INSERT插入点不再作为图框范围，按源哈希、原生布局/句柄和视口包含关系单独恢复导航上下文；所属页与标题回指分开。图名回指、出向节点引用、重复平面索引各自留账，原始引用不因未匹配而删除。完整run写入恢复后的独立导航快照；历史resume仍不重算。

纸空间证据渲染改用原生视口矩形筛选，并向绘图前端传递共享bbox缓存，避免无缓存逐视口处理全部模型。真实开发资料已重跑并导出检查原CAD图片。也确认一个节点可能跨多个断开视口，单一视口命中仍不代表完整节点或计量面。新增可读Markdown导航清单；本轮公开合成测试700通过、2跳过。

编号节点导航已实现并接入完整运行的独立输出：分开读取页号与子图号，恢复视口外的节点标题，核对回指立面，保留歧义候选。另有可选的原生材料标注、引线接触与直线轮廓探查，以及自定义五顶点索引箭头和原生视口逆变换。既有整包开发资料已实际试跑，部分图链能够自动缩小到单一节点候选；同材料仍不等于同构件，边线长度不直接当展开宽度。详见 [节点导航说明](docs/detail-navigation.md)。

本次是查图与原生证据连接能力改进，不是完整报价行通过或95%验收，也没有人工计时对照来证明节省比例。客户原图、索引、路径、测试案例明细和数值均不公开。旧验收口径与冻结失败记录不变。

### 上一轮方向

研究方向进一步收敛为**完整工作表复现与统一执行入口**。已有业务规则和示例直接用于开发，不把代码尚未实现写成“还需用户解释流程”。先保留原值和来源，再规范化人工字段，用已有配对资料反推真实计量面和实例，再由不含人工答案的CAD输入生成带图草稿、冻结和逐行对照。开发重跑不称盲测。

取消局部试验全部闭合后才可处理整表的排程前置，但不改旧冻结、失败记录或验收门槛。失败项留在完整范围内，补查有界；按对象范围、尺寸角色、计数和错拆/错并集中改进。报价算量、制造下料与商业价格放行分开，不让无关精细加工或价格审批阻塞当前算量。当前这一轮只修订方向和记录，没有新代码、运行、工作簿或准确率。

### 上一批实际试跑

对象级方案已进入首批固定对象的实际试跑：冻结版本、输入、既有开发集合和基线，完成有界原生审图、条件算式、预测冻结及随后的人工作业表对照。没有删除失败对象、回填答案或改验收容差。部分截面与实例已形成可追溯的局部计算，仍没有新增经过完整门禁验证的报价行，不能据此宣称95%。这是已知开发数据上的Codex审图试验，不是已经发布的一键自主整包流程。

本批定位了旧视口备用包围框与当前原生矩阵不一致、相邻材料标签父对象误选、内外构造轮廓重复计件的风险。审图记录区分局部跨度、计量起止、名义截面、加工展开及未设单位的局部跨图校核。纠正只存在于私有证据和研究脚本中，尚未改写生产索引或集成自动入口。连续有界补查仍无完整行提升时转计量面范围与反证审计，不继续堆候选数量。

新增私有带图核对工作簿，保留原CAD图像字节、证据锚点和比例，区分空值与零，公式及导出缓存已检查。本机Excel只读打开、重算和图形对象核查通过；打印预览导出失败，因此尚未完成原生Office最终视觉验收。当前公开增量只有这份进度文档，没有新生产代码、安装变更、源码回归结果或正式准确率；客户CAD、清单、截图、尺寸和个案诊断不发布。

### 历史：方案制定与视频复盘

当前已重订**对象级完整审图闭环**研究方案，尚未实现新的自动流程。复用现有读取/渲染/计算能力，以房间和视向覆盖检查为入口，按物理制品完成三图对照、范围起止、尺寸角色、全部实例与算式；巡视墙面不等于按墙强制拆报价行。取消“先全项目聚类达标，再计算尺寸”的串行前置，但整包验收仍须CAD自主发现，不能永久依赖人工提供行名单。

实施顺序为小范围跨构件族闭环、固定开发集合回归、整包自主运行、版本冻结后跨项目验证。既有人工辅助样本只能作开发诊断；有历史接触的数据不能改称全新盲测。保留失败样本和完整分母，以逐项目完整行精确率及复现率验收，不用工具/截图/测试数量替代。每对象补查有界，连续无完整行提升时先做根因审计而非无限增加底层规则。当前仅发布方案摘要，没有新源码、安装、报价表或准确率。

此前一轮为工作人员流程视频复盘，**仅更新研究记录，没有新增生产能力或准确率结论**。对原片完成全帧变化扫描、逐秒时序浏览及有界连续帧检查；机器解码全片不等于逐张读完所有细节。声音研究采用音轨转写与画面对照，不是逐句直接听原声；转写中的图号和数字须交叉核查，矛盾项不自动纠正为计算参数。

研究重点进一步明确为：按观察方向、门窗柱及通道的沿墙顺序核对同一对象；把平面、立面和节点组织成同对象的视觉对照；区分前景遮挡与材料真实中断；将截面展开和沿构件延伸长度分成不同角色。工作人员的局部报价估算惯例须与CAD明示事实分别留证，不能自动升级成全项目通用补量规则。这些是后续实现方向，不是已接入自动报价的功能。

本轮未生成新报价表、未重跑完整行评测或源码回归。原固定开发集合和困难样本保持不变，开发对照与封存验证继续分开；没有通过缩小分母、修改门禁或向人工答案补数提高成绩。视频、帧图、客户名称、尺寸和详细观察仅保存在私有资料库；公开只同步本进度文档。下方代码及测试数字属于此前已完成批次。

项目仍处于研发阶段，**尚未证明纯 CAD 自动算量达到 95% 的完整行匹配率**。候选检索、图像清晰度、测试通过率和某个数值吻合，都不能替代完整报价行验收。

本轮新增**原生引线末段入框的显式审计选项**。`discover_material_leader_branches(..., allow_terminal_entry=True)` 可识别原生最后一段从框外穿过一次边界并结束在框内的连接，保存实际交点；默认 `False`，既有边框落点模式不变。不延长引线、不增大容差，不按最近标签补连。前序路径已进入同框、其他框竞争或穿越、附件/箭尖冲突与资源上限继续保留拒绝。该选项未接入公开 `run`/`resume` 或 CLI，也未借此更新本地安装版。

新增 22 项纯合成回归，覆盖坐标变换、错误入框、竞争、作用域、输入不变、开关类型及上限。公开全量 626 passed / 2 skipped，开发全量 667 passed / 3 skipped；公开 Ruff 通过。这些是工程回归，不是报价匹配率。

私有研发进一步配对开放截面的两条原生边线，并与明确引用的立面范围建立条件几何计算；没有强行闭合图形、认定未设单位为毫米或把重复视图当两件。几何中线不是已批准的制造展开，计量范围、端部处理与完整件数仍未闭合。候选表带原始 CAD 图和计算公式，正式计量字段继续为空；没有新增完整验证报价行或正式准确率。只公开通用 API、合成测试和本进度文档，不公开客户资料。

此前推进**原生轴线对位、平面实例候选定位和逐构件图纸核对表**。使用同名轴线符号的实际圆心而非文字包围框作对应，记录跨图平移及局部残差；区分嵌套的结构与装饰轮廓，不把两层构造当作两个独立制品。定位到一个候选也不等于完成全部报价实例计数。

新增私有核对工作簿，按构件保存平面、立面和节点原生渲染图，已读尺寸与未核实的计量字段分开。图像保留原始字节及纵横比，公式缓存与封装已校验；当前表格预览后端不绘制浮动图片，另查原图和图片锚点，仍未做原生 Office 最终视觉验收。发现标注显示文字与原生几何量存在冲突，保留两种事实，不静默认定其中一个正确。

此前按原生嵌套实体身份审计几何提取异常，局部失败对象的平面投影退化为点，另有近零半径圆弧；保留原始失败日志，不将局部审计推广成全项目完整性证明。该轮相关 39 项本地回归通过；仅公开脱敏进度文档，没有新增公开 API、安装变更、完整正确报价行或正式准确率。固定研发集合和旧冻结结果不改，难例保留并按有界策略转下一构件。

此前完成**节点身份、原始边线围面与跨视图尺寸链核验**。私有已有闭合面分析比较逐图层与显式跨图层模式，保留边线来源和多个标注共用一个面的情况；跨层封闭面可能混合基层、墙体和金属边界，不能直接作为制造展开或实物数量。连续尺寸链总长相同也不代表内层宽度或加工范围相同。单位未设置的节点仍保留单位未定，不通过跨图同值修改整份文件单位。该轮相关 116 项测试通过；私有闭合面分析尚未包含在当前公开源码中。

此前已发布修复**自定义材料属性标注框及镜像坐标读取**：不仅支持拆分的 ITEM/NUM，还支持值中含材料编号的可见自定义属性；要求唯一、闭合、直边矩形及实际文字框中心归属。多框、曲边、无材料编号、隐藏属性保持拒绝。镜像多段线转为世界坐标，纸空间标注边界、箭头列表、落点和嵌套引线一并投影，避免同一证据中混用坐标系。

上述读取补丁新增 23 项合成回归；当时公开全量 604 passed / 2 skipped，开发全量 645 passed / 3 skipped，本地安装路径 23 项通过。仅通用读取/坐标补丁与合成测试发布，私有案例截图和数值不公开。此改动使独立引线审计 API 可消费新边界；**不代表公开 `run`/`resume` 已完成实物自动归并，也没有新增完整验证报价行或新的端到端准确率**。旧索引不会自动修复，必须重新索引或执行有来源审计的局部刷新。

此前新增独立的 **ID-only 研发尺寸选择 API**（`cadquote.research_choices`）。它已在私有固定开发样本中执行，但没有接入公开版 `run`/`resume` 自动报价入口。研究 API 不生成商业 PASS；尚未证明端到端准确率提高。

选择器只能提交候选 ID、尺寸角色及受限运算，不能提交数字、单位、件数或审批。证据包必须由可信 CAD 适配器产生；公开 API 检查身份/作用域/来源元数据，私有试验另核对原生实体、原文件和图像哈希。API 本身不替代 CAD 读取器，也不会把任意自填 JSON 认证为真实 CAD。

未设置单位的原生数字保留为待确认，不能默认为毫米。重叠或嵌套的尺寸不能相加，不同视图里的同一原生尺寸不能重复使用。所有未选样本留在试验分母；单件矩形面积只作几何诊断，不作工程量，数量和金额始终空。人工提供的项目身份和已有截图属于开发辅助，不宣称盲测或纯 CAD 自主发现。

此前已修复**已声明的构件身份在取景链路中丢失**：构件取景框、近景记录和图像证据保留明确的 `component_id`，使尺寸板能按原构件关联测量候选；不从序号猜身份，也不把身份传递当作实物归属确认。此前编号属性语义修复继续生效。多引线和截面 API 尚未接入公开版 `run`/`resume` 自动报价，不能据本页假设下载版本具有全部私有研发能力。

研究主线转为**按构件类别完成完整算量记录**：先验证平板与直线条，再扩展门套和复合制品。每条同时约束真实对象、平面/立面/节点关系、尺寸角色、计数来源及计量规则。复用现有候选/算式/截图框架，不以工具数量作为业务进展。开发预测和商业放行分开，但不通过放宽证据门禁提高分数；人工表与其截图冲突须单列，不能向答案拟合。

## 发布与研发状态

| 层级 | 状态 | 如何理解 |
|---|---|---|
| 公开源码基线 | [ea45ea3](https://github.com/00OO666/cad-stainless-quote/commit/ea45ea3663d93a3544d01b1eef512f3d9ad64fe6) 的预索引功能，加视口修复、截面/多引线审计、原生尺寸近景 | 近景后端已接入独立命令；实物归并和自动报价仍未完成，其他研发能力须分别审查 |
| 开发工作区 | 在上述基线上继续修复读取、原生几何及关联候选 | 经过本地回归；仍需整理通用补丁、脱敏审查和公开仓库回归后发布 |
| 完整自动算量 | 未完成验收 | 跨视图实物归属、尺寸角色、数量与材料范围仍可能 REVIEW/BLOCK |
| 商业报价 | 仍需完整证据和经批准的价格源 | 未确认项不得进入确认金额 |

## 已取得的开发进展（尚未全部发布）

### 2026-09-07：所属图号、回指图号与原生上下文

- 将图框所属图号、节点序号、节点回指图号分开审查。节点内出现的立面引用不能直接当作节点所属图纸；图框属性与视口包含关系可以恢复检索身份，但目前仅为私有有界案例核查，未自动改写公开索引。
- 用原生尺寸引出点识别嵌套/重叠区间。不能为了接近已知人工数值而相加，也不把相邻构造层厚度当金属板厚或展开补量。
- 复用已有纸空间渲染能力，补齐视口边界外的材料文字和引线；保留新 CAD 证据与人工原表截图的不同来源。不是新绘制图形，不是从低分辨率截图放大造细节。
- 新开发核对表对未闭合的数量、工程量继续留空。核验公式缓存、原始 PNG 字节、嵌图比例和锚点；表格预览工具不绘制浮动图片，因此另查全部源图与工作簿封装，尚无原生 Office 最终视觉验收。
- 本案例的人工标签、数值和截图已被用于研发对照，明确不是盲测。困难案例不删除，封存数据继续不参与调试。详细资料仅保存于私有项目和 Obsidian。

### 此前已接入的构件身份保留

- `suggest_component_frames` 和 `render_component_frame_closeups` 保留输入中明确的非空字符串构件身份，覆盖取景记录及图像证据；缺失、非字符串或空身份保持为空，不用行号补造。
- 后续测量板复用已有构件与实体关联逻辑。身份只是调用方声明的候选归组，不证明材料、尺寸角色、实例数量或报价范围，结果仍为 REVIEW/MISSING。
- 6 项独立合成测试实际经过 DXF 创建、取景、渲染及测量板绑定，验证身份保留、无身份时不错误绑定、缺框时不伪造证据，以及输入与源文件不变。
- 开发案例继续把旋转轮廓、原生尺寸端点、内部分格和工程量分开核查；外包矩形面积与轮廓面积都不自动等于可报价工程量。数值接近人工结果不能弥补材料/跨图身份缺失。

### 此前已接入的数量属性语义修复

- 材料编号、图纸编号中的 `NUM/NUMBER` 不构成物理数量证据。若属性文本明确带 `QTY` 等计数标签，仍允许进入独立的显式数量文本规则。
- 结构化数量属性必须是有效正整数且没有 mm/毫米长度单位；仍只生成 REVIEW 候选，不代替跨图身份和实物数量确认。
- 24 项独立合成回归覆盖编号误用、明确计数、大小写/空格、非法计数/单位、输入不变及确认门禁。公开全量 539 passed / 2 skipped；本地安装路径新回归通过。
- 此改动消除不可靠候选，不宣称端到端准确率提高到某个数值。尚未完成按构件族的完整自动验收。

### 此前已接入的原生尺寸近景

- 新增 `cadquote.native_paper_render.render_native_paper_regions`：使用 DXF 原生模型到纸空间矩阵，叠加原布局中的 DIMENSION、文字和引线等受支持实体；保留实体句柄、图像像素、源/图像哈希、实际裁框和变换记录。
- `component-closeups` 对含原视口的候选框默认使用该后端；无视口的模型图保持模型渲染路径。原视口冻结层同时作用于模型渲染；不在未知可见性下静默选取其他视口。
- 拒绝非整周旋转、非顶视图、透视、扩展裁剪、越界和无效区域；源哈希改变与资源上限保留错误。整周角度采用度数，非零目标点由原生矩阵处理。
- 可选文字上下文点只扩大取景，不构造箭尖或物理数量；图像像素不是尺寸值来源。取景边缘仍可能裁切纸面文字，不把“成功生成图像”作为证据链齐全的判断。
- WIPEOUT 不绘制；代理、外部参照、SHX 和打印样式仍有保真限制。不是原生 CAD 应用的像素级截图，也不承诺任意图纸完全一致。
- 新增 16 项独立合成测试；只同步本轮三个渲染模块到本地已安装 Skill，其他私有流水线集成没有借此一并发布。

### 此前已发布的多引线审计 API

- 新增 `cadquote.material_branches.discover_material_leader_branches`：按同源、同图和同空间，保留材料标签边框上每条原生 LEADER 的实际连接、落点和来源。不会把一个标签复制成多个物理构件。
- 多标签竞争、原生附件冲突、矛盾落点、隐藏实体、重复标识和扫描上限均保留拒绝原因；超限结果不允许继续自动绑定。MULTILEADER 尚未支持，不能把未命中当作没有构件。
- 每个分支仍为 REVIEW；连接相符只允许继续核查几何，不证明材料归属或实物数量。数量和计量角色不自动填写。
- 新增 21 项合成分支测试，不包含客户图纸、句柄、尺寸或对照答案。

### 此前已发布的截面审计 API

- 新增 `cadquote.strip_profiles.analyze_native_strip_profile`：在显式选定的原生闭合直线 LWPOLYLINE 上配对端帽与两侧边线，计算几何中间路径；保留每段长度、来源和复核数据。
- 用中间路径的等厚缓冲重建整块截面，核对完整边界，不采用“闭合周长除二”的捷径。多解保留歧义；曲线、开放轮廓、可变线宽、嵌套变换、未知单位及扫描上限不会被当作已支持。
- 几何中线不等于板料弯曲中性层。没有批准的折弯规则时，下料展开宽、数量与计量角色继续为空，输出始终 REVIEW，不能自动进入确认报价。
- 新增 20 项独立合成测试，涵盖平移/旋转/顶点顺序、变厚与无效图形、歧义、资源上限和原生适配器无修改约束。

### 此前已发布的角度补丁

- DXF VIEWPORT 的角度字段以度为单位。整周旋转应视为未旋转；误按弧度取模会拒绝正常视图，还可能误接受小幅旋转。
- 现在按度数规范化，只接受完整周数；非零方向旋转仍安全拒绝，未借此宣称支持任意旋转视口。
- 新合成测试与原生视口逆变换交叉核对，不包含客户几何或测量值。

### 开发版其他进展

1. **图纸读取与视口可见性**

   - 修复标题、材料标注中隐藏属性及图签行政文字带来的错误位置候选。
   - 恢复原生图框和同父块图号引用，避免共享视口内不同图框互相串图。
   - 应用视口冻结层及父块可见性。
   - 保留可核实映射的纯几何视口：没有文字或语义实体，不代表原 CAD 是空图。

2. **引线、几何与跨图关系**

   - 按实际材料标注边框接触恢复原生引线落点，不只比较文字中心。
   - 多分支阶段已接入开发版 run 审计及 resume 快照；候选图、构件近景和选定证据保留每条分支，但仍只保留原材料标注身份，不直接拆件。
   - 修正候选图将“文字锚点”写成“箭头落点”的问题；两类坐标分离，无箭头时不能用文字位置绑定构件。新增纸面标注叠加与原视口可见性渲染，原生 CAD 局部另行核查。
   - 探查真实几何面片、父块及嵌套轮廓，同时保留裁剪、代理和遮挡限制。
   - 解析原生索引箭头方向，并用有来源的轴线研究跨图配准；镜像歧义不自动选边。
   - 新增“原生索引符号 → 连接线 → 闭合范围 → 原视口 → 范围内轮廓”的候选阶段。
   - 开发版已将范围阶段接入运行审计及恢复运行；几何轮廓数仍不等于实物数量。

3. **尺寸与证据**

   - 以原生端点检查连续尺寸链，保留来源、尺度、方向和重叠限制。
   - 新增原生跨视口尺寸候选：把两个尺寸引出点分别映射回各自视口，分别保存纸面换算值、文字覆盖和模型端点跨度。
   - 只有唯一视口、相同尺度、同轴端点及数值相符才标记“几何相符”；重叠、隐藏、单位不明、裁剪/旋转不支持和资源上限保留拒绝原因。几何相符仍是 REVIEW，不代表实物归属已确认。
   - 该候选阶段已接入开发版 run 审计和 resume 快照复用，不自动写入报价尺寸、数量或金额；尚未合入公开流水线。
   - 从原 CAD 矢量重新渲染可读近景，保留图像比例、源哈希、图号和实体证据。
   - 区分外层金属、内部木质构件、基层及骨架，保留相互矛盾的材料证据。

## 本轮明确排除的错误捷径

- 重复的小矩形、装饰图案或 MT 标签出现次数，不直接作为工程件数。
- 某个 MT 标签在构件附近，不代表其引线指向该构件。
- 水平投影尺寸相加，不自动等于折弯展开宽度。
- 截面闭合周长包含两侧边线，不能当作一次展开；几何中间路径也不能直接冒充制造下料宽。
- 断开视图的纸面几何长度，不直接替代明确标注的实际尺寸。
- 未识别到受支持的轮廓，不输出“实际数量为零”。
- 人工答案用于开发复核时，要与 CAD-only 预测和封存评测分开。

## 验证说明

本轮（2026-09-07）**开发工作区**全量回归 **622 项通过、3 项跳过**；公开仓库全量回归 **581 项通过、2 项跳过**；两处 Ruff 全量通过。新增研究 API 有 **36 项合成测试**，已安装 Skill 路径下同组测试通过，并核实实际导入路径。未进行端到端整行准确率评测。

本次安装版只新增研究 API 模块，没有覆盖 SKILL 操作契约和其他生产模块。公开增量仅研究 API、合成测试、本页；私有案例适配器、实体选择、图像和逐项分诊留在本地。各组测试覆盖不同版本或子集，都不是算量正确率；远端 CI 状态以实际运行记录为准。

源文件哈希、原图像素及图像哈希、候选冻结文件与输入关系已做本地核验。客户级计数、尺寸、工程量、对照表和证据资产仅留在私有项目记录中，不公开。

局部阶段在实际图纸上执行，开发核对工作簿使用新图，并区分人工索引、CAD 候选与人工对照数值，不隐藏未完成项。此前部分实验先冻结候选再读数值；本轮已看过人工数据后追查原图，不能沿用“未见答案”或盲测说法。嵌入图片的原始字节、像素、比例与 OOXML 锚点已核对。表格预览不显示浮动图像，因此另行检查全部源图；仍未进行原生 Office 最终视觉验收。这不等于任意图纸的端到端自动报价验证。

## 当前卡点与下一步

| 优先级 | 待打通环节 | 完成标准 |
|---|---|---|
| P1 | 多视口尺寸的实物归属 | 端点映射和数值核对候选已实现；继续证明它属于哪个实物、哪一条计量轴 |
| P1 | 多引线的目标层级与材料冲突 | 原生 LEADER 分支保留已实现；继续核实外饰、内层与整件关系，不能只凭箭头包含关系判材料 |
| P1 | 物理构件跨图归并和数量 | 平面、立面、节点证明同一实物；重复图形与多个实物可区分 |
| P1 | 展开规格与计量轴 | 沿实际金属截面确认折边及展开规则，避免把投影尺寸当展开 |
| P2 | 复合构件和材料冲突 | 外饰、内柜、基层和骨架的报价范围明确；冲突不被静默覆盖 |
| P2 | 自动工作簿证据接入 | 每行绑定同一构件的定位、近景、节点及计算来源，图像清晰可核验 |
| P2 | 完整行评测与泛化 | 先冻结 CAD-only 预测再评分，报告完整行指标和自动化率，不挑成功行当分母 |
| 发布门禁 | 通用补丁合入公开源码 | 删除客户依赖，补合成回归，公开仓库验证通过后再声明功能已发布 |

## 后续记录约定

每个有实质变化的阶段结束，以及切换任务或压缩上下文前：

1. 在私有项目文档和对应 Obsidian 页面记录完成项、失败假设、更正、证据位置、测试结果及下一步。
2. 同步本页的脱敏工程摘要，明确区分研发完成、公开已发布和未验证。
3. 审查准备提交的文件与 diff；不批量复制私有研究目录。
4. 推送后核对远端提交，并将提交编号回写私有接管记录。
5. 如果写入或推送失败，记为待同步，不把“本地已保存”说成“远端已更新”。

进度摘要不是源代码或客户数据备份。未发布开发代码的状态必须继续明确记录，不能只凭文档同步就宣布发布完成。
