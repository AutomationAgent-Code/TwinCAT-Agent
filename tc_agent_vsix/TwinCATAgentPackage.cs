using System;
using System.ComponentModel.Design;
using System.Runtime.InteropServices;
using System.Threading;
using EnvDTE;
using Microsoft.VisualStudio;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Shell.Interop;
using Task = System.Threading.Tasks.Task;

namespace TwinCATAgent.Xae
{
    /// <summary>
    /// Command / GUID constants shared by the package and the .vsct command table.
    /// </summary>
    internal static class PkgIds
    {
        public const string PackageGuidString = "6b6f9d1e-7c4e-4a6b-9e2d-1f0a5c8b3e21";
        public const string CmdSetGuidString  = "f0e1d2c3-b4a5-4968-8776-5a4b3c2d1e0f";
        public static readonly Guid CmdSet = new Guid(CmdSetGuidString);
        public const int ShowChatCommandId = 0x0100;
    }

    /// <summary>
    /// The TwinCAT Agent VSIX package: registers a tool window that hosts the
    /// TwinCAT Agent chat UI, and a View-menu command to open it.
    /// </summary>
    [PackageRegistration(UseManagedResourcesOnly = true, AllowsBackgroundLoading = true)]
    [InstalledProductRegistration("TwinCAT Agent", "你自己的 AI 助手面板 (Claude + tc-mcp)，嵌入 TwinCAT XAE。", "0.2.0")]
    [ProvideMenuResource("Menus.ctmenu", 1)]
    [ProvideToolWindow(typeof(TwinCATAgentToolWindow), Style = VsDockStyle.Tabbed, Orientation = ToolWindowOrientation.Right)]
    // NO ProvideAutoLoad and NO auto-show: loading this package (and showing the tool
    // window) during shell startup deadlocked the 15.0 isolated shell on 2026-07-20.
    // The View-menu item comes from the merged ctmenu resource without the package
    // being loaded; the first click loads the package on demand and opens the panel.
    [Guid(PkgIds.PackageGuidString)]
    public sealed class TwinCATAgentPackage : AsyncPackage
    {
        private Task _buildPipeTask;
        private Task _hmiHierarchyPipeTask;
        private ShellInteractionService _shellInteraction;

        internal async System.Threading.Tasks.Task<DTE> GetDteAsync(CancellationToken cancellationToken)
        {
            await this.JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
            return await GetServiceAsync(typeof(DTE)) as DTE;
        }

        internal async System.Threading.Tasks.Task<IVsSolution> GetVsSolutionAsync(CancellationToken cancellationToken)
        {
            await this.JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
            return await GetServiceAsync(typeof(SVsSolution)) as IVsSolution;
        }

        protected override async Task InitializeAsync(
            CancellationToken cancellationToken, IProgress<ServiceProgressData> progress)
        {
            await this.JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);

            if (await GetServiceAsync(typeof(IMenuCommandService)) is OleMenuCommandService mcs)
            {
                var id = new CommandID(PkgIds.CmdSet, PkgIds.ShowChatCommandId);
                mcs.AddCommand(new MenuCommand(ShowChatWindow, id));
            }

            // The listener itself stays off the UI thread; each build request
            // explicitly marshals just the DTE call back to that thread.
            _buildPipeTask = new XaeBuildPipeService(this).RunAsync(this.DisposalToken);
            _hmiHierarchyPipeTask = new XaeHmiHierarchyPipeService(this).RunAsync(this.DisposalToken);
            _shellInteraction = new ShellInteractionService(this);
            await _shellInteraction.StartAsync(this.DisposalToken);
        }


        private async void ShowChatWindow(object sender, EventArgs e)
        {
            try
            {
                await this.JoinableTaskFactory.SwitchToMainThreadAsync(this.DisposalToken);
                ToolWindowPane window = await ShowToolWindowAsync(
                    typeof(TwinCATAgentToolWindow), 0, create: true, cancellationToken: this.DisposalToken);
                if (window?.Frame == null)
                    throw new NotSupportedException("Cannot create TwinCAT Agent tool window.");

                // ShowToolWindowAsync creates the pane, but the VS 15 isolated
                // shell can leave its frame hidden on the package's first lazy
                // load. Explicitly showing the frame makes the first click
                // deterministic and also brings an existing pane to the front.
                var frame = (IVsWindowFrame)window.Frame;
                ErrorHandler.ThrowOnFailure(frame.Show());
            }
            catch (OperationCanceledException)
            {
                // Package/TcXaeShell is shutting down.
            }
            catch (Exception ex)
            {
                await this.JoinableTaskFactory.SwitchToMainThreadAsync();
                System.Windows.MessageBox.Show(
                    "无法打开 TwinCAT Agent 面板：\n" + ex.Message,
                    "TwinCAT Agent",
                    System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Error);
            }
        }
    }
}
