using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

internal static class UpdateRunner
{
    private static string Quote(string path) { return "\"" + path + "\""; }

    // PyInstaller's one-file bootloader and its child share the executable path.
    private static bool IsRunning(string exe)
    {
        foreach (Process p in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(exe)))
        {
            using (p)
            {
                try
                {
                    if (String.Equals(p.MainModule.FileName, exe, StringComparison.OrdinalIgnoreCase))
                        return true;
                }
                catch (InvalidOperationException) { } // Exited during enumeration.
                catch (System.ComponentModel.Win32Exception) { } // Different user's process.
            }
        }
        return false;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 3) return 2;
        string package = Path.GetFullPath(args[0]);
        string exe = Path.GetFullPath(args[2]);
        string log = Path.ChangeExtension(package, ".log");
        bool restart = false;
        try
        {
            var wait = Stopwatch.StartNew();
            while (IsRunning(exe))
            {
                if (wait.Elapsed.TotalSeconds >= 45)
                    throw new IOException("仍有拾卷窗口正在运行，请关闭后重新检查更新。");
                Thread.Sleep(200);
            }
            restart = true;
            using (var file = File.Open(package, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (var hash = SHA256.Create())
            {
                string actual = BitConverter.ToString(hash.ComputeHash(file)).Replace("-", "");
                if (!String.Equals(actual, args[1], StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装包校验失败。");
                string options = "/i " + Quote(package) + " /qn /norestart /L*v " + Quote(log);
                var start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "msiexec.exe"), options);
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                using (var msi = Process.Start(start))
                {
                    msi.WaitForExit();
                    if (msi.ExitCode != 0 && msi.ExitCode != 3010)
                        throw new IOException("Windows Installer 返回 " + msi.ExitCode + "。");
                }
            }
            if (!File.Exists(exe)) throw new FileNotFoundException("在原安装目录未找到拾卷。");
            Process.Start(exe);
            restart = false;
            try { File.Delete(package); } catch (IOException) { }
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show(e.Message + Environment.NewLine + "更新日志：" + log, "拾卷更新", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            if (restart && File.Exists(exe)) Process.Start(exe);
            return 1;
        }
    }
}
