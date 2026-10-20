// t41 — Batch044: on-play transcription batch 44 (one card).
//
// LEGION_OF_BONE (Legion of Bone, necrobinder Skill, cost 2) is the ONLY remaining behaviour card with
// no transcription at all (game_mod/nekospire/Game/Sim/SimOnPlay.g.cs:109 →
// ["LEGION_OF_BONE"] = new[] { "summon", "summon" }). The game-side card text (read from
// /data/cards → LEGION_OF_BONE.description = "所有玩家召唤6。", vars = [("Summon", 6)]) is a plain
// summon of DynamicVars.Summon for every player; in single-player that is one summon of 6.
//
// Which path does "summon" take in this mod? NOT a new one — the established OnPlay path for every
// other summon card (Batch043: Afterlife / Bodyguard / Reanimate / Cleanse / NecroMastery / Spur /
// Dirge) is `combat.SummonOsty(simulator, card.Owner, card.DynamicVars.Summon.IntValue)`. Two other
// "summon" spellings exist in the codebase and are NOT what we want here:
//   · SimIntents.cs:116-117 / SimResolver.cs:101-102 — the standalone Sim DSL ("summon" = put a
//     placeholder card into hand). That is the self-built SimSolverPlan path, not the CombatSolver brain.
//   · CardPowerOnPlaySupport.cs:112 — SummonNextTurnPower, a deferred summon for a specific power card.
// This batch therefore mirrors Batch043's helper exactly, so the effect is modelled the same way as
// its sibling necrobinder cards.
//
// Note this file lives in Engine/InCombat/Mirrors/Cards/OnPlay/ (the batch is a part of the
// CombatSolver.Engine mirror surface) but the `CardOnPlaySupport` partial itself is declared under
// Solver/Prediction/. The folder is irrelevant to C# (a single namespace + partial class), and keeping
// the new batch here matches the t41 contract's path.
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;
using CombatSolver.Engine.Common;
using CombatSolver.Engine.InCombat.Simulation;

namespace CombatSolver;

internal static partial class CardOnPlaySupport
{
    /// <summary>Batch 44 — cards whose whole on-play behaviour is a plain summon.</summary>
    private static void ApplyBatch044(
        CombatPredictionSimulator simulator,
        SimulatedCombatState combat,
        PredictedCard playedCard,
        Creature? target,
        ISet<uint> processedEnemyDeaths)
    {
        CardModel card = playedCard.Preview;
        switch (card)
        {
            // "所有玩家召唤6。" — one Osty summon of DynamicVars.Summon (6) per player; the solver is a
            // single-player simulation, so the "all players" loop degenerates to the owner.
            case LegionOfBone:
                combat.SummonOsty(simulator, card.Owner, card.DynamicVars.Summon.IntValue);
                break;
        }
    }
}
