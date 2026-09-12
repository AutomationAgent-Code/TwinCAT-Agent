using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using EnvDTE;
using EnvDTE80;
using Microsoft.VisualStudio.Shell;
using Task = System.Threading.Tasks.Task;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// Serves small, local build requests for the XAE process that loaded this
    /// package.  DTE build APIs must run on the shell UI thread; moving that
    /// hop into the VSIX avoids the foreground-window dependency of external
    /// Automation clients.
    /// </summary>
    internal sealed class XaeBuildPipeService
    {
        private const string PipePrefix = "TwinCAT-Agent-Build-";
        private readonly TwinCATAgentPackage _package;
        private readonly string _pipeName;

        internal XaeBuildPipeService(TwinCATAgentPackage package)
        {
            _package = package ?? throw new ArgumentNullException(nameof(package));
            _pipeName = PipePrefix + System.Diagnostics.Process.GetCurrentProcess().Id;
        }

        internal Task RunAsync(CancellationToken cancellationToken)
        {
            return Task.Run(async () =>
            {
                while (!cancellationToken.IsCancellationRequested)
                {
                    try
                    {
                        using (var pipe = new NamedPipeServerStream(
                            _pipeName, PipeDirection.InOut, 1,
                            PipeTransmissionMode.Byte, PipeOptions.Asynchronous))
                        {
                            await pipe.WaitForConnectionAsync(cancellationToken);
                            await ServeOneRequestAsync(pipe, cancellationToken);
                        }
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }
                    catch
                    {
                        // The next loop creates a fresh server.  A malformed
                        // local client must never take down the XAE package.
                    }
                }
            }, cancellationToken);
        }

        private async Task ServeOneRequestAsync(
            NamedPipeServerStream pipe, CancellationToken cancellationToken)
        {
            string request;
            using (var reader = new StreamReader(pipe, Encoding.UTF8, false, 256, true))
            {
                request = await reader.ReadLineAsync();
            }

            string response;
            bool diagnosticsOnly = request != null &&
                request.IndexOf("\"command\":\"diagnostics\"", StringComparison.Ordinal) >= 0;
            if (string.IsNullOrWhiteSpace(request) ||
                request.Length > 128 ||
                (!diagnosticsOnly && request.IndexOf("\"command\":\"build\"", StringComparison.Ordinal) < 0))
            {
                response = "{\"ok\":false,\"error\":\"unsupported request\"}";
            }
            else
            {
                response = BuildOnUiThread(cancellationToken, !diagnosticsOnly);
            }

            using (var writer = new StreamWriter(pipe, new UTF8Encoding(false), 256, true))
            {
                await writer.WriteLineAsync(response);
                await writer.FlushAsync();
            }
        }

        private string BuildOnUiThread(CancellationToken cancellationToken, bool executeBuild = true)
        {
            try
            {
                string solution = "";
                string diagnostics = _package.JoinableTaskFactory.Run(async delegate
                {
                    await _package.JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
                    var dte = await _package.GetDteAsync(cancellationToken);
                    if (dte == null || dte.Solution == null || !dte.Solution.IsOpen)
                        throw new InvalidOperationException("No open TwinCAT solution.");

                    var build = dte.Solution.SolutionBuild;
                    solution = dte.Solution.FullName;
                    if (executeBuild) build.Build(true);
                    int? failedProjects = null;
                    try { failedProjects = build.LastBuildInfo; }
                    catch when (!executeBuild) { /* No build yet: still read the Error List. */ }
                    return ReadDiagnostics(dte, failedProjects);
                });
                return "{\"ok\":true,\"solution\":" + JsonString(solution) +
                    ",\"buildPerformed\":" + (executeBuild ? "true" : "false") + "," + diagnostics + "}";
            }
            catch (Exception ex)
            {
                return "{\"ok\":false,\"error\":" + JsonString(ex.Message) + "}";
            }
        }

        private static string ReadDiagnostics(DTE dte, int? failedProjects)
        {
            // SolutionBuild.Build(true) waits for the compiler, while the
            // ErrorItems collection is refreshed by a separate shell update.
            // Retry briefly on the UI thread so a valid compiler failure is
            // not returned as an empty list due to that small race.
            for (int attempt = 1; attempt <= 5; attempt++)
            {
                try
                {
                    var dte2 = dte as DTE2;
                    if (dte2 == null)
                        throw new InvalidOperationException("DTE2 ErrorItems is unavailable.");
                    var items = dte2.ToolWindows.ErrorList.ErrorItems;
                    var errors = new StringBuilder();
                    var warnings = new StringBuilder();
                    int errorCount = 0;
                    int warningCount = 0;
                    for (int index = 1; index <= items.Count; index++)
                    {
                        var item = items.Item(index);
                        int level = Convert.ToInt32(item.ErrorLevel);
                        bool isError = IsBuildError(item, failedProjects ?? 0, level);
                        string payload = ErrorItemJson(item, isError);
                        if (isError)
                        {
                            AppendJsonItem(errors, payload);
                            errorCount++;
                        }
                        else
                        {
                            AppendJsonItem(warnings, payload);
                            warningCount++;
                        }
                    }

                    // Do not stop at an empty/medium-only snapshot after a
                    // failed PLC build; the compiler diagnostics may arrive
                    // on the next shell update.
                    if (failedProjects > 0 && errorCount == 0 && attempt < 5)
                    {
                        System.Threading.Thread.Sleep(120 * attempt);
                        continue;
                    }
                    return "\"failedProjects\":" + (failedProjects.HasValue ? failedProjects.Value.ToString() : "null") +
                        ",\"errorCount\":" + errorCount +
                        ",\"warningCount\":" + warningCount +
                        ",\"errors\":[" + errors + "]" +
                        ",\"warnings\":[" + warnings + "]" +
                        ",\"errorsRead\":true,\"diagnosticsAvailable\":true" +
                        ",\"errorSource\":\"dte-error-items-ui-thread\"" +
                        ",\"errorReadAttempts\":" + attempt +
                        ",\"diagnosticsPending\":" + ((failedProjects > 0 && errorCount == 0) ? "true" : "false") +
                        ((failedProjects > 0 && errorCount == 0)
                            ? ",\"message\":\"Build failed, but no compiler error was exposed after 5 UI-thread reads; diagnosticsPending=true.\""
                            : "");
                }
                catch
                {
                    if (attempt < 5)
                    {
                        System.Threading.Thread.Sleep(120 * attempt);
                        continue;
                    }
                }
            }
            return "\"failedProjects\":" + (failedProjects.HasValue ? failedProjects.Value.ToString() : "null") +
                ",\"errorCount\":null,\"warningCount\":null" +
                ",\"errors\":[],\"warnings\":[]" +
                ",\"errorsRead\":false,\"diagnosticsAvailable\":false" +
                ",\"errorSource\":\"unavailable-no-focus\"" +
                ",\"errorReadAttempts\":5" +
                ",\"diagnosticsPending\":true" +
                ",\"message\":\"Build status is separate from diagnostics; XAE ErrorItems unavailable after 5 UI-thread reads.\"";
        }

        private static bool IsBuildError(EnvDTE80.ErrorItem item, int failedProjects, int level)
        {
            if (level >= 4)
                return true;
            if (failedProjects <= 0)
                return false;

            // TcXaeShell 15 sometimes reports PLC compiler errors as
            // ErrorLevel=Medium.  A project-scoped compiler diagnostic is
            // still an error when SolutionBuild reports a failed project.
            string project = item.Project ?? string.Empty;
            string file = item.FileName ?? string.Empty;
            string description = item.Description ?? string.Empty;
            // A failed build does not turn every project warning into an error.
            // Promote only a recognizable compiler error code; retain raw level.
            return !string.IsNullOrWhiteSpace(file) && item.Line > 0 &&
                Regex.IsMatch(description,
                    @"^(?:(?:'.*?'|Expression|Identifier|Type) expected\b|Unexpected token\b|[CT]\d{4}\b|VAR_TEMP declaration not allowed in this place\s*\.?\s*$)",
                    RegexOptions.IgnoreCase);
        }

        private static string ErrorItemJson(EnvDTE80.ErrorItem item, bool isError)
        {
            return "{\"severity\":" + JsonString(isError ? "error" : "warning") +
                ",\"raw_error_level\":" + Convert.ToInt32(item.ErrorLevel) +
                ",\"severity_inferred\":" + ((isError && Convert.ToInt32(item.ErrorLevel) < 4) ? "true" : "false") +
                // ErrorItem in the TcXaeShell 15 type library does not
                // expose ErrorCode; the compiler code remains in Description.
                ",\"code\":\"\"" +
                ",\"description\":" + JsonString(item.Description) +
                ",\"project\":" + JsonString(item.Project) +
                ",\"file\":" + JsonString(item.FileName) +
                ",\"line\":" + item.Line + "}";
        }

        private static void AppendJsonItem(StringBuilder output, string item)
        {
            if (output.Length > 0)
                output.Append(',');
            output.Append(item);
        }

        private static string JsonString(string value)
        {
            return "\"" + (value ?? string.Empty)
                .Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\r", "\\r")
                .Replace("\n", "\\n") + "\"";
        }
    }
}
