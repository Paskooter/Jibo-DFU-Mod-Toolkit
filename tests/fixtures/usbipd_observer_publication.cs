// Deterministic portable publication/observer control, NOT Windows qualification.
// Compile the actual producer with File aliased to PublicationFile. Only its file
// transport is paused: StreamWriter uses File.WriteAllText's UTF8/no-BOM and
// FileShare.Read contract. The actual Observe method reads/validates real bytes
// and real process handles; no process is signalled and no USB API is called.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text;
using System.Threading;

public sealed class PublicationComplete : Exception { }
public static class PublicationFile {
    public static Action DuringWrite;
    public static bool SharingConflict;
    public static void WriteAllText(string path, string text) {
        // Mono does not enforce the Framework/Win32 two-way FileShare.Read
        // conflict. Also force that conflict with a real exclusive FileStream;
        // this is an explicit sharing-fault model, not native Windows evidence.
        using(FileStream stream=new FileStream(path,FileMode.Create,FileAccess.Write,
                  SharingConflict ? FileShare.None : FileShare.Read))
        using(StreamWriter writer=new StreamWriter(stream,new UTF8Encoding(false))) {
            DuringWrite(); // Created/open, before any PID bytes are written.
            writer.Write(text); writer.Flush();
            DuringWrite(); // Complete bytes, but writer still owns its handle.
        }
        if(path.EndsWith(".pid",StringComparison.Ordinal)) throw new PublicationComplete();
    }
    public static void Move(string source, string target) {
        File.Move(source,target);
        if(target.EndsWith(".pid",StringComparison.Ordinal)) throw new PublicationComplete();
    }
    public static bool Exists(string path) { return File.Exists(path); }
    public static string ReadAllText(string path) { return File.ReadAllText(path); }
    public static void AppendAllText(string path, string value) { File.AppendAllText(path,value); }
}
public static class ObserverPublicationControl {
    static readonly MethodInfo ObserveMethod=typeof(NativeBoundsObserver).GetMethod("Observe",BindingFlags.Instance|BindingFlags.NonPublic);
    static void Require(bool value, string message) { if(!value) throw new Exception(message); }
    static Process Observe(NativeBoundsObserver observer, string role) {
        try { return (Process)ObserveMethod.Invoke(observer,new object[]{role}); }
        catch(TargetInvocationException e) { ExceptionDispatchInfo.Capture(e.InnerException).Throw(); throw; }
    }
    static string Executable() {
        using(Process current=Process.GetCurrentProcess()) { return current.MainModule.FileName; }
    }
    static void Publication(string folder, string phase, string role) {
        // Finish the polling thread before a synchronous, precisely scheduled
        // Observe call. Separate controls below exercise Watch/Finish/Error.
        using(NativeBoundsObserver observer=new NativeBoundsObserver(folder,Executable())) {
            observer.Finish();
            Require(observer.Error==null,"unexpected initial observer error");
            int checks=0;
            PublicationFile.DuringWrite=delegate {
                using(Process found=Observe(observer,role)) {
                    Require(found==null,"unclosed PID publication was observed");
                }
                Require(!File.Exists(Path.Combine(folder,role+".observed")),"writer released before publication");
                checks++;
            };
            Environment.SetEnvironmentVariable("USBIPD_BOUNDS_DIRECTORY",folder);
            Environment.SetEnvironmentVariable("USBIPD_BOUNDS_MODE",phase=="bounds-hang" ? "hang" : "pipe");
            bool completed=false;
            try { UsbipdBoundsFixture.Main(new string[]{role=="child" ? "--hold" : "--version"}); }
            catch(PublicationComplete) { completed=true; }
            Require(completed && checks==2,"real fixture PID publication slice not completed");
            using(Process found=Observe(observer,role)) {
                Require(found!=null && found.Id==Process.GetCurrentProcess().Id && !found.HasExited,"published live identity not retained");
            }
            Require(File.ReadAllText(Path.Combine(folder,role+".observed"))=="retained","observation marker missing");
            Require(File.ReadAllText(Path.Combine(folder,role+".pid"))==Process.GetCurrentProcess().Id.ToString(),"PID bytes changed");
            Require(Directory.GetFiles(folder,"*.writing").Length==0,"publication temporary file left behind");
            Console.WriteLine("PASS "+phase+" "+role+": invisible during both open-writer stages, real identity retained after close/publish");
        }
    }
    static void Refusal(string folder, string mode) {
        string pid=Path.Combine(folder,"child.pid");
        string expected=mode=="corrupt" ? "System.FormatException" : mode=="identity" ? "System.InvalidOperationException" : "System.IO.IOException";
        File.WriteAllText(pid,mode=="corrupt" ? "not-a-pid" : Process.GetCurrentProcess().Id.ToString());
        using(FileStream locked=mode=="locked" ? new FileStream(pid,FileMode.Open,FileAccess.Write,FileShare.None) : null)
        using(NativeBoundsObserver observer=new NativeBoundsObserver(folder,mode=="identity" ? Executable()+".wrong" : Executable())) {
            Require(SpinWait.SpinUntil(delegate {return observer.Error!=null;},2000),"observer error was swallowed or not observed");
            observer.Finish();
            Require(observer.Error==expected,"wrong observer error: "+observer.Error);
            Require(!observer.ChildObserved && observer.ChildId==0,"failed query became retained observation");
            Require(!File.Exists(Path.Combine(folder,"child.observed")),"failed query released fixture gate");
            Console.WriteLine("PASS fail-closed "+mode+": "+observer.Error+", no retained identity/marker");
        }
    }
    public static int Main(string[] args) {
        try {
            if(args[0]=="publish") {
                PublicationFile.SharingConflict=args[4]=="sharing-conflict";
                Publication(args[1],args[2],args[3]);
            }
            else Refusal(args[1],args[0]);
            return 0;
        } catch(Exception e) {
            Console.Error.WriteLine("FAIL "+args[0]+": "+e.GetType().FullName+" HRESULT=0x"+e.HResult.ToString("X8")+"\n"+e);
            return 1;
        }
    }
}
