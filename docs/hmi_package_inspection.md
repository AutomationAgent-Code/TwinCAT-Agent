# HMI package discovery and read-only inspection

`tc_hmi_framework_packages` enumerates archives in each exact project package
folder and returns `package_path`, `package_exists`, `archive_status` and existing
`package_candidates`. Only an existing archive with the project ID/version filename
is selected automatically. A missing archive does not mean an expanded dependency
is uninstalled. No global NuGet-cache path, package spelling or version is guessed.
Use `package_id` (CLI `--package-id`) to filter an exact registered package.
The model-context adapter preserves actual paths rather than dropping them in a
generic summary. Candidate paths are discovery evidence; archive identity is checked
by inspection, not assumed merely from a filename.

`tc_hmi_framework_package_inspect` accepts control, function, framework and resource
archives. It checks the root nuspec, one runtime Manifest and declared Control and
Function description entries, reporting module types/counts and package kind.
Missing descriptions, invalid archives and empty manifests still fail. A function
package is no longer rejected for having zero Control modules. This is package
structure inspection, not runtime execution or a complete resource/schema audit.

The existing installation workflow remains Control-package-only, with its prior
confirmation, dependency and rollback checks. Expanding read-only inspection does
not silently authorize or implement function-only package installation.

Current project verification uses the actual archives referenced by packages.config:
Framework 14.3.360, Functions 14.3.340, Controls 14.4.1. They are under the project's
`Packages/<ID>.<version>/<ID>.<version>.nupkg`, not a guessed `.nuget` cache directory.
