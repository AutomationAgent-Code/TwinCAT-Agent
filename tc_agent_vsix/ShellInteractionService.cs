using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Threading;
using EnvDTE;
using Microsoft.VisualStudio.Shell;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// One small, process-local bridge between the XAE shell and every embedded
    /// Agent WebView.  It deliberately exposes state, rather than attempting to
    /// mirror the editor: the Agent can then use its normal PLC tools to read the
    /// authoritative declaration/implementation from XAE.
    /// </summary>
    internal static class ShellInteractionHub
    {
        private static readonly object Sync = new object();
        private static string _latestEventJson;

        internal static event Action<string> EventPublished;
        internal static Action<string, int> OpenRequested;
        internal static Action<string> OpenDirectoryRequested;

        internal static string LatestEventJson
        {
            get { lock (Sync) return _latestEventJson; }
        }

        internal static void Publish(object value, bool rememberDocument = false)
        {
            var json = new JavaScriptSerializer().Serialize(value);
            Action<string> subscribers;
            lock (Sync)
            {
                // Reopening the panel must replay editor context, not an old
                // failed command or an intermediate build notification.
                if (rememberDocument) _latestEventJson = json;
                subscribers = EventPublished;
            }
            if (subscribers != null) subscribers(json);
        }

        internal static void RequestOpen(string path, int line)
        {
            Action<string, int> handler;
            lock (Sync) handler = OpenRequested;
            if (handler != null) handler(path, line);
            else Publish(new { type = "xae_command", ok = false, command = "open", path = path,
                error = "XAE 编辑器服务尚未就绪，请等待工程加载完成后重试。" });
        }

        internal static void RequestOpenDirectory(string path)
        {
            Action<string> handler;
            lock (Sync) handler = OpenDirectoryRequested;
            if (handler != null) handler(path);
            else Publish(new { type = "xae_command", ok = false, command = "open_directory", path = path,
                error = "XAE 编辑器服务尚未就绪，请等待工程加载完成后重试。" });
        }
    }

    /// <summary>Publishes active-document and build lifecycle state from XAE.</summary>
    internal sealed class ShellInteractionService : IDisposable
    {
        private readonly TwinCATAgentPackage _package;
        private DTE _dte;
        private WindowEvents _windowEvents;
        private DocumentEvents _documentEvents;
        private TextEditorEvents _textEditorEvents;
        private BuildEvents _buildEvents;
        private _dispWindowEvents_WindowActivatedEventHandler _windowActivated;
        private _dispDocumentEvents_DocumentSavedEventHandler _documentSaved;
        private _dispTextEditorEvents_LineChangedEventHandler _lineChanged;
        private _dispBuildEvents_OnBuildBeginEventHandler _buildBegin;
        private _dispBuildEvents_OnBuildDoneEventHandler _buildDone;
        private DispatcherTimer _editDebounceTimer;
        private bool _disposed;

        internal ShellInteractionService(TwinCATAgentPackage package) { _package = package; }

        internal async System.Threading.Tasks.Task StartAsync(CancellationToken cancellationToken)
        {
            _dte = await _package.GetDteAsync(cancellationToken);
            if (_dte == null) return;

            _windowEvents = _dte.Events.WindowEvents;
            _documentEvents = _dte.Events.DocumentEvents;
            _buildEvents = _dte.Events.BuildEvents;
            _windowActivated = OnWindowActivated;
            _documentSaved = OnDocumentSaved;
            _buildBegin = OnBuildBegin;
            _buildDone = OnBuildDone;
            _windowEvents.WindowActivated += _windowActivated;
            _documentEvents.DocumentSaved += _documentSaved;
            _buildEvents.OnBuildBegin += _buildBegin;
            _buildEvents.OnBuildDone += _buildDone;
            _editDebounceTimer = new DispatcherTimer(DispatcherPriority.Background)
            {
                Interval = TimeSpan.FromMilliseconds(700)
            };
            _editDebounceTimer.Tick += OnEditDebounceTick;
            ShellInteractionHub.OpenRequested = OpenInEditor;
            ShellInteractionHub.OpenDirectoryRequested = OpenSnapshotDirectory;
            cancellationToken.Register(Dispose);
            AttachTextEditorEvents();
            Trace("shell interaction service started");
            PublishCurrentDocument("initial");
            // The XAE editor may finish restoring its active document only after
            // package initialization.  Retry once on the UI dispatcher so that a
            // startup race cannot leave the cache empty until the next click.
            _editDebounceTimer.Interval = TimeSpan.FromSeconds(2);
            _editDebounceTimer.Start();
        }

        private void OnWindowActivated(Window gainedFocus, Window lostFocus)
        {
            AttachTextEditorEvents();
            PublishCurrentDocument("activated");
        }

        private void OnDocumentSaved(Document document)
        {
            if (_editDebounceTimer != null) _editDebounceTimer.Stop();
            PublishCurrentDocument("saved");
        }

        private void AttachTextEditorEvents()
        {
            try
            {
                TextDocument text = TryGetTextDocument(_dte.ActiveDocument);
                if (text == null) return;
                if (_textEditorEvents != null && _lineChanged != null)
                    _textEditorEvents.LineChanged -= _lineChanged;
                _textEditorEvents = _dte.Events.TextEditorEvents[text];
                _lineChanged = OnLineChanged;
                _textEditorEvents.LineChanged += _lineChanged;
            }
            catch
            {
                // Some TwinCAT designers do not expose a TextDocument.  The
                // regular activation/save cache path must remain available.
                _textEditorEvents = null;
                _lineChanged = null;
            }
        }

        private void OnLineChanged(TextPoint startPoint, TextPoint endPoint, int hint)
        {
            if (_disposed || !IsActivePlcDocument()) return;
            _editDebounceTimer.Stop();
            _editDebounceTimer.Start();
        }

        private void OnEditDebounceTick(object sender, EventArgs e)
        {
            _editDebounceTimer.Stop();
            _editDebounceTimer.Interval = TimeSpan.FromMilliseconds(700);
            PublishCurrentDocument("edited_or_startup_retry");
        }

        private void OnBuildBegin(vsBuildScope scope, vsBuildAction action)
        {
            ShellInteractionHub.Publish(new { type = "xae_build", phase = "started" });
        }

        private void OnBuildDone(vsBuildScope scope, vsBuildAction action)
        {
            ShellInteractionHub.Publish(new { type = "xae_build", phase = "completed" });
            PublishCurrentDocument("build_completed");
        }

        private void PublishCurrentDocument(string reason)
        {
            if (_disposed || _dte == null) return;
            try
            {
                Document document = _dte.ActiveDocument;
                string fullName = document == null ? "" : (document.FullName ?? "");
                string displayName = document == null ? "" : (document.Name ?? "");
                string sourcePath = fullName;
                string member = "";
                int at = fullName.IndexOf('@');
                if (at >= 0)
                {
                    sourcePath = fullName.Substring(0, at);
                    member = fullName.Substring(at + 1);
                }
                bool isPlc = sourcePath.EndsWith(".TcPOU", StringComparison.OrdinalIgnoreCase)
                    || sourcePath.EndsWith(".TcGVL", StringComparison.OrdinalIgnoreCase)
                    || sourcePath.EndsWith(".TcDUT", StringComparison.OrdinalIgnoreCase);
                string cacheContent = isPlc ? TryReadDocumentText(document) : "";
                ShellInteractionHub.Publish(new
                {
                    type = "xae_document",
                    reason = reason,
                    name = displayName,
                    path = sourcePath,
                    member = member,
                    saved = document == null || document.Saved,
                    is_plc = isPlc,
                    cache_content = cacheContent
                }, rememberDocument: true);
                if (isPlc && !string.IsNullOrWhiteSpace(sourcePath))
                {
                    Trace("cache push queued: " + reason + " " + sourcePath + "@" + member);
                    PushCacheInvalidation(sourcePath, member, document == null || document.Saved,
                        _dte.Solution.FullName ?? "");
                }
            }
            catch (Exception ex)
            {
                ShellInteractionHub.Publish(new { type = "xae_document", reason = reason, error = ex.Message });
            }
        }

        private static TextDocument TryGetTextDocument(Document document)
        {
            if (document == null) return null;
            try
            {
                return document.Object("TextDocument") as TextDocument;
            }
            catch { return null; }
        }

        private static string TryReadDocumentText(Document document)
        {
            try
            {
                TextDocument text = TryGetTextDocument(document);
                return text == null ? "" : (text.StartPoint.CreateEditPoint().GetText(text.EndPoint) ?? "");
            }
            catch { return ""; }
        }

        private bool IsActivePlcDocument()
        {
            try
            {
                string fullName = _dte.ActiveDocument == null ? "" : (_dte.ActiveDocument.FullName ?? "");
                int at = fullName.IndexOf('@');
                if (at >= 0) fullName = fullName.Substring(0, at);
                return fullName.EndsWith(".TcPOU", StringComparison.OrdinalIgnoreCase)
                    || fullName.EndsWith(".TcGVL", StringComparison.OrdinalIgnoreCase)
                    || fullName.EndsWith(".TcDUT", StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        private static void PushCacheInvalidation(string path, string member, bool saved, string solution)
        {
            System.Threading.Tasks.Task.Run(() =>
            {
                try
                {
                    string body = new JavaScriptSerializer().Serialize(new { path = path, member = member,
                        saved = saved, solution = solution, xae_pid = System.Diagnostics.Process.GetCurrentProcess().Id });
                    using (var client = new WebClient())
                    {
                        client.Headers[HttpRequestHeader.ContentType] = "application/json; charset=utf-8";
                        string result = client.UploadString("http://127.0.0.1:8766/__plc_cache", "POST", body);
                        Trace("cache push succeeded: " + path + "@" + member + " " + result);
                    }
                }
                catch (Exception ex)
                {
                    Trace("cache push failed: " + path + "@" + member + " " + ex.Message);
                }
            });
        }

        private static void Trace(string message)
        {
            try
            {
                string directory = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TcAgent");
                Directory.CreateDirectory(directory);
                File.AppendAllText(Path.Combine(directory, "vsix.log"),
                    DateTime.Now.ToString("O") + " [ShellInteraction] " + message + Environment.NewLine);
            }
            catch { }
        }

        internal void OpenInEditor(string requestedPath, int line)
        {
            string path = requestedPath ?? "";
            try
            {
                if (_disposed || _dte == null) throw new InvalidOperationException("XAE 编辑器服务尚未就绪。");
                if (string.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path) || path.Contains("^"))
                    throw new InvalidOperationException("需要源文件的绝对路径，不能使用 PLC 树路径或对象名。");
                path = Path.GetFullPath(path);
                if (!IsAllowedSolutionFile(path)) throw new InvalidOperationException("只能在当前解决方案内打开 PLC 源文件。");
                Window window = _dte.ItemOperations.OpenFile(path);
                // TwinCAT designers can return no EnvDTE.Window after opening.
                // Verify the resulting document instead of treating null as failure.
                Document document = _dte.ActiveDocument;
                string openedPath = document == null ? "" : (document.FullName ?? "");
                if (window != null) window.Activate();
                if (window == null && !string.Equals(openedPath, path, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("XAE 未返回编辑窗口，且当前文档与请求文件不一致。");
                string warning = "";
                // A PLC designer is not necessarily an EnvDTE text editor.
                // Unsupported line navigation must not turn a successful open into an error.
                if (line > 0)
                {
                    try
                    {
                        var selection = _dte.ActiveDocument == null ? null : _dte.ActiveDocument.Selection as TextSelection;
                        if (selection != null) selection.GotoLine(Math.Min(line, 1000000), true);
                        else warning = "文件已打开；当前 TwinCAT 编辑器不支持按文本行定位。";
                    }
                    catch (Exception ex) { warning = "文件已打开，但无法定位行号：" + ex.Message; }
                }
                Trace("open succeeded: " + path + (warning.Length == 0 ? "" : " | " + warning));
                ShellInteractionHub.Publish(new { type = "xae_command", ok = true, command = "open", path = path, line = line, warning = warning });
            }
            catch (Exception ex)
            {
                Trace("open failed: " + path + " | " + ex);
                ShellInteractionHub.Publish(new { type = "xae_command", ok = false, command = "open", path = path, error = ex.Message });
            }
        }

        internal void OpenSnapshotDirectory(string requestedPath)
        {
            string path = requestedPath ?? "";
            try
            {
                if (_disposed || _dte == null) throw new InvalidOperationException("XAE 编辑器服务尚未就绪。");
                if (string.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path) || path.Contains("^"))
                    throw new InvalidOperationException("需要当前解决方案内的绝对快照目录路径。");
                path = Path.GetFullPath(path);
                if (!IsAllowedSnapshotDirectory(path))
                    throw new InvalidOperationException("只能打开当前解决方案旁经后端核实的 PLC 快照目录。");
                System.Diagnostics.Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
                Trace("snapshot directory opened: " + path);
                ShellInteractionHub.Publish(new { type = "xae_command", ok = true,
                    command = "open_directory", path = path });
            }
            catch (Exception ex)
            {
                Trace("snapshot directory open failed: " + path + " | " + ex);
                ShellInteractionHub.Publish(new { type = "xae_command", ok = false,
                    command = "open_directory", path = path, error = ex.Message });
            }
        }

        private bool IsAllowedSolutionFile(string path)
        {
            string extension = Path.GetExtension(path);
            string[] allowed = { ".TcPOU", ".TcGVL", ".TcDUT", ".TcIO", ".tsproj" };
            if (!allowed.Any(value => string.Equals(value, extension, StringComparison.OrdinalIgnoreCase))) return false;
            string solution = _dte.Solution == null ? "" : (_dte.Solution.FullName ?? "");
            if (string.IsNullOrWhiteSpace(solution)) return false;
            string root = Path.GetDirectoryName(solution);
            if (string.IsNullOrWhiteSpace(root)) return false;
            root = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            return path.StartsWith(root, StringComparison.OrdinalIgnoreCase) && File.Exists(path);
        }

        private bool IsAllowedSnapshotDirectory(string path)
        {
            string solution = _dte.Solution == null ? "" : (_dte.Solution.FullName ?? "");
            if (string.IsNullOrWhiteSpace(solution)) return false;
            string root = Path.GetDirectoryName(solution);
            if (string.IsNullOrWhiteSpace(root)) return false;
            string expected = Path.GetFullPath(Path.Combine(root, ".TwinCATAgent", "snapshots"));
            string normalized = path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            expected = expected.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (!string.Equals(normalized, expected, StringComparison.OrdinalIgnoreCase)) return false;
            if (!Directory.Exists(path)) return false;
            try
            {
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return false;
                string agentRoot = Path.GetDirectoryName(path);
                return !string.IsNullOrWhiteSpace(agentRoot)
                    && (File.GetAttributes(agentRoot) & FileAttributes.ReparsePoint) == 0;
            }
            catch { return false; }
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            if (_windowEvents != null && _windowActivated != null) _windowEvents.WindowActivated -= _windowActivated;
            if (_documentEvents != null && _documentSaved != null) _documentEvents.DocumentSaved -= _documentSaved;
            if (_textEditorEvents != null && _lineChanged != null) _textEditorEvents.LineChanged -= _lineChanged;
            if (_buildEvents != null && _buildBegin != null) _buildEvents.OnBuildBegin -= _buildBegin;
            if (_buildEvents != null && _buildDone != null) _buildEvents.OnBuildDone -= _buildDone;
            if (_editDebounceTimer != null) { _editDebounceTimer.Stop(); _editDebounceTimer.Tick -= OnEditDebounceTick; }
            if (ShellInteractionHub.OpenRequested == OpenInEditor) ShellInteractionHub.OpenRequested = null;
            if (ShellInteractionHub.OpenDirectoryRequested == OpenSnapshotDirectory) ShellInteractionHub.OpenDirectoryRequested = null;
        }
    }
}
