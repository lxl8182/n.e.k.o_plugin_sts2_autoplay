## 事件流程
- dialogue_pending为真时先advance_event_dialogue推进对话，再选选项。
- 只剩唯一Proceed选项时可接续原选择推进，不必反复确认。
- 选择前核对choice_effects与effect_missing；缺说明时先查现场或文档，不猜数值。
