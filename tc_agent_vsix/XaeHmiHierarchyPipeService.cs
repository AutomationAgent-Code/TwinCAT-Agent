using System;
using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using Microsoft.VisualStudio;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Shell.Interop;
using Task = System.Threading.Tasks.Task;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// Read-only, process-scoped capability probe. Do not share the build pipe
    /// parser or expose arbitrary shell commands. No reload, save or deletion.
    /// </summary>
    internal sealed class XaeHmiHierarchyPipeService
    {
        private readonly TwinCATAgentPackage _package;
        private readonly int _pid = System.Diagnostics.Process.GetCurrentProcess().Id;

        internal XaeHmiHierarchyPipeService(TwinCATAgentPackage package) { _package = package; }

        internal Task RunAsync(CancellationToken cancellationToken)
        {
            return Task.Run(async () => {
                var security = new PipeSecurity();
                security.SetAccessRuleProtection(true, false);
                security.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User,
                    PipeAccessRights.ReadWrite, AccessControlType.Allow));
                while (!cancellationToken.IsCancellationRequested)
                {
                    try
                    {
                        using (var pipe = new NamedPipeServerStream("TwinCAT-Agent-HmiHierarchy-" + _pid,
                            PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous,
                            4096, 4096, security))
                        {
                            await pipe.WaitForConnectionAsync(cancellationToken);
                            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
                            {
                                deadline.CancelAfter(TimeSpan.FromSeconds(15));
                                using (deadline.Token.Register(() => pipe.Dispose()))
                                    await ServeAsync(pipe, deadline.Token);
                            }
                        }
                    }
                    catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { return; }
                    catch
                    {
                        // Avoid a tight loop if the endpoint cannot be created.
                        if (cancellationToken.IsCancellationRequested) return;
                        await Task.Delay(250, cancellationToken);
                    }
                }
            }, cancellationToken);
        }

        private async Task ServeAsync(NamedPipeServerStream pipe, CancellationToken cancellationToken)
        {
            object response;
            try
            {
                string json;
                using (var reader = new StreamReader(pipe, new UTF8Encoding(false, true), false, 1024, true))
                {
                    var text = new StringBuilder();
                    var character = new char[1];
                    while (true)
                    {
                        if (await reader.ReadAsync(character, 0, 1) == 0)
                            throw new ArgumentException("Incomplete request.");
                        if (character[0] == '\n') break;
                        text.Append(character[0]);
                        if (text.Length > HmiHierarchyProbeContract.MaxRequestChars)
                            throw new ArgumentException("Request too large.");
                    }
                    json = text.ToString();
                }
                var request = HmiHierarchyProbeContract.Parse(json);
                response = await _package.JoinableTaskFactory.RunAsync(async () => {
                    await _package.JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
                    var solution = await _package.GetVsSolutionAsync(cancellationToken);
                    return Probe(solution, request);
                });
            }
            catch (Exception ex)
            {
                response = new { ok = false, status = "unavailable", written = false,
                    verified = false, reload_performed = false, xae_pid = _pid, error = ex.Message };
            }
            using (var writer = new StreamWriter(pipe, new UTF8Encoding(false), 1024, true))
            {
                await writer.WriteLineAsync(new JavaScriptSerializer().Serialize(response));
                await writer.FlushAsync();
            }
        }

        private object Probe(IVsSolution solution, HmiHierarchyProbeContract request)
        {
            ThreadHelper.ThrowIfNotOnUIThread();
            if (solution == null) throw new InvalidOperationException("SVsSolution is unavailable.");
            string directory, solutionFile, options;
            ErrorHandler.ThrowOnFailure(solution.GetSolutionInfo(out directory, out solutionFile, out options));
            if (!SamePath(solutionFile, request.SolutionFile))
                throw new InvalidOperationException("Current solution does not match the requested solution.");
            Guid filter = Guid.Empty;
            IEnumHierarchies enumerator;
            ErrorHandler.ThrowOnFailure(solution.GetProjectEnum((uint)__VSENUMPROJFLAGS.EPF_LOADEDINSOLUTION,
                ref filter, out enumerator));
            var row = new IVsHierarchy[1];
            uint fetched;
            IVsHierarchy match = null;
            int hr;
            while ((hr = enumerator.Next(1, row, out fetched)) == VSConstants.S_OK && fetched == 1)
            {
                var project = row[0] as IVsProject;
                string moniker;
                if (project == null || project.GetMkDocument(VSConstants.VSITEMID_ROOT, out moniker) < 0 ||
                    !SamePath(moniker, request.ProjectFile)) continue;
                if (match != null) throw new InvalidOperationException("Ambiguous HMI project hierarchy.");
                match = row[0];
            }
            ErrorHandler.ThrowOnFailure(hr);
            if (match == null) throw new InvalidOperationException("Exact loaded HMI hierarchy not found.");
            uint id;
            ErrorHandler.ThrowOnFailure(match.ParseCanonicalName(request.ItemFile, out id));
            if (id >= 0xFFFFFFFD) throw new InvalidOperationException("Root/selection/nil item rejected.");
            string canonical;
            ErrorHandler.ThrowOnFailure(match.GetCanonicalName(id, out canonical));
            if (!SamePath(canonical, request.ItemFile)) throw new InvalidOperationException("Item identity mismatch.");
            var handler = match as IVsHierarchyDeleteHandler3;
            if (handler == null) throw new InvalidOperationException("IVsHierarchyDeleteHandler3 is unavailable.");
            var allowed = new bool[1];
            ErrorHandler.ThrowOnFailure(handler.QueryDeleteItems(1, 1, new[] { id }, allowed));
            // Query is capability evidence only, NOT approval or successful deletion.
            return new { ok = true, status = "probed", protocol_version = 1, xae_pid = _pid,
                solution_file = solutionFile, project_file = request.ProjectFile, item_file = canonical,
                item_id = id, storage_delete_allowed = allowed[0], written = false, verified = false,
                execution_enabled = false, reload_performed = false,
                api = "IVsHierarchyDeleteHandler3.QueryDeleteItems", hierarchy_type = match.GetType().FullName };
        }

        private static bool SamePath(string left, string right)
        {
            return !string.IsNullOrWhiteSpace(left) && string.Equals(Path.GetFullPath(left).TrimEnd('\\', '/'),
                Path.GetFullPath(right).TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase);
        }
    }
}
