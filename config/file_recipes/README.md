# 📚 AGY 文件处理操作手册 (File Recipes)

此目录存放用于指导 AGY 生成文件处理命令的标准作业手册（Recipes）。

## ⚙️ 菜谱是怎么被选中的

**不是**让 AGY 自己来 `ls` 这个目录。选取发生在 Python 侧：
`core/file_pipeline.py` 中的 `RECIPE_INDEX` 按「文件扩展名 + 用户措辞关键词」命中菜谱，
把命中的**正文直接内联进 Planner 的 prompt**。

这么做是为了：
- 省掉 Planner 探索目录的 2-4 轮 tool call
- 让"这次用哪本手册"变成确定事件，而不是概率事件

> ⚠️ **因此：新增一份 `.md` 菜谱后，必须同步在 `RECIPE_INDEX` 中注册它的
> `exts` 与 `keywords`，否则这份菜谱永远不会被命中。**

## 📖 现有菜谱

| 菜谱 | 覆盖 |
|---|---|
| `image_compression.md` | 图片压缩 |
| `image_stitching.md` | 多图拼接成长图 |
| `images_to_pdf.md` | 图片 → PDF |
| `pdf_compression.md` | PDF 压缩 |
| `pdf_to_images.md` | PDF 按页转图片 |
| `video_to_gif.md` | 视频 → GIF |
| `video_trim.md` | 视频剪辑与拼接 |
| `video_compression.md` | 视频压缩 |
| `media_transcribe.md` | 音视频转文字 / 字幕 |
| `document_convert.md` | md / html / epub / rst / txt 互转（pandoc） |
| `office_convert.md` | xls / doc / ppt 系 → PDF 及格式互转（soffice） |

## 📝 编写规范

固定三段：**触发条件** → **处理逻辑** → **输出规范**；能力边界或反直觉结论
另起一节用 `⚠️` 标出。

1. **触发条件**: 适用的文件类型与用户意图关键词（需与 `RECIPE_INDEX` 保持一致）
2. **处理逻辑**: 可直接替换占位符后执行的完整命令，附上选择该参数的理由
3. **输出规范**: 产物命名约定、中间文件的清理、失败时该如何如实告知用户

**只写会改变决策的信息。** 菜谱是给模型读的 prompt，不是调研报告 ——
控制在 60 行以内；实测数据只保留结论（"720p → 480p 降至 8%~21%，是最强杠杆"），
不要罗列测量过程。

### 路径写法铁律

执行环境**不会**定义 `$INPUT` / `$OUTPUT_DIR` 之类的任何变量。因此手册中的示例命令：

- 一律使用 `<输入绝对路径>` / `<输出目录>` 这类**尖括号占位符**，明示"这里需要替换"
- 严禁出现 `image1.jpg` 这种裸相对文件名——Planner 会照抄，产物就落到了错误的目录
- 严禁出现 `/tmp/palette.png` 这类**固定临时文件名**——并发任务会互相覆盖；
  中间产物一律放在输出目录下，并在最后一条命令中删除
- 例外：**不是产物、也不该被回收**的工作目录（如 `office_convert.md` 里
  soffice 的 `-env:UserInstallation`）放输出目录会被当成产物发给用户，
  应放 `/tmp` 并用 `$$` 拼进 PID 保证唯一（命令以 `shell=True` 执行，`$$` 会展开）

## ➕ 新增一份菜谱

菜谱由人写、人审、随代码提交 —— 一份菜谱决定的是此后**所有**同类任务的走法，
所以每一步都要能复核：

1. 先把命令在服务器上真跑一遍，参数结论以**实测**为准，不要照抄网上的写法
2. 按上面的规范写成 `.md`，控制在 60 行以内，只留会影响决策的信息
3. 在 `core/file_pipeline.py` 的 `RECIPE_INDEX` 中注册 `exts` 与 `keywords`；
   若引入新措辞，同步补进 `PROCESS_PHRASES`
4. 在 `tests/test_file_pipeline.py` 中补路由测试与要点断言，并**变异验证**：
   故意破坏一处，确认测试真的会失败
