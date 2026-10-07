// Facade for the in-game catgirl danmaku overlay behind the mod HTTP API.
// POST /danmaku -> DanmakuService.PushAsync -> lazily create the overlay on the game thread and render one
// line. Created lazily so no CanvasLayer node is added if the catgirl never posts.
// Both producers funnel through PushAsync (the NEKO plugin via POST /danmaku and this mod's own
// NekoDanmakuDriver), so the runtime gate lives here rather than at either call site.
using System;
using System.Threading.Tasks;

namespace NekoComm.Game
{
    internal static class DanmakuService
    {
        private static DanmakuOverlay? _overlay;

        public static async Task<string> PushAsync(string text, string? style, string? placement, string? avatar)
        {
            if (string.IsNullOrWhiteSpace(text))
                return "empty";

            // Runtime gate for every danmaku producer. "disabled" is not an error: the HTTP caller still
            // gets ok=true, it just does not render (so a disabled overlay never shows up as a failed action).
            if (!NekoConfig.Current.danmaku_push_enabled)
                return "disabled";

            return await GameThread.InvokeAsync(() =>
            {
                try
                {
                    // An overlay that was torn down by ClearAsync leaves a freed node behind; recreate it
                    // instead of dereferencing a disposed Godot object.
                    if (_overlay == null)
                        _overlay = DanmakuOverlay.Create();
                    _overlay.Add(text, style ?? "catgirl", placement ?? "scrolling", avatar);
                    return "ok";
                }
                catch (Exception ex)
                {
                    return $"error:{ex.Message}";
                }
            });
        }

        /// <summary>Clear the screen and drop the overlay, e.g. when the user switches danmaku off.</summary>
        public static async Task<string> ClearAsync()
        {
            return await GameThread.InvokeAsync(() =>
            {
                try
                {
                    var overlay = _overlay;
                    _overlay = null;   // drop the reference FIRST: a freed node must never be reused
                    overlay?.Dispose();
                    return "ok";
                }
                catch (Exception ex)
                {
                    return $"error:{ex.Message}";
                }
            });
        }
    }

    /// <summary>Payload for POST /danmaku.</summary>
    internal sealed class DanmakuRequest
    {
        public string text { get; set; } = "";
        public string? style { get; set; }
        public string? placement { get; set; }
        public string? avatar { get; set; }
    }
}
