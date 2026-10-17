// Cross-runtime fixture transport only: PowerShell Core cannot load Mono WinForms.
// UI calls and diagnostics run in the foreground Mono controller, not this proxy.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
public sealed class FixtureClipboardFailure {
    public string Utc, ExceptionType, Category, HResult, Apartment;
    public int ManagedThreadId, SummaryLength;
}
public static class ToolkitSetupStatus {
    static Process child;
    public static string Mode = "external", Trace = "";
    public static bool ThrowAfterDialog;
    public static IntPtr OwnerHandle { get { return IntPtr.Zero; } }
    public static void Connect(string binary) {
        var info = new ProcessStartInfo("mono", "\"" + binary + "\"");
        info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardInput = true; info.RedirectStandardOutput = true;
        child = Process.Start(info);
    }
    static string[] Command(string text) {
        child.StandardInput.WriteLine(text); child.StandardInput.Flush();
        var lines = new List<string>();
        string line;
        while ((line = child.StandardOutput.ReadLine()) != null) {
            if (line.StartsWith("OK ")) { Trace += line + ";"; return lines.ToArray(); }
            lines.Add(line);
        }
        throw new Exception("fixture controller exited before completion");
    }
    public static void Start() { Command("START"); }
    public static void Stop() { Command("STOP"); }
    public static bool Details(string message, string summary, string folder, bool allowWait) {
        Command("DETAILS\t" + Mode + "\t" + Convert.ToBase64String(Encoding.UTF8.GetBytes(summary)));
        if (ThrowAfterDialog) throw new InvalidOperationException("synthetic wrapper error");
        return false;
    }
    public static FixtureClipboardFailure[] DrainClipboardFailures() {
        var rows = new List<FixtureClipboardFailure>();
        foreach (string line in Command("DRAIN")) {
            string[] f = line.Split('\t');
            if (f.Length != 8 || f[0] != "EVENT") throw new Exception("invalid fixture protocol");
            rows.Add(new FixtureClipboardFailure { Utc=f[1], ExceptionType=f[2], Category=f[3], HResult=f[4], Apartment=f[5], ManagedThreadId=Int32.Parse(f[6]), SummaryLength=Int32.Parse(f[7]) });
        }
        return rows.ToArray();
    }
    public static void Close() {
        try {
            Command("QUIT");
            if (!child.WaitForExit(5000) || child.ExitCode != 0) throw new Exception("fixture controller exit failure");
        } finally { if (!child.HasExited) child.Kill(); child.Dispose(); }
    }
}
