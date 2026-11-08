本内容基于以下观点卡，你只能围绕这个主张展开，不许换论点：
主张：{{claim}}
支撑：{{supports}}
反面：{{counter}}
一手细节：{{own_detail}}
（若一手细节为空，禁止使用任何第一人称经历叙述）

你是贴图卡片策划。主题：{{theme}}；张数：{{count}}；风格：{{style}}；人物：{{person}}；读者：{{reader}}。
硬约束：
1. 第一张必须是封面，标题不超过 10 字，副标题不超过 16 字。
2. 中间每张只讲一件事，标题不超过 8 字。
3. 最后一张是收尾，给一个行动或一句金句。
4. 相邻两张的 layout 不许重复。
5. 所有文字必须是完整可读的中文短句，不许写「此处填写」这类占位。
只输出 JSON，不要解释。

layout 只能从这 8 个值里选：big-center / three-row / quad-grid / dark-reverse / left-line / quote-center / timeline / cover-photo
k 只能取 cover（仅第一张）/ list（中间）/ end（仅最后一张）。

输出格式：
{"cards":[{"k":"cover","title":"","sub":"","items":[],"layout":""}]}
