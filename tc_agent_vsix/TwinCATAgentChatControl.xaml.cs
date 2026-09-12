using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// Hosts the TwinCAT Agent chat UI in an embedded WebView2.
    /// </summary>
    public partial class TwinCATAgentChatControl : UserControl
    {
        private static readonly object LogSync = new object();
        private bool _initStarted;
        private readonly DispatcherTimer _restoreTimer;
        private bool _sourceHandlerAttached;
        private Window _hostWindow;
        private readonly uint _processId;
        private bool _isLoaded;

        public TwinCATAgentChatControl()
        {
            InitializeComponent();
            _processId = unchecked((uint)Process.GetCurrentProcess().Id);
            _restoreTimer = new DispatcherTimer(
                TimeSpan.FromMilliseconds(280),
                DispatcherPriority.ApplicationIdle,
                OnRestoreTimer,
                Dispatcher);
            _restoreTimer.Stop();
            Loaded += OnLoaded;
            Unloaded += OnUnloaded;
            IsVisibleChanged += OnVisibilityChanged;
            SizeChanged += OnControlSizeChanged;
            ShellInteractionHub.EventPublished += OnShellEventPublished;
        }

        private async void OnLoaded(object sender, RoutedEventArgs e)
        {
            _isLoaded = true;
            AttachSourceHandler();
            AttachHostWindow();
            SafeLog("panel loaded");
            if (_initStarted)
            {
                await RecreateWebViewAfterDockMoveAsync();
                return;
            }
            _initStarted = true;
            await InitializeWebViewAsync();
        }

        private async System.Threading.Tasks.Task RecreateWebViewAfterDockMoveAsync()
        {
            try
            {
                SafeLog("WebView2 recreating after dock move");
                var old = Web;
                if (old != null)
                {
                    if (old.CoreWebView2 != null)
                    {
                        old.CoreWebView2.ProcessFailed -= OnProcessFailed;
                        old.CoreWebView2.NavigationCompleted -= OnNavigationCompleted;
                        old.CoreWebView2.WebMessageReceived -= OnWebMessageReceived;
                    }
                    Root.Children.Remove(old);
                    old.Dispose();
                }
                Web = new WebView2 { Visibility = Visibility.Collapsed };
                Root.Children.Insert(0, Web);
                await InitializeWebViewAsync();
            }
            catch (Exception ex)
            {
                _initStarted = false;
                SafeLog("WebView2 dock recovery failed: " + ex);
                ShowFallback(
                    "移动窗口后恢复内嵌浏览器失败。\n\n错误：" + ex.Message);
            }
        }

        private async System.Threading.Tasks.Task InitializeWebViewAsync()
        {
            try
            {
                SafeLog("WebView2 initialization started");
                string udf = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "TcAgent", "WebView2");
                Directory.CreateDirectory(udf);

                var env = await CoreWebView2Environment.CreateAsync(null, udf);
                if (!_isLoaded) return;
                await Web.EnsureCoreWebView2Async(env);
                if (!_isLoaded) return;
                Web.CoreWebView2.ProcessFailed += OnProcessFailed;
                Web.CoreWebView2.NavigationCompleted += OnNavigationCompleted;
                Web.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
                SafeLog("WebView2 initialized: " +
                    Web.CoreWebView2.Environment.BrowserVersionString);

                // Load through the localhost HTTP server so WebSocket Origin
                // is http://127.0.0.1:8766.  Loading the bundled HTML via
                // file:// produces Origin:null, which the hardened backend
                // correctly rejects and leaves the license page disconnected.
                string page = "http://127.0.0.1:8766/?xae_pid="
                    + _processId.ToString();
                Web.CoreWebView2.Navigate(page);
            }
            catch (Exception ex)
            {
                _initStarted = false;
                SafeLog("WebView2 initialization failed: " + ex);
                ShowFallback(
                    "无法初始化内嵌浏览器 (WebView2)。\n\n" +
                    "请确认已安装 WebView2 Runtime。\n\n" +
                    "错误：" + ex.Message);
            }
        }

        private void OnUnloaded(object sender, RoutedEventArgs e)
        {
            _isLoaded = false;
            _restoreTimer.Stop();
            DetachHostWindow();
            DetachSourceHandler();
            SafeLog("panel unloaded");
        }

        private void OnVisibilityChanged(object sender, DependencyPropertyChangedEventArgs e)
        {
            if (!IsVisible) return;
            AttachHostWindow();
            ScheduleRestore();
        }

        private void OnControlSizeChanged(object sender, SizeChangedEventArgs e)
        {
            ScheduleRestore();
        }

        private void AttachHostWindow()
        {
            Window host = Window.GetWindow(this);
            if (ReferenceEquals(host, _hostWindow)) return;
            DetachHostWindow();
            _hostWindow = host;
            if (_hostWindow == null) return;
            _hostWindow.LocationChanged += OnHostVisualChanged;
            _hostWindow.SizeChanged += OnHostVisualChanged;
            _hostWindow.StateChanged += OnHostVisualChanged;
        }

        private void DetachHostWindow()
        {
            if (_hostWindow == null) return;
            _hostWindow.LocationChanged -= OnHostVisualChanged;
            _hostWindow.SizeChanged -= OnHostVisualChanged;
            _hostWindow.StateChanged -= OnHostVisualChanged;
            _hostWindow = null;
        }

        private void OnHostVisualChanged(object sender, EventArgs e)
        {
            ScheduleRestore();
        }

        private void OnPresentationSourceChanged(object sender, SourceChangedEventArgs e)
        {
            Dispatcher.BeginInvoke(new Action(() =>
            {
                if (!_isLoaded) return;
                AttachHostWindow();
                ScheduleRestore();
            }), DispatcherPriority.Loaded);
        }

        private void AttachSourceHandler()
        {
            if (_sourceHandlerAttached) return;
            PresentationSource.AddSourceChangedHandler(this, OnPresentationSourceChanged);
            _sourceHandlerAttached = true;
        }

        private void DetachSourceHandler()
        {
            if (!_sourceHandlerAttached) return;
            PresentationSource.RemoveSourceChangedHandler(this, OnPresentationSourceChanged);
            _sourceHandlerAttached = false;
        }

        private void ScheduleRestore()
        {
            if (!_isLoaded) return;
            _restoreTimer.Stop();
            _restoreTimer.Start();
        }

        private void OnRestoreTimer(object sender, EventArgs e)
        {
            _restoreTimer.Stop();
            RestoreAfterMove();
        }

        private void RestoreAfterMove()
        {
            if (!_isLoaded || !IsVisible || Web == null || Web.CoreWebView2 == null)
                return;
            try
            {
                // WebView2.UpdateWindowPos is the public WPF-host API.  Call it
                // once after layout settles; do not use native global hooks,
                // private controller reflection, or duplicate native position
                // notifications while VS is reparenting the tool window.
                Web.UpdateWindowPos();
                Web.InvalidateMeasure();
                Web.InvalidateVisual();
                Web.UpdateLayout();
            }
            catch (Exception ex)
            {
                SafeLog("WebView2 position recovery failed: " + ex.Message);
            }
        }

        private void OnNavigationCompleted(
            object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess)
            {
                Fallback.Visibility = Visibility.Collapsed;
                Web.Visibility = Visibility.Visible;
                SafeLog("navigation completed");
                PostShellEvent(ShellInteractionHub.LatestEventJson);
                ScheduleRestore();
            }
            else
            {
                ShowFallback("聊天界面加载失败：" + e.WebErrorStatus);
            }
        }

        private void OnShellEventPublished(string json)
        {
            Dispatcher.BeginInvoke(new Action(() => PostShellEvent(json)), DispatcherPriority.Background);
        }

        private void PostShellEvent(string json)
        {
            try
            {
                if (_isLoaded && Web != null && Web.CoreWebView2 != null && !string.IsNullOrWhiteSpace(json))
                    Web.CoreWebView2.PostWebMessageAsJson(json);
            }
            catch (Exception ex)
            {
                SafeLog("Shell event delivery failed: " + ex.Message);
            }
        }

        private void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(e.Source) || !e.Source.StartsWith("http://127.0.0.1:8766/", StringComparison.OrdinalIgnoreCase)) return;
                var message = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(e.TryGetWebMessageAsString());
                if (message == null || !message.TryGetValue("type", out object type)) return;
                if (string.Equals(type as string, "xae_open", StringComparison.Ordinal))
                {
                    if (!message.TryGetValue("path", out object rawPath) || string.IsNullOrWhiteSpace(rawPath as string)) return;
                    int line = 0;
                    if (message.TryGetValue("line", out object rawLine)) int.TryParse(Convert.ToString(rawLine), out line);
                    ShellInteractionHub.RequestOpen(rawPath as string, line);
                }
                else if (string.Equals(type as string, "xae_open_directory", StringComparison.Ordinal))
                {
                    if (!message.TryGetValue("path", out object rawPath) || string.IsNullOrWhiteSpace(rawPath as string)) return;
                    ShellInteractionHub.RequestOpenDirectory(rawPath as string);
                }
                else if (string.Equals(type as string, "agent_update_download", StringComparison.Ordinal)
                         && message.TryGetValue("url", out object rawUrl))
                {
                    OpenUpdateDownload(rawUrl as string);
                }
            }
            catch (Exception ex)
            {
                SafeLog("Shell command rejected: " + ex.Message);
            }
        }

        private static void OpenUpdateDownload(string value)
        {
            Uri uri;
            if (!Uri.TryCreate(value, UriKind.Absolute, out uri)
                || !string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase)
                || !string.Equals(uri.Host, "github.com", StringComparison.OrdinalIgnoreCase)
                || !uri.AbsolutePath.StartsWith("/AutomationAgent-Code/TwinCAT-Agent/releases/download/", StringComparison.Ordinal)
                || !uri.AbsolutePath.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            {
                SafeLog("Rejected update download URL.");
                return;
            }
            Process.Start(uri.AbsoluteUri);
        }

        private void OnProcessFailed(object sender, CoreWebView2ProcessFailedEventArgs e)
        {
            SafeLog("WebView2 process failed: " + e.ProcessFailedKind);
            Dispatcher.BeginInvoke(new Action(() =>
            {
                try
                {
                    if (_isLoaded && Web != null && Web.CoreWebView2 != null)
                    {
                        Web.Reload();
                        ScheduleRestore();
                    }
                }
                catch (Exception ex)
                {
                    ShowFallback("浏览器渲染进程恢复失败：" + ex.Message);
                }
            }), DispatcherPriority.ApplicationIdle);
        }

        private void ShowFallback(string text)
        {
            Web.Visibility = Visibility.Collapsed;
            Fallback.Text = text;
            Fallback.Visibility = Visibility.Visible;
        }

        private static void SafeLog(string message)
        {
            try
            {
                string directory = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "TcAgent");
                Directory.CreateDirectory(directory);
                string line = DateTime.Now.ToString("O") + " [VSIX] " + message +
                    Environment.NewLine;
                lock (LogSync)
                {
                    File.AppendAllText(Path.Combine(directory, "vsix.log"), line);
                }
            }
            catch
            {
                // Diagnostics must never affect the host IDE.
            }
        }
    }
}
