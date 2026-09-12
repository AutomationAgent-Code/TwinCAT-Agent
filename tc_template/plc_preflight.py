"""Read-only whole-candidate-set preflight; no write or compiler invocation."""
import hashlib
import json
from .plc_write_context import review_dependencies
from .lint import review_write_candidate


def review_candidates(candidates, call):
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 100:
        raise ValueError('Provide 1..100 complete candidate objects.')
    overlay = {}
    source_chars = 0
    for candidate in candidates:
        if not isinstance(candidate, dict) or any(not isinstance(candidate.get(k), str)
                for k in ('name', 'path', 'declaration', 'implementation')):
            raise ValueError('Each candidate needs name/path/declaration/implementation strings.')
        parts = candidate['path'].split('^')
        if not candidate['declaration'].strip():
            raise ValueError('Complete candidate declaration must not be empty; placeholder preflight is not evidence.')
        if len(parts) < 4 or parts[0] != 'TIPC' or any(not p for p in parts):
            raise ValueError('An exact project-qualified TIPC path is required.')
        if parts[-1].casefold() != candidate['name'].casefold():
            raise ValueError('Candidate name and path must match.')
        source_chars += len(candidate['declaration']) + len(candidate['implementation'])
        if source_chars > 2_000_000:
            raise ValueError('Candidate set exceeds the offline review budget.')
        key = ('^'.join(parts[:3]).casefold(), candidate['name'].casefold())
        if key in overlay:
            raise ValueError('Ambiguous candidate name in the same PLC project.')
        overlay[key] = candidate
    results = []
    live_cache = {}
    for candidate in candidates:
        scope = '^'.join(candidate['path'].split('^')[:3]).casefold()

        def lookup(verb, **args):
            name = args.get('query') if verb == 'find-pou' else args.get('name')
            replacement = overlay.get((scope, str(name).casefold()))
            if replacement and not args.get('method'):
                if verb == 'find-pou':
                    return {'matches': [{'name': replacement['name'], 'path': replacement['path']}], 'total': 1}
                return replacement
            cache_key = (verb, json.dumps(args, sort_keys=True))
            if cache_key not in live_cache:
                live_cache[cache_key] = call(verb, **args)
            return live_cache[cache_key]

        quality = review_write_candidate(candidate)
        if any(str(f.get('rule', '')).startswith('syntax-') for f in quality['blocking_findings']):
            result = {'findings': [], 'dependencies': [], 'unresolved': [],
                      'source': 'candidate', 'compiler_verified': False,
                      'semantic_review': {'status': 'not_checked', 'reason': 'syntax_errors'}}
        else:
            result = review_dependencies(candidate, candidate['path'], lookup, semantic=True)
        # Complete candidates use the same full-source quality rules as writes.
        # This is deliberately conservative: no untrusted caller-supplied
        # baseline can waive findings or become a write permission token.
        result['write_review'] = quality
        result['findings'].extend(quality['blocking_findings'])
        result['advisories'] = quality['advisories']
        from .semantic_evidence import classify
        result['semantic_evidence'] = classify(result['findings'])
        for dependency in result['dependencies']:
            if (scope, dependency['name'].casefold()) in overlay:
                dependency['source'] = 'candidate_overlay'
        results.append({'name': candidate['name'], 'path': candidate['path'],
                        'sha256': hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest(),
                        'approved': quality['approved'] and not any(f.get('severity') == 'error' for f in result['findings']),
                        'review': result})
    approved = all(r['approved'] for r in results)
    return {'status': 'preflight_passed' if approved else 'blocked', 'approved': approved,
            'written': False, 'compiler_verified': False, 'candidates': results,
            'review_contract': 'full_candidate_quality_and_semantics',
            'write_authorized': False,
            'next_action': 'Only the supplied candidate set was reviewed. Revalidate live baselines before writing; this is not a write token or compilation proof.'}
