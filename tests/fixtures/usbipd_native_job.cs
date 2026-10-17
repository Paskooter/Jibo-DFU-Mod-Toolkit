// Test-owned outer containment, independent of every product/mutant Job.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
public sealed class NativeBoundsJob : IDisposable {
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr j, uint c);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorking, MaxWorking;
        public uint ActiveLimit;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct Extended {
        public Limits Basic; public Io Counters;
        public UIntPtr ProcessMemory, JobMemory, PeakProcess, PeakJob;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long User, Kernel, PeriodUser, PeriodKernel;
        public uint Faults, Total, Active, Terminated;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref Extended e, int size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr j, int c, out Accounting a, int size, IntPtr length);
    IntPtr job;
    public NativeBoundsJob() {
        job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero) throw new InvalidOperationException("Outer Job creation failed");
        Extended e=new Extended(); e.Basic.Flags=0x2000;
        if(!SetInformationJobObject(job,9,ref e,Marshal.SizeOf(typeof(Extended)))) {
            Dispose(); throw new InvalidOperationException("Outer Job kill-on-close failed");
        }
    }
    public void Assign(Process process) {
        if(!AssignProcessToJobObject(job, process.Handle)) throw new InvalidOperationException("Outer Job assignment failed before gate");
    }
    public bool Stop(int milliseconds) {
        if(!TerminateJobObject(job, 1)) throw new InvalidOperationException("Outer Job termination failed");
        Stopwatch t=Stopwatch.StartNew();
        do {
            Accounting status;
            if(!QueryInformationJobObject(job,1,out status,Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero))
                throw new InvalidOperationException("Outer Job accounting failed");
            if(status.Active == 0) return true;
            Thread.Sleep(2);
        } while(t.ElapsedMilliseconds<milliseconds);
        return false;
    }
    public void Dispose() { if(job!=IntPtr.Zero) { CloseHandle(job); job=IntPtr.Zero; } }
}
// PID files grant observation only. Retain handles before the fixture gate opens;
// all signalling is exclusively via NativeBoundsJob owned by the outer caller.
public sealed class NativeBoundsObserver : IDisposable {
    readonly string folder, executable;
    readonly Thread thread;
    volatile bool stop;
    Process parent, child;
    public string Error;
    public int ParentId, ChildId;
    public bool ParentObserved { get { return parent!=null; } }
    public bool ChildObserved { get { return child!=null; } }
    public bool ParentExited { get { return parent!=null && parent.HasExited; } }
    public bool ChildExited { get { return child!=null && child.HasExited; } }
    public NativeBoundsObserver(string folder, string executable) {
        this.folder=folder; this.executable=executable;
        thread=new Thread(Watch); thread.IsBackground = true; thread.Start();
    }
    Process Observe(string role) {
        string file=Path.Combine(folder,role+".pid");
        if(!File.Exists(file)) return null;
        Process p=Process.GetProcessById(Int32.Parse(File.ReadAllText(file)));
        try {
            IntPtr retained=p.Handle;
            if(p.HasExited || !String.Equals(p.MainModule.FileName,executable,StringComparison.OrdinalIgnoreCase) ||
               Math.Abs((DateTime.UtcNow-p.StartTime.ToUniversalTime()).TotalSeconds)>10)
                throw new InvalidOperationException("Observation identity mismatch");
            File.WriteAllText(Path.Combine(folder,role+".observed"),"retained");
            return p;
        } catch { p.Dispose(); throw; }
    }
    void Watch() {
        try {
            while(!stop) {
                if(parent==null) { parent=Observe("parent"); if(parent!=null) ParentId=parent.Id; }
                if(child==null) { child=Observe("child"); if(child!=null) ChildId=child.Id; }
                Thread.Sleep(1);
            }
        } catch(Exception e) { Error=e.GetType().FullName; }
    }
    public void Finish() { stop=true; if(!thread.Join(1000)) throw new InvalidOperationException("Observer did not stop"); }
    public void Dispose() { Finish(); if(parent!=null) parent.Dispose(); if(child!=null) child.Dispose(); }
}
