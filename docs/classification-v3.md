# maxi dress 识别规则 v3：中心词 + 长度槽

v3 是**影子版本**（`rules/maxi_dress.v3.yaml`，`default: false`）。默认版本仍是 v1。查询时加 `?rule_version=v3` 就能和 v1 对比，事实表和小时表不用重算。

## 1. 为什么要 v3

v1/v2 只看「标题词集合里有没有 maxi 和 dress」，词序丢了，于是出现两类错误：

- **误收**：`Maxi Dress Belt`（卖的是腰带）、`Maxi Dress Sewing Pattern`（卖的是纸样）、`Maxi Dress for 18 Inch Doll`（娃娃衣），三条都同时含 maxi 和 dress，v1 都判 match。
- **漏收**：`Floor-Length ... Gown`、`Ankle-Length Wrap Dress` 这类不写 maxi 的长款，以及 `Maxikleid` 这类粘连词，v1 都判 exclude。

## 2. 两槽模型

一条标题拆成两个独立的槽：

| 槽 | 回答的问题 | 怎么取 |
|---|---|---|
| **中心词槽** | 这是不是一条连衣裙 | 截断后的前半段里，**最后一个**品类词（英文修饰语在前、中心词在后） |
| **长度槽** | 是不是长款 | 前半段里命中的 strong / weak / conflict 三档短语 |

两个槽都满足才判 match。引擎（`HeadSlotRuleSet`，在 `src/salestrend/classify/rules.py`）不认识任何具体单词，词表全部在 yaml 里。

## 3. 判定七步

| 步 | 做什么 | 卡在这一步时的 rule id |
|---|---|---|
| 1 归一化 | 转小写；去所有格（women's / womens → women）；标点转空格（保留中日文字符）；折叠空格；按配置表拆粘连词（maxidress → maxi dress，maxikleid → maxi kleid，shirtdress、slipdress 同理；sundress 不拆） | — |
| 2 截断 | 遇到 `with / for / and` 且**前面已出现过品类词**时截断，只在前半段找中心词；前面还没有品类词就跳过这个截断词继续往后找（`Mother and Daughter Maxi Dress` 不截）。截掉的尾巴另外保存 | — |
| 3 用途排除 | 尾巴里**以 for 开头的段**出现 doll / pet / dog / cat（含复数）→ exclude。不在全标题里查 doll，所以 `Doll Collar Maxi Dress`（娃娃领）不受影响 | `U1_non_wearable_use` |
| 4 中心词 | 找不到 → exclude；是其他服装 → exclude；是配件或非穿着物 → exclude；是 kaftan / abaya / 裙（品类有歧义）→ review | `H0_no_head` / `H1_head_other_garment` / `H3_head_non_wearable` / `H2_head_ambiguous` |
| 5 长度槽 | 先剔除袖长属性短语（short / long sleeve、sleeveless、长袖、短袖 等，词表在 yaml 的 `attribute_phrases`），再匹配三档长度词；判定见下表，自上而下命中即停 | `S1`–`S4`、`S0` |
| 6 类目告警 | 中心词属于 dress 簇、但原始 category 不是 dress 时告警，不改判（沿用 C1） | 告警 `C1_category_conflict` |
| 7 CJK | 中日文没有空格，按 `cjk_words` 在连续串内取最长子串；`长裙` 展开成 `长`（weak）+ `裙`（歧义品类） | — |

长度槽判定表：

| rule id | 条件 | 判定 |
|---|---|---|
| `S2_strong_with_conflict` | strong 和 conflict 同时出现 | review |
| `S1_strong` | 有 strong，没有 conflict | match |
| `S3_weak_only` | 没有 strong，有 weak（含 weak + conflict） | review |
| `S4_conflict_only` | 只有 conflict | exclude |
| `S0_no_length` | 三档都没有 | exclude |

## 4. 分档词表

连字符在第 1 步已变成空格，所以长度词按**短语**匹配（`floor length`，而不是 `floor-length`）。

| 档 | 词 |
|---|---|
| strong | maxi、floor length、ankle length、full length、maxi length、マキシ |
| weak | long、longue、largo、lang、长、ロング |
| conflict | midi、mini、knee length、tea length、short、ミディ、ミニ、短 |
| dress 簇（中心词目标） | dress(es)、gown(s)、frock(s)、sundress(es)、kleid、kleider、robe、vestido(s)、ワンピース、连衣裙 |
| 歧义品类（review） | kaftan、caftan、abaya（含复数）、裙 |
| 其他服装 | skirt、top、cardigan、coat、jacket、shirt、blouse、pants、trousers、jeans、shorts、jumpsuit、romper、sweater、hoodie、tee、cami、vest、shoes、boots、sneakers、sandals、半身裙、スカート 等 |
| 配件 / 非穿着物 | belt、hanger、pattern、doll、bag、sticker、mannequin、fabric（含复数） |
| 袖长属性短语（不进长度槽） | short / long sleeve(s / d)、sleeveless、cap sleeve(s)、长袖、短袖、无袖 |
| 截断词 | with、for、and（前面要先有品类词） |
| 用途排除词（只看 for 段） | doll、pet、dog、cat（含复数） |

## 5. 对抗集：32 条正反例

用例在 `tests/unit/test_classify_v3.py`，每条都断言了判定、中心词和 rule id。最后一列是 v1 的判定，用来对照。

| 标题 | 期望 | v3 中心词 | 卡在哪一步（rule id） | v1 |
|---|---|---|---|---|
| Maxi Denim Dress | match | dress | 5 `S1_strong` | match |
| Women's Maxi-Dress Floral | match | dress | 5 `S1_strong` | match |
| MAXI DRESS WHITE | match | dress | 5 `S1_strong` | match |
| Long Maxi Summer Dress | match | dress | 5 `S1_strong`（strong 压过 weak） | match |
| Floor-Length Chiffon Evening Gown | match | gown | 5 `S1_strong` | **exclude** ✗ |
| Boho Floral Maxi Sundress | match | sundress | 5 `S1_strong` | match |
| Ankle-Length Wrap Dress | match | dress | 5 `S1_strong` | **exclude** ✗ |
| Sommer Maxikleid Blumen | match | kleid | 1 拆粘连词 → 5 `S1_strong` | **exclude** ✗ |
| Maxi Dress with Belt | match | dress | 2 截断 → 5 `S1_strong` | match |
| Floral Pattern Maxi Dress | match | dress | 4 pattern 不是最后一个品类词 → 5 `S1_strong` | match |
| Doll Collar Maxi Dress | match | dress | 3 doll 不在 for 段 → 5 `S1_strong` | match |
| Maxi Skirt | exclude | skirt | 4 `H1_head_other_garment` | exclude |
| Long Floral Skirt | exclude | skirt | 4 `H1_head_other_garment` | exclude |
| Midi Dress Blue | exclude | dress | 5 `S4_conflict_only` | exclude |
| Summer Mini Dress | exclude | dress | 5 `S4_conflict_only` | exclude |
| Casual Dress | exclude | dress | 5 `S0_no_length` | exclude |
| T-shirt Dress（类目 shirt） | exclude + C1 告警 | dress | 5 `S0_no_length`，6 告警 | exclude |
| White Oxford Dress Shirt | exclude | shirt | 4 `H1_head_other_garment` | exclude |
| Maxi Dress Belt | exclude | belt | 4 `H3_head_non_wearable` | **match** ✗ |
| Maxi Dress Sewing Pattern | exclude | pattern | 4 `H3_head_non_wearable` | **match** ✗ |
| Maxi Dress for 18 Inch Doll | exclude | dress | 3 `U1_non_wearable_use` | **match** ✗ |
| Elegant Long Dress | review | dress | 5 `S3_weak_only` | review |
| Midi to Maxi Convertible Dress | review | dress | 5 `S2_strong_with_conflict` | **match** ✗ |
| Floral Kaftan | review | kaftan | 4 `H2_head_ambiguous` | **exclude** ✗ |
| 碎花长裙 | review | 裙 | 7 切出 长 + 裙 → 4 `H2_head_ambiguous` | **exclude** ✗ |
| Short Sleeve Maxi Dress | match | dress | 5 剔除 short sleeve → `S1_strong` | match |
| Long Sleeve Maxi Dress | match | dress | 5 剔除 long sleeve → `S1_strong` | match |
| Long Sleeve Dress | exclude | dress | 5 剔除 long sleeve 后无长度词 → `S0_no_length` | **review** ✗ |
| 长袖连衣裙 | exclude | 连衣裙 | 7 切出 长袖 + 连衣裙 → 5 剔除 长袖 → `S0_no_length` | exclude |
| Short Maxi Dress | review | dress | 5 short 修饰裙身，留在长度槽 → `S2_strong_with_conflict` | **match** ✗ |
| Mother and Daughter Maxi Dress | match | dress | 2 and 前无品类词，不截 → 5 `S1_strong` | match |
| Maxi Dress and Cardigan Set | match | dress | 2 and 前已有 dress，截断 → 5 `S1_strong` | match |

套装（`Maxi Dress and Cardigan Set`）判 match：套装里确实卖出了一条 maxi dress，按「是否售出该品类商品」的口径应计入；若业务要求套装单列，可以在 yaml 里加一个 `set` 歧义词另行处理。

**v1 在对抗集上判错 11 / 32 条**（实测，由 `test_report_v1_errors_on_adversarial_set` 打印）：漏收 3 条（Floor-Length Gown、Ankle-Length Wrap Dress、Maxikleid），误收 3 条（Maxi Dress Belt、Sewing Pattern、for 18 Inch Doll），该待审却判死 3 条（Midi to Maxi、Floral Kaftan、碎花长裙），长度判断错 2 条（Long Sleeve Dress 应 exclude 判了 review，Short Maxi Dress 应 review 判了 match）。

**样例数据回归**：在附件 30 个商品上，v3 的 match 集合与 v1 完全相同（2001–2012，共 12 个）；2017 判 review，2027 判 exclude，2018 触发 C1 告警。v3 只在样例之外的写法上与 v1 不同。

## 6. 扩词流程：离线定档，在线查表

```
离线扩词（日志 / 竞品标题挖候选词、同义词、多语种写法）
  → 人工定档（strong / weak / conflict / dress 簇 / 歧义 / 排除）
  → 改 yaml 发新规则版本（v4 …，先作为影子版本并存）
  → 重跑分类，写入成员表 product_class_member（class_slug, rule_version, product_id → 判定 + rule id）
  → 在线查询按 rule_version + product_id 精确查表
```

**为什么不在查询时扩词**（比如查询时调同义词服务或模型）：

- **可审计**：每个商品为什么算进 maxi dress，都能指到某个规则版本里的某条 rule id；查询时扩词的结果没有落地，事后说不清。
- **可缓存**：成员表只在规则发版时变，汇总表（class_daily）可以按版本预算；查询时扩词会让同一请求的结果随词表服务漂移，缓存无法命中。
- **可复现**：同一个 rule_version 今天查和下个月查结果一致；口径变化一定对应一次可见的版本号变化，方便对比和回滚。

## 7. 已知局限

- **没有词形还原**：复数靠词表手工列出（dress / dresses），`gowned`、德语变格等写法会漏。
- **CJK 只用子串词表**：不是分词器。`长裙` 统一判 review；词表里没登记的组合（如 `七分袖`）不会被识别成袖长。
- **属性短语只覆盖袖长**：`short hem`、`long back` 这类其他部位的长度修饰仍会进长度槽；`3/4 sleeve` 归一化后是 `3 4 sleeve`，不含长度词，不受影响。
- **歧义品类直接 review**：`Mini Kaftan` 这类明显短款的歧义品类也判 review，不再看长度槽。
- **中心词取最后一个品类词**：依赖英文「修饰语在前」的语序；法语、西语是后置修饰（`robe longue noire`），中心词恰好排在最后一个品类词的位置，所以能取对（这条判 weak → review），但语序更复杂的标题没有覆盖。
- **robe 有歧义**：法语里是连衣裙，英语里是浴袍；按产品侧定稿放在 dress 簇。
- **截断只看前面有没有品类词**：`Maxi Dress and Cardigan Set` 在 and 处截断，中心词取 dress、判 match；`Belt and Maxi Dress` 会在 and 处截断，中心词变成 belt、判 exclude。
