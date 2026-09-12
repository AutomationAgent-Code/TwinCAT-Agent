using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;

namespace TwinCATAgent.Xae
{
    // Deliberately read-only. A future delete protocol must have its own
    // transaction/approval contract, not add an `apply` switch to this probe.
    internal sealed class HmiHierarchyProbeContract
    {
        internal const int MaxRequestChars = 8192;
        internal string SolutionFile;
        internal string ProjectFile;
        internal string ItemFile;

        internal static HmiHierarchyProbeContract Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json) || json.Length > MaxRequestChars)
                throw new ArgumentException("Invalid request size.");
            var serializer = new JavaScriptSerializer { MaxJsonLength = MaxRequestChars, RecursionLimit = 8 };
            var obj = serializer.DeserializeObject(json) as Dictionary<string, object>;
            var keys = new[] { "command", "solution_file", "project_file", "item_file" };
            if (obj == null || obj.Count != keys.Length || keys.Any(k => !obj.ContainsKey(k)) ||
                obj.Values.Any(v => !(v is string)) || (string)obj["command"] != "probe-delete")
                throw new ArgumentException("Only the exact read-only probe-delete request is supported.");
            var result = new HmiHierarchyProbeContract {
                SolutionFile = LocalAbsolutePath((string)obj["solution_file"]),
                ProjectFile = LocalAbsolutePath((string)obj["project_file"]),
                ItemFile = LocalAbsolutePath((string)obj["item_file"])
            };
            if (!Path.GetExtension(result.SolutionFile).Equals(".sln", StringComparison.OrdinalIgnoreCase) ||
                !Path.GetExtension(result.ProjectFile).Equals(".hmiproj", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Expected a .sln and .hmiproj.");
            string root = Path.GetDirectoryName(result.ProjectFile);
            if (!result.ItemFile.StartsWith(root + "\\", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Item must be strictly inside the HMI project directory.");
            string relative = result.ItemFile.Substring(root.Length + 1);
            var protectedRoots = new[] { "Properties", "Server", "Packages", "bin", "obj", ".TwinCATAgent" };
            if (protectedRoots.Contains(relative.Split('\\')[0], StringComparer.OrdinalIgnoreCase))
                throw new ArgumentException("Protected HMI directory.");
            if (!File.Exists(result.SolutionFile) || !File.Exists(result.ProjectFile))
                throw new ArgumentException("Solution/project file does not exist.");
            RejectReparsePoints(result.ProjectFile);
            RejectReparsePoints(result.ItemFile);
            if (Directory.Exists(result.ItemFile))
            {
                if (Directory.EnumerateFileSystemEntries(result.ItemFile).Any())
                    throw new ArgumentException("Only empty folders may be probed for deletion.");
            }
            else if (!File.Exists(result.ItemFile) ||
                !new[] { ".view", ".content", ".usercontrol", ".js", ".css" }.Contains(
                    Path.GetExtension(result.ItemFile), StringComparer.OrdinalIgnoreCase))
                throw new ArgumentException("Missing or unsupported HMI item.");
            return result;
        }

        internal static string LocalAbsolutePath(string path)
        {
            // Reject UNC/device/drive-relative paths and alternate data streams.
            if (string.IsNullOrWhiteSpace(path) || path.Length < 3 || !char.IsLetter(path[0]) ||
                path[1] != ':' || (path[2] != '\\' && path[2] != '/') ||
                path.Substring(2).Contains(':') || path.IndexOfAny(Path.GetInvalidPathChars()) >= 0)
                throw new ArgumentException("Expected a local absolute path.");
            string[] parts = path.Substring(3).Replace('/', '\\').Split('\\');
            if (parts.Any(p => p.Length == 0 || p == "." || p == ".." || p.EndsWith(" ") || p.EndsWith(".")))
                throw new ArgumentException("Ambiguous path is not allowed.");
            return Path.GetFullPath(path);
        }

        private static void RejectReparsePoints(string path)
        {
            for (string cursor = path; !string.IsNullOrEmpty(cursor); cursor = Path.GetDirectoryName(cursor))
                if ((File.GetAttributes(cursor) & FileAttributes.ReparsePoint) != 0)
                    throw new ArgumentException("Reparse points are not allowed.");
        }
    }
}
