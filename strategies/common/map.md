## 地图流程
- map.local_vote为空时先choose_map_node投票；已有票则等队友，不重复投票。
- 等待时action_type=null、plan_update=wait，reason写等待原因和释放条件（队友投票、页面变化）。
- 有分支时按路线取舍选择；无分支的唯一选项直接推进。
