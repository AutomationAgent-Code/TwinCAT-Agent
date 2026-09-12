using System.Runtime.InteropServices;
using Microsoft.VisualStudio.Shell;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// The dockable tool window that hosts the TwinCAT Agent chat UI.
    /// v0 hosts a lightweight WPF control (open-panel button); v1 will embed a
    /// WebView2 that loads tc_agent/static/index.html directly.
    /// </summary>
    [Guid("a1c2e3f4-5b6d-4e7f-8a9b-0c1d2e3f4a5b")]
    public sealed class TwinCATAgentToolWindow : ToolWindowPane
    {
        public TwinCATAgentToolWindow() : base(null)
        {
            this.Caption = "TwinCAT Agent";
            this.Content = new TwinCATAgentChatControl();
        }
    }
}
