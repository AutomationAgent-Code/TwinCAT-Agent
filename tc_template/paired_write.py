"""Compensated two-area update; COM setters are not an atomic transaction."""
from .plc import normalize_twincat_text
import hashlib


def comparison_text(value):
    """Ignore transport newline differences, never spaces or source tokens."""
    return value.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')


def equivalent(left, right):
    return [comparison_text(v) for v in left] == [comparison_text(v) for v in right]


def write_pair(item, declaration, implementation, baseline):
    attrs = ('DeclarationText', 'ImplementationText')
    def read():
        values = []
        for attr in attrs:
            value = getattr(item, attr)  # No swallowed read errors / empty fallback.
            values.append(normalize_twincat_text(str(value() if callable(value) else value))[0])
        return values
    before = read()
    valid_baseline = (isinstance(baseline, list) and len(baseline) == 2
                      and all(isinstance(v, str) for v in baseline))
    if not valid_baseline or not equivalent(before, baseline):
        areas = ('declaration', 'implementation')
        def hashes(values):
            return {area: hashlib.sha256(comparison_text(value).encode('utf-8')).hexdigest()
                    for area, value in zip(areas, values)}
        return {'status': 'conflict', 'written': False, 'not_executed': True,
                'error': ('Paired source baseline changed; read and review again.' if valid_baseline
                          else 'Paired source baseline is missing or malformed; read and review again.'),
                'baseline_valid': valid_baseline,
                'comparison': 'newline_normalized_v1',
                'expected_hashes': hashes(baseline) if valid_baseline else {},
                'actual_hashes': hashes(before),
                'changed_areas': [area for i, area in enumerate(areas)
                                  if not valid_baseline or comparison_text(before[i]) != comparison_text(baseline[i])]}
    after = [normalize_twincat_text(v)[0] for v in (declaration, implementation)]
    attempted = []
    try:
        for i, attr in enumerate(attrs):
            # Verify both buffers before every setter; do not overwrite an
            # intervening user edit with the second half of the candidate.
            expected = [after[j] if j < i else before[j] for j in range(2)]
            if not equivalent(read(), expected):
                raise RuntimeError('Source changed during paired write')
            attempted.append(i)
            setattr(item, attr, after[i])
        if not equivalent(read(), after):
            raise RuntimeError('Paired source readback mismatch')
        return {'status': 'written', 'written': True, 'verified': True,
                'verification_scope': 'live_text_readback_only', 'compiler_verified': False}
    except Exception as exc:
        try:
            current = read()
            if any(not equivalent([current[i]], [before[i]])
                   and not equivalent([current[i]], [after[i]]) for i in range(2)):
                raise RuntimeError('Concurrent edit: rollback would overwrite unknown content')
            for i in reversed(attempted):
                if not equivalent([current[i]], [before[i]]):
                    setattr(item, attrs[i], before[i])
            if not equivalent(read(), before):
                raise RuntimeError('Rollback readback mismatch')
            return {'status': 'rolled_back', 'written': False, 'rollback_verified': True,
                    'error': str(exc), 'compiler_verified': False}
        except Exception as rollback:
            return {'status': 'uncertain', 'written': 'unknown', 'verified': False,
                    'error': str(exc), 'rollback_error': str(rollback), 'retry_safe': False}
