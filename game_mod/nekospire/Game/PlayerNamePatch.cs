// Co-op display-name override for nameplates.
//
// Background: the human and the catgirl run on the SAME Steam account, and in the mod's co-op flow the
// in-game "multiplayer test" lobby names every player from the game's own table — "Test Host" / "Test Client
// N", or the raw net id when unmapped. Both windows therefore show machine-ish names: you cannot tell which
// plate is the catgirl, and neither plate shows the human's real name.
//
// Every player-name lookup funnels through one method —
//   PlatformUtil.GetPlayerNameRaw(PlatformType platformType, ulong playerId)
// (playerId == the player's net id). GetPlayerName, Creature.Name (non-monster, multiplayer branch),
// NRemoteLobbyPlayer._Ready and NMultiplayerPlayerState._Ready all call it. We postfix that single choke
// point and rewrite the text of each plate:
//   * the catgirl's plate -> coop_catgirl_name (pushed by the NEKO plugin)
//   * the human's plate   -> coop_host_name, else the real Steam persona (the test lobby never asks Steam)
//
// The judgements, made per process with no cross-process messaging:
//   * who am I      -> ResolveMyNetId(): run/lobby net id of the LOCAL player.
//   * am I catgirl  -> ResolveCatgirlRole(): the net role (Client == catgirl in this setup) is the primary
//                      signal; the launcher env vars are only a fallback, because a launch path that never
//                      sets them would otherwise make BOTH instances call themselves the host.
// For a 2-player room "the other player" is unambiguously the catgirl, so the host rewrites playerId !=
// myNetId and the catgirl rewrites playerId == myNetId. The human plate is the exact mirror. Not generalized
// to 3+ players.
//
// Resolving that id must work in the LOBBY / character-select phase too — that is where the lobby nameplates
// are first built, and it is exactly where the run-level sources are still empty. Verified live: during
// CHARACTER_SELECT both LocalContext.NetId and RunManager.NetService.NetId are null, while the game's own
// /state still reports local_player_id. So the resolver mirrors GameStateService.BuildMultiplayerPayload:
// run-level sources first, then the lobby's LocalPlayer.id (multiplayer-test scene, then character-select).
//
// Registered by CombatSolverRuntime.Install()'s Harmony.PatchAll(), which scans this assembly for
// [HarmonyPatch] types. Diagnostic logging (throttled) lets the judgement be verified from godot.log.
using System;
using HarmonyLib;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext;
using MegaCrit.Sts2.Core.Platform;
using MegaCrit.Sts2.Core.Runs;

namespace NekoComm.Game
{
    [HarmonyPatch(typeof(PlatformUtil), "GetPlayerNameRaw", [typeof(PlatformType), typeof(ulong)])]
    internal static class PlayerNamePatch
    {
        private static int _diagBudget = 8;
        private static bool _personaResolved;
        private static string _personaName = "";

        public static void Postfix(ulong playerId, ref string __result)
        {
            try
            {
                var cfg = NekoConfig.Current;
                var catgirlName = (cfg.coop_catgirl_name ?? "").Trim();
                var hostName = (cfg.coop_host_name ?? "").Trim();

                ulong? myNetId = ResolveMyNetId();
                bool? amCatgirl = ResolveCatgirlRole();
                if (myNetId == null || amCatgirl == null)
                {
                    Diag($"skip(no id/role) playerId={playerId} myNetId={myNetId} catgirlProc={amCatgirl} result={__result}");
                    return;
                }

                bool isCatgirlPlate = amCatgirl.Value
                    ? playerId == myNetId.Value
                    : playerId != myNetId.Value;

                string replacement = null;
                string which;
                if (isCatgirlPlate)
                {
                    if (catgirlName.Length == 0) { Diag($"skip(no catgirl name) playerId={playerId} result={__result}"); return; }
                    replacement = catgirlName;
                    which = "catgirl";
                }
                else
                {
                    // Human/host plate. Only rewrite when a coop naming feature is in play (a catgirl name
                    // was configured) or an explicit host name was given; otherwise leave the game's own
                    // behaviour untouched in normal (non-plugin) multiplayer.
                    if (hostName.Length == 0 && catgirlName.Length == 0)
                    {
                        Diag($"skip(host untouched) playerId={playerId} result={__result}");
                        return;
                    }
                    replacement = hostName.Length > 0 ? hostName : ResolveSteamPersona();
                    if (string.IsNullOrWhiteSpace(replacement))
                    {
                        Diag($"skip(no host name) playerId={playerId} result={__result}");
                        return;
                    }
                    which = "host";
                }

                if (__result == replacement)
                    return;

                Diag($"{which} name='{replacement}' playerId={playerId} myNetId={myNetId.Value} catgirlProc={amCatgirl.Value} before='{__result}'");
                __result = replacement;
            }
            catch
            {
                // Never let a name lookup break the game UI.
            }
        }

        // Mirrors GameStateService.BuildMultiplayerPayload's ladder so we agree with /state about who the
        // local player is. Run-level sources are valid in-run; the lobby sources are valid in
        // lobby / character-select, where the run-level sources are still null.
        private static ulong? ResolveMyNetId()
        {
            try { var id = LocalContext.NetId; if (id != null && id.Value != 0) return id; } catch { }

            try
            {
                var n = RunManager.Instance?.NetService?.NetId;
                if (n != null && n.Value != 0) return n;
            }
            catch { }

            try
            {
                var scene = GameStateService.GetMultiplayerTestScene();
                if (scene != null && GameStateService.GetMultiplayerTestLobby(scene) is { } testLobby)
                    return testLobby.LocalPlayer.id;
            }
            catch { }

            try
            {
                var screen = ActiveScreenContext.Instance?.GetCurrentScreen() as NCharacterSelectScreen;
                if (screen?.Lobby != null && screen.Lobby.NetService.Type.IsMultiplayer())
                    return screen.Lobby.LocalPlayer.id;
            }
            catch { }

            return null;
        }

        // The network role is authoritative when a lobby/run exists and is multiplayer; only if that is
        // unavailable do we fall back to the launcher's env-var heuristic.
        private static bool? ResolveCatgirlRole()
        {
            var type = ResolveNetGameType();
            if (type == NetGameType.Client) return true;
            if (type == NetGameType.Host) return false;

            try { return NekoAutoplayDriver.IsCatgirlProcess(); } catch { return null; }
        }

        private static NetGameType? ResolveNetGameType()
        {
            try
            {
                var t = RunManager.Instance?.NetService?.Type;
                if (t != null && t.Value.IsMultiplayer()) return t;
            }
            catch { }

            try
            {
                var scene = GameStateService.GetMultiplayerTestScene();
                var t = scene != null ? GameStateService.GetMultiplayerTestLobby(scene)?.NetService.Type : null;
                if (t != null && t.Value.IsMultiplayer()) return t;
            }
            catch { }

            try
            {
                var screen = ActiveScreenContext.Instance?.GetCurrentScreen() as NCharacterSelectScreen;
                var t = screen?.Lobby?.NetService.Type;
                if (t != null && t.Value.IsMultiplayer()) return t;
            }
            catch { }

            return null;
        }

        // The real Steam persona. The test lobby fabricates names and never asks Steam, so we read it here.
        // Cached: the persona does not change mid-session, and this avoids a Steam call on every name lookup.
        // Empty if Steam is unavailable — callers then leave the plate as-is.
        private static string ResolveSteamPersona()
        {
            if (_personaResolved)
                return _personaName;
            _personaResolved = true;
            try
            {
                if (global::Steamworks.SteamAPI.IsSteamRunning())
                    _personaName = (global::Steamworks.SteamFriends.GetPersonaName() ?? "").Trim();
            }
            catch (Exception ex)
            {
                try { Log.Info($"[NekoComm.PlayerName] persona lookup failed: {ex.GetType().Name}: {ex.Message}"); } catch { }
                _personaName = "";
            }
            return _personaName;
        }

        private static void Diag(string message)
        {
            if (_diagBudget <= 0)
                return;
            _diagBudget -= 1;
            try { Log.Info($"[NekoComm.PlayerName] {message}"); } catch { }
        }
    }
}
