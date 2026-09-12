# HMI 常用属性记忆

常用属性优先复用，报错后精查；版本不明、不匹配或陌生复杂值也应查询。
这是生成提示，不是绕过本地 Schema 校验的授权。事件仍走专用事件工具。

实测来源：本机 ComplexLineHMI 的安装包 Description.json，2026-09-08。
适用范围：native1.12-tchmi，Framework 14.3.360，Controls 14.4.1。
框架目标和包版本是不同字段，不按包号推断 Framework。

| 控件 | 常用属性 |
|---|---|
| TcHmi.Controls.Beckhoff.TcHmiTextblock / TcHmiButton | data-tchmi-text、data-tchmi-text-font-size、data-tchmi-text-color、data-tchmi-text-font-weight |
| TcHmi.Controls.Beckhoff.TcHmiButton / TcHmiToggleSwitch | data-tchmi-state-symbol |
| TcHmi.Controls.Beckhoff.TcHmiNumericInput | data-tchmi-value、data-tchmi-min-value、data-tchmi-max-value |
| TcHmi.Controls.System.TcHmiGrid | data-tchmi-row-options、data-tchmi-column-options；值结构精查 Schema |

布局属性完整名称为 data-tchmi-left、data-tchmi-top、data-tchmi-width、data-tchmi-height。
颜色属性采用符合当前 Schema 的 JSON 字符串，例如 `{"color":"#ffffff"}`。
不要从其他 UI 框架借用 Label/StackPanel/Border 或 content/font-size/is-on。

Agent 后台提示注入相同速查内容。Schema 名称目录不得因长描述被裁剪掉；
查询失败返回候选及完整名称目录，同一无效参数不得重复试探。
当前为带版本范围的静态记忆，尚非自动学习、持久化豁免或跨版本缓存。
