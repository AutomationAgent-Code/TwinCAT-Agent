using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Web.Script.Serialization;
using TwinCAT.Ads;
using TwinCAT.Ads.TypeSystem;
using TwinCAT.TypeSystem;

internal static class Program
{
    private static Assembly ResolveTwinCatAds(object sender, ResolveEventArgs args)
    {
        if (!new AssemblyName(args.Name).Name.Equals("TwinCAT.Ads", StringComparison.OrdinalIgnoreCase))
            return null;
        string root = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
        string plc = Path.Combine(root, "Beckhoff", "TwinCAT", "3.1", "Components", "Plc", "LacBinaries", "GAC_MSIL", "TwinCAT.Ads");
        if (Directory.Exists(plc))
        {
            string[] files = Directory.GetFiles(plc, "TwinCAT.Ads.dll", SearchOption.AllDirectories);
            if (files.Length > 0) return Assembly.LoadFrom(files[files.Length - 1]);
        }
        string baseDir = Path.Combine(root, "Beckhoff", "TwinCAT", "3.1", "Components", "Base");
        if (Directory.Exists(baseDir))
        {
            string[] files = Directory.GetFiles(baseDir, "TwinCAT.Ads.dll", SearchOption.AllDirectories);
            if (files.Length > 0) return Assembly.LoadFrom(files[files.Length - 1]);
        }
        return null;
    }

    public static int Main(string[] args)
    {
        AppDomain.CurrentDomain.AssemblyResolve += ResolveTwinCatAds;
        return DynamicAdsBridge.Run(args);
    }
}

internal static class DynamicAdsBridge
{
    private static void CollectSymbols(IEnumerable<ISymbol> source, List<ISymbol> found,
                                       string query, string type, bool recursive, int depth, ref int visited)
    {
        foreach (ISymbol item in source)
        {
            if (visited >= 50000) return;
            visited++;
            if (item.InstancePath.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0 &&
                (type.Length == 0 || item.TypeName.IndexOf(type, StringComparison.OrdinalIgnoreCase) >= 0))
                found.Add(item);
            // Arrays are expanded explicitly, never enumerate every element
            // while searching a large machine project.
            if (recursive && depth < 8 && !item.IsPointer && !item.IsReference &&
                !(item.DataType is IArrayType))
                CollectSymbols(item.SubSymbols, found, query, type, true, depth + 1, ref visited);
        }
    }

    private static object Browse(IAdsSymbolLoader loader, Dictionary<string, object> options)
    {
        string parent = Convert.ToString(options["parent"]);
        string query = Convert.ToString(options["query"]);
        string type = Convert.ToString(options["type_filter"]);
        int offset = Math.Max(0, Convert.ToInt32(options["offset"]));
        int limit = Math.Max(1, Math.Min(100, Convert.ToInt32(options["limit"])));
        IEnumerable<ISymbol> source = loader.Symbols;
        if (parent.Length > 0)
        {
            ISymbol root;
            if (!loader.Symbols.TryGetInstance(parent, out root))
                throw new KeyNotFoundException("ADS parent not found: " + parent);
            if (root.IsPointer || root.IsReference) throw new ArgumentException("Pointer expansion is not supported");
            source = root.SubSymbols;
        }
        var found = new List<ISymbol>();
        int visited = 0;
        CollectSymbols(source, found, query, type, parent.Length == 0 && query.Length > 0, 0, ref visited);
        found.Sort((a, b) => StringComparer.OrdinalIgnoreCase.Compare(a.InstancePath, b.InstancePath));
        var items = new List<object>();
        for (int i = offset; i < Math.Min(found.Count, offset + limit); i++) items.Add(Describe(found[i], 0));
        return new Dictionary<string, object> {
            {"ok", true}, {"source", "ads-runtime"}, {"items", items},
            {"total", found.Count}, {"offset", offset}, {"visited", visited},
            {"scan_truncated", visited >= 50000},
            {"next_offset", offset + items.Count < found.Count ? (object)(offset + items.Count) : null},
            {"values_read", false}, {"snapshot_scope", "fresh-request"}
        };
    }
    private static Dictionary<string, object> Describe(ISymbol symbol, int depth)
    {
        var result = new Dictionary<string, object>
        {
            { "name", symbol.InstanceName },
            { "path", symbol.InstancePath },
            { "type", symbol.TypeName },
            { "category", symbol.Category.ToString() },
            { "byte_size", symbol.ByteSize },
            { "read_only", symbol.IsReadOnly },
            { "is_primitive", symbol.IsPrimitiveType },
            { "is_container", symbol.IsContainerType },
            { "is_pointer", symbol.IsPointer },
            { "is_reference", symbol.IsReference },
        };
        var arrayType = symbol.DataType as IArrayType;
        if (arrayType != null)
        {
            var dimensions = new List<object>();
            foreach (IDimension dimension in arrayType.Dimensions)
            {
                dimensions.Add(new Dictionary<string, object>
                {
                    { "lower_bound", dimension.LowerBound },
                    { "element_count", dimension.ElementCount },
                    { "upper_bound", dimension.LowerBound + dimension.ElementCount - 1 },
                });
            }
            result["dimensions"] = dimensions;
            result["element_type"] = arrayType.ElementType.FullName;
        }
        var enumType = symbol.DataType as IEnumType;
        if (enumType != null)
        {
            var values = new List<object>();
            foreach (IEnumValue item in enumType.EnumValues)
            {
                values.Add(new Dictionary<string, object>
                {
                    { "name", item.Name }, { "value", item.Primitive },
                });
            }
            result["enum_values"] = values;
        }
        if (depth > 0 && symbol.SubSymbols.Count > 0)
        {
            var children = new List<object>();
            int count = 0;
            foreach (ISymbol child in symbol.SubSymbols)
            {
                if (count++ >= 256) break;
                children.Add(Describe(child, depth - 1));
            }
            result["children"] = children;
            result["children_truncated"] = symbol.SubSymbols.Count > 256;
        }
        return result;
    }

    private static object JsonValue(object value)
    {
        if (value == null) return null;
        var enumValue = value as IEnumValue;
        if (enumValue != null)
        {
            return new Dictionary<string, object>
            {
                { "name", enumValue.Name }, { "value", enumValue.Primitive },
            };
        }
        var typed = value as IValue;
        if (typed != null)
        {
            object resolved;
            if (typed.TryResolveValue(true, out resolved) && !Object.ReferenceEquals(resolved, value))
                return JsonValue(resolved);
            return new Dictionary<string, object>
            {
                { "dynamic_type", value.GetType().FullName },
                { "display", value.ToString() },
            };
        }
        if (value is DateTime || value is TimeSpan) return value.ToString();
        if (value.GetType().IsPrimitive || value is decimal || value is string) return value;
        return new Dictionary<string, object>
        {
            { "managed_type", value.GetType().FullName },
            { "display", value.ToString() },
        };
    }

    private static void ReadLeaves(ISymbol symbol, Dictionary<string, object> values, ref int count)
    {
        if (count >= 256 || symbol.IsPointer || symbol.IsReference) return;
        if (symbol.IsContainerType && symbol.SubSymbols.Count > 0)
        {
            foreach (ISymbol child in symbol.SubSymbols)
            {
                ReadLeaves(child, values, ref count);
                if (count >= 256) break;
            }
            return;
        }
        IValueSymbol valueSymbol = symbol as IValueSymbol;
        if (valueSymbol != null)
        {
            values[symbol.InstancePath] = JsonValue(valueSymbol.ReadValue());
            count++;
        }
    }

    public static int Run(string[] args)
    {
        var json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
        var output = new Dictionary<string, object>();
        try
        {
            if (args.Length < 3) throw new ArgumentException(
                "usage: TcAdsDynamicProbe.exe <netid> <port> <symbol> [depth] [write-json]");
            string netId = args[0];
            int port = Int32.Parse(args[1]);
            string path = args[2];
            bool metadataOnly = path.StartsWith("@describe:", StringComparison.Ordinal);
            if (metadataOnly) path = path.Substring(10);
            int depth = args.Length > 3 ? Math.Max(0, Math.Min(8, Int32.Parse(args[3]))) : 3;
            using (var client = new TcAdsClient())
            {
                client.Timeout = 5000;
                client.Connect(netId, port);
                IAdsSymbolLoader loader = (IAdsSymbolLoader)SymbolLoaderFactory.Create(
                    client, SymbolLoaderSettings.DefaultDynamic);
                if (path == "@browse")
                {
                    var options = json.Deserialize<Dictionary<string, object>>(args[4]);
                    Console.OutputEncoding = System.Text.Encoding.UTF8;
                    Console.WriteLine(json.Serialize(Browse(loader, options)));
                    return 0;
                }
                ISymbol symbol;
                if (!loader.Symbols.TryGetInstance(path, out symbol))
                    throw new KeyNotFoundException("ADS symbol not found: " + path);
                output["ok"] = true;
                output["net_id"] = netId;
                output["port"] = port;
                output["symbol"] = Describe(symbol, depth);
                if (metadataOnly)
                {
                    output["values_read"] = false;
                    Console.OutputEncoding = System.Text.Encoding.UTF8;
                    Console.WriteLine(json.Serialize(output));
                    return 0;
                }
                var valueSymbol = symbol as IValueSymbol;
                if (valueSymbol == null)
                {
                    output["value"] = null;
                }
                else if (args.Length > 4)
                {
                    if (symbol.IsReadOnly) throw new InvalidOperationException("Symbol is read-only: " + path);
                    if (symbol.IsContainerType || symbol.IsPointer || symbol.IsReference)
                        throw new InvalidOperationException("Whole-object writes are not allowed; write a scalar, string, enum, array element, or struct member path.");
                    object before = valueSymbol.ReadValue();
                    object requested = json.DeserializeObject(args[4]);
                    IEnumType enumType = symbol.DataType as IEnumType;
                    if (enumType != null && requested is string)
                    {
                        bool found = false;
                        foreach (IEnumValue item in enumType.EnumValues)
                        {
                            if (item.Name.Equals((string)requested, StringComparison.OrdinalIgnoreCase))
                            {
                                requested = item.Primitive;
                                found = true;
                                break;
                            }
                        }
                        if (!found) throw new ArgumentException("Unknown enum member: " + requested);
                    }
                    valueSymbol.WriteValue(requested);
                    object readback = valueSymbol.ReadValue();
                    output["before"] = JsonValue(before);
                    output["requested"] = requested;
                    output["readback"] = JsonValue(readback);
                    output["write_verified"] = Object.Equals(
                        Convert.ToString(JsonValue(readback)), Convert.ToString(requested));
                }
                else
                {
                    if (symbol.IsContainerType && symbol.SubSymbols.Count > 0)
                    {
                        var leaves = new Dictionary<string, object>();
                        int leafCount = 0;
                        ReadLeaves(symbol, leaves, ref leafCount);
                        output["leaf_values"] = leaves;
                        output["leaf_count"] = leafCount;
                        output["leaf_values_truncated"] = leafCount >= 256;
                    }
                    else output["value"] = JsonValue(valueSymbol.ReadValue());
                }
            }
            Console.OutputEncoding = System.Text.Encoding.UTF8;
            Console.WriteLine(json.Serialize(output));
            return 0;
        }
        catch (Exception exc)
        {
            output["ok"] = false;
            output["error_type"] = exc.GetType().FullName;
            output["error"] = exc.Message;
            if (exc.InnerException != null) output["inner_error"] = exc.InnerException.Message;
            Console.OutputEncoding = System.Text.Encoding.UTF8;
            Console.WriteLine(json.Serialize(output));
            return 2;
        }
    }
}
