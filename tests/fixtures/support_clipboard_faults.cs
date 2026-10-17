// Synthetic boundary: no System.Windows.Forms.Clipboard operation is performed.
public sealed class fixture_private_type_Exception : Exception {
    public override string Message { get { throw new Exception("exception message must never be read"); } }
}
public static class Clipboard {
    public static string Mode, Value;
    public static int Calls;
    public static void SetText(string text) {
        Calls++;
        if (Mode == "null") throw new ArgumentNullException("text", "fixture-private-exception-message");
        if (Mode == "external") throw new ExternalException("fixture-private-exception-message", unchecked((int)0x800401D0));
        if (Mode == "thread") throw new ThreadStateException("fixture-private-exception-message");
        if (Mode == "invalid-operation") throw new InvalidOperationException("fixture-private-exception-message");
        if (Mode == "custom") throw new fixture_private_type_Exception();
        Value = text;
    }
}
public static class SupportClipboardFaultTests {
    static void Require(bool ok, string reason) { if (!ok) throw new Exception(reason); }
    static object Field(object row, string name) { return row.GetType().GetField(name).GetValue(row); }
    static Array Drain() {
        var method = typeof(ToolkitSetupStatus).GetMethod("DrainClipboardFailures");
        Require(method != null, "COPY-PROVENANCE: failure event was discarded (no drain)");
        return (Array)method.Invoke(null, null);
    }
    [STAThread]
    public static int Main() {
        try {
            const string summary = "Synthetic support summary with fixture-private-summary-payload";
            using (var form = new Form()) {
                var view = new ToolkitSupportView(ToolkitSetupText.Failure("Synthetic failure", false), summary, "", false);
                form.Controls.Add(view); form.ClientSize = view.Size;
                form.Show(); Application.DoEvents();
                foreach (string mode in new string[] { "external", "thread", "invalid-operation", "custom", "success" }) {
                    Clipboard.Mode = mode;
                    int before = Clipboard.Calls;
                    view.Copy.PerformClick();
                    Require(Clipboard.Calls == before + 1, "copy click missing or speculative retries added");
                    Require(view.Details.Text == summary, "support text mutated");
                    if (mode == "success") {
                        Require(view.CopyState == "copied" && view.HelpLine.Text == ToolkitSetupText.CopiedLine && Clipboard.Value == summary, "copy recovery changed");
                        Require(Drain().Length == 0, "success emitted failure");
                    } else {
                        Require(view.CopyState == "failed" && view.Copy.Text == "Copy again" && view.HelpLine.Text == ToolkitSetupText.CopyFailedLine, "failure UI changed");
                        Require(view.Details.SelectedText == view.Details.Text && view.Details.SelectionLength > 0 && view.Details.ShortcutsEnabled && view.Details.Focused, "manual Ctrl+C fallback unavailable");
                        var rows = Drain(); Require(rows.Length == 1, "COPY-PROVENANCE: expected one failure event");
                        object row = rows.GetValue(0);
                        string expectedType = mode == "external" ? "System.Runtime.InteropServices.ExternalException" : mode == "thread" ? "System.Threading.ThreadStateException" : mode == "custom" ? "other" : "System.InvalidOperationException";
                        string category = mode == "external" ? "external" : mode == "thread" ? "thread-state" : mode == "custom" ? "unexpected" : "invalid-operation";
                        Require((string)Field(row, "ExceptionType") == expectedType && (string)Field(row, "Category") == category, "exception classes collapsed");
                        Require((string)Field(row, "Apartment") == "STA" && (int)Field(row, "ManagedThreadId") == Thread.CurrentThread.ManagedThreadId, "wrong failure thread");
                        Require((int)Field(row, "SummaryLength") == summary.Length, "wrong length");
                        if (mode == "external") Require((string)Field(row, "HResult") == "0x800401D0", "wrong HRESULT");
                        foreach (var f in row.GetType().GetFields()) {
                            object value = f.GetValue(row);
                            Require(!(value is Exception), "retained exception object");
                            Require(value == null || (!value.ToString().Contains("fixture-private") && !value.ToString().Contains("fixture_private")), "sensitive diagnostic value");
                        }
                        Require(Drain().Length == 0, "queue did not empty");
                    }
                    Console.WriteLine("PASS copy-" + mode + " manual-selection-and-shortcuts=" + view.Details.ShortcutsEnabled);
                }
                form.Close();
            }
            using (var form = new Form()) {
                var view = new ToolkitSupportView(ToolkitSetupText.Failure("Synthetic failure", false), null, "", false);
                form.Controls.Add(view); form.ClientSize = view.Size; form.Show(); Application.DoEvents();
                Clipboard.Mode = "null";
                view.Copy.PerformClick();
                Require(view.CopyState == "failed", "null summary failure UI changed");
                var rows = Drain(); Require(rows.Length == 1, "null failure was not recorded");
                Require((int)Field(rows.GetValue(0), "SummaryLength") == 0 && (string)Field(rows.GetValue(0), "Category") == "argument", "null provenance incorrect");
                form.Close();
            }
            Console.WriteLine("PASS null-summary-diagnostic-does-not-escape");
            Console.WriteLine("PASS COMPILED-COPY-PROVENANCE");
            return 0;
        } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
