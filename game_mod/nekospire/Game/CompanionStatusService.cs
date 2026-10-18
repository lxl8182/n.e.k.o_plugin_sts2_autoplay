using Godot;
using MegaCrit.Sts2.Core.Nodes;

namespace NekoComm.Game;

// A single leased status, separate from scrolling commentary. All mutations run on the game thread.
internal static class CompanionStatusService
{
    private static readonly HashSet<string> RetiredSessions = new();
    private static CompanionStatusRequest? _current;
    private static DateTime _expiresAt;
    private static CompanionStatusOverlay? _overlay;

    public static Task<string> UpdateAsync(CompanionStatusRequest request) => GameThread.InvokeAsync(() =>
    {
        if (RetiredSessions.Contains(request.session_id) ||
            (_current != null && (request.revision < _current.revision ||
                (request.revision == _current.revision && request.session_id != _current.session_id))))
            return "stale";
        if (_current != null && request.session_id != _current.session_id)
            RetiredSessions.Add(_current.session_id);
        _current = request;
        _expiresAt = DateTime.UtcNow.AddSeconds(request.ttl_seconds);
        if (!NekoAutoplayDriver.IsCatgirlProcess())
        {
            _overlay ??= CompanionStatusOverlay.Create();
            _overlay.Refresh();
        }
        return NekoAutoplayDriver.IsCatgirlProcess() ? "disabled" : "ok";
    });

    public static object Snapshot()
    {
        var stale = _current != null && DateTime.UtcNow >= _expiresAt;
        return new
        {
            state = stale ? "disconnected" : _current?.state ?? "idle",
            text = stale ? "连接中断" : _current?.text ?? "",
            detail = stale ? "同伴状态已停止更新，请检查 N.E.K.O 连接" : _current?.detail ?? "",
            session_id = _current?.session_id ?? "",
            revision = _current?.revision ?? 0,
            enabled = IsVisible,
            expired = stale,
        };
    }

    internal static bool IsVisible => _current != null && _current.enabled &&
        NekoConfig.Current.companion_status_enabled && !NekoAutoplayDriver.IsCatgirlProcess() &&
        !string.IsNullOrWhiteSpace(_current.text);

    internal static (string Text, string Detail, bool Fault) Display()
    {
        var stale = DateTime.UtcNow >= _expiresAt;
        return stale
            ? ("连接中断", "同伴状态已停止更新，请检查 N.E.K.O 连接", true)
            : (_current?.text ?? "", _current?.detail ?? "", _current?.state is "error" or "disconnected");
    }
}

internal sealed class CompanionStatusRequest
{
    public string text { get; set; } = "";
    public string detail { get; set; } = "";
    public string state { get; set; } = "idle";
    public string session_id { get; set; } = "";
    public long revision { get; set; }
    public bool enabled { get; set; } = true;
    public double ttl_seconds { get; set; } = 15;
}

// Built-in Godot nodes plus ProcessFrame, as used by DanmakuOverlay (no generated C# node lifecycle).
internal sealed class CompanionStatusOverlay
{
    private readonly CanvasLayer _layer = new() { Name = "NekoCompanionStatusCanvas", Layer = 65 };
    private readonly PanelContainer _panel = new() { Name = "NekoCompanionStatus", MouseFilter = Control.MouseFilterEnum.Ignore };
    private readonly Label _title = new() { MouseFilter = Control.MouseFilterEnum.Ignore, TextOverrunBehavior = TextServer.OverrunBehavior.TrimEllipsis };
    private readonly Label _detail = new() { MouseFilter = Control.MouseFilterEnum.Ignore, TextOverrunBehavior = TextServer.OverrunBehavior.TrimEllipsis };

    public static CompanionStatusOverlay Create()
    {
        var overlay = new CompanionStatusOverlay();
        var style = new StyleBoxFlat
        {
            BgColor = new Color(0.08f, 0.045f, 0.075f, 0.78f),
            BorderColor = new Color(1f, 0.72f, 0.88f, 0.48f),
            ShadowColor = new Color(0, 0, 0, 0.35f), ShadowSize = 5,
            ContentMarginLeft = 14, ContentMarginRight = 14,
            ContentMarginTop = 8, ContentMarginBottom = 8,
        };
        style.SetCornerRadiusAll(12);
        style.SetBorderWidthAll(1);
        overlay._panel.AddThemeStyleboxOverride("panel", style);
        var rows = new VBoxContainer { MouseFilter = Control.MouseFilterEnum.Ignore };
        rows.AddThemeConstantOverride("separation", 3);
        NekoUi.ApplyFont(overlay._title, 22);
        NekoUi.ApplyFont(overlay._detail, 18);
        overlay._title.AddThemeColorOverride("font_color", new Color(0.98f, 0.9f, 0.94f));
        overlay._detail.AddThemeColorOverride("font_color", new Color(0.88f, 0.85f, 0.83f));
        rows.AddChild(overlay._title);
        rows.AddChild(overlay._detail);
        overlay._panel.AddChild(rows);
        overlay._layer.AddChild(overlay._panel);
        var tree = (NGame.Instance ?? throw new InvalidOperationException("Game is not ready for the status overlay.")).GetTree();
        tree.Root.CallDeferred(Node.MethodName.AddChild, overlay._layer);
        _ = overlay.RefreshLoopAsync(tree);
        return overlay;
    }

    public void Refresh()
    {
        _panel.Visible = CompanionStatusService.IsVisible;
        if (!_panel.Visible || !_panel.IsInsideTree())
            return;
        var (text, detail, fault) = CompanionStatusService.Display();
        _title.Text = text.Replace('\n', ' ').Replace('\r', ' ');
        _detail.Text = detail.Replace('\n', ' ').Replace('\r', ' ');
        _detail.Visible = detail.Length > 0;
        _title.AddThemeColorOverride("font_color", fault ? new Color(1f, 0.72f, 0.62f) : new Color(0.98f, 0.9f, 0.94f));
        var size = _panel.GetViewportRect().Size;
        // Leave the existing damage-statistics panel at the far right unobstructed.
        var rightInset = size.X >= 1000f ? Math.Clamp(size.X * 0.18f, 200f, 340f) : 24f;
        var width = Math.Min(Math.Clamp(size.X * 0.23f, 320f, 440f), Math.Max(160f, size.X - rightInset - 24f));
        _panel.CustomMinimumSize = new Vector2(width, 0);
        _panel.Size = new Vector2(width, 0);
        _panel.Position = new Vector2(Math.Max(12f, size.X - width - rightInset), Math.Max(84f, size.Y * 0.102f));
    }

    private async Task RefreshLoopAsync(SceneTree tree)
    {
        ulong lastRefresh = 0;
        while (GodotObject.IsInstanceValid(_layer))
        {
            await tree.ToSignal(tree, SceneTree.SignalName.ProcessFrame);
            var now = Time.GetTicksMsec();
            if (now - lastRefresh < 200)
                continue;
            lastRefresh = now;
            Refresh();
        }
    }
}
