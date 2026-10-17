# Render the Windows setup window and support dialog to PNG with the real Windows
# WinForms runtime (Windows PowerShell 5.1, .NET Framework). Preview only: it never
# runs setup, WSL, installers or USB, and it uses synthetic sample data.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release/render-setup-previews.ps1 -OutDir setup-previews
#
# Every state is rendered at 100%, 150% and 200% through the window's own DPI layout
# (fonts and sizes in device pixels, as on a per-monitor-DPI screen). "live-*" files are
# real windows driven through ToolkitSetupStatus and captured with their Windows frame.
# Setup captures use the machine scale; the live support capture uses forced 125%
# (see the native-100% product font-lifetime observation in the receipt).
# -HighContrast switches Windows high contrast on for each isolated phase.
# The Mono previews on Linux compile the same $script:ToolkitPreviewSource.
[CmdletBinding()]
param(
    [string]$OutDir = 'setup-previews',
    [switch]$HighContrast,
    [string]$LauncherTextFile = '',
    [string]$Filter = '',
    [switch]$SkipLive,
    [string]$Phase = '',
    [switch]$TimeoutProbe,
    [ValidateRange(1, 120)][int]$PhaseTimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'

# Shared state catalogue and renderer (C# 5; no using directives because it is
# appended to the setup UI source, whose directives cover it).
$script:ToolkitPreviewSource = @'
public static class ToolkitPreview {
    public static string ProgressPath, Phase;
    public static void Trace(string message) {
        string line = DateTime.UtcNow.ToString("o") + " " + Phase + " " + message;
        Console.WriteLine(line); Console.Out.Flush();
        if (!String.IsNullOrEmpty(ProgressPath)) System.IO.File.AppendAllText(ProgressPath, line + Environment.NewLine);
    }
    public delegate void Saver(Control control, string name);
    public static readonly System.Collections.Generic.List<string> Written = new System.Collections.Generic.List<string>();
    public static readonly System.Collections.Generic.List<string> Problems = new System.Collections.Generic.List<string>();
    public const string LogFolder = "C:\\Users\\Owner\\AppData\\Local\\Jibo-DFU-Toolkit\\logs";
    // Same shape as Get-ToolkitSupportDetails, with synthetic values and no user data.
    public const string Sample = "Jibo DFU Toolkit setup support\r\nBuild: 0123456789abcdef0123456789abcdef01234567\r\nWindows: Windows 11 Pro 24H2 (build 26100.2894)\r\nWSL: WSL version: 2.4.13.0; WSLg version: 1.0.65\r\nStep: STEP\r\nError: ERROR\r\nSetup questions and answers:\r\n{\"utc\":\"2026-10-16T19:42:18.8214716Z\",\"event\":\"question-start\",\"question\":\"Set up a private Linux for the toolkit? One-time download of about 345 MB. No Linux account or password needed.\",\"step\":\"Finding Linux\"}\r\n{\"utc\":\"2026-10-16T19:42:25.1002113Z\",\"event\":\"question-end\",\"answer\":\"yes\",\"duration_seconds\":6.279,\"approved\":true,\"step\":\"Finding Linux\"}\r\nLast log lines:\r\n{\"utc\":\"2026-10-16T19:44:02.0001Z\",\"event\":\"download-progress\",\"step\":\"Downloading Ubuntu\",\"bytes\":122683392,\"expected_bytes\":360684292}";
    public static readonly string[] Prompts = {
        "Install/update WSL 2 and WSLg? Windows will ask for administrator approval.",
        "Set up a private Linux for the toolkit? One-time download of about 345 MB. No Linux account or password needed.",
        "Install usbipd-win using the interactive Windows installer?" };
    public static readonly string[] Waits = {
        "Waiting for you: approve the Windows administrator prompt. If you do not see it, look for a flashing shield icon on the taskbar.",
        "Waiting for you: finish the usbipd-win installer. Look for its installer window or a flashing shield icon on the taskbar. Restart Windows if the installer asks.",
        "Waiting for you: restart Windows if the WSL installer requested it, then reopen Jibo-DFU-Windows-x64.exe. Check the installer window or flashing shield icon on the taskbar.",
        "Waiting for you: finish any usbipd-win installer window, restart Windows if requested, then reopen the EXE. Look for its installer window or flashing shield icon on the taskbar.",
        "Waiting for you: approve USB sharing at the Windows administrator prompt. If it is hidden, look for a flashing shield icon on the taskbar." };
    static ToolkitViewState Working(string step, int percent, double seconds, string questionId) {
        var s = new ToolkitViewState(); s.Mode = "working"; s.Step = step; s.Percent = percent; s.Seconds = seconds;
        s.Hint = "Waiting for this step to finish."; s.QuestionId = questionId; return s;
    }
    public static System.Collections.Generic.List<System.Collections.Generic.KeyValuePair<string, ToolkitViewState>> SetupStates() {
        var list = new System.Collections.Generic.List<System.Collections.Generic.KeyValuePair<string, ToolkitViewState>>();
        System.Action<string, ToolkitViewState> add = delegate(string n, ToolkitViewState s) { list.Add(new System.Collections.Generic.KeyValuePair<string, ToolkitViewState>(n, s)); };
        add("working-checking-windows", Working("Checking Windows and WSL", -1, 4, ""));
        add("working-installing-wsl", Working("Installing Windows components", -1, 12, "wsl"));
        add("working-finding-linux", Working("Finding Linux", -1, 2, ""));
        add("working-download-start", Working("Downloading Ubuntu (0.0 of 344.0 MB)", 0, 1, "linux"));
        add("working-download-34", Working("Downloading Ubuntu (117.0 of 344.0 MB)", 34, 95, "linux"));
        add("working-download-complete", Working("Downloading Ubuntu (complete)", 100, 241, "linux"));
        add("working-checking-image", Working("Checking the Ubuntu image", -1, 6, "linux"));
        add("working-installing-linux", Working("Installing the toolkit's Linux", -1, 75, "linux"));
        add("working-preparing-linux", Working("Preparing Linux", -1, 31, "linux"));
        add("working-starting-linux", Working("Starting Linux", -1, 8, "linux"));
        add("working-installing-usb", Working("Installing Windows components", -1, 12, "usb"));
        add("working-unpacking", Working("Unpacking the toolkit", -1, 44, ""));
        add("working-checking-toolkit", Working("Checking the toolkit", -1, 9, ""));
        add("working-connecting-usb", Working("Connecting USB", -1, 3, ""));
        add("working-opening", Working("Opening the toolkit", -1, 14, ""));
        string[] ids = { "wsl", "linux", "usb" };
        for (int i = 0; i < Prompts.Length; i++) {
            var q = new ToolkitViewState(); q.Mode = "question"; q.Prompt = Prompts[i]; q.KeysEnabled = true; q.Step = "Finding Linux";
            add("question-" + ids[i], q);
        }
        var guard = new ToolkitViewState(); guard.Mode = "question"; guard.Prompt = Prompts[1]; guard.KeysEnabled = false; add("question-linux-key-guard", guard);
        var remind = new ToolkitViewState(); remind.Mode = "question"; remind.Prompt = Prompts[1]; remind.KeysEnabled = true; remind.Reminder = true; remind.Seconds = 64; add("question-linux-reminder", remind);
        var compact = remind.Copy(); compact.Reminder = false; compact.Seconds = 0; add("question-linux-compact", compact);
        foreach (string answer in new string[] { "closed", "no", "yes" }) {
            var a = new ToolkitViewState(); a.Mode = "answered"; a.Prompt = Prompts[1]; a.Answer = answer; a.Step = "Finding Linux";
            add("question-linux-" + (answer == "no" ? "not-now" : answer), a);
        }
        string[] waitNames = { "waiting-admin-prompt", "waiting-installer", "waiting-reboot-wsl", "waiting-reboot-usb", "waiting-usb-sharing" };
        string[] waitSteps = { "Installing Windows components", "Installing Windows components", "Installing Windows components", "Installing Windows components", "Connecting USB" };
        string[] waitIds = { "wsl", "usb", "wsl", "usb", "" };
        for (int i = 0; i < Waits.Length; i++) {
            var w = new ToolkitViewState(); w.Mode = "waiting"; w.Wait = Waits[i]; w.Step = waitSteps[i]; w.Seconds = 37; w.QuestionId = waitIds[i];
            add(waitNames[i], w);
        }
        return list;
    }
    // name, exact message given to the support dialog, Keep waiting offered, copy state, question id
    public static System.Collections.Generic.List<object[]> SupportStates() {
        const string shutdown = "Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.";
        const string installer = "Check for an installer or Windows administrator approval prompt. Restart Windows if requested.";
        var list = new System.Collections.Generic.List<object[]>();
        list.Add(new object[] { "stall-download", "Downloading Ubuntu stalled: no bytes arrived for 120 seconds. Check your internet connection, then reopen the toolkit.", true, "", "linux" });
        list.Add(new object[] { "stall-unpacking", "Unpacking the toolkit stalled: no completion after 300 seconds. " + shutdown, true, "", "" });
        list.Add(new object[] { "stall-opening", "Opening the toolkit stalled: its window did not appear within 120 seconds. Close other WSL work, run wsl --shutdown, then reopen.", true, "", "" });
        string generic = "Step: Checking the toolkit\r\nWSL failed:  Error: payload self-check exited with status 7\r\nTry: " + shutdown;
        list.Add(new object[] { "failure-generic", generic, false, "", "" });
        list.Add(new object[] { "failure-generic-copied", generic, false, "copied", "" });
        list.Add(new object[] { "failure-copy-failed", generic, false, "failed", "" });
        list.Add(new object[] { "failure-stalled-stopped", "Step: Unpacking the toolkit\r\nUnpacking the toolkit stalled after 300 seconds. " + shutdown + "\r\nTry: " + shutdown, false, "", "" });
        list.Add(new object[] { "failure-checksum", "Step: Checking the Ubuntu image\r\nUbuntu image checksum mismatch. Nothing was registered.\r\nTry: " + shutdown, false, "", "linux" });
        list.Add(new object[] { "failure-usb-handoff", "Step: Connecting USB\r\nUSB handoff failed or administrator approval was declined. No GUI was started. \r\nTry: Check for a Windows administrator approval prompt. Reconnect the USB cable and reopen the toolkit.", false, "", "" });
        list.Add(new object[] { "stopped-question-closed", "You closed the question. This counts as No; no approval was given.\r\nStep: Finding Linux\r\nDistribution setup declined. No download or import was attempted.\r\nTry: " + shutdown, false, "", "linux" });
        list.Add(new object[] { "stopped-not-now", "Step: Finding Linux\r\nDistribution setup declined. No download or import was attempted.\r\nTry: " + shutdown, false, "", "linux" });
        list.Add(new object[] { "stopped-restart-needed", "Step: Installing Windows components\r\nWSL setup requested. Reboot if Windows requests it, then run this download again.\r\nTry: " + installer, false, "", "wsl" });
        list.Add(new object[] { "stopped-reopen-usb", "Step: Installing Windows components\r\nusbipd-win setup finished. Reopen this app so Windows refreshes PATH.\r\nTry: " + installer, false, "", "usb" });
        return list;
    }
    public static string DetailsFor(string message) {
        var step = System.Text.RegularExpressions.Regex.Match(message, "Step: ([^\r\n]+)");
        var stall = System.Text.RegularExpressions.Regex.Match(message, "^(.+?) stalled");
        string name = step.Success ? step.Groups[1].Value : stall.Success ? stall.Groups[1].Value : "Downloading Ubuntu";
        return Sample.Replace("Step: STEP", "Step: " + name).Replace("Error: ERROR", "Error: " + message);
    }
    static bool Wanted(string name, string filter) { return filter == null || filter.Length == 0 || name.Contains(filter); }
    // The screen each forced DPI stands for (1920x1080 at that scale, minus the taskbar).
    static void UseScreen(int dpi) {
        ToolkitTheme.ForcedDpi = dpi;
        ToolkitTheme.ForcedWorkArea = new Rectangle(0, 0, ToolkitTheme.S(1920, dpi), ToolkitTheme.S(1032, dpi));
    }
    static Form Host() {
        var form = new Form();
        form.FormBorderStyle = FormBorderStyle.None; form.StartPosition = FormStartPosition.Manual; form.Location = new Point(0, 0);
        form.ShowInTaskbar = false; form.AutoScaleMode = AutoScaleMode.None;
        return form;
    }
    static void Pump() { for (int i = 0; i < 3; i++) { Application.DoEvents(); System.Threading.Thread.Sleep(15); } }
    public static void RenderSetup(int[] dpis, string filter, string prefix, Saver save) {
        foreach (int dpi in dpis) {
            foreach (var pair in SetupStates()) {
                string name = prefix + pair.Key + "@" + (dpi * 100 / 96);
                if (!Wanted(name, filter)) continue;
                Trace("begin render " + name);
                UseScreen(dpi);
                using (var form = Host()) {
                    var view = new ToolkitSetupView();
                    view.ApplyDpi(dpi);
                    view.Progress.Frame = 0.45; view.Chip.Frame = 0.2;
                    view.Compact = pair.Key.EndsWith("-compact");
                    form.Controls.Add(view);
                    view.Show(pair.Value);
                    form.ClientSize = view.Size;
                    Trace("begin Show " + name);
                    form.Show(); Pump();
                    Trace("end Show; begin CheckBounds " + name);
                    CheckBounds(view, name);
                    Trace("end CheckBounds; begin save " + name);
                    save(view, name);
                    Trace("end save; begin Close " + name);
                    form.Close();
                    Trace("end render " + name);
                }
            }
        }
        ToolkitTheme.ForcedDpi = 0; ToolkitTheme.ForcedWorkArea = Rectangle.Empty;
    }
    public static void RenderSupport(int[] dpis, string filter, string prefix, Saver save) {
        foreach (int dpi in dpis) {
            foreach (var d in SupportStates()) {
                string key = (string)d[0], message = (string)d[1];
                bool allowWait = (bool)d[2];
                var text = ToolkitSetupText.Failure(message, allowWait);
                string name = prefix + "dialog-" + key + "@" + (dpi * 100 / 96);
                UseScreen(dpi);
                if (Wanted(name, filter)) {
                    Trace("begin support render " + name);
                    using (var form = Host()) {
                        var view = new ToolkitSupportView(text, DetailsFor(message), LogFolder, allowWait);
                        form.Controls.Add(view);
                        // The constructor already lays out at 96. Reapplying identical
                        // fonts hits a product font-lifetime bug (reported separately).
                        if (dpi != 96) view.ApplyDpi(dpi);
                        view.CopyState = (string)d[3]; view.Relayout();
                        form.ClientSize = view.Size;
                        Trace("begin Show " + name);
                        form.Show(); Pump();
                        Trace("end Show; begin CheckBounds " + name);
                        CheckBounds(view, name);
                        Trace("end CheckBounds; begin save " + name);
                        save(view, name);
                        form.Close();
                    }
                }
                // The setup window behind the dialog: the controller's running step (already
                // the cleanup step after a USB failure) and the last question asked.
                string behind = prefix + "behind-" + key + "@" + (dpi * 100 / 96);
                if ((dpi == 96 || key == "stall-download") && !key.Contains("cop") && Wanted(behind, filter)) {
                    var s = new ToolkitViewState(); s.Mode = "support"; s.Support = text; s.QuestionId = (string)d[4];
                    s.Step = key == "failure-usb-handoff" ? "Cleaning up USB support" : (text.StepKey ?? "Downloading Ubuntu");
                    using (var form = Host()) {
                        var view = new ToolkitSetupView(); view.ApplyDpi(dpi); form.Controls.Add(view); view.Show(s);
                        form.ClientSize = view.Size; form.Show(); Pump();
                        CheckBounds(view, behind);
                        save(view, behind);
                        form.Close();
                    }
                }
            }
        }
        ToolkitTheme.ForcedDpi = 0; ToolkitTheme.ForcedWorkArea = Rectangle.Empty;
    }
    // Windows' "Night sky" colors stand in for high contrast when it is not switched on.
    public static void RenderSimulatedHighContrast(string filter, Saver save) {
        ToolkitTheme.ForceHighContrast = true;
        try {
            string[] keep = { "working-download-34@100", "question-linux@100", "waiting-admin-prompt@100", "dialog-stall-download@100", "dialog-failure-generic-copied@100" };
            Saver only = delegate(Control c, string n) { if (System.Array.IndexOf(keep, n.Substring(3)) >= 0) save(c, n); };
            RenderSetup(new int[] { 96 }, filter, "hc-", only);
            RenderSupport(new int[] { 96 }, filter, "hc-", only);
        } finally { ToolkitTheme.ForceHighContrast = false; }
    }
    // Text must fit the control it is painted in, and every control must sit inside its
    // parent: catches clipping with this machine's real font metrics.
    public static void CheckBounds(Control root, string name) {
        foreach (Control child in root.Controls) {
            if (!child.Visible) continue;
            var area = root.ClientRectangle;
            if (child.Left < area.Left || child.Top < area.Top || child.Right > area.Right || child.Bottom > area.Bottom)
                Problems.Add(name + ": " + Describe(child) + " " + child.Bounds + " is outside its parent " + area);
            var text = child as ToolkitText;
            if (text != null && text.Text.Length > 0) {
                if ((text.Flags & TextFormatFlags.SingleLine) != 0) {
                    int need = ToolkitTheme.TextWidth(text.Text, text.Font);
                    if (need > text.Width) Problems.Add(name + ": " + Describe(child) + " needs " + need + " px, has " + text.Width);
                } else {
                    int need = ToolkitTheme.TextHeight(text.Text, text.Font, text.Width);
                    if (need > text.Height) Problems.Add(name + ": " + Describe(child) + " needs " + need + " px tall, has " + text.Height);
                }
            }
            var button = child as ToolkitButton;
            if (button != null && ToolkitTheme.TextWidth(button.Text, button.Font) + 8 > button.Width)
                Problems.Add(name + ": button \"" + button.Text + "\" text is wider than the button");
            CheckBounds(child, name);
        }
    }
    static string Describe(Control c) { return c.GetType().Name + (c.Text.Length > 0 ? " \"" + (c.Text.Length > 40 ? c.Text.Substring(0, 40) + "..." : c.Text) + "\"" : ""); }
}
'@

# Windows-only helpers: real window captures, the real fallback MessageBox, the
# clipboard, and switching Windows high contrast on for one run.
$script:ToolkitPreviewWindowsSource = @'
public static class ToolkitPreviewWindows {
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct HIGHCONTRAST { public int cbSize; public int dwFlags; [MarshalAs(UnmanagedType.LPWStr)] public string lpszDefaultScheme; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int MessageBoxW(IntPtr owner, string text, string title, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowW(string cls, string title);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool SystemParametersInfoW(uint action, uint param, ref HIGHCONTRAST value, uint flags);
    public static bool HighContrast(bool on) {
        var hc = new HIGHCONTRAST(); hc.cbSize = Marshal.SizeOf(typeof(HIGHCONTRAST));
        hc.dwFlags = on ? 1 : 0; hc.lpszDefaultScheme = on ? "High Contrast Black" : null; // HCF_HIGHCONTRASTON
        return SystemParametersInfoW(0x0043, (uint)hc.cbSize, ref hc, 0x03); // SPI_SETHIGHCONTRAST, update + broadcast
    }
    // PrintWindow with PW_RENDERFULLCONTENT includes the DWM-drawn frame. "blank" = one color only.
    public static string Capture(IntPtr hwnd, string path) {
        RECT r;
        if (hwnd == IntPtr.Zero || !GetWindowRect(hwnd, out r) || r.Right <= r.Left) return "no window";
        using (var bmp = new Bitmap(r.Right - r.Left, r.Bottom - r.Top)) {
            bool ok;
            using (var g = Graphics.FromImage(bmp)) { IntPtr hdc = g.GetHdc(); ok = PrintWindow(hwnd, hdc, 2); g.ReleaseHdc(hdc); }
            bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
            if (!ok) return "PrintWindow failed";
            Color first = bmp.GetPixel(0, 0);
            for (int y = 0; y < bmp.Height; y += 7) for (int x = 0; x < bmp.Width; x += 7) if (bmp.GetPixel(x, y) != first) return "ok";
            return "blank";
        }
    }
    public static void Close(IntPtr hwnd) { if (hwnd != IntPtr.Zero) PostMessageW(hwnd, 0x0010, IntPtr.Zero, IntPtr.Zero); }
    // The same box the fallbacks show (flags 0x00050213), captured, then cancelled.
    public static string CaptureMessageBox(string text, string title, string path) {
        var thread = new System.Threading.Thread(delegate() { MessageBoxW(IntPtr.Zero, text, title, 0x00050213); });
        thread.IsBackground = true; thread.Start();
        IntPtr hwnd = IntPtr.Zero;
        var clock = System.Diagnostics.Stopwatch.StartNew();
        while (hwnd == IntPtr.Zero && clock.ElapsedMilliseconds < 10000) { System.Threading.Thread.Sleep(100); hwnd = FindWindowW("#32770", title); }
        if (hwnd == IntPtr.Zero) return "message box not found";
        System.Threading.Thread.Sleep(600);
        string result = Capture(hwnd, path);
        Close(hwnd); // Cancel: the "Close" choice.
        if (!thread.Join(5000)) return "message box did not close";
        return result;
    }
    // Observe the unchanged product same-DPI font lifetime without showing a window.
    public static string FontLifetimeControl() {
        using (var view = new ToolkitSupportView(ToolkitSetupText.Failure("Synthetic failure", false), "sample", "", false)) {
            Font before = view.StepLine.Font;
            view.ApplyDpi(96);
            bool retained = Object.ReferenceEquals(before, view.StepLine.Font);
            try {
                IntPtr font = view.StepLine.Font.ToHfont();
                DeleteObject(font);
                return "retained=" + retained + "; font still usable";
            } catch (ArgumentException) {
                return "retained=" + retained + "; disposed font reproduced";
            }
        }
    }
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
    // Real clipboard: Copy details puts exactly the summary text there and says Copied.
    public static string ClipboardCheck(string summary) {
        string result = "not run";
        var thread = new System.Threading.Thread(delegate() {
            try {
                using (var form = new Form()) {
                    var view = new ToolkitSupportView(ToolkitSetupText.Failure("Synthetic failure", false), summary, "", false);
                    form.Controls.Add(view); form.ClientSize = view.Size;
                    view.DoCopy();
                    string back = Clipboard.GetText();
                    result = view.CopyState == "copied" && back == summary && view.HelpLine.Text == ToolkitSetupText.CopiedLine ? "ok" : "mismatch: " + view.CopyState;
                }
            } catch (Exception e) { result = "error: " + e.Message; }
        });
        thread.IsBackground = true; thread.SetApartmentState(System.Threading.ApartmentState.STA); thread.Start();
        if (!thread.Join(15000)) return "timeout";
        return result;
    }
    static Form[] OpenForms() {
        var forms = new Form[Application.OpenForms.Count];
        ((System.Collections.ICollection)Application.OpenForms).CopyTo(forms, 0);
        return forms;
    }
    // The support dialog the controller opened on its own thread (not found by title).
    public static IntPtr SupportDialog() {
        foreach (Form form in OpenForms()) {
            var dialog = form as ToolkitSupportForm;
            if (dialog == null || !dialog.IsHandleCreated) continue;
            IntPtr handle = IntPtr.Zero;
            IAsyncResult pending = dialog.BeginInvoke((MethodInvoker)delegate { handle = dialog.Handle; });
            using (var ready = pending.AsyncWaitHandle) {
                if (!ready.WaitOne(2000)) return IntPtr.Zero;
                dialog.EndInvoke(pending);
            }
            return handle;
        }
        return IntPtr.Zero;
    }
    public static string SupportLayoutCheck() {
        foreach (Form form in OpenForms()) {
            var dialog = form as ToolkitSupportForm;
            if (dialog == null || !dialog.IsHandleCreated) continue;
            string result = "timeout";
            IAsyncResult pending = dialog.BeginInvoke((MethodInvoker)delegate {
                ToolkitPreview.CheckBounds(dialog.View, "live-stall-dialog");
                result = dialog.ClientSize == dialog.View.Size ? "ok" : "client " + dialog.ClientSize + "; view " + dialog.View.Size;
            });
            using (var ready = pending.AsyncWaitHandle) {
                if (!ready.WaitOne(2000)) return "timeout";
                dialog.EndInvoke(pending);
            }
            return result;
        }
        return "no dialog";
    }
    // Same as pressing Esc or the close box: Stop setup.
    public static void CloseSupportDialogs() {
        foreach (Form form in OpenForms()) {
            var dialog = form as ToolkitSupportForm;
            if (dialog != null && dialog.IsHandleCreated) dialog.BeginInvoke((MethodInvoker)delegate { dialog.Close(); });
        }
    }
    static bool keepWaiting;
    static System.Threading.Thread details;
    // ToolkitSetupStatus.Details blocks until the dialog closes, so run it on its own thread.
    public static void StartDetails(string message, string summary, string folder, bool allowWait) {
        details = new System.Threading.Thread(delegate() { keepWaiting = ToolkitSetupStatus.Details(message, summary, folder, allowWait); });
        details.IsBackground = true; details.Start();
    }
    public static string FinishDetails() {
        if (details == null) return "not started";
        return details.Join(10000) ? (keepWaiting ? "keep-waiting" : "stopped") : "still open";
    }
}
'@

function Write-PreviewProgress([string]$Message) {
    $line = [DateTime]::UtcNow.ToString('o') + " $Phase " + $Message
    Write-Host $line
    [Console]::Out.Flush()
    [IO.File]::AppendAllText((Join-Path $OutDir 'progress.txt'), $line + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}
function Save-LiveCapture([IntPtr]$Handle, [string]$Key, [string]$Name) {
    Write-PreviewProgress "begin write $Name"
    $receipt.live[$Key] = [ToolkitPreviewWindows]::Capture($Handle, (Join-Path $OutDir $Name))
    Write-PreviewProgress "end write $Name ($($receipt.live[$Key]))"
    if ($receipt.live[$Key] -ne 'ok') { throw "Live capture $Key failed: $($receipt.live[$Key])" }
}
function Save-PreviewReceipt($Receipt, [string]$Path) {
    Write-PreviewProgress "begin write $Path"
    [IO.File]::WriteAllText($Path, ($Receipt | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Write-PreviewProgress "end write $Path"
}

if ($env:OS -ne 'Windows_NT') { throw 'This renders with the Windows WinForms runtime. On Linux, use the Mono previews.' }
if (-not $Phase) {
    & (Join-Path $PSScriptRoot 'invoke-preview-phases.ps1') -OutDir $OutDir -HighContrast:$HighContrast -LauncherTextFile $LauncherTextFile -Filter $Filter -SkipLive:$SkipLive -TimeoutProbe:$TimeoutProbe -PhaseTimeoutSeconds $PhaseTimeoutSeconds
    exit $LASTEXITCODE
}
if (-not [IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path (Get-Location) $OutDir }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$receipt = [ordered]@{
    files = @(); problems = @(); live = [ordered]@{}; fallback = [ordered]@{}; failed = $false
}
$receiptPath = Join-Path $OutDir "$Phase.json"
Write-PreviewProgress 'begin worker'
if ($Phase -eq 'timeout-probe') { Write-PreviewProgress 'deliberately hanging'; Start-Sleep -Seconds 60; exit 2 }
if ($Phase -eq 'success-probe') { Save-PreviewReceipt $receipt $receiptPath; exit 0 }
try {
    Write-PreviewProgress 'begin source console.ps1'
    . (Join-Path $PSScriptRoot 'console.ps1')
    Write-PreviewProgress 'end source console.ps1; begin Add-Type'
    Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition ($script:ToolkitSetupUiSource + $script:ToolkitQuestionModelSource + $script:ToolkitPreviewSource + $script:ToolkitPreviewWindowsSource)
    [ToolkitPreview]::ProgressPath = Join-Path $OutDir 'progress.txt'
    [ToolkitPreview]::Phase = $Phase
    Write-PreviewProgress 'end Add-Type; begin DPI awareness'
    [ToolkitTheme]::UsePerMonitorDpi()
    Write-PreviewProgress 'end DPI awareness'
    if ($HighContrast) {
        Write-PreviewProgress 'begin enable Windows high contrast'
        $receipt.high_contrast_switched = [ToolkitPreviewWindows]::HighContrast($true)
        Start-Sleep -Seconds 3
        $receipt.high_contrast_active = [Windows.Forms.SystemInformation]::HighContrast
        Write-PreviewProgress "end enable Windows high contrast: active=$($receipt.high_contrast_active)"
        if (-not $receipt.high_contrast_switched -or -not $receipt.high_contrast_active) { throw 'Windows high contrast did not activate' }
    }
    $save = [ToolkitPreview+Saver]{
        param([Windows.Forms.Control]$Control, [string]$Name)
        Write-PreviewProgress "begin DrawToBitmap $Name"
        $bitmap = New-Object Drawing.Bitmap $Control.Width, $Control.Height
        try {
            $Control.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle 0, 0, $Control.Width, $Control.Height))
            Write-PreviewProgress "end DrawToBitmap $Name; begin write $Name.png"
            $bitmap.Save((Join-Path $OutDir ($Name + '.png')), [Drawing.Imaging.ImageFormat]::Png)
            Write-PreviewProgress "end write $Name.png"
        } finally { $bitmap.Dispose() }
        [ToolkitPreview]::Written.Add("$Name $($Control.Width)x$($Control.Height)")
    }
    Write-PreviewProgress "begin action $Phase"
    if ($Phase -match '^setup-(96|144|192)$') {
        [ToolkitPreview]::RenderSetup([int[]]@([int]$Matches[1]), $Filter, '', $save)
    } elseif ($Phase -match '^support-(96|144|192)$') {
        [ToolkitPreview]::RenderSupport([int[]]@([int]$Matches[1]), $Filter, '', $save)
    } elseif ($Phase -eq 'simulated-hc') {
        [ToolkitPreview]::RenderSimulatedHighContrast($Filter, $save)
    } elseif ($Phase -eq 'fallback-setup') {
        $text = Get-ToolkitFallbackText 'Opening the toolkit stalled: its window did not appear within 120 seconds. Close other WSL work, run wsl --shutdown, then reopen.'
        $path = Join-Path $OutDir 'fallback-setup-window@100.png'
        Write-PreviewProgress "begin write $path"
        $receipt.fallback.setup_window = [ToolkitPreviewWindows]::CaptureMessageBox($text, (Get-ToolkitFallbackTitle), $path)
        Write-PreviewProgress "end write $path ($($receipt.fallback.setup_window))"
        if ($receipt.fallback.setup_window -ne 'ok') { throw 'Fallback setup capture failed' }
    } elseif ($Phase -eq 'launcher-text') {
        if (-not $LauncherTextFile) {
            $LauncherTextFile = Join-Path $OutDir 'launcher-fallback.txt'
            $code = "import importlib.util,sys; s=importlib.util.spec_from_file_location('l',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); open(sys.argv[2],'w',encoding='utf-8').write(m.error_dialog_text('Windows setup could not start or ended without a result (PowerShell exit 1). Restart Windows and reopen the toolkit; use Copy details if it happens again.', m.Path(sys.argv[3])))"
            Write-PreviewProgress "begin write $LauncherTextFile"
            & python -c $code (Join-Path $PSScriptRoot 'windows_launcher.py') $LauncherTextFile 'C:\Users\Owner\AppData\Local\Jibo-DFU-Toolkit\logs\setup-20261016T194218.821Z-1a2b3c4d.log'
            if ($LASTEXITCODE -ne 0) { throw 'Launcher text generation failed' }
            Write-PreviewProgress "end write $LauncherTextFile"
        }
    } elseif ($Phase -eq 'fallback-launcher') {
        if (-not $LauncherTextFile) { $LauncherTextFile = Join-Path $OutDir 'launcher-fallback.txt' }
        $text = [IO.File]::ReadAllText($LauncherTextFile, [Text.Encoding]::UTF8)
        $path = Join-Path $OutDir 'fallback-early-error@100.png'
        Write-PreviewProgress "begin write $path"
        $receipt.fallback.launcher = [ToolkitPreviewWindows]::CaptureMessageBox($text, (Get-ToolkitFallbackTitle), $path)
        Write-PreviewProgress "end write $path ($($receipt.fallback.launcher))"
        if ($receipt.fallback.launcher -ne 'ok') { throw 'Fallback launcher capture failed' }
    } elseif ($Phase -eq 'font-control') {
        $receipt.font_lifetime_control = [ToolkitPreviewWindows]::FontLifetimeControl()
        Write-PreviewProgress $receipt.font_lifetime_control
    } elseif ($Phase -eq 'clipboard') {
        $receipt.clipboard = [ToolkitPreviewWindows]::ClipboardCheck([ToolkitPreview]::DetailsFor('Synthetic failure'))
        if ($receipt.clipboard -ne 'ok') { throw "Clipboard: $($receipt.clipboard)" }
    } elseif ($Phase -eq 'live') {
        Write-PreviewProgress 'begin live controller Start'
        [ToolkitSetupStatus]::Start()
        Write-PreviewProgress 'end live controller Start'
        $owner = [ToolkitSetupStatus]::OwnerHandle
        [ToolkitSetupStatus]::Update('Downloading Ubuntu (117.0 of 344.0 MB)', 'Only received bytes count as progress. You will be warned if no bytes arrive.', 34)
        Start-Sleep -Milliseconds 600
        Save-LiveCapture $owner 'working' 'live-working-download@100.png'
        [ToolkitSetupStatus]::AskQuestion([ToolkitPreview]::Prompts[1])
        Start-Sleep -Milliseconds 900
        Save-LiveCapture $owner 'question' 'live-question-linux@100.png'
        [ToolkitSetupStatus]::AnswerQuestion('closed')
        Start-Sleep -Milliseconds 400
        Save-LiveCapture $owner 'question_closed' 'live-question-closed@100.png'
        [ToolkitSetupStatus]::UserWait($true, [ToolkitPreview]::Waits[0])
        Start-Sleep -Milliseconds 600
        Save-LiveCapture $owner 'waiting' 'live-waiting-admin-prompt@100.png'
        [ToolkitSetupStatus]::UserWait($false, '')
        $stall = [ToolkitPreview]::SupportStates()[0]
        # Exercise the controller unchanged at 125%; its native 100% OnLoad
        # redundantly reapplies 96 DPI and has a separately reported font bug.
        # 125% also fits the hosted runner desktop; 150% was visibly clamped.
        [ToolkitTheme]::ForcedDpi = 120
        $receipt.live.stall_dpi = 120
        Write-PreviewProgress 'begin live stall dialog at forced 120 DPI (native 96 DPI product defect recorded separately)'
        [ToolkitPreviewWindows]::StartDetails($stall[1], [ToolkitPreview]::DetailsFor($stall[1]), [ToolkitPreview]::LogFolder, $true)
        $dialog = [IntPtr]::Zero
        for ($i = 0; $i -lt 50 -and $dialog -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 100; $dialog = [ToolkitPreviewWindows]::SupportDialog() }
        Start-Sleep -Milliseconds 600
        Save-LiveCapture $dialog 'stall_dialog' 'live-dialog-stall-download@125.png'
        $receipt.live.stall_layout = [ToolkitPreviewWindows]::SupportLayoutCheck()
        if ($receipt.live.stall_layout -ne 'ok') { throw "Live support layout: $($receipt.live.stall_layout)" }
        Save-LiveCapture $owner 'behind_dialog' 'live-behind-stall-download@100.png'
        [ToolkitPreviewWindows]::CloseSupportDialogs()
        $receipt.live.stall_answer = [ToolkitPreviewWindows]::FinishDetails()
        Write-PreviewProgress "end live stall dialog: $($receipt.live.stall_answer)"
        if ($receipt.live.stall_answer -ne 'stopped') { throw 'Live stall dialog did not stop' }
        [ToolkitTheme]::ForcedDpi = 0
        Write-PreviewProgress 'begin live controller Stop'
        [ToolkitSetupStatus]::Stop()
        Write-PreviewProgress 'end live controller Stop'
    } else { throw "Unknown phase $Phase" }
    Write-PreviewProgress "end action $Phase"
} catch {
    $receipt.error = $_.Exception.ToString()
    $receipt.failed = $true
    Write-PreviewProgress "error $($receipt.error)"
} finally {
    Write-PreviewProgress 'begin cleanup'
    if ('ToolkitPreviewWindows' -as [type]) { try { [ToolkitPreviewWindows]::CloseSupportDialogs() } catch { } }
    if ('ToolkitSetupStatus' -as [type]) { try { [ToolkitSetupStatus]::Stop() } catch { } }
    if ($HighContrast -and ('ToolkitPreviewWindows' -as [type])) {
        Write-PreviewProgress 'begin restore Windows high contrast'
        $receipt.high_contrast_restored = [ToolkitPreviewWindows]::HighContrast($false)
        Write-PreviewProgress "end restore Windows high contrast: $($receipt.high_contrast_restored)"
    }
    Write-PreviewProgress 'end cleanup'
}
if ('ToolkitPreview' -as [type]) {
    $receipt.files = @([ToolkitPreview]::Written)
    $receipt.problems = @([ToolkitPreview]::Problems)
    if ([ToolkitPreview]::Problems.Count) { $receipt.failed = $true }
}
Save-PreviewReceipt $receipt $receiptPath
Write-PreviewProgress "end worker: $($receipt.files.Count) previews, $($receipt.problems.Count) layout problems"
if ($receipt.failed) { exit 1 }
exit 0
