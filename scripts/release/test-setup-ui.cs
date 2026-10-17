// Executable native Windows regressions, compiled with the shipped UI source.
public static class ToolkitUiTests {
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] static extern IntPtr SendMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    static void Require(bool ok, string message) { if (!ok) throw new InvalidOperationException(message); }
    static void Pump() { Application.DoEvents(); System.Threading.Thread.Sleep(15); Application.DoEvents(); }
    static ToolkitFonts Owner(Control view) {
        if (view is ToolkitSetupView) return ((ToolkitSetupView)view).Fonts;
        return (ToolkitFonts)typeof(ToolkitSupportView).GetField("fonts", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance).GetValue(view);
    }
    static Font[] All(ToolkitFonts f) { return new Font[] { f.Brand, f.Title, f.Body, f.BodyBold, f.Small, f.SmallBold, f.Caps, f.Button, f.Mono }; }
    static void Released(ToolkitFonts owner, string test) {
        foreach (Font f in All(owner)) {
            bool disposed = false;
            try { IntPtr h = f.ToHfont(); DeleteObject(h); } catch (ArgumentException) { disposed = true; }
            Require(disposed, test + ": owner retained an undisposed font after release");
        }
    }
    public static void Fonts(Control root, string test) {
        try { IntPtr h = root.Font.ToHfont(); DeleteObject(h); }
        catch (Exception e) { throw new InvalidOperationException(test + ": unusable " + root.GetType().Name + " font (" + root.Text + ")", e); }
        foreach (Control child in root.Controls) Fonts(child, test);
    }
    static void Test(string name, Action action, System.Collections.Generic.List<string> errors) {
        ToolkitPreview.Trace("begin test " + name);
        try { action(); ToolkitPreview.Trace("PASS " + name); }
        catch (Exception e) { errors.Add(name + ": " + e); ToolkitPreview.Trace("FAIL " + name + ": " + e.Message); }
    }
    static ToolkitSupportForm Support(bool wait) {
        var state = ToolkitPreview.SupportStates()[wait ? 0 : 3];
        return new ToolkitSupportForm(ToolkitSetupText.Failure((string)state[1], wait), ToolkitPreview.DetailsFor((string)state[1]), "", wait);
    }
    static void NativeOnLoad() {
        ToolkitTheme.ForcedDpi = 0; ToolkitTheme.ForcedWorkArea = Rectangle.Empty;
        using (var form = Support(false)) {
            IntPtr handle = form.Handle;
            int dpi = ToolkitTheme.DpiOf(form);
            Require(dpi == 96, "requires native 96 DPI, got " + dpi);
            typeof(ToolkitSupportForm).GetMethod("OnLoad", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance).Invoke(form, new object[] { EventArgs.Empty });
            Fonts(form, "Native100SupportOnLoadFontsRemainUsable");
            ToolkitPreview.Trace("native OnLoad forced=0; actual=" + dpi);
        }
    }
    static void Sequence(bool support) {
        // Same-size, same-family role alias across a real supported DPI transition:
        // a 144-DPI Button is equal to a 96-DPI Title. Equal setters must not leave
        // the old owner behind, even when another live child inherited that font.
        for (int repeat = 0; repeat < 8; repeat++) {
            using (Form form = support ? (Form)Support(true) : new ToolkitSetupForm()) {
                Control view = support ? (Control)((ToolkitSupportForm)form).View : ((ToolkitSetupForm)form).View;
                if (!support) ((ToolkitSetupForm)form).View.Show(ToolkitPreview.SetupStates()[16].Value);
                foreach (int dpi in new int[] { 96, 144, 144, 192, 96 }) {
                    ToolkitFonts old = Owner(view);
                    if (support) ((ToolkitSupportView)view).ApplyDpi(dpi); else ((ToolkitSetupView)view).ApplyDpi(dpi);
                    Fonts(form, "DpiSequence");
                    if (!Object.ReferenceEquals(old, Owner(view))) Released(old, "DpiSequence retired owner");
                }
                if (support) ((ToolkitSupportView)view).ApplyDpi(144); else ((ToolkitSetupView)view).ApplyDpi(144);
                ToolkitFonts aliased = Owner(view);
                if (support) ((ToolkitSupportView)view).Headline.Font = aliased.Button; else ((ToolkitSetupView)view).Title.Font = aliased.Button;
                var inherited = new Control(); view.Controls.Add(inherited); inherited.Font = aliased.Button;
                if (support) ((ToolkitSupportView)view).ApplyDpi(96); else ((ToolkitSetupView)view).ApplyDpi(96);
                Fonts(form, "EqualFontRoleAcrossDpi");
                Released(aliased, "EqualFontRoleAcrossDpi retired owner");
                view.Controls.Remove(inherited); inherited.Dispose();
                form.Show(); Pump(); Fonts(form, "RepeatedShowClose");
                using (var bitmap = new Bitmap(view.Width, view.Height)) view.DrawToBitmap(bitmap, new Rectangle(Point.Empty, view.Size));
                ToolkitFonts last = Owner(view);
                form.Close(); form.Dispose(); Released(last, "RepeatedShowClose final owner");
            }
        }
    }
    public static string FontLifetime() {
        var errors = new System.Collections.Generic.List<string>();
        Test("Native100SupportOnLoadFontsRemainUsable", NativeOnLoad, errors);
        Test("FinalFontOwnersDisposed", delegate {
            var support = Support(false); ToolkitFonts first = Owner(support.View); support.Dispose(); Released(first, "support");
            var setup = new ToolkitSetupForm(); ToolkitFonts second = Owner(setup.View); setup.Dispose(); Released(second, "setup");
        }, errors);
        Test("SupportRepeatedDpiAndFinalOwnerRelease", delegate { Sequence(true); }, errors);
        Test("SetupRepeatedDpiAndFinalOwnerRelease", delegate { Sequence(false); }, errors);
        if (errors.Count > 0) throw new InvalidOperationException(String.Join("\r\n", errors.ToArray()));
        return "ok";
    }
    public static void ClickReachable(Form form, Control button, string test) {
        form.ScrollControlIntoView(button); button.Focus(); Pump();
        var screen = button.RectangleToScreen(button.ClientRectangle);
        var client = form.RectangleToScreen(form.ClientRectangle);
        Require(button.Visible && button.Enabled && client.Contains(screen) && ToolkitTheme.WorkArea(form).Contains(screen), test + ": action not visible " + button.Text + " " + screen + "; client=" + client);
        var point = new Point(screen.Left + screen.Width / 2, screen.Top + screen.Height / 2);
        Require(WindowFromPoint(point) == button.Handle, test + ": action occluded " + button.Text);
        int clicks = 0; EventHandler clicked = delegate { clicks++; }; button.Click += clicked;
        IntPtr position = new IntPtr((button.Height / 2 << 16) | (button.Width / 2));
        SendMessageW(button.Handle, 0x0201, new IntPtr(1), position);
        SendMessageW(button.Handle, 0x0202, IntPtr.Zero, position);
        button.Click -= clicked;
        Require(clicks == 1, test + ": native mouse click not delivered " + button.Text);
        ToolkitPreview.Trace(test + ": clicked " + button.Text + "; rect=" + screen);
    }
    public static string ScaledForms(string folder) {
        var errors = new System.Collections.Generic.List<string>();
        foreach (int dpi in new int[] { 96, 144, 192 }) {
            foreach (bool constrained in new bool[] { false, true }) {
                int scale = dpi * 100 / 96;
                string name = "WorkingAreaActions@" + scale + (constrained ? "-constrained" : "-desktop");
                Test(name, delegate {
                    if (dpi == 96) NativeOnLoad(); // Fail before entering the historical invalid-font paint loop.
                    ToolkitTheme.ForcedDpi = dpi;
                    var desktop = Screen.PrimaryScreen.WorkingArea;
                    ToolkitTheme.ForcedWorkArea = constrained ? new Rectangle(desktop.Left + 20, desktop.Top + 20, Math.Min(760, desktop.Width - 40), Math.Min(600, desktop.Height - 40)) : Rectangle.Empty;
                    using (var form = Support(true)) {
                        form.Show(); Pump(); Fonts(form, name);
                        var area = ToolkitTheme.WorkArea(form);
                        Require(area.Contains(form.Bounds), name + ": support outside working area; form=" + form.Bounds + "; area=" + area);
                        ClickReachable(form, form.View.Copy, name);
                        Require(Clipboard.GetText() == form.View.Details.Text && form.View.CopyState == "copied", name + ": copy mismatch");
                        ClickReachable(form, form.KeepWaiting, name);
                        ClickReachable(form, form.CloseButton, name);
                        string result = ToolkitPreviewWindows.Capture(form.Handle, System.IO.Path.Combine(folder, "scaled-support@" + scale + (constrained ? "-constrained" : "-desktop") + ".png"));
                        Require(result == "ok", name + ": capture " + result);
                        ToolkitPreview.Trace(name + ": support contained; bounds=" + form.Bounds + "; area=" + area + "; view=" + form.View.Size + "; client=" + form.ClientSize);
                        form.Close();
                    }
                    using (var form = new ToolkitSetupForm()) {
                        form.View.Show(ToolkitPreview.SetupStates()[16].Value);
                        form.Show(); form.FitView(); Pump(); Fonts(form, name);
                        Require(ToolkitTheme.WorkArea(form).Contains(form.Bounds), name + ": setup outside working area " + form.Bounds);
                        ClickReachable(form, form.View.No, name); ClickReachable(form, form.View.Yes, name);
                        form.Close();
                    }
                }, errors);
            }
        }
        ToolkitTheme.ForcedDpi = 0; ToolkitTheme.ForcedWorkArea = Rectangle.Empty;
        if (errors.Count > 0) throw new InvalidOperationException(String.Join("\r\n", errors.ToArray()));
        return "ok";
    }
}
