// Shared driver and portable controller server. Python shims FlashWindowEx and
// GetClientRect and stops the setup window before Details for Mono X11. Native
// mode uses only Exercise, with neither platform shim nor the extra Stop.
public static class SupportFailureController {
    static string driverError;
    public static string DriverError { get { return driverError; } }
    public static bool ThrowAfterDialog;
    public static int DialogCount;
    static void Require(bool ok, string message) { if (!ok) throw new Exception(message); }
    static ToolkitSupportForm WaitForDialog() {
        var clock = System.Diagnostics.Stopwatch.StartNew();
        while (clock.Elapsed.TotalSeconds < 8) {
            try {
                foreach (Form form in Application.OpenForms)
                    if (form is ToolkitSupportForm && form.IsHandleCreated && !form.IsDisposed) return (ToolkitSupportForm)form;
            } catch (InvalidOperationException) { }
            Thread.Sleep(20);
        }
        throw new Exception("dialog readiness timeout");
    }
    public static void Exercise(string mode) {
        driverError = null;
        Console.Error.WriteLine("fixture driver armed " + mode);
        var driver = new Thread(delegate() {
            try {
                var form = WaitForDialog();
                Console.Error.WriteLine("fixture dialog found");
                form.BeginInvoke((Action)delegate {
                    Console.Error.WriteLine("fixture dialog action entered");
                    Interlocked.Increment(ref DialogCount);
                    try {
                        var view = form.View;
                        Require(Thread.CurrentThread.GetApartmentState() == ApartmentState.STA, "dialog is not STA");
                        Clipboard.Mode = mode;
                        int before = Clipboard.Calls;
                        view.Copy.PerformClick();
                        Require(Clipboard.Calls == before + 1, "copy handler did not run exactly once");
                        if (mode == "success") Require(view.CopyState == "copied" && Clipboard.Value == view.Details.Text, "success changed");
                        else {
                            Require(view.CopyState == "failed" && view.HelpLine.Text == ToolkitSetupText.CopyFailedLine, "failure UI changed");
                            Require(view.Details.SelectedText == view.Details.Text && view.Details.ShortcutsEnabled, "manual fallback changed");
                        }
                    } catch (Exception e) { driverError = e.Message; }
                    finally { form.CloseButton.PerformClick(); }
                });
            } catch (Exception e) { driverError = e.Message; }
        });
        driver.IsBackground = true; driver.Start();
    }
    [MTAThread]
    public static int Main() {
        try {
            string command;
            while ((command = Console.ReadLine()) != null) {
                string[] fields = command.Split('\t');
                Console.Error.WriteLine("fixture command " + fields[0]);
                if (fields[0] == "START") {
                    ToolkitSetupStatus.Start();
                    var window = (Form)typeof(ToolkitSetupStatus).GetField("window", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static).GetValue(null);
                    using (var pumped = new ManualResetEvent(false)) {
                        window.BeginInvoke((Action)delegate { pumped.Set(); });
                        Require(pumped.WaitOne(5000), "setup message pump did not run");
                    }
                    Console.WriteLine("OK START");
                }
                else if (fields[0] == "STOP") { ToolkitSetupStatus.Stop(); Console.WriteLine("OK STOP"); }
                else if (fields[0] == "DETAILS") {
                    string summary = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(fields[2]));
                    // Mono X11 cannot safely run both WinForms UI threads at
                    // once (AddInvalidArea race). Native runner does NOT do this.
                    Console.Error.WriteLine("portable-only extra Stop before modal dialog");
                    ToolkitSetupStatus.Stop();
                    Exercise(fields[1]);
                    Require(!ToolkitSetupStatus.Details("Synthetic failure", summary, "", false), "unexpected keep waiting");
                    Require(driverError == null, driverError);
                    Console.WriteLine("OK DETAILS STA button=1 manual-fallback=ok");
                } else if (fields[0] == "DRAIN") {
                    // Reflection keeps the pre-feature RED executable compilable.
                    var method = typeof(ToolkitSetupStatus).GetMethod("DrainClipboardFailures");
                    var rows = method == null ? new object[0] : (Array)method.Invoke(null, null);
                    foreach (object row in rows) {
                        string[] names = { "Utc", "ExceptionType", "Category", "HResult", "Apartment", "ManagedThreadId", "SummaryLength" };
                        var values = new System.Collections.Generic.List<string>();
                        foreach (string name in names) values.Add(row.GetType().GetField(name).GetValue(row).ToString());
                        Console.WriteLine("EVENT\t" + String.Join("\t", values.ToArray()));
                    }
                    Console.WriteLine("OK DRAIN");
                } else if (fields[0] == "QUIT") { ToolkitSetupStatus.Stop(); Console.WriteLine("OK QUIT"); break; }
                else throw new Exception("unknown fixture command");
                Console.Out.Flush();
            }
            return 0;
        } catch (Exception e) { Console.Error.WriteLine(e); return 1; }
    }
}
