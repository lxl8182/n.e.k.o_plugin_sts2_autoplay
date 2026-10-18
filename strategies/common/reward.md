## 奖励流程
- 顺序：claim_reward领取奖励 → 选牌/跳牌 → resolve_rewards离开。
- 跳牌用skip_reward_cards；总览离开用resolve_rewards且kwargs={"option_index":-1}。
- 药水栏满时先discard_potion换药，或直接resolve_rewards离开；不直接领取满栏药水。
- 奖励绑定局/楼层/页面，页面变化后按新页面重新判断。
