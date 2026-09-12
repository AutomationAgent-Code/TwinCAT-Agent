"""Validate bounded, explicit DATE/DT/TOD literals without target execution."""
import datetime
import re

ALIASES = {'DATE_AND_TIME':'DT','TIME_OF_DAY':'TOD','LDATE_AND_TIME':'LDT','LTIME_OF_DAY':'LTOD'}


def date_literal_error(value):
    prefix, sep, body = value.upper().partition('#')
    kind = ALIASES.get(prefix, {'D':'DATE'}.get(prefix,prefix))
    if not sep or kind not in {'DATE','DT','TOD'}:
        return None
    try:
        if kind == 'TOD':
            match=re.fullmatch(r'(\d+):(\d+):(\d+)(?:\.(\d{1,3}))?',body)
            if not match:
                return 'Unsupported time-of-day literal format.'
            datetime.time(int(match[1]),int(match[2]),int(match[3]))
        else:
            match=re.fullmatch(r'(\d+)-(\d+)-(\d+)' + (r'-(\d+):(\d+):(\d+)' if kind=='DT' else ''),body)
            if not match:
                return 'Unsupported date literal format.'
            parts=[int(part) for part in match.groups()]
            date=datetime.datetime(*parts)
            maximum=datetime.datetime(2106,2,7,6,28,15) if kind=='DT' else datetime.datetime(2106,2,7)
            if not datetime.datetime(1970,1,1)<=date<=maximum:
                return 'Date literal is outside the documented type range.'
    except (ValueError, OverflowError):
        return 'Invalid calendar or clock value in date/time literal.'
    return None
