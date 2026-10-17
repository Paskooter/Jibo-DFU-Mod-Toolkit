// Offline .NET Framework / Windows PowerShell 5.1 fixture. Never touches USB/WSL.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
public static class UsbipdBoundsFixture {
    public static int Main(string[] args) {
        string folder=Environment.GetEnvironmentVariable("USBIPD_BOUNDS_DIRECTORY");
        string mode=Environment.GetEnvironmentVariable("USBIPD_BOUNDS_MODE") ?? "healthy";
        if(String.IsNullOrEmpty(folder)) return 91;
        if(args.Length==1 && args[0]=="--hold") {
            File.WriteAllText(Path.Combine(folder,"child.pid"),Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(60000); return 0;
        }
        if(args.Length!=1 || args[0]!="--version") return 92;
        File.AppendAllText(Path.Combine(folder,"probe.entries"),"version\n");
        File.WriteAllText(Path.Combine(folder,"parent.pid"),Process.GetCurrentProcess().Id.ToString());
        if(mode=="hang" || mode=="pipe") {
            ProcessStartInfo info=new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,"--hold");
            info.UseShellExecute=false; info.CreateNoWindow=true;
            // Inherit the current redirected native stdout/stderr handles.
            Process child=Process.Start(info);
            IntPtr retained=child.Handle;
            Stopwatch t=Stopwatch.StartNew();
            while(!File.Exists(Path.Combine(folder,"child.pid")) && t.ElapsedMilliseconds<2000) Thread.Sleep(5);
            if(!File.Exists(Path.Combine(folder,"child.pid"))) return 93;
            if(mode=="hang") { child.WaitForExit(); return 94; }
            Console.WriteLine("5.3.0"); return 0;
        }
        if(mode=="broken") return 17;
        if(mode=="noisy-stderr") Console.Error.Write(new string('x',5000));
        if(mode=="blocked-output") { for(;;) Console.Write("token=fixture-secret"); }
        Console.WriteLine("5.3.0"); return 0;
    }
}
