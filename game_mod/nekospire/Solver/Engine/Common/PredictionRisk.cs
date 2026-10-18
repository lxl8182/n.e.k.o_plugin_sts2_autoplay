namespace CombatSolver.Engine.Common;

internal enum PredictionRiskReason
{
    MethodNotMirrored,
    MethodMirrorIncomplete,
    UnresolvedPlayerChoice,
    // 玩家选择点，但选项已枚举、搜索会按 CardChoiceSpec 分支：属"已建模的不确定"，不是覆盖缺失。
    ModeledPlayerChoice,
    CardDrawLimitExceeded,
    OrbChannelLimitExceeded,
}

internal class PredictionRisk(bool hasRisk)
{
    public static PredictionRisk None { get; } = new(false);

    public virtual bool HasRisk { get; } = hasRisk;
}
