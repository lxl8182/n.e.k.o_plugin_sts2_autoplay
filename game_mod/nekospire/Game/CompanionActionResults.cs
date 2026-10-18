using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Combat.History.Entries;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Runs;

namespace NekoComm.Game;

// Read actual finished card plays, after the executor and its follow-up actions have settled.
// This is a bounded cooperation fact window, not a combat replay.
internal static class CompanionActionResults
{
    public static string Epoch { get; } = Guid.NewGuid().ToString("N");
    private static object? _run;
    private static readonly HashSet<CardPlayFinishedEntry> Seen = new(ReferenceEqualityComparer.Instance);
    private static readonly Queue<CompanionActionResult> Recent = new();
    private static long _sequence;

    public static IReadOnlyList<CompanionActionResult> Observe()
    {
        var run = RunManager.Instance.DebugOnlyGetState();
        if (!ReferenceEquals(run, _run))
        {
            _run = run;
            Seen.Clear();
            Recent.Clear();
        }
        if (run == null)
            return [];
        if (RunManager.Instance.ActionExecutor.CurrentlyRunningAction != null ||
            RunManager.Instance.ActionQueueSet.GetReadyAction() != null)
            return Recent.ToArray();

        foreach (var entry in CombatManager.Instance.History.Entries.OfType<CardPlayFinishedEntry>())
        {
            if (!Seen.Add(entry))
                continue;
            var play = entry.CardPlay;
            Recent.Enqueue(new CompanionActionResult
            {
                sequence = ++_sequence,
                actor = play.Player.NetId == LocalContext.NetId ? "self" : "master",
                player_id = play.Player.NetId.ToString(),
                action_type = "play_card",
                card_entity_id = GameStateService.GetCardEntityId(play.Card),
                card_id = play.Card.Id.Entry,
                target_entity_id = play.Target == null ? "" : GameStateService.GetEnemyEntityId(play.Target),
            });
            while (Recent.Count > 32)
                Recent.Dequeue();
        }
        return Recent.ToArray();
    }
}

internal sealed class CompanionActionResult
{
    public long sequence { get; init; }
    public string actor { get; init; } = "";
    public string player_id { get; init; } = "";
    public string action_type { get; init; } = "";
    public string card_entity_id { get; init; } = "";
    public string card_id { get; init; } = "";
    public string target_entity_id { get; init; } = "";
    public string status { get; init; } = "completed";
    public bool executed { get; init; } = true;
}
