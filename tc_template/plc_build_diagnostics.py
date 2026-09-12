"""Turn compiler evidence into repair guidance without inventing diagnostics."""
import hashlib
import json
import re


def _has_exact_source_location(item):
    """Return true only for a diagnostic safe to map to a source edit."""
    if not isinstance(item, dict):
        return False
    file = str(item.get('file') or '').strip()
    line = item.get('line')
    return bool(file) and type(line) is int and line > 0


def normalize_compiler_severity(raw):
    """Recover known source errors from older XAE Medium diagnostics.

    Never promote ordinary warnings merely because a project failed. Preserve
    the original level and require a source location and a narrow error form.
    """
    result = dict(raw)
    errors, warnings = raw.get('errors'), raw.get('warnings')
    if not isinstance(errors, list) or not isinstance(warnings, list):
        return result
    promoted, remaining = [], []
    failed = raw.get('failedProjects', raw.get('failed_projects'))
    for item in warnings:
        description = str(item.get('description', '')) if isinstance(item, dict) else ''
        source_error = (isinstance(item, dict) and item.get('file')
            and type(item.get('line')) is int and item['line'] > 0
            and re.search(r"^(?:(?:'.*?'|Expression|Identifier|Type) expected\b|Unexpected token\b|[CT]\d{4}\b|VAR_TEMP declaration not allowed in this place\s*\.?\s*$)", description.strip(), re.I))
        if type(failed) is int and failed > 0 and source_error:
            promoted.append({**item, 'severity': 'error', 'severity_inferred': True,
                             'classification_source': 'source-compiler-diagnostic'})
        else:
            remaining.append(item)
    if promoted:
        result.update(errors=errors + promoted, warnings=remaining,
                      errorCount=len(errors) + len(promoted), warningCount=len(remaining),
                      severity_reclassified=True)
        if raw.get('errorCount', raw.get('error_count')) != len(errors):
            result['diagnostics_complete'] = False
        # Old VSIX versions derived pending solely from failedProjects>0 /
        # errorCount==0, even when their complete warning array held errors.
        # Recover only that exact known contract, never arbitrary pending data.
        legacy_pending = (
            raw.get('diagnosticsPending') is True
            and raw.get('errorSource') == 'dte-error-items-ui-thread'
            and raw.get('message') == 'Build failed, but no compiler error was exposed after 5 UI-thread reads; diagnosticsPending=true.'
            and raw.get('errorReadAttempts') == 5
            and raw.get('diagnosticsAvailable') is True
            and raw.get('errorCount') == 0 and not errors
            and type(raw.get('warningCount')) is int and raw['warningCount'] == len(warnings)
            and all(isinstance(w, dict) for w in warnings)
            and not raw.get('truncated') and not raw.get('diagnosticsTruncated')
            and raw.get('diagnostics_complete') is not False and raw.get('ok') is not False)
        if legacy_pending:
            result.update(diagnosticsPending=False,
                          original_diagnostics_pending=True,
                          original_diagnostic_message=raw['message'],
                          diagnostic_recovery='legacy-medium-errors-reclassified',
                          message='Recovered source errors from the complete legacy warning list; build remains failed.')
    return result


def build_diagnostics(raw):
    raw = normalize_compiler_severity(raw)
    result = dict(raw)
    def get(camel, snake):
        return raw[camel] if camel in raw else raw.get(snake)
    errors = raw.get('errors')
    if isinstance(errors, list) and len(errors) == 1 and isinstance(errors[0], dict) and isinstance(errors[0].get('value'), list):
        errors = errors[0]['value']
    count = get('errorCount', 'error_count')
    failed = get('failedProjects', 'failed_projects')
    available = get('diagnosticsAvailable', 'diagnostics_available')
    if available is None:
        available = raw.get('errorsRead')
    pending = get('diagnosticsPending', 'diagnostics_pending')
    performed = get('buildPerformed', 'build_performed')
    complete = (available is True and pending is not True and performed is True
                and isinstance(errors, list) and all(isinstance(e, dict) for e in errors)
                and type(count) is int and count >= 0 and len(errors) == count
                and type(failed) is int and failed >= 0
                and raw.get('errorCountSource') != 'failed-project-fallback'
                and raw.get('diagnostics_complete') is not False
                and not raw.get('truncated') and not raw.get('diagnosticsTruncated')
                and not (failed > 0 and count == 0))
    verified = complete and count == 0 and failed == 0 and raw.get('ok') is not False and not raw.get('error')
    located = [item for item in errors if _has_exact_source_location(item)] if isinstance(errors, list) else []
    unlocated = [item for item in errors if isinstance(item, dict) and not _has_exact_source_location(item)] \
        if isinstance(errors, list) else []
    # A located error is not enough when the same build also has an
    # unlocated failure: the source edit could mask only one symptom and still
    # leave the actual build blocker untouched.
    repair_allowed = complete and not verified and bool(located) and not unlocated
    classification = (
        'verified_clean' if verified else
        'mixed_diagnostics' if located and unlocated else
        'source_diagnostic' if located else
        'unlocated_build_failure' if complete and not verified else
        'incomplete_build_evidence'
    )
    result.update(compiler_verified=verified, diagnostics_complete=complete,
                  diagnostics_available=available is True,
                  error_count=count, failed_projects=failed,
                  status='succeeded' if verified else ('failed' if complete else 'incomplete'),
                  repair_allowed=repair_allowed,
                  diagnostic_classification=classification,
                  source_diagnostics=located,
                  unlocated_diagnostics=unlocated)
    result['repair_diagnostics'] = []
    for item in errors if isinstance(errors, list) else []:
        if not isinstance(item, dict):
            continue
        result['repair_diagnostics'].append({k: item.get(k) for k in
            ('code', 'description', 'project', 'file', 'line', 'column')})
    result['diagnostic_fingerprint'] = hashlib.sha256(json.dumps(
        result['repair_diagnostics'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result['next_action'] = (
        'Compiler verification passed. Report warnings separately; no online action is authorized.' if verified else
        'Read the exact diagnostic location, fix one root cause with plc_patch, read back, then plc_build. Maximum three repair rounds; stop if unchanged diagnostics recur without progress.' if result['repair_allowed'] else
        'Build failed with at least one diagnostic that has no exact source file and line. Do not patch source or infer the root cause from old service/license messages; obtain fresh diagnostics for the matching project/build first.' if classification in {'unlocated_build_failure', 'mixed_diagnostics'} else
        'Stop source edits: compiler evidence is incomplete or build was blocked. Resolve platform/build/diagnostic collection first; never infer zero errors or retry compilation blindly.')
    return result
